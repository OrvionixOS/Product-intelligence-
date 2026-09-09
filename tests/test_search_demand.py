import json
from datetime import UTC, datetime
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
    KeywordDemandMetrics,
    MissingCredentialsError,
    MonthlySearchVolume,
    ProviderAuthError,
    ProviderRateLimitError,
    ProviderResponseError,
    ProviderTimeoutError,
    SearchDemandBatchResult,
    SearchDemandProvider,
)
from app.providers.dataforseo import DataForSeoSearchDemandProvider
from app.services.search_demand import (
    MISSING_BUDGET,
    MISSING_NOT_RETURNED,
    plan_queries,
    run_search_demand_research,
)
from app.services.search_demand_features import summarize_search_demand
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
        search_queries=queries,
        marketplace_queries=["m"],
        content_queries=["c"],
        generation_reason="A hypothesis",
    )


def metrics_for(keyword: str, volume: int | None = 100, **overrides: Any) -> KeywordDemandMetrics:
    payload: dict[str, Any] = dict(
        keyword=keyword,
        search_volume=volume,
        monthly_history=(MonthlySearchVolume(2025, 7, volume),) if volume is not None else (),
        competition="LOW",
        competition_index=20,
        cpc=0.5,
        retrieved_at=datetime.now(UTC),
    )
    payload.update(overrides)
    return KeywordDemandMetrics(**payload)


class FakeSearchProvider(SearchDemandProvider):
    name = "fake"
    max_keywords_per_request = 2

    def __init__(
        self,
        metrics_map: dict[str, KeywordDemandMetrics] | None = None,
        fail_on_calls: set[int] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.metrics_map = metrics_map or {}
        self.fail_on_calls = fail_on_calls or set()
        self.error = error or ProviderResponseError("simulated provider failure")
        self.calls = 0
        self.batches_seen: list[list[str]] = []

    async def fetch_keyword_metrics(
        self, keywords: list[str], location: str, language: str
    ) -> SearchDemandBatchResult:
        self.calls += 1
        self.batches_seen.append(list(keywords))
        if self.calls in self.fail_on_calls:
            raise self.error
        return SearchDemandBatchResult(
            provider=self.name,
            metrics=[self.metrics_map[k] for k in keywords if k in self.metrics_map],
            retrieved_at=datetime.now(UTC),
            collection_method="official_api",
            source_reference="/fake",
            call_count=1,
            cost=0.05,
        )


# ------------------------------------------------------------- query planning


def test_plan_queries_deduplicates_across_candidates_and_keeps_mapping():
    a = make_candidate(["Shared  Keyword", "only a"])
    b = make_candidate(["shared keyword", "only b"], title="Other")
    plan = plan_queries([a, b])
    assert plan.keywords == ["shared keyword", "only a", "only b"]
    assert plan.keyword_to_candidates["shared keyword"] == [a.id, b.id]
    assert plan.keyword_to_candidates["only b"] == [b.id]


# ----------------------------------------------------- DataForSEO normalization


DFS_BODY = {
    "version": "0.1.20250815",
    "status_code": 20000,
    "cost": 0.075,
    "tasks": [
        {
            "status_code": 20000,
            "status_message": "Ok.",
            "result": [
                {
                    "keyword": "sourdough baking pdf guide",
                    "location_code": 2840,
                    "language_code": "en",
                    "search_volume": 720,
                    "competition": "LOW",
                    "competition_index": 22,
                    "cpc": 0.42,
                    "low_top_of_page_bid": 0.21,
                    "high_top_of_page_bid": 0.98,
                    "monthly_searches": [
                        {"year": 2025, "month": 7, "search_volume": 700},
                        {"year": 2025, "month": 8, "search_volume": 740},
                    ],
                },
                {
                    "keyword": "sourdough baking digital download",
                    "search_volume": None,
                    "competition": None,
                    "competition_index": None,
                    "cpc": None,
                    "monthly_searches": None,
                },
            ],
        }
    ],
}


def dfs_provider(handler, monkeypatch) -> DataForSeoSearchDemandProvider:
    monkeypatch.setenv("DATAFORSEO_LOGIN", "test-login")
    monkeypatch.setenv("DATAFORSEO_PASSWORD", "test-secret-password")
    return DataForSeoSearchDemandProvider(transport=httpx.MockTransport(handler))


async def test_dataforseo_normalizes_returned_fields(monkeypatch):
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content)
        captured["auth"] = request.headers.get("authorization", "")
        return httpx.Response(200, json=DFS_BODY)

    provider = dfs_provider(handler, monkeypatch)
    result = await provider.fetch_keyword_metrics(
        ["sourdough baking pdf guide", "sourdough baking digital download"], "US", "en"
    )

    assert captured["payload"] == [
        {
            "keywords": ["sourdough baking pdf guide", "sourdough baking digital download"],
            "location_name": "United States",
            "language_code": "en",
        }
    ]
    assert result.provider == "dataforseo"
    assert result.provider_version == "0.1.20250815"
    assert result.cost == 0.075
    assert result.call_count == 1

    full = next(m for m in result.metrics if m.keyword == "sourdough baking pdf guide")
    assert full.search_volume == 720
    assert full.competition == "LOW"
    assert full.competition_index == 22
    assert full.cpc == 0.42
    assert full.low_top_of_page_bid == 0.21
    assert full.high_top_of_page_bid == 0.98
    assert full.location == "2840"
    assert full.language == "en"
    assert [h.search_volume for h in full.monthly_history] == [700, 740]

    sparse = next(m for m in result.metrics if m.keyword == "sourdough baking digital download")
    assert sparse.search_volume is None
    assert sparse.competition is None
    assert sparse.cpc is None
    assert sparse.monthly_history == ()


async def test_dataforseo_error_mapping(monkeypatch):
    for status, exc_type in ((401, ProviderAuthError), (429, ProviderRateLimitError), (500, ProviderResponseError)):
        provider = dfs_provider(lambda r, s=status: httpx.Response(s), monkeypatch)
        with pytest.raises(exc_type):
            await provider.fetch_keyword_metrics(["kw"], "US", "en")

    def timeout_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("slow")

    provider = dfs_provider(timeout_handler, monkeypatch)
    with pytest.raises(ProviderTimeoutError):
        await provider.fetch_keyword_metrics(["kw"], "US", "en")


async def test_dataforseo_partial_task_failure_reports_errors(monkeypatch):
    body = {
        "version": "0.1",
        "cost": 0.01,
        "tasks": [
            {"status_code": 40501, "status_message": "Invalid Field."},
            DFS_BODY["tasks"][0],
        ],
    }
    provider = dfs_provider(lambda r: httpx.Response(200, json=body), monkeypatch)
    result = await provider.fetch_keyword_metrics(["sourdough baking pdf guide"], "US", "en")
    assert len(result.metrics) == 2
    assert any("40501" in e for e in result.errors)


def test_missing_credentials_raise(monkeypatch):
    monkeypatch.delenv("DATAFORSEO_LOGIN", raising=False)
    monkeypatch.delenv("DATAFORSEO_PASSWORD", raising=False)
    with pytest.raises(MissingCredentialsError):
        DataForSeoSearchDemandProvider()


def test_credentials_never_exposed(monkeypatch):
    secret = "extremely-secret-password"
    monkeypatch.setenv("DATAFORSEO_LOGIN", "login")
    monkeypatch.setenv("DATAFORSEO_PASSWORD", secret)
    provider = DataForSeoSearchDemandProvider(
        transport=httpx.MockTransport(lambda r: httpx.Response(401))
    )
    assert secret not in repr(provider)
    assert secret not in str(vars(provider).get("_base_url", ""))


async def test_auth_error_message_contains_no_secret(monkeypatch):
    secret = "extremely-secret-password"
    monkeypatch.setenv("DATAFORSEO_LOGIN", "login")
    monkeypatch.setenv("DATAFORSEO_PASSWORD", secret)
    provider = DataForSeoSearchDemandProvider(
        transport=httpx.MockTransport(lambda r: httpx.Response(401))
    )
    with pytest.raises(ProviderAuthError) as exc_info:
        await provider.fetch_keyword_metrics(["kw"], "US", "en")
    assert secret not in str(exc_info.value)


# ------------------------------------------------------------ research service


async def test_shared_keyword_supports_multiple_candidates():
    a = make_candidate(["shared keyword"])
    b = make_candidate(["shared keyword"], title="Other")
    provider = FakeSearchProvider({"shared keyword": metrics_for("shared keyword", 500)})
    store = ResearchStore()

    result = await run_search_demand_research([a, b], provider, store)

    assert provider.calls == 1  # deduplicated: one keyword, one call
    assert len(result.evidence_ids_by_candidate[a.id]) == 1
    assert len(result.evidence_ids_by_candidate[b.id]) == 1
    ev_a = store.get_evidence(result.evidence_ids_by_candidate[a.id][0])
    ev_b = store.get_evidence(result.evidence_ids_by_candidate[b.id][0])
    assert ev_a.id != ev_b.id
    assert ev_a.raw_payload_hash == ev_b.raw_payload_hash
    assert ev_a.candidate_id == a.id and ev_b.candidate_id == b.id


async def test_search_demand_is_not_purchase_evidence():
    candidate = make_candidate(["kw one"])
    provider = FakeSearchProvider({"kw one": metrics_for("kw one")})
    store = ResearchStore()
    result = await run_search_demand_research([candidate], provider, store)

    items = store.evidence_for_snapshot(result.snapshot.snapshot_id)
    assert items
    for item in items:
        assert item.purpose == EvidencePurpose.SEARCH_DEMAND
        assert item.purpose != EvidencePurpose.PURCHASE
        assert item.truth_class == TruthClass.OBSERVED
        assert any("not purchases" in lim for lim in item.known_limitations)
        assert item.unit == "searches_per_month"


async def test_missing_metrics_stay_unknown_not_invented():
    candidate = make_candidate(["kw one", "kw two"])
    provider = FakeSearchProvider(
        {
            "kw one": metrics_for("kw one", volume=None, competition=None, cpc=None, competition_index=None),
        }
    )
    store = ResearchStore()
    result = await run_search_demand_research([candidate], provider, store)

    item = store.get_evidence(result.evidence_ids_by_candidate[candidate.id][0])
    assert item.raw_value is None
    assert item.normalized_value is None
    assert item.truth_class == TruthClass.UNKNOWN

    missing = {m.keyword: m.reason for m in result.missing_keywords}
    assert missing == {"kw two": MISSING_NOT_RETURNED}
    summary = result.summaries[candidate.id]
    assert summary.relevant_keyword_count == 0
    assert summary.median_search_volume is None
    assert summary.missing_data_count == 1


async def test_partial_provider_failure_keeps_run_alive():
    candidate = make_candidate(["kw a", "kw b", "kw c", "kw d"])
    provider = FakeSearchProvider(
        {k: metrics_for(k) for k in ("kw a", "kw b", "kw c", "kw d")},
        fail_on_calls={2},
    )
    store = ResearchStore()
    result = await run_search_demand_research([candidate], provider, store)

    assert provider.calls == 2  # batch size 2 -> 2 calls, second failed
    assert result.snapshot.status == SnapshotStatus.PARTIAL
    assert len(result.evidence_ids_by_candidate[candidate.id]) == 2
    assert result.provider_errors
    failed_keywords = {m.keyword for m in result.missing_keywords}
    assert failed_keywords == {"kw c", "kw d"}


async def test_budget_exhaustion_returns_missing_evidence_explicitly():
    candidate = make_candidate(["kw a", "kw b", "kw c"])
    provider = FakeSearchProvider({k: metrics_for(k) for k in ("kw a", "kw b", "kw c")})
    store = ResearchStore()
    result = await run_search_demand_research(
        [candidate], provider, store, max_provider_calls=1
    )

    assert provider.calls == 1
    assert result.snapshot.provider_call_count == 1
    budget_missing = [m for m in result.missing_keywords if m.reason == MISSING_BUDGET]
    assert [m.keyword for m in budget_missing] == ["kw c"]
    assert result.snapshot.status == SnapshotStatus.PARTIAL


async def test_snapshots_are_immutable_and_new_runs_create_new_snapshots():
    candidate = make_candidate(["kw one"])
    provider = FakeSearchProvider({"kw one": metrics_for("kw one", 300)})
    store = ResearchStore()

    first = await run_search_demand_research([candidate], provider, store)
    first_evidence = store.evidence_for_snapshot(first.snapshot.snapshot_id)

    second = await run_search_demand_research([candidate], provider, store)
    assert second.snapshot.snapshot_id != first.snapshot.snapshot_id
    # First snapshot's evidence is untouched by the second run.
    assert store.evidence_for_snapshot(first.snapshot.snapshot_id) == first_evidence

    # Evidence records and snapshots reject mutation and re-insertion.
    with pytest.raises(Exception):
        first_evidence[0].raw_value = 999999
    with pytest.raises(ImmutableEvidenceError):
        store.add_snapshot(first.snapshot)
    with pytest.raises(ImmutableEvidenceError):
        store.add_evidence(first_evidence[0])


async def test_cache_avoids_repeat_provider_calls():
    candidate = make_candidate(["kw one"])
    provider = FakeSearchProvider({"kw one": metrics_for("kw one", 300)})
    store = ResearchStore()

    first = await run_search_demand_research([candidate], provider, store)
    second = await run_search_demand_research([candidate], provider, store)

    assert provider.calls == 1  # second run served from cache
    assert first.cached_keyword_count == 0
    assert second.cached_keyword_count == 1
    assert second.snapshot.provider_call_count == 0
    assert len(second.evidence_ids_by_candidate[candidate.id]) == 1


# --------------------------------------------------------------- summaries


def test_summary_is_deterministic_and_robust():
    cid = uuid4()
    metrics = [
        metrics_for("a", 100, cpc=0.2, competition="LOW", competition_index=10),
        metrics_for("b", 400, cpc=0.6, competition="HIGH", competition_index=80),
        metrics_for("c", None, cpc=None, competition=None, competition_index=None),
    ]
    s1 = summarize_search_demand(cid, metrics)
    s2 = summarize_search_demand(cid, metrics)
    assert s1 == s2  # deterministic

    assert s1.relevant_keyword_count == 2
    assert s1.median_search_volume == 250.0
    assert s1.max_search_volume == 400
    assert s1.total_search_volume == 500
    assert s1.median_cpc == 0.4
    assert s1.competition_counts == {"LOW": 1, "HIGH": 1}
    assert s1.median_competition_index == 45.0
    assert s1.missing_data_count == 1
    assert s1.search_demand_dimension == 39.99  # 100*log10(251)/6 rounded
    assert s1.dimension_version == "search_demand_dimension_v1"


def test_summary_with_no_metrics_reports_missing_not_zero_demand():
    summary = summarize_search_demand(uuid4(), [])
    assert summary.relevant_keyword_count == 0
    assert summary.median_search_volume is None
    assert summary.search_demand_dimension is None


# ---------------------------------------------------------------- endpoints


def fake_provider_cls(metrics_map: dict[str, KeywordDemandMetrics]):
    class _Cls(FakeSearchProvider):
        def __init__(self) -> None:
            super().__init__(metrics_map)

    return _Cls


def test_endpoint_runs_research_with_inline_candidates(monkeypatch):
    candidate = make_candidate(["kw one", "kw two"])
    monkeypatch.setitem(
        routes.SEARCH_DEMAND_PROVIDERS,
        "dataforseo",
        fake_provider_cls(
            {"kw one": metrics_for("kw one", 900), "kw two": metrics_for("kw two", 100)}
        ),
    )
    response = client.post(
        "/research/search-demand",
        json={"candidates": [json.loads(candidate.model_dump_json())], "location": "US"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["snapshot"]["status"] == "COMPLETE"
    assert body["snapshot"]["provider_call_count"] == 1
    assert body["snapshot"]["normalization_version"] == "search_demand_norm_v1"
    assert len(body["summaries"]) == 1
    assert body["summaries"][0]["relevant_keyword_count"] == 2
    assert body["evidence_ids_by_candidate"][str(candidate.id)]
    assert body["provider_errors"] == []

    # Stored snapshot is retrievable with its evidence.
    snap_id = body["snapshot"]["snapshot_id"]
    stored = client.get(f"/research/search-demand/snapshots/{snap_id}")
    assert stored.status_code == 200
    assert len(stored.json()["evidence"]) == 2
    assert stored.json()["evidence"][0]["purpose"] == "SEARCH_DEMAND"


def test_endpoint_accepts_research_run_id(monkeypatch):
    discovery = client.post("/candidates/discover", json={"seed_keyword": "gardening"})
    assert discovery.status_code == 200
    run_id = discovery.json()["research_run_id"]

    monkeypatch.setitem(
        routes.SEARCH_DEMAND_PROVIDERS, "dataforseo", fake_provider_cls({})
    )
    response = client.post(
        "/research/search-demand", json={"research_run_id": run_id, "max_provider_calls": 0}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["snapshot"]["research_run_id"] == run_id
    assert body["snapshot"]["provider_call_count"] == 0
    assert body["missing_keywords"]  # budget 0 -> everything explicitly missing


def test_endpoint_requires_exactly_one_input_source():
    assert (
        client.post("/research/search-demand", json={"location": "US"}).status_code == 422
    )
    candidate = json.loads(make_candidate(["kw"]).model_dump_json())
    assert (
        client.post(
            "/research/search-demand",
            json={"candidates": [candidate], "research_run_id": str(uuid4())},
        ).status_code
        == 422
    )


def test_endpoint_unknown_run_and_unknown_provider():
    assert (
        client.post(
            "/research/search-demand", json={"research_run_id": str(uuid4())}
        ).status_code
        == 404
    )
    candidate = json.loads(make_candidate(["kw"]).model_dump_json())
    assert (
        client.post(
            "/research/search-demand",
            json={"candidates": [candidate], "provider": "googleads"},
        ).status_code
        == 422
    )


def test_endpoint_missing_credentials_returns_503(monkeypatch):
    monkeypatch.delenv("DATAFORSEO_LOGIN", raising=False)
    monkeypatch.delenv("DATAFORSEO_PASSWORD", raising=False)
    candidate = json.loads(make_candidate(["kw"]).model_dump_json())
    response = client.post("/research/search-demand", json={"candidates": [candidate]})
    assert response.status_code == 503
    assert "DATAFORSEO_LOGIN" in response.json()["detail"]


def test_endpoint_response_never_contains_credentials(monkeypatch):
    secret = "endpoint-secret-password"
    monkeypatch.setenv("DATAFORSEO_LOGIN", "login")
    monkeypatch.setenv("DATAFORSEO_PASSWORD", secret)
    monkeypatch.setitem(
        routes.SEARCH_DEMAND_PROVIDERS,
        "dataforseo",
        fake_provider_cls({"kw": metrics_for("kw", 50)}),
    )
    candidate = json.loads(make_candidate(["kw"]).model_dump_json())
    response = client.post("/research/search-demand", json={"candidates": [candidate]})
    assert response.status_code == 200
    assert secret not in response.text
    assert "Authorization" not in response.text
