"""Evidence -> preliminary dimension bridge (Milestone 3C).

Turns evidence already collected by Milestones 2/3A/3B into explicit,
inspectable preliminary dimensions without flattening what the evidence
actually proves. Every dimension carries its provenance (contributing
evidence ids, providers, source references), its truth basis, its
missing/unknown state, and the named formula version that produced it.

This is deliberately NOT the final Product Opportunity Score engine:

- Only formulas already approved in the repository produce a 0-100 value:
  `search_demand_dimension_v1` and `audience_interest_dimension_v1`.
- Marketplace evidence (price, purchase proxy, competition) has no approved
  0-100 formula, so it is bridged as EVIDENCE_PRESENT_UNSCORED with its raw
  observable counts. No score is invented here.
- No POS weights, no ECS thresholds, no RED/YELLOW/GREEN classification.

Truth rules enforced here:

- MISSING (no evidence collected) and UNKNOWN (evidence collected, the
  measurement itself was unavailable) are distinct states, and neither is
  ever converted to 0. Zero appears only where a formula explicitly defines
  it as an observed value (an observed median of 0).
- A dimension value computed by a formula is a derivation: its
  `value_truth_class` is INFERRED even when every input was OBSERVED. The
  inputs' own class is reported separately as `evidence_truth_basis`.
- Etsy review counts stay a purchase PROXY and are never read as sales.
- YouTube views/engagement stay audience interest and are never read as
  purchase evidence.
"""

from dataclasses import dataclass, field
from enum import Enum
from uuid import UUID

from app.domain.enums import TruthClass
from app.domain.models import EvidenceItem
from app.services.marketplace_features import MarketplaceCandidateSummary
from app.services.public_content_features import (
    AUDIENCE_INTEREST_DIMENSION_VERSION,
    PublicContentSummary,
)
from app.services.search_demand_features import (
    SEARCH_DEMAND_DIMENSION_VERSION,
    SearchDemandSummary,
)

PRELIMINARY_DIMENSIONS_VERSION = "preliminary_dimensions_v1"

# Preliminary dimension names are deliberately distinct from the eventual
# ScoreDimensions field names so preliminary features can never be mistaken
# for, or silently wired into, the final POS/ECS engine.
DIM_SEARCH_DEMAND = "preliminary_search_demand"
DIM_AUDIENCE_INTEREST = "preliminary_audience_interest"
DIM_PRICE_EVIDENCE = "preliminary_price_evidence"
DIM_PURCHASE_PROXY = "preliminary_purchase_proxy"
DIM_COMPETITION_FIELD = "preliminary_competition_field"

PRELIMINARY_DIMENSION_NAMES = (
    DIM_SEARCH_DEMAND,
    DIM_AUDIENCE_INTEREST,
    DIM_PRICE_EVIDENCE,
    DIM_PURCHASE_PROXY,
    DIM_COMPETITION_FIELD,
)

# Signal types emitted by the 3A/3B/M2 evidence builders.
SIGNAL_SEARCH_VOLUME = "search_volume"
SIGNAL_LISTING_PRICE = "marketplace_listing_price"
SIGNAL_REVIEW_PROXY = "marketplace_review_count_purchase_proxy"
SIGNAL_COMPETING_LISTING = "marketplace_competing_listing"
SIGNAL_VIDEO_VIEWS = "public_video_view_count"

NO_APPROVED_FORMULA = "no_approved_formula_v1"

# Most-conservative-wins precedence: a dimension can never claim a stronger
# truth class than the weakest evidence that contributed to it.
_TRUTH_PRECEDENCE = {
    TruthClass.UNKNOWN: 3,
    TruthClass.INFERRED: 2,
    TruthClass.ESTIMATED: 1,
    TruthClass.OBSERVED: 0,
}

LIMITATIONS_SEARCH_DEMAND = (
    "Search interest only: never purchases, buyers, revenue, or validation.",
    "Value is a derivation of an observed median via search_demand_dimension_v1, not an observed quantity.",
)

LIMITATIONS_AUDIENCE_INTEREST = (
    "Public content interest only: never purchase evidence, buyers, or revenue.",
    "Value is a derivation of an observed median via audience_interest_dimension_v1, not an observed quantity.",
)

LIMITATIONS_PRICE_EVIDENCE = (
    "Public asking prices of comparable listings, not transaction prices.",
    "No approved 0-100 price formula exists; raw observables are reported unscored.",
)

LIMITATIONS_PURCHASE_PROXY = (
    "Review counts are a PURCHASE PROXY only: never units sold, buyers, or revenue.",
    "Exact competitor sales and revenue are not public and remain UNKNOWN.",
    "No approved 0-100 purchase formula exists; raw observables are reported unscored.",
)

LIMITATIONS_COMPETITION_FIELD = (
    "Listing presence describes the observable competitive field, not competitor performance.",
    "No approved competition scoring exists; 'low competition = good' is not implemented.",
)


class DimensionState(str, Enum):
    """Why a preliminary dimension does or does not carry a value."""

    # An approved, versioned formula produced a 0-100 value.
    SCORED = "SCORED"
    # Evidence exists, but no approved formula converts it to 0-100 yet.
    EVIDENCE_PRESENT_UNSCORED = "EVIDENCE_PRESENT_UNSCORED"
    # Evidence was collected, but the underlying measurement was unavailable.
    UNKNOWN = "UNKNOWN"
    # No evidence was collected at all (capability not run, failed, or capped).
    MISSING = "MISSING"


@dataclass(slots=True, frozen=True)
class PreliminaryDimension:
    """One preliminary dimension for one candidate, with provenance intact."""

    candidate_id: UUID
    name: str
    state: DimensionState
    value: float | None
    # Class of the evidence that fed the dimension (weakest wins). None when
    # no evidence contributed at all.
    evidence_truth_basis: TruthClass | None
    # Class of `value` itself. A formula output is a derivation, so this is
    # INFERRED whenever a value exists — never OBSERVED.
    value_truth_class: TruthClass | None
    formula_version: str
    observed_input_count: int
    unknown_input_count: int
    duplicate_evidence_suppressed: int
    contributing_evidence_ids: tuple[UUID, ...]
    providers: tuple[str, ...]
    source_references: tuple[str, ...]
    limitations: tuple[str, ...]
    missing_reason: str | None = None
    bridge_version: str = PRELIMINARY_DIMENSIONS_VERSION

    @property
    def has_value(self) -> bool:
        return self.value is not None


@dataclass(slots=True, frozen=True)
class CandidatePreliminaryProfile:
    """Every preliminary dimension for one candidate, keyed by name."""

    candidate_id: UUID
    candidate_title: str
    dimensions: dict[str, PreliminaryDimension] = field(default_factory=dict)
    bridge_version: str = PRELIMINARY_DIMENSIONS_VERSION

    def dimension(self, name: str) -> PreliminaryDimension:
        return self.dimensions[name]

    @property
    def present_dimension_count(self) -> int:
        """Dimensions backed by real evidence (scored or unscored)."""
        return sum(
            1
            for d in self.dimensions.values()
            if d.state in (DimensionState.SCORED, DimensionState.EVIDENCE_PRESENT_UNSCORED)
        )


def combine_truth_class(classes: list[TruthClass]) -> TruthClass | None:
    """Weakest (most conservative) class wins; OBSERVED never absorbs INFERRED."""
    if not classes:
        return None
    return max(classes, key=lambda c: _TRUTH_PRECEDENCE[c])


def _dedupe_evidence(items: list[EvidenceItem]) -> tuple[list[EvidenceItem], int]:
    """Collapse re-delivered observations of the same underlying measurement.

    Identity is (signal_type, raw_payload_hash, raw_value) — the same listing
    or keyword measurement arriving twice for one candidate is one
    observation, not two. Items without a payload hash are never collapsed,
    since nothing proves they describe the same thing.
    """
    seen: set[tuple[str, str, str]] = set()
    kept: list[EvidenceItem] = []
    suppressed = 0
    for item in items:
        if item.raw_payload_hash is None:
            kept.append(item)
            continue
        key = (item.signal_type, item.raw_payload_hash, str(item.raw_value))
        if key in seen:
            suppressed += 1
            continue
        seen.add(key)
        kept.append(item)
    return kept, suppressed


def _provenance(items: list[EvidenceItem]) -> tuple[tuple[UUID, ...], tuple[str, ...], tuple[str, ...]]:
    providers = sorted({item.provider for item in items})
    references = sorted({item.source_reference for item in items if item.source_reference})
    return tuple(item.id for item in items), tuple(providers), tuple(references)


def _build_dimension(
    candidate_id: UUID,
    name: str,
    items: list[EvidenceItem],
    value: float | None,
    formula_version: str,
    limitations: tuple[str, ...],
    missing_reason: str | None,
    scored: bool,
) -> PreliminaryDimension:
    deduped, suppressed = _dedupe_evidence(items)
    evidence_ids, providers, references = _provenance(deduped)
    observed = sum(1 for i in deduped if i.truth_class == TruthClass.OBSERVED)
    unknown = sum(1 for i in deduped if i.truth_class == TruthClass.UNKNOWN)
    basis = combine_truth_class([i.truth_class for i in deduped])

    if not deduped:
        state = DimensionState.MISSING
        value = None
    elif value is not None:
        state = DimensionState.SCORED
    elif scored:
        # Evidence arrived but the measurement itself was unavailable. UNKNOWN
        # stays UNKNOWN; it is never rewritten as 0.
        state = DimensionState.UNKNOWN
    else:
        state = DimensionState.EVIDENCE_PRESENT_UNSCORED

    return PreliminaryDimension(
        candidate_id=candidate_id,
        name=name,
        state=state,
        value=value,
        evidence_truth_basis=basis,
        # A formula output is derived, never an observation.
        value_truth_class=TruthClass.INFERRED if value is not None else None,
        formula_version=formula_version,
        observed_input_count=observed,
        unknown_input_count=unknown,
        duplicate_evidence_suppressed=suppressed,
        contributing_evidence_ids=evidence_ids,
        providers=providers,
        source_references=references,
        limitations=limitations,
        missing_reason=missing_reason if state == DimensionState.MISSING else None,
    )


def _by_signal(evidence: list[EvidenceItem], signal_type: str) -> list[EvidenceItem]:
    return [item for item in evidence if item.signal_type == signal_type]


def build_preliminary_profile(
    candidate_id: UUID,
    candidate_title: str,
    evidence: list[EvidenceItem],
    search_demand: SearchDemandSummary | None,
    marketplace: MarketplaceCandidateSummary | None,
    public_content: PublicContentSummary | None,
    missing_reasons: dict[str, str] | None = None,
) -> CandidatePreliminaryProfile:
    """Bridge one candidate's stored evidence into preliminary dimensions.

    A summary of None means that capability produced nothing for this
    candidate (never run, failed, or capped): its dimensions are MISSING with
    a stated reason, never zero.
    """
    reasons = missing_reasons or {}
    dimensions: dict[str, PreliminaryDimension] = {}

    # --- search demand: approved formula search_demand_dimension_v1 --------
    dimensions[DIM_SEARCH_DEMAND] = _build_dimension(
        candidate_id,
        DIM_SEARCH_DEMAND,
        _by_signal(evidence, SIGNAL_SEARCH_VOLUME),
        value=search_demand.search_demand_dimension if search_demand else None,
        formula_version=SEARCH_DEMAND_DIMENSION_VERSION,
        limitations=LIMITATIONS_SEARCH_DEMAND,
        missing_reason=reasons.get(DIM_SEARCH_DEMAND, "no_search_demand_evidence_collected"),
        scored=True,
    )

    # --- audience interest: approved audience_interest_dimension_v1 --------
    # Only OBSERVED view-count evidence feeds this. The INFERRED engagement
    # ratio is deliberately excluded so a derived value cannot be counted as
    # an observed input.
    dimensions[DIM_AUDIENCE_INTEREST] = _build_dimension(
        candidate_id,
        DIM_AUDIENCE_INTEREST,
        _by_signal(evidence, SIGNAL_VIDEO_VIEWS),
        value=public_content.audience_interest_dimension if public_content else None,
        formula_version=AUDIENCE_INTEREST_DIMENSION_VERSION,
        limitations=LIMITATIONS_AUDIENCE_INTEREST,
        missing_reason=reasons.get(DIM_AUDIENCE_INTEREST, "no_public_content_evidence_collected"),
        scored=True,
    )

    # --- marketplace: no approved 0-100 formula; unscored observables ------
    marketplace_missing = reasons.get(
        DIM_PRICE_EVIDENCE, "no_marketplace_evidence_collected"
    )
    dimensions[DIM_PRICE_EVIDENCE] = _build_dimension(
        candidate_id,
        DIM_PRICE_EVIDENCE,
        _by_signal(evidence, SIGNAL_LISTING_PRICE),
        value=None,
        formula_version=NO_APPROVED_FORMULA,
        limitations=LIMITATIONS_PRICE_EVIDENCE,
        missing_reason=marketplace_missing,
        scored=False,
    )
    dimensions[DIM_PURCHASE_PROXY] = _build_dimension(
        candidate_id,
        DIM_PURCHASE_PROXY,
        _by_signal(evidence, SIGNAL_REVIEW_PROXY),
        value=None,
        formula_version=NO_APPROVED_FORMULA,
        limitations=LIMITATIONS_PURCHASE_PROXY,
        missing_reason=marketplace_missing,
        scored=False,
    )
    dimensions[DIM_COMPETITION_FIELD] = _build_dimension(
        candidate_id,
        DIM_COMPETITION_FIELD,
        _by_signal(evidence, SIGNAL_COMPETING_LISTING),
        value=None,
        formula_version=NO_APPROVED_FORMULA,
        limitations=LIMITATIONS_COMPETITION_FIELD,
        missing_reason=marketplace_missing,
        scored=False,
    )

    # The price feature extractor applies this milestone's inclusion rules
    # (a price with no currency is not a comparable), so its count is
    # authoritative over a raw evidence tally. It stays a count, never a score.
    if marketplace is not None:
        dimensions[DIM_PRICE_EVIDENCE] = _replace_counts(
            dimensions[DIM_PRICE_EVIDENCE],
            observed=marketplace.price.relevant_paid_comparable_count,
        )

    return CandidatePreliminaryProfile(
        candidate_id=candidate_id,
        candidate_title=candidate_title,
        dimensions=dimensions,
    )


def _replace_counts(dimension: PreliminaryDimension, observed: int) -> PreliminaryDimension:
    """Restate a dimension's observed-input count from its feature summary."""
    if observed == dimension.observed_input_count:
        return dimension
    return PreliminaryDimension(
        candidate_id=dimension.candidate_id,
        name=dimension.name,
        state=dimension.state,
        value=dimension.value,
        evidence_truth_basis=dimension.evidence_truth_basis,
        value_truth_class=dimension.value_truth_class,
        formula_version=dimension.formula_version,
        observed_input_count=observed,
        unknown_input_count=dimension.unknown_input_count,
        duplicate_evidence_suppressed=dimension.duplicate_evidence_suppressed,
        contributing_evidence_ids=dimension.contributing_evidence_ids,
        providers=dimension.providers,
        source_references=dimension.source_references,
        limitations=dimension.limitations,
        missing_reason=dimension.missing_reason,
    )
