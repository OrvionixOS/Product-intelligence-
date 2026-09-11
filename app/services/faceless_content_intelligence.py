"""Milestone 5A: robust creator-relative outlier evidence.

This module is a pure derivation over public-content evidence that has already
been collected by Milestone 3B. It makes no provider calls, opens no network
connections, writes no persistence records, and does not replace
``content_outlier_v1``.

The existing 3B content-outlier ratio remains exactly as it was. This module
adds a separate, versioned derivation:

    robust_content_outlier_v1

For each creator with at least ``MIN_CREATOR_SAMPLE`` unique videos carrying
an OBSERVED view count, the creator baseline is the median observed view
count. For a video with a positive observed view count and a positive
baseline, the relative score is exactly:

    log2(video_views / creator_median_views)

The score is a content-performance comparison with the creator's collected
sample. It is not a prediction, a probability, a demand measure, or a
commercial score. No universal outlier threshold is imposed here.

The evidence record is authoritative. A value present only in ``raw_payload``
never becomes an observation when the evidence record is UNKNOWN. Unknown
view counts remain UNKNOWN, observed zero remains an observed zero, and a
zero creator baseline produces a structured non-scoreable result rather than
an epsilon-adjusted value.

Only the identity fields needed to associate a stored view observation with a
video and creator are read from the payload: ``video_id`` and ``channel_id``.
Publication time and all channel aggregate metrics are deliberately not read.
"""

from dataclasses import dataclass, field
from enum import Enum
import math
from statistics import median
from uuid import UUID

from app.domain.enums import EvidencePurpose, TruthClass
from app.domain.models import EvidenceItem
from app.services.preliminary_dimensions import DimensionState

ROBUST_CONTENT_OUTLIER_VERSION = "robust_content_outlier_v1"
# Versioned independently from the existing content_outlier_v1 derivation.
FACELESS_CONTENT_INTELLIGENCE_VERSION = "faceless_content_intelligence_v1"

DIM_ROBUST_CONTENT_OUTLIER = "preliminary_robust_content_outlier"
SIGNAL_VIDEO_VIEWS = "public_video_view_count"
MIN_CREATOR_SAMPLE = 3
# Alias matching the public-content feature naming used by Milestone 3B.
MIN_CHANNEL_SAMPLE = MIN_CREATOR_SAMPLE

LIMITATIONS = (
    "This is content-performance evidence relative to a creator's collected sample, not a viral prediction.",
    "A public view is a playback event, not a person, buyer, purchase, or purchase intent.",
    "The sampled videos are provider-ordered public-content observations, not the whole content field.",
    "Only OBSERVED view-count evidence contributes; UNKNOWN view counts remain UNKNOWN and are never zero-filled.",
    "The creator baseline is a V1 deterministic heuristic and is not calibrated against outcome data.",
    "No score here establishes demand, conversion, market size, sales, revenue, or commercial success.",
)


class RobustOutlierObservationState(str, Enum):
    """Why one stored video observation does or does not receive a score."""

    SCORED = "SCORED"
    UNKNOWN_VIEW_COUNT = "UNKNOWN_VIEW_COUNT"
    MISSING_VIDEO_ID = "MISSING_VIDEO_ID"
    MISSING_CHANNEL_ID = "MISSING_CHANNEL_ID"
    BASELINE_UNAVAILABLE = "BASELINE_UNAVAILABLE"
    BASELINE_ZERO = "BASELINE_ZERO"
    VIDEO_VIEW_COUNT_ZERO = "VIDEO_VIEW_COUNT_ZERO"
    INVALID_OBSERVED_VIEW_COUNT = "INVALID_OBSERVED_VIEW_COUNT"
    CONFLICTING_OBSERVED_VIEW_COUNTS = "CONFLICTING_OBSERVED_VIEW_COUNTS"


@dataclass(slots=True, frozen=True)
class CreatorBaseline:
    """The observed-view baseline for one creator in one scoped run."""

    channel_id: str
    observed_video_ids: tuple[str, ...]
    observed_view_counts: tuple[int, ...]
    sample_size: int
    median_views: float
    state: str
    evidence_ids: tuple[UUID, ...]


@dataclass(slots=True, frozen=True)
class RobustOutlierObservation:
    """One candidate-scoped content-performance comparison.

    ``relative_score`` is present only for ``SCORED`` observations. The
    observation keeps a structured state for every other case so missing,
    unknown, zero, and insufficient-baseline facts cannot be collapsed.
    """

    candidate_id: UUID
    video_id: str | None
    channel_id: str | None
    evidence_ids: tuple[UUID, ...]
    source_truth_class: TruthClass
    view_count: int | None
    creator_sample_size: int
    creator_median_views: float | None
    relative_score: float | None
    state: RobustOutlierObservationState
    formula_version: str = ROBUST_CONTENT_OUTLIER_VERSION

    @property
    def scoreable(self) -> bool:
        return self.state == RobustOutlierObservationState.SCORED


@dataclass(slots=True, frozen=True)
class RobustOutlierProvenance:
    """Canonical lineage for one candidate/run derivation."""

    candidate_id: UUID
    research_run_id: UUID | None
    evidence_ids: tuple[UUID, ...]
    video_ids: tuple[str, ...]
    channel_ids: tuple[str, ...]
    providers: tuple[str, ...]
    platforms: tuple[str, ...]
    source_truth_classes: tuple[str, ...]
    duplicate_evidence_suppressed: int


@dataclass(slots=True, frozen=True)
class RobustContentIntelligenceResult:
    """Milestone 5A result for one candidate and one evidence scope."""

    candidate_id: UUID
    research_run_id: UUID | None
    state: DimensionState
    # This derivation deliberately has no candidate-level numeric value.
    value: None
    observations: tuple[RobustOutlierObservation, ...]
    creator_baselines: tuple[CreatorBaseline, ...]
    provenance: RobustOutlierProvenance
    observed_video_count: int
    unknown_view_video_count: int
    videos_without_channel_id: int
    videos_without_video_id: int
    creators_with_baseline: int
    zero_baseline_creator_count: int
    scoreable_observation_count: int
    non_scoreable_observation_count: int
    missing_reason: str | None = None
    dimension_name: str = DIM_ROBUST_CONTENT_OUTLIER
    version: str = FACELESS_CONTENT_INTELLIGENCE_VERSION
    formula_version: str = ROBUST_CONTENT_OUTLIER_VERSION
    limitations: tuple[str, ...] = LIMITATIONS


@dataclass(slots=True)
class _VideoAccumulator:
    """Internal identity-preserving accumulator for one video."""

    key: tuple[str, str] | tuple[str, UUID]
    video_id: str | None
    evidence_ids: list[UUID] = field(default_factory=list)
    channel_ids: set[str] = field(default_factory=set)
    observed_values: set[int] = field(default_factory=set)
    observed_value_evidence_ids: dict[int, list[UUID]] = field(default_factory=dict)
    unknown_evidence_ids: list[UUID] = field(default_factory=list)
    invalid_observed_evidence_ids: list[UUID] = field(default_factory=list)
    source_truth_classes: set[TruthClass] = field(default_factory=set)


@dataclass(slots=True)
class _ScopedEvidence:
    items: list[EvidenceItem]
    duplicate_evidence_suppressed: int


def _id_sort_key(value: object) -> tuple[str, str]:
    """Total order for identifiers without coercing their stored values."""
    return (str(value), type(value).__name__)


def _in_scope(
    item: EvidenceItem,
    candidate_id: UUID,
    research_run_id: UUID | None,
) -> bool:
    """Keep candidate and run ownership at the derivation boundary."""
    if item.candidate_id != candidate_id:
        return False
    if research_run_id is not None and item.research_run_id != research_run_id:
        return False
    # With no run id, only explicitly runless inline evidence is in scope.
    # This prevents an omitted run stamp from silently mixing historical runs.
    if research_run_id is None and item.research_run_id is not None:
        return False
    return True


def _payload_video_id(payload: dict) -> str | None:
    """Read only the stored video identity needed by this derivation."""
    value = payload.get("video_id")
    if isinstance(value, str) and value:
        return value
    return None


def _payload_channel_id(payload: dict) -> str | None:
    """Read only the stored creator identity needed by this derivation."""
    value = payload.get("channel_id")
    if isinstance(value, str) and value:
        return value
    return None


def _numeric_observed_view_count(item: EvidenceItem) -> int | None:
    """Return a valid observed count, never a payload fallback or guess."""
    if item.truth_class != TruthClass.OBSERVED:
        return None
    raw = item.raw_value
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw if raw >= 0 else None
    if isinstance(raw, float) and math.isfinite(raw) and raw.is_integer() and raw >= 0:
        return int(raw)
    return None


def _collect_scoped_evidence(
    candidate_id: UUID,
    evidence: list[EvidenceItem],
    research_run_id: UUID | None,
) -> _ScopedEvidence:
    """Filter to one candidate/run and collapse only proven duplicates.

    A record with no payload hash is never collapsed. It may still resolve to
    the same video identity, but its multiplicity remains visible in lineage.
    """
    kept: list[EvidenceItem] = []
    seen: set[tuple[str, str | None, str | None, str]] = set()
    suppressed = 0
    for item in evidence:
        if not _in_scope(item, candidate_id, research_run_id):
            continue
        if item.signal_type != SIGNAL_VIDEO_VIEWS or item.purpose != EvidencePurpose.AUDIENCE:
            continue
        payload = item.raw_payload or {}
        video_id = _payload_video_id(payload)
        fingerprint = (
            item.signal_type,
            video_id,
            item.raw_payload_hash,
            str(item.raw_value),
        )
        if item.raw_payload_hash is not None and fingerprint in seen:
            suppressed += 1
            continue
        seen.add(fingerprint)
        kept.append(item)
    return _ScopedEvidence(items=kept, duplicate_evidence_suppressed=suppressed)


def _accumulate(
    scoped: _ScopedEvidence,
) -> tuple[list[_VideoAccumulator], tuple[UUID, ...]]:
    by_key: dict[tuple[str, str] | tuple[str, UUID], _VideoAccumulator] = {}
    order: list[tuple[str, str] | tuple[str, UUID]] = []
    contributing_ids: list[UUID] = []

    for item in scoped.items:
        payload = item.raw_payload or {}
        video_id = _payload_video_id(payload)
        # A missing video id cannot safely be joined to another record. Use
        # the evidence id only as an internal key; it is never exposed as a
        # synthetic video id and can never create a synthetic creator.
        key: tuple[str, str] | tuple[str, UUID]
        if video_id is None:
            key = ("missing_video_id", item.id)
        else:
            key = ("video_id", video_id)
        accumulator = by_key.get(key)
        if accumulator is None:
            accumulator = _VideoAccumulator(key=key, video_id=video_id)
            by_key[key] = accumulator
            order.append(key)

        accumulator.evidence_ids.append(item.id)
        contributing_ids.append(item.id)
        accumulator.source_truth_classes.add(item.truth_class)
        channel_id = _payload_channel_id(payload)
        if channel_id is not None:
            accumulator.channel_ids.add(channel_id)

        if item.truth_class != TruthClass.OBSERVED:
            accumulator.unknown_evidence_ids.append(item.id)
            continue

        value = _numeric_observed_view_count(item)
        if value is None:
            accumulator.invalid_observed_evidence_ids.append(item.id)
            continue
        accumulator.observed_values.add(value)
        accumulator.observed_value_evidence_ids.setdefault(value, []).append(item.id)

    # The returned order is deliberately canonical. This derivation exposes
    # an ordered result, so arrival order must not affect it.
    accumulators = [by_key[key] for key in sorted(order, key=_id_sort_key)]
    return accumulators, tuple(sorted(contributing_ids))


def _channel_id(accumulator: _VideoAccumulator) -> str | None:
    if len(accumulator.channel_ids) == 1:
        return next(iter(accumulator.channel_ids))
    return None


def _source_truth_class(accumulator: _VideoAccumulator) -> TruthClass:
    if accumulator.source_truth_classes == {TruthClass.OBSERVED}:
        return TruthClass.OBSERVED
    if TruthClass.OBSERVED in accumulator.source_truth_classes:
        return TruthClass.OBSERVED
    return TruthClass.UNKNOWN


def _baseline_state(sample_size: int, median_views: float) -> str:
    if sample_size < MIN_CREATOR_SAMPLE:
        return RobustOutlierObservationState.BASELINE_UNAVAILABLE.value
    if median_views == 0:
        return RobustOutlierObservationState.BASELINE_ZERO.value
    return "BASELINE_READY"


def _build_baselines(
    accumulators: list[_VideoAccumulator],
) -> tuple[dict[str, CreatorBaseline], tuple[CreatorBaseline, ...]]:
    by_channel: dict[str, list[_VideoAccumulator]] = {}
    for accumulator in accumulators:
        if accumulator.video_id is None:
            continue
        channel_id = _channel_id(accumulator)
        if channel_id is None or len(accumulator.observed_values) != 1:
            continue
        by_channel.setdefault(channel_id, []).append(accumulator)

    baselines: dict[str, CreatorBaseline] = {}
    for channel_id, channel_videos in by_channel.items():
        # One accumulator is one video identity. Unknown and conflicting
        # observations never enter a creator's numeric baseline.
        ordered = sorted(
            channel_videos,
            key=lambda a: _id_sort_key(a.video_id),
        )
        values = [next(iter(a.observed_values)) for a in ordered]
        median_views = float(median(values))
        evidence_ids = tuple(
            sorted(
                evidence_id
                for accumulator in ordered
                for evidence_id in accumulator.evidence_ids
            )
        )
        baseline = CreatorBaseline(
            channel_id=channel_id,
            observed_video_ids=tuple(
                sorted(
                    (a.video_id for a in ordered if a.video_id is not None),
                    key=_id_sort_key,
                )
            ),
            observed_view_counts=tuple(values),
            sample_size=len(values),
            median_views=median_views,
            state=_baseline_state(len(values), median_views),
            evidence_ids=evidence_ids,
        )
        baselines[channel_id] = baseline

    ordered_baselines = tuple(
        baselines[channel_id]
        for channel_id in sorted(baselines, key=_id_sort_key)
    )
    return baselines, ordered_baselines


def _observation_for(
    candidate_id: UUID,
    accumulator: _VideoAccumulator,
    baselines: dict[str, CreatorBaseline],
) -> RobustOutlierObservation:
    video_id = accumulator.video_id
    channel_id = _channel_id(accumulator)
    evidence_ids = tuple(sorted(accumulator.evidence_ids))
    source_truth = _source_truth_class(accumulator)

    if video_id is None:
        state = RobustOutlierObservationState.MISSING_VIDEO_ID
        view_count = None
    elif len(accumulator.channel_ids) > 1:
        state = RobustOutlierObservationState.CONFLICTING_OBSERVED_VIEW_COUNTS
        view_count = None
    elif len(accumulator.observed_values) > 1:
        state = RobustOutlierObservationState.CONFLICTING_OBSERVED_VIEW_COUNTS
        view_count = None
    elif len(accumulator.observed_values) == 0:
        if accumulator.invalid_observed_evidence_ids:
            state = RobustOutlierObservationState.INVALID_OBSERVED_VIEW_COUNT
        else:
            state = RobustOutlierObservationState.UNKNOWN_VIEW_COUNT
        view_count = None
    else:
        view_count = next(iter(accumulator.observed_values))
        if channel_id is None:
            state = RobustOutlierObservationState.MISSING_CHANNEL_ID
        else:
            baseline = baselines.get(channel_id)
            if baseline is None or baseline.sample_size < MIN_CREATOR_SAMPLE:
                state = RobustOutlierObservationState.BASELINE_UNAVAILABLE
            elif baseline.median_views == 0:
                state = RobustOutlierObservationState.BASELINE_ZERO
            elif view_count <= 0:
                state = RobustOutlierObservationState.VIDEO_VIEW_COUNT_ZERO
            else:
                state = RobustOutlierObservationState.SCORED

    baseline = baselines.get(channel_id) if channel_id is not None else None
    if baseline is not None and state == RobustOutlierObservationState.SCORED:
        # The positivity checks above are intentional. No epsilon, offset, or
        # other fabricated value is allowed into the specified formula.
        score = math.log2(view_count / baseline.median_views)  # type: ignore[operator]
        sample_size = baseline.sample_size
        median_views = baseline.median_views
    else:
        score = None
        sample_size = baseline.sample_size if baseline is not None else 0
        median_views = baseline.median_views if baseline is not None else None

    return RobustOutlierObservation(
        candidate_id=candidate_id,
        video_id=video_id,
        channel_id=channel_id,
        evidence_ids=evidence_ids,
        source_truth_class=source_truth,
        view_count=view_count,
        creator_sample_size=sample_size,
        creator_median_views=median_views,
        relative_score=score,
        state=state,
    )


def _build_provenance(
    candidate_id: UUID,
    research_run_id: UUID | None,
    accumulators: list[_VideoAccumulator],
    scoped: _ScopedEvidence,
    contributing_ids: tuple[UUID, ...],
) -> RobustOutlierProvenance:
    contributing_set = set(contributing_ids)
    contributing = [item for item in scoped.items if item.id in contributing_set]
    video_ids = sorted(
        {
            accumulator.video_id
            for accumulator in accumulators
            if accumulator.video_id is not None
        },
        key=_id_sort_key,
    )
    channel_ids = sorted(
        {
            channel_id
            for accumulator in accumulators
            for channel_id in accumulator.channel_ids
        },
        key=_id_sort_key,
    )
    return RobustOutlierProvenance(
        candidate_id=candidate_id,
        research_run_id=research_run_id,
        evidence_ids=tuple(sorted(contributing_ids)),
        video_ids=tuple(video_ids),
        channel_ids=tuple(channel_ids),
        providers=tuple(sorted({item.provider for item in contributing})),
        platforms=tuple(sorted({item.platform for item in contributing if item.platform})),
        source_truth_classes=tuple(
            sorted({item.truth_class.value for item in contributing})
        ),
        duplicate_evidence_suppressed=scoped.duplicate_evidence_suppressed,
    )


def derive_robust_content_intelligence(
    candidate_id: UUID,
    evidence: list[EvidenceItem],
    research_run_id: UUID | None = None,
) -> RobustContentIntelligenceResult:
    """Derive robust creator-relative outlier evidence for one scope.

    ``research_run_id`` is an exact scope when supplied. When it is omitted,
    only explicitly runless inline evidence is considered; stored evidence
    belonging to any research run is excluded rather than mixed.

    The function is deterministic and pure. It consumes evidence records but
    never calls a provider, performs network I/O, mutates a store, or reads a
    field outside the public-content view identity contract.
    """
    scoped = _collect_scoped_evidence(candidate_id, evidence, research_run_id)
    accumulators, contributing_ids = _accumulate(scoped)
    baselines, ordered_baselines = _build_baselines(accumulators)
    observations = tuple(
        _observation_for(candidate_id, accumulator, baselines)
        for accumulator in accumulators
    )
    provenance = _build_provenance(
        candidate_id,
        research_run_id,
        accumulators,
        scoped,
        contributing_ids,
    )

    observed_video_count = sum(
        1
        for observation in observations
        if observation.view_count is not None
        and observation.source_truth_class == TruthClass.OBSERVED
        and observation.state
        not in (
            RobustOutlierObservationState.CONFLICTING_OBSERVED_VIEW_COUNTS,
            RobustOutlierObservationState.INVALID_OBSERVED_VIEW_COUNT,
        )
    )
    unknown_view_video_count = sum(
        1
        for observation in observations
        if observation.state
        in (
            RobustOutlierObservationState.UNKNOWN_VIEW_COUNT,
            RobustOutlierObservationState.INVALID_OBSERVED_VIEW_COUNT,
        )
    )
    videos_without_channel_id = sum(
        1
        for observation in observations
        if observation.video_id is not None and observation.channel_id is None
    )
    videos_without_video_id = sum(
        1
        for observation in observations
        if observation.state == RobustOutlierObservationState.MISSING_VIDEO_ID
    )
    scoreable_count = sum(1 for observation in observations if observation.scoreable)
    non_scoreable_count = len(observations) - scoreable_count
    zero_baseline_count = sum(
        1
        for baseline in ordered_baselines
        if baseline.state == RobustOutlierObservationState.BASELINE_ZERO.value
    )

    if not scoped.items:
        state = DimensionState.MISSING
        missing_reason = "no_scoped_public_video_view_evidence"
    elif observed_video_count == 0:
        state = DimensionState.UNKNOWN
        missing_reason = "no_observed_view_count_available_in_scope"
    else:
        # This derivation has no candidate-level numeric value. Even when
        # individual videos are scoreable, the result is evidence-present and
        # unscored at the candidate level.
        state = DimensionState.EVIDENCE_PRESENT_UNSCORED
        missing_reason = None

    return RobustContentIntelligenceResult(
        candidate_id=candidate_id,
        research_run_id=research_run_id,
        state=state,
        value=None,
        observations=observations,
        creator_baselines=ordered_baselines,
        provenance=provenance,
        observed_video_count=observed_video_count,
        unknown_view_video_count=unknown_view_video_count,
        videos_without_channel_id=videos_without_channel_id,
        videos_without_video_id=videos_without_video_id,
        creators_with_baseline=sum(
            1
            for baseline in ordered_baselines
            if baseline.sample_size >= MIN_CREATOR_SAMPLE
        ),
        zero_baseline_creator_count=zero_baseline_count,
        scoreable_observation_count=scoreable_count,
        non_scoreable_observation_count=non_scoreable_count,
        missing_reason=missing_reason,
    )


# Explicit aliases make the service discoverable without creating a second
# implementation or a second formula.
extract_robust_content_outliers = derive_robust_content_intelligence
extract_robust_outlier_evidence = derive_robust_content_intelligence
extract_faceless_content_intelligence = derive_robust_content_intelligence


__all__ = [
    "CreatorBaseline",
    "DIM_ROBUST_CONTENT_OUTLIER",
    "FACELESS_CONTENT_INTELLIGENCE_VERSION",
    "LIMITATIONS",
    "MIN_CHANNEL_SAMPLE",
    "MIN_CREATOR_SAMPLE",
    "ROBUST_CONTENT_OUTLIER_VERSION",
    "RobustContentIntelligenceResult",
    "RobustOutlierObservation",
    "RobustOutlierObservationState",
    "RobustOutlierProvenance",
    "derive_robust_content_intelligence",
    "extract_faceless_content_intelligence",
    "extract_robust_content_outliers",
    "extract_robust_outlier_evidence",
]
