"""Milestone 4D tests: Buyer Reach.

No network, no live API calls, no new provider.

The rule the whole milestone rests on is that CHANNEL evidence is not BUYER
evidence, and most of this file exists to keep that rule enforced rather than
merely documented. Relevant videos with engagement demonstrate an observable
content audience around a problem; they never establish that a viewer will
buy. Etsy listings demonstrate marketplace supply; they never establish buyer
reach. An ad auction shows advertisers bid; it says nothing about intent.

The second rule is that missing evidence must never become zero. "We did not
look", "we looked and it failed", and "we looked and found nothing" are three
different facts and stay three different states.
"""

import random
import re
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from app.domain.enums import EvidencePurpose, TruthClass
from app.domain.models import EvidenceItem
from app.services.buyer_reach import (
    BUYER_REACH_VERSION,
    DIM_BUYER_REACH,
    FORBIDDEN_REACH_CLAIMS,
    ChannelClass,
    ClaimState,
    ReachEvidencePattern,
    extract_buyer_reach,
    unsupported_reach_claim_in,
)
from app.services.preliminary_dimensions import DimensionState
from app.services.purchase_evidence import extract_purchase_evidence

NOW = datetime(2026, 1, 1, tzinfo=UTC)
SHUFFLES = 150

MARKETPLACE = "marketplace"
CONTENT = "public_content"
SEARCH = "search_demand"

NOT_REQUESTED = "capability_not_requested"
FAILED = "capability_provider_failed"
UNEXPECTED = "capability_unexpected_error"


# ----------------------------------------------------------------- fixtures


def listing_record(
    listing_id="L1",
    *,
    seller_id="shopA",
    candidate_id=None,
    is_digital=True,
    created_days_ago=400,
    queries=("printable planner",),
    shared=False,
    payload_hash="h",
    item_id=None,
    signal="marketplace_competing_listing",
    truth=TruthClass.OBSERVED,
) -> EvidenceItem:
    payload = {
        "listing_id": listing_id,
        "seller_id": seller_id,
        "is_digital": is_digital,
        "url": f"https://www.etsy.com/listing/{listing_id}",
        "title": f"Listing {listing_id}",
    }
    if created_days_ago is not None:
        payload["created_at"] = (NOW - timedelta(days=created_days_ago)).isoformat()
    fields = dict(
        candidate_id=candidate_id,
        signal_type=signal,
        purpose=EvidencePurpose.COMPETITION,
        truth_class=truth,
        provider="etsy",
        marketplace="etsy",
        collection_method="official_api",
        retrieved_at=NOW - timedelta(days=1),
        raw_payload=payload,
        raw_payload_hash=payload_hash,
        originating_queries=tuple(queries) if queries is not None else None,
        originating_query_shared=shared if queries is not None else None,
    )
    if item_id is not None:
        fields["id"] = item_id
    return EvidenceItem(**fields)


def video_record(
    video_id="V1",
    *,
    channel_id="chanA",
    candidate_id=None,
    published_days_ago=30,
    queries=("budget tutorial",),
    shared=False,
    payload_hash="hv",
    item_id=None,
) -> EvidenceItem:
    payload = {
        "video_id": video_id,
        "channel_id": channel_id,
        "channel_title": f"Channel {channel_id}",
        "url": f"https://www.youtube.com/watch?v={video_id}",
        "title": f"Video {video_id}",
        "view_count": 500000,
        "channel_subscriber_count": 250000,
    }
    if published_days_ago is not None:
        payload["published_at"] = (NOW - timedelta(days=published_days_ago)).isoformat()
    fields = dict(
        candidate_id=candidate_id,
        signal_type="public_content_observation",
        purpose=EvidencePurpose.CONTENT,
        truth_class=TruthClass.OBSERVED,
        provider="youtube",
        platform="youtube",
        collection_method="official_api",
        retrieved_at=NOW - timedelta(days=1),
        raw_payload=payload,
        raw_payload_hash=payload_hash,
        originating_queries=tuple(queries) if queries is not None else None,
        originating_query_shared=shared if queries is not None else None,
    )
    if item_id is not None:
        fields["id"] = item_id
    return EvidenceItem(**fields)


def keyword_record(
    keyword="budget planner",
    *,
    volume=1200,
    cpc=None,
    low_bid=None,
    high_bid=None,
    candidate_id=None,
    geography="US",
    language="en",
    queries=("budget planner",),
    shared=False,
    truth=TruthClass.OBSERVED,
) -> EvidenceItem:
    payload = {"keyword": keyword, "search_volume": volume}
    if cpc is not None:
        payload["cpc"] = cpc
    if low_bid is not None:
        payload["low_top_of_page_bid"] = low_bid
    if high_bid is not None:
        payload["high_top_of_page_bid"] = high_bid
    return EvidenceItem(
        candidate_id=candidate_id,
        signal_type="search_volume",
        purpose=EvidencePurpose.SEARCH_DEMAND,
        truth_class=truth,
        provider="dataforseo",
        collection_method="official_api",
        geography=geography,
        language=language,
        retrieved_at=NOW - timedelta(days=1),
        raw_payload=payload,
        raw_payload_hash=f"hk-{keyword}",
        originating_queries=tuple(queries) if queries is not None else None,
        originating_query_shared=shared if queries is not None else None,
    )


def reach(evidence, missing_reasons=None, candidate_id=None):
    return extract_buyer_reach(
        candidate_id=candidate_id or uuid4(),
        evidence=list(evidence),
        missing_reasons=missing_reasons,
        now=NOW,
    )


def channel(result, channel_class):
    return next(c for c in result.channels if c.channel_class == channel_class)


def shuffled(evidence, seed=2026, count=SHUFFLES):
    rng = random.Random(seed)
    for _ in range(count):
        variant = list(evidence)
        rng.shuffle(variant)
        yield variant


# ------------------------------------------------- the no-score guarantee


def test_buyer_reach_never_produces_a_value():
    result = reach([listing_record()])
    assert result.value is None
    assert result.state == DimensionState.EVIDENCE_PRESENT_UNSCORED
    assert result.version == BUYER_REACH_VERSION


def test_dimension_name_cannot_collide_with_legacy_score_dimensions():
    """The legacy ScoreDimensions.buyer_reach carries an unapproved weight."""
    assert DIM_BUYER_REACH == "preliminary_buyer_reach"
    assert DIM_BUYER_REACH != "buyer_reach"


def test_permanent_unknown_markers_are_structural():
    result = reach([listing_record(), video_record(), keyword_record()])
    for marker in (
        result.buyer_count,
        result.audience_size,
        result.market_size,
        result.conversion_probability,
        result.addressability,
        result.guaranteed_distribution,
    ):
        assert marker == TruthClass.UNKNOWN


# ------------------------------------------------------- channel classes


def test_marketplace_channel_counts_distinct_sellers():
    result = reach(
        [
            listing_record("L1", seller_id="shopA"),
            listing_record("L2", seller_id="shopB", payload_hash="h2"),
            listing_record("L3", seller_id="shopA", payload_hash="h3"),
        ]
    )
    marketplace = channel(result, ChannelClass.MARKETPLACE_STOREFRONT)
    assert marketplace.sellers_with_relevant_listing_count == 2
    assert marketplace.distinct_listing_count == 3
    assert marketplace.sellers_with_multiple_listings == 1
    assert marketplace.existence == ClaimState.OBSERVED


def test_one_seller_with_many_listings_is_one_endpoint():
    result = reach(
        [listing_record(f"L{i}", seller_id="shopA", payload_hash=f"h{i}") for i in range(10)]
    )
    marketplace = channel(result, ChannelClass.MARKETPLACE_STOREFRONT)
    assert marketplace.distinct_endpoint_count == 1
    assert marketplace.distinct_listing_count == 10
    assert len(marketplace.endpoint_sample) == 1
    assert marketplace.endpoint_sample[0].observation_count == 10


def test_content_channel_counts_distinct_creators():
    result = reach(
        [
            video_record("V1", channel_id="chanA"),
            video_record("V2", channel_id="chanB", payload_hash="hv2"),
            video_record("V3", channel_id="chanA", payload_hash="hv3"),
        ]
    )
    content = channel(result, ChannelClass.CONTENT_PLATFORM_CREATOR)
    assert content.creator_channels_with_relevant_video_count == 2
    assert content.distinct_video_count == 3
    assert content.channels_with_multiple_videos == 1


def test_search_channel_counts_keywords_with_observed_volume():
    result = reach(
        [
            keyword_record("budget planner", volume=1200),
            keyword_record("budget tracker", volume=None, truth=TruthClass.UNKNOWN),
        ]
    )
    search = channel(result, ChannelClass.SEARCH_QUERY_SURFACE)
    assert search.keywords_with_observed_volume == 1
    assert search.keywords_without_observed_volume == 1


def test_relevance_is_permanently_inferred_never_observed():
    """A query is a Milestone 1 hypothesis; relevance can never be observed."""
    result = reach([listing_record(), video_record(), keyword_record()])
    for reach_channel in result.channels:
        assert reach_channel.relevance != ClaimState.OBSERVED
        if reach_channel.existence == ClaimState.OBSERVED:
            assert reach_channel.relevance == ClaimState.INFERRED
            assert reach_channel.relevance_basis


def test_commercial_activity_is_not_a_channel_class():
    """Purchase proxies stay owned by 4A and never become a reach surface."""
    classes = {c.channel_class for c in reach([listing_record()]).channels}
    assert classes == {
        ChannelClass.MARKETPLACE_STOREFRONT,
        ChannelClass.CONTENT_PLATFORM_CREATOR,
        ChannelClass.SEARCH_QUERY_SURFACE,
    }
    assert not any("COMMERCIAL" in c.value for c in ChannelClass)


# ------------------------------- capability-aware MISSING (never zero)


def test_capability_failed_is_unknown_not_zero_reach():
    result = reach([], missing_reasons={MARKETPLACE: FAILED})
    assert result.state == DimensionState.MISSING
    assert result.pattern == ReachEvidencePattern.CHANNELS_UNKNOWN
    assert result.missing_reason == FAILED
    assert channel(result, ChannelClass.MARKETPLACE_STOREFRONT).missing_reason == FAILED


def test_capability_not_requested_is_distinguishable_from_failed():
    not_requested = reach([], missing_reasons={MARKETPLACE: NOT_REQUESTED})
    failed = reach([], missing_reasons={MARKETPLACE: FAILED})
    unexpected = reach([], missing_reasons={MARKETPLACE: UNEXPECTED})
    assert not_requested.missing_reason == NOT_REQUESTED
    assert failed.missing_reason == FAILED
    assert unexpected.missing_reason == UNEXPECTED
    assert len({not_requested.missing_reason, failed.missing_reason,
                unexpected.missing_reason}) == 3


def test_capability_ran_and_found_nothing_is_its_own_state():
    """Different from 'we did not look'. Neither is zero reach."""
    result = reach([], missing_reasons={})
    assert result.state == DimensionState.UNKNOWN
    assert result.pattern == ReachEvidencePattern.NO_CHANNELS_OBSERVED
    assert result.missing_reason == "no_channel_evidence_returned_for_candidate"


def test_missing_reason_helpers_agree_for_every_capability_outcome():
    """The two helpers encode related semantics in different layers.

    `_missing_reasons` answers "why does this DIMENSION have no evidence" for
    3C's profile builder; `_capability_missing_reasons` answers the same
    question keyed by CAPABILITY for Buyer Reach. They read the same outcomes
    and must never drift apart — if they did, 3C and 4D would disagree about
    whether a capability failed or simply found nothing.
    """
    from app.services.preliminary_dimensions import (
        DIM_AUDIENCE_INTEREST,
        DIM_PRICE_EVIDENCE,
        DIM_SEARCH_DEMAND,
    )
    from app.services.research_orchestration import (
        CAPABILITY_MARKETPLACE,
        CAPABILITY_PUBLIC_CONTENT,
        CAPABILITY_SEARCH_DEMAND,
        MISSING_NO_EVIDENCE_FOR_CANDIDATE,
        STATUS_NOT_REQUESTED,
        STATUS_PROVIDER_FAILED,
        STATUS_UNEXPECTED_PROVIDER_ERROR,
        CapabilityOutcome,
        _capability_missing_reasons,
        _missing_reasons,
    )

    dimension_of = {
        CAPABILITY_SEARCH_DEMAND: DIM_SEARCH_DEMAND,
        CAPABILITY_MARKETPLACE: DIM_PRICE_EVIDENCE,
        CAPABILITY_PUBLIC_CONTENT: DIM_AUDIENCE_INTEREST,
    }
    statuses = (
        STATUS_NOT_REQUESTED,
        STATUS_PROVIDER_FAILED,
        STATUS_UNEXPECTED_PROVIDER_ERROR,
        "COMPLETE",
        "PARTIAL",
    )
    for capability, dimension in dimension_of.items():
        for status in statuses:
            outcomes = [CapabilityOutcome(capability=capability, status=status)]

            # The capability produced nothing: both helpers must name the
            # same reason.
            by_capability = _capability_missing_reasons({capability}, outcomes)
            by_dimension = _missing_reasons({capability}, outcomes)
            assert by_capability[capability] == by_dimension[dimension], (
                capability,
                status,
            )

            # The capability produced evidence: the capability-keyed helper
            # stays silent, the dimension-keyed one reports the per-candidate
            # absence. Different questions, both correct.
            assert _capability_missing_reasons(set(), outcomes) == {}
            assert (
                _missing_reasons(set(), outcomes)[dimension]
                == MISSING_NO_EVIDENCE_FOR_CANDIDATE
            )


def test_absent_field_within_present_evidence_is_unknown_not_zero():
    """The fifth missing case: evidence exists, one field does not.

    A listing with no seller_id must not silently become "zero sellers" —
    it is one listing whose storefront is unknown.
    """
    result = reach([listing_record("L1", seller_id=None)])
    marketplace = channel(result, ChannelClass.MARKETPLACE_STOREFRONT)
    assert marketplace.distinct_listing_count == 1
    assert marketplace.listings_without_seller_id == 1
    assert marketplace.sellers_with_relevant_listing_count == 0
    # The channel exists as evidence but has no observable endpoint.
    assert marketplace.existence == ClaimState.UNKNOWN
    assert marketplace.missing_reason is None  # the capability did run


def test_marketplace_unavailable_while_content_exists():
    result = reach([video_record()], missing_reasons={MARKETPLACE: FAILED})
    assert result.state == DimensionState.EVIDENCE_PRESENT_UNSCORED
    assert channel(result, ChannelClass.CONTENT_PLATFORM_CREATOR).existence == (
        ClaimState.OBSERVED
    )
    marketplace = channel(result, ChannelClass.MARKETPLACE_STOREFRONT)
    assert marketplace.existence == ClaimState.UNKNOWN
    assert marketplace.missing_reason == FAILED
    assert marketplace.distinct_endpoint_count == 0
    assert result.pattern == ReachEvidencePattern.SINGLE_CHANNEL_CLASS


def test_content_unavailable_while_marketplace_exists():
    result = reach([listing_record()], missing_reasons={CONTENT: NOT_REQUESTED})
    assert channel(result, ChannelClass.MARKETPLACE_STOREFRONT).existence == (
        ClaimState.OBSERVED
    )
    content = channel(result, ChannelClass.CONTENT_PLATFORM_CREATOR)
    assert content.existence == ClaimState.UNKNOWN
    assert content.missing_reason == NOT_REQUESTED


# --------------------------------------------------- evidence patterns


def test_pattern_describes_shape_only():
    single = reach([listing_record()])
    multi = reach([listing_record(), video_record()])
    assert single.pattern == ReachEvidencePattern.SINGLE_CHANNEL_CLASS
    assert multi.pattern == ReachEvidencePattern.MULTI_CHANNEL_CLASS
    # Shape, not quality: neither result carries a value or a ranking.
    assert single.value is None and multi.value is None


def test_pattern_enum_has_exactly_the_four_approved_members():
    assert [p.value for p in ReachEvidencePattern] == [
        "NO_CHANNELS_OBSERVED",
        "SINGLE_CHANNEL_CLASS",
        "MULTI_CHANNEL_CLASS",
        "CHANNELS_UNKNOWN",
    ]


def test_multiple_providers_is_reported_literally():
    one = reach([listing_record()])
    two = reach([listing_record(), video_record()])
    assert one.observed_across_multiple_providers is False
    assert two.observed_across_multiple_providers is True
    # The field name must not claim verification.
    assert not hasattr(two, "corroborated_across_independent_sources")


# ------------------------------------------------ paid auction semantics


def test_paid_auction_unknown_when_no_auction_data_present():
    """None means UNKNOWN, never 'no auction'."""
    search = channel(reach([keyword_record()]), ChannelClass.SEARCH_QUERY_SURFACE)
    assert search.paid_auction_observed is None
    assert search.keywords_with_observed_auction is None


def test_paid_auction_observed_when_a_bid_is_present():
    search = channel(
        reach([keyword_record(cpc=1.25)]), ChannelClass.SEARCH_QUERY_SURFACE
    )
    assert search.paid_auction_observed is True
    assert search.keywords_with_observed_auction == 1


def test_paid_auction_absent_when_auction_data_says_zero():
    search = channel(
        reach([keyword_record(cpc=0.0, low_bid=0.0)]),
        ChannelClass.SEARCH_QUERY_SURFACE,
    )
    assert search.paid_auction_observed is False
    assert search.keywords_with_observed_auction == 0


def test_paid_auction_never_implies_intent_or_competition():
    result = reach([keyword_record(cpc=9.99)])
    rendered = str(asdict(result))
    for banned in ("purchase intent", "conversion", "market attractiveness"):
        assert banned not in rendered.lower() or "not " in rendered.lower()
    assert result.buyer_count == TruthClass.UNKNOWN


# ---------------------------------------------------- geography honesty


def test_geography_is_known_only_for_search_evidence():
    result = reach([listing_record(), video_record(), keyword_record()])
    search = channel(result, ChannelClass.SEARCH_QUERY_SURFACE)
    assert search.observed_locations == ("US",)
    assert search.observed_languages == ("en",)
    for other in (ChannelClass.MARKETPLACE_STOREFRONT, ChannelClass.CONTENT_PLATFORM_CREATOR):
        assert channel(result, other).observed_locations == ()
        assert channel(result, other).observed_languages == ()


# ------------------------------------- dedupe, determinism, provenance


def test_duplicate_listings_do_not_inflate_endpoint_counts():
    single = reach([listing_record("L1", seller_id="shopA")])
    duplicated = reach([listing_record("L1", seller_id="shopA")] * 5)
    assert (
        channel(duplicated, ChannelClass.MARKETPLACE_STOREFRONT).distinct_endpoint_count
        == channel(single, ChannelClass.MARKETPLACE_STOREFRONT).distinct_endpoint_count
        == 1
    )
    assert (
        channel(duplicated, ChannelClass.MARKETPLACE_STOREFRONT).distinct_listing_count
        == 1
    )


def test_duplicate_videos_do_not_inflate_channel_counts():
    duplicated = reach([video_record("V1", channel_id="chanA")] * 4)
    content = channel(duplicated, ChannelClass.CONTENT_PLATFORM_CREATOR)
    assert content.distinct_endpoint_count == 1
    assert content.distinct_video_count == 1


def test_output_is_identical_across_shuffled_permutations():
    candidate_id = uuid4()
    evidence = (
        [listing_record(f"L{i}", seller_id=f"shop{i % 3}", payload_hash=f"h{i}") for i in range(6)]
        + [video_record(f"V{i}", channel_id=f"chan{i % 2}", payload_hash=f"hv{i}") for i in range(4)]
        + [keyword_record(f"kw{i}", cpc=0.5 + i) for i in range(3)]
    )
    baseline = extract_buyer_reach(
        candidate_id=candidate_id, evidence=list(evidence), now=NOW
    )
    for variant in shuffled(evidence):
        assert (
            extract_buyer_reach(candidate_id=candidate_id, evidence=variant, now=NOW)
            == baseline
        )


def test_reversed_and_repeated_execution_are_stable():
    candidate_id = uuid4()
    evidence = [listing_record(f"L{i}", payload_hash=f"h{i}") for i in range(5)]
    baseline = extract_buyer_reach(candidate_id=candidate_id, evidence=list(evidence), now=NOW)
    assert (
        extract_buyer_reach(candidate_id=candidate_id, evidence=list(reversed(evidence)), now=NOW)
        == baseline
    )
    for _ in range(5):
        assert (
            extract_buyer_reach(candidate_id=candidate_id, evidence=list(evidence), now=NOW)
            == baseline
        )


def test_provenance_is_canonically_sorted():
    """4D canonicalizes its own lineage; collect_listing_views keeps arrival order."""
    result = reach(
        [listing_record(f"L{i}", payload_hash=f"h{i}") for i in range(5)]
        + [video_record(f"V{i}", payload_hash=f"hv{i}") for i in range(3)]
    )
    provenance = result.provenance
    assert list(provenance.evidence_ids) == sorted(provenance.evidence_ids)
    assert list(provenance.providers) == sorted(provenance.providers)
    assert list(provenance.originating_queries) == sorted(provenance.originating_queries)
    for reach_channel in result.channels:
        assert list(reach_channel.evidence_ids) == sorted(reach_channel.evidence_ids)
        for endpoint in reach_channel.endpoint_sample:
            assert list(endpoint.evidence_ids) == sorted(endpoint.evidence_ids)


def test_provenance_preserves_duplicate_multiplicity():
    """sorted(), never set(): a hash-less record is never collapsed."""
    duplicated = listing_record("L1", payload_hash=None, item_id=UUID(int=9))
    result = reach([duplicated, duplicated])
    endpoint = channel(result, ChannelClass.MARKETPLACE_STOREFRONT).endpoint_sample[0]
    assert endpoint.evidence_ids == (UUID(int=9), UUID(int=9))


def test_endpoint_sample_is_sorted_by_identifier_not_by_size():
    """Sorting by size would make the sample read as a ranking."""
    evidence = [
        listing_record("L1", seller_id="zzz", payload_hash="h1"),
        listing_record("L2", seller_id="aaa", payload_hash="h2"),
        listing_record("L3", seller_id="aaa", payload_hash="h3"),
    ]
    sample = channel(reach(evidence), ChannelClass.MARKETPLACE_STOREFRONT).endpoint_sample
    assert [e.endpoint_id for e in sample] == ["aaa", "zzz"]


def test_mixed_identifier_types_order_without_coercion():
    evidence = [
        listing_record("L1", seller_id="shopA", payload_hash="h1"),
        listing_record("L2", seller_id=5, payload_hash="h2"),
        listing_record("L3", seller_id=1, payload_hash="h3"),
    ]
    sample = channel(reach(evidence), ChannelClass.MARKETPLACE_STOREFRONT).endpoint_sample
    ids = [e.endpoint_id for e in sample]
    assert 5 in ids and "5" not in ids
    assert ids == sorted(ids, key=lambda v: (str(v), type(v).__name__))


# ----------------------------------------------- relevance dilution


def test_shared_query_only_channels_are_flagged():
    result = reach([listing_record(shared=True)])
    endpoint = channel(result, ChannelClass.MARKETPLACE_STOREFRONT).endpoint_sample[0]
    assert endpoint.found_only_via_shared_queries is True
    assert result.channels_found_only_via_shared_queries == 1


def test_exclusive_query_channels_are_not_flagged():
    result = reach([listing_record(shared=False)])
    assert result.channels_found_only_via_shared_queries == 0


def test_unknown_query_provenance_contributes_no_shared_flag():
    """None means UNKNOWN, never 'not shared'."""
    result = reach([listing_record(queries=None)])
    endpoint = channel(result, ChannelClass.MARKETPLACE_STOREFRONT).endpoint_sample[0]
    assert endpoint.originating_queries == ()
    assert endpoint.found_only_via_shared_queries is False


# --------------------------------------------- absent and awkward data


def test_physical_only_listings_create_no_digital_channel():
    result = reach([listing_record("L1", is_digital=False)])
    marketplace = channel(result, ChannelClass.MARKETPLACE_STOREFRONT)
    assert marketplace.distinct_endpoint_count == 0
    assert marketplace.excluded_physical_listing_count == 1
    assert marketplace.existence == ClaimState.UNKNOWN


def test_absent_is_digital_is_not_a_disqualification():
    result = reach([listing_record("L1", is_digital=None)])
    assert channel(result, ChannelClass.MARKETPLACE_STOREFRONT).distinct_endpoint_count == 1


def test_missing_seller_id_is_counted_as_unknown_not_a_seller():
    result = reach(
        [
            listing_record("L1", seller_id=None),
            listing_record("L2", seller_id="shopA", payload_hash="h2"),
        ]
    )
    marketplace = channel(result, ChannelClass.MARKETPLACE_STOREFRONT)
    assert marketplace.sellers_with_relevant_listing_count == 1
    assert marketplace.listings_without_seller_id == 1
    assert marketplace.distinct_listing_count == 2


def test_missing_channel_id_is_counted_as_unknown():
    result = reach([video_record("V1", channel_id=None)])
    content = channel(result, ChannelClass.CONTENT_PLATFORM_CREATOR)
    assert content.creator_channels_with_relevant_video_count == 0
    assert content.videos_without_channel_id == 1


def test_missing_timestamps_make_activity_unknown_not_stale():
    result = reach([listing_record("L1", created_days_ago=None)])
    marketplace = channel(result, ChannelClass.MARKETPLACE_STOREFRONT)
    assert marketplace.activity == ClaimState.UNKNOWN
    assert marketplace.listings_without_created_at == 1
    assert marketplace.established_listing_count == 0


def test_stale_content_is_observed_activity_not_absence():
    result = reach([video_record("V1", published_days_ago=900)])
    content = channel(result, ChannelClass.CONTENT_PLATFORM_CREATOR)
    assert content.activity == ClaimState.OBSERVED
    assert content.recent_video_count == 0
    assert content.videos_without_published_at == 0


def test_non_observed_records_contribute_no_channel():
    result = reach([listing_record(truth=TruthClass.UNKNOWN)])
    assert channel(result, ChannelClass.MARKETPLACE_STOREFRONT).distinct_endpoint_count == 0


def test_empty_evidence_yields_no_endpoints_and_no_value():
    result = reach([])
    assert all(c.distinct_endpoint_count == 0 for c in result.channels)
    assert result.value is None
    assert result.provenance.evidence_ids == ()


# --------------------------------------- 4A separation and no conversion


def test_seller_population_differs_from_purchase_evidence_seller_count():
    """4D counts sellers with any relevant listing; 4A counts sellers carrying
    review-proxy evidence. Same word, different populations — so the field
    names must differ and the counts must be allowed to differ."""
    candidate_id = uuid4()
    listings = [
        listing_record("L1", seller_id="shopA", candidate_id=candidate_id),
        listing_record("L2", seller_id="shopB", candidate_id=candidate_id, payload_hash="h2"),
    ]
    # Only shopA carries a review-proxy record, so 4A sees one seller.
    proxy = EvidenceItem(
        candidate_id=candidate_id,
        signal_type="marketplace_review_count_purchase_proxy",
        purpose=EvidencePurpose.PURCHASE,
        truth_class=TruthClass.OBSERVED,
        provider="etsy",
        marketplace="etsy",
        collection_method="official_api",
        raw_value=12,
        raw_payload={"listing_id": "L1", "seller_id": "shopA", "is_digital": True},
        raw_payload_hash="hp",
    )
    evidence = listings + [proxy]
    reach_result = extract_buyer_reach(
        candidate_id=candidate_id, evidence=evidence, now=NOW
    )
    purchase = extract_purchase_evidence(candidate_id, evidence, now=NOW)

    marketplace = channel(reach_result, ChannelClass.MARKETPLACE_STOREFRONT)
    assert marketplace.sellers_with_relevant_listing_count == 2
    assert purchase.features is not None
    assert purchase.features.distinct_seller_count == 1
    # Different names, so the two can never be silently conflated.
    assert not hasattr(marketplace, "distinct_seller_count")


def test_view_and_subscriber_counts_are_never_read_as_reach():
    """The video fixture carries 500k views and 250k subscribers."""
    result = reach([video_record()])
    rendered = str(asdict(result))
    assert "500000" not in rendered
    assert "250000" not in rendered
    assert result.audience_size == TruthClass.UNKNOWN


@pytest.mark.parametrize("pattern", FORBIDDEN_REACH_CLAIMS)
def test_serialized_output_asserts_no_buyer_or_market_claim(pattern):
    result = reach(
        [listing_record(), video_record(), keyword_record(cpc=2.0)],
        missing_reasons={},
    )
    rendered = str(asdict(result))
    for match in re.finditer(pattern, rendered, re.IGNORECASE):
        window = rendered[max(0, match.start() - 200) : match.end() + 200].lower()
        assert any(word in window for word in ("not ", "never", "unknown", "no ")), window


def test_forbidden_claim_helper_detects_and_permits_correctly():
    assert unsupported_reach_claim_in("Estimated market size is large") is not None
    assert unsupported_reach_claim_in("This shows purchase intent") is not None
    assert unsupported_reach_claim_in("Two seller storefronts were observed") is None


# ------------------------------------------------------- scope guards


def test_only_identity_and_presence_fields_are_read_from_payloads():
    """Structural proof that no magnitude can become reach.

    Walks the module's AST for every payload key it actually reads. Audience
    and purchase magnitudes must not appear at all: if the code never reads a
    view count, it cannot convert one into buyers, however the prose is
    later edited. `search_volume`, `cpc` and the bid fields ARE read, but
    only for a presence check — the values themselves are never emitted,
    which the serialization tests above assert separately.
    """
    import ast
    import pathlib

    tree = ast.parse(pathlib.Path("app/services/buyer_reach.py").read_text())
    read_keys: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
        ):
            for arg in node.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    read_keys.add(arg.value)
        if (
            isinstance(node, ast.Subscript)
            and isinstance(node.slice, ast.Constant)
            and isinstance(node.slice.value, str)
        ):
            read_keys.add(node.slice.value)

    magnitudes = {
        "view_count",
        "channel_view_count",
        "channel_subscriber_count",
        "like_count",
        "comment_count",
        "review_count",
        "rating",
    }
    assert not (read_keys & magnitudes), sorted(read_keys & magnitudes)
    # The identity and presence fields it legitimately needs.
    assert {"seller_id", "channel_id", "listing_id", "video_id"} <= read_keys


def test_buyer_reach_cannot_reach_legacy_scoring():
    from tests.test_orchestration import _transitive_app_imports

    reachable = _transitive_app_imports("app.services.buyer_reach")
    assert "app.services.scoring" not in reachable


def test_buyer_reach_cannot_import_product_specification():
    """4C prose is a design artefact and must never be read as market evidence."""
    from tests.test_orchestration import _transitive_app_imports

    reachable = _transitive_app_imports("app.services.buyer_reach")
    assert "app.services.product_specification" not in reachable


def test_buyer_reach_introduces_no_scoring_vocabulary():
    import pathlib

    source = pathlib.Path("app/services/buyer_reach.py").read_text()
    for banned in (
        r"opportunity_score",
        r"evidence_confidence_score",
        r"\bRED\b",
        r"\bYELLOW\b",
        r"\bGREEN\b",
    ):
        assert not re.search(banned, source), banned


def test_score_endpoint_remains_gone():
    from fastapi.testclient import TestClient

    from app.main import app

    assert TestClient(app).post("/score").status_code == 410
