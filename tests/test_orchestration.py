"""Milestone 3C tests: orchestration, evidence bridge, preliminary ranking.

Every provider here is an in-memory fake. No network, no live API calls, no
Etsy or YouTube smoke tests.
"""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.domain.enums import ProductFormat, SnapshotStatus, TruthClass
from app.domain.models import Candidate
from app.providers.base import (
    ChannelStatsResult,
    KeywordDemandMetrics,
    ListingReviewStats,
    MarketplaceListing,
    MarketplaceProvider,
    MarketplaceQueryResult,
    MonthlySearchVolume,
    ProviderResponseError,
    PublicContentProvider,
    PublicContentQueryResult,
    SearchDemandBatchResult,
    SearchDemandProvider,
    VideoObservation,
)
from app.services.preliminary_dimensions import (
    DIM_AUDIENCE_INTEREST,
    DIM_COMPETITION_FIELD,
    DIM_PRICE_EVIDENCE,
    DIM_PURCHASE_PROXY,
    DIM_SEARCH_DEMAND,
    NO_APPROVED_FORMULA,
    PRELIMINARY_DIMENSION_NAMES,
    DimensionState,
    build_preliminary_profile,
    combine_truth_class,
)
from app.services.preliminary_ranking import (
    CRITERIA_ORDER,
    DEEP_RESEARCH_SELECTION_SIZE,
    PRELIMINARY_RANKING_VERSION,
    explain_pairwise,
    rank_candidates,
)
from app.services.research_orchestration import (
    CAPABILITY_MARKETPLACE,
    CAPABILITY_PUBLIC_CONTENT,
    CAPABILITY_SEARCH_DEMAND,
    MISSING_CAPABILITY_FAILED,
    MISSING_CAPABILITY_NOT_REQUESTED,
    STATUS_NOT_REQUESTED,
    STATUS_PROVIDER_FAILED,
    CapabilityCaps,
    run_preliminary_research,
)
from app.storage.memory import ResearchStore


# --------------------------------------------------------------- fixtures


def make_candidate(
    title: str,
    search_queries: list[str] | None = None,
    marketplace_queries: list[str] | None = None,
    content_queries: list[str] | None = None,
    candidate_id: UUID | None = None,
) -> Candidate:
    fields: dict[str, Any] = dict(
        seed_keyword="sourdough baking",
        title=title,
        problem="A problem statement.",
        target_buyer="A buyer",
        proposed_format=ProductFormat.PDF_GUIDE,
        buyer_outcome="An outcome",
        search_queries=search_queries or [f"{title} keyword"],
        marketplace_queries=marketplace_queries or [f"{title} listing"],
        content_queries=content_queries or [f"{title} video"],
        generation_reason="A hypothesis",
    )
    if candidate_id is not None:
        fields["id"] = candidate_id
    return Candidate(**fields)


def metrics_for(keyword: str, volume: int | None = 1000) -> KeywordDemandMetrics:
    return KeywordDemandMetrics(
        keyword=keyword,
        search_volume=volume,
        monthly_history=(MonthlySearchVolume(2025, 7, volume),) if volume is not None else (),
        competition="LOW",
        competition_index=20,
        cpc=0.5,
        retrieved_at=datetime(2025, 8, 1, tzinfo=UTC),
    )


def listing_for(
    listing_id: str,
    price: float | None = 12.5,
    review_count: int | None = 10,
    seller_id: str | None = "shop-1",
) -> MarketplaceListing:
    return MarketplaceListing(
        listing_id=listing_id,
        title=f"Listing {listing_id}",
        url=f"https://www.etsy.com/listing/{listing_id}",
        price=price,
        currency="USD" if price is not None else None,
        seller_id=seller_id,
        review_count=review_count,
        created_at=datetime(2024, 1, 1, tzinfo=UTC),
        state="active",
        listing_type="download",
        is_digital=True,
        retrieved_at=datetime(2025, 8, 1, tzinfo=UTC),
    )


def video_for(
    video_id: str,
    view_count: int | None = 5000,
    like_count: int | None = 100,
    channel_id: str | None = "chan-1",
) -> VideoObservation:
    return VideoObservation(
        video_id=video_id,
        title=f"Video {video_id}",
        published_at=datetime(2025, 6, 1, tzinfo=UTC),
        channel_id=channel_id,
        view_count=view_count,
        like_count=like_count,
        comment_count=10,
        url=f"https://www.youtube.com/watch?v={video_id}",
        retrieved_at=datetime(2025, 8, 1, tzinfo=UTC),
    )


class FakeSearchProvider(SearchDemandProvider):
    name = "fake-search"
    max_keywords_per_request = 50

    def __init__(
        self,
        volumes: dict[str, int | None] | None = None,
        fail: bool = False,
    ) -> None:
        self.volumes = volumes or {}
        self.fail = fail
        self.calls = 0
        self.batches_seen: list[list[str]] = []

    async def fetch_keyword_metrics(
        self, keywords: list[str], location: str, language: str
    ) -> SearchDemandBatchResult:
        self.calls += 1
        self.batches_seen.append(list(keywords))
        if self.fail:
            raise ProviderResponseError("simulated search-demand failure")
        return SearchDemandBatchResult(
            provider=self.name,
            metrics=[
                metrics_for(k, self.volumes[k]) for k in keywords if k in self.volumes
            ],
            retrieved_at=datetime(2025, 8, 1, tzinfo=UTC),
            collection_method="official_api",
            source_reference="/fake/keywords",
            call_count=1,
            cost=0.05,
        )


class FakeMarketplaceProvider(MarketplaceProvider):
    name = "fake-marketplace"
    collection_method = "official_api"
    supports_review_stats = True

    def __init__(
        self,
        listings_by_query: dict[str, list[MarketplaceListing]] | None = None,
        fail_queries: set[str] | None = None,
    ) -> None:
        self.listings_by_query = listings_by_query or {}
        self.fail_queries = fail_queries or set()
        self.search_calls: list[str] = []

    async def search_listings(self, query: str, limit: int) -> MarketplaceQueryResult:
        self.search_calls.append(query)
        if query in self.fail_queries:
            raise ProviderResponseError("simulated marketplace failure")
        return MarketplaceQueryResult(
            provider=self.name,
            query=query,
            listings=self.listings_by_query.get(query, [])[:limit],
            retrieved_at=datetime(2025, 8, 1, tzinfo=UTC),
            collection_method=self.collection_method,
            source_reference="/fake/listings",
            call_count=1,
        )

    async def fetch_review_stats(self, listing_id: str) -> ListingReviewStats:
        return ListingReviewStats(
            listing_id=listing_id,
            review_count=None,
            retrieved_at=datetime(2025, 8, 1, tzinfo=UTC),
        )


class FakeContentProvider(PublicContentProvider):
    name = "fake-content"
    collection_method = "official_api"
    supports_channel_stats = False
    calls_per_search = 2
    quota_units_per_search = 101

    def __init__(
        self,
        videos_by_query: dict[str, list[VideoObservation]] | None = None,
        fail_queries: set[str] | None = None,
    ) -> None:
        self.videos_by_query = videos_by_query or {}
        self.fail_queries = fail_queries or set()
        self.search_calls: list[str] = []

    async def search_videos(self, query: str, limit: int) -> PublicContentQueryResult:
        self.search_calls.append(query)
        if query in self.fail_queries:
            raise ProviderResponseError("simulated content failure")
        return PublicContentQueryResult(
            provider=self.name,
            query=query,
            videos=self.videos_by_query.get(query, [])[:limit],
            retrieved_at=datetime(2025, 8, 1, tzinfo=UTC),
            collection_method=self.collection_method,
            source_reference="/fake/search",
            call_count=self.calls_per_search,
            quota_units=self.quota_units_per_search,
        )

    async def fetch_channel_stats(self, channel_ids: list[str]) -> ChannelStatsResult:
        return ChannelStatsResult(stats={}, call_count=1, quota_units=1)


def full_providers(candidates: list[Candidate]):
    """Providers that return evidence for every query of every candidate."""
    volumes: dict[str, int | None] = {}
    listings: dict[str, list[MarketplaceListing]] = {}
    videos: dict[str, list[VideoObservation]] = {}
    for index, candidate in enumerate(candidates):
        volumes[candidate.search_queries[0].lower()] = 1000 * (index + 1)
        listings[candidate.marketplace_queries[0].lower()] = [
            listing_for(f"L{index}-{n}", price=10.0 + n, review_count=n)
            for n in range(index + 1)
        ]
        videos[candidate.content_queries[0].lower()] = [
            video_for(f"V{index}-{n}", view_count=1000 * (index + 1))
            for n in range(2)
        ]
    return (
        FakeSearchProvider(volumes),
        FakeMarketplaceProvider(listings),
        FakeContentProvider(videos),
    )


async def orchestrate(candidates, search=None, marketplace=None, content=None, **kwargs):
    store = ResearchStore()
    return await run_preliminary_research(
        candidates=candidates,
        store=store,
        search_demand_provider=search,
        marketplace_provider=marketplace,
        public_content_provider=content,
        **kwargs,
    )


# ------------------------------------------------------- evidence bridge


def test_missing_capability_leaves_dimensions_missing_not_zero():
    candidate = make_candidate("Alpha")
    profile = build_preliminary_profile(
        candidate_id=candidate.id,
        candidate_title=candidate.title,
        evidence=[],
        search_demand=None,
        marketplace=None,
        public_content=None,
    )
    for name in PRELIMINARY_DIMENSION_NAMES:
        dimension = profile.dimension(name)
        assert dimension.state == DimensionState.MISSING
        assert dimension.value is None, f"{name} must stay missing, never 0"
        assert dimension.missing_reason is not None
        assert dimension.evidence_truth_basis is None
    assert profile.present_dimension_count == 0


@pytest.mark.asyncio
async def test_unknown_measurement_stays_unknown_and_is_not_zero():
    # The provider returns the keyword but with no search volume at all.
    candidate = make_candidate("Alpha", search_queries=["alpha kw"])
    search = FakeSearchProvider({"alpha kw": None})
    result = await orchestrate([candidate], search=search)

    dimension = result.profiles[0].dimension(DIM_SEARCH_DEMAND)
    assert dimension.state == DimensionState.UNKNOWN
    assert dimension.value is None
    assert dimension.unknown_input_count == 1
    assert dimension.observed_input_count == 0
    assert dimension.evidence_truth_basis == TruthClass.UNKNOWN


@pytest.mark.asyncio
async def test_observed_zero_volume_is_a_real_value_distinct_from_unknown():
    candidate = make_candidate("Alpha", search_queries=["alpha kw"])
    search = FakeSearchProvider({"alpha kw": 0})
    result = await orchestrate([candidate], search=search)

    dimension = result.profiles[0].dimension(DIM_SEARCH_DEMAND)
    # 0 here is an observed measurement the formula explicitly defines as 0,
    # not a stand-in for absent data.
    assert dimension.state == DimensionState.SCORED
    assert dimension.value == 0.0
    assert dimension.evidence_truth_basis == TruthClass.OBSERVED


@pytest.mark.asyncio
async def test_marketplace_dimensions_are_unscored_no_invented_formula():
    candidate = make_candidate("Alpha", marketplace_queries=["alpha listing"])
    marketplace = FakeMarketplaceProvider(
        {"alpha listing": [listing_for("L1"), listing_for("L2")]}
    )
    result = await orchestrate([candidate], marketplace=marketplace)

    for name in (DIM_PRICE_EVIDENCE, DIM_PURCHASE_PROXY, DIM_COMPETITION_FIELD):
        dimension = result.profiles[0].dimension(name)
        assert dimension.state == DimensionState.EVIDENCE_PRESENT_UNSCORED
        assert dimension.value is None, f"{name} must not receive an invented score"
        assert dimension.formula_version == NO_APPROVED_FORMULA


@pytest.mark.asyncio
async def test_review_counts_are_purchase_proxy_never_sales():
    candidate = make_candidate("Alpha", marketplace_queries=["alpha listing"])
    marketplace = FakeMarketplaceProvider(
        {"alpha listing": [listing_for("L1", review_count=40)]}
    )
    result = await orchestrate([candidate], marketplace=marketplace)

    dimension = result.profiles[0].dimension(DIM_PURCHASE_PROXY)
    text = " ".join(dimension.limitations).lower()
    assert "proxy" in text
    assert "never units sold" in text
    # Nothing in the dimension expresses sales, units, or revenue as a value.
    assert dimension.value is None


@pytest.mark.asyncio
async def test_youtube_views_feed_audience_interest_not_purchase():
    candidate = make_candidate("Alpha", content_queries=["alpha video"])
    content = FakeContentProvider({"alpha video": [video_for("V1", view_count=10_000)]})
    result = await orchestrate([candidate], content=content)

    profile = result.profiles[0]
    assert profile.dimension(DIM_AUDIENCE_INTEREST).state == DimensionState.SCORED
    # Views never populate any purchase-related dimension.
    assert profile.dimension(DIM_PURCHASE_PROXY).state == DimensionState.MISSING
    text = " ".join(profile.dimension(DIM_AUDIENCE_INTEREST).limitations).lower()
    assert "never purchase evidence" in text


@pytest.mark.asyncio
async def test_provenance_is_preserved_on_every_dimension():
    candidate = make_candidate("Alpha")
    search, marketplace, content = full_providers([candidate])
    result = await orchestrate(
        [candidate], search=search, marketplace=marketplace, content=content
    )
    profile = result.profiles[0]

    demand = profile.dimension(DIM_SEARCH_DEMAND)
    assert demand.contributing_evidence_ids, "evidence ids must be retained"
    assert demand.providers == ("fake-search",)
    assert demand.source_references == ("/fake/keywords",)
    assert demand.formula_version == "search_demand_dimension_v1"
    assert demand.bridge_version == "preliminary_dimensions_v1"

    audience = profile.dimension(DIM_AUDIENCE_INTEREST)
    assert audience.providers == ("fake-content",)
    assert audience.source_references == ("/fake/search",)


def test_truth_class_combination_is_most_conservative():
    assert combine_truth_class([TruthClass.OBSERVED, TruthClass.INFERRED]) == TruthClass.INFERRED
    assert combine_truth_class([TruthClass.OBSERVED, TruthClass.UNKNOWN]) == TruthClass.UNKNOWN
    assert combine_truth_class([TruthClass.OBSERVED]) == TruthClass.OBSERVED
    assert combine_truth_class([]) is None


@pytest.mark.asyncio
async def test_derived_dimension_value_is_inferred_never_observed():
    candidate = make_candidate("Alpha")
    search, _, content = full_providers([candidate])
    result = await orchestrate([candidate], search=search, content=content)
    profile = result.profiles[0]

    for name in (DIM_SEARCH_DEMAND, DIM_AUDIENCE_INTEREST):
        dimension = profile.dimension(name)
        assert dimension.state == DimensionState.SCORED
        # Inputs were observed; the formula output is a derivation.
        assert dimension.evidence_truth_basis == TruthClass.OBSERVED
        assert dimension.value_truth_class == TruthClass.INFERRED


@pytest.mark.asyncio
async def test_inferred_engagement_evidence_never_counts_as_observed_input():
    """The INFERRED engagement ratio must not contaminate audience inputs."""
    candidate = make_candidate("Alpha", content_queries=["alpha video"])
    content = FakeContentProvider(
        {"alpha video": [video_for("V1", view_count=1000, like_count=50)]}
    )
    result = await orchestrate([candidate], content=content)

    dimension = result.profiles[0].dimension(DIM_AUDIENCE_INTEREST)
    # One video contributed exactly one OBSERVED view-count observation; the
    # derived engagement-rate record is excluded from the input tally.
    assert dimension.observed_input_count == 1
    assert dimension.evidence_truth_basis == TruthClass.OBSERVED


@pytest.mark.asyncio
async def test_duplicate_evidence_is_collapsed_not_double_counted():
    candidate = make_candidate("Alpha")
    search, _, _ = full_providers([candidate])
    store = ResearchStore()
    result = await run_preliminary_research(
        candidates=[candidate], store=store, search_demand_provider=search
    )
    evidence = [
        item
        for outcome in result.capabilities
        if outcome.snapshot_id is not None
        for item in store.evidence_for_snapshot(outcome.snapshot_id)
    ]
    assert evidence
    # Feed the identical observation twice; it describes one measurement.
    profile = build_preliminary_profile(
        candidate_id=candidate.id,
        candidate_title=candidate.title,
        evidence=evidence + evidence,
        search_demand=None,
        marketplace=None,
        public_content=None,
    )
    dimension = profile.dimension(DIM_SEARCH_DEMAND)
    assert dimension.duplicate_evidence_suppressed == len(evidence)
    assert dimension.observed_input_count == len(evidence)
    assert len(dimension.contributing_evidence_ids) == len(evidence)


# --------------------------------------------------------------- ranking


@pytest.mark.asyncio
async def test_ranking_is_deterministic_across_identical_runs():
    candidates = [make_candidate(f"Cand {i}") for i in range(6)]

    async def one_run():
        search, marketplace, content = full_providers(candidates)
        result = await orchestrate(
            candidates, search=search, marketplace=marketplace, content=content
        )
        return [
            (r.rank, r.candidate_id, tuple(c.value for c in r.criteria))
            for r in result.ranking.ranked
        ]

    assert await one_run() == await one_run()


@pytest.mark.asyncio
async def test_candidate_order_does_not_change_the_ranking():
    candidates = [make_candidate(f"Cand {i}") for i in range(5)]
    search, marketplace, content = full_providers(candidates)
    forward = await orchestrate(
        candidates, search=search, marketplace=marketplace, content=content
    )
    search2, marketplace2, content2 = full_providers(candidates)
    reversed_run = await orchestrate(
        list(reversed(candidates)),
        search=search2,
        marketplace=marketplace2,
        content=content2,
    )
    assert [r.candidate_id for r in forward.ranking.ranked] == [
        r.candidate_id for r in reversed_run.ranking.ranked
    ]


def test_higher_search_demand_ranks_first_with_equal_breadth():
    low = make_candidate("Low")
    high = make_candidate("High")
    profiles = [
        _profile_with(low, search_value=10.0),
        _profile_with(high, search_value=90.0),
    ]
    ranking = rank_candidates(profiles)
    assert ranking.ranked[0].candidate_id == high.id
    explanation = explain_pairwise(ranking.ranked[0], ranking.ranked[1])
    assert explanation.deciding_criterion == "search_demand"
    assert "90.0" in explanation.higher_value


def test_missing_dimension_sorts_below_an_observed_zero():
    zero = make_candidate("Zero")
    unknown = make_candidate("Unknown")
    profiles = [
        _profile_with(unknown, search_value=None),
        _profile_with(zero, search_value=0.0),
    ]
    ranking = rank_candidates(profiles)
    assert ranking.ranked[0].candidate_id == zero.id
    explanation = explain_pairwise(*ranking.ranked[:2])
    # Breadth decides first here: the scored candidate has one more dimension
    # backed by evidence, and absence is never read as a measurement.
    assert explanation.deciding_criterion == "evidence_breadth"
    assert explanation.higher_value == "1.0"
    assert explanation.lower_value == "0.0"
    # The candidate with no search evidence carries no value on that
    # criterion — it was never rewritten as a zero score.
    assert ranking.ranked[1].criterion("search_demand").value is None
    assert ranking.ranked[0].criterion("search_demand").value == 0.0


@pytest.mark.asyncio
async def test_with_equal_breadth_a_measured_value_outranks_an_absent_one():
    """Criterion 2 with breadth genuinely tied: 'no value' sorts last, not as 0.

    'Zulu' has search + marketplace evidence; 'Alpha' has audience +
    marketplace evidence. Both have four dimensions backed by evidence, so
    criterion 1 ties and criterion 2 (search demand) decides. Alpha has no
    search demand value at all, and a huge audience value cannot rescue it,
    because absence is not scored as zero and criterion 2 is checked first.
    """
    zulu = make_candidate("Zulu", search_queries=["zulu kw"], marketplace_queries=["z listing"])
    alpha = make_candidate("Alpha", content_queries=["a video"], marketplace_queries=["a listing"])
    search = FakeSearchProvider({"zulu kw": 10})  # deliberately tiny
    marketplace = FakeMarketplaceProvider(
        {"z listing": [listing_for("LZ")], "a listing": [listing_for("LA")]}
    )
    content = FakeContentProvider({"a video": [video_for("VA", view_count=9_000_000)]})
    result = await orchestrate(
        [alpha, zulu], search=search, marketplace=marketplace, content=content
    )

    by_id = {r.candidate_id: r for r in result.ranking.ranked}
    assert by_id[alpha.id].criterion("evidence_breadth").value == 4.0
    assert by_id[zulu.id].criterion("evidence_breadth").value == 4.0
    assert by_id[alpha.id].criterion("search_demand").value is None
    assert by_id[zulu.id].criterion("search_demand").value is not None

    assert result.ranking.ranked[0].candidate_id == zulu.id
    explanation = explain_pairwise(*result.ranking.ranked[:2])
    assert explanation.deciding_criterion == "search_demand"
    assert explanation.lower_value == "no value"
    assert "not read as zero" in explanation.reason


def test_full_tie_breaks_on_title_then_candidate_id():
    a_id = UUID("00000000-0000-4000-8000-000000000001")
    b_id = UUID("00000000-0000-4000-8000-000000000002")
    beta = make_candidate("Beta", candidate_id=b_id)
    alpha = make_candidate("Alpha", candidate_id=a_id)
    ranking = rank_candidates(
        [_profile_with(beta, search_value=50.0), _profile_with(alpha, search_value=50.0)]
    )
    assert [r.candidate_title for r in ranking.ranked] == ["Alpha", "Beta"]
    assert explain_pairwise(*ranking.ranked[:2]).deciding_criterion == "title"

    # Same title: the candidate id is the final deterministic tie-break.
    same_a = make_candidate("Same", candidate_id=a_id)
    same_b = make_candidate("Same", candidate_id=b_id)
    ranking = rank_candidates(
        [
            _profile_with(same_b, search_value=50.0),
            _profile_with(same_a, search_value=50.0),
        ]
    )
    assert [r.candidate_id for r in ranking.ranked] == [a_id, b_id]
    assert explain_pairwise(*ranking.ranked[:2]).deciding_criterion == "candidate_id"


def test_criteria_order_is_recorded_and_stable():
    assert CRITERIA_ORDER == (
        "evidence_breadth",
        "search_demand",
        "audience_interest",
        "price_comparables",
        "purchase_proxy_signals",
        "title",
        "candidate_id",
    )


def test_every_pairwise_order_is_explainable():
    profiles = [_profile_with(make_candidate(f"C{i}"), search_value=float(i)) for i in range(4)]
    ranking = rank_candidates(profiles)
    for higher, lower in zip(ranking.ranked, ranking.ranked[1:]):
        explanation = explain_pairwise(higher, lower)
        assert explanation.deciding_criterion in CRITERIA_ORDER
        assert explanation.higher_candidate_id == higher.candidate_id
        assert explanation.reason


# ------------------------------------------------------- top-5 selection


@pytest.mark.asyncio
async def test_selects_exactly_top_five_deterministically():
    candidates = [make_candidate(f"Cand {i}") for i in range(9)]
    search, marketplace, content = full_providers(candidates)
    result = await orchestrate(
        candidates, search=search, marketplace=marketplace, content=content
    )

    assert len(result.ranking.selected) == DEEP_RESEARCH_SELECTION_SIZE
    assert [r.rank for r in result.ranking.selected] == [1, 2, 3, 4, 5]
    assert all(r.selected_for_deep_research for r in result.ranking.ranked[:5])
    assert not any(r.selected_for_deep_research for r in result.ranking.ranked[5:])
    assert result.selected_candidate_ids == [r.candidate_id for r in result.ranking.ranked[:5]]


@pytest.mark.asyncio
async def test_fewer_than_five_candidates_selects_all_without_padding():
    candidates = [make_candidate(f"Cand {i}") for i in range(3)]
    search, marketplace, content = full_providers(candidates)
    result = await orchestrate(
        candidates, search=search, marketplace=marketplace, content=content
    )
    assert len(result.ranking.selected) == 3
    assert len(result.selected_candidate_ids) == 3


def test_single_candidate_is_ranked_and_selected():
    candidate = make_candidate("Only")
    ranking = rank_candidates([_profile_with(candidate, search_value=1.0)])
    assert ranking.ranked[0].rank == 1
    assert ranking.ranked[0].selected_for_deep_research
    assert ranking.ranking_version == PRELIMINARY_RANKING_VERSION


# --------------------------------------------------- orchestration honesty


@pytest.mark.asyncio
async def test_one_failed_provider_does_not_destroy_the_run():
    candidate = make_candidate("Alpha", marketplace_queries=["alpha listing"])
    search, _, content = full_providers([candidate])
    marketplace = FakeMarketplaceProvider(
        {"alpha listing": [listing_for("L1")]}, fail_queries={"alpha listing"}
    )
    result = await orchestrate(
        [candidate], search=search, marketplace=marketplace, content=content
    )

    by_capability = {o.capability: o for o in result.capabilities}
    assert by_capability[CAPABILITY_MARKETPLACE].status == SnapshotStatus.FAILED.value
    assert by_capability[CAPABILITY_MARKETPLACE].provider_errors
    assert by_capability[CAPABILITY_SEARCH_DEMAND].status == SnapshotStatus.COMPLETE.value
    assert by_capability[CAPABILITY_PUBLIC_CONTENT].status == SnapshotStatus.COMPLETE.value

    profile = result.profiles[0]
    assert profile.dimension(DIM_SEARCH_DEMAND).state == DimensionState.SCORED
    assert profile.dimension(DIM_AUDIENCE_INTEREST).state == DimensionState.SCORED
    assert profile.dimension(DIM_PRICE_EVIDENCE).state == DimensionState.MISSING
    assert profile.dimension(DIM_PRICE_EVIDENCE).missing_reason == MISSING_CAPABILITY_FAILED
    assert result.ranking.ranked[0].candidate_id == candidate.id


@pytest.mark.asyncio
async def test_capability_not_requested_is_reported_not_scored():
    candidate = make_candidate("Alpha")
    search, _, _ = full_providers([candidate])
    result = await orchestrate([candidate], search=search)

    by_capability = {o.capability: o for o in result.capabilities}
    assert by_capability[CAPABILITY_MARKETPLACE].status == STATUS_NOT_REQUESTED
    assert by_capability[CAPABILITY_PUBLIC_CONTENT].status == STATUS_NOT_REQUESTED
    profile = result.profiles[0]
    assert (
        profile.dimension(DIM_AUDIENCE_INTEREST).missing_reason
        == MISSING_CAPABILITY_NOT_REQUESTED
    )
    assert profile.dimension(DIM_AUDIENCE_INTEREST).value is None


@pytest.mark.asyncio
async def test_provider_error_escaping_a_runner_is_contained_by_the_orchestrator():
    """A failure outside a runner's own handled path must not kill the run."""

    class BrokenSearchProvider(FakeSearchProvider):
        @property
        def max_keywords_per_request(self) -> int:
            raise ProviderResponseError("adapter is misconfigured")

    candidate = make_candidate("Alpha")
    _, marketplace, content = full_providers([candidate])
    result = await orchestrate(
        [candidate],
        search=BrokenSearchProvider({}),
        marketplace=marketplace,
        content=content,
    )

    by_capability = {o.capability: o for o in result.capabilities}
    assert by_capability[CAPABILITY_SEARCH_DEMAND].status == STATUS_PROVIDER_FAILED
    assert "misconfigured" in by_capability[CAPABILITY_SEARCH_DEMAND].failure_reason
    # The other capabilities' evidence survived intact.
    assert by_capability[CAPABILITY_MARKETPLACE].status == SnapshotStatus.COMPLETE.value
    assert by_capability[CAPABILITY_PUBLIC_CONTENT].status == SnapshotStatus.COMPLETE.value
    profile = result.profiles[0]
    assert profile.dimension(DIM_AUDIENCE_INTEREST).state == DimensionState.SCORED
    assert profile.dimension(DIM_SEARCH_DEMAND).state == DimensionState.MISSING
    assert profile.dimension(DIM_SEARCH_DEMAND).missing_reason == MISSING_CAPABILITY_FAILED


@pytest.mark.asyncio
async def test_provider_setup_failure_is_reported_as_unavailable():
    candidate = make_candidate("Alpha")
    search, _, _ = full_providers([candidate])
    result = await orchestrate(
        [candidate],
        search=search,
        unavailable_capabilities={CAPABILITY_MARKETPLACE: "MissingCredentialsError: no key"},
    )
    by_capability = {o.capability: o for o in result.capabilities}
    assert by_capability[CAPABILITY_MARKETPLACE].status == STATUS_PROVIDER_FAILED
    assert "MissingCredentialsError" in by_capability[CAPABILITY_MARKETPLACE].failure_reason


@pytest.mark.asyncio
async def test_all_providers_failing_still_produces_a_ranked_run():
    candidates = [make_candidate("Alpha"), make_candidate("Beta")]
    result = await orchestrate(candidates)

    assert all(o.status == STATUS_NOT_REQUESTED for o in result.capabilities)
    assert len(result.ranking.ranked) == 2
    # With no evidence at all, ordering falls through to the pure tie-breaks.
    assert [r.candidate_title for r in result.ranking.ranked] == ["Alpha", "Beta"]
    for profile in result.profiles:
        assert profile.present_dimension_count == 0


@pytest.mark.asyncio
async def test_caps_and_cost_quota_accounting_are_preserved():
    candidates = [make_candidate(f"Cand {i}") for i in range(4)]
    search, marketplace, content = full_providers(candidates)
    result = await orchestrate(
        candidates,
        search=search,
        marketplace=marketplace,
        content=content,
        search_demand_caps=CapabilityCaps(max_provider_calls=1),
        marketplace_caps=CapabilityCaps(max_queries=2),
        public_content_caps=CapabilityCaps(max_quota_units=101),
    )
    by_capability = {o.capability: o for o in result.capabilities}

    # Search-demand call budget of 1 was honoured.
    assert search.calls == 1
    assert by_capability[CAPABILITY_SEARCH_DEMAND].provider_cost == pytest.approx(0.05)
    # Marketplace query cap of 2 was honoured (4 candidates -> 4 queries).
    assert len(marketplace.search_calls) == 2
    assert by_capability[CAPABILITY_MARKETPLACE].missing_query_count >= 2
    # Quota budget allowed exactly one YouTube search pass of 101 units.
    assert len(content.search_calls) == 1
    assert by_capability[CAPABILITY_PUBLIC_CONTENT].quota_units_used == 101
    assert by_capability[CAPABILITY_PUBLIC_CONTENT].quota_units_is_exact is True


@pytest.mark.asyncio
async def test_shared_evidence_across_candidates_keeps_both_provenances():
    shared = "shared listing"
    a = make_candidate("Alpha", marketplace_queries=[shared])
    b = make_candidate("Beta", marketplace_queries=[shared])
    marketplace = FakeMarketplaceProvider({shared: [listing_for("L1")]})
    result = await orchestrate([a, b], marketplace=marketplace)

    assert len(marketplace.search_calls) == 1, "one query serves both candidates"
    ids_a = result.profiles[0].dimension(DIM_PRICE_EVIDENCE).contributing_evidence_ids
    ids_b = result.profiles[1].dimension(DIM_PRICE_EVIDENCE).contributing_evidence_ids
    assert ids_a and ids_b
    assert set(ids_a).isdisjoint(ids_b), "each candidate keeps its own evidence records"


@pytest.mark.asyncio
async def test_no_final_scoring_leaks_into_preliminary_output():
    """3C must not emit POS, ECS, or RED/YELLOW/GREEN classification."""
    candidates = [make_candidate(f"Cand {i}") for i in range(3)]
    search, marketplace, content = full_providers(candidates)
    result = await orchestrate(
        candidates, search=search, marketplace=marketplace, content=content
    )

    forbidden = {
        "opportunity_score",
        "evidence_confidence",
        "classification",
        "kill_rules_triggered",
    }
    for ranked in result.ranking.ranked:
        assert forbidden.isdisjoint(ranked.__slots__)
        for dimension in ranked.profile.dimensions.values():
            assert forbidden.isdisjoint(dimension.__slots__)
            # Preliminary names are namespaced away from ScoreDimensions.
            assert dimension.name.startswith("preliminary_")


def test_versions_are_recorded_for_reproducibility():
    profile = _profile_with(make_candidate("Alpha"), search_value=1.0)
    ranking = rank_candidates([profile])
    assert ranking.ranking_version == "preliminary_rank_v1"
    assert profile.bridge_version == "preliminary_dimensions_v1"
    assert profile.dimension(DIM_SEARCH_DEMAND).formula_version == "search_demand_dimension_v1"


# -------------------------------------------------------------- API route


def test_preliminary_endpoint_returns_ranked_explainable_output(monkeypatch):
    from fastapi.testclient import TestClient

    from app.api import routes
    from app.main import app

    candidates = [make_candidate(f"Cand {i}") for i in range(7)]
    search, marketplace, content = full_providers(candidates)
    monkeypatch.setitem(routes.SEARCH_DEMAND_PROVIDERS, "dataforseo", lambda: search)
    monkeypatch.setitem(routes.MARKETPLACE_PROVIDERS, "etsy", lambda: marketplace)
    monkeypatch.setitem(routes.PUBLIC_CONTENT_PROVIDERS, "youtube", lambda: content)

    client = TestClient(app)
    response = client.post(
        "/research/preliminary",
        json={"candidates": [c.model_dump(mode="json") for c in candidates]},
    )
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["candidate_count"] == 7
    assert len(body["ranked"]) == 7
    assert len(body["selected_candidate_ids"]) == DEEP_RESEARCH_SELECTION_SIZE
    assert body["ranking_version"] == "preliminary_rank_v1"
    assert body["orchestration_version"] == "preliminary_orchestration_v1"
    assert body["criteria_order"][0] == "evidence_breadth"
    # Every adjacent pair carries a stated deciding criterion.
    assert len(body["adjacent_rank_explanations"]) == 6
    assert all(e["deciding_criterion"] for e in body["adjacent_rank_explanations"])
    # Provenance survives serialization.
    top = body["ranked"][0]
    demand = next(d for d in top["dimensions"] if d["name"] == DIM_SEARCH_DEMAND)
    assert demand["contributing_evidence_ids"]
    assert demand["value_truth_class"] == "INFERRED"
    assert demand["formula_version"] == "search_demand_dimension_v1"
    # No final scoring anywhere in the payload.
    assert "opportunity_score" not in response.text
    assert "classification" not in response.text


def test_preliminary_endpoint_reports_disabled_capability(monkeypatch):
    from fastapi.testclient import TestClient

    from app.api import routes
    from app.main import app

    candidates = [make_candidate("Alpha")]
    search, _, _ = full_providers(candidates)
    monkeypatch.setitem(routes.SEARCH_DEMAND_PROVIDERS, "dataforseo", lambda: search)

    client = TestClient(app)
    response = client.post(
        "/research/preliminary",
        json={
            "candidates": [c.model_dump(mode="json") for c in candidates],
            "marketplace": None,
            "public_content_provider": None,
        },
    )
    assert response.status_code == 200, response.text
    statuses = {c["capability"]: c["status"] for c in response.json()["capabilities"]}
    assert statuses[CAPABILITY_MARKETPLACE] == STATUS_NOT_REQUESTED
    assert statuses[CAPABILITY_PUBLIC_CONTENT] == STATUS_NOT_REQUESTED
    assert statuses[CAPABILITY_SEARCH_DEMAND] == SnapshotStatus.COMPLETE.value


def test_preliminary_endpoint_rejects_unknown_provider():
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    response = client.post(
        "/research/preliminary",
        json={
            "candidates": [make_candidate("Alpha").model_dump(mode="json")],
            "marketplace": "not-a-marketplace",
        },
    )
    assert response.status_code == 422


# ---------------------------------------------------------------- helpers


def _profile_with(candidate: Candidate, search_value: float | None):
    """A profile carrying only a search-demand dimension, for ranking tests."""
    from app.services.search_demand_features import SearchDemandSummary

    summary = (
        SearchDemandSummary(
            candidate_id=candidate.id,
            relevant_keyword_count=1,
            median_search_volume=1.0,
            max_search_volume=1,
            total_search_volume=1,
            median_cpc=None,
            median_competition_index=None,
            history_months_median=None,
            missing_data_count=0,
            search_demand_dimension=search_value,
        )
        if search_value is not None
        else None
    )
    evidence = []
    if search_value is not None:
        from app.services.search_demand import build_evidence_item

        evidence = [
            build_evidence_item(
                candidate.id,
                uuid4(),
                uuid4(),
                metrics_for("kw", 1000),
                SearchDemandBatchResult(
                    provider="fake-search",
                    metrics=[],
                    retrieved_at=datetime(2025, 8, 1, tzinfo=UTC),
                    collection_method="official_api",
                    source_reference="/fake/keywords",
                ),
                "US",
                "en",
            )
        ]
    return build_preliminary_profile(
        candidate_id=candidate.id,
        candidate_title=candidate.title,
        evidence=evidence,
        search_demand=summary,
        marketplace=None,
        public_content=None,
    )
