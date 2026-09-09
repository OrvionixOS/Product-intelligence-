from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.api.routes import get_candidate_provider
from app.domain.enums import CandidateStatus, ProductFormat
from app.domain.models import Candidate
from app.main import app
from app.providers.llm import (
    CandidateGenerationError,
    CandidateGenerationProvider,
    TemplateCandidateProvider,
)
from app.services.candidate_dedupe import dedupe_candidates, similarity
from app.services.candidate_discovery import (
    InvalidSeedKeywordError,
    discover_candidates,
    normalize_raw_candidate,
    normalize_seed_keyword,
)
from app.services.candidate_validation import resolve_format, validate_candidate_format


def make_candidate(**overrides: Any) -> Candidate:
    payload: dict[str, Any] = dict(
        seed_keyword="sourdough baking",
        title="The Sourdough Baking Getting-Started Guide",
        problem="Beginners face scattered, contradictory advice about starters and hydration.",
        target_buyer="Complete beginners exploring sourdough baking",
        proposed_format=ProductFormat.PDF_GUIDE,
        buyer_outcome="A clear starting path without weeks of research.",
        search_queries=["sourdough baking pdf guide"],
        marketplace_queries=["sourdough baking guide"],
        content_queries=["sourdough baking for beginners"],
        generation_reason="Beginner segments often prefer a condensed guide.",
    )
    payload.update(overrides)
    return Candidate(**payload)


class FakeProvider(CandidateGenerationProvider):
    name = "fake"

    def __init__(self, raw: list[dict[str, Any]]):
        self.raw = raw

    async def generate(self, seed_keyword: str, count: int) -> list[dict[str, Any]]:
        return self.raw


class FailingProvider(CandidateGenerationProvider):
    name = "failing"

    async def generate(self, seed_keyword: str, count: int) -> list[dict[str, Any]]:
        raise RuntimeError("upstream LLM unavailable")


def raw_candidate(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "title": f"Unique Candidate {uuid4().hex[:8]}",
        "problem": f"A distinct problem statement {uuid4().hex[:8]}.",
        "target_buyer": "Some buyer",
        "proposed_format": "PDF_GUIDE",
        "buyer_outcome": "Some outcome",
        "generation_reason": "Pattern hypothesis",
        "search_queries": ["q1"],
        "marketplace_queries": ["q2"],
        "content_queries": ["q3"],
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------- data model


def test_candidate_defaults_to_unresearched():
    candidate = make_candidate()
    assert candidate.status == CandidateStatus.UNRESEARCHED
    assert candidate.id is not None


def test_candidate_requires_supported_format_enum():
    with pytest.raises(ValueError):
        make_candidate(proposed_format="SAAS_APP")


# ------------------------------------------------------------- seed keyword


def test_seed_keyword_is_normalized():
    assert normalize_seed_keyword("  Sourdough   Baking ") == "sourdough baking"


@pytest.mark.parametrize("bad_seed", ["", " ", "a", "!!!", "x" * 200])
def test_invalid_seed_keywords_are_rejected(bad_seed: str):
    with pytest.raises(InvalidSeedKeywordError):
        normalize_seed_keyword(bad_seed)


# -------------------------------------------------------------- normalization


def test_normalize_raw_candidate_cleans_text_and_queries():
    raw = raw_candidate(
        title="  Messy   Title ",
        search_queries=["  Query One ", "query one", "", 42, "query two"],
    )
    normalized = normalize_raw_candidate(raw, "sourdough baking")
    assert normalized["title"] == "Messy Title"
    assert normalized["search_queries"] == ["query one", "query two"]
    assert normalized["seed_keyword"] == "sourdough baking"


# ------------------------------------------------------------------- dedupe


def test_near_duplicate_titles_are_removed():
    a = make_candidate()
    b = make_candidate(
        title="Sourdough Baking Getting Started Guide",
        problem="Beginners face scattered and contradictory advice about starters.",
    )
    kept, removed = dedupe_candidates([a, b])
    assert kept == [a]
    assert removed == [b]


def test_distinct_candidates_survive_dedupe():
    a = make_candidate()
    b = make_candidate(
        title="Sourdough Baking Cost Tracker Spreadsheet",
        problem="Flour, tools, and classes add up and overspending is noticed too late.",
        proposed_format=ProductFormat.SPREADSHEET_TOOL,
    )
    kept, removed = dedupe_candidates([a, b])
    assert kept == [a, b]
    assert removed == []


def test_similarity_ignores_shared_seed_keyword_tokens():
    a = make_candidate()
    b = make_candidate(
        title="Sourdough Baking Inventory CSV",
        problem="Cataloging equipment in ad-hoc lists makes it unsearchable.",
        proposed_format=ProductFormat.DATA_TEMPLATE,
    )
    assert similarity(a, b) < 0.3


# --------------------------------------------------------- format validation


def test_supported_format_aliases_resolve():
    assert resolve_format("pdf guide") == ProductFormat.PDF_GUIDE
    assert resolve_format("XLSX tool") == ProductFormat.SPREADSHEET_TOOL
    assert resolve_format("printables") == ProductFormat.PRINTABLE_BUNDLE
    assert resolve_format("TEMPLATE_PACK") == ProductFormat.TEMPLATE_PACK


@pytest.mark.parametrize(
    "bad_format",
    ["saas", "micro-app", "coaching program", "community", "video course", "custom software", None],
)
def test_unsupported_formats_are_rejected(bad_format: Any):
    fmt, reason = validate_candidate_format(raw_candidate(proposed_format=bad_format))
    assert fmt is None
    assert reason is not None


def test_unsupported_signal_in_title_is_rejected():
    fmt, reason = validate_candidate_format(
        raw_candidate(title="Sourdough SaaS Dashboard", proposed_format="PDF_GUIDE")
    )
    assert fmt is None
    assert "saas" in reason


def test_seed_keyword_terms_are_exempt_from_signal_scan():
    fmt, reason = validate_candidate_format(
        raw_candidate(title="Video Editing Starter Guide", proposed_format="PDF_GUIDE"),
        seed_keyword="video editing",
    )
    assert fmt == ProductFormat.PDF_GUIDE
    assert reason is None


# ---------------------------------------------------------- discovery service


async def test_discovery_returns_about_twenty_unresearched_candidates():
    result = await discover_candidates("sourdough baking", TemplateCandidateProvider())
    assert 15 <= len(result.candidates) <= 20
    for candidate in result.candidates:
        assert candidate.status == CandidateStatus.UNRESEARCHED
        assert candidate.seed_keyword == "sourdough baking"
        assert candidate.title
        assert candidate.problem
        assert candidate.target_buyer
        assert candidate.buyer_outcome
        assert candidate.generation_reason
        assert candidate.search_queries
        assert candidate.marketplace_queries
        assert candidate.content_queries
        assert isinstance(candidate.proposed_format, ProductFormat)


async def test_discovery_filters_unsupported_and_malformed_candidates():
    raw = [
        raw_candidate(),
        raw_candidate(proposed_format="saas"),
        raw_candidate(title=""),
        "not a dict",
    ]
    result = await discover_candidates("gardening", FakeProvider(raw))
    assert len(result.candidates) == 1
    assert len(result.rejected) == 3
    reasons = " ".join(r.reason for r in result.rejected)
    assert "unsupported_format" in reasons
    assert "missing_fields" in reasons
    assert "malformed_candidate" in reasons


async def test_discovery_wraps_provider_failures():
    with pytest.raises(CandidateGenerationError):
        await discover_candidates("gardening", FailingProvider())


async def test_discovery_rejects_invalid_seed():
    with pytest.raises(InvalidSeedKeywordError):
        await discover_candidates("  ", TemplateCandidateProvider())


# ------------------------------------------------------------------ endpoint


client = TestClient(app)


def test_discover_endpoint_returns_structured_candidates():
    response = client.post(
        "/candidates/discover", json={"seed_keyword": "Sourdough Baking"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["seed_keyword"] == "sourdough baking"
    assert 15 <= len(body["candidates"]) <= 20
    first = body["candidates"][0]
    for field in (
        "id",
        "seed_keyword",
        "title",
        "problem",
        "target_buyer",
        "proposed_format",
        "buyer_outcome",
        "search_queries",
        "marketplace_queries",
        "content_queries",
        "generation_reason",
        "status",
    ):
        assert field in first
    assert first["status"] == "UNRESEARCHED"


def test_discover_endpoint_validates_seed_keyword():
    response = client.post("/candidates/discover", json={"seed_keyword": ""})
    assert response.status_code == 422


def test_discover_endpoint_maps_provider_failure_to_502():
    app.dependency_overrides[get_candidate_provider] = FailingProvider
    try:
        response = client.post(
            "/candidates/discover", json={"seed_keyword": "gardening"}
        )
    finally:
        app.dependency_overrides.pop(get_candidate_provider, None)
    assert response.status_code == 502


def test_discover_endpoint_respects_target_count():
    response = client.post(
        "/candidates/discover",
        json={"seed_keyword": "gardening", "target_count": 5},
    )
    assert response.status_code == 200
    assert len(response.json()["candidates"]) == 5
