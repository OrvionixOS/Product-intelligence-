"""Milestone 7A tests: the end-to-end product workflow.

No network, no live API calls, no new provider.

Four rules carry this slice.

1. Scoring happens before product generation. 4C and the content chain run
   only on a candidate that carries an actual score.

2. A blocked score is a gate, not a low score. A candidate that could not be
   scored is unmeasured, and the refusal says which evidence was missing.

3. Contextual evidence is surfaced beside a score, never inside one.

4. Preliminary triage never crosses into Step 8, and candidate/run scope is
   preserved at every stage.
"""

import ast
import random
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from app.domain.enums import ProductFormat
from app.domain.models import Candidate
from app.providers.base import (
    KeywordDemandMetrics,
    SearchDemandBatchResult,
    SearchDemandProvider,
)
from app.services.deep_research import DeepResearchState
from app.services.evidence_confidence import EcsDimension
from app.services.opportunity_scoring import ScoringState, not_scored
from app.services.product_workflow import (
    CONTEXT_DIMENSIONS,
    GATE_INSUFFICIENT_EVIDENCE,
    GATE_NOT_SCORED,
    LIMITATIONS,
    WORKFLOW_VERSION,
    CandidateWorkflowResult,
    WorkflowGateError,
    WorkflowStage,
    derive_content_plan,
    require_scored_candidate,
    run_deep_research_and_score,
    unscoreable_reason,
    workflow_state,
)
from app.storage.memory import ResearchStore

from tests.test_deep_collection import (
    TEN_LISTINGS,
    TEN_VIDEOS,
    FakeContent,
    FakeMarketplace,
)

MODULE_PATH = Path("app/services/product_workflow.py")
QUERY = "sourdough guide"


# ----------------------------------------------------------------- fixtures


class FakeSearchDemand(SearchDemandProvider):
    """Returns a real measurement for every keyword asked for."""

    name = "fake-search"
    max_keywords_per_request = 50

    def __init__(self, volume: int = 5000, measured: bool = True) -> None:
        self.volume = volume
        self.measured = measured
        self.calls = 0

    async def fetch_keyword_metrics(
        self, keywords: list[str], location: str, language: str
    ) -> SearchDemandBatchResult:
        self.calls += 1
        return SearchDemandBatchResult(
            provider=self.name,
            metrics=[
                KeywordDemandMetrics(
                    keyword=keyword,
                    search_volume=(
                        self.volume + index * 250 if self.measured else None
                    ),
                    retrieved_at=datetime.now(UTC),
                )
                for index, keyword in enumerate(keywords)
            ],
            retrieved_at=datetime.now(UTC),
            collection_method="official_api",
            source_reference="/fake/keywords",
            call_count=1,
        )


def workflow_candidate(title: str = "Sourdough Guide") -> Candidate:
    return Candidate(
        seed_keyword="sourdough baking",
        title=title,
        problem="A problem statement.",
        target_buyer="A buyer",
        proposed_format=ProductFormat.PDF_GUIDE,
        buyer_outcome="An outcome",
        search_queries=["sourdough starter", "sourdough schedule", "sourdough loaf"],
        marketplace_queries=[QUERY],
        content_queries=[QUERY],
        generation_reason="A hypothesis",
    )


async def full_run(candidates=None, *, search=True, store=None, run_id=None):
    """A complete deep pass with every capability available."""
    store = store or ResearchStore()
    candidates = candidates or [workflow_candidate()]
    run_id = run_id or uuid4()
    result = await run_deep_research_and_score(
        candidates=candidates,
        store=store,
        research_run_id=run_id,
        search_demand_provider=FakeSearchDemand() if search else None,
        marketplace_provider=FakeMarketplace(TEN_LISTINGS),
        public_content_provider=FakeContent(TEN_VIDEOS),
    )
    return store, run_id, result


# -------------------------------------------------- the end-to-end happy path


async def test_a_full_pass_scores_the_candidate_and_opens_the_product_gate():
    store, run_id, result = await full_run()
    candidate = result.candidates[0]
    assert candidate.scoring.scoring_state in (
        ScoringState.SCORED_UNCLASSIFIED,
        ScoringState.CLASSIFIED,
    )
    assert candidate.scoring.opportunity_score is not None
    assert candidate.scoring.evidence_confidence is not None
    assert candidate.stage is WorkflowStage.SCORED
    assert candidate.may_generate_product
    assert result.scored_candidate_ids == (candidate.candidate_id,)
    # The gate opens without raising.
    assert require_scored_candidate(
        store, candidate.candidate_id, run_id
    ) is not None


async def test_the_run_carries_every_component_version_it_used():
    _, _, result = await full_run()
    assert result.component_versions == {
        "workflow": WORKFLOW_VERSION,
        "deep_pass_caps": "deep_pass_caps_v1",
        "deep_research": "deep_research_v1",
        "opportunity_score": "opportunity_score_v1",
        "classification_thresholds": "classification_thresholds_v1",
    }
    assert result.version == WORKFLOW_VERSION


async def test_the_scoring_record_is_persisted_for_every_candidate():
    store, run_id, result = await full_run(
        [workflow_candidate("A"), workflow_candidate("B")]
    )
    assert len(result.candidates) == 2
    for candidate in result.candidates:
        stored = store.latest_scoring_result(candidate.candidate_id, run_id)
        assert stored is candidate.scoring


async def test_content_intelligence_runs_end_to_end_on_a_scored_candidate():
    store, run_id, result = await full_run()
    candidate = result.candidates[0]
    plan = derive_content_plan(store, candidate.candidate_id, run_id)
    assert plan.candidate_id == candidate.candidate_id
    assert plan.research_run_id == run_id
    # Each stage produced its own result, and 5C consumed 5B.
    assert plan.outliers is not None
    assert plan.patterns is not None
    assert plan.experiments is not None


# ------------------------------------------------ scoring gates the product


async def test_an_unscoreable_candidate_cannot_reach_product_generation():
    """No search-demand provider, so D1 is unscoreable and no POS exists."""
    store, run_id, result = await full_run(search=False)
    candidate = result.candidates[0]
    assert candidate.scoring.scoring_state is ScoringState.INSUFFICIENT_EVIDENCE
    assert candidate.scoring.opportunity_score is None
    assert candidate.stage is WorkflowStage.DEEP_RESEARCH
    assert not candidate.may_generate_product

    with pytest.raises(WorkflowGateError) as raised:
        require_scored_candidate(store, candidate.candidate_id, run_id)
    assert raised.value.reason == GATE_INSUFFICIENT_EVIDENCE
    # The refusal names the missing evidence and denies it is a low score.
    assert EcsDimension.SEARCH_DEMAND.value in raised.value.detail
    assert "not a low score" in raised.value.detail


async def test_the_content_chain_is_gated_by_the_same_rule():
    store, run_id, result = await full_run(search=False)
    with pytest.raises(WorkflowGateError) as raised:
        derive_content_plan(store, result.candidates[0].candidate_id, run_id)
    assert raised.value.reason == GATE_INSUFFICIENT_EVIDENCE


def test_a_candidate_with_no_scoring_record_at_all_is_refused():
    store = ResearchStore()
    with pytest.raises(WorkflowGateError) as raised:
        require_scored_candidate(store, uuid4(), uuid4())
    assert raised.value.reason == GATE_NOT_SCORED
    assert "run deep research and scoring first" in raised.value.detail


def test_an_explicit_not_scored_record_is_refused_too():
    """NOT_SCORED is the absence of an attempt, not a pass."""
    store = ResearchStore()
    candidate_id, run_id = uuid4(), uuid4()
    store.add_scoring_result(not_scored(candidate_id, run_id))
    with pytest.raises(WorkflowGateError) as raised:
        require_scored_candidate(store, candidate_id, run_id)
    assert raised.value.reason == GATE_NOT_SCORED


async def test_a_below_floor_score_still_opens_the_product_gate():
    """SCORED_UNCLASSIFIED has a score. Only the colour is withheld."""
    from app.services.opportunity_scoring import ScoringResult
    from app.services.opportunity_scoring import (
        STATE_BOUNDARIES as SCORING_BOUNDARIES,
    )

    store = ResearchStore()
    candidate_id, run_id = uuid4(), uuid4()
    store.add_scoring_result(
        ScoringResult(
            candidate_id=candidate_id,
            research_run_id=run_id,
            scoring_state=ScoringState.SCORED_UNCLASSIFIED,
            state_boundary=SCORING_BOUNDARIES[ScoringState.SCORED_UNCLASSIFIED],
            opportunity_score=71.0,
            evidence_confidence=22.0,
            classification=None,
            sub_scores=(),
            excluded_dimensions=(),
            kill_rules_triggered=(),
            pos_version="opportunity_score_v1",
            ecs_version="ecs_v1",
            threshold_set_version="classification_thresholds_v1",
        )
    )
    scoring = require_scored_candidate(store, candidate_id, run_id)
    assert scoring.classification is None
    assert scoring.opportunity_score == 71.0


async def test_an_unscoreable_candidate_still_keeps_its_confidence_and_dossier():
    """§10: knowing how good the evidence was matters most when it was not
    good enough to score."""
    _, _, result = await full_run(search=False)
    candidate = result.candidates[0]
    assert candidate.scoring.evidence_confidence is not None
    assert len(candidate.dossier.dimensions) == 6
    assert candidate.scoring.excluded_dimensions


# --------------------------------------------- contextual, never in a score


async def test_the_contextual_dimensions_are_surfaced_beside_the_score():
    _, _, result = await full_run()
    candidate = result.candidates[0]
    names = [c.dimension_name for c in candidate.context]
    assert names == [d.value for d in CONTEXT_DIMENSIONS]
    assert all(c.contextual_only for c in candidate.context)


def test_only_contextual_dimensions_may_be_surfaced_as_context():
    """The set is §5's, not this module's, and a POS-eligible dimension can
    never be rendered as context."""
    from app.services.deep_research import CONTEXTUAL_ONLY, POS_ELIGIBLE

    assert set(CONTEXT_DIMENSIONS) == CONTEXTUAL_ONLY
    assert set(CONTEXT_DIMENSIONS) & POS_ELIGIBLE == set()


async def test_context_is_refused_if_a_dimension_stops_being_contextual():
    """The guard fires rather than silently surfacing a scored dimension."""
    from dataclasses import replace

    from app.services.product_workflow import _context_for

    _, _, result = await full_run()
    dossier = result.candidates[0].dossier
    tampered = dict(dossier.dimensions)
    price = tampered[EcsDimension.PRICE_EVIDENCE.value]
    tampered[EcsDimension.PRICE_EVIDENCE.value] = replace(
        price, contextual_only=False
    )
    with pytest.raises(ValueError, match="not contextual-only"):
        _context_for(replace(dossier, dimensions=tampered))


async def test_price_context_carries_observables_and_no_willingness_to_pay():
    _, _, result = await full_run()
    price = next(
        c
        for c in result.candidates[0].context
        if c.dimension_name == EcsDimension.PRICE_EVIDENCE.value
    )
    for forbidden in (
        "willingness_to_pay",
        "recommended_price",
        "transaction_price",
        "revenue",
    ):
        assert not any(forbidden in key for key in price.observed_features), forbidden


# ------------------------------------------------------ scope preservation


async def test_two_candidates_in_one_run_never_cross_contaminate():
    store, run_id, result = await full_run(
        [workflow_candidate("A"), workflow_candidate("B")]
    )
    first, second = result.candidates
    assert first.candidate_id != second.candidate_id
    assert set(first.dossier.evidence_ids).isdisjoint(second.dossier.evidence_ids)
    for candidate in result.candidates:
        assert candidate.dossier.candidate_id == candidate.candidate_id
        assert candidate.scoring.candidate_id == candidate.candidate_id
        assert candidate.dossier.research_run_id == run_id


async def test_a_second_run_does_not_read_the_first_runs_evidence():
    """Disjointness alone is not enough to prove this.

    An unscoped read returns both runs' records, the dossier correctly refuses
    the mixed scope, and `evidence_ids` becomes empty — which satisfies
    disjointness vacuously while the second run is in fact completely broken.
    So this asserts the second run actually WORKED as well as that it stayed
    in its own lane.
    """
    store = ResearchStore()
    candidate = workflow_candidate()
    _, first_run, first = await full_run([candidate], store=store)
    _, second_run, second = await full_run([candidate], store=store)
    assert first_run != second_run

    first_dossier = first.candidates[0].dossier
    second_dossier = second.candidates[0].dossier
    for dossier, run_id in (
        (first_dossier, first_run),
        (second_dossier, second_run),
    ):
        assert dossier.state is not DeepResearchState.SCOPE_MISMATCH
        assert dossier.evidence_ids, "the run read its own evidence"
        assert dossier.research_run_id == run_id
    # Each run scored on its own evidence.
    assert first.candidates[0].scoring.is_scored
    assert second.candidates[0].scoring.is_scored

    first_ids = set(first_dossier.evidence_ids)
    second_ids = set(second_dossier.evidence_ids)
    assert first_ids and second_ids
    assert first_ids.isdisjoint(second_ids)
    # Both scoring records survive: the store is append-only.
    assert len(store.scoring_history(candidate.id, first_run)) == 1
    assert len(store.scoring_history(candidate.id, second_run)) == 1


async def test_the_deep_pass_id_is_shared_across_the_selection_and_recorded():
    _, _, result = await full_run(
        [workflow_candidate("A"), workflow_candidate("B")]
    )
    ids = {c.deep_pass_id for c in result.candidates}
    assert ids == {result.deep_pass_id}
    assert all(c.dossier.deep_pass_id == result.deep_pass_id for c in result.candidates)


def test_workflow_state_reports_no_stage_before_deep_research_runs():
    store = ResearchStore()
    stage, scoring = workflow_state(store, uuid4(), uuid4())
    assert stage is None
    assert scoring is None


async def test_workflow_state_reports_the_reached_stage_without_rerunning():
    store, run_id, result = await full_run()
    candidate = result.candidates[0]
    stage, scoring = workflow_state(store, candidate.candidate_id, run_id)
    assert stage is WorkflowStage.SCORED
    assert scoring is candidate.scoring


def test_the_unscoreable_reason_distinguishes_scope_from_thin_evidence():
    from app.services.deep_research import DeepResearchState

    from tests.test_deep_research import all_capabilities, build, keyword_evidence

    mine, theirs, run_id = uuid4(), uuid4(), uuid4()
    mixed = build(
        mine,
        run_id,
        [
            keyword_evidence(mine, run_id, "a", 100),
            keyword_evidence(theirs, run_id, "b", 100),
        ],
        all_capabilities(),
    )
    assert mixed.state is DeepResearchState.SCOPE_MISMATCH
    from app.services.opportunity_scoring import score_candidate

    result = CandidateWorkflowResult(
        candidate_id=mine,
        research_run_id=run_id,
        deep_pass_id=uuid4(),
        stage=WorkflowStage.DEEP_RESEARCH,
        dossier=mixed,
        scoring=score_candidate(mixed),
        context=(),
    )
    assert unscoreable_reason(result) == "candidate_is_not_part_of_this_research_run"


async def test_a_scored_candidate_has_no_unscoreable_reason():
    _, _, result = await full_run()
    assert unscoreable_reason(result.candidates[0]) is None


# ------------------------------------------------------------ determinism


async def test_the_same_evidence_produces_the_same_score_across_runs():
    """Two runs over identical fake evidence agree on every number."""
    store_a, run_a, first = await full_run()
    store_b, run_b, second = await full_run()
    a, b = first.candidates[0].scoring, second.candidates[0].scoring
    assert a.opportunity_score == b.opportunity_score
    assert a.scoring_state is b.scoring_state
    assert [s.value for s in a.sub_scores] == [s.value for s in b.sub_scores]


async def test_candidate_order_does_not_change_any_candidates_result():
    names = ["A", "B", "C"]
    baseline = None
    rng = random.Random(7100)
    for _ in range(6):
        candidates = [workflow_candidate(n) for n in names]
        rng.shuffle(candidates)
        _, _, result = await full_run(candidates)
        by_title = {
            c.candidate_id: c.scoring.opportunity_score for c in result.candidates
        }
        scores = sorted(v for v in by_title.values() if v is not None)
        if baseline is None:
            baseline = scores
        else:
            assert scores == baseline


# ------------------------------------------------------ module boundaries


def _symbols() -> set[str]:
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            out.add(node.id)
        elif isinstance(node, ast.Attribute):
            out.add(node.attr)
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.add(node.module)
            out.update(a.asname or a.name for a in node.names)
    return out


def test_the_workflow_invents_no_score_of_its_own():
    symbols = _symbols()
    for forbidden in (
        "app.services.scoring", "score_opportunity", "weighted_opportunity_score",
        "WEIGHTS", "apply_kill_rules", "classify", "search_norm", "review_norm",
        "attention_norm", "ECS_CLASSIFICATION_FLOOR", "RED_UPPER_BOUND",
    ):
        assert forbidden not in symbols, forbidden


def test_no_preliminary_triage_crosses_into_the_workflow():
    symbols = _symbols()
    for forbidden in (
        "app.services.preliminary_ranking", "preliminary_rank", "rank",
        "PreliminaryRanking", "preliminary_dimensions", "selection_size",
    ):
        assert forbidden not in symbols, forbidden


def test_the_workflow_touches_no_parked_or_legacy_work():
    text = MODULE_PATH.read_text(encoding="utf-8")
    for forbidden in ("pricing_context", "4h", "scoring.py"):
        assert forbidden not in text.lower(), forbidden


def test_score_endpoint_remains_quarantined():
    from fastapi.testclient import TestClient

    from app.main import app
    from app.services.scoring import SCORING_STATUS

    assert TestClient(app).post("/score", json={}).status_code == 410
    assert SCORING_STATUS == "UNAPPROVED_EXPERIMENTAL"


def test_the_limitations_state_what_the_workflow_does_not_claim():
    joined = " ".join(LIMITATIONS).lower()
    assert "computes no score" in joined
    assert "never willingness to pay" in joined
    assert "unmeasured, not bad" in joined
    assert "never predictions" in joined


# ------------------------------------------------- the flow through the API


def _api_client(monkeypatch):
    """A client whose providers are the deterministic fakes."""
    from fastapi.testclient import TestClient

    from app.api import routes
    from app.main import app

    monkeypatch.setitem(routes.SEARCH_DEMAND_PROVIDERS, "fake", FakeSearchDemand)
    monkeypatch.setitem(
        routes.MARKETPLACE_PROVIDERS, "fake", lambda: FakeMarketplace(TEN_LISTINGS)
    )
    monkeypatch.setitem(
        routes.PUBLIC_CONTENT_PROVIDERS, "fake", lambda: FakeContent(TEN_VIDEOS)
    )
    return TestClient(app)


def test_the_whole_flow_runs_through_the_api(monkeypatch):
    """Discover is out of scope here (it needs a generation provider), so the
    run is registered directly; everything after it is the real surface."""
    from app.api.routes import get_research_store

    client = _api_client(monkeypatch)
    store = get_research_store()
    candidate = workflow_candidate()
    run_id = uuid4()
    store.register_run(run_id, [candidate])

    # Steps 7-9.
    deep = client.post(
        "/workflow/deep-research",
        json={
            "research_run_id": str(run_id),
            "candidate_ids": [str(candidate.id)],
            "search_demand_provider": "fake",
            "marketplace": "fake",
            "public_content_provider": "fake",
        },
    )
    assert deep.status_code == 200, deep.text
    body = deep.json()
    assert body["scored_candidate_ids"] == [str(candidate.id)]
    scored = body["candidates"][0]
    assert scored["may_generate_product"] is True
    assert scored["unscoreable_reason"] is None
    assert scored["scoring"]["opportunity_score"] is not None
    # The dossier, its missing-data reasons and the contextual evidence are
    # all exposed for reading.
    assert len(scored["dimensions"]) == 6
    assert [c["dimension_name"] for c in scored["context"]] == [
        d.value for d in CONTEXT_DIMENSIONS
    ]
    assert scored["limitations"]

    # Stage lookup without re-running anything.
    state = client.get(
        f"/workflow/runs/{run_id}/candidates/{candidate.id}"
    )
    assert state.status_code == 200
    assert state.json()["stage"] == "SCORED"

    # Step 10, now that a score exists.
    spec = client.post(
        "/product/specification",
        json={"research_run_id": str(run_id), "candidate_id": str(candidate.id)},
    )
    assert spec.status_code == 200, spec.text
    spec_body = spec.json()
    assert spec_body["specification"]["state"] == "GENERATED"
    # 4G ran after 4C.
    assert spec_body["product_job_fit"] is not None
    # The score travelled with the specification.
    assert spec_body["scoring"]["opportunity_score"] == (
        scored["scoring"]["opportunity_score"]
    )

    # Content intelligence, post-score.
    plan = client.post(
        "/workflow/content-plan",
        json={"research_run_id": str(run_id), "candidate_id": str(candidate.id)},
    )
    assert plan.status_code == 200, plan.text
    plan_body = plan.json()
    assert plan_body["candidate_id"] == str(candidate.id)
    assert plan_body["experiments_state"]
    assert plan_body["limitations"]


def test_the_api_refuses_product_generation_before_deep_research(monkeypatch):
    from app.api.routes import get_research_store

    client = _api_client(monkeypatch)
    store = get_research_store()
    candidate = workflow_candidate()
    run_id = uuid4()
    store.register_run(run_id, [candidate])

    spec = client.post(
        "/product/specification",
        json={"research_run_id": str(run_id), "candidate_id": str(candidate.id)},
    )
    assert spec.status_code == 409
    assert spec.json()["detail"]["reason"] == GATE_NOT_SCORED

    plan = client.post(
        "/workflow/content-plan",
        json={"research_run_id": str(run_id), "candidate_id": str(candidate.id)},
    )
    assert plan.status_code == 409


def test_the_api_refuses_a_candidate_outside_the_run(monkeypatch):
    from app.api.routes import get_research_store

    client = _api_client(monkeypatch)
    store = get_research_store()
    run_id = uuid4()
    store.register_run(run_id, [workflow_candidate()])
    response = client.post(
        "/workflow/deep-research",
        json={
            "research_run_id": str(run_id),
            "candidate_ids": [str(uuid4())],
            "marketplace": "fake",
        },
    )
    assert response.status_code == 404
    assert "not part of research run" in response.json()["detail"]


def test_the_api_refuses_an_unknown_run(monkeypatch):
    client = _api_client(monkeypatch)
    response = client.post(
        "/workflow/deep-research",
        json={"research_run_id": str(uuid4()), "candidate_ids": [str(uuid4())]},
    )
    assert response.status_code == 404
    assert "not found" in response.json()["detail"]


def test_the_route_surface_is_exactly_the_pinned_one():
    from app.main import app

    from tests.route_surface import assert_route_surface_unchanged

    assert_route_surface_unchanged(app)
