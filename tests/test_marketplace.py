import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api import routes
from app.domain.enums import EvidencePurpose, ProductFormat, SnapshotStatus, TruthClass
from app.domain.models import Candidate
from app.main import app
from app.providers.base import (
    ListingReviewStats,
    MarketplaceListing,
    MarketplaceProvider,
    MarketplaceSearchResult,
    MissingCredentialsError,
    ProviderAuthError,
    ProviderRateLimitError,
    ProviderResponseError,
)
from app.providers.etsy import EtsyMarketplaceProvider
from app.services.marketplace_features import (
    summarize_competition,
    summarize_price,
    summarize_purchase_proxy,
)
from app.services.marketplace_research import (
    MISSING_BUDGET,
    MISSING_PROVIDER_ERROR,
    run_marketplace_research,
)
from app.storage.memory import ImmutableEvidenceError, ResearchStore

client = TestClient(app)


def make_candidate(queries: list[str], title: str = "Sourdough Guide") -> Candidate:
    return Candidate(
        seed_keyword="sourdough baking",
        title=title,
        problem="A problem statement.",
        target_buyer="A buyer",
        proposed_format=ProductFormat.PDF_GUIDE,
        buyer_outcome="An outcome",
        search_queries=["s"],
        marketplace_queries=queries,
        content_queries=["c"],
        generation_reason="A hypothesis",
    )


def listing(listing_id: str, **overrides: Any) -> MarketplaceListing:
    payload: dict[str, Any] = dict(
        listing_id=listing_id,
        title=f"Listing {listing_id}",
        source_reference=f"https://www.etsy.com/listing/{listing_id}",
        price=12.5,
        currency="USD",
        seller_id="shop-1",
        review_count=None,
        created_at=datetime.now(UTC) - timedelta(days=100),
        state="active",
        is_digital=True,
        retrieved_at=datetime.now(UTC),
    )
    payload.update(overrides)
    return MarketplaceListing(**payload)


class FakeMarketplaceProvider(MarketplaceProvider):
    name = "fake-market"
    collection_method = "official_api"
    max_results_per_query = 25

    def __init__(
        self,
        listings_map: dict[str, list[MarketplaceListing]] | None = None,
        reviews_map: dict[str, ListingReviewStats] | None = None,
        fail_queries: set[str] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.listings_map = listings_map or {}
        self.reviews_map = reviews_map or {}
        self.fail_queries = fail_queries or set()
        self.error = error or ProviderResponseError("simulated marketplace failure")
        self.search_calls = 0
        self.review_calls = 0

    async def search_listings(self, query: str, limit: int) -> MarketplaceSearchResult:
        self.search_calls += 1
        if query in self.fail_queries:
            raise self.error
        return MarketplaceSearchResult(
            provider=self.name,
            query=query,
            listings=self.listings_map.get(query, [])[:limit],
            retrieved_at=datetime.now(UTC),
            collection_method=self.collection_method,
            source_reference="/fake",
            call_count=1,
        )

    async def fetch_listing_reviews(self, listing_id: str) -> ListingReviewStats:
        self.review_calls += 1
        return self.reviews_map.get(
            listing_id, ListingReviewStats(listing_id=listing_id, review_count=None)
        )


# ------------------------------------------------------ Etsy normalization


ETSY_SEARCH_BODY = {
    "count": 2,
    "results": [
        {
            "listing_id": 111222333,
            "user_id": 42,
            "shop_id": 777,
            "title": "Sourdough Baking Guide PDF",
            "state": "active",
            "original_creation_timestamp": 1700000000,
            "url": "https://www.etsy.com/listing/111222333/sourdough-guide",
            "price": {"amount": 899, "divisor": 100, "currency_code": "USD"},
            "taxonomy_id": 2078,
            "listing_type": "download",
        },
        {
            "listing_id": 444555666,
            "title": "Mystery listing",
            "price": None,
        },
    ],
}


def etsy_provider(handler, monkeypatch) -> EtsyMarketplaceProvider:
    monkeypatch.setenv("ETSY_API_KEY", "test-etsy-keystring")
    return EtsyMarketplaceProvider(transport=httpx.MockTransport(handler))


async def test_etsy_search_normalizes_returned_fields(monkeypatch):
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["api_key_header"] = request.headers.get("x-api-key")
        return httpx.Response(200, json=ETSY_SEARCH_BODY)

    provider = etsy_provider(handler, monkeypatch)
    result = await provider.search_listings("sourdough baking pdf guide", limit=10)

    assert "/v3/application/listings/active" in captured["url"]
    assert "keywords=sourdough" in captured["url"]
    assert captured["api_key_header"] == "test-etsy-keystring"

    full = next(l for l in result.listings if l.listing_id == "111222333")
    assert full.title == "Sourdough Baking Guide PDF"
    assert full.price == 8.99
    assert full.currency == "USD"
    assert full.seller_id == "777"
    assert full.state == "active"
    assert full.taxonomy == "2078"
    assert full.is_digital is True
    assert full.source_reference == "https://www.etsy.com/listing/111222333/sourdough-guide"
    assert full.created_at == datetime.fromtimestamp(1700000000, tz=UTC)
    # Review data is NOT part of listing search and must stay unknown here.
    assert full.review_count is None
    assert full.rating is None


async def test_etsy_missing_fields_stay_null(monkeypatch):
    provider = etsy_provider(lambda r: httpx.Response(200, json=ETSY_SEARCH_BODY), monkeypatch)
    result = await provider.search_listings("anything", limit=10)
    sparse = next(l for l in result.listings if l.listing_id == "444555666")
    assert sparse.price is None
    assert sparse.currency is None
    assert sparse.seller_id is None
    assert sparse.created_at is None
    assert sparse.is_digital is None
    assert sparse.taxonomy is None


async def test_etsy_review_stats(monkeypatch):
    body = {"count": 128, "results": [{"rating": 5}, {"rating": 4}, {"rating": 5}]}
    provider = etsy_provider(lambda r: httpx.Response(200, json=body), monkeypatch)
    stats = await provider.fetch_listing_reviews("111222333")
    assert stats.review_count == 128
    assert stats.average_rating == 4.67  # mean of the sample only
    assert stats.rating_sample_size == 3


async def test_etsy_error_mapping(monkeypatch):
    for status, exc_type in (
        (401, ProviderAuthError),
        (403, ProviderAuthError),
        (429, ProviderRateLimitError),
        (500, ProviderResponseError),
    ):
        provider = etsy_provider(lambda r, s=status: httpx.Response(s), monkeypatch)
        with pytest.raises(exc_type):
            await provider.search_listings("kw", limit=5)


def test_etsy_missing_credentials(monkeypatch):
    monkeypatch.delenv("ETSY_API_KEY", raising=False)
    with pytest.raises(MissingCredentialsError):
        EtsyMarketplaceProvider()


async def test_etsy_secret_never_exposed(monkeypatch):
    secret = "super-secret-etsy-keystring"
    monkeypatch.setenv("ETSY_API_KEY", secret)
    provider = EtsyMarketplaceProvider(transport=httpx.MockTransport(lambda r: httpx.Response(403)))
    assert secret not in repr(provider)
    with pytest.raises(ProviderAuthError) as exc_info:
        await provider.search_listings("kw", limit=5)
    assert secret not in str(exc_info.value)


# ------------------------------------------------------------- research service


async def test_listing_dedupe_within_and_across_queries():
    # Same listing returned by both queries of one candidate: one observation.
    shared = listing("L1")
    candidate = make_candidate(["query one", "query two"])
    provider = FakeMarketplaceProvider(
        {"query one": [shared, listing("L2")], "query two": [shared]},
        reviews_map={},
    )
    store = ResearchStore()
    result = await run_marketplace_research([candidate], provider, store, max_review_lookups=0)

    items = store.evidence_for_snapshot(result.snapshot.snapshot_id)
    presence = [i for i in items if i.signal_type == "marketplace_listing_presence"]
    assert {i.raw_value for i in presence} == {"L1", "L2"}
    assert len(presence) == 2  # L1 not duplicated for the same candidate


async def test_shared_listing_supports_multiple_candidates_without_duplicate_observation():
    shared = listing("L1")
    a = make_candidate(["shared query"])
    b = make_candidate(["shared query"], title="Other")
    provider = FakeMarketplaceProvider({"shared query": [shared]})
    store = ResearchStore()
    result = await run_marketplace_research([a, b], provider, store, max_review_lookups=0)

    assert provider.search_calls == 1  # query deduplicated across candidates
    ev_a = [store.get_evidence(e) for e in result.evidence_ids_by_candidate[a.id]]
    ev_b = [store.get_evidence(e) for e in result.evidence_ids_by_candidate[b.id]]
    assert ev_a and ev_b
    # Same underlying observation: identical raw_payload_hash, distinct records.
    assert ev_a[0].raw_payload_hash == ev_b[0].raw_payload_hash
    assert ev_a[0].id != ev_b[0].id


async def test_evidence_classification_matches_what_fields_prove():
    provider = FakeMarketplaceProvider(
        {"q": [listing("L1", price=9.99, currency="USD")]},
        reviews_map={"L1": ListingReviewStats("L1", review_count=42, average_rating=4.8, rating_sample_size=42)},
    )
    candidate = make_candidate(["q"])
    store = ResearchStore()
    result = await run_marketplace_research([candidate], provider, store)

    items = store.evidence_for_snapshot(result.snapshot.snapshot_id)
    by_purpose = {i.purpose: i for i in items}
    assert set(by_purpose) == {
        EvidencePurpose.COMPETITION,
        EvidencePurpose.PRICE,
        EvidencePurpose.PURCHASE,
    }
    for item in items:
        assert item.truth_class == TruthClass.OBSERVED
        assert item.marketplace == "fake-market"

    price = by_purpose[EvidencePurpose.PRICE]
    assert price.signal_type == "listing_price"
    assert price.raw_value == 9.99
    assert price.unit == "USD"

    purchase = by_purpose[EvidencePurpose.PURCHASE]
    assert purchase.signal_type == "review_count_purchase_proxy"
    assert purchase.raw_value == 42
    assert purchase.unit == "reviews"


async def test_reviews_are_never_interpreted_as_exact_sales():
    provider = FakeMarketplaceProvider(
        {"q": [listing("L1")]},
        reviews_map={"L1": ListingReviewStats("L1", review_count=500, rating_sample_size=100)},
    )
    candidate = make_candidate(["q"])
    store = ResearchStore()
    result = await run_marketplace_research([candidate], provider, store)

    items = store.evidence_for_snapshot(result.snapshot.snapshot_id)
    for item in items:
        # No evidence anywhere claims sales, units, or revenue.
        assert "sales" not in item.signal_type
        assert "revenue" not in item.signal_type
        assert item.unit not in ("sales", "units_sold", "revenue")
    purchase = next(i for i in items if i.purpose == EvidencePurpose.PURCHASE)
    assert "PROXY" in " ".join(purchase.known_limitations)
    assert any("never be converted into sales" in lim for lim in purchase.known_limitations)
    # The summary names it purchase_proxy and exposes no sales field.
    summary = result.purchase_proxy_summaries[candidate.id]
    assert type(summary).__name__ == "PurchaseProxySummary"
    assert not any("sales" in f for f in summary.__dataclass_fields__)


async def test_missing_fields_produce_no_evidence_not_invented_values():
    sparse = listing("L1", price=None, currency=None, seller_id=None, created_at=None, is_digital=None)
    provider = FakeMarketplaceProvider({"q": [sparse]})
    candidate = make_candidate(["q"])
    store = ResearchStore()
    result = await run_marketplace_research([candidate], provider, store, max_review_lookups=0)

    items = store.evidence_for_snapshot(result.snapshot.snapshot_id)
    assert [i.purpose for i in items] == [EvidencePurpose.COMPETITION]  # no price, no reviews
    summary = result.price_summaries[candidate.id]
    assert summary.relevant_paid_comparable_count == 0
    assert summary.insufficient_evidence is True
    assert summary.median_price is None


async def test_partial_provider_failure_keeps_run_alive():
    provider = FakeMarketplaceProvider(
        {"good query": [listing("L1")]},
        fail_queries={"bad query"},
    )
    candidate = make_candidate(["good query", "bad query"])
    store = ResearchStore()
    result = await run_marketplace_research([candidate], provider, store, max_review_lookups=0)

    assert result.snapshot.status == SnapshotStatus.PARTIAL
    assert result.provider_errors
    assert [m.reason for m in result.missing_queries if m.keyword == "bad query"] == [
        MISSING_PROVIDER_ERROR
    ]
    assert result.evidence_ids_by_candidate[candidate.id]  # good query still produced evidence


async def test_rate_limit_error_is_partial_not_fatal():
    provider = FakeMarketplaceProvider(
        {"ok": [listing("L1")]},
        fail_queries={"limited"},
        error=ProviderRateLimitError("Etsy rate limit reached"),
    )
    candidate = make_candidate(["ok", "limited"])
    store = ResearchStore()
    result = await run_marketplace_research([candidate], provider, store, max_review_lookups=0)
    assert result.snapshot.status == SnapshotStatus.PARTIAL
    assert any("ProviderRateLimitError" in e for e in result.provider_errors)


async def test_call_budget_exhaustion_reports_missing_queries():
    provider = FakeMarketplaceProvider({"q1": [listing("L1")], "q2": [listing("L2")]})
    candidate = make_candidate(["q1", "q2"])
    store = ResearchStore()
    result = await run_marketplace_research(
        [candidate], provider, store, max_provider_calls=1, max_review_lookups=0
    )
    assert provider.search_calls == 1
    assert [m.keyword for m in result.missing_queries if m.reason == MISSING_BUDGET] == ["q2"]


async def test_review_lookup_cap_limits_calls():
    listings = [listing(f"L{i}") for i in range(5)]
    provider = FakeMarketplaceProvider(
        {"q": listings},
        reviews_map={l.listing_id: ListingReviewStats(l.listing_id, review_count=10) for l in listings},
    )
    candidate = make_candidate(["q"])
    store = ResearchStore()
    result = await run_marketplace_research([candidate], provider, store, max_review_lookups=2)
    assert provider.review_calls == 2
    assert result.review_lookups_used == 2
    summary = result.purchase_proxy_summaries[candidate.id]
    assert summary.listings_with_review_data == 2
    assert summary.missing_data_count == 3  # unenriched listings reported missing


async def test_snapshots_immutable_and_cache_avoids_repeat_calls():
    provider = FakeMarketplaceProvider({"q": [listing("L1")]})
    candidate = make_candidate(["q"])
    store = ResearchStore()

    first = await run_marketplace_research([candidate], provider, store, max_review_lookups=0)
    first_items = store.evidence_for_snapshot(first.snapshot.snapshot_id)

    second = await run_marketplace_research([candidate], provider, store, max_review_lookups=0)
    assert provider.search_calls == 1  # second run served from cache
    assert second.cached_query_count == 1
    assert second.snapshot.snapshot_id != first.snapshot.snapshot_id
    assert store.evidence_for_snapshot(first.snapshot.snapshot_id) == first_items

    with pytest.raises(Exception):
        first_items[0].raw_value = "tampered"
    with pytest.raises(ImmutableEvidenceError):
        store.add_snapshot(first.snapshot)
    with pytest.raises(ImmutableEvidenceError):
        store.add_evidence(first_items[0])


# ----------------------------------------------------------------- summaries


def test_price_quartiles_are_deterministic_and_robust():
    cid = uuid4()
    listings = [
        listing("L1", price=5.0, seller_id="a"),
        listing("L2", price=10.0, seller_id="b"),
        listing("L3", price=15.0, seller_id="c"),
        listing("L4", price=20.0, seller_id="a"),
        listing("L5", price=100.0, seller_id="d"),  # outlier
        listing("L6", price=None),
    ]
    s1 = summarize_price(cid, listings)
    s2 = summarize_price(cid, listings)
    assert s1 == s2

    assert s1.relevant_paid_comparable_count == 5
    assert s1.unique_seller_count == 4
    assert s1.currency == "USD"
    assert s1.min_price == 5.0
    assert s1.p25_price == 10.0
    assert s1.median_price == 15.0
    assert s1.p75_price == 20.0
    assert s1.max_price == 100.0
    assert s1.missing_data_count == 1
    assert s1.insufficient_evidence is False
    assert not hasattr(s1, "optimal_price")


def test_insufficient_price_evidence_is_reported_not_computed():
    cid = uuid4()
    summary = summarize_price(cid, [listing("L1", price=9.0), listing("L2", price=None)])
    assert summary.relevant_paid_comparable_count == 1
    assert summary.insufficient_evidence is True
    assert summary.median_price is None
    assert summary.p25_price is None


def test_purchase_proxy_summary_features():
    cid = uuid4()
    listings = [
        listing("L1", review_count=0, rating=None),
        listing("L2", review_count=10, rating=4.5),
        listing("L3", review_count=90, rating=5.0),
        listing("L4", review_count=None),
    ]
    summary = summarize_purchase_proxy(cid, listings)
    assert summary.relevant_listing_count == 4
    assert summary.listings_with_review_data == 3
    assert summary.listings_with_reviews == 2
    assert summary.median_review_count == 10.0
    assert summary.upper_quartile_review_count == 50.0
    assert summary.review_concentration == 0.9  # 90 of 100 observed reviews
    assert summary.rating_distribution == {"4.5": 1, "5.0": 1}
    assert summary.missing_data_count == 1


def test_competition_summary_makes_no_goodness_judgment():
    cid = uuid4()
    listings = [
        listing("L1", seller_id="a", review_count=10, price=10.0),
        listing("L2", seller_id="a", review_count=20, price=12.0),
        listing("L3", seller_id="b", review_count=None, price=20.0),
    ]
    summary = summarize_competition(cid, listings)
    assert summary.relevant_listing_count == 3
    assert summary.unique_seller_count == 2
    assert summary.seller_concentration == round(2 / 3, 4)
    assert summary.review_burden_median == 15.0
    assert summary.price_dispersion == round((16.0 - 11.0) / 12.0, 4)
    assert summary.median_listing_age_days is not None
    # No score, no classification, no opportunity judgment fields.
    for name in summary.__dataclass_fields__:
        assert "score" not in name and "opportunity" not in name and "good" not in name


# ---------------------------------------------------------------- endpoints


def fake_provider_cls(
    listings_map: dict[str, list[MarketplaceListing]],
    reviews_map: dict[str, ListingReviewStats] | None = None,
):
    class _Cls(FakeMarketplaceProvider):
        def __init__(self) -> None:
            super().__init__(listings_map, reviews_map)

    return _Cls


def test_marketplace_endpoint_with_inline_candidates(monkeypatch):
    candidate = make_candidate(["kw one"])
    monkeypatch.setitem(
        routes.MARKETPLACE_PROVIDERS,
        "etsy",
        fake_provider_cls(
            {"kw one": [listing("L1", price=8.0), listing("L2", price=12.0), listing("L3", price=16.0)]},
            {"L1": ListingReviewStats("L1", review_count=25, average_rating=4.6, rating_sample_size=25)},
        ),
    )
    response = client.post(
        "/research/marketplace",
        json={"candidates": [json.loads(candidate.model_dump_json())]},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["snapshot"]["provider"] == "fake-market"
    assert body["snapshot"]["status"] == "COMPLETE"
    assert body["snapshot"]["normalization_version"] == "marketplace_norm_v1"
    assert body["snapshot"]["provider_call_count"] >= 1

    summary = body["summaries"][0]
    assert summary["candidate_id"] == str(candidate.id)
    assert summary["purchase_proxy"]["listings_with_review_data"] == 1
    assert summary["price"]["median_price"] == 12.0
    assert summary["price"]["insufficient_evidence"] is False
    assert summary["competition"]["relevant_listing_count"] == 3
    assert body["evidence_ids_by_candidate"][str(candidate.id)]

    # Generalized snapshot retrieval returns the marketplace evidence.
    snap_id = body["snapshot"]["snapshot_id"]
    stored = client.get(f"/research/snapshots/{snap_id}")
    assert stored.status_code == 200
    purposes = {e["purpose"] for e in stored.json()["evidence"]}
    assert purposes == {"COMPETITION", "PRICE", "PURCHASE"}


def test_marketplace_endpoint_accepts_research_run_id(monkeypatch):
    discovery = client.post("/candidates/discover", json={"seed_keyword": "woodworking"})
    run_id = discovery.json()["research_run_id"]
    monkeypatch.setitem(routes.MARKETPLACE_PROVIDERS, "etsy", fake_provider_cls({}))
    response = client.post(
        "/research/marketplace", json={"research_run_id": run_id, "max_provider_calls": 0}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["snapshot"]["research_run_id"] == run_id
    assert body["missing_queries"]  # budget 0 -> everything explicitly missing


def test_marketplace_endpoint_validation_errors():
    assert client.post("/research/marketplace", json={}).status_code == 422
    candidate = json.loads(make_candidate(["kw"]).model_dump_json())
    assert (
        client.post(
            "/research/marketplace",
            json={"candidates": [candidate], "provider": "amazon"},
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/research/marketplace", json={"research_run_id": str(uuid4())}
        ).status_code
        == 404
    )


def test_marketplace_endpoint_missing_credentials_503(monkeypatch):
    monkeypatch.delenv("ETSY_API_KEY", raising=False)
    candidate = json.loads(make_candidate(["kw"]).model_dump_json())
    response = client.post("/research/marketplace", json={"candidates": [candidate]})
    assert response.status_code == 503
    assert "ETSY_API_KEY" in response.json()["detail"]


def test_marketplace_endpoint_response_never_contains_secret(monkeypatch):
    secret = "endpoint-etsy-secret"
    monkeypatch.setenv("ETSY_API_KEY", secret)
    monkeypatch.setitem(
        routes.MARKETPLACE_PROVIDERS, "etsy", fake_provider_cls({"kw": [listing("L1")]})
    )
    candidate = json.loads(make_candidate(["kw"]).model_dump_json())
    response = client.post("/research/marketplace", json={"candidates": [candidate]})
    assert response.status_code == 200
    assert secret not in response.text
