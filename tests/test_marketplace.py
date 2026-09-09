"""Milestone 3A tests. All Etsy traffic goes through httpx.MockTransport or
fake providers — no automated test ever makes a real Etsy network request."""

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
    MarketplaceQueryResult,
    MissingCredentialsError,
    ProviderAuthError,
    ProviderRateLimitError,
    ProviderResponseError,
)
from app.providers.etsy import ENV_API_KEY, EtsyMarketplaceProvider, normalize_listing
from app.services.marketplace import (
    MISSING_BUDGET,
    MISSING_PROVIDER_ERROR,
    MISSING_QUERY_CAP,
    plan_marketplace_queries,
    run_marketplace_research,
)
from app.services.marketplace_features import (
    summarize_marketplace,
    summarize_price,
    summarize_purchase_proxy,
)
from app.storage.memory import ImmutableEvidenceError, ResearchStore

client = TestClient(app)

FAKE_KEY = "test-etsy-keystring-not-a-real-secret"


def make_candidate(marketplace_queries: list[str], title: str = "Sourdough Guide") -> Candidate:
    return Candidate(
        seed_keyword="sourdough baking",
        title=title,
        problem="A problem statement.",
        target_buyer="A buyer",
        proposed_format=ProductFormat.PDF_GUIDE,
        buyer_outcome="An outcome",
        search_queries=["s"],
        marketplace_queries=marketplace_queries,
        content_queries=["c"],
        generation_reason="A hypothesis",
    )


def listing_for(
    listing_id: str,
    price: float | None = 12.5,
    review_count: int | None = 10,
    seller_id: str | None = "shop-1",
    **overrides: Any,
) -> MarketplaceListing:
    fields: dict[str, Any] = dict(
        listing_id=listing_id,
        title=f"Listing {listing_id}",
        url=f"https://www.etsy.com/listing/{listing_id}",
        price=price,
        currency="USD",
        seller_id=seller_id,
        review_count=review_count,
        created_at=datetime(2024, 1, 1, tzinfo=UTC),
        state="active",
        listing_type="download",
        is_digital=True,
        retrieved_at=datetime.now(UTC),
    )
    fields.update(overrides)
    return MarketplaceListing(**fields)


class FakeMarketplaceProvider(MarketplaceProvider):
    """Deterministic in-memory provider; records calls, never touches network."""

    name = "fake-marketplace"
    collection_method = "official_api"
    supports_review_stats = True

    def __init__(
        self,
        listings_by_query: dict[str, list[MarketplaceListing]],
        review_counts: dict[str, int] | None = None,
        fail_queries: set[str] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.listings_by_query = listings_by_query
        self.review_counts = review_counts or {}
        self.fail_queries = fail_queries or set()
        self.error = error
        self.search_calls: list[str] = []
        self.review_calls: list[str] = []

    async def search_listings(self, query: str, limit: int) -> MarketplaceQueryResult:
        self.search_calls.append(query)
        if query in self.fail_queries:
            raise self.error or ProviderResponseError("simulated failure")
        listings = self.listings_by_query.get(query, [])[:limit]
        return MarketplaceQueryResult(
            provider=self.name,
            query=query,
            listings=listings,
            retrieved_at=datetime.now(UTC),
            collection_method=self.collection_method,
            source_reference="/fake/listings",
            call_count=1,
        )

    async def fetch_review_stats(self, listing_id: str) -> ListingReviewStats:
        self.review_calls.append(listing_id)
        return ListingReviewStats(
            listing_id=listing_id,
            review_count=self.review_counts.get(listing_id),
            retrieved_at=datetime.now(UTC),
        )


# --------------------------------------------------------------- query planning


def test_plan_marketplace_queries_deduplicates_and_keeps_mapping():
    c1 = make_candidate(["Sourdough  PDF", "sourdough pdf", "starter kit"])
    c2 = make_candidate(["SOURDOUGH PDF", "bread checklist"], title="Other")
    plan = plan_marketplace_queries([c1, c2])
    assert plan.queries == ["sourdough pdf", "starter kit", "bread checklist"]
    assert plan.query_to_candidates["sourdough pdf"] == [c1.id, c2.id]
    assert plan.query_to_candidates["bread checklist"] == [c2.id]


# ------------------------------------------------------ Etsy response normalization


def etsy_listing_payload(listing_id: int = 111, **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "listing_id": listing_id,
        "title": "Sourdough Baking Guide PDF",
        "url": f"https://www.etsy.com/listing/{listing_id}",
        "price": {"amount": 1250, "divisor": 100, "currency_code": "USD"},
        "shop_id": 777,
        "state": "active",
        "taxonomy_id": 42,
        "listing_type": "download",
        "original_creation_timestamp": 1700000000,
        "quantity": 5,
    }
    payload.update(overrides)
    return payload


def etsy_transport(handler):
    return httpx.MockTransport(handler)


def make_etsy_provider(monkeypatch, handler) -> EtsyMarketplaceProvider:
    monkeypatch.setenv(ENV_API_KEY, FAKE_KEY)
    return EtsyMarketplaceProvider(transport=etsy_transport(handler))


async def test_etsy_normalizes_returned_fields(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-api-key"] == FAKE_KEY
        assert request.url.path == "/v3/application/listings/active"
        assert request.url.params["keywords"] == "sourdough pdf"
        return httpx.Response(200, json={"count": 5000, "results": [etsy_listing_payload()]})

    provider = make_etsy_provider(monkeypatch, handler)
    result = await provider.search_listings("sourdough pdf", limit=10)

    assert result.total_available == 5000
    assert len(result.listings) == 1
    listing = result.listings[0]
    assert listing.listing_id == "111"
    assert listing.price == 12.5
    assert listing.currency == "USD"
    assert listing.seller_id == "777"
    assert listing.state == "active"
    assert listing.taxonomy == "42"
    assert listing.listing_type == "download"
    assert listing.is_digital is True
    assert listing.created_at is not None and listing.created_at.tzinfo is not None
    # Etsy listing search returns no review data; it must stay None.
    assert listing.review_count is None
    assert listing.rating is None


def test_etsy_missing_fields_stay_none():
    minimal = normalize_listing({"listing_id": 9}, datetime.now(UTC))
    assert minimal is not None
    assert minimal.listing_id == "9"
    for name in ("title", "url", "price", "currency", "seller_id", "rating",
                 "review_count", "created_at", "state", "taxonomy",
                 "listing_type", "is_digital"):
        assert getattr(minimal, name) is None
    # No usable listing_id -> no record, not a fabricated one.
    assert normalize_listing({"title": "no id"}, datetime.now(UTC)) is None


async def test_etsy_review_stats_uses_count_only(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v3/application/listings/111/reviews"
        return httpx.Response(
            200, json={"count": 321, "results": [{"rating": 5}, {"rating": 1}]}
        )

    provider = make_etsy_provider(monkeypatch, handler)
    stats = await provider.fetch_review_stats("111")
    assert stats.review_count == 321  # provider-reported total only; page ignored


async def test_etsy_auth_and_rate_limit_errors(monkeypatch):
    for status, exc_type in ((401, ProviderAuthError), (403, ProviderAuthError),
                             (429, ProviderRateLimitError), (500, ProviderResponseError)):
        provider = make_etsy_provider(
            monkeypatch, lambda request, s=status: httpx.Response(s, json={})
        )
        with pytest.raises(exc_type) as excinfo:
            await provider.search_listings("q", limit=5)
        assert FAKE_KEY not in str(excinfo.value)


def test_etsy_missing_credentials_raise(monkeypatch):
    monkeypatch.delenv(ENV_API_KEY, raising=False)
    with pytest.raises(MissingCredentialsError):
        EtsyMarketplaceProvider()


def test_etsy_secret_never_exposed(monkeypatch):
    monkeypatch.setenv(ENV_API_KEY, FAKE_KEY)
    provider = EtsyMarketplaceProvider()
    assert FAKE_KEY not in repr(provider)
    assert FAKE_KEY not in str(provider)


# ------------------------------------------------------------ research service


async def test_listing_dedupe_and_shared_listings_across_candidates():
    shared = listing_for("L1", review_count=None)
    c1 = make_candidate(["query a"])
    c2 = make_candidate(["query b"], title="Other")
    provider = FakeMarketplaceProvider(
        {"query a": [shared, listing_for("L2", review_count=None)], "query b": [shared]},
        review_counts={"L1": 40, "L2": 7},
    )
    store = ResearchStore()
    result = await run_marketplace_research([c1, c2], provider, store)

    assert result.unique_listing_count == 2
    # The shared listing was looked up once, not once per candidate/query.
    assert provider.review_calls.count("L1") == 1
    # Both candidates carry evidence for the shared listing with the SAME
    # underlying observation (identical payload hash).
    evidence = store.evidence_for_snapshot(result.snapshot.snapshot_id)
    l1_items = [e for e in evidence if e.raw_payload and e.raw_payload.get("listing_id") == "L1"]
    assert {e.candidate_id for e in l1_items} == {c1.id, c2.id}
    assert len({e.raw_payload_hash for e in l1_items}) == 1
    assert result.snapshot.status == SnapshotStatus.COMPLETE


async def test_purchase_proxy_classification_not_sales():
    candidate = make_candidate(["query a"])
    provider = FakeMarketplaceProvider(
        {"query a": [listing_for("L1", review_count=None)]}, review_counts={"L1": 55}
    )
    store = ResearchStore()
    result = await run_marketplace_research([candidate], provider, store)
    evidence = store.evidence_for_snapshot(result.snapshot.snapshot_id)

    proxy = [e for e in evidence if e.purpose == EvidencePurpose.PURCHASE]
    assert len(proxy) == 1
    item = proxy[0]
    # OBSERVED marketplace data, but explicitly a proxy signal.
    assert item.truth_class == TruthClass.OBSERVED
    assert "purchase_proxy" in item.signal_type
    assert item.raw_value == 55
    assert item.unit == "reviews"
    assert any("PURCHASE PROXY" in lim for lim in item.known_limitations)
    assert any("remain UNKNOWN" in lim for lim in item.known_limitations)
    # Nothing anywhere claims sales/revenue as a value.
    for e in evidence:
        assert "sales" not in e.signal_type
        assert "revenue" not in e.signal_type
        assert e.unit not in ("sales", "units_sold", "revenue")


async def test_reviews_never_transformed_into_exact_sales():
    """A review count of N must surface as N reviews — never as a sales figure,
    a multiplied estimate, or an ESTIMATED/INFERRED sales record."""
    candidate = make_candidate(["query a"])
    provider = FakeMarketplaceProvider(
        {"query a": [listing_for("L1", review_count=None)]}, review_counts={"L1": 100}
    )
    store = ResearchStore()
    result = await run_marketplace_research([candidate], provider, store)
    evidence = store.evidence_for_snapshot(result.snapshot.snapshot_id)
    values = [e.raw_value for e in evidence if e.raw_value is not None]
    # The only numeric values are the observed price and the raw review count.
    assert set(values) == {12.5, 100}
    assert all(
        e.truth_class in (TruthClass.OBSERVED, TruthClass.UNKNOWN) for e in evidence
    )
    summary = result.summaries[candidate.id]
    assert summary.purchase_proxy.median_review_count == 100.0
    assert not hasattr(summary.purchase_proxy, "estimated_sales")


async def test_unfetched_reviews_are_unknown_not_zero():
    candidate = make_candidate(["query a"])
    provider = FakeMarketplaceProvider(
        {"query a": [listing_for("L1", review_count=None), listing_for("L2", review_count=None)]},
        review_counts={"L1": 5, "L2": 5},
    )
    store = ResearchStore()
    result = await run_marketplace_research(
        [candidate], provider, store, max_review_lookups=1
    )
    assert result.review_lookups_performed == 1
    assert result.review_lookups_skipped == 1
    evidence = store.evidence_for_snapshot(result.snapshot.snapshot_id)
    proxies = {e.raw_payload["listing_id"]: e for e in evidence if e.purpose == EvidencePurpose.PURCHASE}
    assert proxies["L1"].truth_class == TruthClass.OBSERVED
    assert proxies["L2"].truth_class == TruthClass.UNKNOWN
    assert proxies["L2"].raw_value is None
    summary = result.summaries[candidate.id]
    assert summary.purchase_proxy.missing_review_data_count == 1


async def test_price_evidence_observed_and_missing_price_unknown():
    candidate = make_candidate(["query a"])
    provider = FakeMarketplaceProvider(
        {"query a": [listing_for("L1"), listing_for("L2", price=None, currency=None)]}
    )
    store = ResearchStore()
    result = await run_marketplace_research([candidate], provider, store, max_review_lookups=0)
    evidence = store.evidence_for_snapshot(result.snapshot.snapshot_id)
    price_items = {e.raw_payload["listing_id"]: e for e in evidence if e.purpose == EvidencePurpose.PRICE}
    assert price_items["L1"].truth_class == TruthClass.OBSERVED
    assert price_items["L1"].raw_value == 12.5
    assert price_items["L1"].unit == "USD"
    assert price_items["L2"].truth_class == TruthClass.UNKNOWN
    assert price_items["L2"].raw_value is None


# ------------------------------------------------------------- feature extraction


def test_price_quartiles_deterministic():
    cid = uuid4()
    listings = [
        listing_for(f"L{i}", price=p, seller_id=f"s{i}")
        for i, p in enumerate([5.0, 10.0, 15.0, 20.0, 25.0, 30.0, 35.0, 40.0])
    ]
    summary = summarize_price(cid, listings)
    assert summary.relevant_paid_comparable_count == 8
    assert summary.min_price == 5.0
    assert summary.p25_price == 13.75
    assert summary.median_price == 22.5
    assert summary.p75_price == 31.25
    assert summary.max_price == 40.0
    assert summary.currency == "USD"
    assert summary.insufficient_evidence is False
    # No "optimal price" output exists.
    assert not hasattr(summary, "optimal_price")
    # Deterministic: same input, same output.
    assert summarize_price(cid, listings) == summary


def test_insufficient_pricing_evidence_is_flagged():
    cid = uuid4()
    listings = [listing_for("L1", price=9.99), listing_for("L2", price=None)]
    summary = summarize_price(cid, listings)
    assert summary.relevant_paid_comparable_count == 1
    assert summary.insufficient_evidence is True
    assert summary.missing_price_data_count == 1
    empty = summarize_price(cid, [])
    assert empty.insufficient_evidence is True
    assert empty.median_price is None


def test_price_without_currency_excluded_from_stats():
    """A price whose currency Etsy did not return has no comparable unit; it
    must never mix into another currency's statistics."""
    cid = uuid4()
    listings = [
        listing_for("L1", price=10.0, currency="USD"),
        listing_for("L2", price=12.0, currency="USD"),
        listing_for("L3", price=900.0, currency=None),  # unknown unit
    ]
    summary = summarize_price(cid, listings)
    assert summary.relevant_paid_comparable_count == 2
    assert summary.max_price == 12.0  # the unknown-unit 900.0 never mixes in
    assert summary.currency == "USD"
    assert summary.mixed_currencies is False
    assert summary.missing_price_data_count == 1
    # All prices of unknown currency -> no comparables, insufficient evidence.
    only_unknown = summarize_price(cid, [listing_for("L4", price=5.0, currency=None)])
    assert only_unknown.relevant_paid_comparable_count == 0
    assert only_unknown.insufficient_evidence is True
    assert only_unknown.median_price is None


def test_mixed_currencies_not_averaged_together():
    cid = uuid4()
    listings = [
        listing_for("L1", price=10.0, currency="USD"),
        listing_for("L2", price=12.0, currency="USD"),
        listing_for("L3", price=900.0, currency="JPY"),
    ]
    summary = summarize_price(cid, listings)
    assert summary.mixed_currencies is True
    assert summary.currency == "USD"  # dominant currency only
    assert summary.relevant_paid_comparable_count == 2
    assert summary.max_price == 12.0  # the JPY price never mixes into USD stats


def test_purchase_proxy_summary_robust_and_missing_reported():
    cid = uuid4()
    listings = [
        listing_for("L1", review_count=0, seller_id="a"),
        listing_for("L2", review_count=10, seller_id="a"),
        listing_for("L3", review_count=90, seller_id="b"),
        listing_for("L4", review_count=None, seller_id="c"),
    ]
    summary = summarize_purchase_proxy(cid, listings)
    assert summary.relevant_listing_count == 4
    assert summary.unique_seller_count == 3
    assert summary.listings_with_reviews == 2
    assert summary.median_review_count == 10.0
    assert summary.review_concentration == 0.9
    assert summary.missing_review_data_count == 1


def test_competition_summary_features():
    cid = uuid4()
    now = datetime(2026, 1, 1, tzinfo=UTC)
    listings = [
        listing_for("L1", price=10.0, review_count=100, seller_id="big",
                    created_at=now - timedelta(days=400)),
        listing_for("L2", price=20.0, review_count=10, seller_id="big",
                    created_at=now - timedelta(days=200)),
        listing_for("L3", price=30.0, review_count=1, seller_id="small",
                    created_at=None),
    ]
    summary = summarize_marketplace(cid, listings, now=now).competition
    assert summary.relevant_listing_count == 3
    assert summary.unique_seller_count == 2
    assert summary.seller_concentration == round(2 / 3, 4)
    assert summary.review_burden_median == 10.0
    assert summary.price_dispersion == 0.5  # (25-15)/20
    assert summary.listing_age_days_median == 300.0
    assert summary.listings_with_age_data == 2
    assert summary.missing_data_count == 1
    # No competition score or "low competition = good" verdict exists.
    for name in ("competition_score", "opportunity", "classification"):
        assert not hasattr(summary, name)


def test_summaries_deterministic():
    cid = uuid4()
    now = datetime(2026, 1, 1, tzinfo=UTC)
    listings = [listing_for("L1"), listing_for("L2", price=30.0, review_count=3)]
    assert summarize_marketplace(cid, listings, now=now) == summarize_marketplace(
        cid, listings, now=now
    )


# ------------------------------------------------------ failure and cost control


async def test_partial_provider_failure_keeps_run_alive():
    c1 = make_candidate(["good query"])
    c2 = make_candidate(["bad query"], title="Other")
    provider = FakeMarketplaceProvider(
        {"good query": [listing_for("L1", review_count=None)]},
        review_counts={"L1": 3},
        fail_queries={"bad query"},
        error=ProviderResponseError("simulated outage"),
    )
    store = ResearchStore()
    result = await run_marketplace_research([c1, c2], provider, store)
    assert result.snapshot.status == SnapshotStatus.PARTIAL
    assert any("simulated outage" in e for e in result.provider_errors)
    assert [m for m in result.missing_queries if m.reason == MISSING_PROVIDER_ERROR]
    # The good query still produced evidence.
    assert len(result.evidence_ids_by_candidate[c1.id]) == 3


async def test_total_failure_marks_snapshot_failed_not_crash():
    candidate = make_candidate(["q1"])
    provider = FakeMarketplaceProvider(
        {}, fail_queries={"q1"}, error=ProviderAuthError("bad key")
    )
    store = ResearchStore()
    result = await run_marketplace_research([candidate], provider, store)
    assert result.snapshot.status == SnapshotStatus.FAILED
    assert result.evidence_ids_by_candidate[candidate.id] == []


async def test_rate_limit_error_reported_gracefully():
    candidate = make_candidate(["q1", "q2"])
    provider = FakeMarketplaceProvider(
        {"q2": [listing_for("L1")]},
        fail_queries={"q1"},
        error=ProviderRateLimitError("Etsy rate limit reached"),
    )
    store = ResearchStore()
    result = await run_marketplace_research([candidate], provider, store, max_review_lookups=0)
    assert result.snapshot.status == SnapshotStatus.PARTIAL
    assert any("ProviderRateLimitError" in e for e in result.provider_errors)


async def test_query_and_call_caps_enforced():
    candidate = make_candidate(["q1", "q2", "q3"])
    provider = FakeMarketplaceProvider({q: [listing_for(f"L-{q}")] for q in ("q1", "q2", "q3")})
    store = ResearchStore()
    result = await run_marketplace_research(
        [candidate], provider, store, max_queries=2, max_provider_calls=1, max_review_lookups=0
    )
    assert provider.search_calls == ["q1"]
    reasons = {m.query: m.reason for m in result.missing_queries}
    assert reasons["q3"] == MISSING_QUERY_CAP
    assert reasons["q2"] == MISSING_BUDGET
    assert result.snapshot.provider_call_count == 1


async def test_cache_avoids_repeat_provider_calls():
    candidate = make_candidate(["q1"])
    provider = FakeMarketplaceProvider(
        {"q1": [listing_for("L1", review_count=None)]}, review_counts={"L1": 8}
    )
    store = ResearchStore()
    first = await run_marketplace_research([candidate], provider, store)
    assert provider.search_calls == ["q1"]
    second = await run_marketplace_research([candidate], provider, store)
    # No new search or review calls; cached listings already carry review data.
    assert provider.search_calls == ["q1"]
    assert provider.review_calls == ["L1"]
    assert second.cached_query_count == 1
    assert second.snapshot.provider_call_count == 0
    # And the cached run still classifies review data as OBSERVED.
    evidence = store.evidence_for_snapshot(second.snapshot.snapshot_id)
    proxy = [e for e in evidence if e.purpose == EvidencePurpose.PURCHASE][0]
    assert proxy.truth_class == TruthClass.OBSERVED
    assert proxy.raw_value == 8
    assert first.snapshot.snapshot_id != second.snapshot.snapshot_id


async def test_snapshots_immutable_and_history_preserved():
    candidate = make_candidate(["q1"])
    provider = FakeMarketplaceProvider({"q1": [listing_for("L1")]})
    store = ResearchStore()
    result = await run_marketplace_research([candidate], provider, store, max_review_lookups=0)
    with pytest.raises(ImmutableEvidenceError):
        store.add_snapshot(result.snapshot)
    evidence = store.evidence_for_snapshot(result.snapshot.snapshot_id)
    with pytest.raises(ImmutableEvidenceError):
        store.add_evidence(evidence[0])
    with pytest.raises(Exception):  # frozen model: no in-place edits
        evidence[0].raw_value = 999.0


# ---------------------------------------------------------------------- endpoint


def _install_fake_provider(monkeypatch, provider: FakeMarketplaceProvider) -> None:
    monkeypatch.setitem(routes.MARKETPLACE_PROVIDERS, "etsy", lambda: provider)


def test_endpoint_runs_marketplace_research(monkeypatch):
    provider = FakeMarketplaceProvider(
        {"handmade sourdough kit": [listing_for("L1", review_count=None)]},
        review_counts={"L1": 12},
    )
    _install_fake_provider(monkeypatch, provider)
    candidate = make_candidate(["Handmade Sourdough KIT"])
    response = client.post(
        "/research/marketplace",
        json={"candidates": [json.loads(candidate.model_dump_json())]},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["snapshot"]["status"] == "COMPLETE"
    assert body["unique_listing_count"] == 1
    assert body["review_lookups_performed"] == 1
    summary = body["summaries"][0]
    assert summary["purchase_proxy"]["median_review_count"] == 12.0
    assert summary["price"]["median_price"] == 12.5
    assert summary["price"]["insufficient_evidence"] is True  # only 1 comparable
    assert summary["competition"]["relevant_listing_count"] == 1
    assert body["evidence_ids_by_candidate"][str(candidate.id)]

    # Snapshot retrieval endpoint returns the stored evidence.
    snap = client.get(f"/research/marketplace/snapshots/{body['snapshot']['snapshot_id']}")
    assert snap.status_code == 200
    assert len(snap.json()["evidence"]) == 3
    purposes = {e["purpose"] for e in snap.json()["evidence"]}
    assert purposes == {"PRICE", "PURCHASE", "COMPETITION"}


def test_endpoint_requires_exactly_one_input_source():
    assert client.post("/research/marketplace", json={}).status_code == 422
    candidate = make_candidate(["q"])
    both = {
        "candidates": [json.loads(candidate.model_dump_json())],
        "research_run_id": str(uuid4()),
    }
    assert client.post("/research/marketplace", json=both).status_code == 422


def test_endpoint_unknown_run_and_unknown_marketplace():
    missing_run = client.post(
        "/research/marketplace", json={"research_run_id": str(uuid4())}
    )
    assert missing_run.status_code == 404
    candidate = make_candidate(["q"])
    unknown = client.post(
        "/research/marketplace",
        json={
            "candidates": [json.loads(candidate.model_dump_json())],
            "marketplace": "amazon",
        },
    )
    assert unknown.status_code == 422


def test_endpoint_missing_credentials_returns_503(monkeypatch):
    monkeypatch.delenv(ENV_API_KEY, raising=False)
    candidate = make_candidate(["q"])
    response = client.post(
        "/research/marketplace",
        json={"candidates": [json.loads(candidate.model_dump_json())]},
    )
    assert response.status_code == 503
    assert "ETSY_API_KEY" in response.json()["detail"]


def test_endpoint_response_never_contains_secret(monkeypatch):
    monkeypatch.setenv(ENV_API_KEY, FAKE_KEY)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"count": 1, "results": [etsy_listing_payload()]})

    monkeypatch.setitem(
        routes.MARKETPLACE_PROVIDERS,
        "etsy",
        lambda: EtsyMarketplaceProvider(transport=httpx.MockTransport(handler)),
    )
    candidate = make_candidate(["q"])
    response = client.post(
        "/research/marketplace",
        json={
            "candidates": [json.loads(candidate.model_dump_json())],
            "max_review_lookups": 0,
        },
    )
    assert response.status_code == 200
    assert FAKE_KEY not in response.text
