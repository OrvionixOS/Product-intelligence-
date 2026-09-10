"""Buyer Reach (Milestone 4D).

The question this answers, for one candidate:

    Where could a seller show up to attempt to reach potential buyers, and
    what is observably true about each of those places?

The question it deliberately does NOT answer:

    How many buyers exist?
    How large is the audience or the market?
    Will any of those people buy?

CHANNEL EVIDENCE IS NOT BUYER EVIDENCE
--------------------------------------

This is the rule the whole milestone is built around, and every field below
is shaped by it. Relevant videos with real engagement demonstrate an
observable content audience around a problem; they cannot establish that any
viewer will buy the proposed product. Etsy listings demonstrate marketplace
supply and public commercial proxies; they cannot establish total buyer
reach. A search keyword with an ad auction demonstrates that advertisers bid
to appear there; it says nothing about purchase intent.

So Buyer Reach records CHANNELS, never buyers. It converts nothing:

    views          are NOT buyers
    subscribers    are NOT reachable buyers
    listings       are NOT a buyer population
    sellers        are NOT market size
    review proxies are NOT sales
    CPC            is NOT purchase intent
    multi-channel  is NOT a better opportunity

Four claim layers, never collapsed
----------------------------------

    existence    a provider returned a concrete surface        OBSERVED
    relevance    the surface is linked to THIS candidate       INFERRED (capped)
    activity     the surface is live / recent                  OBSERVED or UNKNOWN
    commercial   anyone actually transacts there               cited from 4A only

Relevance is permanently capped at INFERRED. Milestone 4D-0 made its basis
auditable — every record now carries the normalized provider query that
returned it, and whether that query was generated for other candidates too —
but a query is a Milestone 1 hypothesis, so "a query generated for this
candidate returned this surface" is a derivation, never an observation of
relevance. Commercial activity is not computed here at all: purchase proxies
belong to Milestone 4A, and promoting one to a channel would let a proxy read
as a reach surface.

No score
--------

Like 4A and 4B, this milestone produces structured evidence and no number.
`value` is permanently None and the state is EVIDENCE_PRESENT_UNSCORED when
evidence exists. No approved formula converts channel evidence into a 0-100
reach figure, and inventing one would be the exact overclaim this design
exists to prevent.
"""

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from uuid import UUID

from app.domain.enums import TruthClass
from app.domain.models import EvidenceItem
from app.services.preliminary_dimensions import DimensionState

BUYER_REACH_VERSION = "buyer_reach_v1"
REACH_PATTERN_VERSION = "reach_evidence_pattern_v1"

# Deliberately NOT "buyer_reach". The legacy ScoreDimensions.buyer_reach field
# carries an unapproved weight and kill rule in the quarantined scoring module;
# the `preliminary_` prefix is the repository's existing guard against a
# derivation being silently wired into the final engine.
DIM_BUYER_REACH = "preliminary_buyer_reach"

SIGNAL_COMPETING_LISTING = "marketplace_competing_listing"
SIGNAL_CONTENT_OBSERVATION = "public_content_observation"
SIGNAL_SEARCH_VOLUME = "search_volume"

# Inherited rather than invented. 4A already fixed an "established listing"
# age and 3B a content recency window; introducing different numbers here
# would create two answers to the same question.
from app.services.public_content_features import RECENT_WINDOW_DAYS  # noqa: E402
from app.services.purchase_evidence import (  # noqa: E402
    ESTABLISHED_LISTING_MIN_AGE_DAYS,
)

# How many endpoints are listed per channel. A sample, not a ranking: entries
# are sorted by identifier, never by size, so nothing here can be read as
# "the biggest" or "the best".
MAX_ENDPOINT_SAMPLE = 10

LIMITATIONS = (
    "Buyer Reach records CHANNELS, not buyers. It never estimates how many "
    "buyers exist, how large an audience or market is, or whether anyone will buy.",
    "Channel relevance is INFERRED and permanently capped there: it rests on a "
    "Milestone 1 query hypothesis, not on an observation that the channel serves "
    "this candidate's buyers.",
    "A channel found only through a query shared with other candidates is weaker "
    "evidence for this candidate; that is reported, not corrected for.",
    "Public view counts, subscriber counts and listing counts are never converted "
    "into buyers, audience size, market size, or reachable population.",
    "An observed advertiser auction means only that advertisers bid on the "
    "keyword. It is not purchase intent, conversion likelihood, market "
    "attractiveness, or marketplace competition.",
    "Commercial activity is not derived here. Purchase proxies belong to "
    "Milestone 4A and are cited, never recomputed or restated as sales.",
    "Presence on several channel classes describes the SHAPE of the evidence. "
    "It is never evidence of a better, larger, or more reachable opportunity.",
    "Geography is only known for search evidence. Marketplace and public-content "
    "records carry no per-record geography and it is never inferred from absence.",
    "Distinct sellers and channels approximate distinct reachable endpoints. That "
    "approximation is an unvalidated V1 assumption.",
    "No approved formula converts this evidence into a score, so no numeric Buyer "
    "Reach value is produced.",
)


class ChannelClass(str, Enum):
    """A kind of place a seller could show up. Not a kind of buyer."""

    MARKETPLACE_STOREFRONT = "MARKETPLACE_STOREFRONT"
    CONTENT_PLATFORM_CREATOR = "CONTENT_PLATFORM_CREATOR"
    SEARCH_QUERY_SURFACE = "SEARCH_QUERY_SURFACE"


class ClaimState(str, Enum):
    """How well evidence supports one claim layer about a channel."""

    OBSERVED = "OBSERVED"
    INFERRED = "INFERRED"
    UNKNOWN = "UNKNOWN"


class ReachEvidencePattern(str, Enum):
    """The SHAPE of the observed channel evidence.

    Describes evidence structure only. It is never a statement about quality,
    desirability, opportunity strength, reachability, or predicted commercial
    performance. MULTI_CHANNEL_CLASS is not "better" than SINGLE_CHANNEL_CLASS.
    """

    # No capability produced any channel evidence for this candidate.
    NO_CHANNELS_OBSERVED = "NO_CHANNELS_OBSERVED"
    # Channel evidence exists in exactly one class.
    SINGLE_CHANNEL_CLASS = "SINGLE_CHANNEL_CLASS"
    # Channel evidence exists in more than one class.
    MULTI_CHANNEL_CLASS = "MULTI_CHANNEL_CLASS"
    # Capabilities did not run, failed, or errored, so the channel picture is
    # not merely empty — it is unknown. Never collapses to "no channels".
    CHANNELS_UNKNOWN = "CHANNELS_UNKNOWN"


def _id_sort_key(value: object) -> tuple[str, str]:
    """Total order over identifiers of any type, without coercing them.

    Same rule Milestone 4D-0.1 established for 4A/4B provenance: the type name
    breaks the tie `str()` alone leaves between distinct values that render
    identically, such as 1 and "1".
    """
    return (str(value), type(value).__name__)


@dataclass(slots=True, frozen=True)
class ReachEndpoint:
    """One concrete surface a seller could appear on."""

    endpoint_id: object
    url: str | None
    # Distinct observations of this endpoint (listings by a seller, videos by
    # a channel). A count of OBSERVATIONS, never of buyers or of reach.
    observation_count: int
    evidence_ids: tuple[UUID, ...]
    originating_queries: tuple[str, ...]
    # True when every query that surfaced this endpoint was also generated for
    # another candidate in the same run — weaker evidence for THIS candidate.
    found_only_via_shared_queries: bool


@dataclass(slots=True, frozen=True)
class ReachChannel:
    """One channel class, with the four claim layers kept apart."""

    channel_class: ChannelClass
    platform: str | None

    existence: ClaimState
    relevance: ClaimState
    relevance_basis: str
    activity: ClaimState

    distinct_endpoint_count: int
    endpoint_sample: tuple[ReachEndpoint, ...]

    # Marketplace only. Named so it can never be mistaken for 4A's
    # distinct_seller_count, which counts a DIFFERENT population: sellers
    # carrying review-proxy evidence, not sellers with any relevant listing.
    sellers_with_relevant_listing_count: int | None = None
    distinct_listing_count: int | None = None
    listings_without_seller_id: int | None = None
    sellers_with_multiple_listings: int | None = None
    established_listing_count: int | None = None
    listings_without_created_at: int | None = None
    excluded_physical_listing_count: int | None = None

    # Content only.
    creator_channels_with_relevant_video_count: int | None = None
    distinct_video_count: int | None = None
    videos_without_channel_id: int | None = None
    channels_with_multiple_videos: int | None = None
    recent_video_count: int | None = None
    videos_without_published_at: int | None = None

    # Search only.
    keywords_with_observed_volume: int | None = None
    keywords_without_observed_volume: int | None = None
    # None means UNKNOWN (no keyword carried auction data), not "no auction".
    paid_auction_observed: bool | None = None
    keywords_with_observed_auction: int | None = None
    observed_locations: tuple[str, ...] = ()
    observed_languages: tuple[str, ...] = ()

    evidence_ids: tuple[UUID, ...] = ()
    missing_reason: str | None = None


@dataclass(slots=True, frozen=True)
class BuyerReachProvenance:
    """Lineage of the derived channels, canonically ordered.

    Milestone 4D-0.1 left `collect_listing_views` returning its contributing
    ids and views in arrival order by design, and this milestone does not
    change that. Anything externally observable is therefore canonicalized
    HERE: sorted, multiplicity preserved (sorted(), never set()), identifier
    types untouched.
    """

    candidate_id: UUID
    research_run_id: UUID | None
    evidence_ids: tuple[UUID, ...]
    providers: tuple[str, ...]
    platforms: tuple[str, ...]
    source_truth_classes: tuple[str, ...]
    originating_queries: tuple[str, ...]
    earliest_retrieved_at: datetime | None
    latest_retrieved_at: datetime | None


@dataclass(slots=True, frozen=True)
class BuyerReachResult:
    """Channel evidence for one candidate. Never a buyer estimate."""

    candidate_id: UUID
    state: DimensionState
    # Always None in 4D: no approved formula converts channels into a score.
    value: float | None
    pattern: ReachEvidencePattern
    channels: tuple[ReachChannel, ...]
    provenance: BuyerReachProvenance

    channel_classes_with_observed_evidence: int
    # Literally what it says: evidence came from more than one provider.
    # Deliberately NOT "corroborated" — different provider surfaces do not
    # verify the same proposition, they observe different things.
    observed_across_multiple_providers: bool
    channels_found_only_via_shared_queries: int

    # Permanent UNKNOWN markers. Structural, so the refusal is visible in the
    # payload rather than only in prose.
    buyer_count: TruthClass = TruthClass.UNKNOWN
    audience_size: TruthClass = TruthClass.UNKNOWN
    market_size: TruthClass = TruthClass.UNKNOWN
    conversion_probability: TruthClass = TruthClass.UNKNOWN
    addressability: TruthClass = TruthClass.UNKNOWN
    guaranteed_distribution: TruthClass = TruthClass.UNKNOWN

    missing_reason: str | None = None
    limitations: tuple[str, ...] = LIMITATIONS
    dimension_name: str = DIM_BUYER_REACH
    version: str = BUYER_REACH_VERSION
    pattern_version: str = REACH_PATTERN_VERSION


# ------------------------------------------------------------- collection


@dataclass(slots=True)
class _Endpoint:
    """Mutable accumulator, collapsed into a ReachEndpoint at the end."""

    # Every url seen for this endpoint. Kept as a set and reduced
    # canonically at the end: taking the first-seen url would make the
    # sample depend on the order evidence arrived in.
    urls: set = field(default_factory=set)
    observations: set = field(default_factory=set)
    evidence_ids: list[UUID] = field(default_factory=list)
    queries: set = field(default_factory=set)
    shared_flags: list[bool] = field(default_factory=list)


def _observed(item: EvidenceItem) -> bool:
    return item.truth_class == TruthClass.OBSERVED


def _as_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


def _finish(endpoints: dict, sample_cap: int = MAX_ENDPOINT_SAMPLE) -> tuple:
    """Collapse accumulators into a canonically ordered endpoint sample.

    Sorted by identifier — never by observation count — so the sample cannot
    be read as a ranking of the biggest or most reachable surfaces.
    """
    ordered = sorted(endpoints.items(), key=lambda kv: _id_sort_key(kv[0]))
    return tuple(
        ReachEndpoint(
            endpoint_id=endpoint_id,
            url=min(acc.urls) if acc.urls else None,
            observation_count=len(acc.observations),
            evidence_ids=tuple(sorted(acc.evidence_ids)),
            originating_queries=tuple(sorted(acc.queries)),
            found_only_via_shared_queries=(
                bool(acc.shared_flags) and all(acc.shared_flags)
            ),
        )
        for endpoint_id, acc in ordered[:sample_cap]
    )


def _record_queries(acc: _Endpoint, item: EvidenceItem) -> None:
    """Carry Milestone 4D-0 provenance onto the endpoint.

    None means provenance is UNKNOWN — it contributes no query and no shared
    flag, rather than being treated as "no queries".
    """
    if item.originating_queries is None:
        return
    acc.queries.update(item.originating_queries)
    if item.originating_query_shared is not None:
        acc.shared_flags.append(item.originating_query_shared)


def _marketplace_channel(
    evidence: list[EvidenceItem], now: datetime, missing_reason: str | None
) -> ReachChannel:
    """Distinct seller storefronts carrying a candidate-relevant listing."""
    sellers: dict[object, _Endpoint] = {}
    listings: set = set()
    listings_without_seller: set = set()
    listings_without_created_at: set = set()
    established: set = set()
    excluded_physical: set = set()
    evidence_ids: list[UUID] = []
    platform: str | None = None

    for item in evidence:
        if item.signal_type != SIGNAL_COMPETING_LISTING or not _observed(item):
            continue
        payload = item.raw_payload or {}
        listing_id = payload.get("listing_id")
        if listing_id is None:
            continue
        # A listing the marketplace said is physical is not a channel for a
        # digital product. Absent information is not a disqualification.
        if payload.get("is_digital") is False:
            excluded_physical.add(listing_id)
            continue

        platform = platform or item.marketplace or item.provider
        evidence_ids.append(item.id)
        listings.add(listing_id)

        created = _as_datetime(payload.get("created_at"))
        if created is None:
            listings_without_created_at.add(listing_id)
        elif (now - created).days >= ESTABLISHED_LISTING_MIN_AGE_DAYS:
            established.add(listing_id)

        seller_id = payload.get("seller_id")
        if seller_id is None:
            listings_without_seller.add(listing_id)
            continue

        acc = sellers.setdefault(seller_id, _Endpoint())
        acc.observations.add(listing_id)
        acc.evidence_ids.append(item.id)
        if payload.get("url"):
            acc.urls.add(payload["url"])
        _record_queries(acc, item)

    if not listings and not excluded_physical:
        return ReachChannel(
            channel_class=ChannelClass.MARKETPLACE_STOREFRONT,
            platform=None,
            existence=ClaimState.UNKNOWN,
            relevance=ClaimState.UNKNOWN,
            relevance_basis="no marketplace evidence was available for this candidate",
            activity=ClaimState.UNKNOWN,
            distinct_endpoint_count=0,
            endpoint_sample=(),
            missing_reason=missing_reason,
        )

    return ReachChannel(
        channel_class=ChannelClass.MARKETPLACE_STOREFRONT,
        platform=platform,
        existence=ClaimState.OBSERVED if sellers else ClaimState.UNKNOWN,
        relevance=ClaimState.INFERRED if sellers else ClaimState.UNKNOWN,
        relevance_basis=(
            "returned by a marketplace query generated for this candidate; the "
            "query is a Milestone 1 hypothesis, so relevance is inferred, never "
            "observed"
        ),
        activity=(
            ClaimState.OBSERVED
            if listings and len(listings_without_created_at) < len(listings)
            else ClaimState.UNKNOWN
        ),
        distinct_endpoint_count=len(sellers),
        endpoint_sample=_finish(sellers),
        sellers_with_relevant_listing_count=len(sellers),
        distinct_listing_count=len(listings),
        listings_without_seller_id=len(listings_without_seller),
        sellers_with_multiple_listings=sum(
            1 for acc in sellers.values() if len(acc.observations) > 1
        ),
        established_listing_count=len(established),
        listings_without_created_at=len(listings_without_created_at),
        excluded_physical_listing_count=len(excluded_physical),
        evidence_ids=tuple(sorted(evidence_ids)),
    )


def _content_channel(
    evidence: list[EvidenceItem], now: datetime, missing_reason: str | None
) -> ReachChannel:
    """Distinct creator channels publishing candidate-relevant content.

    Content/discovery reach only. Nothing here says a viewer is a buyer, and
    no view or subscriber count is read.
    """
    channels: dict[object, _Endpoint] = {}
    videos: set = set()
    videos_without_channel: set = set()
    videos_without_published: set = set()
    recent: set = set()
    evidence_ids: list[UUID] = []
    platform: str | None = None

    for item in evidence:
        if item.signal_type != SIGNAL_CONTENT_OBSERVATION or not _observed(item):
            continue
        payload = item.raw_payload or {}
        video_id = payload.get("video_id")
        if video_id is None:
            continue

        platform = platform or item.platform or item.provider
        evidence_ids.append(item.id)
        videos.add(video_id)

        published = _as_datetime(payload.get("published_at"))
        if published is None:
            videos_without_published.add(video_id)
        elif (now - published).days <= RECENT_WINDOW_DAYS:
            recent.add(video_id)

        channel_id = payload.get("channel_id")
        if channel_id is None:
            videos_without_channel.add(video_id)
            continue

        acc = channels.setdefault(channel_id, _Endpoint())
        acc.observations.add(video_id)
        acc.evidence_ids.append(item.id)
        if payload.get("url"):
            acc.urls.add(payload["url"])
        _record_queries(acc, item)

    if not videos:
        return ReachChannel(
            channel_class=ChannelClass.CONTENT_PLATFORM_CREATOR,
            platform=None,
            existence=ClaimState.UNKNOWN,
            relevance=ClaimState.UNKNOWN,
            relevance_basis="no public-content evidence was available for this candidate",
            activity=ClaimState.UNKNOWN,
            distinct_endpoint_count=0,
            endpoint_sample=(),
            missing_reason=missing_reason,
        )

    return ReachChannel(
        channel_class=ChannelClass.CONTENT_PLATFORM_CREATOR,
        platform=platform,
        existence=ClaimState.OBSERVED if channels else ClaimState.UNKNOWN,
        relevance=ClaimState.INFERRED if channels else ClaimState.UNKNOWN,
        relevance_basis=(
            "returned by a content query generated for this candidate; this is "
            "an observable content audience around the topic, never evidence "
            "that any viewer will buy"
        ),
        activity=(
            ClaimState.OBSERVED
            if len(videos_without_published) < len(videos)
            else ClaimState.UNKNOWN
        ),
        distinct_endpoint_count=len(channels),
        endpoint_sample=_finish(channels),
        creator_channels_with_relevant_video_count=len(channels),
        distinct_video_count=len(videos),
        videos_without_channel_id=len(videos_without_channel),
        channels_with_multiple_videos=sum(
            1 for acc in channels.values() if len(acc.observations) > 1
        ),
        recent_video_count=len(recent),
        videos_without_published_at=len(videos_without_published),
        evidence_ids=tuple(sorted(evidence_ids)),
    )


def _search_channel(
    evidence: list[EvidenceItem], missing_reason: str | None
) -> ReachChannel:
    """Keyword surfaces people actually search, and whether ads run there.

    `paid_auction_observed` means ONLY that an advertiser auction was
    observably present for at least one keyword. It is never purchase intent,
    conversion likelihood, market attractiveness, or marketplace competition.
    """
    keywords: dict[object, _Endpoint] = {}
    with_volume: set = set()
    without_volume: set = set()
    with_auction: set = set()
    auction_data_seen = False
    locations: set = set()
    languages: set = set()
    evidence_ids: list[UUID] = []
    platform: str | None = None

    for item in evidence:
        if item.signal_type != SIGNAL_SEARCH_VOLUME:
            continue
        payload = item.raw_payload or {}
        keyword = payload.get("keyword")
        if keyword is None:
            continue

        platform = platform or item.provider
        evidence_ids.append(item.id)

        if _observed(item) and payload.get("search_volume") is not None:
            with_volume.add(keyword)
        else:
            without_volume.add(keyword)

        # Cited from what the provider already returned; no new statistic is
        # computed here, and none is compared against 3A's median_cpc.
        cpc = payload.get("cpc")
        low_bid = payload.get("low_top_of_page_bid")
        high_bid = payload.get("high_top_of_page_bid")
        if cpc is not None or low_bid is not None or high_bid is not None:
            auction_data_seen = True
            if any(
                isinstance(v, (int, float)) and v > 0 for v in (cpc, low_bid, high_bid)
            ):
                with_auction.add(keyword)

        # Geography is known ONLY for search evidence. It is never inferred
        # for marketplace or content records from their absent values.
        if item.geography:
            locations.add(item.geography)
        if item.language:
            languages.add(item.language)

        acc = keywords.setdefault(keyword, _Endpoint())
        acc.observations.add(keyword)
        acc.evidence_ids.append(item.id)
        _record_queries(acc, item)

    if not keywords:
        return ReachChannel(
            channel_class=ChannelClass.SEARCH_QUERY_SURFACE,
            platform=None,
            existence=ClaimState.UNKNOWN,
            relevance=ClaimState.UNKNOWN,
            relevance_basis="no search-demand evidence was available for this candidate",
            activity=ClaimState.UNKNOWN,
            distinct_endpoint_count=0,
            endpoint_sample=(),
            missing_reason=missing_reason,
        )

    return ReachChannel(
        channel_class=ChannelClass.SEARCH_QUERY_SURFACE,
        platform=platform,
        existence=ClaimState.OBSERVED if with_volume else ClaimState.UNKNOWN,
        relevance=ClaimState.INFERRED if with_volume else ClaimState.UNKNOWN,
        relevance_basis=(
            "a keyword generated for this candidate returned observed search "
            "volume; searchers are not buyers and their number is not a buyer "
            "count"
        ),
        # A keyword surface has no observable recency in this evidence.
        activity=ClaimState.UNKNOWN,
        distinct_endpoint_count=len(keywords),
        endpoint_sample=_finish(keywords),
        keywords_with_observed_volume=len(with_volume),
        keywords_without_observed_volume=len(without_volume),
        # None = UNKNOWN (no keyword carried auction data), never "no auction".
        paid_auction_observed=bool(with_auction) if auction_data_seen else None,
        keywords_with_observed_auction=len(with_auction) if auction_data_seen else None,
        observed_locations=tuple(sorted(locations)),
        observed_languages=tuple(sorted(languages)),
        evidence_ids=tuple(sorted(evidence_ids)),
    )


def _build_provenance(
    candidate_id: UUID,
    research_run_id: UUID | None,
    evidence: list[EvidenceItem],
    channels: tuple[ReachChannel, ...],
) -> BuyerReachProvenance:
    contributing_ids = {eid for channel in channels for eid in channel.evidence_ids}
    contributing = [item for item in evidence if item.id in contributing_ids]
    retrieved = sorted(i.retrieved_at for i in contributing if i.retrieved_at is not None)
    queries = {
        query
        for item in contributing
        if item.originating_queries is not None
        for query in item.originating_queries
    }
    return BuyerReachProvenance(
        candidate_id=candidate_id,
        research_run_id=research_run_id,
        # Canonicalized here, because collect_listing_views deliberately keeps
        # arrival order and 4D must not change it. sorted(), never set().
        evidence_ids=tuple(sorted(contributing_ids)),
        providers=tuple(sorted({i.provider for i in contributing})),
        platforms=tuple(
            sorted({p for i in contributing if (p := i.platform or i.marketplace)})
        ),
        source_truth_classes=tuple(sorted({i.truth_class.value for i in contributing})),
        originating_queries=tuple(sorted(queries)),
        earliest_retrieved_at=retrieved[0] if retrieved else None,
        latest_retrieved_at=retrieved[-1] if retrieved else None,
    )


def _classify(
    channels: tuple[ReachChannel, ...], any_capability_unknown: bool
) -> ReachEvidencePattern:
    """reach_evidence_pattern_v1. Evidence SHAPE only, never quality."""
    observed = [c for c in channels if c.existence == ClaimState.OBSERVED]
    if len(observed) > 1:
        return ReachEvidencePattern.MULTI_CHANNEL_CLASS
    if len(observed) == 1:
        return ReachEvidencePattern.SINGLE_CHANNEL_CLASS
    # Nothing observed. "We did not look" is not "there is nothing".
    if any_capability_unknown:
        return ReachEvidencePattern.CHANNELS_UNKNOWN
    return ReachEvidencePattern.NO_CHANNELS_OBSERVED


# Capability names, mirrored from the orchestrator so this module stays
# importable on its own without reaching back into it.
CAPABILITY_MARKETPLACE = "marketplace"
CAPABILITY_PUBLIC_CONTENT = "public_content"
CAPABILITY_SEARCH_DEMAND = "search_demand"

# A capability that produced no evidence FOR THIS CANDIDATE is a different
# fact from one that never ran or failed. Only the latter makes the channel
# picture unknown rather than empty.
_REASONS_MEANING_NOT_LOOKED = frozenset(
    {
        "capability_not_requested",
        "capability_provider_failed",
        "capability_unexpected_error",
    }
)


def extract_buyer_reach(
    candidate_id: UUID,
    evidence: list[EvidenceItem],
    missing_reasons: dict[str, str] | None = None,
    research_run_id: UUID | None = None,
    now: datetime | None = None,
) -> BuyerReachResult:
    """Derive Buyer Reach for one candidate from stored evidence.

    Deterministic and pure: identical evidence yields identical output,
    whatever order the records arrive in. No provider calls, no quota, no
    score, and no conversion of any observation into a buyer count.

    `missing_reasons` maps a capability name to why it produced nothing. It
    is what keeps "we did not look" distinguishable from "we looked and found
    nothing" — without it, a failed capability would read as an absent
    channel, which is the one thing missing evidence must never become.
    """
    reference_time = now or datetime.now(UTC)
    reasons = missing_reasons or {}

    marketplace = _marketplace_channel(
        evidence, reference_time, reasons.get(CAPABILITY_MARKETPLACE)
    )
    content = _content_channel(
        evidence, reference_time, reasons.get(CAPABILITY_PUBLIC_CONTENT)
    )
    search = _search_channel(evidence, reasons.get(CAPABILITY_SEARCH_DEMAND))
    channels = (marketplace, content, search)

    provenance = _build_provenance(candidate_id, research_run_id, evidence, channels)
    observed_channels = [c for c in channels if c.existence == ClaimState.OBSERVED]

    any_capability_unknown = any(
        reason in _REASONS_MEANING_NOT_LOOKED for reason in reasons.values()
    )
    pattern = _classify(channels, any_capability_unknown)

    shared_only = sum(
        1
        for channel in observed_channels
        if channel.endpoint_sample
        and all(e.found_only_via_shared_queries for e in channel.endpoint_sample)
    )

    if observed_channels:
        state = DimensionState.EVIDENCE_PRESENT_UNSCORED
        missing_reason = None
    elif any_capability_unknown:
        state = DimensionState.MISSING
        missing_reason = next(
            reason
            for reason in reasons.values()
            if reason in _REASONS_MEANING_NOT_LOOKED
        )
    else:
        # Capabilities ran and returned nothing for this candidate. That is an
        # observed absence of channel evidence, reported as UNKNOWN rather
        # than as zero reach: no channel was found, which is not the same as
        # no channel existing.
        state = DimensionState.UNKNOWN
        missing_reason = "no_channel_evidence_returned_for_candidate"

    return BuyerReachResult(
        candidate_id=candidate_id,
        state=state,
        value=None,
        pattern=pattern,
        channels=channels,
        provenance=provenance,
        channel_classes_with_observed_evidence=len(observed_channels),
        observed_across_multiple_providers=len(set(provenance.providers)) > 1,
        channels_found_only_via_shared_queries=shared_only,
        missing_reason=missing_reason,
    )


# Vocabulary this milestone must never emit about its own findings. Used by
# the test suite to scan serialized output, and kept here so the rule lives
# beside the code it constrains.
FORBIDDEN_REACH_CLAIMS = (
    r"\bbuyer count\b",
    r"\bnumber of buyers\b",
    r"\bmarket size\b",
    r"\btotal addressable\b",
    r"\bconversion rate\b",
    r"\bwill buy\b",
    r"\breachable buyers\b",
    r"\bguaranteed (distribution|reach)\b",
    r"\bpurchase intent\b",
)

_FORBIDDEN_RE = tuple(re.compile(p, re.IGNORECASE) for p in FORBIDDEN_REACH_CLAIMS)


def unsupported_reach_claim_in(text: str) -> str | None:
    """Return the forbidden-claim pattern a text asserts, or None."""
    for pattern in _FORBIDDEN_RE:
        if pattern.search(text):
            return pattern.pattern
    return None
