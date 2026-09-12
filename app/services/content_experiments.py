"""Milestone 5C: production-ready content experiments.

A deterministic, provider-free derivation that turns Milestone 5B's content
patterns — which already carry Milestone 5A's creator-relative outlier
co-occurrence, verified for scope — into a bounded set of content EXPERIMENTS.

WHAT AN EXPERIMENT IS HERE

An experiment states what to test, what evidence makes it worth testing, how
to produce the variant, and what result would justify continuing. It is a
question posed to reality, not an answer taken from it. Nothing in this
module predicts an outcome, ranks patterns by expected performance, or
asserts that any feature works.

THE EVIDENCE BOUNDARY THIS MODULE DEFENDS

5C may say: "this pattern was observed in N videos across M creators, and
co-occurred with observations Milestone 5A scored above their own creator's
baseline." It may NOT say that the pattern performs better, increases views,
is a winning hook, a viral format, a proven strategy, or a best-performing
pattern. Co-occurrence is observational. Videos that outperform their
creator's median differ in topic, timing, thumbnail, existing audience and
platform distribution, and neither 5B nor 5C isolates a pattern from any of
them. That is precisely why the output is an experiment: the isolation this
evidence cannot provide is what the test is for.

WHAT IT NEVER INFERS

Click-through rate, retention, watch time, conversions, sales, revenue,
profitability and algorithmic preference are not observable in public content
metadata. They are never estimated, never approximated, and never named as a
measurement. The only outcome this module will nominate is the one 5A already
works in: a public view count read against the producing channel's own
baseline.

SINGLE GATEWAY TO 5A

5C consumes the 5B result and nothing else. 5B already verifies 5A's
candidate_id and research_run_id before reading a single observation, and
encodes the outcome in each pattern's co-occurrence state. Re-verifying 5A
here would duplicate that check in a second place where the two could drift
apart and disagree about the same fact. 5B is therefore the only path by
which 5A evidence reaches an experiment, and a 5B result whose own scope does
not match this derivation is refused outright.

NO SCORING

No Evidence Confidence, no POS, no final opportunity score, no
RED/YELLOW/GREEN, no virality score, no commercial score, no content
performance score, and no 0-100 experiment ranking. Experiments are ORDERED
by a published, evidence-backed rule; ordering is not scoring, and the
ordering inputs are emitted alongside every experiment so the order can be
recomputed and checked by hand.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import Enum
from uuid import UUID

from app.domain.enums import TruthClass
from app.services.content_patterns import (
    ConcentrationState,
    ContentPattern,
    ContentPatternsResult,
    ContentPatternsState,
    CooccurrenceState,
    DurationBand,
    FieldState,
    PatternKind,
)
from app.services.preliminary_dimensions import DimensionState

CONTENT_EXPERIMENTS_VERSION = "content_experiments_v1"
EXPERIMENT_ID_VERSION = "experiment_id_v1"
EXPERIMENT_ORDERING_VERSION = "experiment_ordering_v1"
EXPERIMENT_TEMPLATE_VERSION = "experiment_template_v1"

DIM_CONTENT_EXPERIMENTS = "preliminary_content_experiments"

# The approved ceiling. Reaching it is not a goal: generating thirty
# experiments from evidence that supports eight would be manufacturing, and
# the generation state says explicitly when the cap bound the result.
MAX_EXPERIMENTS = 30

# A feature seen once is not something to test. This currently coincides with
# 5B's own recurrence floor, so the ordinary pipeline cannot deliver a pattern
# below it; the check defends 5C's INPUT TYPE, which is a result object a
# caller may build or a future 5B may fill differently, rather than trusting a
# constant in another module. A test reaches the branch through that input.
MIN_EXPERIMENT_VIDEO_COUNT = 2

# Creator breadth at which prevalence stops describing one creator's habits.
# The same floor 5B requires before it will report co-occurrence at all.
MIN_CREATORS_FOR_BROAD_EVIDENCE = 3

# How many publications a test must accumulate before its criterion is read.
# An UNVALIDATED V1 assumption: it is a stopping rule chosen by inspection to
# keep a decision from resting on one upload, not a statistical power
# calculation, and it is calibrated against no outcome data.
MIN_TEST_PUBLICATIONS = 5

# The only outcome this milestone will nominate. Public, creator-relative,
# and already the measure 5A works in. Anything richer — click-through,
# retention, conversion, revenue — is invisible in public content metadata
# and is never inferred from it.
PRIMARY_MEASUREMENT = (
    "Public view count of each published variant, read against the producing "
    "channel's own median for the same period (the creator-relative outcome "
    "Milestone 5A already uses). No click-through rate, retention, watch "
    "time, conversion, sale, revenue or algorithmic-preference figure is "
    "available in public content evidence, and none is estimated here."
)

# Reported once per result. Every experiment additionally carries the
# limitations specific to its own evidence.
LIMITATIONS: tuple[str, ...] = (
    "An experiment is a test to run, not a result. Nothing here is evidence "
    "that a pattern performs better. Nothing here is evidence that a pattern "
    "drives views, causes virality, or increases engagement. No pattern here "
    "is a winning hook, a viral format, a proven strategy, or a "
    "best-performing feature.",
    "Co-occurrence with creator-relative outliers is an observation about a "
    "sample. It isolates no variable, so it can never support a causal or "
    "predictive reading, and it is reported separately from prevalence and "
    "never combined with it.",
    "The evidence describes OTHER creators' public content. It says nothing "
    "about the producing channel's own audience, history or baseline, which "
    "is why every experiment names a baseline the producer must supply.",
    "Click-through rate, retention, watch time, conversions, sales, revenue, "
    "profitability and algorithmic preference are not observable in public "
    "content metadata and are never inferred here.",
    "The sampled videos are provider-ordered public-content observations, "
    "not the whole content field, so prevalence describes the sample.",
    "Success criteria are decision rules for a test, not predicted outcomes, "
    "and their thresholds are unvalidated V1 assumptions.",
    "Experiments are ORDERED by a published evidence rule. The order is not "
    "a score, a ranking of expected performance, or a recommendation.",
)


class VariableFamily(str, Enum):
    """Which production lever an experiment manipulates.

    The family groups levers for reporting and for deciding what must be held
    constant. It is NOT an equivalence class: two variables in one family are
    still independently manipulable and remain separate experiments.
    """

    TITLE = "TITLE"
    TAGS = "TAGS"
    CATEGORY = "CATEGORY"
    DURATION = "DURATION"


class EvidenceSufficiency(str, Enum):
    """How much the evidence behind one experiment actually establishes.

    A state, not a score. It is derived from 5B's creator-concentration and
    co-occurrence states by published rules and carries no magnitude.
    """

    # Breadth across creators AND a reported co-occurrence comparison.
    BROAD_WITH_COOCCURRENCE = "BROAD_WITH_COOCCURRENCE"
    # Breadth across creators; co-occurrence was not comparable or absent.
    BROAD_PREVALENCE_ONLY = "BROAD_PREVALENCE_ONLY"
    # Known to rest on one creator, or on too few to call it a field.
    NARROW_CREATOR_BASE = "NARROW_CREATOR_BASE"
    # Creator identity is missing or contradictory for some or all
    # occurrences, so the breadth of the base cannot be characterized.
    CREATOR_IDENTITY_INCOMPLETE = "CREATOR_IDENTITY_INCOMPLETE"


class BaselineState(str, Enum):
    """What the experiment can be compared against."""

    # The corpus itself contains observed videos whose field was available
    # and which do NOT carry the pattern, usable as an observational contrast.
    OBSERVED_CORPUS_CONTRAST = "OBSERVED_CORPUS_CONTRAST"
    # Every observed video carrying the field also carries the pattern, so no
    # contrast exists in this evidence and the producer must supply one.
    PRODUCER_HISTORY_REQUIRED = "PRODUCER_HISTORY_REQUIRED"


class ExperimentsState(str, Enum):
    """Why the result looks the way it does."""

    PATTERN_EVIDENCE_UNAVAILABLE = "PATTERN_EVIDENCE_UNAVAILABLE"
    PATTERNS_SCOPE_MISMATCH = "PATTERNS_SCOPE_MISMATCH"
    NO_PATTERN_EVIDENCE = "NO_PATTERN_EVIDENCE"
    NO_ELIGIBLE_PATTERNS = "NO_ELIGIBLE_PATTERNS"
    EXPERIMENTS_GENERATED = "EXPERIMENTS_GENERATED"


class GenerationState(str, Enum):
    """What bound the number of experiments produced."""

    NO_ELIGIBLE_PATTERNS = "NO_ELIGIBLE_PATTERNS"
    # Evidence was exhausted before the cap. The honest common case.
    ALL_ELIGIBLE_PATTERNS_USED = "ALL_ELIGIBLE_PATTERNS_USED"
    # More materially distinct experiments existed than the cap allows.
    CAPPED_AT_MAXIMUM = "CAPPED_AT_MAXIMUM"


STATE_BOUNDARIES: dict[ExperimentsState, str] = {
    ExperimentsState.PATTERN_EVIDENCE_UNAVAILABLE: (
        "Milestone 5B produced no pattern evidence for this scope, so nothing "
        "could be proposed. This is not evidence that no experiment is worth "
        "running."
    ),
    ExperimentsState.PATTERNS_SCOPE_MISMATCH: (
        "The supplied pattern evidence belongs to another candidate or "
        "research run and was refused unread. Nothing here describes either "
        "scope."
    ),
    ExperimentsState.NO_PATTERN_EVIDENCE: (
        "Milestone 5B observed content but found no recurring pattern, so "
        "there is no observed feature to test. This describes the sampled "
        "content only."
    ),
    ExperimentsState.NO_ELIGIBLE_PATTERNS: (
        "Patterns were observed but none cleared the evidence floor for an "
        "experiment. This is not evidence that the patterns are unimportant."
    ),
    ExperimentsState.EXPERIMENTS_GENERATED: (
        "These experiments are tests to run, chosen because the evidence "
        "makes them worth testing. None of them is a prediction, a "
        "recommendation guaranteed to perform, or evidence that the feature "
        "under test works."
    ),
}

# Which co-occurrence states are an actual comparison against 5A's
# above-baseline observations. The rest report why no comparison was made.
_COOCCURRENCE_REPORTED: frozenset[CooccurrenceState] = frozenset(
    {
        CooccurrenceState.OVER_REPRESENTED,
        CooccurrenceState.UNDER_REPRESENTED,
        CooccurrenceState.COMPARABLE,
        CooccurrenceState.ABSENT_AMONG_OUTLIERS,
    }
)

# Publication-level outcome for one variant, and the decision the criterion
# yields. UNKNOWN exists because a publication whose view count is
# unavailable must never be counted as a failure: missing is not below.
class PublicationOutcome(str, Enum):
    AT_OR_ABOVE_BASELINE = "AT_OR_ABOVE_BASELINE"
    BELOW_BASELINE = "BELOW_BASELINE"
    UNKNOWN = "UNKNOWN"


class ExperimentDecision(str, Enum):
    CONTINUE = "CONTINUE"
    STOP = "STOP"
    # Not a stop. Too few known outcomes to read the rule at all.
    INSUFFICIENT_PUBLICATIONS = "INSUFFICIENT_PUBLICATIONS"


_KIND_FAMILY: dict[PatternKind, VariableFamily] = {
    PatternKind.TITLE_TOKEN: VariableFamily.TITLE,
    PatternKind.TITLE_BIGRAM: VariableFamily.TITLE,
    PatternKind.TAG: VariableFamily.TAGS,
    PatternKind.CATEGORY: VariableFamily.CATEGORY,
    PatternKind.DURATION_BAND: VariableFamily.DURATION,
}

# Seconds each band covers: (inclusive lower, exclusive upper or None).
# Declared here rather than imported from 5B's private table, and a test
# asserts every boundary agrees with 5B's own `duration_band()` so the two
# cannot drift apart.
DURATION_BAND_SECONDS: dict[DurationBand, tuple[int, int | None]] = {
    DurationBand.UNDER_1_MIN: (0, 60),
    DurationBand.ONE_TO_5_MIN: (60, 300),
    DurationBand.FIVE_TO_15_MIN: (300, 900),
    DurationBand.FIFTEEN_TO_30_MIN: (900, 1800),
    DurationBand.OVER_30_MIN: (1800, None),
}

# Ordering rank for each sufficiency state. Lower sorts first. This is a
# published ordering key, not a score: it has no magnitude, nothing is
# multiplied by it, and it is emitted with every experiment.
_SUFFICIENCY_RANK: dict[EvidenceSufficiency, int] = {
    EvidenceSufficiency.BROAD_WITH_COOCCURRENCE: 0,
    EvidenceSufficiency.BROAD_PREVALENCE_ONLY: 1,
    # A known-narrow base is fully characterized; an incomplete one cannot be
    # characterized at all, so it sorts last.
    EvidenceSufficiency.NARROW_CREATOR_BASE: 2,
    EvidenceSufficiency.CREATOR_IDENTITY_INCOMPLETE: 3,
}


# ------------------------------------------------------------------- records


@dataclass(slots=True, frozen=True)
class ProductionInstructions:
    """How to produce the variant, and what must not move while testing."""

    title_structure: str | None
    format_category: str | None
    duration_band: DurationBand | None
    duration_seconds_range: tuple[int, int | None] | None
    tags: tuple[str, ...]
    hold_constant: tuple[str, ...]


@dataclass(slots=True, frozen=True)
class ExperimentEvidence:
    """Exactly what was observed, with full lineage. No derived magnitudes."""

    pattern_kind: PatternKind
    pattern_value: str

    # Prevalence, with 5B's denominator preserved.
    video_count: int
    field_available_video_count: int
    prevalence_share: float

    # Creator spread, carried through unchanged from 5B.
    distinct_channel_count: int
    videos_without_channel_id: int
    videos_with_conflicting_channel_id: int
    top_channel_share: float | None
    concentration: ConcentrationState

    # 5A co-occurrence, reported separately and never combined with the above.
    cooccurrence_state: CooccurrenceState
    outlier_video_count: int
    outlier_distinct_channel_count: int
    comparable_outlier_total: int
    share_among_outliers: float | None
    share_in_corpus: float | None

    # Lineage.
    video_ids: tuple[str, ...]
    channel_ids: tuple[str, ...]
    evidence_ids: tuple[UUID, ...]


@dataclass(slots=True, frozen=True)
class EquivalentVariable:
    """A pattern co-extensive with the retained one over the same videos.

    Recorded rather than dropped: the evidence cannot separate these
    variables, and saying so is part of the result.
    """

    kind: PatternKind
    value: str
    video_count: int


@dataclass(slots=True, frozen=True)
class ContentExperiment:
    """One experiment to run. Not a prediction and not a recommendation."""

    experiment_id: str
    title: str
    hypothesis: str

    variable_family: VariableFamily
    variable_kind: PatternKind
    variable_value: str

    baseline_state: BaselineState
    baseline_definition: str

    evidence: ExperimentEvidence
    equivalent_variables: tuple[EquivalentVariable, ...]

    instructions: ProductionInstructions
    primary_measurement: str
    success_criterion: str

    sufficiency: EvidenceSufficiency
    limitations: tuple[str, ...]

    # Emitted so the published ordering can be recomputed by hand. Ordering
    # inputs, not a score.
    ordering_rank: int
    template_version: str = EXPERIMENT_TEMPLATE_VERSION
    id_version: str = EXPERIMENT_ID_VERSION


@dataclass(slots=True, frozen=True)
class ExperimentProvenance:
    """Canonical lineage for one candidate/run derivation."""

    candidate_id: UUID
    research_run_id: UUID | None
    pattern_dimension: str
    evidence_ids: tuple[UUID, ...]
    video_ids: tuple[str, ...]
    channel_ids: tuple[str, ...]
    patterns_version: str
    patterns_state: ContentPatternsState


@dataclass(slots=True, frozen=True)
class ContentExperimentsResult:
    """Milestone 5C result for one candidate and one evidence scope."""

    candidate_id: UUID
    research_run_id: UUID | None
    state: DimensionState
    experiments_state: ExperimentsState
    generation_state: GenerationState
    state_boundary: str
    # No candidate-level numeric value, by design.
    value: None

    experiments: tuple[ContentExperiment, ...]
    provenance: ExperimentProvenance

    # Structured account of how the count was reached, so a short list is
    # explained rather than merely short.
    observed_pattern_count: int
    eligible_pattern_count: int
    excluded_below_floor_count: int
    suppressed_as_equivalent_count: int
    generated_experiment_count: int
    experiment_cap: int = MAX_EXPERIMENTS

    outlier_evidence_available: bool = False
    features_truth_class: TruthClass | None = TruthClass.INFERRED
    missing_reason: str | None = None
    limitations: tuple[str, ...] = LIMITATIONS
    dimension_name: str = DIM_CONTENT_EXPERIMENTS
    version: str = CONTENT_EXPERIMENTS_VERSION
    ordering_version: str = EXPERIMENT_ORDERING_VERSION
    template_version: str = EXPERIMENT_TEMPLATE_VERSION


# ----------------------------------------------------------------- internals


def _id_sort_key(value: object) -> tuple[str, str]:
    """Total order for identifiers without coercing their stored values."""
    return (str(value), type(value).__name__)


def _experiment_id(
    candidate_id: UUID,
    research_run_id: UUID | None,
    kind: PatternKind,
    value: str,
) -> str:
    """Stable id for one (scope, variable) pair.

    Derived from the scope and the variable alone, never from position in the
    result. An experiment therefore keeps its id when other experiments
    appear, disappear, or change order, which is what makes it usable as a
    reference in anything downstream.
    """
    material = "|".join(
        (
            EXPERIMENT_ID_VERSION,
            str(candidate_id),
            str(research_run_id),
            kind.value,
            value,
        )
    )
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return f"exp_{digest[:12]}"


def _canonical_value(value: str) -> str:
    """Fold a variable value to the form the instructions would carry.

    5B already normalizes pattern values, so this is defensive: 5C accepts a
    result object a caller may build, and two spellings that would produce
    identical production instructions must not become two experiments.
    """
    return " ".join(value.split()).casefold()


def _intervention_key(kind: PatternKind, value: str) -> tuple:
    """Identity of the actual intervention a producer would perform.

    Deliberately independent of which videos were observed. Different values
    are different interventions, and a token is a different intervention from
    a bigram even when one contains the other, because a producer manipulates
    them separately.
    """
    return (_KIND_FAMILY[kind], kind, _canonical_value(value))


def evaluate_success_criterion(
    outcomes: "list[PublicationOutcome] | tuple[PublicationOutcome, ...]",
) -> ExperimentDecision:
    """Apply the emitted success criterion to a set of publication outcomes.

    The rule, stated once here and once in the emitted text, which a test
    holds to each other:

    - A publication whose creator-relative view outcome is unavailable is
      UNKNOWN. It is EXCLUDED from the count rather than counted as below,
      because missing evidence must never become negative evidence.
    - With fewer than `MIN_TEST_PUBLICATIONS` known outcomes the rule yields
      INSUFFICIENT_PUBLICATIONS. That is not a stop; it is not yet a decision.
    - Otherwise continue when STRICTLY more than half of the known outcomes
      are at or above the baseline, and stop otherwise. An exact half stops.

    The decision is a count over a set, so it cannot depend on the order the
    publications are evaluated in. Both the publication floor and the
    more-than-half threshold are unvalidated V1 assumptions chosen by
    inspection. This is a decision rule for a test: it is not a score, a
    prediction, a statistical-power claim, or a statement about why an
    outcome occurred.
    """
    known = [o for o in outcomes if o is not PublicationOutcome.UNKNOWN]
    if len(known) < MIN_TEST_PUBLICATIONS:
        return ExperimentDecision.INSUFFICIENT_PUBLICATIONS
    at_or_above = sum(
        1 for o in known if o is PublicationOutcome.AT_OR_ABOVE_BASELINE
    )
    if at_or_above * 2 > len(known):
        return ExperimentDecision.CONTINUE
    return ExperimentDecision.STOP


def _sufficiency(pattern: ContentPattern) -> EvidenceSufficiency:
    """Classify what this pattern's evidence establishes about its base.

    Concentration is consulted BEFORE breadth: 5B's concentration states
    already encode whether creator identity was complete, and a
    distinct-creator count alone cannot distinguish "three creators carry
    this" from "three creators and six unattributed occurrences carry this".
    """
    concentration = pattern.concentration
    if concentration in (
        ConcentrationState.SINGLE_CREATOR,
        ConcentrationState.CREATOR_CONCENTRATED,
    ):
        return EvidenceSufficiency.NARROW_CREATOR_BASE
    if concentration in (
        ConcentrationState.CREATOR_PARTIALLY_UNKNOWN,
        ConcentrationState.CREATOR_UNKNOWN,
    ):
        return EvidenceSufficiency.CREATOR_IDENTITY_INCOMPLETE
    if pattern.distinct_channel_count < MIN_CREATORS_FOR_BROAD_EVIDENCE:
        return EvidenceSufficiency.NARROW_CREATOR_BASE
    if pattern.cooccurrence.state in _COOCCURRENCE_REPORTED:
        return EvidenceSufficiency.BROAD_WITH_COOCCURRENCE
    return EvidenceSufficiency.BROAD_PREVALENCE_ONLY


def _baseline(pattern: ContentPattern) -> tuple[BaselineState, str]:
    """Whether the corpus itself offers a contrast group."""
    without = pattern.field_available_video_count - pattern.video_count
    if without > 0:
        return (
            BaselineState.OBSERVED_CORPUS_CONTRAST,
            (
                f"{without} of the {pattern.field_available_video_count} "
                "observed videos whose field was available do NOT carry this "
                "pattern and can serve as an observational contrast. They "
                "were not produced under controlled conditions, so the "
                "contrast describes the sample and is not a control group. "
                "The controlled baseline remains the producing channel's own "
                "comparable content."
            ),
        )
    return (
        BaselineState.PRODUCER_HISTORY_REQUIRED,
        (
            "Every observed video whose field was available carries this "
            "pattern, so this evidence contains no contrast. The baseline "
            "must be the producing channel's own comparable content from "
            "before the test."
        ),
    )


def _variable_description(kind: PatternKind, value: str) -> str:
    if kind is PatternKind.TITLE_TOKEN:
        return f"the title token {value!r}"
    if kind is PatternKind.TITLE_BIGRAM:
        return f"the adjacent title phrase {value!r}"
    if kind is PatternKind.TAG:
        return f"the tag {value!r}"
    if kind is PatternKind.CATEGORY:
        return f"the category {value!r}"
    return f"the duration band {value}"


def _title(kind: PatternKind, value: str) -> str:
    return f"Test {_variable_description(kind, value)}"


def _creator_phrase(pattern: ContentPattern) -> str:
    """State the creator base honestly, including what is not known."""
    parts = [f"{pattern.distinct_channel_count} known creator"
             f"{'' if pattern.distinct_channel_count == 1 else 's'}"]
    if pattern.videos_without_channel_id:
        parts.append(
            f"{pattern.videos_without_channel_id} occurrence"
            f"{'' if pattern.videos_without_channel_id == 1 else 's'} with no "
            "creator recorded"
        )
    if pattern.videos_with_conflicting_channel_id:
        parts.append(
            f"{pattern.videos_with_conflicting_channel_id} occurrence"
            f"{'' if pattern.videos_with_conflicting_channel_id == 1 else 's'} "
            "whose records named different creators"
        )
    return ", ".join(parts)


def _cooccurrence_phrase(pattern: ContentPattern) -> str:
    """Describe the 5A comparison without ever implying it caused anything."""
    state = pattern.cooccurrence.state
    if state is CooccurrenceState.OUTLIER_SCOPE_MISMATCH:
        return (
            "Milestone 5A evidence was supplied for a different candidate or "
            "research run and was refused, so no co-occurrence is reported."
        )
    if state is CooccurrenceState.OUTLIER_EVIDENCE_UNAVAILABLE:
        return (
            "No Milestone 5A outlier evidence was supplied for this scope, so "
            "no co-occurrence is reported. That is not evidence of absence."
        )
    if state is CooccurrenceState.NO_QUALIFYING_OUTLIERS:
        return (
            "Milestone 5A ran and found no observation above its creator's "
            "own baseline that could carry this field, so there was nothing "
            "to compare against."
        )
    if state is CooccurrenceState.INSUFFICIENT_INDEPENDENT_CREATORS:
        return (
            "Too few independent creators carry this pattern among the "
            "above-baseline observations for a comparison to be reported."
        )
    if state is CooccurrenceState.ABSENT_AMONG_OUTLIERS:
        return (
            "This pattern appears in none of the observations Milestone 5A "
            "scored above their own creator's baseline. That is an "
            "observation about the sample, not evidence the pattern is bad."
        )
    co = pattern.cooccurrence
    descriptor = {
        CooccurrenceState.OVER_REPRESENTED: (
            "occurs more often among those observations than in the corpus"
        ),
        CooccurrenceState.UNDER_REPRESENTED: (
            "occurs less often among those observations than in the corpus"
        ),
        CooccurrenceState.COMPARABLE: (
            "occurs at a comparable rate among those observations and in the "
            "corpus"
        ),
    }[co.state]
    return (
        f"Among the {co.comparable_outlier_total} observations Milestone 5A "
        f"scored above their own creator's baseline, {co.outlier_video_count} "
        f"carry this pattern across {co.outlier_distinct_channel_count} "
        f"creators, so it {descriptor} "
        f"({co.share_among_outliers} against {co.share_in_corpus}). This is "
        "co-occurrence in a sample. It does not establish that the pattern "
        "affected any outcome, and those videos differ in topic, timing, "
        "thumbnail, existing audience and distribution."
    )


def _sentence_case(text: str) -> str:
    """Upper-case the first letter only.

    `str.capitalize()` lower-cases the remainder, which would turn the band
    name FIVE_TO_15_MIN into five_to_15_min and quietly corrupt an emitted
    identifier.
    """
    return text[:1].upper() + text[1:] if text else text


def _hypothesis(pattern: ContentPattern) -> str:
    return (
        f"{_sentence_case(_variable_description(pattern.kind, pattern.value))} "
        f"was observed in {pattern.video_count} of the "
        f"{pattern.field_available_video_count} sampled videos whose "
        f"corresponding field was available ({_creator_phrase(pattern)}). "
        f"{_cooccurrence_phrase(pattern)} This experiment tests whether "
        "producing content that carries it, with the listed variables held "
        "constant, changes the producing channel's own creator-relative view "
        "outcome. The observation above is the reason to test it and is not "
        "evidence of what the test will show."
    )


def _success_criterion(pattern: ContentPattern) -> str:
    """The decision rule, worded to match `evaluate_success_criterion`."""
    return (
        "For each publication produced under the conditions above, record "
        "whether its creator-relative view outcome was at or above the "
        "producing channel's own median for the same period, measured "
        "against the baseline named above. A publication whose view count is "
        "unavailable is recorded as UNKNOWN and is excluded from the count; "
        "it is never counted as below. Once at least "
        f"{MIN_TEST_PUBLICATIONS} publications have a known outcome, continue "
        "this line of testing if strictly more than half of those known "
        "outcomes are at or above the baseline, and stop otherwise; an exact "
        f"half stops. Fewer than {MIN_TEST_PUBLICATIONS} known outcomes "
        "yields no decision, which is not a stop. The decision is a count "
        "over a set, so it does not depend on the order the publications are "
        f"evaluated in. Both the {MIN_TEST_PUBLICATIONS}-publication floor "
        "and the more-than-half threshold are unvalidated V1 assumptions "
        "chosen by inspection. This is a decision rule for a test: it is not "
        "a prediction, a target, or a claim about what the variant will "
        "achieve."
    )


_HOLD_CONSTANT: dict[VariableFamily, tuple[str, ...]] = {
    VariableFamily.TITLE: (
        "publishing category",
        "duration band",
        "tag set",
        "thumbnail treatment",
        "publication cadence and time of day",
    ),
    VariableFamily.TAGS: (
        "title structure",
        "publishing category",
        "duration band",
        "thumbnail treatment",
        "publication cadence and time of day",
    ),
    VariableFamily.CATEGORY: (
        "title structure",
        "duration band",
        "tag set",
        "thumbnail treatment",
        "publication cadence and time of day",
    ),
    VariableFamily.DURATION: (
        "title structure",
        "publishing category",
        "tag set",
        "thumbnail treatment",
        "publication cadence and time of day",
    ),
}


def _instructions(kind: PatternKind, value: str) -> ProductionInstructions:
    """Deterministic production template. No generation, no paraphrase."""
    family = _KIND_FAMILY[kind]
    title_structure: str | None = None
    format_category: str | None = None
    band: DurationBand | None = None
    seconds: tuple[int, int | None] | None = None
    tags: tuple[str, ...] = ()

    if kind is PatternKind.TITLE_TOKEN:
        title_structure = (
            f"Write the title so it contains the token {value!r}. Keep the "
            "rest of the title in the channel's existing style; do not also "
            "change length, punctuation or capitalization conventions."
        )
    elif kind is PatternKind.TITLE_BIGRAM:
        title_structure = (
            f"Write the title so it contains the words {value!r} adjacent and "
            "in that order. Keep the rest of the title in the channel's "
            "existing style."
        )
    elif kind is PatternKind.TAG:
        tags = (value,)
    elif kind is PatternKind.CATEGORY:
        format_category = value
    else:
        band = DurationBand(value)
        seconds = DURATION_BAND_SECONDS[band]

    return ProductionInstructions(
        title_structure=title_structure,
        format_category=format_category,
        duration_band=band,
        duration_seconds_range=seconds,
        tags=tags,
        hold_constant=_HOLD_CONSTANT[family],
    )


def _pattern_limitations(pattern: ContentPattern) -> tuple[str, ...]:
    """Limitations this specific evidence forces, beyond the global set."""
    out: list[str] = []
    concentration = pattern.concentration

    if concentration is ConcentrationState.SINGLE_CREATOR:
        out.append(
            "Every occurrence comes from one creator, so this is one "
            "creator's practice and not evidence about the field."
        )
    elif concentration is ConcentrationState.CREATOR_CONCENTRATED:
        out.append(
            f"One creator holds {pattern.top_channel_share} of this pattern's "
            "occurrences, so it is concentrated rather than field-wide."
        )
    elif concentration is ConcentrationState.CREATOR_PARTIALLY_UNKNOWN:
        out.append(
            "Some occurrences have no usable creator identity, so the number "
            "of creators carrying this pattern is a lower bound and its "
            "breadth is not established."
        )
    elif concentration is ConcentrationState.CREATOR_UNKNOWN:
        out.append(
            "No occurrence has a usable creator identity, so nothing is "
            "known about how many creators carry this pattern."
        )

    if pattern.videos_with_conflicting_channel_id:
        out.append(
            f"{pattern.videos_with_conflicting_channel_id} occurrence(s) have "
            "contradictory creator records. They are counted against breadth "
            "and reported apart from ordinary absence."
        )
    if pattern.videos_without_channel_id:
        out.append(
            f"{pattern.videos_without_channel_id} occurrence(s) record no "
            "creator at all; they are never merged into a synthetic creator."
        )

    if pattern.cooccurrence.state not in _COOCCURRENCE_REPORTED:
        out.append(
            "No creator-relative co-occurrence comparison is available for "
            f"this pattern ({pattern.cooccurrence.state.value}); the "
            "experiment rests on prevalence alone."
        )
    carrying_field = pattern.field_available_video_count
    if carrying_field and pattern.video_count == carrying_field:
        out.append(
            "No observed video lacks this pattern, so this evidence offers "
            "no contrast group."
        )
    return tuple(out)


def _evidence(pattern: ContentPattern) -> ExperimentEvidence:
    co = pattern.cooccurrence
    return ExperimentEvidence(
        pattern_kind=pattern.kind,
        pattern_value=pattern.value,
        video_count=pattern.video_count,
        field_available_video_count=pattern.field_available_video_count,
        prevalence_share=pattern.prevalence_share,
        distinct_channel_count=pattern.distinct_channel_count,
        videos_without_channel_id=pattern.videos_without_channel_id,
        videos_with_conflicting_channel_id=(
            pattern.videos_with_conflicting_channel_id
        ),
        top_channel_share=pattern.top_channel_share,
        concentration=pattern.concentration,
        cooccurrence_state=co.state,
        outlier_video_count=co.outlier_video_count,
        outlier_distinct_channel_count=co.outlier_distinct_channel_count,
        comparable_outlier_total=co.comparable_outlier_total,
        share_among_outliers=co.share_among_outliers,
        share_in_corpus=co.share_in_corpus,
        video_ids=tuple(
            sorted((o.video_id for o in pattern.occurrences), key=_id_sort_key)
        ),
        # Only occurrences whose creator 5B actually resolved count as
        # attributed. The channel_state test is deliberate redundancy: 5B
        # already resolves a CONFLICTING channel to None, so the None check
        # alone would suffice today, and the state check says WHY rather than
        # relying on that coincidence. A test pins 5B's guarantee so this
        # stays a second line rather than becoming the only one.
        channel_ids=tuple(
            sorted(
                {
                    o.channel_id
                    for o in pattern.occurrences
                    if o.channel_id is not None
                    and o.channel_state is FieldState.AVAILABLE
                },
                key=_id_sort_key,
            )
        ),
        evidence_ids=tuple(sorted(pattern.evidence_ids)),
    )


def _order_key(pattern: ContentPattern) -> tuple:
    """The published ordering rule, in order of precedence.

    1. evidence sufficiency
    2. creator breadth (more distinct known creators first)
    3. observation count (more occurrences first)
    4. a reported 5A co-occurrence comparison before none
    5. canonical tie-break on (kind, value), which is unique per pattern

    Every input is emitted on the experiment, so the order can be recomputed
    by hand. Ordering is not scoring: no input carries a magnitude, nothing
    is combined or weighted, and the position implies nothing about expected
    performance.
    """
    return (
        _SUFFICIENCY_RANK[_sufficiency(pattern)],
        -pattern.distinct_channel_count,
        -pattern.video_count,
        0 if pattern.cooccurrence.state in _COOCCURRENCE_REPORTED else 1,
        pattern.kind.value,
        pattern.value,
    )


def _empty(
    candidate_id: UUID,
    research_run_id: UUID | None,
    experiments_state: ExperimentsState,
    dimension_state: DimensionState,
    provenance: ExperimentProvenance,
    missing_reason: str,
    *,
    observed_pattern_count: int = 0,
    eligible_pattern_count: int = 0,
    excluded_below_floor_count: int = 0,
    suppressed_as_equivalent_count: int = 0,
    outlier_evidence_available: bool = False,
    features_truth_class: TruthClass | None = None,
) -> ContentExperimentsResult:
    return ContentExperimentsResult(
        candidate_id=candidate_id,
        research_run_id=research_run_id,
        state=dimension_state,
        experiments_state=experiments_state,
        generation_state=GenerationState.NO_ELIGIBLE_PATTERNS,
        state_boundary=STATE_BOUNDARIES[experiments_state],
        value=None,
        experiments=(),
        provenance=provenance,
        observed_pattern_count=observed_pattern_count,
        eligible_pattern_count=eligible_pattern_count,
        excluded_below_floor_count=excluded_below_floor_count,
        suppressed_as_equivalent_count=suppressed_as_equivalent_count,
        generated_experiment_count=0,
        outlier_evidence_available=outlier_evidence_available,
        features_truth_class=features_truth_class,
        missing_reason=missing_reason,
    )


def _provenance(
    candidate_id: UUID,
    research_run_id: UUID | None,
    patterns: ContentPatternsResult | None,
) -> ExperimentProvenance:
    if patterns is None:
        return ExperimentProvenance(
            candidate_id=candidate_id,
            research_run_id=research_run_id,
            pattern_dimension="",
            evidence_ids=(),
            video_ids=(),
            channel_ids=(),
            patterns_version="",
            patterns_state=ContentPatternsState.CONTENT_EVIDENCE_UNAVAILABLE,
        )
    return ExperimentProvenance(
        candidate_id=candidate_id,
        research_run_id=research_run_id,
        pattern_dimension=patterns.dimension_name,
        evidence_ids=tuple(sorted(patterns.provenance.evidence_ids)),
        video_ids=tuple(sorted(patterns.provenance.video_ids, key=_id_sort_key)),
        channel_ids=tuple(
            sorted(patterns.provenance.channel_ids, key=_id_sort_key)
        ),
        patterns_version=patterns.version,
        patterns_state=patterns.pattern_state,
    )


# ---------------------------------------------------------------- derivation


def derive_content_experiments(
    candidate_id: UUID,
    patterns: ContentPatternsResult | None,
    research_run_id: UUID | None = None,
) -> ContentExperimentsResult:
    """Derive up to `MAX_EXPERIMENTS` experiments for one candidate and scope.

    Deterministic and pure. The same 5B result yields the same experiments,
    the same ids, the same order and the same lineage; there is no provider
    call, no network I/O, no persistence, no LLM and no semantic clustering.

    `patterns` is Milestone 5B's result for the SAME scope. Its candidate_id
    and research_run_id are VERIFIED before anything is read: pattern values
    and video ids are unique only within a scope, so a 5B result from another
    candidate or run could otherwise contaminate every experiment silently.
    Supplying None is a distinct, reported state.
    """
    if patterns is None:
        return _empty(
            candidate_id,
            research_run_id,
            ExperimentsState.PATTERN_EVIDENCE_UNAVAILABLE,
            DimensionState.MISSING,
            _provenance(candidate_id, research_run_id, None),
            "no_content_pattern_evidence_supplied",
        )

    if (
        patterns.candidate_id != candidate_id
        or patterns.research_run_id != research_run_id
    ):
        # Refused unread: no count, value or lineage from the foreign result
        # reaches the output.
        return _empty(
            candidate_id,
            research_run_id,
            ExperimentsState.PATTERNS_SCOPE_MISMATCH,
            DimensionState.MISSING,
            _provenance(candidate_id, research_run_id, None),
            "content_pattern_evidence_out_of_scope",
        )

    provenance = _provenance(candidate_id, research_run_id, patterns)
    outlier_available = patterns.outlier_evidence_available

    if patterns.pattern_state is not ContentPatternsState.PATTERNS_OBSERVED:
        unavailable = patterns.pattern_state in (
            ContentPatternsState.CONTENT_EVIDENCE_UNAVAILABLE,
        )
        return _empty(
            candidate_id,
            research_run_id,
            (
                ExperimentsState.PATTERN_EVIDENCE_UNAVAILABLE
                if unavailable
                else ExperimentsState.NO_PATTERN_EVIDENCE
            ),
            DimensionState.MISSING if unavailable else DimensionState.UNKNOWN,
            provenance,
            f"content_patterns_{patterns.pattern_state.value.lower()}",
            outlier_evidence_available=outlier_available,
        )

    observed = len(patterns.patterns)

    # Evidence floor. 5B already refuses to call a single occurrence a
    # pattern; stating the floor here keeps 5C's contract independent of that.
    eligible = [
        p for p in patterns.patterns if p.video_count >= MIN_EXPERIMENT_VIDEO_COUNT
    ]
    excluded = observed - len(eligible)

    if not eligible:
        return _empty(
            candidate_id,
            research_run_id,
            ExperimentsState.NO_ELIGIBLE_PATTERNS,
            DimensionState.UNKNOWN,
            provenance,
            "no_pattern_cleared_the_experiment_evidence_floor",
            observed_pattern_count=observed,
            excluded_below_floor_count=excluded,
            outlier_evidence_available=outlier_available,
            features_truth_class=TruthClass.INFERRED,
        )

    # Material distinctness is judged on the INTERVENTION, never on the
    # observed videos. Two patterns that happen to occur in the same videos
    # are not the same experiment: "meal", "plan" and the phrase "meal plan"
    # can be co-extensive in a sample while remaining three independently
    # manipulable things a producer can do to a title. Collapsing them would
    # mistake observational co-occurrence for experimental equivalence and
    # would silently discard testable variables.
    #
    # Suppression therefore fires only when two candidates canonicalize to the
    # SAME intervention — the same lever, the same kind, the same canonical
    # value, and so byte-identical production instructions. The cap, not
    # equivalence, is what bounds output volume.
    grouped: dict[tuple, list[ContentPattern]] = {}
    for pattern in eligible:
        grouped.setdefault(
            _intervention_key(pattern.kind, pattern.value), []
        ).append(pattern)

    representatives: list[tuple[ContentPattern, tuple[ContentPattern, ...]]] = []
    suppressed = 0
    for members in grouped.values():
        ordered_members = sorted(members, key=_order_key)
        representatives.append((ordered_members[0], tuple(ordered_members[1:])))
        suppressed += len(ordered_members) - 1

    representatives.sort(key=lambda pair: _order_key(pair[0]))

    capped = representatives[:MAX_EXPERIMENTS]
    generation_state = (
        GenerationState.CAPPED_AT_MAXIMUM
        if len(representatives) > MAX_EXPERIMENTS
        else GenerationState.ALL_ELIGIBLE_PATTERNS_USED
    )

    experiments: list[ContentExperiment] = []
    for rank, (pattern, equivalents) in enumerate(capped):
        baseline_state, baseline_definition = _baseline(pattern)
        experiments.append(
            ContentExperiment(
                experiment_id=_experiment_id(
                    candidate_id, research_run_id, pattern.kind, pattern.value
                ),
                title=_title(pattern.kind, pattern.value),
                hypothesis=_hypothesis(pattern),
                variable_family=_KIND_FAMILY[pattern.kind],
                variable_kind=pattern.kind,
                variable_value=pattern.value,
                baseline_state=baseline_state,
                baseline_definition=baseline_definition,
                evidence=_evidence(pattern),
                equivalent_variables=tuple(
                    EquivalentVariable(
                        kind=other.kind,
                        value=other.value,
                        video_count=other.video_count,
                    )
                    for other in equivalents
                ),
                instructions=_instructions(pattern.kind, pattern.value),
                primary_measurement=PRIMARY_MEASUREMENT,
                success_criterion=_success_criterion(pattern),
                sufficiency=_sufficiency(pattern),
                limitations=_pattern_limitations(pattern),
                ordering_rank=rank,
            )
        )

    return ContentExperimentsResult(
        candidate_id=candidate_id,
        research_run_id=research_run_id,
        state=DimensionState.EVIDENCE_PRESENT_UNSCORED,
        experiments_state=ExperimentsState.EXPERIMENTS_GENERATED,
        generation_state=generation_state,
        state_boundary=STATE_BOUNDARIES[ExperimentsState.EXPERIMENTS_GENERATED],
        value=None,
        experiments=tuple(experiments),
        provenance=provenance,
        observed_pattern_count=observed,
        eligible_pattern_count=len(eligible),
        excluded_below_floor_count=excluded,
        suppressed_as_equivalent_count=suppressed,
        generated_experiment_count=len(experiments),
        outlier_evidence_available=outlier_available,
        features_truth_class=TruthClass.INFERRED,
    )
