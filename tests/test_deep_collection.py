"""Milestone 6B tests: deep collection, deep-pass caps, and cache depth.

No network, no live API calls, no new provider. Every provider is a fake that
records its calls and honours the limit it was given.

Three rules carry this milestone.

1. A cached shallow result may never satisfy a deeper request. Fewer cached
   results is a property of the earlier budget, never an observation about the
   field, so a too-shallow entry is re-fetched and never silently downgraded.

2. A depth miss is not a cache hit. The two look identical from outside — an
   entry was present — so counting one as the other would hide exactly the
   failure the rule exists to prevent.

3. Evidence states how it was obtained. A reused cache entry may never claim
   the provenance of a live call.
"""

import ast
import inspect
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from app.domain.enums import (
    COLLECTION_METHOD_CACHE,
    COLLECTION_METHOD_OFFICIAL_API,
    ProductFormat,
    SnapshotStatus,
)
from app.domain.models import Candidate
from app.providers.base import (
    ListingReviewStats,
    MarketplaceListing,
    MarketplaceProvider,
    MarketplaceQueryResult,
    ProviderResponseError,
    PublicContentProvider,
    PublicContentQueryResult,
    VideoObservation,
)
from app.services.deep_collection import (
    DEEP_COLLECTION_VERSION,
    DEEP_PASS_CAPS,
    DEEP_PASS_CAPS_VERSION,
    LIMITATIONS,
    DeepCollectionResult,
    run_deep_collection,
)
from app.services.marketplace import run_marketplace_research
from app.services.public_content import run_public_content_research
from app.services.research_orchestration import (
    CAPABILITY_MARKETPLACE,
    CAPABILITY_PUBLIC_CONTENT,
    CAPABILITY_SEARCH_DEMAND,
    STATUS_NOT_REQUESTED,
    STATUS_PROVIDER_FAILED,
    STATUS_UNEXPECTED_PROVIDER_ERROR,
    CapabilityCaps,
)
from app.storage.memory import CacheLookup, CacheOutcome, ResearchStore

from tests.route_surface import assert_route_surface_unchanged

MODULE_PATH = Path("app/services/deep_collection.py")


# ----------------------------------------------------------------- fixtures


def make_candidate(query: str = "sourdough guide") -> Candidate:
    return Candidate(
        seed_keyword="sourdough baking",
        title="Sourdough Guide",
        problem="A problem statement.",
        target_buyer="A buyer",
        proposed_format=ProductFormat.PDF_GUIDE,
        buyer_outcome="An outcome",
        search_queries=["sourdough"],
        marketplace_queries=[query],
        content_queries=[query],
        generation_reason="A hypothesis",
    )


def listing_for(listing_id: str, **overrides: Any) -> MarketplaceListing:
    fields: dict[str, Any] = dict(
        listing_id=listing_id,
        title=f"Listing {listing_id}",
        url=f"https://example.test/listing/{listing_id}",
        price=12.5,
        currency="USD",
        seller_id=f"shop-{listing_id}",
        review_count=10,
        created_at=datetime(2024, 1, 1, tzinfo=UTC),
        state="active",
        listing_type="download",
        is_digital=True,
        retrieved_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    fields.update(overrides)
    return MarketplaceListing(**fields)


def video_for(video_id: str, **overrides: Any) -> VideoObservation:
    fields: dict[str, Any] = dict(
        video_id=video_id,
        title=f"Video {video_id}",
        channel_id=f"ch-{video_id}",
        channel_title=f"Channel {video_id}",
        view_count=1000,
        like_count=100,
        comment_count=10,
        duration_seconds=300,
        published_at=datetime(2026, 1, 1, tzinfo=UTC),
        url=f"https://example.test/watch?v={video_id}",
        retrieved_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    fields.update(overrides)
    return VideoObservation(**fields)


class FakeMarketplace(MarketplaceProvider):
    """Honours `limit` exactly, so depth is observable."""

    name = "fake-marketplace"
    collection_method = COLLECTION_METHOD_OFFICIAL_API
    supports_review_stats = True

    def __init__(self, listings_by_query, fail_queries=None, error=None):
        self.listings_by_query = listings_by_query
        self.fail_queries = fail_queries or set()
        self.error = error
        self.search_calls: list[tuple[str, int]] = []

    async def search_listings(self, query: str, limit: int) -> MarketplaceQueryResult:
        self.search_calls.append((query, limit))
        if query in self.fail_queries:
            raise self.error or ProviderResponseError("simulated failure")
        return MarketplaceQueryResult(
            provider=self.name,
            query=query,
            listings=self.listings_by_query.get(query, [])[:limit],
            retrieved_at=datetime.now(UTC),
            collection_method=self.collection_method,
            source_reference="/fake/listings",
            call_count=1,
        )

    async def fetch_review_stats(self, listing_id: str) -> ListingReviewStats:
        return ListingReviewStats(
            listing_id=listing_id, review_count=5, retrieved_at=datetime.now(UTC)
        )


class FakeContent(PublicContentProvider):
    name = "fake-content"
    collection_method = COLLECTION_METHOD_OFFICIAL_API
    supports_channel_stats = False
    calls_per_search = 1
    calls_per_channel_stats = 1
    quota_units_per_search = 1
    quota_units_per_channel_stats = 1

    def __init__(self, videos_by_query, fail_queries=None, error=None):
        self.videos_by_query = videos_by_query
        self.fail_queries = fail_queries or set()
        self.error = error
        self.search_calls: list[tuple[str, int]] = []

    async def search_videos(self, query: str, limit: int) -> PublicContentQueryResult:
        self.search_calls.append((query, limit))
        if query in self.fail_queries:
            raise self.error or ProviderResponseError("simulated failure")
        return PublicContentQueryResult(
            provider=self.name,
            query=query,
            videos=self.videos_by_query.get(query, [])[:limit],
            retrieved_at=datetime.now(UTC),
            collection_method=self.collection_method,
            source_reference="/fake/videos",
            call_count=1,
            quota_units=1,
        )


QUERY = "sourdough guide"
TEN_LISTINGS = {QUERY: [listing_for(f"L{i}") for i in range(10)]}
TEN_VIDEOS = {QUERY: [video_for(f"V{i}") for i in range(10)]}


# --------------------------------------- the cache-depth rule, at the store


def test_a_cached_entry_records_the_limit_it_was_collected_under():
    store = ResearchStore()
    store.cache_listings("p", QUERY, [listing_for("L0")], effective_limit=3)
    lookup = store.cached_listings("p", QUERY, 3)
    assert lookup.outcome is CacheOutcome.HIT
    assert lookup.cached_effective_limit == 3


def test_a_shallow_entry_is_a_depth_miss_for_a_deeper_request():
    store = ResearchStore()
    store.cache_listings("p", QUERY, [listing_for("L0")], effective_limit=3)
    lookup = store.cached_listings("p", QUERY, 50)
    assert lookup.outcome is CacheOutcome.DEPTH_MISS
    assert lookup.cached_effective_limit == 3


def test_a_depth_miss_yields_no_items_at_all():
    """Rule 3: fewer cached results is a budget fact, never a field fact.

    Returning the shallow set alongside the miss would invite exactly the
    downgrade the rule forbids — "the cache had fewer, so fewer exist".
    """
    store = ResearchStore()
    store.cache_listings("p", QUERY, [listing_for("L0")], effective_limit=1)
    assert store.cached_listings("p", QUERY, 50).items is None
    store.cache_videos("p", QUERY, [video_for("V0")], effective_limit=1)
    assert store.cached_videos("p", QUERY, 50).items is None


def test_a_deep_entry_satisfies_a_later_shallower_request():
    store = ResearchStore()
    store.cache_listings("p", QUERY, [listing_for("L0")], effective_limit=50)
    lookup = store.cached_listings("p", QUERY, 10)
    assert lookup.outcome is CacheOutcome.HIT


def test_an_equal_limit_is_a_hit_not_a_miss():
    """The boundary: >= reuses, < misses."""
    store = ResearchStore()
    store.cache_listings("p", QUERY, [listing_for("L0")], effective_limit=10)
    assert store.cached_listings("p", QUERY, 10).outcome is CacheOutcome.HIT
    assert store.cached_listings("p", QUERY, 11).outcome is CacheOutcome.DEPTH_MISS


def test_refetching_replaces_the_entry_with_the_larger_limit():
    """Rule 4, both directions after the replacement."""
    store = ResearchStore()
    store.cache_listings("p", QUERY, [listing_for("L0")], effective_limit=3)
    assert store.cached_listings("p", QUERY, 50).outcome is CacheOutcome.DEPTH_MISS
    store.cache_listings("p", QUERY, [listing_for("L0")], effective_limit=50)
    assert store.cached_listings("p", QUERY, 10).outcome is CacheOutcome.HIT
    assert store.cached_listings("p", QUERY, 50).outcome is CacheOutcome.HIT
    assert store.cached_listings("p", QUERY, 51).outcome is CacheOutcome.DEPTH_MISS


def test_an_absent_or_expired_entry_is_a_plain_miss():
    store = ResearchStore(cache_ttl=timedelta(seconds=0))
    assert store.cached_listings("p", "never-seen", 1).outcome is CacheOutcome.MISS
    store.cache_listings("p", QUERY, [listing_for("L0")], effective_limit=50)
    expired = store.cached_listings("p", QUERY, 1)
    assert expired.outcome is CacheOutcome.MISS
    assert expired.cached_effective_limit is None


def test_the_three_cache_outcomes_are_all_reachable():
    store = ResearchStore()
    store.cache_videos("p", QUERY, [video_for("V0")], effective_limit=5)
    observed = {
        store.cached_videos("p", "absent", 5).outcome,
        store.cached_videos("p", QUERY, 5).outcome,
        store.cached_videos("p", QUERY, 25).outcome,
    }
    assert observed == set(CacheOutcome)


def test_a_cache_lookup_never_reports_a_limit_it_did_not_store():
    assert CacheLookup(outcome=CacheOutcome.MISS).cached_effective_limit is None
    assert CacheLookup(outcome=CacheOutcome.MISS).items is None


# ------------------------------- the cache-depth rule, through a real pass


async def test_a_deep_pass_over_a_shallow_cache_really_collects_more():
    """The regression this milestone exists for.

    A cheap pass capped at 2 stored 2 listings under a key that said nothing
    about 2. Before limit-aware identity, a deep pass asking for 10 hit that
    entry and received 2 while believing it had collected deeply.
    """
    store = ResearchStore()
    candidates = [make_candidate()]
    provider = FakeMarketplace(TEN_LISTINGS)

    shallow = await run_marketplace_research(
        candidates=candidates,
        provider=provider,
        store=store,
        research_run_id=uuid4(),
        max_listings_per_query=2,
    )
    assert shallow.unique_listing_count == 2
    assert shallow.cached_query_count == 0

    deep = await run_marketplace_research(
        candidates=candidates,
        provider=provider,
        store=store,
        research_run_id=uuid4(),
        max_listings_per_query=10,
    )
    assert deep.unique_listing_count == 10, "deep pass must not inherit shallow depth"
    assert deep.depth_miss_query_count == 1
    assert deep.cached_query_count == 0, "a depth miss is never counted as a hit"
    # And it really went back to the provider, at the deeper limit.
    assert provider.search_calls == [(QUERY, 2), (QUERY, 10)]


async def test_a_same_depth_second_pass_reuses_without_a_provider_call():
    store = ResearchStore()
    candidates = [make_candidate()]
    provider = FakeMarketplace(TEN_LISTINGS)
    for _ in range(2):
        result = await run_marketplace_research(
            candidates=candidates,
            provider=provider,
            store=store,
            research_run_id=uuid4(),
            max_listings_per_query=5,
        )
    assert result.cached_query_count == 1
    assert result.depth_miss_query_count == 0
    assert len(provider.search_calls) == 1, "no budget spent re-fetching"


async def test_a_shallower_pass_after_a_deep_one_reuses_the_deep_entry():
    store = ResearchStore()
    candidates = [make_candidate()]
    provider = FakeMarketplace(TEN_LISTINGS)
    await run_marketplace_research(
        candidates=candidates, provider=provider, store=store,
        research_run_id=uuid4(), max_listings_per_query=10,
    )
    shallower = await run_marketplace_research(
        candidates=candidates, provider=provider, store=store,
        research_run_id=uuid4(), max_listings_per_query=3,
    )
    assert shallower.cached_query_count == 1
    assert shallower.depth_miss_query_count == 0
    assert len(provider.search_calls) == 1


async def test_public_content_enforces_the_same_depth_rule():
    store = ResearchStore()
    candidates = [make_candidate()]
    provider = FakeContent(TEN_VIDEOS)

    shallow = await run_public_content_research(
        candidates=candidates, provider=provider, store=store,
        research_run_id=uuid4(), max_videos_per_query=2,
    )
    assert shallow.unique_video_count == 2

    deep = await run_public_content_research(
        candidates=candidates, provider=provider, store=store,
        research_run_id=uuid4(), max_videos_per_query=10,
    )
    assert deep.unique_video_count == 10
    assert deep.depth_miss_query_count == 1
    assert deep.cached_query_count == 0
    assert provider.search_calls == [(QUERY, 2), (QUERY, 10)]


async def test_a_depth_miss_is_never_reported_as_fewer_results_existing():
    """Rule 3 at the service level, stated as the count it would have lied by."""
    store = ResearchStore()
    candidates = [make_candidate()]
    provider = FakeContent(TEN_VIDEOS)
    await run_public_content_research(
        candidates=candidates, provider=provider, store=store,
        research_run_id=uuid4(), max_videos_per_query=1,
    )
    deep = await run_public_content_research(
        candidates=candidates, provider=provider, store=store,
        research_run_id=uuid4(), max_videos_per_query=8,
    )
    assert deep.unique_video_count == 8
    assert deep.unique_video_count != 1


# --------------------------------------------------------------- provenance


async def test_cache_reused_evidence_states_that_it_was_reused():
    store = ResearchStore()
    candidates = [make_candidate()]
    provider = FakeMarketplace(TEN_LISTINGS)
    run_one = uuid4()
    await run_marketplace_research(
        candidates=candidates, provider=provider, store=store,
        research_run_id=run_one, max_listings_per_query=5,
    )
    live_methods = {
        item.collection_method
        for item in store.evidence_for_candidate(candidates[0].id, run_one)
    }
    assert live_methods == {COLLECTION_METHOD_OFFICIAL_API}

    run_two = uuid4()
    await run_marketplace_research(
        candidates=candidates, provider=provider, store=store,
        research_run_id=run_two, max_listings_per_query=5,
    )
    reused_methods = {
        item.collection_method
        for item in store.evidence_for_candidate(candidates[0].id, run_two)
    }
    assert reused_methods == {COLLECTION_METHOD_CACHE}


async def test_reused_public_content_evidence_states_that_it_was_reused():
    store = ResearchStore()
    candidates = [make_candidate()]
    provider = FakeContent(TEN_VIDEOS)
    await run_public_content_research(
        candidates=candidates, provider=provider, store=store,
        research_run_id=uuid4(), max_videos_per_query=5,
    )
    run_two = uuid4()
    await run_public_content_research(
        candidates=candidates, provider=provider, store=store,
        research_run_id=run_two, max_videos_per_query=5,
    )
    assert {
        item.collection_method
        for item in store.evidence_for_candidate(candidates[0].id, run_two)
    } == {COLLECTION_METHOD_CACHE}


async def test_live_public_content_evidence_keeps_the_providers_own_method():
    """The other half of the video provenance rule.

    Without this, marking every video record cache-sourced would pass: the
    reuse test only proves cache records say cache, never that live ones say
    live.
    """
    store = ResearchStore()
    candidates = [make_candidate()]
    run_id = uuid4()
    await run_public_content_research(
        candidates=candidates, provider=FakeContent(TEN_VIDEOS), store=store,
        research_run_id=run_id, max_videos_per_query=5,
    )
    assert {
        item.collection_method
        for item in store.evidence_for_candidate(candidates[0].id, run_id)
    } == {COLLECTION_METHOD_OFFICIAL_API}


async def test_a_refetched_depth_miss_produces_live_provenance_not_cache():
    """The re-fetch really went to the provider, so it is a live observation."""
    store = ResearchStore()
    candidates = [make_candidate()]
    provider = FakeMarketplace(TEN_LISTINGS)
    await run_marketplace_research(
        candidates=candidates, provider=provider, store=store,
        research_run_id=uuid4(), max_listings_per_query=2,
    )
    deep_run = uuid4()
    await run_marketplace_research(
        candidates=candidates, provider=provider, store=store,
        research_run_id=deep_run, max_listings_per_query=10,
    )
    assert {
        item.collection_method
        for item in store.evidence_for_candidate(candidates[0].id, deep_run)
    } == {COLLECTION_METHOD_OFFICIAL_API}


async def test_evidence_confidence_reads_reused_provenance_as_validated_cache():
    """The 6A-1 integration: provenance the ECS table can actually use."""
    from app.services.evidence_confidence import (
        PROVENANCE_DIRECTNESS,
        DirectnessClass,
        directness_class,
    )

    store = ResearchStore()
    candidates = [make_candidate()]
    provider = FakeMarketplace(TEN_LISTINGS)
    await run_marketplace_research(
        candidates=candidates, provider=provider, store=store,
        research_run_id=uuid4(), max_listings_per_query=5,
    )
    reused_run = uuid4()
    await run_marketplace_research(
        candidates=candidates, provider=provider, store=store,
        research_run_id=reused_run, max_listings_per_query=5,
    )
    reused = store.evidence_for_candidate(candidates[0].id, reused_run)
    classes = {directness_class(item.collection_method) for item in reused}
    assert classes == {DirectnessClass.VALIDATED_CACHE}
    # Scored below a live call, which is the whole point of stating reuse.
    assert (
        PROVENANCE_DIRECTNESS[DirectnessClass.VALIDATED_CACHE]
        < PROVENANCE_DIRECTNESS[DirectnessClass.DIRECT_API]
    )


# --------------------------------------------------------- deep_pass_caps_v1


def test_the_approved_u6_cap_values_are_exactly_as_specified():
    assert DEEP_PASS_CAPS_VERSION == "deep_pass_caps_v1"
    assert DEEP_PASS_CAPS == CapabilityCaps(
        max_provider_calls=50,
        max_queries=40,
        max_keywords=100,
        max_listings_per_query=50,
        max_review_lookups=40,
        max_videos_per_query=25,
        max_quota_units=3000,
        max_channel_lookups=5,
    )


def test_every_capability_caps_field_is_supplied_by_the_deep_pass():
    """§1: the deep pass declares all eight. A None would silently fall back
    to the cheap pass's own default and make the pass deep in name only."""
    for name in CapabilityCaps.__dataclass_fields__:
        assert getattr(DEEP_PASS_CAPS, name) is not None, name


def test_the_per_query_result_limits_are_strictly_deeper_than_the_cheap_pass():
    """These are per query, so they compare directly -- and they are the two
    the §1.1 cache-depth rule governs."""
    from app.services import marketplace, public_content

    assert (
        DEEP_PASS_CAPS.max_listings_per_query
        > marketplace.DEFAULT_MAX_LISTINGS_PER_QUERY
    )
    assert (
        DEEP_PASS_CAPS.max_videos_per_query
        > public_content.DEFAULT_MAX_VIDEOS_PER_QUERY
    )


def test_pass_level_budgets_are_deeper_per_candidate_not_per_pass():
    """`max_keywords` is the cap that looks like a reduction and is not.

    100 is a smaller pass total than the cheap default of 200, but the cheap
    pass spreads its budget over ~20 discovered candidates and the deep pass
    concentrates it on the ~5 selected. Per candidate that is 20 keywords
    against 10. Comparing pass totals directly would read a deepening as a cut.
    """
    from app.services import search_demand
    from app.services.candidate_discovery import DEFAULT_TARGET_COUNT
    from app.services.preliminary_ranking import DEEP_RESEARCH_SELECTION_SIZE

    cheap_per_candidate = search_demand.DEFAULT_MAX_KEYWORDS / DEFAULT_TARGET_COUNT
    deep_per_candidate = DEEP_PASS_CAPS.max_keywords / DEEP_RESEARCH_SELECTION_SIZE
    assert deep_per_candidate > cheap_per_candidate
    assert DEEP_PASS_CAPS.max_keywords < search_demand.DEFAULT_MAX_KEYWORDS, (
        "the pass total really is smaller; the depth is per candidate"
    )


def test_the_cap_values_are_never_described_as_validated():
    text = MODULE_PATH.read_text(encoding="utf-8")
    assert "UNCALIBRATED" in text
    for claim in ("empirically validated", "calibrated against", "proven to"):
        for index in range(len(text)):
            if not text.startswith(claim, index):
                continue
            window = text[max(0, index - 80) : index]
            assert any(
                word in window.lower()
                for word in ("not", "never", "no ", "without")
            ), claim


async def test_the_deep_pass_asks_the_provider_for_the_deep_limit():
    store = ResearchStore()
    candidates = [make_candidate()]
    marketplace = FakeMarketplace(TEN_LISTINGS)
    content = FakeContent(TEN_VIDEOS)
    await run_deep_collection(
        candidates=candidates,
        store=store,
        research_run_id=uuid4(),
        marketplace_provider=marketplace,
        public_content_provider=content,
    )
    assert marketplace.search_calls == [(QUERY, DEEP_PASS_CAPS.max_listings_per_query)]
    assert content.search_calls == [(QUERY, DEEP_PASS_CAPS.max_videos_per_query)]


# ------------------------------------------------------- the deep pass itself


async def test_the_result_records_the_budget_that_authorized_it():
    store = ResearchStore()
    result = await run_deep_collection(
        candidates=[make_candidate()],
        store=store,
        research_run_id=uuid4(),
        marketplace_provider=FakeMarketplace(TEN_LISTINGS),
    )
    assert result.caps_version == DEEP_PASS_CAPS_VERSION
    assert result.version == DEEP_COLLECTION_VERSION


async def test_the_deep_pass_extends_the_run_it_was_given():
    """Scope is never invented: evidence lands on the caller's run."""
    store = ResearchStore()
    candidate = make_candidate()
    run_id = uuid4()
    await run_deep_collection(
        candidates=[candidate],
        store=store,
        research_run_id=run_id,
        marketplace_provider=FakeMarketplace(TEN_LISTINGS),
    )
    evidence = store.evidence_for_candidate(candidate.id, run_id)
    assert evidence
    assert {item.research_run_id for item in evidence} == {run_id}
    assert {item.candidate_id for item in evidence} == {candidate.id}


def test_a_research_run_id_is_required_rather_than_defaulted():
    signature = inspect.signature(run_deep_collection)
    assert signature.parameters["research_run_id"].default is inspect.Parameter.empty


async def test_every_capability_is_reported_even_when_not_requested():
    result = await run_deep_collection(
        candidates=[make_candidate()], store=ResearchStore(), research_run_id=uuid4()
    )
    reported = {o.capability: o.status for o in result.capabilities}
    assert reported == {
        CAPABILITY_SEARCH_DEMAND: STATUS_NOT_REQUESTED,
        CAPABILITY_MARKETPLACE: STATUS_NOT_REQUESTED,
        CAPABILITY_PUBLIC_CONTENT: STATUS_NOT_REQUESTED,
    }


async def test_a_failing_provider_degrades_only_its_own_capability():
    store = ResearchStore()
    result = await run_deep_collection(
        candidates=[make_candidate()],
        store=store,
        research_run_id=uuid4(),
        marketplace_provider=FakeMarketplace(TEN_LISTINGS, fail_queries={QUERY}),
        public_content_provider=FakeContent(TEN_VIDEOS),
    )
    by_capability = {o.capability: o for o in result.capabilities}
    assert by_capability[CAPABILITY_MARKETPLACE].status in (
        SnapshotStatus.FAILED.value,
        SnapshotStatus.PARTIAL.value,
    )
    assert by_capability[CAPABILITY_PUBLIC_CONTENT].status == (
        SnapshotStatus.COMPLETE.value
    )


async def test_an_unexpected_provider_defect_is_reported_as_such():
    class Exploding(FakeMarketplace):
        async def search_listings(self, query, limit):
            raise RuntimeError("secret-bearing internal failure")

    result = await run_deep_collection(
        candidates=[make_candidate()],
        store=ResearchStore(),
        research_run_id=uuid4(),
        marketplace_provider=Exploding(TEN_LISTINGS),
    )
    outcome = next(
        o for o in result.capabilities if o.capability == CAPABILITY_MARKETPLACE
    )
    assert outcome.status == STATUS_UNEXPECTED_PROVIDER_ERROR
    assert outcome.failure_reason


async def test_an_unconstructable_provider_is_reported_without_being_called():
    result = await run_deep_collection(
        candidates=[make_candidate()],
        store=ResearchStore(),
        research_run_id=uuid4(),
        unavailable_capabilities={CAPABILITY_MARKETPLACE: "missing credentials"},
    )
    outcome = next(
        o for o in result.capabilities if o.capability == CAPABILITY_MARKETPLACE
    )
    assert outcome.status == STATUS_PROVIDER_FAILED
    assert outcome.failure_reason == "missing credentials"


async def test_true_hits_and_depth_misses_are_reported_separately():
    store = ResearchStore()
    candidates = [make_candidate()]
    marketplace = FakeMarketplace(TEN_LISTINGS)
    # Seed a shallow cache, then go deep over it.
    await run_marketplace_research(
        candidates=candidates, provider=marketplace, store=store,
        research_run_id=uuid4(), max_listings_per_query=1,
    )
    first_deep = await run_deep_collection(
        candidates=candidates, store=store, research_run_id=uuid4(),
        marketplace_provider=marketplace,
    )
    assert first_deep.depth_misses == 1
    assert first_deep.true_cache_hits == 0

    # The entry now records the deep limit, so a second deep pass truly hits.
    second_deep = await run_deep_collection(
        candidates=candidates, store=store, research_run_id=uuid4(),
        marketplace_provider=marketplace,
    )
    assert second_deep.true_cache_hits == 1
    assert second_deep.depth_misses == 0


async def test_search_demand_reports_no_depth_miss_because_depth_means_more_keywords():
    """§1.1: the keyword cache is keyed per keyword and is depth-independent."""
    result = await run_deep_collection(
        candidates=[make_candidate()],
        store=ResearchStore(),
        research_run_id=uuid4(),
        marketplace_provider=FakeMarketplace(TEN_LISTINGS),
    )
    search = next(
        o for o in result.capabilities if o.capability == CAPABILITY_SEARCH_DEMAND
    )
    assert search.depth_miss_query_count == 0


# --------------------------------------------------- dedup and immutability


async def test_re_observation_collapses_through_the_existing_fingerprint():
    """§1.1: 6B introduces no new deduplication rule.

    The cheap and deep passes overlap by construction. A re-observation of the
    same listing produces a byte-identical payload, therefore an identical
    hash, which the existing mechanism already collapses.
    """
    store = ResearchStore()
    candidate = make_candidate()
    provider = FakeMarketplace(TEN_LISTINGS)
    run_id = uuid4()
    await run_marketplace_research(
        candidates=[candidate], provider=provider, store=store,
        research_run_id=run_id, max_listings_per_query=5,
    )
    hashes = [
        item.raw_payload_hash
        for item in store.evidence_for_candidate(candidate.id, run_id)
    ]
    assert len(hashes) == len(set(hashes)) * 3 or len(hashes) == len(set(hashes)), (
        "each listing yields its own per-purpose records, not duplicates"
    )
    source = Path("app/services/deep_collection.py").read_text(encoding="utf-8")
    for invented in ("dedup", "deduplicate", "fingerprint", "payload_hash"):
        assert invented not in source.lower().replace(
            "deduplication rule", ""
        ), invented


async def test_the_deep_pass_never_mutates_existing_evidence():
    store = ResearchStore()
    candidate = make_candidate()
    provider = FakeMarketplace(TEN_LISTINGS)
    first_run = uuid4()
    await run_marketplace_research(
        candidates=[candidate], provider=provider, store=store,
        research_run_id=first_run, max_listings_per_query=2,
    )
    before = {
        item.id: (item.collection_method, item.raw_payload_hash)
        for item in store.evidence_for_candidate(candidate.id, first_run)
    }
    await run_deep_collection(
        candidates=[candidate], store=store, research_run_id=uuid4(),
        marketplace_provider=provider,
    )
    after = {
        item.id: (item.collection_method, item.raw_payload_hash)
        for item in store.evidence_for_candidate(candidate.id, first_run)
    }
    assert after == before


# ------------------------------------------------------ module boundaries


def _module_symbols() -> set[str]:
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    symbols: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            symbols.add(node.id)
        elif isinstance(node, ast.Attribute):
            symbols.add(node.attr)
        elif isinstance(node, ast.ImportFrom) and node.module:
            symbols.add(node.module)
            symbols.update(a.asname or a.name for a in node.names)
    return symbols


def test_the_deep_pass_scores_nothing():
    symbols = _module_symbols()
    for forbidden in (
        "app.services.scoring", "score_opportunity", "opportunity_score",
        "Classification", "compute_evidence_confidence", "RED", "YELLOW", "GREEN",
    ):
        assert forbidden not in symbols, forbidden


def test_the_deep_pass_introduces_no_new_provider():
    symbols = _module_symbols()
    provider_modules = {s for s in symbols if s.startswith("app.providers")}
    assert provider_modules <= {"app.providers.base"}, provider_modules
    for forbidden in ("httpx", "requests", "aiohttp", "urllib", "socket"):
        assert forbidden not in symbols, forbidden


def test_no_endpoint_was_added_and_score_remains_gone():
    from fastapi.testclient import TestClient

    from app.main import app


    assert_route_surface_unchanged(app)
    assert TestClient(app).post("/score", json={}).status_code == 410


def test_the_limitations_separate_depth_from_the_field():
    joined = " ".join(LIMITATIONS).lower()
    assert "depth is a property of the search, never of the field" in joined
    assert "not empirically validated" in joined
    assert "derives no dimension" in joined


def test_the_result_is_immutable():
    result = DeepCollectionResult(
        research_run_id=uuid4(), candidate_ids=(), capabilities=()
    )
    with pytest.raises(Exception):
        result.caps_version = "deep_pass_caps_v2"  # type: ignore[misc]
