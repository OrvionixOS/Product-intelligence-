"""Milestone 6C — Step 7b: the deep research boundary object.

SPEC_STEP_7_8.md §11 requires one uniformly scoped object per candidate: the
thing Step 8 consumes instead of reaching into the evidence store itself.

Four rules carry this slice.

**Uniform, verified scope.** One candidate, one run, both checked before any
evidence is read. A mixed-scope collection is refused unread rather than
filtered, because filtering silently answers a question nobody asked.

**Evidence and explicit states, never precomputed composites (§4).** Every
dimension reports raw observables, a `DimensionState`, its truth basis, its
own formula version, its conflict count and its sample size. Step 8 cannot be
handed a number this module invented, because this module computes none.

**Required is not the same as POS-eligible (§2, §5).** Five dimensions are
required and three are contextual-only, and the two sets overlap: competition
structure and channel reach are REQUIRED — a dossier without them is
incomplete, and ECS coverage counts them — yet both are POS-ineligible in V1
because their derivations refuse magnitude. Collapsing the two ideas into one
flag is how contextual evidence ends up inside a score.

**`preliminary_rank` is excluded.** Rank is selection-only. Carrying it here
would let triage policy leak into scoring, so it appears nowhere on this
object, in any form.

No POS. No scoring, no classification, no thresholds, no weights. This module
derives the dossier and computes Evidence Confidence over it; it produces no
opportunity magnitude of any kind.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime
from enum import Enum
from uuid import UUID

from app.domain.enums import TruthClass
from app.domain.models import EvidenceItem
from app.services.audience_attention import extract_audience_attention
from app.services.buyer_reach import extract_buyer_reach
from app.services.competition_opportunity import extract_competition_opportunity
from app.services.marketplace_listing_view import quantile
from app.services.evidence_confidence import (
    DIMENSION_CAPABILITIES,
    REQUIRED_DIMENSIONS,
    Capability,
    CapabilityReport,
    DimensionObservation,
    EcsDimension,
    EvidenceConfidenceResult,
    compute_evidence_confidence,
    record_capability,
)
from app.services.preliminary_dimensions import DimensionState
from app.services.price_evidence import extract_price_evidence
from app.services.purchase_evidence import extract_purchase_evidence
from app.services.research_orchestration import CapabilityOutcome

DEEP_RESEARCH_VERSION = "deep_research_v1"
# D1 is the one Step 7 dimension with no existing derivation to reuse: §2
# requires a distribution over observed keyword volumes and forbids a single
# 0-100 value, which is exactly what the 3C preliminary bridge produces.
DEEP_SEARCH_DEMAND_VERSION = "deep_search_demand_v1"

# §5 and §2. Required governs the dossier and ECS coverage; POS-eligibility
# governs what may ever carry a score magnitude. They are different questions
# with different answers, so they are different sets.
CONTEXTUAL_ONLY: frozenset[EcsDimension] = frozenset(
    {
        # Opposed readings with no ordering over the patterns.
        EcsDimension.COMPETITION_STRUCTURE,
        # Relevance permanently capped at INFERRED; refuses magnitude.
        EcsDimension.CHANNEL_REACH,
        # Asking prices without transaction prices or willingness-to-pay
        # cannot establish opportunity strength.
        EcsDimension.PRICE_EVIDENCE,
    }
)

# The closed V1 POS surface (§5). Named here so the boundary object can state
# which dimensions are eligible; no POS value is computed anywhere in 6C.
POS_ELIGIBLE: frozenset[EcsDimension] = frozenset(
    {
        EcsDimension.SEARCH_DEMAND,
        EcsDimension.PURCHASE_PROXY_EVIDENCE,
        EcsDimension.AUDIENCE_ATTENTION,
    }
)

DIMENSION_ORDER: tuple[EcsDimension, ...] = (
    EcsDimension.SEARCH_DEMAND,
    EcsDimension.PURCHASE_PROXY_EVIDENCE,
    EcsDimension.PRICE_EVIDENCE,
    EcsDimension.COMPETITION_STRUCTURE,
    EcsDimension.AUDIENCE_ATTENTION,
    EcsDimension.CHANNEL_REACH,
)

MISSING_NO_EVIDENCE = "no_evidence_collected_for_dimension"

SIGNAL_SEARCH_VOLUME = "search_volume"


class DeepResearchState(str, Enum):
    """Whether this boundary object is something Step 8 can consume.

    Structural, never a judgement about the opportunity. The spec names this
    field (§11) without enumerating it; these three follow from §8's missing
    data semantics and from the scope rule §4 and §11 both state.
    """

    # Every required dimension reached a scoreable state.
    COMPLETE = "COMPLETE"
    # At least one required dimension is MISSING or UNKNOWN. The dossier is
    # still emitted in full: §8 keeps what was computable and names what was
    # not, rather than discarding the candidate.
    PARTIAL = "PARTIAL"
    # Inputs belonged to another candidate or run and were refused unread.
    SCOPE_MISMATCH = "SCOPE_MISMATCH"


STATE_BOUNDARIES: dict[DeepResearchState, str] = {
    DeepResearchState.COMPLETE: (
        "Every required dimension reached a scoreable state. This describes "
        "the evidence record, not the opportunity: a complete dossier can "
        "still describe a candidate nobody wants."
    ),
    DeepResearchState.PARTIAL: (
        "At least one required dimension is missing or unknown. What was "
        "computable is retained and what was not is named. This is not a low "
        "score and must never be rendered as one."
    ),
    DeepResearchState.SCOPE_MISMATCH: (
        "The supplied inputs belong to another candidate or research run and "
        "were refused unread. Nothing here describes either scope."
    ),
}

LIMITATIONS: tuple[str, ...] = (
    "This is a dossier, not a score. No dimension carries an opportunity "
    "magnitude, and every value here is an observable or a state.",
    "Required and POS-eligible are different properties. Competition "
    "structure and channel reach are required and contextual-only: they "
    "complete the dossier and count toward evidence coverage, and they may "
    "never contribute a score magnitude.",
    "A conflict count is a count of disagreeing observations, never a "
    "judgement about which observation is right. Conflicting records are "
    "excluded from derivation and reported, never silently reconciled.",
    "A sample size of None means the dimension could not report one. It is "
    "never zero, and zero never means absent.",
)


# ----------------------------------------------------------------- contract


@dataclass(slots=True, frozen=True)
class DeepDimension:
    """§4's Step 8 input contract for one dimension."""

    dimension_name: str
    state: DimensionState
    # Required when state is MISSING.
    missing_reason: str | None
    # Raw observables, never scores.
    observed_features: dict
    # Weakest contributing class. None when nothing contributed.
    truth_basis: TruthClass | None
    evidence_ids: tuple[UUID, ...]
    formula_version: str
    conflict_count: int
    # None means unknown, never 0.
    sample_size: int | None
    # §2/§5. Stated on the dimension so a consumer cannot mistake one for the
    # other, and so "required" can never be read as "may feed a score".
    required: bool
    contextual_only: bool
    pos_eligible: bool
    # Distinct observed entities per capability, which is what sample
    # adequacy needs: `sample_size` above is §4's single figure, and for a
    # dimension spanning several capabilities those counts are in
    # incompatible units, so the per-capability breakdown is carried too.
    sample_sizes_by_capability: tuple[tuple[Capability, int], ...] = ()

    @property
    def scoreable(self) -> bool:
        """§8: evidence present is scoreable; unknown and missing are not."""
        return self.state in (
            DimensionState.SCORED,
            DimensionState.EVIDENCE_PRESENT_UNSCORED,
        )


@dataclass(slots=True, frozen=True)
class DeepResearchResult:
    """§11's boundary object. One candidate, one run, uniformly scoped."""

    candidate_id: UUID
    research_run_id: UUID
    deep_pass_id: UUID
    dimensions: dict[str, DeepDimension]
    capability_outcomes: tuple[CapabilityOutcome, ...]
    evidence_ids: tuple[UUID, ...]
    providers: tuple[str, ...]
    platforms: tuple[str, ...]
    component_versions: dict[str, str]
    state: DeepResearchState
    state_boundary: str
    # 6A-1 wired in: Evidence Confidence over this dossier's own dimensions
    # and capability outcomes. None only when scope was refused.
    evidence_confidence: EvidenceConfidenceResult | None = None
    version: str = DEEP_RESEARCH_VERSION
    limitations: tuple[str, ...] = field(default=LIMITATIONS)

    @property
    def missing_dimensions(self) -> tuple[str, ...]:
        """§8's `excluded_dimensions`: what was unavailable, and why."""
        return tuple(
            name
            for name, dimension in self.dimensions.items()
            if not dimension.scoreable
        )


# --------------------------------------------------------------- derivation


def _id_sort_key(value: object) -> tuple[str, str]:
    """Total order for identifiers without coercing their stored values."""
    return (str(value), type(value).__name__)


def _in_scope(
    item: EvidenceItem, candidate_id: UUID, research_run_id: UUID
) -> bool:
    return (
        item.candidate_id == candidate_id
        and item.research_run_id == research_run_id
    )


def _entity_key(item: EvidenceItem) -> str | None:
    """What one record is an observation OF.

    Conflict is disagreement about one thing, so it can only be detected
    against an identity. A record whose payload names no entity cannot be
    compared with another and is never counted as conflicting.
    """
    payload = item.raw_payload or {}
    for field_name in ("keyword", "listing_id", "video_id"):
        value = payload.get(field_name)
        if value is not None:
            return f"{field_name}:{value}"
    return None


def _conflicting_ids(items: list[EvidenceItem]) -> frozenset[UUID]:
    """Records that disagree with another OBSERVED record about one entity.

    §2 states this per dimension in the same shape each time: two OBSERVED
    values for one entity, in one run, conflict. Identical repeat observations
    are not conflicts — those are duplicates, and the 5B fingerprint rule
    already collapses them.
    """
    by_entity: dict[tuple[str, str], dict[object, list[UUID]]] = {}
    for item in items:
        if item.truth_class is not TruthClass.OBSERVED:
            continue
        key = _entity_key(item)
        if key is None:
            continue
        values = by_entity.setdefault((item.signal_type, key), {})
        values.setdefault(item.raw_value, []).append(item.id)

    conflicting: set[UUID] = set()
    for values in by_entity.values():
        if len(values) > 1:
            for ids in values.values():
                conflicting.update(ids)
    return frozenset(conflicting)


def _observed_entities(items: list[EvidenceItem]) -> set[str]:
    return {
        key
        for item in items
        if item.truth_class is TruthClass.OBSERVED
        and (key := _entity_key(item)) is not None
    }


def _samples_by_capability(
    dimension: EcsDimension, items: list[EvidenceItem]
) -> tuple[tuple[Capability, int], ...]:
    """Distinct observed entities per capability the dimension draws on.

    Only capabilities that actually reported are listed: an absent entry means
    "this capability reported no count", which sample adequacy treats as 0.0
    rather than excluding — so silence is never free.
    """
    counts: dict[Capability, set[str]] = {}
    for item in items:
        capability = record_capability(item)
        if capability is None or capability not in DIMENSION_CAPABILITIES[dimension]:
            continue
        if item.truth_class is not TruthClass.OBSERVED:
            continue
        key = _entity_key(item)
        if key is None:
            continue
        counts.setdefault(capability, set()).add(key)
    return tuple(
        (capability, len(entities))
        for capability, entities in sorted(
            counts.items(), key=lambda pair: pair[0].value
        )
    )


def _features_of(result: object) -> dict:
    """Raw observables only.

    `value` lives on the result, never inside `features`, and this refuses to
    carry it even if that ever changes: §4's contract is observables, and a
    composite that slipped in here would be a number Step 8 did not derive.
    """
    features = getattr(result, "features", None)
    if features is None or not is_dataclass(features):
        return {}
    return {
        key: value
        for key, value in asdict(features).items()
        if key not in ("value", "score", "normalized_value")
    }


def _dimension_from_result(
    dimension: EcsDimension,
    result: object,
    items: list[EvidenceItem],
    formula_version: str,
) -> DeepDimension:
    conflicting = _conflicting_ids(items)
    observed = _observed_entities(items)
    provenance = getattr(result, "provenance", None)
    evidence_ids = tuple(
        sorted(getattr(provenance, "evidence_ids", ()) or (), key=_id_sort_key)
    )
    return DeepDimension(
        dimension_name=dimension.value,
        state=getattr(result, "state"),
        missing_reason=getattr(result, "missing_reason", None),
        observed_features=_features_of(result),
        truth_basis=getattr(result, "evidence_truth_basis", None),
        evidence_ids=evidence_ids,
        formula_version=formula_version,
        conflict_count=len(conflicting),
        # None, never 0: a dimension with no observed entity could not report
        # a sample, which is not the same as having observed none.
        sample_size=len(observed) or None,
        required=dimension in REQUIRED_DIMENSIONS,
        contextual_only=dimension in CONTEXTUAL_ONLY,
        pos_eligible=dimension in POS_ELIGIBLE,
        sample_sizes_by_capability=_samples_by_capability(dimension, items),
    )


def _deep_search_demand(
    items: list[EvidenceItem], missing_reason: str | None
) -> DeepDimension:
    """D1. A distribution, never a single 0-100 value (§2, §5 R-3).

    The 3C bridge produces `search_demand_dimension`, a 0-100 composite. §2
    forbids that shape here, so this derives the distribution directly:
    median observed volume, how many keywords carried a measurement, how many
    were queried. Conflicting keywords are excluded from the median and
    reported, never averaged away.
    """
    conflicting = _conflicting_ids(items)
    usable = [
        item
        for item in items
        if item.id not in conflicting and item.truth_class is TruthClass.OBSERVED
    ]
    volumes = sorted(
        float(item.raw_value)
        for item in usable
        if isinstance(item.raw_value, (int, float))
    )
    keywords_queried = len({key for item in items if (key := _entity_key(item))})
    keywords_measured = len(_observed_entities(usable))

    if not items:
        state = DimensionState.MISSING
        features: dict = {}
    elif not volumes:
        # Queried, and the provider returned no measurement. UNKNOWN, not zero.
        state = DimensionState.UNKNOWN
        features = {
            "keywords_queried": keywords_queried,
            "keywords_measured": 0,
        }
    else:
        # All three quartiles come from the one deterministic quantile function
        # 4A and 4F already share, so "Q25" means the same thing in every
        # dimension rather than depending on which module computed it. Rounded
        # to 4dp exactly as those two do.
        state = DimensionState.EVIDENCE_PRESENT_UNSCORED
        features = {
            "q25_search_volume": round(quantile(volumes, 0.25), 4),
            "median_search_volume": round(quantile(volumes, 0.5), 4),
            "q75_search_volume": round(quantile(volumes, 0.75), 4),
            "keywords_queried": keywords_queried,
            "keywords_measured": keywords_measured,
            "lowest_observed_search_volume": volumes[0],
            "highest_observed_search_volume": volumes[-1],
        }

    return DeepDimension(
        dimension_name=EcsDimension.SEARCH_DEMAND.value,
        state=state,
        missing_reason=(
            (missing_reason or MISSING_NO_EVIDENCE)
            if state is DimensionState.MISSING
            else None
        ),
        observed_features=features,
        # A derived aggregate is INFERRED even when every input is OBSERVED.
        truth_basis=TruthClass.OBSERVED if usable else None,
        evidence_ids=tuple(sorted((item.id for item in items), key=_id_sort_key)),
        formula_version=DEEP_SEARCH_DEMAND_VERSION,
        conflict_count=len(conflicting),
        sample_size=keywords_measured or None,
        required=EcsDimension.SEARCH_DEMAND in REQUIRED_DIMENSIONS,
        contextual_only=EcsDimension.SEARCH_DEMAND in CONTEXTUAL_ONLY,
        pos_eligible=EcsDimension.SEARCH_DEMAND in POS_ELIGIBLE,
        sample_sizes_by_capability=_samples_by_capability(
            EcsDimension.SEARCH_DEMAND, items
        ),
    )


def _capability_items(
    evidence: list[EvidenceItem], capability: Capability
) -> list[EvidenceItem]:
    return [item for item in evidence if record_capability(item) is capability]


def _missing_reasons(outcomes: tuple[CapabilityOutcome, ...]) -> dict[str, str]:
    return {
        outcome.capability: outcome.failure_reason
        for outcome in outcomes
        if not outcome.produced_evidence and outcome.failure_reason
    }


def build_deep_research_result(
    candidate_id: UUID,
    research_run_id: UUID,
    deep_pass_id: UUID,
    evidence: list[EvidenceItem],
    capability_outcomes: tuple[CapabilityOutcome, ...] | list[CapabilityOutcome],
    now: datetime | None = None,
) -> DeepResearchResult:
    """Derive the Step 7 boundary object for one candidate and one run.

    `evidence` is the union of cheap-pass and deep-pass records for this
    scope: §1 derives the Step 7 dimensions from the enlarged set, not from
    the deep pass alone.

    Deterministic and pure: same evidence set, same object, whatever order it
    arrives in. No provider call, no network, no persistence, no LLM.
    """
    outcomes = tuple(capability_outcomes)

    # §4 and §11: verify both ids before reading anything. Refused unread,
    # never filtered — filtering would silently answer a different question.
    if any(not _in_scope(item, candidate_id, research_run_id) for item in evidence):
        return DeepResearchResult(
            candidate_id=candidate_id,
            research_run_id=research_run_id,
            deep_pass_id=deep_pass_id,
            dimensions={},
            capability_outcomes=outcomes,
            evidence_ids=(),
            providers=(),
            platforms=(),
            component_versions={},
            state=DeepResearchState.SCOPE_MISMATCH,
            state_boundary=STATE_BOUNDARIES[DeepResearchState.SCOPE_MISMATCH],
        )

    reasons = _missing_reasons(outcomes)
    search_items = [
        item for item in evidence if item.signal_type == SIGNAL_SEARCH_VOLUME
    ]
    marketplace_items = _capability_items(evidence, Capability.MARKETPLACE)
    content_items = _capability_items(evidence, Capability.PUBLIC_CONTENT)

    purchase = extract_purchase_evidence(candidate_id, evidence)
    price = extract_price_evidence(candidate_id, evidence)
    competition = extract_competition_opportunity(
        candidate_id=candidate_id, evidence=evidence, missing_reasons=reasons
    )
    attention = extract_audience_attention(
        candidate_id=candidate_id, evidence=evidence, missing_reasons=reasons
    )
    reach = extract_buyer_reach(
        candidate_id=candidate_id,
        evidence=evidence,
        missing_reasons=reasons,
        research_run_id=research_run_id,
        now=now,
    )

    # Deliberately NOT written in emission order: `DIMENSION_ORDER` below is
    # what fixes the order of the emitted dossier, and writing this mapping in
    # that same order would let the canonical sequence quietly stop mattering.
    built = {
        EcsDimension.CHANNEL_REACH: _dimension_from_result(
            EcsDimension.CHANNEL_REACH, reach, evidence, reach.version
        ),
        EcsDimension.AUDIENCE_ATTENTION: _dimension_from_result(
            EcsDimension.AUDIENCE_ATTENTION,
            attention,
            content_items,
            attention.version,
        ),
        EcsDimension.COMPETITION_STRUCTURE: _dimension_from_result(
            EcsDimension.COMPETITION_STRUCTURE,
            competition,
            marketplace_items,
            competition.version,
        ),
        EcsDimension.PRICE_EVIDENCE: _dimension_from_result(
            EcsDimension.PRICE_EVIDENCE, price, marketplace_items, price.version
        ),
        EcsDimension.PURCHASE_PROXY_EVIDENCE: _dimension_from_result(
            EcsDimension.PURCHASE_PROXY_EVIDENCE,
            purchase,
            marketplace_items,
            purchase.version,
        ),
        EcsDimension.SEARCH_DEMAND: _deep_search_demand(
            search_items, reasons.get(Capability.SEARCH_DEMAND.value)
        ),
    }
    dimensions: dict[str, DeepDimension] = {
        dimension.value: built[dimension] for dimension in DIMENSION_ORDER
    }

    in_scope_sorted = sorted(evidence, key=lambda item: _id_sort_key(item.id))
    confidence = compute_evidence_confidence(
        candidate_id=candidate_id,
        dimensions=[
            DimensionObservation(
                dimension=dimension,
                scoreable=built[dimension].scoreable,
                sample_sizes_by_capability=built[dimension].sample_sizes_by_capability,
                distinct_surfaces=_distinct_surfaces(dimension, evidence),
                observation_count=len(built[dimension].evidence_ids),
                conflicting_observation_count=built[dimension].conflict_count,
            )
            for dimension in DIMENSION_ORDER
        ],
        capabilities=[
            CapabilityReport(capability=capability, status=status)
            for capability, status in _capability_statuses(outcomes)
        ],
        evidence=in_scope_sorted,
        research_run_id=research_run_id,
        now=now,
    )

    required_unscoreable = any(
        not built[dimension].scoreable for dimension in REQUIRED_DIMENSIONS
    )
    state = (
        DeepResearchState.PARTIAL
        if required_unscoreable
        else DeepResearchState.COMPLETE
    )

    return DeepResearchResult(
        candidate_id=candidate_id,
        research_run_id=research_run_id,
        deep_pass_id=deep_pass_id,
        dimensions=dimensions,
        capability_outcomes=outcomes,
        evidence_ids=tuple(item.id for item in in_scope_sorted),
        providers=tuple(sorted({item.provider for item in evidence if item.provider})),
        platforms=tuple(sorted({item.platform for item in evidence if item.platform})),
        component_versions={
            "deep_research": DEEP_RESEARCH_VERSION,
            EcsDimension.SEARCH_DEMAND.value: DEEP_SEARCH_DEMAND_VERSION,
            EcsDimension.PURCHASE_PROXY_EVIDENCE.value: purchase.version,
            EcsDimension.PRICE_EVIDENCE.value: price.version,
            EcsDimension.COMPETITION_STRUCTURE.value: competition.version,
            EcsDimension.AUDIENCE_ATTENTION.value: attention.version,
            EcsDimension.CHANNEL_REACH.value: reach.version,
        },
        state=state,
        state_boundary=STATE_BOUNDARIES[state],
        evidence_confidence=confidence,
    )


def _distinct_surfaces(dimension: EcsDimension, evidence: list[EvidenceItem]) -> int:
    """How many of the dimension's expected capabilities actually reported."""
    expected = DIMENSION_CAPABILITIES[dimension]
    return len(
        {
            capability
            for item in evidence
            if (capability := record_capability(item)) in expected
            and item.truth_class is TruthClass.OBSERVED
        }
    )


def _capability_statuses(
    outcomes: tuple[CapabilityOutcome, ...],
) -> tuple[tuple[Capability, str], ...]:
    """Capability outcomes in the vocabulary Evidence Confidence reads."""
    by_name = {outcome.capability: outcome.status for outcome in outcomes}
    return tuple(
        (capability, by_name[capability.value])
        for capability in Capability
        if capability.value in by_name
    )
