"""Milestone 6A-1: Evidence Confidence inputs and `ecs_v1`.

Implements the aggregation contract approved in `SPEC_STEP_7_8.md` §6 and §6.1
**exactly**. This module invents no mapping, no weight, no threshold and no
default. Everything it does is either fixed by §6.1 or supplied by one of the
three named V1 policy sets below.

WHAT EVIDENCE CONFIDENCE MEASURES

ECS measures the EVIDENCE, never the opportunity. Per §7 the split is absolute:
the Opportunity Score reads the VALUES of observations; Evidence Confidence
reads the PROPERTIES of the evidence record set — its coverage, sample, source,
recency, agreement and capability health. No fact is read by both. A measured
low value is a POS input; a missing measurement is an ECS input.

No ECS input reads the magnitude of any observation. There is no view count, no
search volume, no price and no review count anywhere in this module.

WHY THE LEGACY MODEL WAS REPLACED RATHER THAN POPULATED

`EvidenceItem` carries eight confidence fields with defaults of 0.5 and 1.0, of
which exactly two are ever assigned in production. Populating the rest would
require inventing a per-provider constant, and `cross_source_agreement` is
incoherent as a per-item field because agreement is a property of a SET. Those
eight fields are deprecated by §6 and are **never read here**.

NO DEFAULTS, EVER

§6 hard rule 1: no ECS input may take an invented constant. Missingness resolves
into exactly three cases and only the first leaves the denominator:

  (a) not applicable BY CONTRACT  -> excluded from that component's own average
  (b) expected but unavailable    -> stays in, scores 0.0, lowers ECS
  (c) opportunity observation UNKNOWN -> lowers coverage, never becomes a POS
      value of any kind

The anti-inflation rule follows: it is impossible for ECS to RISE because
evidence went missing. Case (b) never becomes case (a).

BLOCKED IS NOT LOW

A blocked ECS is `None`, never `0.0`. It may not be rendered, stored, compared
or classified as a zero. A low ECS is a real measurement about weak evidence; a
blocked ECS is the absence of a measurement.

NOT SCORING

No POS, no weights, no thresholds, no RED/YELLOW/GREEN, no classification. This
module produces one confidence number and the components behind it. It neither
imports nor reaches `app.services.scoring`, which remains quarantined.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from uuid import UUID

from app.domain.enums import EvidencePurpose
from app.domain.models import EvidenceItem

ECS_VERSION = "ecs_v1"

# ---------------------------------------------------------------- V1 policy
#
# Three approved policy sets. Each is a NAMED, VERSIONED, EXPLICITLY
# UNCALIBRATED V1 assumption chosen by inspection. None has been validated
# against outcome data, and none may be described as empirically validated.
# A future recalibration ships as `_v2` rather than editing these in place,
# so an earlier result stays attributable to the policy that produced it.

SAMPLE_FLOORS_VERSION = "sample_floors_v1"
FRESHNESS_WINDOWS_VERSION = "freshness_windows_v1"
PROVENANCE_DIRECTNESS_VERSION = "provenance_directness_v1"


class Capability(str, Enum):
    """The three approved collection capabilities (ARCHITECTURE.md V1 providers)."""

    SEARCH_DEMAND = "search_demand"
    MARKETPLACE = "marketplace"
    PUBLIC_CONTENT = "public_content"


# U-4 — the observation count at which a dimension's sample stops being thin.
# UNCALIBRATED V1 policy.
SAMPLE_FLOORS: dict[Capability, int] = {
    Capability.SEARCH_DEMAND: 5,  # measured keywords
    Capability.MARKETPLACE: 10,  # unique comparable listings
    Capability.PUBLIC_CONTENT: 10,  # unique videos
}

# U-5 — how long an observation stays fully current. UNCALIBRATED V1 policy.
FRESHNESS_WINDOWS: dict[Capability, timedelta] = {
    Capability.SEARCH_DEMAND: timedelta(days=30),
    Capability.MARKETPLACE: timedelta(days=7),
    Capability.PUBLIC_CONTENT: timedelta(days=7),
}


class DirectnessClass(str, Enum):
    """How directly a record was obtained. Ordered most to least direct."""

    DIRECT_API = "DIRECT_API"
    VALIDATED_CACHE = "VALIDATED_CACHE"
    DETERMINISTIC_DERIVATION = "DETERMINISTIC_DERIVATION"
    INDIRECT_PROVIDER_MEDIATED = "INDIRECT_PROVIDER_MEDIATED"
    UNKNOWN = "UNKNOWN"


# U-7 — the provenance_directness table. UNCALIBRATED V1 policy.
PROVENANCE_DIRECTNESS: dict[DirectnessClass, float] = {
    DirectnessClass.DIRECT_API: 1.00,
    DirectnessClass.VALIDATED_CACHE: 0.90,
    DirectnessClass.DETERMINISTIC_DERIVATION: 0.80,
    DirectnessClass.INDIRECT_PROVIDER_MEDIATED: 0.60,
    # Not a penalty chosen for effect: an unrecognised collection method is
    # provenance we cannot vouch for at all, and §6 forbids treating that as a
    # waiver. It is the weakest value in the table, never an exemption.
    DirectnessClass.UNKNOWN: 0.00,
}

# The approved U-7 values are keyed by collection-method CLASS alone, so
# `provenance_directness_v1` is degenerate in signal_type and purpose: those
# are accepted by the lookup and ignored. The lookup keeps its full
# (signal_type, purpose, collection_method) signature anyway, so a future
# table that does differentiate by signal becomes `_v2` without changing a
# single caller.
_COLLECTION_METHOD_CLASS: dict[str, DirectnessClass] = {
    "official_api": DirectnessClass.DIRECT_API,
    "cache": DirectnessClass.VALIDATED_CACHE,
}


# ----------------------------------------------------------- §6.1 constants
#
# Fixed normatively by the specification, NOT policy values.

# §6.1: an evenly spaced ordinal over an ordered three-state enum. No free
# parameter, so this is not an open decision.
_HEALTH_CLEAN = 1.0
_HEALTH_PARTIAL = 0.5
_HEALTH_FAILED = 0.0


class EcsDimension(str, Enum):
    """The six Step 7 dimensions (§2). ECS reads their states, never values."""

    SEARCH_DEMAND = "deep_search_demand"  # D1
    PURCHASE_PROXY_EVIDENCE = "deep_purchase_proxy_evidence"  # D2
    PRICE_EVIDENCE = "deep_price_evidence"  # D3
    COMPETITION_STRUCTURE = "deep_competition_structure"  # D4
    AUDIENCE_ATTENTION = "deep_audience_attention"  # D5
    CHANNEL_REACH = "deep_channel_reach"  # D6


# §6: D3 is optional and is excluded from BOTH sides of dimension_coverage.
REQUIRED_DIMENSIONS: tuple[EcsDimension, ...] = (
    EcsDimension.SEARCH_DEMAND,
    EcsDimension.PURCHASE_PROXY_EVIDENCE,
    EcsDimension.COMPETITION_STRUCTURE,
    EcsDimension.AUDIENCE_ATTENTION,
    EcsDimension.CHANNEL_REACH,
)

# §6.1: read from each dimension's own contract in §2, never chosen. D1-D5 are
# each served by one capability; D6 draws on all three.
DIMENSION_CAPABILITIES: dict[EcsDimension, tuple[Capability, ...]] = {
    EcsDimension.SEARCH_DEMAND: (Capability.SEARCH_DEMAND,),
    EcsDimension.PURCHASE_PROXY_EVIDENCE: (Capability.MARKETPLACE,),
    EcsDimension.PRICE_EVIDENCE: (Capability.MARKETPLACE,),
    EcsDimension.COMPETITION_STRUCTURE: (Capability.MARKETPLACE,),
    EcsDimension.AUDIENCE_ATTENTION: (Capability.PUBLIC_CONTENT,),
    EcsDimension.CHANNEL_REACH: (
        Capability.SEARCH_DEMAND,
        Capability.MARKETPLACE,
        Capability.PUBLIC_CONTENT,
    ),
}

EXPECTED_SURFACES: dict[EcsDimension, int] = {
    dimension: len(capabilities)
    for dimension, capabilities in DIMENSION_CAPABILITIES.items()
}


class CapabilityHealth(str, Enum):
    """The three-state health §6.1 maps to an ordinal."""

    CLEAN = "CLEAN"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    # A capability nobody asked for was never attempted, so "how did the
    # attempt go" does not apply to it. Case (a), excluded from the health
    # average; the cost of not collecting shows up in dimension_coverage.
    NOT_ATTEMPTED = "NOT_ATTEMPTED"


# Orchestration's status vocabulary, mapped onto the three health states.
_STATUS_HEALTH: dict[str, CapabilityHealth] = {
    "COMPLETE": CapabilityHealth.CLEAN,
    "PARTIAL": CapabilityHealth.PARTIAL,
    "FAILED": CapabilityHealth.FAILED,
    "PROVIDER_FAILED": CapabilityHealth.FAILED,
    "UNEXPECTED_PROVIDER_ERROR": CapabilityHealth.FAILED,
    "NOT_REQUESTED": CapabilityHealth.NOT_ATTEMPTED,
    "PENDING": CapabilityHealth.NOT_ATTEMPTED,
}


class ComponentName(str, Enum):
    DIMENSION_COVERAGE = "dimension_coverage"
    SAMPLE_ADEQUACY = "sample_adequacy"
    PROVENANCE_DIRECTNESS = "provenance_directness"
    CORROBORATION_BREADTH = "corroboration_breadth"
    FRESHNESS = "freshness"
    CAPABILITY_HEALTH = "capability_health"
    CONFLICT_RATE = "conflict_rate"


COMPONENT_ORDER: tuple[ComponentName, ...] = (
    ComponentName.DIMENSION_COVERAGE,
    ComponentName.SAMPLE_ADEQUACY,
    ComponentName.PROVENANCE_DIRECTNESS,
    ComponentName.CORROBORATION_BREADTH,
    ComponentName.FRESHNESS,
    ComponentName.CAPABILITY_HEALTH,
    ComponentName.CONFLICT_RATE,
)

# §6.1: these can never be excluded by any path. Each is a question that a
# required Step 7 dimension always answers -- if the dimension is missing, the
# answer is "no evidence", which is a 0.0, never an inapplicable question.
NEVER_EXCLUDABLE: frozenset[ComponentName] = frozenset(
    {
        ComponentName.DIMENSION_COVERAGE,
        ComponentName.CORROBORATION_BREADTH,
        ComponentName.CAPABILITY_HEALTH,
    }
)


class Applicability(str, Enum):
    """Whether a component entered the top-level mean, and why."""

    COUNTED = "COUNTED"
    # Every unit for this component was case-(a) excluded by contract.
    NOT_APPLICABLE = "NOT_APPLICABLE"


class EcsState(str, Enum):
    COMPUTED = "COMPUTED"
    # Not a low score. The absence of a score.
    BLOCKED = "BLOCKED"
    SCOPE_MISMATCH = "SCOPE_MISMATCH"


STATE_BOUNDARIES: dict[EcsState, str] = {
    EcsState.COMPUTED: (
        "This measures the evidence, not the opportunity. A low value says the "
        "evidence is thin, stale, narrow or contested. It says nothing about "
        "whether the opportunity is attractive."
    ),
    EcsState.BLOCKED: (
        "Evidence Confidence could not be computed. This is not a confidence of "
        "zero and must never be rendered, stored or compared as one."
    ),
    EcsState.SCOPE_MISMATCH: (
        "The supplied inputs belong to another candidate or research run and "
        "were refused unread. Nothing here describes either scope."
    ),
}

LIMITATIONS: tuple[str, ...] = (
    "Evidence Confidence measures the quality and completeness of the evidence "
    "record set. It is never a judgement about the opportunity itself, and no "
    "component reads the magnitude of any observation.",
    "The sample floors, freshness windows and provenance-directness values are "
    "unvalidated V1 policy assumptions chosen by inspection and calibrated "
    "against no outcome data. They are not empirically validated.",
    "`ecs_v1` is an unweighted mean. Equal weighting is not a finding that the "
    "components are equally important; it is the absence of evidence that they "
    "are not.",
    "A blocked result is the absence of a measurement, never a confidence of "
    "zero.",
    "Cache reuse is only visible for search demand. Marketplace and public "
    "content report the provider's own collection method even when a result "
    "was reused from cache, so those records are currently scored as direct "
    "API observations. This OVER-credits their provenance. Explicit cache "
    "provenance is 6B's responsibility (SPEC_STEP_7_8.md §1.1); until it "
    "lands, `provenance_directness` is an upper bound for those two "
    "capabilities, not a measurement.",
)


# ------------------------------------------------------------------ inputs


@dataclass(slots=True, frozen=True)
class DimensionObservation:
    """What ECS needs to know about one Step 7 dimension.

    Deliberately a SUMMARY, never the dimension's values: ECS reads properties
    of the evidence set, so a dimension hands over counts and states and keeps
    its measurements to itself.
    """

    dimension: EcsDimension
    # Did the dimension reach a scoreable state at all?
    scoreable: bool
    # Sample counts keyed by the capability that produced each one.
    #
    # Not a scalar, because a dimension may draw on more than one capability
    # and their counts are in incompatible units. D6 spans all three: a keyword
    # count, a listing count and a video count, measured against three
    # different floors. One integer cannot stand for all three without being
    # compared to floors it does not belong to.
    #
    # A capability the dimension draws on but which is absent from this tuple
    # is expected-but-unreported -- case (b): it contributes 0.0 to the
    # dimension's adequacy and is never excluded from it.
    #
    # Canonically ordered on construction, so the order a caller supplies
    # counts in cannot change any emitted value.
    sample_sizes_by_capability: tuple[tuple[Capability, int], ...]
    # Distinct provider/platform surfaces that contributed.
    distinct_surfaces: int
    observation_count: int
    conflicting_observation_count: int

    def __post_init__(self) -> None:
        expected = DIMENSION_CAPABILITIES[self.dimension]
        seen: set[Capability] = set()
        for capability, count in self.sample_sizes_by_capability:
            if capability not in expected:
                raise ValueError(
                    f"{self.dimension.value} does not draw on "
                    f"{capability.value}; a count from a capability the "
                    "dimension does not use is a caller error, not evidence"
                )
            if capability in seen:
                raise ValueError(
                    f"duplicate sample count for {capability.value} on "
                    f"{self.dimension.value}"
                )
            if count < 0:
                raise ValueError(
                    f"negative sample count for {capability.value} on "
                    f"{self.dimension.value}"
                )
            seen.add(capability)
        object.__setattr__(
            self,
            "sample_sizes_by_capability",
            tuple(
                sorted(self.sample_sizes_by_capability, key=lambda pair: pair[0].value)
            ),
        )

    def sample_size_for(self, capability: Capability) -> int | None:
        """This capability's reported count, or None if it reported none."""
        for reported, count in self.sample_sizes_by_capability:
            if reported is capability:
                return count
        return None


def single_capability_sample(
    dimension: EcsDimension, count: int
) -> tuple[tuple[Capability, int], ...]:
    """Sample counts for a dimension that draws on exactly one capability.

    The scalar shorthand is available only where it is unambiguous. Calling it
    for a multi-capability dimension raises rather than silently comparing one
    integer against several unrelated floors.
    """
    capabilities = DIMENSION_CAPABILITIES[dimension]
    if len(capabilities) != 1:
        raise ValueError(
            f"{dimension.value} draws on {len(capabilities)} capabilities; "
            "supply a count per capability instead of one scalar"
        )
    return ((capabilities[0], count),)


@dataclass(slots=True, frozen=True)
class CapabilityReport:
    """One capability's collection outcome, as orchestration already records it."""

    capability: Capability
    status: str


# ----------------------------------------------------------------- outputs


@dataclass(slots=True, frozen=True)
class EcsComponent:
    """One component score, with everything needed to recompute it by hand."""

    name: ComponentName
    # None only when applicability is NOT_APPLICABLE.
    score: float | None
    applicability: Applicability
    # Why it was excluded, when it was. Always None when COUNTED.
    exclusion_clause: str | None
    # How many units (dimensions or records) the component averaged over, and
    # how many were case-(a) excluded from that average.
    units_counted: int
    units_excluded: int


@dataclass(slots=True, frozen=True)
class EcsProvenance:
    candidate_id: UUID
    research_run_id: UUID | None
    evidence_ids: tuple[UUID, ...]
    capabilities: tuple[str, ...]
    dimensions: tuple[str, ...]


@dataclass(slots=True, frozen=True)
class EvidenceConfidenceResult:
    """Milestone 6A-1 result for one candidate and one evidence scope."""

    candidate_id: UUID
    research_run_id: UUID | None
    state: EcsState
    state_boundary: str
    # None whenever state is not COMPUTED. Never 0.0 as a stand-in.
    evidence_confidence: float | None
    blocked_reason: str | None

    components: tuple[EcsComponent, ...]
    # The denominator, emitted so the mean can be recomputed by hand.
    counted_components: tuple[ComponentName, ...]
    not_applicable_components: tuple[ComponentName, ...]

    provenance: EcsProvenance
    limitations: tuple[str, ...] = LIMITATIONS
    version: str = ECS_VERSION
    sample_floors_version: str = SAMPLE_FLOORS_VERSION
    freshness_windows_version: str = FRESHNESS_WINDOWS_VERSION
    provenance_directness_version: str = PROVENANCE_DIRECTNESS_VERSION


# --------------------------------------------------------------- internals


def _id_sort_key(value: object) -> tuple[str, str]:
    """Total order for identifiers without coercing their stored values."""
    return (str(value), type(value).__name__)


def capability_health(status: str) -> CapabilityHealth:
    """Map an orchestration status onto the three-state health enum.

    An unrecognised status is NOT silently treated as healthy: it is FAILED,
    because a status this module cannot interpret is a collection history it
    cannot vouch for.
    """
    return _STATUS_HEALTH.get(status, CapabilityHealth.FAILED)


def directness_class(collection_method: str | None) -> DirectnessClass:
    """Classify a record's collection method. Unrecognised means UNKNOWN."""
    if collection_method is None:
        return DirectnessClass.UNKNOWN
    return _COLLECTION_METHOD_CLASS.get(collection_method, DirectnessClass.UNKNOWN)


def provenance_directness(
    signal_type: str, purpose: EvidencePurpose, collection_method: str | None
) -> float:
    """Look up `provenance_directness_v1`.

    `signal_type` and `purpose` are accepted and deliberately unused: the
    approved V1 table is keyed by collection-method class alone. Keeping them in
    the signature means a `_v2` table that differentiates by signal needs no
    caller change.
    """
    del signal_type, purpose
    return PROVENANCE_DIRECTNESS[directness_class(collection_method)]


def record_capability(item: EvidenceItem) -> Capability | None:
    """Which capability produced this record, by signal-type family.

    None means the signal type is unrecognised, so no freshness window applies
    to it. That is case (b), not a waiver.
    """
    signal = item.signal_type
    if signal == "search_volume":
        return Capability.SEARCH_DEMAND
    if signal.startswith("marketplace_"):
        return Capability.MARKETPLACE
    if signal.startswith("public_"):
        return Capability.PUBLIC_CONTENT
    return None


def _freshness_score(item: EvidenceItem, now: datetime) -> float:
    """Position within the declared window: 1.0 at retrieval, 0.0 at the edge.

    Only `retrieved_at` is read. `collected_at` is when this system stored the
    record, not when the observation was obtained from the provider; using it as
    a fallback would let storage time masquerade as evidence recency. A record
    with no `retrieved_at` has un-evidenced recency and scores 0.0.
    """
    retrieved = item.retrieved_at
    if retrieved is None:
        return 0.0
    capability = record_capability(item)
    if capability is None:
        return 0.0
    window = FRESHNESS_WINDOWS[capability]
    age = now - retrieved
    if age <= timedelta(0):
        return 1.0
    if age >= window:
        return 0.0
    return 1.0 - (age / window)


def _capability_adequacy(
    observation: DimensionObservation, capability: Capability
) -> float:
    """One capability's sample adequacy within a dimension, against its floor.

    Each count is measured against the floor for the capability that produced
    it, never against another capability's floor.
    """
    count = observation.sample_size_for(capability)
    if count is None:
        # Expected from this capability and not reported. Case (b): 0.0, and it
        # stays in the dimension's average.
        return 0.0
    return min(1.0, count / SAMPLE_FLOORS[capability])


def _in_scope(
    item: EvidenceItem, candidate_id: UUID, research_run_id: UUID | None
) -> bool:
    """Candidate and run ownership, matching Milestones 5A and 5B exactly."""
    if item.candidate_id != candidate_id:
        return False
    if research_run_id is not None and item.research_run_id != research_run_id:
        return False
    if research_run_id is None and item.research_run_id is not None:
        return False
    return True


def _component(
    name: ComponentName,
    scores: list[float],
    excluded: int,
    exclusion_clause: str | None,
) -> EcsComponent:
    """Average a component's units, honouring §6.1 step 3.

    A component with no counted units is NOT_APPLICABLE — unless §6.1 forbids
    excluding it, in which case it scores 0.0 and stays in the mean. Nothing is
    ever defaulted to a convenient constant.
    """
    if scores:
        return EcsComponent(
            name=name,
            score=round(sum(scores) / len(scores), 6),
            applicability=Applicability.COUNTED,
            exclusion_clause=None,
            units_counted=len(scores),
            units_excluded=excluded,
        )
    if name in NEVER_EXCLUDABLE:
        return EcsComponent(
            name=name,
            score=0.0,
            applicability=Applicability.COUNTED,
            exclusion_clause=None,
            units_counted=0,
            units_excluded=excluded,
        )
    return EcsComponent(
        name=name,
        score=None,
        applicability=Applicability.NOT_APPLICABLE,
        exclusion_clause=exclusion_clause,
        units_counted=0,
        units_excluded=excluded,
    )


def _blocked(
    candidate_id: UUID,
    research_run_id: UUID | None,
    state: EcsState,
    reason: str,
    provenance: EcsProvenance,
) -> EvidenceConfidenceResult:
    return EvidenceConfidenceResult(
        candidate_id=candidate_id,
        research_run_id=research_run_id,
        state=state,
        state_boundary=STATE_BOUNDARIES[state],
        evidence_confidence=None,
        blocked_reason=reason,
        components=(),
        counted_components=(),
        not_applicable_components=(),
        provenance=provenance,
    )


# -------------------------------------------------------------- derivation


def compute_evidence_confidence(
    candidate_id: UUID,
    dimensions: list[DimensionObservation],
    capabilities: list[CapabilityReport],
    evidence: list[EvidenceItem],
    research_run_id: UUID | None = None,
    now: datetime | None = None,
) -> EvidenceConfidenceResult:
    """Compute `ecs_v1` for one candidate and one scope.

    Deterministic and pure: the same inputs yield the same number whatever order
    they arrive in. No provider call, no network, no persistence, no LLM, and no
    Opportunity Score.

    `dimensions` are the Step 7 dimension summaries (6B/6C supply these; this
    slice does not collect). `capabilities` are the collection outcomes
    orchestration already records. Every evidence record is scope-verified
    before it is read.
    """
    moment = now or datetime.now(UTC)

    by_dimension = {d.dimension: d for d in dimensions}
    reported = {c.capability: capability_health(c.status) for c in capabilities}

    in_scope = [
        item for item in evidence if _in_scope(item, candidate_id, research_run_id)
    ]
    provenance = EcsProvenance(
        candidate_id=candidate_id,
        research_run_id=research_run_id,
        evidence_ids=tuple(sorted(item.id for item in in_scope)),
        capabilities=tuple(sorted(c.capability.value for c in capabilities)),
        dimensions=tuple(sorted(d.dimension.value for d in dimensions)),
    )

    # A supplied dimension or capability from another scope is a caller error,
    # not evidence. Refuse unread rather than silently mixing scopes.
    if len(in_scope) != len(evidence):
        return _blocked(
            candidate_id,
            research_run_id,
            EcsState.SCOPE_MISMATCH,
            "evidence_out_of_scope_for_candidate_or_run",
            EcsProvenance(
                candidate_id=candidate_id,
                research_run_id=research_run_id,
                evidence_ids=(),
                capabilities=(),
                dimensions=(),
            ),
        )

    # BLOCKED: a capability that some required dimension depends on has no
    # outcome at all, so the collection history is unverifiable (§6.1 step 4).
    needed: set[Capability] = set()
    for dimension in REQUIRED_DIMENSIONS:
        needed.update(DIMENSION_CAPABILITIES[dimension])
    absent = sorted(c.value for c in needed - set(reported))
    if absent:
        return _blocked(
            candidate_id,
            research_run_id,
            EcsState.BLOCKED,
            f"capability_outcome_absent:{','.join(absent)}",
            provenance,
        )

    # 1. dimension_coverage — required Step 7 dimensions only (D3 excluded).
    coverage_scores = [
        1.0
        if (
            (observation := by_dimension.get(dimension)) is not None
            and observation.scoreable
        )
        else 0.0
        for dimension in REQUIRED_DIMENSIONS
    ]
    coverage = _component(
        ComponentName.DIMENSION_COVERAGE, coverage_scores, 0, None
    )

    # 2. sample_adequacy — applicable to every dimension that reached a
    #    scoreable state. A scoreable dimension that cannot report its sample
    #    scores 0.0 and stays in (case b).
    sample_scores: list[float] = []
    sample_excluded = 0
    for dimension in REQUIRED_DIMENSIONS:
        observation = by_dimension.get(dimension)
        if observation is None:
            # A required dimension that was never supplied is missing expected
            # evidence, not an inapplicable question. Excluding it would let
            # dropping the worst dimension RAISE the component.
            sample_scores.append(0.0)
            continue
        if not observation.scoreable:
            # Present but not scoreable: §6 makes this component applicable to
            # dimensions that reached a scoreable state, so the question really
            # does not apply, and the cost is carried by dimension_coverage.
            sample_excluded += 1
            continue
        # Each capability the dimension draws on is measured against its own
        # floor, and the dimension's adequacy is the mean of those. For a
        # single-capability dimension that is just its own ratio; for D6 it is
        # three ratios in their own units, never one count against three
        # unrelated floors. No number enters that the policy set does not
        # already contain.
        per_capability = [
            _capability_adequacy(observation, capability)
            for capability in DIMENSION_CAPABILITIES[dimension]
        ]
        sample_scores.append(sum(per_capability) / len(per_capability))
    sample = _component(
        ComponentName.SAMPLE_ADEQUACY,
        sample_scores,
        sample_excluded,
        "no_required_dimension_reached_a_scoreable_state",
    )

    # 3. provenance_directness — applicable to every contributing record.
    directness_scores = [
        provenance_directness(item.signal_type, item.purpose, item.collection_method)
        for item in in_scope
    ]
    directness = _component(
        ComponentName.PROVENANCE_DIRECTNESS,
        directness_scores,
        0,
        "no_evidence_record_contributed",
    )

    # 4. corroboration_breadth — expected surfaces read from §2, never chosen.
    breadth_scores: list[float] = []
    for dimension in REQUIRED_DIMENSIONS:
        observation = by_dimension.get(dimension)
        if observation is None:
            # Missing expected corroboration, not an inapplicable question.
            breadth_scores.append(0.0)
            continue
        breadth_scores.append(
            min(1.0, observation.distinct_surfaces / EXPECTED_SURFACES[dimension])
        )
    # Every required dimension contributes, so nothing is ever excluded here
    # and the component is never empty.
    breadth = _component(
        ComponentName.CORROBORATION_BREADTH, breadth_scores, 0, None
    )

    # 5. freshness — applicable to every contributing record.
    freshness_scores = [_freshness_score(item, moment) for item in in_scope]
    freshness = _component(
        ComponentName.FRESHNESS,
        freshness_scores,
        0,
        "no_evidence_record_contributed",
    )

    # 6. capability_health — never excludable. A capability nobody requested is
    #    case (a) within the average; if that empties it, the component still
    #    counts, at 0.0: no attempted collection is no evidence of health.
    health_scores: list[float] = []
    health_excluded = 0
    for capability in sorted(reported, key=lambda c: c.value):
        health = reported[capability]
        if health is CapabilityHealth.NOT_ATTEMPTED:
            health_excluded += 1
            continue
        health_scores.append(
            {
                CapabilityHealth.CLEAN: _HEALTH_CLEAN,
                CapabilityHealth.PARTIAL: _HEALTH_PARTIAL,
                CapabilityHealth.FAILED: _HEALTH_FAILED,
            }[health]
        )
    health_component = _component(
        ComponentName.CAPABILITY_HEALTH, health_scores, health_excluded, None
    )

    # 7. conflict_rate — applicable to dimensions with at least one
    #    observation. Direction inverted here, once.
    conflict_scores: list[float] = []
    conflict_excluded = 0
    for dimension in REQUIRED_DIMENSIONS:
        observation = by_dimension.get(dimension)
        if observation is None:
            # Missing expected evidence cannot be uncontested evidence.
            conflict_scores.append(0.0)
            continue
        if observation.observation_count <= 0:
            # Present but carrying nothing to agree or disagree about. That is
            # a genuinely inapplicable question, so it is case (a).
            conflict_excluded += 1
            continue
        rate = observation.conflicting_observation_count / observation.observation_count
        conflict_scores.append(1.0 - min(1.0, rate))
    conflict = _component(
        ComponentName.CONFLICT_RATE,
        conflict_scores,
        conflict_excluded,
        "no_required_dimension_carried_an_observation",
    )

    components = (
        coverage,
        sample,
        directness,
        breadth,
        freshness,
        health_component,
        conflict,
    )
    counted = [c for c in components if c.applicability is Applicability.COUNTED]
    # Unreachable while dimension_coverage and capability_health are
    # never-excludable, but the guard states the invariant rather than assuming
    # it: an empty denominator would be a blocked result, never a zero.
    if not counted:
        return _blocked(
            candidate_id,
            research_run_id,
            EcsState.BLOCKED,
            "no_applicable_confidence_component",
            provenance,
        )

    total = sum(c.score for c in counted if c.score is not None)
    confidence = round(100 * total / len(counted), 2)

    return EvidenceConfidenceResult(
        candidate_id=candidate_id,
        research_run_id=research_run_id,
        state=EcsState.COMPUTED,
        state_boundary=STATE_BOUNDARIES[EcsState.COMPUTED],
        evidence_confidence=confidence,
        blocked_reason=None,
        components=components,
        counted_components=tuple(c.name for c in counted),
        not_applicable_components=tuple(
            c.name
            for c in components
            if c.applicability is Applicability.NOT_APPLICABLE
        ),
        provenance=provenance,
    )
