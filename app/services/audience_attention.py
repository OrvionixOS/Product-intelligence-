"""Audience Attention (Milestone 4F).

A deterministic derivation over public-content evidence 3B already collected
and 3C already resolved. No provider, no provider call, no endpoint, no
persistence, and no score.

Attention is not interest, and interest is not demand
------------------------------------------------------

This module formalizes the one thing public content can defensibly show:
**that some number of people looked at content about a problem, and how that
observed attention was distributed**. Every step beyond that is a different
claim needing evidence this module does not have:

    a view          is one playback event, not a person, not a buyer, and
                    not a purchase intent
    a like          is an interaction with a video, not a willingness to pay
    a subscriber    is context about a CHANNEL, never the size of a
                    candidate's audience
    a viral video   is one video that was watched, never proof that demand
                    for a product exists or will persist

So `buyer_count`, `purchase_intent`, `candidate_audience_size`,
`demand_durability`, `willingness_to_pay`, `watch_time` and
`conversion_probability` are permanent UNKNOWN markers on every result. The
refusal is structural rather than prose.

Subscriber counts are not read at all
--------------------------------------

`VideoObservation` carries `channel_subscriber_count`, `channel_view_count`
and `channel_video_count`. This module reads none of them, and a test walks
the AST to prove it. A subscriber count describes a creator's whole audience
across every topic they cover; treating it as a candidate's audience is the
single easiest way to turn channel context into a fabricated market size, and
the safest guarantee is code that cannot reach the number.

Distribution and consistency, never a sum
------------------------------------------

Summing views is how one viral video makes a whole opportunity look
universally popular. This module therefore reports **how observed attention
was distributed**, and isolates the largest contributor explicitly:

    top_video_attention_share       what fraction of all observed views sits
                                    in the single most-watched video
    median_views_excluding_top_video  what the field looks like with that
                                    video removed
    videos_covering_half_of_attention  how few videos account for half of it
    top_channel_attention_share     the same question at creator level
    videos_within_band_of_median    how many videos are typical rather than
                                    freak

`attention_pattern_v1` then reports the SHAPE of that distribution. A field
whose attention is one video reports SINGLE_VIDEO_ATTENTION no matter how
large the total, and every pattern states explicitly what it does NOT
establish.

Missing metrics stay UNKNOWN
-----------------------------

Four facts stay distinguishable: the public-content capability was not
requested, the provider failed, the provider raised something unexpected, or
it ran and returned nothing. A fifth is kept separate from all of them:
videos were observed but their view counts were not. That is
ATTENTION_UNMEASURED, and it is never the same as attention measured at zero,
which is NO_OBSERVED_ATTENTION.

What this module deliberately does not compute
-----------------------------------------------

- **Keyword search volume, CPC, auction data** — Milestone 3A/search demand
  owns those, and search interest is a different observation from watching.
- **Channels as reachable endpoints** — Milestone 4D owns those.
- **Listings, sellers, prices, review proxies** — Milestones 4A, 4B and 4E
  own those; nothing here touches marketplace evidence.

An AST guard asserts the module reads none of their payload fields.

Relationship to the existing audience surfaces
-----------------------------------------------

`public_content_features.summarize_public_content` (Milestone 3B) produces
per-snapshot features from provider objects, including
`audience_interest_dimension_v1`, a 0-100 value derived from median views
that 3C exposes as the SCORED dimension `preliminary_audience_interest` and
the preliminary ranking consumes.

This module neither changes nor replaces that. It is a different layer: a
candidate-scoped derivation over STORED evidence with truth classes,
missing-versus-zero semantics, provenance, and no value at all. Its dimension
is named `preliminary_audience_attention` so the two can never be confused,
and it is deliberately unscored — a single number derived from median views
is monotone in views, which is precisely the reading this milestone exists to
avoid.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from uuid import UUID

from app.domain.enums import TruthClass
from app.domain.models import EvidenceItem
from app.services.marketplace_listing_view import as_datetime, weakest_truth_class
from app.services.preliminary_dimensions import DimensionState

AUDIENCE_ATTENTION_VERSION = "audience_attention_v1"
ATTENTION_FEATURES_VERSION = "attention_distribution_features_v1"
ATTENTION_PATTERN_VERSION = "attention_pattern_v1"

# Never "audience_interest": that is a ScoreDimensions field feeding the
# unapproved legacy weights. Never "preliminary_audience_interest" either:
# that is 3C's SCORED dimension, which this milestone leaves untouched.
DIM_AUDIENCE_ATTENTION = "preliminary_audience_attention"

CAPABILITY_PUBLIC_CONTENT = "public_content"

# Signal types emitted by the 3B evidence builder.
SIGNAL_VIDEO_VIEWS = "public_video_view_count"
SIGNAL_ENGAGEMENT_RATE = "public_video_engagement_rate"
SIGNAL_CONTENT_OBSERVATION = "public_content_observation"

# ---------------------------------------------------------------- thresholds
#
# Every constant below is an UNVALIDATED V1 ASSUMPTION chosen by inspection.
# None is calibrated against outcomes, because no outcome data exists in this
# repository. They decide which SHAPE is reported; none decides whether that
# shape is good, and no threshold is a quality bar.

# At or below this many videos WITH an observed view count, the sample cannot
# describe a distribution at all.
SPARSE_SAMPLE_MAX_VIDEOS = 3

# Share of all observed views held by the single most-watched video at which
# the field's attention is reported as that one video's.
SINGLE_VIDEO_DOMINANCE_SHARE = 0.6

# Share held by the top video or the top channel at which attention is
# reported concentrated rather than distributed.
CONCENTRATION_SHARE = 0.4

# A field is reported distributed only when attention reaches several
# creators, not merely several videos by one creator.
DISTRIBUTED_MIN_CHANNELS = 3

# Multiplicative band around the median used to count "typical" videos. A
# video between half and twice the median is within band. Symmetric in ratio
# because view distributions are heavy-tailed, so a symmetric absolute band
# would classify almost everything as atypical.
TYPICAL_BAND_LOW = 0.5
TYPICAL_BAND_HIGH = 2.0

LIMITATIONS = (
    "A view is a playback event. It is never a person, a buyer, a purchase "
    "intent, or a unit sold.",
    "Engagement is interaction with a video. It is never willingness to pay "
    "and never conversion.",
    "Subscriber counts describe a creator's whole audience across every topic "
    "they cover. They are not a candidate's audience and are not read here.",
    "A high-attention video is one video that was watched. It is never "
    "evidence that demand exists, or that any demand would persist.",
    "Observed attention is a provider-ordered sample of public content, not "
    "the whole of it, so absence of attention is weak evidence of absence.",
    "Attention statistics describe the videos that were returned. They "
    "describe no population of buyers.",
    "Consistency is measured RELATIVE TO THE MEDIAN of this field. A field "
    "whose videos are uniformly ignored is highly consistent at a trivial "
    "level, so proportion_within_band_of_median must always be read next to "
    "median_views and never as a level of interest.",
    "Thresholds are unvalidated V1 assumptions, not calibrated against any "
    "outcome data.",
)


class AttentionPattern(str, Enum):
    """The SHAPE of observed attention. Never its desirability.

    None of these is a level of demand, and a pattern reporting broad
    attention is not evidence that a product would sell.
    """

    # The capability did not produce a usable answer.
    UNKNOWN_ATTENTION = "UNKNOWN_ATTENTION"
    # The capability ran and returned no content for this candidate.
    NO_CONTENT_OBSERVED = "NO_CONTENT_OBSERVED"
    # Content was observed; its view counts were not. Missing measurement,
    # never measured absence.
    ATTENTION_UNMEASURED = "ATTENTION_UNMEASURED"
    # View counts were observed and every one of them is zero. A measured
    # absence of attention, which is still not a measure of demand.
    NO_OBSERVED_ATTENTION = "NO_OBSERVED_ATTENTION"
    # Too few measured videos to describe a distribution.
    SPARSE_SAMPLE = "SPARSE_SAMPLE"
    # One video holds most of the observed attention.
    SINGLE_VIDEO_ATTENTION = "SINGLE_VIDEO_ATTENTION"
    # Attention sits with few videos or one creator.
    CONCENTRATED_ATTENTION = "CONCENTRATED_ATTENTION"
    # Attention reaches several videos across several creators.
    DISTRIBUTED_ATTENTION = "DISTRIBUTED_ATTENTION"


@dataclass(slots=True, frozen=True)
class AttentionClaim:
    """What a pattern observes, and what it explicitly does not establish.

    The second half is not commentary: it is emitted with every result so a
    consumer cannot read a pattern without also receiving the boundary of
    what that pattern supports.
    """

    observes: str
    does_not_establish: str


# Every pattern appears here. A test asserts the mapping is total and that
# neither half is empty, so a future pattern cannot be added without stating
# the boundary of what it supports.
PATTERN_CLAIMS: dict[AttentionPattern, AttentionClaim] = {
    AttentionPattern.UNKNOWN_ATTENTION: AttentionClaim(
        observes="Nothing was measured for this candidate.",
        does_not_establish=(
            "That no attention exists. Attention is unknown here and must "
            "not be read as low or absent."
        ),
    ),
    AttentionPattern.NO_CONTENT_OBSERVED: AttentionClaim(
        observes="The provider ran and returned no relevant content.",
        does_not_establish=(
            "That nobody is interested in the problem. Public content is a "
            "provider-ordered sample, and its absence measures the sample, "
            "not the world."
        ),
    ),
    AttentionPattern.ATTENTION_UNMEASURED: AttentionClaim(
        observes=(
            "Content exists, but no view count was observed for any of it."
        ),
        does_not_establish=(
            "That the content was unwatched. This is absent measurement, "
            "never attention measured at zero."
        ),
    ),
    AttentionPattern.NO_OBSERVED_ATTENTION: AttentionClaim(
        observes="View counts were observed and every one of them is zero.",
        does_not_establish=(
            "That the problem lacks an audience. It records that this "
            "sample of content was not watched."
        ),
    ),
    AttentionPattern.SPARSE_SAMPLE: AttentionClaim(
        observes=(
            "Too few videos carry an observed view count to describe how "
            "attention is distributed."
        ),
        does_not_establish=(
            "Anything about concentration or breadth. The statistics "
            "reported alongside describe a handful of videos."
        ),
    ),
    AttentionPattern.SINGLE_VIDEO_ATTENTION: AttentionClaim(
        observes=(
            "Most of the observed attention sits in one video."
        ),
        does_not_establish=(
            "That the topic is broadly watched. One video's reach is that "
            "video's, and the total views of this field are dominated by it "
            "however large that total is."
        ),
    ),
    AttentionPattern.CONCENTRATED_ATTENTION: AttentionClaim(
        observes=(
            "Observed attention sits with few videos, or with one creator."
        ),
        does_not_establish=(
            "That attention would transfer to a new entrant, or that the "
            "creators holding it are serving the same need a product would."
        ),
    ),
    AttentionPattern.DISTRIBUTED_ATTENTION: AttentionClaim(
        observes=(
            "Observed attention reaches several videos across several "
            "creators, rather than resting on one."
        ),
        does_not_establish=(
            "That demand exists, that it would persist, or that any viewer "
            "would buy anything. Broad attention is still only attention."
        ),
    ),
}


def _id_sort_key(value: object) -> tuple[str, str]:
    """Total order over identifiers of any type, without coercing them.

    Same rule Milestone 4D-0.1 established for 4A/4B provenance: the type name
    breaks the tie `str()` alone leaves between distinct values that render
    identically, such as 1 and "1".
    """
    return (str(value), type(value).__name__)


@dataclass(slots=True, frozen=True)
class VideoView:
    """One deduplicated video, rebuilt from stored public-content evidence.

    Every optional field is None unless an evidence record supplied it.
    Nothing is inferred, defaulted, or zero-filled. `view_count` is populated
    only from an OBSERVED view record, so 0 is a real observation and None
    means no view count was ever observed.
    """

    video_id: str
    channel_id: str | None
    view_count: int | None
    views_observed: bool
    engagement_rate: float | None
    published_at: datetime | None
    evidence_ids: tuple[UUID, ...]


@dataclass(slots=True, frozen=True)
class AttentionProvenance:
    """Lineage of every derived feature, preserved intact."""

    candidate_id: UUID
    evidence_ids: tuple[UUID, ...]
    video_ids: tuple[str, ...]
    channel_ids: tuple[str, ...]
    providers: tuple[str, ...]
    platforms: tuple[str, ...]
    source_truth_classes: tuple[str, ...]
    duplicate_evidence_suppressed: int


@dataclass(slots=True, frozen=True)
class AttentionFeatures:
    """How observed attention was distributed. Never a total to be read alone.

    Every count is identity-based, so a re-delivered video and a repeated
    research run cannot inflate any of them.
    """

    # Sample composition. Videos with no observed view count are reported,
    # never counted as zero-view videos.
    video_count: int
    videos_with_observed_views: int
    videos_with_unmeasured_views: int
    channels_in_sample: int
    # Channels contributing at least one OBSERVED view count. Deliberately
    # not Milestone 4D's channel count, which counts creators with a relevant
    # video whether or not any view count was observed.
    channels_with_observed_attention: int

    # Central tendency over OBSERVED view counts only. Median, never mean:
    # view distributions are heavy-tailed and one video must not move the
    # centre. No total is reported, because a total is exactly the statistic
    # one viral video corrupts.
    median_views: float | None
    lower_quartile_views: float | None
    upper_quartile_views: float | None

    # Concentration. The outlier is isolated rather than averaged away.
    top_video_attention_share: float | None
    median_views_excluding_top_video: float | None
    videos_covering_half_of_attention: int | None
    top_channel_attention_share: float | None

    # Consistency, measured RELATIVE TO THIS FIELD'S MEDIAN. A field of
    # uniformly ignored videos is perfectly consistent at a trivial level, so
    # these are only interpretable next to median_views: consistency is a
    # statement about spread, never about level.
    videos_within_band_of_median: int | None
    proportion_within_band_of_median: float | None

    # Temporal spread of the sample. Observations about when content
    # appeared, never a statement that attention persisted.
    videos_with_publish_date: int
    publish_span_days: int | None
    distinct_publish_months: int | None

    # Derived from OBSERVED like/view pairs by 3B and INFERRED there. It
    # measures interaction with a video and nothing else.
    median_engagement_rate: float | None
    videos_with_engagement_rate: int

    features_version: str = ATTENTION_FEATURES_VERSION


@dataclass(slots=True, frozen=True)
class AudienceAttentionResult:
    """Audience Attention for one candidate: shape, boundary, lineage."""

    candidate_id: UUID
    state: DimensionState
    # Permanently None in 4F. No audience formula is approved here, and a
    # single number over views is monotone in views, which is the reading
    # this milestone exists to avoid.
    value: float | None
    pattern: AttentionPattern
    claim: AttentionClaim
    features: AttentionFeatures | None
    provenance: AttentionProvenance

    value_truth_class: TruthClass | None
    features_truth_class: TruthClass | None
    evidence_truth_basis: TruthClass | None

    # Permanent UNKNOWN markers. Each names a conclusion that observed
    # attention cannot support.
    buyer_count: TruthClass = TruthClass.UNKNOWN
    purchase_intent: TruthClass = TruthClass.UNKNOWN
    candidate_audience_size: TruthClass = TruthClass.UNKNOWN
    demand_durability: TruthClass = TruthClass.UNKNOWN
    willingness_to_pay: TruthClass = TruthClass.UNKNOWN
    watch_time: TruthClass = TruthClass.UNKNOWN
    conversion_probability: TruthClass = TruthClass.UNKNOWN

    missing_reason: str | None = None
    limitations: tuple[str, ...] = LIMITATIONS
    dimension_name: str = DIM_AUDIENCE_ATTENTION
    version: str = AUDIENCE_ATTENTION_VERSION
    pattern_version: str = ATTENTION_PATTERN_VERSION


# ------------------------------------------------------------------ internals


def collect_video_views(
    evidence: list[EvidenceItem],
) -> tuple[list[VideoView], int, tuple[UUID, ...]]:
    """Rebuild deduplicated videos from stored public-content evidence.

    Returns (views, duplicate_evidence_suppressed, contributing_evidence_ids).

    One canonical video per video_id; every evidence record that referenced it
    contributes its id to the lineage. Re-delivered copies of the same
    observation are collapsed and counted, never double-counted.

    The evidence RECORD, not the payload snapshot, is the authority on what
    was observed: a view count is read only from a record whose truth_class is
    OBSERVED, so a video whose views the provider never returned keeps None
    rather than becoming a zero-view video.

    `evidence_ids` on each video is canonically sorted, preserving
    multiplicity: sorted(), never set(), because a record carrying no payload
    hash is deliberately never collapsed and discarding the repeat would
    contradict duplicate_evidence_suppressed. The returned list itself is
    left in arrival order; this module canonicalizes its own provenance.
    """
    by_video: dict[str, dict] = {}
    order: list[str] = []
    suppressed = 0
    seen: set[tuple] = set()
    contributing: list[UUID] = []

    wanted = (SIGNAL_VIDEO_VIEWS, SIGNAL_ENGAGEMENT_RATE, SIGNAL_CONTENT_OBSERVATION)
    for item in evidence:
        if item.signal_type not in wanted:
            continue
        payload = item.raw_payload or {}
        video_id = payload.get("video_id")
        if not video_id:
            continue

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
        contributing.append(item.id)

        if video_id not in by_video:
            by_video[video_id] = {
                "channel_id": payload.get("channel_id"),
                "view_count": None,
                "views_observed": False,
                "engagement_rate": None,
                "published_at": payload.get("published_at"),
                "evidence_ids": [],
            }
            order.append(video_id)

        record = by_video[video_id]
        record["evidence_ids"].append(item.id)

        if item.raw_value is None:
            continue
        if (
            item.signal_type == SIGNAL_VIDEO_VIEWS
            and item.truth_class == TruthClass.OBSERVED
        ):
            record["view_count"] = int(item.raw_value)
            record["views_observed"] = True
        elif item.signal_type == SIGNAL_ENGAGEMENT_RATE:
            # 3B derives this from OBSERVED likes and views and classes it
            # INFERRED. It stays INFERRED here; it is never promoted.
            record["engagement_rate"] = float(item.raw_value)

    views = [
        VideoView(
            video_id=video_id,
            channel_id=by_video[video_id]["channel_id"],
            view_count=by_video[video_id]["view_count"],
            views_observed=by_video[video_id]["views_observed"],
            engagement_rate=by_video[video_id]["engagement_rate"],
            published_at=as_datetime(by_video[video_id]["published_at"]),
            evidence_ids=tuple(sorted(by_video[video_id]["evidence_ids"])),
        )
        for video_id in order
    ]
    return views, suppressed, tuple(contributing)


def _quantile(sorted_values: list[float], q: float) -> float:
    """Deterministic linear-interpolation quantile over a sorted list."""
    if len(sorted_values) == 1:
        return sorted_values[0]
    position = q * (len(sorted_values) - 1)
    low = int(position)
    high = min(low + 1, len(sorted_values) - 1)
    weight = position - low
    return sorted_values[low] * (1 - weight) + sorted_values[high] * weight


def _covering_half(counts: list[int]) -> int:
    """Fewest contributors accounting for at least half the observed total.

    Deterministic by construction rather than by tie-break: the answer is a
    function of the MULTISET of values alone, since contributors are taken
    largest-first and only the running sum decides when to stop. Two equal
    contributors are interchangeable, so no permutation changes the result.
    """
    total = sum(counts)
    running = 0
    covered = 0
    for value in sorted(counts, reverse=True):
        running += value
        covered += 1
        if running * 2 >= total:
            break
    return covered


def _build_features(videos: list[VideoView]) -> AttentionFeatures:
    measured = [v for v in videos if v.views_observed and v.view_count is not None]
    counts = [v.view_count for v in measured]  # type: ignore[misc]
    total = sum(counts)

    median_views = lower_q = upper_q = None
    if counts:
        ordered = sorted(float(c) for c in counts)
        median_views = round(_quantile(ordered, 0.5), 4)
        lower_q = round(_quantile(ordered, 0.25), 4)
        upper_q = round(_quantile(ordered, 0.75), 4)

    # Concentration. Shares are None rather than zero when no attention was
    # observed at all: a share of nothing is unknown, not zero.
    top_video_share = None
    median_excluding_top = None
    covering_half = None
    if counts and total > 0:
        top_video_share = round(max(counts) / total, 4)
        covering_half = _covering_half(counts)
        remaining = sorted(float(c) for c in counts)
        remaining.pop()  # drop exactly one copy of the largest value
        if remaining:
            median_excluding_top = round(_quantile(remaining, 0.5), 4)

    by_channel: dict[str, int] = {}
    for video in measured:
        if video.channel_id is not None:
            by_channel[video.channel_id] = (
                by_channel.get(video.channel_id, 0) + (video.view_count or 0)
            )
    channel_total = sum(by_channel.values())
    top_channel_share = (
        round(max(by_channel.values()) / channel_total, 4)
        if by_channel and channel_total > 0
        else None
    )

    within_band = proportion_within = None
    if median_views is not None and median_views > 0:
        within_band = sum(
            1
            for count in counts
            if TYPICAL_BAND_LOW * median_views
            <= count
            <= TYPICAL_BAND_HIGH * median_views
        )
        proportion_within = round(within_band / len(counts), 4)

    dated = [v.published_at for v in videos if v.published_at is not None]
    span = months = None
    if dated:
        span = (max(dated) - min(dated)).days
        months = len({(d.year, d.month) for d in dated})

    rates = [v.engagement_rate for v in videos if v.engagement_rate is not None]
    median_rate = (
        round(_quantile(sorted(rates), 0.5), 6) if rates else None
    )

    return AttentionFeatures(
        video_count=len(videos),
        videos_with_observed_views=len(measured),
        videos_with_unmeasured_views=len(videos) - len(measured),
        channels_in_sample=len(
            {v.channel_id for v in videos if v.channel_id is not None}
        ),
        channels_with_observed_attention=len(by_channel),
        median_views=median_views,
        lower_quartile_views=lower_q,
        upper_quartile_views=upper_q,
        top_video_attention_share=top_video_share,
        median_views_excluding_top_video=median_excluding_top,
        videos_covering_half_of_attention=covering_half,
        top_channel_attention_share=top_channel_share,
        videos_within_band_of_median=within_band,
        proportion_within_band_of_median=proportion_within,
        videos_with_publish_date=len(dated),
        publish_span_days=span,
        distinct_publish_months=months,
        median_engagement_rate=median_rate,
        videos_with_engagement_rate=len(rates),
    )


def _classify(features: AttentionFeatures) -> AttentionPattern:
    """attention_pattern_v1. Describes distribution shape, not desirability.

    Order matters: each rule is reached only when the ones above it did not
    apply, which is what keeps the classification explainable. The order of
    the rules is not an order over the patterns they return, and no pattern
    is a level of demand.
    """
    if features.video_count == 0:
        return AttentionPattern.NO_CONTENT_OBSERVED
    if features.videos_with_observed_views == 0:
        # Content exists; nothing measured it. Absent measurement.
        return AttentionPattern.ATTENTION_UNMEASURED
    if features.top_video_attention_share is None:
        # View counts were observed and every one was zero, so no share can
        # be computed. A measured absence.
        return AttentionPattern.NO_OBSERVED_ATTENTION
    if features.videos_with_observed_views <= SPARSE_SAMPLE_MAX_VIDEOS:
        return AttentionPattern.SPARSE_SAMPLE
    if features.top_video_attention_share >= SINGLE_VIDEO_DOMINANCE_SHARE:
        # One video IS this field's attention, whatever the total.
        return AttentionPattern.SINGLE_VIDEO_ATTENTION
    if features.top_video_attention_share >= CONCENTRATION_SHARE:
        return AttentionPattern.CONCENTRATED_ATTENTION
    if (
        features.top_channel_attention_share is not None
        and features.top_channel_attention_share >= SINGLE_VIDEO_DOMINANCE_SHARE
    ):
        # Spread across videos, but they are one creator's videos.
        return AttentionPattern.CONCENTRATED_ATTENTION
    if features.channels_with_observed_attention < DISTRIBUTED_MIN_CHANNELS:
        return AttentionPattern.CONCENTRATED_ATTENTION
    return AttentionPattern.DISTRIBUTED_ATTENTION


def _build_provenance(
    candidate_id: UUID,
    evidence: list[EvidenceItem],
    videos: list[VideoView],
    evidence_ids: tuple[UUID, ...],
    suppressed: int,
) -> AttentionProvenance:
    contributing = [item for item in evidence if item.id in set(evidence_ids)]
    return AttentionProvenance(
        candidate_id=candidate_id,
        evidence_ids=tuple(sorted(evidence_ids)),
        video_ids=tuple(sorted((v.video_id for v in videos), key=_id_sort_key)),
        channel_ids=tuple(
            sorted(
                {v.channel_id for v in videos if v.channel_id is not None},
                key=_id_sort_key,
            )
        ),
        providers=tuple(sorted({item.provider for item in contributing})),
        platforms=tuple(
            sorted({item.platform for item in contributing if item.platform})
        ),
        source_truth_classes=tuple(
            sorted({item.truth_class.value for item in contributing})
        ),
        duplicate_evidence_suppressed=suppressed,
    )


# ----------------------------------------------------------------- extraction


def extract_audience_attention(
    candidate_id: UUID,
    evidence: list[EvidenceItem],
    missing_reasons: dict[str, str] | None = None,
) -> AudienceAttentionResult:
    """Derive Audience Attention for one candidate from stored evidence.

    Deterministic and pure: identical evidence yields identical output,
    whatever order it arrives in. No provider calls, no LLM, no fabricated
    values, and no conversion of attention into buyers, demand, or intent.

    `missing_reasons` maps a capability name to why it produced nothing. It is
    what keeps a failed public-content call from reading as an absence of
    attention.
    """
    reasons = missing_reasons or {}
    videos, suppressed, evidence_ids = collect_video_views(evidence)
    provenance = _build_provenance(
        candidate_id, evidence, videos, evidence_ids, suppressed
    )
    contributing = [item for item in evidence if item.id in set(evidence_ids)]
    basis = weakest_truth_class(contributing)

    capability_reason = reasons.get(CAPABILITY_PUBLIC_CONTENT)

    if not videos:
        if capability_reason is not None:
            return AudienceAttentionResult(
                candidate_id=candidate_id,
                state=DimensionState.MISSING,
                value=None,
                pattern=AttentionPattern.UNKNOWN_ATTENTION,
                claim=PATTERN_CLAIMS[AttentionPattern.UNKNOWN_ATTENTION],
                features=None,
                provenance=provenance,
                value_truth_class=None,
                features_truth_class=None,
                evidence_truth_basis=basis,
                missing_reason=capability_reason,
            )
        return AudienceAttentionResult(
            candidate_id=candidate_id,
            state=DimensionState.UNKNOWN,
            value=None,
            pattern=AttentionPattern.NO_CONTENT_OBSERVED,
            claim=PATTERN_CLAIMS[AttentionPattern.NO_CONTENT_OBSERVED],
            features=None,
            provenance=provenance,
            value_truth_class=None,
            features_truth_class=None,
            evidence_truth_basis=basis,
            missing_reason="no_public_content_returned_for_candidate",
        )

    features = _build_features(videos)
    pattern = _classify(features)

    if pattern == AttentionPattern.ATTENTION_UNMEASURED:
        # Videos were observed; their view counts were not. UNKNOWN, and
        # emphatically not attention measured at zero.
        return AudienceAttentionResult(
            candidate_id=candidate_id,
            state=DimensionState.UNKNOWN,
            value=None,
            pattern=pattern,
            claim=PATTERN_CLAIMS[pattern],
            features=features,
            provenance=provenance,
            value_truth_class=None,
            features_truth_class=TruthClass.INFERRED,
            evidence_truth_basis=basis,
            missing_reason="no_view_count_observed_for_any_video",
        )

    return AudienceAttentionResult(
        candidate_id=candidate_id,
        state=DimensionState.EVIDENCE_PRESENT_UNSCORED,
        value=None,
        pattern=pattern,
        claim=PATTERN_CLAIMS[pattern],
        features=features,
        provenance=provenance,
        value_truth_class=None,
        # Derived from OBSERVED inputs, but itself a derivation.
        features_truth_class=TruthClass.INFERRED,
        evidence_truth_basis=basis,
    )
