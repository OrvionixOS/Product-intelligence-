"""Milestones 6E and 6G — the candidate POS scalar and its classification.

6E aggregates the three V1 sub-scores into one candidate Opportunity Score.
6G decides whether that score may carry a colour. They live together because
they are one state machine: SPEC_STEP_7_8.md §10's four scoring states are
determined by whether POS exists (6E) and whether Evidence Confidence clears
the floor (6G), and splitting them across modules would let the two disagree
about which state a candidate is in.

`opportunity_score_v1` and `classification_thresholds_v1` are named,
versioned, **explicitly uncalibrated** V1 policy assumptions adopted by
decision. Neither is calibrated against outcome data and neither may be
described as empirically validated. A recalibration ships as `_v2`.

Four rules carry this slice.

**No partial scoring, and no renormalization (§8).** All three sub-scores are
required. If any is unavailable the candidate POS is NULL, the classification
is NULL, and the state is INSUFFICIENT_EVIDENCE. The legacy
`weighted_opportunity_score` renormalized over whichever weights happened to
be present, which is how a candidate missing most of its evidence could score
80; a mean over a varying subset is not comparable between candidates, because
two identical printed scores would rest on different dimensions.

**What was computable is still retained.** An insufficient-evidence result
keeps every sub-score that WAS computed and names every dimension that was
not, with its reason. Evidence Confidence is still computed and stored:
knowing how good the evidence was is most useful precisely when it was not
good enough to score.

**Below the floor is not a colour.** Score without a colour is a real state
(SCORED_UNCLASSIFIED). A missing classification is never rendered as RED.

**V1 has no kill rules.** Every legacy kill rule was rejected in §12, and none
is revived here. The field exists because §10 requires it and because a future
version may have one; in V1 it is always empty, and a test holds that.

No provider call, no network, no LLM. `POST /score` stays 410: §12 retains
that unchanged and §15 does not place activation in this slice.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from uuid import UUID

from app.services.deep_research import DeepResearchResult
from app.services.evidence_confidence import ECS_VERSION, EcsState
from app.services.pos_dimensions import (
    POS_ROUNDING,
    POS_SCALE_MAX,
    POS_SCALE_MIN,
    PosSubScore,
    PosSubScoreSet,
    compute_pos_sub_scores,
)

OPPORTUNITY_SCORE_VERSION = "opportunity_score_v1"
CLASSIFICATION_THRESHOLDS_VERSION = "classification_thresholds_v1"

# ------------------------------------------------ U-2: equal-weight scalar
#
# The three sub-scores are combined by an unweighted mean. Equal weighting is
# not a finding that the three are equally important; it is the absence of any
# evidence that they are not. UNCALIBRATED V1 policy.
POS_DIMENSION_COUNT = 3

# --------------------------------------------- U-3: classification policy
#
# UNCALIBRATED V1 policy values, chosen by decision and calibrated against no
# outcome data.
ECS_CLASSIFICATION_FLOOR = 60.0
RED_UPPER_BOUND = 40.0  # POS < 40          -> RED
YELLOW_UPPER_BOUND = 70.0  # 40 <= POS < 70 -> YELLOW, POS >= 70 -> GREEN

# §12 rejected every legacy kill rule and V1 adds none.
V1_KILL_RULES: tuple[str, ...] = ()


class ScoringState(str, Enum):
    """§10's four states. Every one of them is representable in storage."""

    # Step 8 has not run for this candidate and run.
    NOT_SCORED = "NOT_SCORED"
    # Step 8 ran; at least one required POS dimension was not scoreable. No
    # candidate score, no colour. Computable sub-scores and the excluded
    # dimensions are still retained, and ECS is still computed.
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    # All three required dimensions scored and a candidate POS exists, but
    # Evidence Confidence did not clear the floor. Score exists, colour does
    # not.
    SCORED_UNCLASSIFIED = "SCORED_UNCLASSIFIED"
    # POS, ECS and a colour all exist.
    CLASSIFIED = "CLASSIFIED"


class Classification(str, Enum):
    RED = "RED"
    YELLOW = "YELLOW"
    GREEN = "GREEN"


STATE_BOUNDARIES: dict[ScoringState, str] = {
    ScoringState.NOT_SCORED: (
        "Step 8 has not run for this candidate and run. This is the absence "
        "of an attempt, not a result, and says nothing about the candidate."
    ),
    ScoringState.INSUFFICIENT_EVIDENCE: (
        "At least one required dimension was not scoreable, so no candidate "
        "score and no colour exist. This is not a low score and must never be "
        "rendered, stored or compared as one. What was computable is retained."
    ),
    ScoringState.SCORED_UNCLASSIFIED: (
        "A score exists but the evidence behind it did not clear the "
        "confidence floor, so no colour was assigned. An absent colour is not "
        "RED."
    ),
    ScoringState.CLASSIFIED: (
        "A score, a confidence and a colour all exist. The colour describes "
        "the evidence-backed score under an uncalibrated V1 threshold set, "
        "never a prediction that the product will sell."
    ),
}

LIMITATIONS: tuple[str, ...] = (
    "The aggregation weights and the classification thresholds are "
    "unvalidated V1 policy assumptions chosen by decision. They are not "
    "empirically validated, and no outcome data exists against which they "
    "could be calibrated.",
    "Equal weighting is not a finding that the three dimensions matter "
    "equally; it is the absence of evidence that they do not.",
    "A colour is a band over an uncalibrated score, never a forecast of "
    "sales, revenue, or probability of success.",
    "A NULL score and a NULL colour are absences of measurement, never zeros "
    "and never RED.",
    "One of the three inputs is a purchase PROXY. The composite inherits that "
    "limitation: it is never evidence that anything was sold.",
)


def score_in_range(value: float) -> bool:
    """Both scales are 0-100 and neither may leave it."""
    return POS_SCALE_MIN <= value <= POS_SCALE_MAX


@dataclass(slots=True, frozen=True)
class ScoringResult:
    """§10's persisted scoring record for one candidate and run."""

    candidate_id: UUID
    research_run_id: UUID
    scoring_state: ScoringState
    state_boundary: str
    # NULL unless the state is SCORED_UNCLASSIFIED or CLASSIFIED.
    opportunity_score: float | None
    # NULL unless computed. Retained even when the candidate could not be
    # scored, per §10.
    evidence_confidence: float | None
    # NULL unless the state is CLASSIFIED.
    classification: Classification | None
    # Every sub-score that was computable, blocked ones included.
    sub_scores: tuple[PosSubScore, ...]
    # (dimension_name, reason) for each dimension that could not be scored.
    excluded_dimensions: tuple[tuple[str, str], ...]
    # Always empty in V1: §12 rejected every legacy rule and none was added.
    kill_rules_triggered: tuple[str, ...]
    pos_version: str
    ecs_version: str
    threshold_set_version: str
    limitations: tuple[str, ...] = field(default=LIMITATIONS)

    def __post_init__(self) -> None:
        """§10's invariants, enforced where the record is built.

        The schema mirrors these as check constraints, but a record that
        cannot be constructed wrongly cannot be persisted wrongly either, and
        this catches it in the process that made the mistake rather than at
        the database boundary.
        """
        scored_states = (
            ScoringState.SCORED_UNCLASSIFIED,
            ScoringState.CLASSIFIED,
        )
        if (self.opportunity_score is not None) != (
            self.scoring_state in scored_states
        ):
            raise ValueError(
                f"{self.scoring_state.value} cannot carry "
                f"opportunity_score={self.opportunity_score!r}"
            )
        if (self.classification is not None) != (
            self.scoring_state is ScoringState.CLASSIFIED
        ):
            raise ValueError(
                f"{self.scoring_state.value} cannot carry "
                f"classification={self.classification!r}"
            )
        if (
            self.scoring_state is ScoringState.CLASSIFIED
            and self.evidence_confidence is None
        ):
            raise ValueError(
                "a classified result must carry the confidence that let it "
                "be classified"
            )
        if self.opportunity_score is not None and not score_in_range(
            self.opportunity_score
        ):
            raise ValueError(f"score outside 0-100: {self.opportunity_score}")

    @property
    def is_scored(self) -> bool:
        return self.scoring_state in (
            ScoringState.SCORED_UNCLASSIFIED,
            ScoringState.CLASSIFIED,
        )


def classify(opportunity_score: float) -> Classification:
    """`classification_thresholds_v1` over a POS that already exists.

    Called only once Evidence Confidence has cleared the floor; the floor
    decides whether a colour may exist at all, and this decides which.
    """
    if opportunity_score < RED_UPPER_BOUND:
        return Classification.RED
    if opportunity_score < YELLOW_UPPER_BOUND:
        return Classification.YELLOW
    return Classification.GREEN


def aggregate_opportunity_score(sub_scores: PosSubScoreSet) -> float | None:
    """`opportunity_score_v1`: the unweighted mean of all three sub-scores.

    None when any required sub-score is unavailable. There is deliberately no
    renormalization path: a mean over two of three dimensions is not the same
    measurement as a mean over three, and printing both as one number is how
    a candidate missing its evidence scores well.
    """
    if not sub_scores.all_scored:
        return None
    values = [s.value for s in sub_scores.sub_scores if s.value is not None]
    if len(values) != POS_DIMENSION_COUNT:
        # Deliberately redundant with the `all_scored` check above: either
        # alone prevents a short mean, and a mutation removing one is caught
        # by the other. The legacy `weighted_opportunity_score` had neither,
        # which is how it renormalized over whichever dimensions happened to
        # be present, so this one is guarded twice on purpose.
        return None
    return round(sum(values) / POS_DIMENSION_COUNT, POS_ROUNDING)


def not_scored(candidate_id: UUID, research_run_id: UUID) -> ScoringResult:
    """The state of a candidate Step 8 has not run for."""
    return ScoringResult(
        candidate_id=candidate_id,
        research_run_id=research_run_id,
        scoring_state=ScoringState.NOT_SCORED,
        state_boundary=STATE_BOUNDARIES[ScoringState.NOT_SCORED],
        opportunity_score=None,
        evidence_confidence=None,
        classification=None,
        sub_scores=(),
        excluded_dimensions=(),
        kill_rules_triggered=V1_KILL_RULES,
        pos_version=OPPORTUNITY_SCORE_VERSION,
        ecs_version=ECS_VERSION,
        threshold_set_version=CLASSIFICATION_THRESHOLDS_VERSION,
    )


def score_candidate(result: DeepResearchResult) -> ScoringResult:
    """Run Step 8 over one Step 7 boundary object.

    Deterministic and pure: same dossier, same result. Reads only what the
    dossier declares and computes nothing of its own beyond the two approved
    formulas.
    """
    sub_scores = compute_pos_sub_scores(result)
    confidence = result.evidence_confidence
    # ECS is retained whatever happens to POS: knowing how good the evidence
    # was matters most when it was not good enough to score.
    ecs_value = (
        confidence.evidence_confidence
        if confidence is not None and confidence.state is EcsState.COMPUTED
        else None
    )

    excluded = tuple(
        (
            sub.source_dimension.value,
            sub.blocked_reason or "unavailable",
        )
        for sub in sub_scores.blocked
    )

    opportunity_score = aggregate_opportunity_score(sub_scores)

    if opportunity_score is None:
        state = ScoringState.INSUFFICIENT_EVIDENCE
        classification = None
    elif ecs_value is None or ecs_value < ECS_CLASSIFICATION_FLOOR:
        # No confidence, or not enough of it. A score without a colour, never
        # a colour chosen by default.
        state = ScoringState.SCORED_UNCLASSIFIED
        classification = None
    else:
        state = ScoringState.CLASSIFIED
        classification = classify(opportunity_score)

    return ScoringResult(
        candidate_id=result.candidate_id,
        research_run_id=result.research_run_id,
        scoring_state=state,
        state_boundary=STATE_BOUNDARIES[state],
        opportunity_score=opportunity_score,
        evidence_confidence=ecs_value,
        classification=classification,
        sub_scores=sub_scores.sub_scores,
        excluded_dimensions=excluded,
        kill_rules_triggered=V1_KILL_RULES,
        pos_version=OPPORTUNITY_SCORE_VERSION,
        ecs_version=ECS_VERSION,
        threshold_set_version=CLASSIFICATION_THRESHOLDS_VERSION,
    )
