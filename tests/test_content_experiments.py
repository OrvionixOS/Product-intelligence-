"""Milestone 5C tests: production-ready content experiments.

No network, no live API calls, no new provider.

Four rules carry the milestone.

1. An experiment is a question, never an answer. No emitted string may claim
   that a pattern performs, wins, or is proven, and no structure may imply it.

2. The evidence behind an experiment is reported exactly as Milestone 5B
   established it. Creator concentration, conflicts and missing identity
   propagate into the experiment's sufficiency and limitations; a pattern
   carried by one creator is never presented as field-wide evidence.

3. Only public, creator-relative outcomes may be named as a measurement.
   Click-through, retention, conversion, revenue and algorithmic preference
   are not observable in this evidence and are never inferred.

4. Experiments are ORDERED by a published rule and never scored. Same
   evidence, same experiments, same ids, same order, same lineage.
"""

import ast
import hashlib
import random
import re
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.domain.enums import EvidencePurpose, TruthClass
from app.domain.models import EvidenceItem
from app.services.content_patterns import (
    ConcentrationState,
    ContentPatternsState,
    CooccurrenceState,
    DurationBand,
    FieldState,
    PatternKind,
    derive_content_patterns,
    duration_band,
)
from app.services.content_experiments import (
    CONTENT_EXPERIMENTS_VERSION,
    DIM_CONTENT_EXPERIMENTS,
    DURATION_BAND_SECONDS,
    EXPERIMENT_ID_VERSION,
    LIMITATIONS,
    MAX_EXPERIMENTS,
    MIN_CREATORS_FOR_BROAD_EVIDENCE,
    MIN_EXPERIMENT_VIDEO_COUNT,
    MIN_TEST_PUBLICATIONS,
    PRIMARY_MEASUREMENT,
    STATE_BOUNDARIES,
    BaselineState,
    EvidenceSufficiency,
    ExperimentDecision,
    ExperimentsState,
    GenerationState,
    PublicationOutcome,
    VariableFamily,
    derive_content_experiments,
    evaluate_success_criterion,
)
from app.services.faceless_content_intelligence import (
    derive_robust_content_intelligence,
)
from app.services.preliminary_dimensions import DimensionState

MODULE_PATH = Path("app/services/content_experiments.py")
PUBLIC_CONTENT = "public_content"
NOT_REQUESTED = "capability_not_requested"
FAILED = "capability_provider_failed"

_UNSET = object()


# ----------------------------------------------------------------- fixtures


def _payload_hash(payload: dict) -> str:
    import json

    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def video_evidence(
    video_id: str,
    *,
    candidate_id: UUID,
    channel_id: str | None = "UC_A",
    title: str | None = "meal plan guide",
    tags: Any = _UNSET,
    category: str | None = "Education",
    duration_seconds: Any = 600,
    views: int | None = 1000,
    research_run_id: UUID | None = None,
    truth_class: TruthClass = TruthClass.OBSERVED,
    payload_hash: str | None = None,
    item_id: UUID | None = None,
) -> list[EvidenceItem]:
    """The evidence shape Milestone 3B emits for one (candidate, video) pair."""
    payload: dict[str, Any] = {
        "video_id": video_id,
        "channel_id": channel_id,
        "title": title,
        "category": category,
        "view_count": views,
        "like_count": 10,
        "comment_count": 3,
        "channel_subscriber_count": 500_000,
    }
    if tags is not _UNSET:
        payload["tags"] = tags
    if duration_seconds is not _UNSET:
        payload["duration_seconds"] = duration_seconds

    common = dict(
        candidate_id=candidate_id,
        research_run_id=research_run_id,
        provider="youtube",
        collection_method="official_api",
        platform="youtube",
        raw_payload=payload,
        raw_payload_hash=payload_hash or _payload_hash(payload),
    )
    items = [
        EvidenceItem(
            signal_type="public_content_observation",
            purpose=EvidencePurpose.CONTENT,
            truth_class=truth_class,
            raw_value=None,
            **common,
        ),
        EvidenceItem(
            signal_type="public_video_view_count",
            purpose=EvidencePurpose.AUDIENCE,
            truth_class=(
                TruthClass.OBSERVED if views is not None else TruthClass.UNKNOWN
            ),
            raw_value=views,
            **common,
        ),
    ]
    if item_id is not None:
        items = [item.model_copy(update={"id": item_id}) for item in items]
    return items


def corpus(*specs, candidate_id: UUID | None = None):
    """Build a corpus from dicts or (video_id, channel_id, title) triples."""
    cid = candidate_id or uuid4()
    evidence: list[EvidenceItem] = []
    for spec in specs:
        if isinstance(spec, dict):
            evidence.extend(video_evidence(candidate_id=cid, **spec))
        else:
            video_id, channel_id, title = spec
            evidence.extend(
                video_evidence(
                    video_id, candidate_id=cid, channel_id=channel_id, title=title
                )
            )
    return cid, evidence


def patterns_for(evidence, candidate_id, *, with_outliers=True, **kwargs):
    outlier = None
    if with_outliers:
        outlier = derive_robust_content_intelligence(
            candidate_id=candidate_id,
            evidence=evidence,
            research_run_id=kwargs.get("research_run_id"),
        )
    return derive_content_patterns(
        candidate_id=candidate_id,
        evidence=evidence,
        outlier_evidence=outlier,
        **kwargs,
    )


def run(evidence, candidate_id, *, with_outliers=True, **kwargs):
    """5B then 5C, for the same scope."""
    research_run_id = kwargs.pop("research_run_id", None)
    patterns = patterns_for(
        evidence,
        candidate_id,
        with_outliers=with_outliers,
        research_run_id=research_run_id,
        **kwargs,
    )
    return derive_content_experiments(
        candidate_id=candidate_id,
        patterns=patterns,
        research_run_id=research_run_id,
    )


def experiment_for(result, kind: PatternKind, value: str):
    return next(
        (
            e
            for e in result.experiments
            if e.variable_kind is kind and e.variable_value == value
        ),
        None,
    )


def title_experiment(result, value: str = "meal"):
    """One named TITLE_TOKEN experiment, for evidence and lineage assertions.

    Co-extensive title variables are separate experiments, so a test that
    cares about the underlying evidence names the token it means rather than
    taking whichever happens to sort first.
    """
    return experiment_for(result, PatternKind.TITLE_TOKEN, value)


def _outlier_corpus(candidate_id: UUID) -> list[EvidenceItem]:
    """Four creators, each with enough videos for 5A to build a baseline.

    Milestone 5A needs MIN_CREATOR_SAMPLE videos per creator before it will
    score anything against that creator's own median, so three ordinary
    uploads sit behind each creator's one high-view video.
    """
    evidence: list[EvidenceItem] = []
    for index, channel in enumerate(("UC_A", "UC_B", "UC_C", "UC_D")):
        for low in range(3):
            evidence.extend(
                video_evidence(
                    f"LOW{index}_{low}", candidate_id=candidate_id,
                    channel_id=channel, title=f"ordinary upload number{low}",
                    views=100,
                )
            )
        evidence.extend(
            video_evidence(
                f"HIGH{index}", candidate_id=candidate_id, channel_id=channel,
                title="meal plan guide", views=10_000,
            )
        )
    return evidence


def _spread_corpus(candidate_id: UUID) -> list[EvidenceItem]:
    """Six videos, six creators, a pattern with genuine creator breadth."""
    plan = [
        ("V1", "UC_A", "meal plan guide for parents", ["meal", "plan"], "Education", 600, 900),
        ("V2", "UC_B", "meal plan hacks", ["meal"], "Education", 240, 8000),
        ("V3", "UC_C", "weekly meal plan", ["meal", "budget"], "Education", 700, 7500),
        ("V4", "UC_D", "budget meal plan guide", ["budget"], "Howto", 1000, 600),
        ("V5", "UC_E", "grocery list template", ["budget"], "Howto", 500, 300),
        ("V6", "UC_F", "meal plan template", ["meal"], "Education", 650, 9000),
    ]
    evidence: list[EvidenceItem] = []
    for vid, ch, title, tags, cat, dur, views in plan:
        evidence.extend(
            video_evidence(
                vid, candidate_id=candidate_id, channel_id=ch, title=title,
                tags=tags, category=cat, duration_seconds=dur, views=views,
            )
        )
    return evidence


def _many_patterns_corpus(candidate_id: UUID, videos: int = 40):
    """Overlapping token chains: many patterns, each over a DISTINCT video set.

    Video i is titled "tokNN tokNN+1", so token k lands in videos k-1 and k
    and every token covers a different pair. That produces far more than the
    cap without any two experiments being co-extensive.
    """
    evidence: list[EvidenceItem] = []
    for index in range(videos):
        evidence.extend(
            video_evidence(
                f"V{index:02d}",
                candidate_id=candidate_id,
                channel_id=f"UC_{index % 7}",
                title=f"tok{index:02d} tok{index + 1:02d}",
                tags=_UNSET,
                category=None,
                duration_seconds=_UNSET,
                views=100 + index,
            )
        )
    return evidence


# ------------------------------------------------------- absent evidence


def test_no_pattern_evidence_supplied_is_missing_not_empty():
    result = derive_content_experiments(candidate_id=uuid4(), patterns=None)
    assert result.experiments_state is ExperimentsState.PATTERN_EVIDENCE_UNAVAILABLE
    assert result.state is DimensionState.MISSING
    assert result.experiments == ()
    assert result.value is None
    assert result.missing_reason == "no_content_pattern_evidence_supplied"
    assert result.generated_experiment_count == 0
    assert "not evidence" in result.state_boundary


def test_no_content_evidence_at_all_is_unknown_not_no_experiments():
    candidate_id = uuid4()
    result = run([], candidate_id)
    assert result.experiments_state is ExperimentsState.NO_PATTERN_EVIDENCE
    assert result.state is DimensionState.UNKNOWN
    assert result.experiments == ()


@pytest.mark.parametrize("reason", [NOT_REQUESTED, FAILED])
def test_5b_capability_failure_is_missing_not_unknown(reason):
    candidate_id = uuid4()
    result = run([], candidate_id, missing_reasons={PUBLIC_CONTENT: reason})
    assert result.experiments_state is ExperimentsState.PATTERN_EVIDENCE_UNAVAILABLE
    assert result.state is DimensionState.MISSING
    assert result.value is None


def test_videos_observed_but_no_recurring_pattern():
    candidate_id, evidence = corpus(("ONLY", "UC_A", "meal plan guide"))
    result = run(evidence, candidate_id)
    assert result.experiments_state is ExperimentsState.NO_PATTERN_EVIDENCE
    assert result.state is DimensionState.UNKNOWN
    assert result.experiments == ()
    assert "describes the sampled content only" in result.state_boundary


def test_metadata_unavailable_produces_no_experiments_but_stays_unknown():
    candidate_id = uuid4()
    evidence: list[EvidenceItem] = []
    for index in range(3):
        evidence.extend(
            video_evidence(
                f"V{index}", candidate_id=candidate_id, title=None, tags=_UNSET,
                category=None, duration_seconds=_UNSET,
            )
        )
    result = run(evidence, candidate_id)
    assert result.experiments_state is ExperimentsState.NO_PATTERN_EVIDENCE
    assert result.state is DimensionState.UNKNOWN
    assert result.value is None


# ---------------------------------------------------------------- 5B scope


def test_pattern_evidence_for_another_candidate_is_refused_unread():
    mine, theirs = uuid4(), uuid4()
    foreign_patterns = patterns_for(_spread_corpus(theirs), theirs)
    assert foreign_patterns.patterns  # the foreign result is rich

    result = derive_content_experiments(candidate_id=mine, patterns=foreign_patterns)
    assert result.experiments_state is ExperimentsState.PATTERNS_SCOPE_MISMATCH
    assert result.state is DimensionState.MISSING
    assert result.experiments == ()
    # Nothing from the foreign scope leaked into counts or lineage.
    assert result.observed_pattern_count == 0
    assert result.provenance.evidence_ids == ()
    assert result.provenance.video_ids == ()
    assert result.candidate_id == mine


def test_pattern_evidence_from_another_research_run_is_refused():
    candidate_id = uuid4()
    run_a, run_b = uuid4(), uuid4()
    evidence: list[EvidenceItem] = []
    for vid, ch in (("V1", "UC_A"), ("V2", "UC_B"), ("V3", "UC_C")):
        evidence.extend(
            video_evidence(
                vid, candidate_id=candidate_id, channel_id=ch,
                title="meal plan guide", research_run_id=run_a,
            )
        )
    patterns = patterns_for(evidence, candidate_id, research_run_id=run_a)
    assert patterns.patterns

    result = derive_content_experiments(
        candidate_id=candidate_id, patterns=patterns, research_run_id=run_b
    )
    assert result.experiments_state is ExperimentsState.PATTERNS_SCOPE_MISMATCH
    assert result.experiments == ()


def test_a_runless_pattern_result_is_refused_for_a_run_scoped_derivation():
    candidate_id = uuid4()
    patterns = patterns_for(_spread_corpus(candidate_id), candidate_id)
    assert patterns.research_run_id is None
    result = derive_content_experiments(
        candidate_id=candidate_id, patterns=patterns, research_run_id=uuid4()
    )
    assert result.experiments_state is ExperimentsState.PATTERNS_SCOPE_MISMATCH


def test_matching_scope_is_consumed():
    candidate_id = uuid4()
    result = run(_spread_corpus(candidate_id), candidate_id)
    assert result.experiments_state is ExperimentsState.EXPERIMENTS_GENERATED
    assert result.experiments
    assert result.provenance.evidence_ids


# ------------------------------------------------------ 5A co-occurrence


def test_5a_unavailable_still_produces_experiments_on_prevalence_alone():
    candidate_id = uuid4()
    result = run(_spread_corpus(candidate_id), candidate_id, with_outliers=False)
    assert result.experiments
    assert result.outlier_evidence_available is False
    for experiment in result.experiments:
        assert experiment.evidence.cooccurrence_state is (
            CooccurrenceState.OUTLIER_EVIDENCE_UNAVAILABLE
        )
        assert experiment.sufficiency is not (
            EvidenceSufficiency.BROAD_WITH_COOCCURRENCE
        )
        assert any("prevalence alone" in lim for lim in experiment.limitations)
        assert "no co-occurrence is reported" in experiment.hypothesis
        assert "not evidence of absence" in experiment.hypothesis


def test_a_foreign_run_5a_result_never_reaches_an_experiment():
    """5B refuses it; 5C must report the refusal, not paper over it."""
    mine, theirs = uuid4(), uuid4()
    my_evidence = _spread_corpus(mine)
    their_evidence = _spread_corpus(theirs)
    foreign_outlier = derive_robust_content_intelligence(
        candidate_id=theirs, evidence=their_evidence
    )
    patterns = derive_content_patterns(
        candidate_id=mine, evidence=my_evidence, outlier_evidence=foreign_outlier
    )
    result = derive_content_experiments(candidate_id=mine, patterns=patterns)

    assert result.experiments
    assert result.outlier_evidence_available is False
    for experiment in result.experiments:
        assert experiment.evidence.cooccurrence_state is (
            CooccurrenceState.OUTLIER_SCOPE_MISMATCH
        )
        assert experiment.evidence.outlier_video_count == 0
        assert experiment.evidence.share_among_outliers is None
        assert "refused" in experiment.hypothesis
        assert experiment.sufficiency is not (
            EvidenceSufficiency.BROAD_WITH_COOCCURRENCE
        )


def test_no_qualifying_outliers_is_distinct_from_unavailable():
    candidate_id = uuid4()
    # Every video at the same view count: nothing sits above its own baseline.
    evidence: list[EvidenceItem] = []
    for index, channel in enumerate(("UC_A", "UC_B", "UC_C", "UC_D")):
        evidence.extend(
            video_evidence(
                f"V{index}", candidate_id=candidate_id, channel_id=channel,
                title="meal plan guide", views=500,
            )
        )
    result = run(evidence, candidate_id)
    states = {e.evidence.cooccurrence_state for e in result.experiments}
    assert CooccurrenceState.OUTLIER_EVIDENCE_UNAVAILABLE not in states
    for experiment in result.experiments:
        assert experiment.sufficiency is not (
            EvidenceSufficiency.BROAD_WITH_COOCCURRENCE
        )


def test_cooccurrence_when_present_is_reported_without_a_performance_claim():
    candidate_id = uuid4()
    result = run(_outlier_corpus(candidate_id), candidate_id)
    reported = [
        e
        for e in result.experiments
        if e.evidence.cooccurrence_state
        in (
            CooccurrenceState.OVER_REPRESENTED,
            CooccurrenceState.UNDER_REPRESENTED,
            CooccurrenceState.COMPARABLE,
        )
    ]
    assert reported, "expected at least one reportable co-occurrence"
    for experiment in reported:
        assert experiment.evidence.share_among_outliers is not None
        assert experiment.evidence.share_in_corpus is not None
        # The two shares stay separate values, never fused.
        assert "co-occurrence in a sample" in experiment.hypothesis
        assert "does not establish" in experiment.hypothesis


# ------------------------------------------------ creator concentration


def test_one_creator_supplying_every_occurrence_is_never_broad_evidence():
    candidate_id = uuid4()
    evidence: list[EvidenceItem] = []
    for index in range(4):
        evidence.extend(
            video_evidence(
                f"V{index}", candidate_id=candidate_id, channel_id="UC_ONLY",
                title="meal plan guide",
            )
        )
    result = run(evidence, candidate_id)
    assert result.experiments
    for experiment in result.experiments:
        assert experiment.evidence.concentration is ConcentrationState.SINGLE_CREATOR
        assert experiment.sufficiency is EvidenceSufficiency.NARROW_CREATOR_BASE
        assert any(
            "one creator's practice and not evidence about the field" in lim
            for lim in experiment.limitations
        )


def test_creator_concentrated_patterns_carry_the_concentration_limitation():
    candidate_id = uuid4()
    evidence: list[EvidenceItem] = []
    for index in range(4):
        evidence.extend(
            video_evidence(
                f"D{index}", candidate_id=candidate_id, channel_id="UC_BIG",
                title="meal plan guide",
            )
        )
    for index, channel in enumerate(("UC_X", "UC_Y")):
        evidence.extend(
            video_evidence(
                f"S{index}", candidate_id=candidate_id, channel_id=channel,
                title="meal plan guide",
            )
        )
    result = run(evidence, candidate_id)
    token = title_experiment(result)
    assert token.evidence.concentration is ConcentrationState.CREATOR_CONCENTRATED
    assert token.sufficiency is EvidenceSufficiency.NARROW_CREATOR_BASE
    assert any("concentrated rather than field-wide" in l for l in token.limitations)


def test_partially_unknown_creators_never_claim_breadth():
    candidate_id = uuid4()
    evidence: list[EvidenceItem] = []
    evidence.extend(
        video_evidence(
            "K1", candidate_id=candidate_id, channel_id="UC_A",
            title="meal plan guide",
        )
    )
    for index in range(6):
        evidence.extend(
            video_evidence(
                f"U{index}", candidate_id=candidate_id, channel_id=None,
                title="meal plan guide",
            )
        )
    result = run(evidence, candidate_id)
    token = title_experiment(result)
    assert token.evidence.concentration is (
        ConcentrationState.CREATOR_PARTIALLY_UNKNOWN
    )
    assert token.sufficiency is EvidenceSufficiency.CREATOR_IDENTITY_INCOMPLETE
    assert token.evidence.videos_without_channel_id == 6
    # Unknown identity cannot inflate the reported dominance.
    assert token.evidence.top_channel_share == pytest.approx(1 / 7, abs=1e-4)
    assert any("lower bound" in l for l in token.limitations)


def test_conflicting_creator_records_stay_distinct_from_absence():
    candidate_id = uuid4()
    evidence: list[EvidenceItem] = []
    evidence.extend(
        video_evidence(
            "SAME", candidate_id=candidate_id, channel_id="UC_A",
            title="meal plan guide", payload_hash="h1",
        )
    )
    evidence.extend(
        video_evidence(
            "SAME", candidate_id=candidate_id, channel_id="UC_B",
            title="meal plan guide", payload_hash="h2",
        )
    )
    evidence.extend(
        video_evidence(
            "OTHER", candidate_id=candidate_id, channel_id="UC_C",
            title="meal plan guide",
        )
    )
    result = run(evidence, candidate_id)
    token = title_experiment(result)
    assert token.evidence.videos_with_conflicting_channel_id == 1
    assert token.evidence.videos_without_channel_id == 0
    assert any("contradictory creator records" in l for l in token.limitations)
    # A conflicting creator is never listed as an attributed creator.
    assert token.evidence.channel_ids == ("UC_C",)


def test_all_creators_unknown_reports_no_share_and_says_so():
    candidate_id = uuid4()
    evidence: list[EvidenceItem] = []
    for index in range(3):
        evidence.extend(
            video_evidence(
                f"V{index}", candidate_id=candidate_id, channel_id=None,
                title="meal plan guide",
            )
        )
    result = run(evidence, candidate_id)
    token = title_experiment(result)
    assert token.evidence.concentration is ConcentrationState.CREATOR_UNKNOWN
    assert token.evidence.top_channel_share is None
    assert token.sufficiency is EvidenceSufficiency.CREATOR_IDENTITY_INCOMPLETE
    assert any("nothing is known about how many creators" in l for l in token.limitations)


def test_broad_evidence_requires_the_creator_floor():
    candidate_id = uuid4()
    evidence: list[EvidenceItem] = []
    for index, channel in enumerate(("UC_A", "UC_B")):
        evidence.extend(
            video_evidence(
                f"V{index}", candidate_id=candidate_id, channel_id=channel,
                title="meal plan guide",
            )
        )
    result = run(evidence, candidate_id)
    token = title_experiment(result)
    assert token.evidence.distinct_channel_count == 2 < MIN_CREATORS_FOR_BROAD_EVIDENCE
    assert token.evidence.concentration is ConcentrationState.MULTI_CREATOR
    assert token.sufficiency is EvidenceSufficiency.NARROW_CREATOR_BASE


# --------------------------------------------------- generation contract


def test_one_pattern_yields_one_experiment():
    candidate_id = uuid4()
    evidence: list[EvidenceItem] = []
    for index, channel in enumerate(("UC_A", "UC_B", "UC_C")):
        evidence.extend(
            video_evidence(
                f"V{index}", candidate_id=candidate_id, channel_id=channel,
                title="solo", tags=_UNSET, category=None, duration_seconds=_UNSET,
            )
        )
    result = run(evidence, candidate_id)
    assert result.generated_experiment_count == 1
    assert result.generation_state is GenerationState.ALL_ELIGIBLE_PATTERNS_USED
    assert result.experiments[0].variable_value == "solo"


def test_fewer_than_the_cap_reports_why_not_more():
    candidate_id = uuid4()
    result = run(_spread_corpus(candidate_id), candidate_id)
    assert 0 < result.generated_experiment_count < MAX_EXPERIMENTS
    assert result.generation_state is GenerationState.ALL_ELIGIBLE_PATTERNS_USED
    # The shortfall is accounted for, not merely short.
    assert (
        result.generated_experiment_count + result.suppressed_as_equivalent_count
        == result.eligible_pattern_count
    )
    assert result.eligible_pattern_count + result.excluded_below_floor_count == (
        result.observed_pattern_count
    )


def test_more_than_the_cap_is_capped_and_says_so():
    candidate_id = uuid4()
    result = run(_many_patterns_corpus(candidate_id), candidate_id)
    assert result.generated_experiment_count == MAX_EXPERIMENTS == 30
    assert result.generation_state is GenerationState.CAPPED_AT_MAXIMUM
    assert result.eligible_pattern_count > MAX_EXPERIMENTS


def test_the_cap_selects_deterministically_not_arbitrarily():
    candidate_id = uuid4()
    evidence = _many_patterns_corpus(candidate_id)
    baseline = [e.experiment_id for e in run(evidence, candidate_id).experiments]
    rng = random.Random(5150)
    for _ in range(40):
        shuffled = list(evidence)
        rng.shuffle(shuffled)
        assert [
            e.experiment_id for e in run(shuffled, candidate_id).experiments
        ] == baseline


def test_co_extensive_variables_remain_distinct_experiments():
    """Observational co-occurrence is NOT experimental equivalence.

    "meal", "plan" and the phrase "meal plan" occur in exactly the same three
    videos here, but a producer manipulates each of them separately: writing
    a title containing "meal" is a different intervention from writing one
    containing "plan", and both differ from requiring the two adjacent. An
    earlier version grouped by (family, observed video set) and suppressed
    two of the three, which mistook a property of the sample for a property
    of the intervention and silently discarded testable variables.
    """
    candidate_id = uuid4()
    evidence: list[EvidenceItem] = []
    for index, channel in enumerate(("UC_A", "UC_B", "UC_C")):
        evidence.extend(
            video_evidence(
                f"V{index}", candidate_id=candidate_id, channel_id=channel,
                title="meal plan", tags=_UNSET, category=None,
                duration_seconds=_UNSET,
            )
        )
    result = run(evidence, candidate_id)

    meal = experiment_for(result, PatternKind.TITLE_TOKEN, "meal")
    plan = experiment_for(result, PatternKind.TITLE_TOKEN, "plan")
    phrase = experiment_for(result, PatternKind.TITLE_BIGRAM, "meal plan")
    assert meal is not None and plan is not None and phrase is not None

    # They really are co-extensive in the observed corpus...
    assert meal.evidence.video_ids == plan.evidence.video_ids
    assert meal.evidence.video_ids == phrase.evidence.video_ids
    # ...and they are still three separate experiments.
    assert result.generated_experiment_count == 3
    assert result.suppressed_as_equivalent_count == 0
    assert len({e.experiment_id for e in (meal, plan, phrase)}) == 3
    for experiment in (meal, plan, phrase):
        assert experiment.equivalent_variables == ()
    # Each carries its own intervention instruction.
    assert "'meal'" in meal.instructions.title_structure
    assert "'plan'" in plan.instructions.title_structure
    assert "'meal plan'" in phrase.instructions.title_structure
    assert "adjacent" in phrase.instructions.title_structure


def test_a_token_is_never_collapsed_into_a_bigram_that_contains_it():
    """Containment is not equivalence, even at identical counts."""
    candidate_id = uuid4()
    evidence: list[EvidenceItem] = []
    for index, channel in enumerate(("UC_A", "UC_B", "UC_C", "UC_D")):
        evidence.extend(
            video_evidence(
                f"V{index}", candidate_id=candidate_id, channel_id=channel,
                title="budget grocery", tags=_UNSET, category=None,
                duration_seconds=_UNSET,
            )
        )
    result = run(evidence, candidate_id)
    values = {(e.variable_kind, e.variable_value) for e in result.experiments}
    assert (PatternKind.TITLE_TOKEN, "budget") in values
    assert (PatternKind.TITLE_TOKEN, "grocery") in values
    assert (PatternKind.TITLE_BIGRAM, "budget grocery") in values


def test_variables_over_different_video_sets_stay_separate_experiments():
    candidate_id = uuid4()
    evidence: list[EvidenceItem] = []
    titles = ["alpha bravo", "alpha bravo", "alpha charlie"]
    for index, (channel, title) in enumerate(zip(("UC_A", "UC_B", "UC_C"), titles)):
        evidence.extend(
            video_evidence(
                f"V{index}", candidate_id=candidate_id, channel_id=channel,
                title=title, tags=_UNSET, category=None, duration_seconds=_UNSET,
            )
        )
    result = run(evidence, candidate_id)
    alpha = experiment_for(result, PatternKind.TITLE_TOKEN, "alpha")
    bravo = experiment_for(result, PatternKind.TITLE_TOKEN, "bravo")
    phrase = experiment_for(result, PatternKind.TITLE_BIGRAM, "alpha bravo")
    # "alpha" covers all three videos; "bravo" and the phrase cover two. The
    # evidence differs and the interventions differ, so all three stand.
    assert alpha.evidence.video_ids == ("V0", "V1", "V2")
    assert bravo.evidence.video_ids == ("V0", "V1")
    assert phrase.evidence.video_ids == ("V0", "V1")
    assert len({alpha.experiment_id, bravo.experiment_id, phrase.experiment_id}) == 3


def test_different_families_over_the_same_videos_are_not_collapsed():
    """A title lever and a duration lever are different experiments."""
    candidate_id = uuid4()
    evidence: list[EvidenceItem] = []
    for index, channel in enumerate(("UC_A", "UC_B", "UC_C")):
        evidence.extend(
            video_evidence(
                f"V{index}", candidate_id=candidate_id, channel_id=channel,
                title="solo", tags=_UNSET, category="Education",
                duration_seconds=600,
            )
        )
    result = run(evidence, candidate_id)
    families = {e.variable_family for e in result.experiments}
    assert VariableFamily.TITLE in families
    assert VariableFamily.CATEGORY in families
    assert VariableFamily.DURATION in families


def test_every_pattern_kind_can_become_an_experiment():
    candidate_id = uuid4()
    evidence: list[EvidenceItem] = []
    for index, channel in enumerate(("UC_A", "UC_B", "UC_C")):
        evidence.extend(
            video_evidence(
                f"V{index}", candidate_id=candidate_id, channel_id=channel,
                title="meal plan", tags=["weekly"], category="Education",
                duration_seconds=600,
            )
        )
    result = run(evidence, candidate_id)
    kinds = {e.variable_kind for e in result.experiments}
    # Title tokens and the bigram collapse (same videos); the other three
    # families each contribute their own experiment.
    assert PatternKind.TAG in kinds
    assert PatternKind.CATEGORY in kinds
    assert PatternKind.DURATION_BAND in kinds


def _synthetic_patterns(candidate_id: UUID, video_count: int):
    """A 5B-shaped result built by hand, to reach 5C's own evidence floor.

    5C accepts a ContentPatternsResult, and its floor defends that input
    rather than trusting whatever 5B's floor happens to be. Today the two
    coincide at 2, so the ordinary pipeline cannot produce a below-floor
    pattern; a caller handing 5C a result directly still can, which is what
    the floor is for.
    """
    from app.services.content_patterns import (
        ContentPattern,
        ContentPatternProvenance,
        ContentPatternsResult,
        OutlierCooccurrence,
        PatternOccurrence,
    )
    from app.services.content_patterns import (
        STATE_BOUNDARIES as PATTERN_BOUNDARIES,
    )

    occurrences = tuple(
        PatternOccurrence(
            video_id=f"V{index}",
            channel_id=f"UC_{index}",
            channel_state=FieldState.AVAILABLE,
            evidence_ids=(uuid4(),),
        )
        for index in range(video_count)
    )
    pattern = ContentPattern(
        kind=PatternKind.TITLE_TOKEN,
        value="lonely",
        video_count=video_count,
        field_available_video_count=4,
        prevalence_share=round(video_count / 4, 4),
        distinct_channel_count=video_count,
        videos_without_channel_id=0,
        videos_with_conflicting_channel_id=0,
        top_channel_share=round(1 / video_count, 4) if video_count else None,
        concentration=ConcentrationState.MULTI_CREATOR,
        occurrences=occurrences,
        evidence_ids=tuple(
            sorted(eid for o in occurrences for eid in o.evidence_ids)
        ),
        cooccurrence=OutlierCooccurrence(
            state=CooccurrenceState.OUTLIER_EVIDENCE_UNAVAILABLE,
            outlier_video_count=0,
            outlier_distinct_channel_count=0,
            comparable_outlier_total=0,
            share_among_outliers=None,
            share_in_corpus=round(video_count / 4, 4),
        ),
    )
    state = ContentPatternsState.PATTERNS_OBSERVED
    return ContentPatternsResult(
        candidate_id=candidate_id,
        research_run_id=None,
        state=DimensionState.EVIDENCE_PRESENT_UNSCORED,
        pattern_state=state,
        state_boundary=PATTERN_BOUNDARIES[state],
        value=None,
        patterns=(pattern,),
        field_availability=(),
        provenance=ContentPatternProvenance(
            candidate_id=candidate_id,
            research_run_id=None,
            evidence_ids=pattern.evidence_ids,
            video_ids=tuple(o.video_id for o in occurrences),
            channel_ids=tuple(sorted(f"UC_{i}" for i in range(video_count))),
            providers=("youtube",),
            platforms=("youtube",),
            source_truth_classes=("OBSERVED",),
            duplicate_evidence_suppressed=0,
        ),
        observed_video_count=4,
        videos_without_channel_id=0,
        videos_with_conflicting_channel_id=0,
        distinct_channel_count=video_count,
        outlier_observation_count=0,
        outlier_distinct_channel_count=0,
        outlier_evidence_available=False,
    )


def test_a_pattern_below_the_experiment_floor_yields_no_eligible_patterns():
    candidate_id = uuid4()
    patterns = _synthetic_patterns(candidate_id, video_count=1)
    assert patterns.patterns[0].video_count < MIN_EXPERIMENT_VIDEO_COUNT

    result = derive_content_experiments(candidate_id=candidate_id, patterns=patterns)
    assert result.experiments_state is ExperimentsState.NO_ELIGIBLE_PATTERNS
    assert result.generation_state is GenerationState.NO_ELIGIBLE_PATTERNS
    assert result.state is DimensionState.UNKNOWN
    assert result.experiments == ()
    assert result.observed_pattern_count == 1
    assert result.excluded_below_floor_count == 1
    assert result.eligible_pattern_count == 0
    assert result.value is None
    assert "not evidence that the patterns are unimportant" in result.state_boundary


def test_a_pattern_at_the_experiment_floor_is_eligible():
    """The floor is inclusive; one more occurrence flips the same input."""
    candidate_id = uuid4()
    patterns = _synthetic_patterns(candidate_id, video_count=MIN_EXPERIMENT_VIDEO_COUNT)
    result = derive_content_experiments(candidate_id=candidate_id, patterns=patterns)
    assert result.experiments_state is ExperimentsState.EXPERIMENTS_GENERATED
    assert result.generated_experiment_count == 1
    assert result.excluded_below_floor_count == 0


def test_every_emitted_state_is_reachable():
    """No state may exist that no input can produce.

    An earlier milestone shipped a classification the upstream derivation
    could never assign, which made every branch past it dead code. This
    sweeps 5C's own enums against real derivations instead.
    """
    reached_sufficiency = set()
    reached_state = set()
    reached_generation = set()
    reached_baseline = set()
    reached_family = set()

    def record(result):
        reached_state.add(result.experiments_state)
        reached_generation.add(result.generation_state)
        for experiment in result.experiments:
            reached_sufficiency.add(experiment.sufficiency)
            reached_baseline.add(experiment.baseline_state)
            reached_family.add(experiment.variable_family)

    outlier_id = uuid4()
    record(run(_outlier_corpus(outlier_id), outlier_id))
    spread_id = uuid4()
    record(run(_spread_corpus(spread_id), spread_id))
    many_id = uuid4()
    record(run(_many_patterns_corpus(many_id), many_id))
    empty_id = uuid4()
    record(run([], empty_id))
    record(run([], empty_id, missing_reasons={PUBLIC_CONTENT: FAILED}))
    single_id, single = corpus(("ONLY", "UC_A", "meal plan"))
    record(run(single, single_id))
    record(derive_content_experiments(candidate_id=uuid4(), patterns=None))
    foreign_id = uuid4()
    record(
        derive_content_experiments(
            candidate_id=uuid4(),
            patterns=patterns_for(_spread_corpus(foreign_id), foreign_id),
        )
    )
    floor_id = uuid4()
    record(
        derive_content_experiments(
            candidate_id=floor_id, patterns=_synthetic_patterns(floor_id, 1)
        )
    )

    solo_id = uuid4()
    solo: list[EvidenceItem] = []
    for index in range(4):
        solo.extend(
            video_evidence(
                f"V{index}", candidate_id=solo_id, channel_id="UC_ONLY",
                title="meal plan guide",
            )
        )
    record(run(solo, solo_id))

    unknown_id = uuid4()
    unknown: list[EvidenceItem] = []
    for index in range(3):
        unknown.extend(
            video_evidence(
                f"V{index}", candidate_id=unknown_id, channel_id=None,
                title="meal plan guide",
            )
        )
    record(run(unknown, unknown_id))

    tagged_id = uuid4()
    tagged: list[EvidenceItem] = []
    for index, channel in enumerate(("UC_A", "UC_B", "UC_C")):
        tagged.extend(
            video_evidence(
                f"V{index}", candidate_id=tagged_id, channel_id=channel,
                title="solo", tags=["weekly"], category="Education",
                duration_seconds=600,
            )
        )
    record(run(tagged, tagged_id))

    assert reached_sufficiency == set(EvidenceSufficiency)
    assert reached_state == set(ExperimentsState)
    assert reached_generation == set(GenerationState)
    assert reached_baseline == set(BaselineState)
    assert reached_family == set(VariableFamily)


# ------------------------------------------------------------- structure


def test_every_experiment_is_completely_specified():
    candidate_id = uuid4()
    result = run(_spread_corpus(candidate_id), candidate_id)
    assert result.experiments
    for experiment in result.experiments:
        assert experiment.experiment_id.startswith("exp_")
        assert experiment.title.startswith("Test ")
        assert experiment.hypothesis
        assert experiment.variable_family in VariableFamily
        assert experiment.variable_kind in PatternKind
        assert experiment.variable_value
        assert experiment.baseline_state in BaselineState
        assert experiment.baseline_definition
        assert experiment.primary_measurement == PRIMARY_MEASUREMENT
        assert str(MIN_TEST_PUBLICATIONS) in experiment.success_criterion
        assert experiment.sufficiency in EvidenceSufficiency
        assert experiment.instructions.hold_constant
        # Lineage reaches back to records and videos.
        assert experiment.evidence.video_ids
        assert experiment.evidence.evidence_ids
        assert experiment.evidence.video_count >= MIN_EXPERIMENT_VIDEO_COUNT


def test_production_instructions_match_the_variable_under_test():
    candidate_id = uuid4()
    evidence: list[EvidenceItem] = []
    for index, channel in enumerate(("UC_A", "UC_B", "UC_C")):
        evidence.extend(
            video_evidence(
                f"V{index}", candidate_id=candidate_id, channel_id=channel,
                title="meal plan", tags=["weekly"], category="Education",
                duration_seconds=600,
            )
        )
    result = run(evidence, candidate_id)

    tag = experiment_for(result, PatternKind.TAG, "weekly")
    assert tag.instructions.tags == ("weekly",)
    assert tag.instructions.title_structure is None
    assert "title structure" in tag.instructions.hold_constant

    category = experiment_for(result, PatternKind.CATEGORY, "education")
    assert category.instructions.format_category == "education"
    assert category.instructions.duration_band is None

    band = experiment_for(
        result, PatternKind.DURATION_BAND, DurationBand.FIVE_TO_15_MIN.value
    )
    assert band.instructions.duration_band is DurationBand.FIVE_TO_15_MIN
    assert band.instructions.duration_seconds_range == (300, 900)
    assert "title structure" in band.instructions.hold_constant

    title = next(e for e in result.experiments if e.variable_family is VariableFamily.TITLE)
    assert title.instructions.title_structure
    assert "duration band" in title.instructions.hold_constant


def test_duration_band_seconds_agree_with_milestone_5b():
    """5C declares its own bounds, so they must be proven consistent with 5B."""
    assert set(DURATION_BAND_SECONDS) == set(DurationBand)
    for band, (lower, upper) in DURATION_BAND_SECONDS.items():
        assert duration_band(lower) is band
        if upper is not None:
            assert duration_band(upper - 1) is band
            assert duration_band(upper) is not band
    # The bands tile the line without gaps.
    ordered = sorted(DURATION_BAND_SECONDS.values(), key=lambda pair: pair[0])
    for (_, upper), (lower, _) in zip(ordered, ordered[1:]):
        assert upper == lower
    assert ordered[0][0] == 0
    assert ordered[-1][1] is None


def test_baseline_reports_a_contrast_only_when_one_exists():
    candidate_id = uuid4()
    # Every video carries the token: no contrast in this evidence.
    same: list[EvidenceItem] = []
    for index, channel in enumerate(("UC_A", "UC_B", "UC_C")):
        same.extend(
            video_evidence(
                f"V{index}", candidate_id=candidate_id, channel_id=channel,
                title="solo", tags=_UNSET, category=None, duration_seconds=_UNSET,
            )
        )
    result = run(same, candidate_id)
    only = result.experiments[0]
    assert only.baseline_state is BaselineState.PRODUCER_HISTORY_REQUIRED
    assert "no contrast" in only.baseline_definition
    assert any("no contrast group" in l for l in only.limitations)

    # A corpus where some videos lack the token does have a contrast.
    mixed_id = uuid4()
    mixed = list(_spread_corpus(mixed_id))
    result2 = run(mixed, mixed_id)
    contrasted = [
        e
        for e in result2.experiments
        if e.baseline_state is BaselineState.OBSERVED_CORPUS_CONTRAST
    ]
    assert contrasted
    assert "not a control group" in contrasted[0].baseline_definition


def test_ordering_follows_the_published_rule():
    candidate_id = uuid4()
    result = run(_spread_corpus(candidate_id), candidate_id)
    ranks = [e.ordering_rank for e in result.experiments]
    assert ranks == list(range(len(result.experiments)))

    from app.services.content_experiments import _SUFFICIENCY_RANK

    keys = [
        (
            _SUFFICIENCY_RANK[e.sufficiency],
            -e.evidence.distinct_channel_count,
            -e.evidence.video_count,
        )
        for e in result.experiments
    ]
    assert keys == sorted(keys)


def test_sufficiency_outranks_creator_breadth_in_the_ordering():
    """Sufficiency is the FIRST ordering key, not a tiebreak after breadth.

    Built so the two disagree: the concentrated pattern has more creators and
    far more occurrences than the broad one, so an ordering that skipped
    sufficiency would put it first.
    """
    candidate_id = uuid4()
    evidence: list[EvidenceItem] = []
    for index in range(6):
        evidence.extend(
            video_evidence(
                f"BIG{index}", candidate_id=candidate_id, channel_id="UC_BIG",
                title="alpha", tags=_UNSET, category=None,
                duration_seconds=_UNSET,
            )
        )
    for index, channel in enumerate(("UC_1", "UC_2", "UC_3")):
        evidence.extend(
            video_evidence(
                f"SM{index}", candidate_id=candidate_id, channel_id=channel,
                title="alpha bravo", tags=_UNSET, category=None,
                duration_seconds=_UNSET,
            )
        )
    result = run(evidence, candidate_id)

    alpha = experiment_for(result, PatternKind.TITLE_TOKEN, "alpha")
    bravo = next(
        e for e in result.experiments if "bravo" in e.variable_value
    )
    # The concentrated pattern is the larger one on every count.
    assert alpha.sufficiency is EvidenceSufficiency.NARROW_CREATOR_BASE
    assert bravo.sufficiency is EvidenceSufficiency.BROAD_PREVALENCE_ONLY
    assert alpha.evidence.distinct_channel_count > bravo.evidence.distinct_channel_count
    assert alpha.evidence.video_count > bravo.evidence.video_count
    # Sufficiency still decides the order.
    assert bravo.ordering_rank < alpha.ordering_rank


def _tag_pattern(value: str, channels: list[str]):
    """A TAG pattern whose occurrences come from the given creators."""
    from app.services.content_patterns import (
        ContentPattern,
        OutlierCooccurrence,
        PatternOccurrence,
    )

    occurrences = tuple(
        PatternOccurrence(
            video_id=f"V{index}_{channel}",
            channel_id=channel,
            channel_state=FieldState.AVAILABLE,
            evidence_ids=(uuid4(),),
        )
        for index, channel in enumerate(channels)
    )
    counts = {c: channels.count(c) for c in set(channels)}
    return ContentPattern(
        kind=PatternKind.TAG,
        value=value,
        video_count=len(channels),
        field_available_video_count=len(channels),
        prevalence_share=1.0,
        distinct_channel_count=len(counts),
        videos_without_channel_id=0,
        videos_with_conflicting_channel_id=0,
        top_channel_share=round(max(counts.values()) / len(channels), 4),
        concentration=(
            ConcentrationState.SINGLE_CREATOR
            if len(counts) == 1
            else ConcentrationState.MULTI_CREATOR
        ),
        occurrences=occurrences,
        evidence_ids=tuple(
            sorted(eid for o in occurrences for eid in o.evidence_ids)
        ),
        cooccurrence=OutlierCooccurrence(
            state=CooccurrenceState.OUTLIER_EVIDENCE_UNAVAILABLE,
            outlier_video_count=0,
            outlier_distinct_channel_count=0,
            comparable_outlier_total=0,
            share_among_outliers=None,
            share_in_corpus=1.0,
        ),
    )


def _result_with(candidate_id: UUID, patterns_tuple):
    from app.services.content_patterns import (
        ContentPatternProvenance,
        ContentPatternsResult,
    )
    from app.services.content_patterns import (
        STATE_BOUNDARIES as PATTERN_BOUNDARIES,
    )

    state = ContentPatternsState.PATTERNS_OBSERVED
    evidence_ids = tuple(
        sorted({eid for p in patterns_tuple for eid in p.evidence_ids})
    )
    return ContentPatternsResult(
        candidate_id=candidate_id, research_run_id=None,
        state=DimensionState.EVIDENCE_PRESENT_UNSCORED, pattern_state=state,
        state_boundary=PATTERN_BOUNDARIES[state], value=None,
        patterns=patterns_tuple, field_availability=(),
        provenance=ContentPatternProvenance(
            candidate_id=candidate_id, research_run_id=None,
            evidence_ids=evidence_ids,
            video_ids=tuple(
                sorted({o.video_id for p in patterns_tuple for o in p.occurrences})
            ),
            channel_ids=(), providers=("youtube",), platforms=("youtube",),
            source_truth_classes=("OBSERVED",), duplicate_evidence_suppressed=0,
        ),
        observed_video_count=4, videos_without_channel_id=0,
        videos_with_conflicting_channel_id=0, distinct_channel_count=3,
        outlier_observation_count=0, outlier_distinct_channel_count=0,
        outlier_evidence_available=False,
    )


def test_only_an_identical_intervention_is_suppressed():
    """Suppression fires on the canonicalized intervention, not the evidence.

    Two tag spellings that canonicalize to the same value would produce
    byte-identical production instructions, so they are one experiment. This
    is the only thing that collapses; different values never do.
    """
    candidate_id = uuid4()
    narrow = _tag_pattern("meal plan", ["UC_A", "UC_A"])
    broad = _tag_pattern("Meal  Plan", ["UC_A", "UC_B", "UC_C"])
    # Same canonical intervention, different raw spelling.
    from app.services.content_experiments import _canonical_value, _intervention_key

    assert _canonical_value(narrow.kind, narrow.value) == _canonical_value(
        broad.kind, broad.value
    )
    assert _intervention_key(narrow.kind, narrow.value) == _intervention_key(
        broad.kind, broad.value
    )

    # The weaker one arrives FIRST, so arrival order would pick it.
    patterns = _result_with(candidate_id, (narrow, broad))
    result = derive_content_experiments(candidate_id=candidate_id, patterns=patterns)

    assert result.generated_experiment_count == 1
    assert result.suppressed_as_equivalent_count == 1
    retained = result.experiments[0]
    # The published rule prefers the broader evidence, not the first arrival.
    assert retained.sufficiency is EvidenceSufficiency.BROAD_PREVALENCE_ONLY
    assert retained.evidence.distinct_channel_count == 3
    # The intervention is stated canonically, not in whichever raw spelling
    # became the representative.
    assert retained.variable_value == "meal plan"
    assert retained.instructions.tags == ("meal plan",)
    # The raw observed spelling survives as lineage on the evidence.
    assert retained.evidence.pattern_value == "Meal  Plan"
    assert [(e.kind, e.value) for e in retained.equivalent_variables] == [
        (PatternKind.TAG, "meal plan")
    ]


def test_collapsed_spellings_emit_one_canonical_intervention_and_one_id():
    """Suppression is only honest if the survivor states the canonical form.

    The contract is that two candidates collapse when they would produce
    byte-identical production instructions. An earlier version canonicalized
    only the GROUPING KEY and then emitted the representative's raw value, so
    the same pair could ship ("meal plan",) or ("Meal Plan",) depending on
    which spelling carried the stronger evidence — contradicting the very
    contract that justified dropping one of them.
    """
    lower = "meal plan"
    upper = "Meal  Plan"
    emitted = {}

    # Four combinations: which spelling is stronger x which arrives first.
    for stronger, weaker in ((lower, upper), (upper, lower)):
        for order in ("forward", "reversed"):
            candidate_id = UUID("11111111-1111-4111-8111-111111111111")
            strong = _tag_pattern(stronger, ["UC_A", "UC_B", "UC_C"])
            weak = _tag_pattern(weaker, ["UC_A", "UC_A"])
            supplied = (weak, strong) if order == "forward" else (strong, weak)
            result = derive_content_experiments(
                candidate_id=candidate_id,
                patterns=_result_with(candidate_id, supplied),
            )
            # (1) one intervention
            assert result.generated_experiment_count == 1
            assert result.suppressed_as_equivalent_count == 1
            experiment = result.experiments[0]
            emitted[(stronger, order)] = (
                experiment.experiment_id,
                experiment.variable_value,
                experiment.title,
                experiment.instructions,
            )

    # (2) identical canonical instructions regardless of which is stronger,
    # (3) identical experiment id regardless of which became representative,
    # (4) and identical under reversed input.
    assert len(set(emitted.values())) == 1
    experiment_id, value, title, instructions = next(iter(emitted.values()))
    assert value == "meal plan"
    assert instructions.tags == ("meal plan",)
    assert "'meal plan'" in title
    assert experiment_id == _experiment_id_for(
        UUID("11111111-1111-4111-8111-111111111111"), PatternKind.TAG, "meal plan"
    )


def test_the_lineage_of_a_collapsed_intervention_is_order_independent():
    """When co-canonical candidates tie on every ordering input, raw breaks it.

    The emitted intervention is canonical either way, so the id and the
    instructions cannot move. What CAN move is the evidence and lineage
    attached to the survivor — and if the final tie-break used the canonical
    value the two would tie completely, leaving `sorted` to fall back to
    input order.
    """
    candidate_id = uuid4()
    # Identical evidence strength: they differ only in raw spelling.
    lower = _tag_pattern("meal plan", ["UC_A", "UC_B", "UC_C"])
    upper = _tag_pattern("Meal  Plan", ["UC_A", "UC_B", "UC_C"])

    def retained(patterns_tuple):
        result = derive_content_experiments(
            candidate_id=candidate_id,
            patterns=_result_with(candidate_id, patterns_tuple),
        )
        assert result.generated_experiment_count == 1
        assert result.suppressed_as_equivalent_count == 1
        experiment = result.experiments[0]
        return (
            experiment.experiment_id,
            experiment.variable_value,
            experiment.evidence.pattern_value,
            tuple((e.kind, e.value) for e in experiment.equivalent_variables),
        )

    forward = retained((lower, upper))
    reverse = retained((upper, lower))
    assert forward == reverse
    # The intervention is canonical; the lineage names one fixed raw spelling.
    assert forward[1] == "meal plan"
    assert forward[2] in {"meal plan", "Meal  Plan"}


def _experiment_id_for(candidate_id: UUID, kind: PatternKind, canonical: str) -> str:
    from app.services.content_experiments import _experiment_id

    return _experiment_id(candidate_id, None, kind, canonical)


def test_a_duration_band_is_an_identifier_and_is_not_case_folded():
    """Canonicalization must not corrupt a value whose case carries meaning."""
    from app.services.content_experiments import _canonical_value

    assert (
        _canonical_value(PatternKind.DURATION_BAND, "FIVE_TO_15_MIN")
        == "FIVE_TO_15_MIN"
    )
    # A lower-cased spelling canonicalizes UP to the real identifier, so it
    # still resolves to a band rather than to a name no band answers to.
    assert (
        _canonical_value(PatternKind.DURATION_BAND, "five_to_15_min")
        == "FIVE_TO_15_MIN"
    )
    assert DurationBand(
        _canonical_value(PatternKind.DURATION_BAND, "five_to_15_min")
    ) is DurationBand.FIVE_TO_15_MIN
    # Prose values still fold.
    assert _canonical_value(PatternKind.TAG, "Meal  Plan") == "meal plan"
    assert _canonical_value(PatternKind.TITLE_TOKEN, "MEAL") == "meal"

    # And the emitted band experiment keeps the identifier intact end to end.
    candidate_id = uuid4()
    evidence: list[EvidenceItem] = []
    for index, channel in enumerate(("UC_A", "UC_B", "UC_C")):
        evidence.extend(
            video_evidence(
                f"V{index}", candidate_id=candidate_id, channel_id=channel,
                title=None, tags=_UNSET, category=None, duration_seconds=600,
            )
        )
    result = run(evidence, candidate_id)
    band = experiment_for(
        result, PatternKind.DURATION_BAND, DurationBand.FIVE_TO_15_MIN.value
    )
    assert band is not None
    assert band.instructions.duration_band is DurationBand.FIVE_TO_15_MIN
    assert band.instructions.duration_seconds_range == (300, 900)


def test_a_token_and_a_bigram_are_different_interventions_even_when_equal():
    """The kind is part of the intervention identity, not decoration.

    5B's tokenizer splits on whitespace, so a token value can never equal a
    bigram value and the kind component of the key never discriminates in
    the ordinary pipeline. 5C accepts a result object, though, and the key
    must be correct for its input type rather than for one producer's
    current tokenization: "include this word" and "include these two words
    adjacently" stay different instructions whatever the values look like.
    """
    from app.services.content_patterns import (
        ContentPattern,
        OutlierCooccurrence,
        PatternOccurrence,
    )
    from app.services.content_experiments import _intervention_key

    candidate_id = uuid4()
    occurrences = tuple(
        PatternOccurrence(
            video_id=f"V{index}", channel_id=f"UC_{index}",
            channel_state=FieldState.AVAILABLE, evidence_ids=(uuid4(),),
        )
        for index in range(3)
    )

    def make(kind: PatternKind) -> ContentPattern:
        return ContentPattern(
            kind=kind, value="meal plan", video_count=3,
            field_available_video_count=3, prevalence_share=1.0,
            distinct_channel_count=3, videos_without_channel_id=0,
            videos_with_conflicting_channel_id=0, top_channel_share=0.3334,
            concentration=ConcentrationState.MULTI_CREATOR,
            occurrences=occurrences,
            evidence_ids=tuple(
                sorted(eid for o in occurrences for eid in o.evidence_ids)
            ),
            cooccurrence=OutlierCooccurrence(
                state=CooccurrenceState.OUTLIER_EVIDENCE_UNAVAILABLE,
                outlier_video_count=0, outlier_distinct_channel_count=0,
                comparable_outlier_total=0, share_among_outliers=None,
                share_in_corpus=1.0,
            ),
        )

    token = make(PatternKind.TITLE_TOKEN)
    bigram = make(PatternKind.TITLE_BIGRAM)
    # Identical family and identical value; only the kind differs.
    assert token.value == bigram.value
    assert _intervention_key(token.kind, token.value) != _intervention_key(
        bigram.kind, bigram.value
    )

    result = derive_content_experiments(
        candidate_id=candidate_id,
        patterns=_result_with(candidate_id, (token, bigram)),
    )
    assert result.generated_experiment_count == 2
    assert result.suppressed_as_equivalent_count == 0
    kinds = {e.variable_kind for e in result.experiments}
    assert kinds == {PatternKind.TITLE_TOKEN, PatternKind.TITLE_BIGRAM}
    # And they carry different production instructions.
    instructions = {e.instructions.title_structure for e in result.experiments}
    assert len(instructions) == 2
    assert any("adjacent" in i for i in instructions)


def test_different_tag_values_are_never_suppressed():
    candidate_id = uuid4()
    patterns = _result_with(
        candidate_id,
        (
            _tag_pattern("weekly", ["UC_A", "UC_B", "UC_C"]),
            _tag_pattern("budget", ["UC_A", "UC_B", "UC_C"]),
        ),
    )
    result = derive_content_experiments(candidate_id=candidate_id, patterns=patterns)
    assert result.generated_experiment_count == 2
    assert result.suppressed_as_equivalent_count == 0


def test_a_conflicting_creator_is_never_counted_as_an_attributed_creator():
    """5B resolves a conflicting field to None; 5C relies on that and re-checks.

    The `channel_state is AVAILABLE` test is belt-and-braces over 5B's own
    guarantee that a CONFLICTING channel resolves to None. The guarantee is
    pinned here so the redundancy stays a redundancy rather than quietly
    becoming the only thing holding the line.
    """
    candidate_id = uuid4()
    evidence: list[EvidenceItem] = []
    evidence.extend(
        video_evidence(
            "SAME", candidate_id=candidate_id, channel_id="UC_A",
            title="meal plan guide", payload_hash="h1",
        )
    )
    evidence.extend(
        video_evidence(
            "SAME", candidate_id=candidate_id, channel_id="UC_B",
            title="meal plan guide", payload_hash="h2",
        )
    )
    evidence.extend(
        video_evidence(
            "OTHER", candidate_id=candidate_id, channel_id="UC_C",
            title="meal plan guide",
        )
    )
    patterns = patterns_for(evidence, candidate_id)
    conflicting = [
        o
        for p in patterns.patterns
        for o in p.occurrences
        if o.channel_state is FieldState.CONFLICTING
    ]
    assert conflicting
    # 5B's guarantee, which 5C's attribution filter depends on.
    assert all(o.channel_id is None for o in conflicting)

    result = derive_content_experiments(candidate_id=candidate_id, patterns=patterns)
    experiment = title_experiment(result)
    assert "UC_A" not in experiment.evidence.channel_ids
    assert "UC_B" not in experiment.evidence.channel_ids
    assert experiment.evidence.channel_ids == ("UC_C",)


def test_emitted_identifiers_survive_sentence_casing():
    """Band names are identifiers, not prose, and must not be case-folded."""
    candidate_id = uuid4()
    evidence: list[EvidenceItem] = []
    for index, channel in enumerate(("UC_A", "UC_B", "UC_C")):
        evidence.extend(
            video_evidence(
                f"V{index}", candidate_id=candidate_id, channel_id=channel,
                title=None, tags=_UNSET, category=None, duration_seconds=600,
            )
        )
    result = run(evidence, candidate_id)
    band = experiment_for(
        result, PatternKind.DURATION_BAND, DurationBand.FIVE_TO_15_MIN.value
    )
    assert band is not None
    # The exact identifier appears in the title and the hypothesis.
    assert "FIVE_TO_15_MIN" in band.title
    assert "FIVE_TO_15_MIN" in band.hypothesis
    assert "five_to_15_min" not in band.hypothesis


def test_sufficiency_is_a_state_not_a_number():
    candidate_id = uuid4()
    result = run(_spread_corpus(candidate_id), candidate_id)
    for experiment in result.experiments:
        assert isinstance(experiment.sufficiency, EvidenceSufficiency)
        assert isinstance(experiment.sufficiency.value, str)


# ------------------------------------------------- success criterion


def test_the_success_criterion_needs_the_publication_floor():
    """Below the floor there is no decision — and no decision is not a stop."""
    for count in range(MIN_TEST_PUBLICATIONS):
        outcomes = [PublicationOutcome.AT_OR_ABOVE_BASELINE] * count
        assert evaluate_success_criterion(outcomes) is (
            ExperimentDecision.INSUFFICIENT_PUBLICATIONS
        ), count
    # Even unanimously good results cannot decide below the floor.
    short = [PublicationOutcome.AT_OR_ABOVE_BASELINE] * (MIN_TEST_PUBLICATIONS - 1)
    assert evaluate_success_criterion(short) is not ExperimentDecision.CONTINUE
    assert evaluate_success_criterion(short) is not ExperimentDecision.STOP
    # One more known outcome reaches a decision.
    assert evaluate_success_criterion(
        short + [PublicationOutcome.AT_OR_ABOVE_BASELINE]
    ) is ExperimentDecision.CONTINUE


def test_the_success_criterion_boundary_is_strictly_more_than_half():
    above = PublicationOutcome.AT_OR_ABOVE_BASELINE
    below = PublicationOutcome.BELOW_BASELINE

    # Five known outcomes: three above is a majority, two is not.
    assert evaluate_success_criterion([above] * 3 + [below] * 2) is (
        ExperimentDecision.CONTINUE
    )
    assert evaluate_success_criterion([above] * 2 + [below] * 3) is (
        ExperimentDecision.STOP
    )
    # Six known outcomes: an exact half stops.
    assert evaluate_success_criterion([above] * 3 + [below] * 3) is (
        ExperimentDecision.STOP
    )
    assert evaluate_success_criterion([above] * 4 + [below] * 2) is (
        ExperimentDecision.CONTINUE
    )
    # The extremes.
    assert evaluate_success_criterion([above] * 5) is ExperimentDecision.CONTINUE
    assert evaluate_success_criterion([below] * 5) is ExperimentDecision.STOP


def test_an_unknown_outcome_is_excluded_never_counted_as_below():
    """Missing evidence must not become negative evidence."""
    above = PublicationOutcome.AT_OR_ABOVE_BASELINE
    below = PublicationOutcome.BELOW_BASELINE
    unknown = PublicationOutcome.UNKNOWN

    # Three above, two below, plus unknowns: the unknowns change nothing.
    decided = [above] * 3 + [below] * 2
    assert evaluate_success_criterion(decided) is ExperimentDecision.CONTINUE
    assert evaluate_success_criterion(decided + [unknown] * 4) is (
        ExperimentDecision.CONTINUE
    )
    # Had unknowns counted as below, this would flip to STOP; it must not.
    assert evaluate_success_criterion(decided + [unknown] * 10) is (
        ExperimentDecision.CONTINUE
    )
    # Unknowns do not count toward the floor either, so they delay a decision
    # rather than forcing one.
    assert evaluate_success_criterion([above] * 4 + [unknown] * 20) is (
        ExperimentDecision.INSUFFICIENT_PUBLICATIONS
    )
    assert evaluate_success_criterion([unknown] * 50) is (
        ExperimentDecision.INSUFFICIENT_PUBLICATIONS
    )


def test_the_success_criterion_is_order_independent():
    """The decision is a count over a set, so permutation cannot move it."""
    rng = random.Random(20260912)
    pool = (
        [PublicationOutcome.AT_OR_ABOVE_BASELINE] * 4
        + [PublicationOutcome.BELOW_BASELINE] * 3
        + [PublicationOutcome.UNKNOWN] * 2
    )
    baseline = evaluate_success_criterion(pool)
    assert baseline is ExperimentDecision.CONTINUE
    for _ in range(300):
        shuffled = list(pool)
        rng.shuffle(shuffled)
        assert evaluate_success_criterion(shuffled) is baseline

    # And for a set that stops.
    stopping = (
        [PublicationOutcome.AT_OR_ABOVE_BASELINE] * 2
        + [PublicationOutcome.BELOW_BASELINE] * 4
        + [PublicationOutcome.UNKNOWN] * 3
    )
    assert evaluate_success_criterion(stopping) is ExperimentDecision.STOP
    for _ in range(300):
        shuffled = list(stopping)
        rng.shuffle(shuffled)
        assert evaluate_success_criterion(shuffled) is ExperimentDecision.STOP


def test_the_emitted_wording_and_the_implementation_agree():
    """The rule a producer reads must be the rule the code applies."""
    candidate_id = uuid4()
    result = run(_spread_corpus(candidate_id), candidate_id)
    assert result.experiments
    text = result.experiments[0].success_criterion

    # Every clause of the implementation appears in the emitted rule.
    assert str(MIN_TEST_PUBLICATIONS) in text
    assert "strictly more than half" in text
    assert "an exact half stops" in text
    assert "excluded from the count" in text
    assert "never counted as below" in text
    assert "yields no decision, which is not a stop" in text
    assert "does not depend on the order" in text
    assert "unvalidated V1 assumptions" in text
    # And it still refuses to predict.
    assert "not a prediction" in text

    # Every experiment carries the identical rule.
    assert {e.success_criterion for e in result.experiments} == {text}

    # The wording's own worked example holds in the implementation.
    above = PublicationOutcome.AT_OR_ABOVE_BASELINE
    below = PublicationOutcome.BELOW_BASELINE
    half = [above] * MIN_TEST_PUBLICATIONS + [below] * MIN_TEST_PUBLICATIONS
    assert evaluate_success_criterion(half) is ExperimentDecision.STOP


def test_the_decision_rule_emits_no_score_and_no_prediction():
    for decision in ExperimentDecision:
        assert isinstance(decision.value, str)
        assert not any(
            ch.isdigit() for ch in decision.value
        ), decision
    for outcome in PublicationOutcome:
        assert isinstance(outcome.value, str)


# ------------------------------------------------------- experiment ids


def test_experiment_ids_are_stable_and_scope_bound():
    candidate_id = uuid4()
    evidence = _spread_corpus(candidate_id)
    first = run(evidence, candidate_id)
    second = run(evidence, candidate_id)
    assert [e.experiment_id for e in first.experiments] == [
        e.experiment_id for e in second.experiments
    ]
    # Same variable, different candidate -> different id.
    other_id = uuid4()
    other = run(_spread_corpus(other_id), other_id)
    assert not (
        {e.experiment_id for e in first.experiments}
        & {e.experiment_id for e in other.experiments}
    )


def test_an_experiment_id_does_not_move_when_other_experiments_change():
    """Ids must be derived from the variable, never from position."""
    candidate_id = uuid4()
    base: list[EvidenceItem] = []
    for index, channel in enumerate(("UC_A", "UC_B", "UC_C")):
        base.extend(
            video_evidence(
                f"V{index}", candidate_id=candidate_id, channel_id=channel,
                title="meal plan", tags=_UNSET, category=None,
                duration_seconds=_UNSET,
            )
        )
    before = run(base, candidate_id)
    target = before.experiments[0]

    extra = list(base)
    for index, channel in enumerate(("UC_D", "UC_E", "UC_F")):
        extra.extend(
            video_evidence(
                f"W{index}", candidate_id=candidate_id, channel_id=channel,
                title="budget grocery haul", tags=_UNSET, category=None,
                duration_seconds=_UNSET,
            )
        )
    after = run(extra, candidate_id)
    assert len(after.experiments) > len(before.experiments)
    survivor = next(
        e for e in after.experiments if e.variable_value == target.variable_value
    )
    assert survivor.experiment_id == target.experiment_id


def test_experiment_ids_are_unique_within_a_result():
    candidate_id = uuid4()
    result = run(_many_patterns_corpus(candidate_id), candidate_id)
    ids = [e.experiment_id for e in result.experiments]
    assert len(ids) == len(set(ids))


# ------------------------------------------------------------ determinism


def _identity(result) -> tuple:
    """Everything material about a result, for permutation comparison."""
    return (
        result.state,
        result.experiments_state,
        result.generation_state,
        result.missing_reason,
        result.observed_pattern_count,
        result.eligible_pattern_count,
        result.excluded_below_floor_count,
        result.suppressed_as_equivalent_count,
        result.generated_experiment_count,
        result.outlier_evidence_available,
        result.provenance.evidence_ids,
        result.provenance.video_ids,
        result.provenance.channel_ids,
        result.provenance.patterns_state,
        tuple(
            (
                e.experiment_id,
                e.ordering_rank,
                e.title,
                e.hypothesis,
                e.variable_family,
                e.variable_kind,
                e.variable_value,
                e.baseline_state,
                e.baseline_definition,
                e.sufficiency,
                e.limitations,
                e.success_criterion,
                e.primary_measurement,
                tuple(
                    (q.kind, q.value, q.video_count) for q in e.equivalent_variables
                ),
                (
                    e.instructions.title_structure,
                    e.instructions.format_category,
                    e.instructions.duration_band,
                    e.instructions.duration_seconds_range,
                    e.instructions.tags,
                    e.instructions.hold_constant,
                ),
                (
                    e.evidence.pattern_kind,
                    e.evidence.pattern_value,
                    e.evidence.video_count,
                    e.evidence.field_available_video_count,
                    e.evidence.prevalence_share,
                    e.evidence.distinct_channel_count,
                    e.evidence.videos_without_channel_id,
                    e.evidence.videos_with_conflicting_channel_id,
                    e.evidence.top_channel_share,
                    e.evidence.concentration,
                    e.evidence.cooccurrence_state,
                    e.evidence.outlier_video_count,
                    e.evidence.outlier_distinct_channel_count,
                    e.evidence.comparable_outlier_total,
                    e.evidence.share_among_outliers,
                    e.evidence.share_in_corpus,
                    e.evidence.video_ids,
                    e.evidence.channel_ids,
                    e.evidence.evidence_ids,
                ),
            )
            for e in result.experiments
        ),
    )


def _permutation_corpus(candidate_id: UUID) -> list[EvidenceItem]:
    """Conflicts, duplicates, absences and Unicode in one corpus."""
    evidence = list(_spread_corpus(candidate_id))
    # Disagreeing OBSERVED records for one video.
    evidence.extend(
        video_evidence(
            "CONFLICT", candidate_id=candidate_id, channel_id="UC_A",
            title="meal plan alpha", payload_hash="c1",
        )
    )
    evidence.extend(
        video_evidence(
            "CONFLICT", candidate_id=candidate_id, channel_id="UC_B",
            title="meal plan beta", payload_hash="c2",
        )
    )
    # Byte-identical duplicate rows with distinct evidence ids.
    evidence.extend(
        video_evidence(
            "DUPE", candidate_id=candidate_id, channel_id="UC_C",
            title="meal plan guide",
            item_id=UUID("00000000-0000-4000-8000-00000000000a"),
        )
    )
    evidence.extend(
        video_evidence(
            "DUPE", candidate_id=candidate_id, channel_id="UC_C",
            title="meal plan guide",
            item_id=UUID("ffffffff-ffff-4fff-8fff-ffffffffffff"),
        )
    )
    # No creator recorded.
    evidence.extend(
        video_evidence(
            "NOCHAN", candidate_id=candidate_id, channel_id=None,
            title="meal plan guide",
        )
    )
    # Missing metadata.
    evidence.extend(
        video_evidence(
            "BARE", candidate_id=candidate_id, channel_id="UC_D", title=None,
            tags=_UNSET, category=None, duration_seconds=_UNSET,
        )
    )
    # Unicode.
    evidence.extend(
        video_evidence(
            "UNI1", candidate_id=candidate_id, channel_id="UC_E",
            title="café planning 日本語",
        )
    )
    evidence.extend(
        video_evidence(
            "UNI2", candidate_id=candidate_id, channel_id="UC_F",
            title="café planning 日本語",
        )
    )
    # A record that is not OBSERVED but whose payload carries values.
    evidence.extend(
        video_evidence(
            "UNKNOWN", candidate_id=candidate_id, channel_id="UC_G",
            title="meal plan guide", truth_class=TruthClass.UNKNOWN,
        )
    )
    return evidence


def test_output_is_identical_under_500_arrival_order_permutations():
    candidate_id = uuid4()
    evidence = _permutation_corpus(candidate_id)
    first = run(evidence, candidate_id)
    assert first.experiments
    baseline = _identity(first)
    rng = random.Random(20260912)
    mismatches = 0
    for _ in range(500):
        shuffled = list(evidence)
        rng.shuffle(shuffled)
        if _identity(run(shuffled, candidate_id)) != baseline:
            mismatches += 1
    assert mismatches == 0


def test_reversed_and_repeated_input_are_identical():
    candidate_id = uuid4()
    evidence = _permutation_corpus(candidate_id)
    baseline = _identity(run(evidence, candidate_id))
    assert _identity(run(list(reversed(evidence)), candidate_id)) == baseline
    assert _identity(run(evidence + evidence, candidate_id)) == baseline


def test_duplicate_fingerprints_with_distinct_ids_are_order_independent():
    candidate_id = uuid4()
    small = UUID("00000000-0000-4000-8000-00000000000a")
    large = UUID("ffffffff-ffff-4fff-8fff-ffffffffffff")
    evidence: list[EvidenceItem] = []
    for item_id in (large, small):
        evidence.extend(
            video_evidence(
                "SAME", candidate_id=candidate_id, channel_id="UC_A",
                title="meal plan guide", item_id=item_id,
            )
        )
    evidence.extend(
        video_evidence(
            "PEER", candidate_id=candidate_id, channel_id="UC_B",
            title="meal plan guide",
        )
    )
    forward = run(evidence, candidate_id)
    reverse = run(list(reversed(evidence)), candidate_id)
    assert _identity(forward) == _identity(reverse)
    # The retained lineage is the smaller id, in both directions.
    token = title_experiment(forward)
    assert small in token.evidence.evidence_ids
    assert large not in token.evidence.evidence_ids


def test_repeated_identical_evidence_does_not_inflate_an_experiment():
    candidate_id = uuid4()
    evidence: list[EvidenceItem] = []
    for index, channel in enumerate(("UC_A", "UC_B", "UC_C")):
        evidence.extend(
            video_evidence(
                f"V{index}", candidate_id=candidate_id, channel_id=channel,
                title="meal plan guide",
            )
        )
    once = run(evidence, candidate_id)
    twice = run(evidence + evidence, candidate_id)
    token_once = title_experiment(once)
    token_twice = title_experiment(twice)
    assert token_once.evidence.video_count == token_twice.evidence.video_count == 3
    assert (
        token_once.evidence.distinct_channel_count
        == token_twice.evidence.distinct_channel_count
        == 3
    )


def test_unicode_patterns_survive_into_experiments():
    candidate_id = uuid4()
    evidence: list[EvidenceItem] = []
    for index, channel in enumerate(("UC_A", "UC_B", "UC_C")):
        evidence.extend(
            video_evidence(
                f"V{index}", candidate_id=candidate_id, channel_id=channel,
                title="café 日本語 planning", tags=_UNSET, category=None,
                duration_seconds=_UNSET,
            )
        )
    result = run(evidence, candidate_id)
    values = {e.variable_value for e in result.experiments}
    joined = " ".join(values)
    assert "café" in joined
    assert "日本語" in joined
    for experiment in result.experiments:
        assert experiment.variable_value in experiment.hypothesis


def test_a_payload_behind_an_unknown_record_never_reaches_an_experiment():
    candidate_id = uuid4()
    evidence: list[EvidenceItem] = []
    for index, channel in enumerate(("UC_A", "UC_B", "UC_C")):
        evidence.extend(
            video_evidence(
                f"V{index}", candidate_id=candidate_id, channel_id=channel,
                title="meal plan guide",
            )
        )
    for index in range(3):
        evidence.extend(
            video_evidence(
                f"HIDDEN{index}", candidate_id=candidate_id, channel_id="UC_Z",
                title="secret unread token", truth_class=TruthClass.UNKNOWN,
            )
        )
    result = run(evidence, candidate_id)
    emitted = " ".join(
        [e.variable_value for e in result.experiments]
        + [e.hypothesis for e in result.experiments]
    )
    assert "secret" not in emitted
    assert "unread" not in emitted
    assert "meal" in title_experiment(result).variable_value


# --------------------------------------------------------------- boundaries


def test_no_path_ever_claims_the_dimension_is_scored():
    candidate_id = uuid4()
    populated = run(_spread_corpus(candidate_id), candidate_id)
    assert populated.experiments
    assert populated.state is DimensionState.EVIDENCE_PRESENT_UNSCORED

    single_id, single = corpus(("ONLY", "UC_A", "meal plan"))
    foreign_id = uuid4()
    paths = [
        populated,
        run([], candidate_id),
        run(single, single_id),
        run([], candidate_id, missing_reasons={PUBLIC_CONTENT: FAILED}),
        derive_content_experiments(candidate_id=foreign_id, patterns=None),
        derive_content_experiments(
            candidate_id=foreign_id,
            patterns=patterns_for(_spread_corpus(candidate_id), candidate_id),
        ),
    ]
    for result in paths:
        assert result.state is not DimensionState.SCORED
        assert result.value is None
    assert {r.state for r in paths} == {
        DimensionState.EVIDENCE_PRESENT_UNSCORED,
        DimensionState.UNKNOWN,
        DimensionState.MISSING,
    }


def test_derived_experiments_are_inferred_never_observed():
    candidate_id = uuid4()
    evidence = _spread_corpus(candidate_id)
    assert all(
        item.truth_class is TruthClass.OBSERVED
        for item in evidence
        if item.signal_type == "public_content_observation"
    )
    populated = run(evidence, candidate_id)
    assert populated.experiments
    assert populated.features_truth_class is TruthClass.INFERRED

    single_id, single = corpus(("ONLY", "UC_A", "meal plan"))
    lonely = run(single, single_id)
    assert lonely.features_truth_class is None

    absent = derive_content_experiments(candidate_id=uuid4(), patterns=None)
    assert absent.features_truth_class is None

    for result in (populated, lonely, absent):
        assert result.features_truth_class is not TruthClass.OBSERVED


def test_no_numeric_score_of_any_kind_is_emitted():
    candidate_id = uuid4()
    result = run(_spread_corpus(candidate_id), candidate_id)
    assert result.value is None
    assert result.dimension_name == DIM_CONTENT_EXPERIMENTS
    assert result.dimension_name == "preliminary_content_experiments"

    from app.services.content_experiments import (
        ContentExperiment,
        ContentExperimentsResult,
        ExperimentEvidence,
        ProductionInstructions,
    )

    banned = ("score", "rank", "strength", "quality", "lift", "index",
              "grade", "tier", "confidence", "rating", "weight")
    for cls in (
        ContentExperimentsResult,
        ContentExperiment,
        ExperimentEvidence,
        ProductionInstructions,
    ):
        for field in cls.__dataclass_fields__:
            # `ordering_rank` is the published ordering position, and the
            # test below pins it to a contiguous index rather than a score.
            if field == "ordering_rank":
                continue
            assert not any(word in field.lower() for word in banned), (cls, field)

    for experiment in result.experiments:
        assert experiment.ordering_rank == result.experiments.index(experiment)
        assert 0.0 <= experiment.evidence.prevalence_share <= 1.0
        if experiment.evidence.top_channel_share is not None:
            assert 0.0 <= experiment.evidence.top_channel_share <= 1.0


def test_no_emitted_string_makes_a_causal_or_predictive_claim():
    candidate_id = uuid4()
    outlier_id = uuid4()
    outlier_evidence = _outlier_corpus(outlier_id)
    results = [
        run(_permutation_corpus(candidate_id), candidate_id),
        run(outlier_evidence, outlier_id),
        run([], candidate_id),
        run([], candidate_id, missing_reasons={PUBLIC_CONTENT: FAILED}),
        derive_content_experiments(candidate_id=uuid4(), patterns=None),
    ]

    banned = (
        r"performs? better", r"drives? views", r"causes?", r"virality",
        r"goes? viral", r"viral format", r"winning hook", r"winning",
        r"increases? engagement", r"boosts?", r"will (?:perform|increase)",
        r"proven", r"guarantee\w*", r"best[- ]performing", r"optimi[sz]e\w*",
        r"recommend\w*",
    )
    negators = (
        r"(?:not|never|no|none|nothing|cannot|neither|nor|refus\w*|without)"
    )

    for result in results:
        # 1. Derived VALUES are data: no claim vocabulary at all. Separators
        #    are folded so a SCREAMING_SNAKE rename cannot slip through.
        derived_parts = (
            [e.variable_value for e in result.experiments]
            + [e.variable_kind.value for e in result.experiments]
            + [e.variable_family.value for e in result.experiments]
            + [e.sufficiency.value for e in result.experiments]
            + [e.baseline_state.value for e in result.experiments]
            + [e.evidence.concentration.value for e in result.experiments]
            + [e.evidence.cooccurrence_state.value for e in result.experiments]
            + [s.value for s in EvidenceSufficiency]
            + [s.value for s in ExperimentsState]
            + [s.value for s in GenerationState]
            + [s.value for s in BaselineState]
            + [s.value for s in VariableFamily]
            + [result.experiments_state.value, result.generation_state.value,
               result.dimension_name]
        )
        derived = " ".join(derived_parts).replace("_", " ").replace("-", " ")
        for term in banned:
            assert not re.search(rf"\b{term}\b", derived, re.IGNORECASE), (
                term, derived[:200],
            )

        # 2. PROSE may name a claim only to refuse it.
        prose_parts = [result.state_boundary, *result.limitations]
        for experiment in result.experiments:
            prose_parts.extend(
                [
                    experiment.title,
                    experiment.hypothesis,
                    experiment.baseline_definition,
                    experiment.success_criterion,
                    experiment.primary_measurement,
                    *experiment.limitations,
                    *experiment.instructions.hold_constant,
                ]
            )
            if experiment.instructions.title_structure:
                prose_parts.append(experiment.instructions.title_structure)
        for prose in prose_parts:
            for term in banned:
                for match in re.finditer(rf"\b{term}\b", prose, re.IGNORECASE):
                    window = prose[max(0, match.start() - 110) : match.start()]
                    assert re.search(rf"\b{negators}\b", window, re.IGNORECASE), (
                        term, prose,
                    )


def test_no_unavailable_private_metric_is_ever_named_as_a_measurement():
    """Public evidence cannot see these, so nothing may promise to measure them."""
    candidate_id = uuid4()
    result = run(_spread_corpus(candidate_id), candidate_id)
    private = (
        r"click[- ]through", r"\bctr\b", r"retention", r"watch time",
        r"average view duration", r"impressions?", r"conversions?",
        r"conversion rate", r"revenue", r"profit\w*", r"sales?",
        r"subscribers? gained", r"algorithm\w* (?:preference|boost)",
        r"willingness to pay",
    )
    negators = r"(?:not|never|no|none|nothing|cannot|neither|nor|without)"
    for experiment in result.experiments:
        for text in (
            experiment.primary_measurement,
            experiment.success_criterion,
            experiment.hypothesis,
        ):
            for term in private:
                for match in re.finditer(rf"{term}", text, re.IGNORECASE):
                    window = text[max(0, match.start() - 130) : match.start()]
                    assert re.search(rf"\b{negators}\b", window, re.IGNORECASE), (
                        term, text,
                    )
    # The measurement names the one observable outcome, explicitly.
    assert "view count" in PRIMARY_MEASUREMENT
    assert "creator-relative" in PRIMARY_MEASUREMENT


def test_every_state_states_what_it_does_not_establish():
    for state in ExperimentsState:
        assert state in STATE_BOUNDARIES, state
        assert STATE_BOUNDARIES[state].strip()


def test_versions_are_reported():
    candidate_id = uuid4()
    result = run(_spread_corpus(candidate_id), candidate_id)
    assert result.version == CONTENT_EXPERIMENTS_VERSION == "content_experiments_v1"
    assert result.ordering_version == "experiment_ordering_v1"
    assert result.template_version == "experiment_template_v1"
    for experiment in result.experiments:
        assert experiment.id_version == EXPERIMENT_ID_VERSION
        assert experiment.template_version == "experiment_template_v1"
    assert result.limitations == LIMITATIONS


# ------------------------------------------------------ module boundaries


def _module_tree() -> ast.Module:
    return ast.parse(MODULE_PATH.read_text(encoding="utf-8"))


def test_module_is_provider_free_and_service_layer_only():
    forbidden = (
        "httpx", "requests", "aiohttp", "urllib", "socket", "sqlalchemy",
        "psycopg", "asyncpg", "app.providers", "app.repositories", "app.api",
        "app.main", "openai", "anthropic",
    )
    for node in ast.walk(_module_tree()):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not any(alias.name.startswith(f) for f in forbidden), alias.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            assert not any(node.module.startswith(f) for f in forbidden), node.module


def test_module_does_not_reach_scoring_machinery():
    text = MODULE_PATH.read_text(encoding="utf-8")
    for forbidden in (
        "app.services.scoring", "score_opportunity", "Classification",
        "evidence_confidence", "opportunity_score",
    ):
        assert forbidden not in text, forbidden


def test_module_imports_only_5b_and_shared_primitives():
    allowed = {
        "app.domain.enums",
        "app.services.content_patterns",
        "app.services.preliminary_dimensions",
    }
    for node in ast.walk(_module_tree()):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module.startswith("app."):
                assert node.module in allowed, node.module


def test_module_never_reads_private_engagement_or_channel_aggregates():
    """5C consumes 5B's result only; it must not reach into raw payloads."""
    text = MODULE_PATH.read_text(encoding="utf-8")
    for forbidden in (
        "raw_payload", "view_count", "like_count", "comment_count",
        "channel_subscriber_count", "channel_view_count", "channel_video_count",
        "EvidenceItem",
    ):
        assert forbidden not in text, forbidden


def test_no_endpoint_was_added_and_score_remains_gone():
    from fastapi.testclient import TestClient

    from app.main import app

    paths = {route.path for route in app.routes if hasattr(route, "methods")}
    assert len(paths) == 15
    assert TestClient(app).post("/score", json={}).status_code == 410


def test_5c_is_not_wired_into_any_route():
    import app.api.routes as routes

    assert "content_experiments" not in Path(routes.__file__).read_text(
        encoding="utf-8"
    )


def test_5a_and_5b_semantics_are_untouched_by_5c():
    candidate_id = uuid4()
    evidence = _spread_corpus(candidate_id)
    outlier = derive_robust_content_intelligence(
        candidate_id=candidate_id, evidence=evidence
    )
    patterns = derive_content_patterns(
        candidate_id=candidate_id, evidence=evidence, outlier_evidence=outlier
    )
    before_patterns = (
        patterns.pattern_state,
        patterns.state,
        tuple((p.kind, p.value, p.video_count) for p in patterns.patterns),
    )
    before_outlier = (outlier.state, outlier.provenance.evidence_ids)

    derive_content_experiments(candidate_id=candidate_id, patterns=patterns)

    assert (
        patterns.pattern_state,
        patterns.state,
        tuple((p.kind, p.value, p.video_count) for p in patterns.patterns),
    ) == before_patterns
    assert (outlier.state, outlier.provenance.evidence_ids) == before_outlier
    assert patterns.pattern_state is ContentPatternsState.PATTERNS_OBSERVED
