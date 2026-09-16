"""Milestones 6D–6G tests: POS sub-scores, the candidate scalar, state and colour.

No network, no live API calls, no new provider.

Five rules carry these milestones.

1. Missing never becomes zero. A blocked sub-score is `None` with a reason.
   An absent score is `None`. An absent colour is `None` and never RED.

2. Observed zero is a real measurement and scores. `log10(0 + 1) = 0` is a
   score, not an absence.

3. No partial scoring and no renormalization. All three sub-scores are
   required; a mean over a subset is a different measurement.

4. A proxy stays a proxy, and attention stays attention.

5. The four §10 states and the results they carry cannot disagree.
"""

import ast
import random
import re
from datetime import UTC, datetime
from math import log10
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from app.services.deep_research import DeepResearchState
from app.services.evidence_confidence import EcsDimension
from app.services.opportunity_scoring import (
    CLASSIFICATION_THRESHOLDS_VERSION,
    ECS_CLASSIFICATION_FLOOR,
    LIMITATIONS,
    OPPORTUNITY_SCORE_VERSION,
    POS_DIMENSION_COUNT,
    RED_UPPER_BOUND,
    STATE_BOUNDARIES,
    V1_KILL_RULES,
    YELLOW_UPPER_BOUND,
    Classification,
    ScoringResult,
    ScoringState,
    aggregate_opportunity_score,
    classify,
    not_scored,
    score_candidate,
)
from app.services.pos_dimensions import (
    ATTENTION_SATURATION_VALUE,
    POS_DIMENSION_ORDER,
    POS_REQUIRED_STATISTICS,
    POS_SOURCE_DIMENSION,
    REVIEW_SATURATION_VALUE,
    SEARCH_SATURATION_VALUE,
    PosDimensionName,
    PosDimensionState,
    attention_norm,
    compute_pos_sub_scores,
    compute_sub_score,
    proxy_prevalence,
    review_norm,
    search_norm,
)
from app.domain.enums import SnapshotStatus
from app.services.preliminary_dimensions import DimensionState
from app.services.research_orchestration import (
    CAPABILITY_MARKETPLACE,
    CAPABILITY_PUBLIC_CONTENT,
    CAPABILITY_SEARCH_DEMAND,
    CapabilityOutcome,
)
from app.storage.memory import ResearchStore

from tests.test_deep_research import (
    all_capabilities,
    build,
    keyword_evidence,
    marketplace_listing,
    video_evidence,
)

from tests.route_surface import assert_route_surface_unchanged

POS_MODULE = Path("app/services/pos_dimensions.py")
SCORING_MODULE = Path("app/services/opportunity_scoring.py")
SCHEMA = Path("schema.sql")


# ----------------------------------------------------------------- fixtures


def dossier(
    *,
    keyword_volumes: list[int] | None = None,
    review_counts: list[int] | None = None,
    view_counts: list[int] | None = None,
    candidate_id: UUID | None = None,
    run_id: UUID | None = None,
):
    """A Step 7 dossier with exactly the evidence asked for."""
    candidate_id = candidate_id or uuid4()
    run_id = run_id or uuid4()
    evidence = []
    for index, volume in enumerate(keyword_volumes or []):
        evidence.append(keyword_evidence(candidate_id, run_id, f"k{index}", volume))
    for index, reviews in enumerate(review_counts or []):
        evidence += marketplace_listing(
            candidate_id, run_id, f"L{index}", price=10.0 + index, review_count=reviews
        )
    for index, views in enumerate(view_counts or []):
        evidence.append(video_evidence(candidate_id, run_id, f"V{index}", views))
    return build(candidate_id, run_id, evidence, all_capabilities())


def rich_dossier(**kwargs):
    """Evidence deep enough for all three sub-scores and a healthy ECS."""
    defaults = dict(
        keyword_volumes=[5000] * 8,
        review_counts=[60] * 12,
        view_counts=[40000] * 12,
    )
    defaults.update(kwargs)
    return dossier(**defaults)


def sub(result, name: PosDimensionName):
    return next(s for s in result.sub_scores if s.name is name)


# -------------------------------------------- 6D: the normalization curves


@pytest.mark.parametrize(
    "norm,saturation,exponent",
    [
        (search_norm, SEARCH_SATURATION_VALUE, 5),
        (review_norm, REVIEW_SATURATION_VALUE, 3),
        (attention_norm, ATTENTION_SATURATION_VALUE, 6),
    ],
)
def test_each_scale_matches_its_approved_formula(norm, saturation, exponent):
    for value in (0, 1, 9, 42, 999, 12345):
        expected = min(100.0, 100.0 * log10(value + 1) / exponent)
        assert norm(value) == pytest.approx(expected)
    assert saturation == 10**exponent


@pytest.mark.parametrize(
    "norm,saturation",
    [
        (search_norm, SEARCH_SATURATION_VALUE),
        (review_norm, REVIEW_SATURATION_VALUE),
        (attention_norm, ATTENTION_SATURATION_VALUE),
    ],
)
def test_each_scale_saturates_at_its_stated_point_and_never_exceeds_100(
    norm, saturation
):
    assert norm(saturation) == 100.0
    assert norm(saturation * 1000) == 100.0
    # The `+ 1` shifts the curve by one unit, so `saturation - 1` is already
    # exactly 100 and the last value BELOW saturation is two under.
    assert norm(saturation - 1) == 100.0
    assert norm(saturation - 2) < 100.0


def test_an_observed_zero_normalizes_to_zero_rather_than_to_negative_infinity():
    """The `+ 1` is what makes a real zero observation scoreable."""
    assert search_norm(0) == 0.0
    assert review_norm(0) == 0.0
    assert attention_norm(0) == 0.0


@pytest.mark.parametrize("norm", [search_norm, review_norm, attention_norm])
def test_every_scale_is_monotonic(norm):
    rng = random.Random(6400)
    values = sorted(rng.uniform(0, 2_000_000) for _ in range(200))
    scores = [norm(v) for v in values]
    assert scores == sorted(scores)


@pytest.mark.parametrize("norm", [search_norm, review_norm, attention_norm])
def test_a_negative_observation_is_refused_rather_than_clamped(norm):
    """No observed count can be negative; silently reading one as 0 would
    hide an upstream defect."""
    with pytest.raises(ValueError, match="negative"):
        norm(-1)


def test_proxy_prevalence_is_a_proportion_scaled_not_log_normalized():
    assert proxy_prevalence(0.0) == 0.0
    assert proxy_prevalence(0.25) == 25.0
    assert proxy_prevalence(1.0) == 100.0
    for outside in (-0.01, 1.01):
        with pytest.raises(ValueError, match="proportion"):
            proxy_prevalence(outside)


# ------------------------------------------------ 6D: the three sub-scores


def test_the_v1_pos_surface_is_exactly_three_dimensions():
    assert len(POS_DIMENSION_ORDER) == POS_DIMENSION_COUNT == 3
    assert set(POS_SOURCE_DIMENSION.values()) == {
        EcsDimension.SEARCH_DEMAND,
        EcsDimension.PURCHASE_PROXY_EVIDENCE,
        EcsDimension.AUDIENCE_ATTENTION,
    }


def test_search_demand_sub_score_is_the_mean_of_its_three_normalized_quartiles():
    result = compute_pos_sub_scores(rich_dossier(keyword_volumes=[100, 1000, 10000]))
    demand = sub(result, PosDimensionName.SEARCH_DEMAND)
    assert demand.state is PosDimensionState.SCORED
    by_hand = round(
        sum(search_norm(value) for _, value in demand.inputs) / 3, 2
    )
    assert demand.value == by_hand
    assert [name for name, _ in demand.inputs] == list(
        POS_REQUIRED_STATISTICS[PosDimensionName.SEARCH_DEMAND]
    )


def test_purchase_proxy_sub_score_mixes_two_review_norms_and_a_prevalence():
    result = compute_pos_sub_scores(rich_dossier())
    proxy = sub(result, PosDimensionName.PURCHASE_PROXY)
    assert proxy.state is PosDimensionState.SCORED
    observed = dict(proxy.inputs)
    normalized = dict(proxy.normalized_inputs)
    assert normalized["median_review_count"] == review_norm(
        observed["median_review_count"]
    )
    assert normalized["proportion_of_comparables_with_proxy"] == proxy_prevalence(
        observed["proportion_of_comparables_with_proxy"]
    )
    assert proxy.value == round(sum(normalized.values()) / 3, 2)


def test_audience_attention_sub_score_is_the_mean_of_its_three_view_quartiles():
    result = compute_pos_sub_scores(rich_dossier())
    attention = sub(result, PosDimensionName.AUDIENCE_ATTENTION)
    assert attention.state is PosDimensionState.SCORED
    assert attention.value == round(
        sum(score for _, score in attention.normalized_inputs) / 3, 2
    )


def test_every_sub_score_is_recomputable_by_hand_from_what_it_emits():
    result = compute_pos_sub_scores(rich_dossier())
    for score in result.sub_scores:
        assert score.state is PosDimensionState.SCORED
        assert score.value == round(
            sum(v for _, v in score.normalized_inputs)
            / len(score.normalized_inputs),
            2,
        )
        assert len(score.inputs) == len(score.normalized_inputs) == 3


def test_every_sub_score_is_rounded_to_two_decimals_and_stays_in_range():
    rng = random.Random(6401)
    for _ in range(60):
        result = compute_pos_sub_scores(
            rich_dossier(
                keyword_volumes=[rng.randint(0, 500_000) for _ in range(6)],
                review_counts=[rng.randint(0, 5_000) for _ in range(6)],
                view_counts=[rng.randint(0, 5_000_000) for _ in range(6)],
            )
        )
        for score in result.sub_scores:
            if score.value is None:
                continue
            assert 0.0 <= score.value <= 100.0
            assert round(score.value, 2) == score.value


def test_every_sub_score_carries_its_own_version():
    result = compute_pos_sub_scores(rich_dossier())
    versions = {s.name: s.version for s in result.sub_scores}
    assert versions == {
        PosDimensionName.SEARCH_DEMAND: "pos_search_demand_v1",
        PosDimensionName.PURCHASE_PROXY: "pos_purchase_proxy_v1",
        PosDimensionName.AUDIENCE_ATTENTION: "pos_audience_attention_v1",
    }


# ---------------------------------------- 6D: missing never becomes zero


def test_an_unscoreable_dimension_blocks_its_sub_score_rather_than_scoring_zero():
    result = compute_pos_sub_scores(dossier(keyword_volumes=[]))
    demand = sub(result, PosDimensionName.SEARCH_DEMAND)
    assert demand.state is PosDimensionState.BLOCKED
    assert demand.value is None
    assert demand.value != 0.0
    assert demand.blocked_reason == "source_dimension_not_scoreable"


def test_a_queried_but_unmeasured_dimension_blocks_rather_than_scoring_zero():
    """UNKNOWN is not a zero observation."""
    candidate_id, run_id = uuid4(), uuid4()
    unmeasured = build(
        candidate_id,
        run_id,
        [keyword_evidence(candidate_id, run_id, "a", None)],
        all_capabilities(),
    )
    demand = sub(compute_pos_sub_scores(unmeasured), PosDimensionName.SEARCH_DEMAND)
    assert demand.state is PosDimensionState.BLOCKED
    assert demand.value is None


def test_an_observed_zero_scores_and_a_missing_one_blocks():
    """The two cases that must never be confused, side by side."""
    zero = compute_pos_sub_scores(rich_dossier(keyword_volumes=[0, 0, 0, 0, 0, 0]))
    absent = compute_pos_sub_scores(rich_dossier(keyword_volumes=[]))
    zero_demand = sub(zero, PosDimensionName.SEARCH_DEMAND)
    absent_demand = sub(absent, PosDimensionName.SEARCH_DEMAND)
    assert zero_demand.state is PosDimensionState.SCORED
    assert zero_demand.value == 0.0
    assert absent_demand.state is PosDimensionState.BLOCKED
    assert absent_demand.value is None


def test_a_missing_required_statistic_blocks_and_names_which_one():
    from app.services.deep_research import DeepDimension

    incomplete = DeepDimension(
        dimension_name=EcsDimension.SEARCH_DEMAND.value,
        state=DimensionState.EVIDENCE_PRESENT_UNSCORED,
        missing_reason=None,
        observed_features={"q25_search_volume": 10, "median_search_volume": 20},
        truth_basis=None,
        evidence_ids=(),
        formula_version="x",
        conflict_count=0,
        sample_size=2,
        required=True,
        contextual_only=False,
        pos_eligible=True,
    )
    score = compute_sub_score(PosDimensionName.SEARCH_DEMAND, incomplete)
    assert score.state is PosDimensionState.BLOCKED
    assert score.blocked_reason == "required_statistic_unavailable:q75_search_volume"
    assert score.value is None


def test_an_absent_dimension_blocks_without_raising():
    score = compute_sub_score(PosDimensionName.PURCHASE_PROXY, None)
    assert score.state is PosDimensionState.BLOCKED
    assert score.blocked_reason == "source_dimension_absent"


# -------------------------------------------- 6E: the candidate scalar


def test_the_candidate_score_is_the_unweighted_mean_of_all_three():
    result = score_candidate(rich_dossier())
    assert result.opportunity_score is not None
    by_hand = round(
        sum(s.value for s in result.sub_scores if s.value is not None) / 3, 2
    )
    assert result.opportunity_score == by_hand


def test_weighting_is_equal_and_order_cannot_change_the_scalar():
    sub_scores = compute_pos_sub_scores(rich_dossier())
    values = [s.value for s in sub_scores.sub_scores]
    rng = random.Random(6402)
    baseline = aggregate_opportunity_score(sub_scores)
    for _ in range(50):
        shuffled = list(values)
        rng.shuffle(shuffled)
        assert round(sum(shuffled) / 3, 2) == baseline


def test_any_blocked_sub_score_blocks_the_candidate_score_entirely():
    """No partial scoring, and no renormalization over what survived."""
    result = score_candidate(rich_dossier(view_counts=[]))
    assert result.scoring_state is ScoringState.INSUFFICIENT_EVIDENCE
    assert result.opportunity_score is None
    assert result.classification is None


def test_a_blocked_dimension_is_never_renormalized_away():
    """The legacy defect, stated as the number it would have produced.

    `weighted_opportunity_score` renormalized over whichever weights were
    present, so a candidate with two strong dimensions and one missing scored
    as though the missing one did not exist.
    """
    full = score_candidate(rich_dossier())
    partial = score_candidate(rich_dossier(view_counts=[]))
    surviving = [
        s.value for s in partial.sub_scores if s.value is not None
    ]
    assert len(surviving) == 2
    renormalized = round(sum(surviving) / len(surviving), 2)
    assert partial.opportunity_score is None
    assert partial.opportunity_score != renormalized
    assert full.opportunity_score is not None


def test_what_was_computable_is_still_retained_when_the_candidate_is_blocked():
    result = score_candidate(rich_dossier(view_counts=[]))
    scored = [s for s in result.sub_scores if s.value is not None]
    assert len(scored) == 2
    assert result.excluded_dimensions == (
        (EcsDimension.AUDIENCE_ATTENTION.value, "source_dimension_not_scoreable"),
    )
    # §10: confidence is still computed and stored.
    assert result.evidence_confidence is not None


def test_the_scalar_is_bounded_and_rounded():
    rng = random.Random(6403)
    for _ in range(40):
        result = score_candidate(
            rich_dossier(
                keyword_volumes=[rng.randint(0, 999_999) for _ in range(6)],
                review_counts=[rng.randint(0, 9_999) for _ in range(6)],
                view_counts=[rng.randint(0, 9_999_999) for _ in range(6)],
            )
        )
        if result.opportunity_score is None:
            continue
        assert 0.0 <= result.opportunity_score <= 100.0
        assert round(result.opportunity_score, 2) == result.opportunity_score


# ------------------------------------------------ 6G: the colour bands


@pytest.mark.parametrize(
    "score,expected",
    [
        (0.0, Classification.RED),
        (39.99, Classification.RED),
        (40.0, Classification.YELLOW),
        (55.0, Classification.YELLOW),
        (69.99, Classification.YELLOW),
        (70.0, Classification.GREEN),
        (100.0, Classification.GREEN),
    ],
)
def test_the_approved_bands_including_both_boundaries(score, expected):
    assert classify(score) is expected


def test_the_band_edges_are_the_approved_values():
    assert RED_UPPER_BOUND == 40.0
    assert YELLOW_UPPER_BOUND == 70.0
    assert ECS_CLASSIFICATION_FLOOR == 60.0


def weak_evidence_dossier():
    """A real dossier whose POS is strong but whose evidence is not.

    Thin samples, every record stale, every record of unrecognised
    provenance, every capability degraded. All three POS dimensions still
    score, so this is the case the floor exists for: a GREEN-range number
    resting on evidence nobody should act on.
    """
    candidate_id, run_id = uuid4(), uuid4()
    evidence = [keyword_evidence(candidate_id, run_id, f"k{i}", 5000) for i in range(2)]
    for index in range(2):
        evidence += marketplace_listing(
            candidate_id, run_id, f"L{index}", price=10.0, review_count=30
        )
    evidence += [
        video_evidence(candidate_id, run_id, f"V{i}", 40_000) for i in range(2)
    ]
    evidence = [
        item.model_copy(update={"collection_method": "scraped"}) for item in evidence
    ]
    capabilities = tuple(
        CapabilityOutcome(capability=name, status=SnapshotStatus.PARTIAL.value)
        for name in (
            CAPABILITY_SEARCH_DEMAND,
            CAPABILITY_MARKETPLACE,
            CAPABILITY_PUBLIC_CONTENT,
        )
    )
    return build(
        candidate_id,
        run_id,
        evidence,
        capabilities,
        now=datetime(2027, 6, 1, tzinfo=UTC),
    )


def test_a_score_below_the_confidence_floor_gets_no_colour():
    """Score exists, colour does not. An absent colour is not RED."""
    result = score_candidate(weak_evidence_dossier())
    assert result.scoring_state is ScoringState.SCORED_UNCLASSIFIED
    assert result.opportunity_score is not None
    assert result.evidence_confidence < ECS_CLASSIFICATION_FLOOR
    assert result.classification is None
    # The score is in GREEN territory, and still gets no colour: the floor is
    # about the evidence, not the magnitude.
    assert result.opportunity_score >= YELLOW_UPPER_BOUND
    assert classify(result.opportunity_score) is Classification.GREEN


def test_the_floor_is_the_only_thing_withholding_that_colour():
    """Same score, confidence above the floor, and the colour appears."""
    withheld = score_candidate(weak_evidence_dossier())
    granted = score_candidate(rich_dossier())
    assert withheld.classification is None
    assert granted.classification is not None
    assert granted.evidence_confidence >= ECS_CLASSIFICATION_FLOOR


def test_a_dossier_with_no_computable_confidence_gets_no_colour_either():
    """A blocked ECS is not a passing ECS. Absence never clears a floor."""
    candidate_id, run_id = uuid4(), uuid4()
    # Public content never reports at all, which blocks ECS outright.
    evidence = [keyword_evidence(candidate_id, run_id, f"k{i}", 5000) for i in range(6)]
    for index in range(8):
        evidence += marketplace_listing(
            candidate_id, run_id, f"L{index}", price=10.0, review_count=40
        )
    evidence += [
        video_evidence(candidate_id, run_id, f"V{i}", 40_000) for i in range(8)
    ]
    partial_capabilities = (
        CapabilityOutcome(
            capability=CAPABILITY_SEARCH_DEMAND, status=SnapshotStatus.COMPLETE.value
        ),
        CapabilityOutcome(
            capability=CAPABILITY_MARKETPLACE, status=SnapshotStatus.COMPLETE.value
        ),
    )
    dossier_ = build(candidate_id, run_id, evidence, partial_capabilities)
    result = score_candidate(dossier_)
    assert result.evidence_confidence is None
    assert result.opportunity_score is not None
    assert result.scoring_state is ScoringState.SCORED_UNCLASSIFIED
    assert result.classification is None


def test_v1_triggers_no_kill_rules_and_revives_none():
    result = score_candidate(rich_dossier())
    assert result.kill_rules_triggered == ()
    assert V1_KILL_RULES == ()
    text = SCORING_MODULE.read_text(encoding="utf-8")
    for legacy in (
        "NO_PROVEN_DEMAND_OR_PURCHASE_SIGNAL",
        "NO_IDENTIFIABLE_DISTRIBUTION_ROUTE",
        "apply_kill_rules",
    ):
        assert legacy not in text, legacy


# --------------------------------------------- 6F: the four-state contract


def test_every_scoring_state_is_reachable():
    observed = {
        not_scored(uuid4(), uuid4()).scoring_state,
        score_candidate(rich_dossier(view_counts=[])).scoring_state,
        score_candidate(rich_dossier()).scoring_state,
    }
    assert ScoringState.NOT_SCORED in observed
    assert ScoringState.INSUFFICIENT_EVIDENCE in observed
    assert ScoringState.CLASSIFIED in observed
    # SCORED_UNCLASSIFIED needs a POS with a below-floor ECS, built directly.
    unclassified = ScoringResult(
        candidate_id=uuid4(),
        research_run_id=uuid4(),
        scoring_state=ScoringState.SCORED_UNCLASSIFIED,
        state_boundary=STATE_BOUNDARIES[ScoringState.SCORED_UNCLASSIFIED],
        opportunity_score=55.0,
        evidence_confidence=12.0,
        classification=None,
        sub_scores=(),
        excluded_dimensions=(),
        kill_rules_triggered=(),
        pos_version=OPPORTUNITY_SCORE_VERSION,
        ecs_version="ecs_v1",
        threshold_set_version=CLASSIFICATION_THRESHOLDS_VERSION,
    )
    assert unclassified.scoring_state is ScoringState.SCORED_UNCLASSIFIED
    assert len(ScoringState) == 4


def test_not_scored_is_the_absence_of_an_attempt():
    result = not_scored(uuid4(), uuid4())
    assert result.opportunity_score is None
    assert result.evidence_confidence is None
    assert result.classification is None
    assert "absence of an attempt" in result.state_boundary


@pytest.mark.parametrize(
    "state,score,classification",
    [
        (ScoringState.NOT_SCORED, 50.0, None),
        (ScoringState.INSUFFICIENT_EVIDENCE, 50.0, None),
        (ScoringState.SCORED_UNCLASSIFIED, None, None),
        (ScoringState.CLASSIFIED, None, Classification.GREEN),
        (ScoringState.SCORED_UNCLASSIFIED, 50.0, Classification.GREEN),
        (ScoringState.INSUFFICIENT_EVIDENCE, None, Classification.RED),
    ],
)
def test_a_state_can_never_carry_results_it_forbids(state, score, classification):
    with pytest.raises(ValueError):
        ScoringResult(
            candidate_id=uuid4(),
            research_run_id=uuid4(),
            scoring_state=state,
            state_boundary="",
            opportunity_score=score,
            evidence_confidence=90.0,
            classification=classification,
            sub_scores=(),
            excluded_dimensions=(),
            kill_rules_triggered=(),
            pos_version="x",
            ecs_version="y",
            threshold_set_version="z",
        )


def test_a_classified_result_must_carry_the_confidence_that_classified_it():
    with pytest.raises(ValueError, match="confidence"):
        ScoringResult(
            candidate_id=uuid4(),
            research_run_id=uuid4(),
            scoring_state=ScoringState.CLASSIFIED,
            state_boundary="",
            opportunity_score=80.0,
            evidence_confidence=None,
            classification=Classification.GREEN,
            sub_scores=(),
            excluded_dimensions=(),
            kill_rules_triggered=(),
            pos_version="x",
            ecs_version="y",
            threshold_set_version="z",
        )


def test_an_out_of_range_score_is_refused():
    with pytest.raises(ValueError, match="0-100"):
        ScoringResult(
            candidate_id=uuid4(),
            research_run_id=uuid4(),
            scoring_state=ScoringState.SCORED_UNCLASSIFIED,
            state_boundary="",
            opportunity_score=101.0,
            evidence_confidence=50.0,
            classification=None,
            sub_scores=(),
            excluded_dimensions=(),
            kill_rules_triggered=(),
            pos_version="x",
            ecs_version="y",
            threshold_set_version="z",
        )


def test_scoring_records_are_append_only_and_scoped():
    store = ResearchStore()
    candidate_id, run_id = uuid4(), uuid4()
    first = not_scored(candidate_id, run_id)
    store.add_scoring_result(first)
    assert store.latest_scoring_result(candidate_id, run_id) is first

    second = score_candidate(
        rich_dossier(candidate_id=candidate_id, run_id=run_id)
    )
    store.add_scoring_result(second)
    history = store.scoring_history(candidate_id, run_id)
    assert history == [first, second], "a rescore never destroys the earlier record"
    assert store.latest_scoring_result(candidate_id, run_id) is second
    # Another scope sees nothing.
    assert store.latest_scoring_result(uuid4(), run_id) is None


def test_no_result_means_step_8_has_not_run_not_a_score_of_zero():
    store = ResearchStore()
    assert store.latest_scoring_result(uuid4(), uuid4()) is None


def test_every_state_says_what_it_does_not_establish():
    for state in ScoringState:
        assert state in STATE_BOUNDARIES
        assert STATE_BOUNDARIES[state].strip()
    assert "never be rendered" in STATE_BOUNDARIES[ScoringState.INSUFFICIENT_EVIDENCE]
    assert "not RED" in STATE_BOUNDARIES[ScoringState.SCORED_UNCLASSIFIED]


# -------------------------------------------------- 6F: the schema itself


def test_the_schema_makes_every_result_nullable():
    schema = SCHEMA.read_text(encoding="utf-8")
    for column in ("opportunity_score", "evidence_confidence", "classification"):
        assert (
            f"alter table score_versions alter column {column} drop not null"
            in schema
        ), column


def test_the_schema_constrains_the_state_to_the_four_specified_values():
    schema = SCHEMA.read_text(encoding="utf-8")
    # The constraint must be ADDED, not merely named in a preceding `drop`:
    # matching the bare name passes even if the add line was renamed away.
    assert "add constraint score_versions_scoring_state_check" in schema
    assert "check (scoring_state in (" in schema
    for state in ScoringState:
        assert f"'{state.value}'" in schema, state


def test_every_added_schema_constraint_is_also_dropped_first_for_idempotency():
    """Each `add constraint` has a matching `drop constraint if exists`, so
    re-running the migration cannot fail on a constraint that already
    exists."""
    schema = SCHEMA.read_text(encoding="utf-8")
    added = set(re.findall(r"add constraint (\w+)", schema))
    dropped = set(re.findall(r"drop constraint if exists (\w+)", schema))
    assert added
    assert added <= dropped, added - dropped


def test_the_schema_still_constrains_the_colour_but_allows_absence():
    schema = SCHEMA.read_text(encoding="utf-8")
    assert (
        "check (classification is null or classification in ('RED','YELLOW','GREEN'))"
        in schema
    )


def test_the_schema_mirrors_the_state_invariants_the_type_enforces():
    schema = SCHEMA.read_text(encoding="utf-8")
    assert "score_versions_state_consistency_check" in schema
    assert "(opportunity_score is not null)" in schema
    assert "(classification is not null) = (scoring_state = 'CLASSIFIED')" in schema


def test_the_migration_backfills_nothing():
    """There is no value that could honestly stand in for a score that was
    never computed, so existing rows are not invented into."""
    schema = SCHEMA.read_text(encoding="utf-8")
    assert "Nothing is back-filled" in schema
    forbidden = re.findall(r"update\s+score_versions\s+set", schema, re.IGNORECASE)
    assert forbidden == []
    # The new state column defaults to the one truthful value for an old row.
    assert "add column if not exists scoring_state text not null default 'NOT_SCORED'" in schema


def test_the_version_fields_are_persisted():
    schema = SCHEMA.read_text(encoding="utf-8")
    for column in ("pos_version", "ecs_version", "threshold_set_version",
                   "excluded_dimensions"):
        assert f"add column if not exists {column}" in schema, column


def test_every_result_carries_all_three_policy_versions():
    result = score_candidate(rich_dossier())
    assert result.pos_version == OPPORTUNITY_SCORE_VERSION == "opportunity_score_v1"
    assert result.ecs_version == "ecs_v1"
    assert result.threshold_set_version == CLASSIFICATION_THRESHOLDS_VERSION
    assert CLASSIFICATION_THRESHOLDS_VERSION == "classification_thresholds_v1"


# ------------------------------------------------------ determinism


def _identity(result) -> tuple:
    return (
        result.scoring_state,
        result.opportunity_score,
        result.evidence_confidence,
        result.classification,
        result.excluded_dimensions,
        tuple(
            (s.name, s.state, s.value, s.blocked_reason, s.inputs)
            for s in result.sub_scores
        ),
    )


def test_the_same_dossier_always_produces_the_same_score():
    candidate_id, run_id = uuid4(), uuid4()
    built = rich_dossier(candidate_id=candidate_id, run_id=run_id)
    first = _identity(score_candidate(built))
    for _ in range(20):
        assert _identity(score_candidate(built)) == first


def test_evidence_arrival_order_cannot_change_the_score():
    candidate_id, run_id = uuid4(), uuid4()
    evidence = [
        keyword_evidence(candidate_id, run_id, f"k{i}", 100 * (i + 1))
        for i in range(6)
    ]
    for index in range(8):
        evidence += marketplace_listing(
            candidate_id, run_id, f"L{index}", price=10.0, review_count=20 + index
        )
    evidence += [
        video_evidence(candidate_id, run_id, f"V{i}", 5000 * (i + 1)) for i in range(8)
    ]
    baseline = _identity(
        score_candidate(build(candidate_id, run_id, evidence, all_capabilities()))
    )
    rng = random.Random(6404)
    for _ in range(100):
        shuffled = list(evidence)
        rng.shuffle(shuffled)
        assert (
            _identity(
                score_candidate(
                    build(candidate_id, run_id, shuffled, all_capabilities())
                )
            )
            == baseline
        )


def test_a_scope_mismatched_dossier_cannot_be_scored():
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
    result = score_candidate(mixed)
    assert result.scoring_state is ScoringState.INSUFFICIENT_EVIDENCE
    assert result.opportunity_score is None
    assert result.evidence_confidence is None


# ------------------------------------------------------ language boundaries


def test_the_proxy_label_is_never_dropped():
    text = POS_MODULE.read_text(encoding="utf-8")
    assert "PURCHASE PROXY" in text or "purchase PROXY" in text
    for claim in ("units sold", "revenue", "buyers", "probability of success"):
        for match in re.finditer(re.escape(claim), text, re.IGNORECASE):
            window = text[max(0, match.start() - 90) : match.start()]
            assert re.search(r"\b(never|not|no)\b", window, re.IGNORECASE), claim


def test_attention_is_never_called_demand_or_intent():
    text = POS_MODULE.read_text(encoding="utf-8")
    for claim in ("buyer demand", "purchase intent", "willingness to pay"):
        for match in re.finditer(re.escape(claim), text, re.IGNORECASE):
            window = text[max(0, match.start() - 90) : match.start()]
            assert re.search(r"\b(never|not|no)\b", window, re.IGNORECASE), claim


@pytest.mark.parametrize("module", [POS_MODULE, SCORING_MODULE])
def test_the_policy_values_are_never_described_as_validated(module):
    text = module.read_text(encoding="utf-8")
    assert "UNCALIBRATED" in text
    for claim in (r"empirically\s+validated", r"calibrated\s+against", r"\bproven\b"):
        for match in re.finditer(claim, text, re.IGNORECASE):
            window = text[max(0, match.start() - 90) : match.start()]
            assert re.search(
                r"\b(not|never|no|none|neither|without|un\w+)\b",
                window,
                re.IGNORECASE,
            ), f"unhedged: {text[match.start() - 90 : match.end() + 20]!r}"


def test_the_limitations_state_what_a_score_is_not():
    joined = " ".join(LIMITATIONS).lower()
    assert "not empirically validated" in joined
    assert "equal weighting is not a finding" in joined
    assert "never a forecast of" in joined
    assert "never zeros" in joined and "never red" in joined
    assert "purchase proxy" in joined


# ------------------------------------------------------ module boundaries


def _symbols(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
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


@pytest.mark.parametrize("module", [POS_MODULE, SCORING_MODULE])
def test_no_legacy_scoring_is_revived(module):
    symbols = _symbols(module)
    for forbidden in (
        "app.services.scoring", "score_opportunity", "weighted_opportunity_score",
        "WEIGHTS", "CONFIDENCE_WEIGHTS", "ScoreDimensions", "apply_kill_rules",
    ):
        assert forbidden not in symbols, forbidden


@pytest.mark.parametrize("module", [POS_MODULE, SCORING_MODULE])
def test_the_modules_are_provider_free(module):
    for name in _symbols(module):
        for forbidden in ("httpx", "requests", "aiohttp", "urllib", "socket",
                          "app.providers", "app.api"):
            assert not name.startswith(forbidden), name


def test_score_endpoint_remains_quarantined():
    """§12 retains 410 unchanged and §15 does not place activation in 6G."""
    from fastapi.testclient import TestClient

    from app.main import app

    from app.services.scoring import SCORING_STATUS

    assert_route_surface_unchanged(app)
    assert TestClient(app).post("/score", json={}).status_code == 410
    assert SCORING_STATUS == "UNAPPROVED_EXPERIMENTAL"


def test_the_scoring_is_reached_through_the_workflow_not_a_scoring_route():
    """Milestone 7A wired this in. Scoring happens inside the deep-research
    workflow, so no route computes a score on its own and `/score` stays 410."""
    from tests.route_surface import EXPECTED_ROUTE_PATHS

    routes = Path("app/api/routes.py").read_text(encoding="utf-8")
    # The workflow owns the composition; routes never call the scorer directly.
    assert "score_candidate" not in routes
    assert "compute_pos_sub_scores" not in routes
    assert "run_deep_research_and_score" in routes
    # No endpoint exists whose job is to score something handed to it.
    assert "/score" in EXPECTED_ROUTE_PATHS
    assert not any(
        path.endswith("/score") and path != "/score" for path in EXPECTED_ROUTE_PATHS
    )
