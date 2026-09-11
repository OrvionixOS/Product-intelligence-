"""Problem/Product Fit Evidence (Milestone 4G).

A deterministic assessment of whether a Milestone 4C product specification is
STRUCTURALLY appropriate for the job its own evidence supports. No provider,
no provider call, no network, no persistence, and no score.

Four things kept strictly apart
--------------------------------

    observed market evidence    what a provider actually returned
    inferred problem/job        Milestone 4C's job_to_be_done_v1 derivation
    generated specification     Milestone 4C's template-generated document
    fit assessment              this module

4G is the first milestone permitted to consume 4C, because its whole purpose
is to evaluate the RELATIONSHIP between the proposed product and the
evidence-backed job. That permission comes with the obligation that makes it
safe: consuming generated text must never launder it into evidence.

Generated prose is never promoted
----------------------------------

This module reads only 4C's STRUCTURED result — claim classes, job scores,
format enums, conflict topics, which fields are UNKNOWN. It never reads a
single generated string: not the product name, not the core promise, not a
differentiation line. A test generates a specification containing a
distinctive invented phrase, runs this module, and asserts the phrase appears
nowhere in the serialized assessment.

Every claim this module makes is capped by `cap_claim_class` against the 4C
inputs it derives from, using 4C's own capper, so a fit claim can never be
stronger than the weakest link it rests on. The assessment itself is always
INFERRED, even when every input was OBSERVED.

What is actually assessable, and what would be a tautology
-----------------------------------------------------------

Milestone 4C chooses a format FROM the job, via `JOB_TO_IDEAL_FORMAT`. So
asking "does the format match the job?" is answered YES by construction and
measures nothing. 4G therefore assesses the SUPPORT CHAIN that choice rests
on, and the one place the chain genuinely breaks:

    1. is the job backed by observed provider text, or only by candidate
       text? (4C caps the job at INFERRED either way: reading a job out of
       text is a derivation, so OBSERVED is unreachable by design.)
    2. was the job classification close enough to be arbitrary?
    3. is the recommended ideal format buildable at all — and when it is not,
       does the V1 substitute preserve what the job actually needs?
    4. is the specification complete enough for any of this to mean anything?

Step 3 is where structural fit lives. `IDEAL_TO_BUILDABLE` substitutes a
spreadsheet for a calculator and a PDF for a community. The first preserves
what the job needs: both perform repeated computation on demand. The second
does not: an ongoing interaction between people cannot be a static document,
however good the document is. This module derives that difference from an
explicit interaction-mode taxonomy rather than asserting it case by case.

No magnitudes, ever
--------------------

Search volume, view counts, review counts, listing counts and price
magnitudes answer different questions and are owned by other milestones.
They can make a market look bigger; they can never make a product structurally
more suitable for a job. This module reads none of them, and an AST guard
asserts it cannot.

What this module never claims
------------------------------

`sales_probability`, `conversion_probability`, `product_market_fit`,
`willingness_to_pay`, `market_size`, `expected_revenue`,
`usefulness_to_buyer` and `buyer_demand_proven` are permanent UNKNOWN
markers. Structural suitability is not demand: a product can fit a job
perfectly and sell nothing. The output is deliberately named product-JOB fit,
never product-MARKET fit, because a job is what 4C derived and a market is
not.
"""

from dataclasses import dataclass
from enum import Enum
from uuid import UUID

from app.domain.enums import ProductFormat, TruthClass
from app.services.preliminary_dimensions import DimensionState
from app.services.product_specification import (
    IdealFormat,
    JobToBeDone,
    ProductSpecification,
    SpecClaimClass,
    SpecificationState,
    cap_claim_class,
)

PRODUCT_JOB_FIT_VERSION = "product_job_fit_v1"
FIT_FEATURES_VERSION = "product_job_fit_features_v1"
FIT_PATTERN_VERSION = "fit_assessment_pattern_v1"
INTERACTION_MODE_VERSION = "interaction_mode_v1"

# Never "product_market_fit": a job is what 4C derived; a market is not.
# Never bare "problem_product_fit" either — that is a ScoreDimensions field
# feeding the unapproved legacy weights.
DIM_PRODUCT_JOB_FIT = "preliminary_product_job_fit"

# ---------------------------------------------------------------- thresholds
#
# UNVALIDATED V1 ASSUMPTIONS chosen by inspection, calibrated against no
# outcome data. They decide which SHAPE is reported; none decides whether
# that shape is good, and no threshold is a quality bar.

# Milestone 4C classifies a job on a lead of JOB_CLASSIFICATION_MIN_LEAD (1)
# over the runner-up. A win by exactly that margin is a real classification,
# but it is one token away from having gone the other way, so this module
# reports it as ambiguous rather than silently inheriting the winner.
AMBIGUITY_MAX_LEAD = 1


class InteractionMode(str, Enum):
    """How a format is used — the property a substitute must preserve.

    UNVALIDATED V1 TAXONOMY (`interaction_mode_v1`). It exists so that
    substitution fidelity is DERIVED from a stated property of each format
    rather than asserted pair by pair, which would hide the reasoning and
    let any inconvenient pair be quietly reclassified.
    """

    # Read once or consulted; the artefact does not change.
    STATIC_REFERENCE = "STATIC_REFERENCE"
    # The buyer writes into it; the artefact captures their input.
    FILL_IN = "FILL_IN"
    # An ordered sequence the buyer works through and verifies.
    SEQUENCED_EXECUTION = "SEQUENCED_EXECUTION"
    # The buyer uses it to produce an artefact of their own.
    ARTEFACT_PRODUCTION = "ARTEFACT_PRODUCTION"
    # The buyer supplies inputs repeatedly and gets outputs back.
    REPEATED_COMPUTATION = "REPEATED_COMPUTATION"
    # Delivered over time; the schedule is part of the product.
    TIME_SEQUENCED_DELIVERY = "TIME_SEQUENCED_DELIVERY"
    # A service that keeps running; the seller's involvement continues.
    ONGOING_SERVICE = "ONGOING_SERVICE"
    # People interacting with each other; other buyers are the product.
    ONGOING_INTERACTION = "ONGOING_INTERACTION"


# Every IdealFormat's defining mode. A test asserts this mapping is total.
IDEAL_FORMAT_MODE: dict[IdealFormat, InteractionMode] = {
    IdealFormat.PDF_GUIDE: InteractionMode.STATIC_REFERENCE,
    IdealFormat.WORKBOOK: InteractionMode.FILL_IN,
    IdealFormat.CHECKLIST: InteractionMode.SEQUENCED_EXECUTION,
    IdealFormat.TEMPLATE_PACK: InteractionMode.ARTEFACT_PRODUCTION,
    IdealFormat.SPREADSHEET_TOOL: InteractionMode.REPEATED_COMPUTATION,
    IdealFormat.DATA_TEMPLATE: InteractionMode.ARTEFACT_PRODUCTION,
    IdealFormat.PRINTABLE_BUNDLE: InteractionMode.ARTEFACT_PRODUCTION,
    IdealFormat.CALCULATOR: InteractionMode.REPEATED_COMPUTATION,
    IdealFormat.QUIZ_ASSESSMENT: InteractionMode.FILL_IN,
    IdealFormat.GENERATOR: InteractionMode.ARTEFACT_PRODUCTION,
    IdealFormat.FIVE_DAY_CHALLENGE: InteractionMode.TIME_SEQUENCED_DELIVERY,
    IdealFormat.MINI_COURSE: InteractionMode.TIME_SEQUENCED_DELIVERY,
    IdealFormat.COMMUNITY: InteractionMode.ONGOING_INTERACTION,
    IdealFormat.COACHING_OFFER: InteractionMode.ONGOING_SERVICE,
    IdealFormat.MICRO_SAAS: InteractionMode.ONGOING_SERVICE,
}

# Every buildable ProductFormat's mode. A test asserts this is total too, and
# that it agrees with IDEAL_FORMAT_MODE wherever both name the same format.
BUILDABLE_FORMAT_MODE: dict[ProductFormat, InteractionMode] = {
    ProductFormat.PDF_GUIDE: InteractionMode.STATIC_REFERENCE,
    ProductFormat.WORKBOOK: InteractionMode.FILL_IN,
    ProductFormat.CHECKLIST: InteractionMode.SEQUENCED_EXECUTION,
    ProductFormat.TEMPLATE_PACK: InteractionMode.ARTEFACT_PRODUCTION,
    ProductFormat.SPREADSHEET_TOOL: InteractionMode.REPEATED_COMPUTATION,
    ProductFormat.DATA_TEMPLATE: InteractionMode.ARTEFACT_PRODUCTION,
    ProductFormat.PRINTABLE_BUNDLE: InteractionMode.ARTEFACT_PRODUCTION,
}

# Modes a static, one-off artefact structurally CANNOT provide, whatever its
# quality. A weekly cohort is not a chapter, and other buyers are not a page.
UNDELIVERABLE_BY_ARTEFACT = frozenset(
    {
        InteractionMode.TIME_SEQUENCED_DELIVERY,
        InteractionMode.ONGOING_SERVICE,
        InteractionMode.ONGOING_INTERACTION,
    }
)


class SubstitutionFidelity(str, Enum):
    """How much of the job's needed interaction survives substitution.

    These are NOT ranked scores and never map to a number. They name which
    structural property was kept or lost, so a reader can see what the
    buildable product will and will not do.
    """

    # No substitution happened: the ideal format is buildable as itself.
    DIRECT = "DIRECT"
    # A different format, but the same interaction mode.
    MODE_PRESERVED = "MODE_PRESERVED"
    # The interaction mode changed; the buyer will use it differently.
    MODE_CHANGED = "MODE_CHANGED"
    # The needed mode is one a static artefact cannot provide at all.
    MODE_UNMET = "MODE_UNMET"
    # No format, or no job, so nothing can be compared.
    UNKNOWN = "UNKNOWN"


class FitPattern(str, Enum):
    """Where the support chain from evidence to product first breaks.

    Reported as the WEAKEST LINK: the first break, in chain order. The full
    set of findings is emitted separately as `observations`, so nothing is
    lost by summarizing. These are unordered with respect to desirability —
    ALIGNED_WITH_OBSERVED_JOB is not a recommendation and not a prediction.
    """

    # Nothing to assess.
    NO_SPECIFICATION = "NO_SPECIFICATION"
    ASSESSMENT_UNAVAILABLE = "ASSESSMENT_UNAVAILABLE"
    # A specification exists but 4C itself flagged its evidence insufficient.
    SPECIFICATION_INSUFFICIENT = "SPECIFICATION_INSUFFICIENT"
    # The job could not be classified, so fit is not assessable.
    JOB_UNKNOWN = "JOB_UNKNOWN"
    # The job rests on the candidate's own wording rather than on any
    # observed provider text. Milestone 4C calls this ASSUMED.
    JOB_ASSUMED_NOT_OBSERVED = "JOB_ASSUMED_NOT_OBSERVED"
    # Another job was within a token of winning; the format follows the job,
    # so the format choice is arbitrary between them.
    JOB_AMBIGUOUS = "JOB_AMBIGUOUS"
    # The job needs an interaction a V1-buildable artefact cannot provide.
    STRUCTURAL_NEED_UNMET = "STRUCTURAL_NEED_UNMET"
    # A substitute was used and the buyer's interaction mode changed.
    SUBSTITUTED_WITH_MODE_CHANGE = "SUBSTITUTED_WITH_MODE_CHANGE"
    # The chain holds: an observed, unambiguous job, and a buildable format
    # that preserves the interaction the job needs.
    ALIGNED_WITH_OBSERVED_JOB = "ALIGNED_WITH_OBSERVED_JOB"


@dataclass(slots=True, frozen=True)
class FitObservation:
    """One finding about the support chain, with its own claim class.

    `does_not_establish` is emitted with every observation so no finding can
    be read as a commercial conclusion.
    """

    topic: str
    finding: str
    claim_class: SpecClaimClass
    does_not_establish: str


# Every pattern's boundary statement. A test asserts the mapping is total and
# that no entry is empty, so a future pattern cannot be added without saying
# what it refuses to claim.
PATTERN_BOUNDARIES: dict[FitPattern, str] = {
    FitPattern.NO_SPECIFICATION: (
        "Nothing was assessed. This is not evidence that a product would or "
        "would not suit the problem."
    ),
    FitPattern.ASSESSMENT_UNAVAILABLE: (
        "The assessment did not complete. Nothing here argues for or against "
        "any product."
    ),
    FitPattern.SPECIFICATION_INSUFFICIENT: (
        "The specification's own evidence was insufficient, so structural fit "
        "rests on too little to mean anything. It is not a finding of misfit."
    ),
    FitPattern.JOB_UNKNOWN: (
        "The job was not classified, so no structural claim is possible. This "
        "is absent analysis, never a finding that no job exists."
    ),
    FitPattern.JOB_ASSUMED_NOT_OBSERVED: (
        "The job comes from the candidate's own wording rather than observed "
        "market evidence. The fit assessed is fit to a hypothesis."
    ),
    FitPattern.JOB_AMBIGUOUS: (
        "Another job nearly classified instead. Because the format follows "
        "the job, the recommended format would have differed. This is not a "
        "finding that either format is wrong."
    ),
    FitPattern.STRUCTURAL_NEED_UNMET: (
        "The job needs an interaction a one-off artefact cannot provide. This "
        "says the substitute will not do what the job needs; it says nothing "
        "about whether anyone would buy either one."
    ),
    FitPattern.SUBSTITUTED_WITH_MODE_CHANGE: (
        "A buildable substitute was used and the buyer will interact with it "
        "differently than the ideal format implies. Neither is established as "
        "better, and neither is established as saleable."
    ),
    FitPattern.ALIGNED_WITH_OBSERVED_JOB: (
        "The chain from observed provider text to job to format holds "
        "structurally. The job itself remains INFERRED, never observed. "
        "This is NOT product-market fit, not demand, not willingness to pay, "
        "and not evidence that the product would sell. A product can suit a "
        "job perfectly and sell nothing."
    ),
}


@dataclass(slots=True, frozen=True)
class ProductJobFitFeatures:
    """Structural properties of the support chain. No magnitudes anywhere."""

    # Job support. Counts of CITED EVIDENCE RECORDS, never of market size.
    job: str | None
    job_claim_class: SpecClaimClass
    job_supported_by_observed_evidence: bool
    job_citation_count: int
    job_matched_token_count: int

    # Ambiguity. `job_score_lead` is the winning job's margin over the
    # runner-up in 4C's own token tally; None when fewer than two jobs scored.
    job_score_lead: int | None
    job_is_ambiguous: bool
    competing_jobs: tuple[str, ...]

    # Format and structural substitution.
    ideal_format: str | None
    buildable_format: str | None
    outside_v1_build_capability: bool
    ideal_interaction_mode: str | None
    buildable_interaction_mode: str | None
    substitution_fidelity: SubstitutionFidelity

    # Specification completeness. Which fields 4C could not fill.
    unknown_specification_fields: tuple[str, ...]
    known_specification_field_count: int
    specification_insufficient_evidence: bool

    # Conflicts 4C recorded, surfaced rather than hidden.
    conflict_count: int
    conflict_topics: tuple[str, ...]

    # Reported, never ranked. 4C deliberately does not copy the market, so
    # divergence is a fact about the recommendation, not a defect in it.
    diverges_from_observed_market: bool

    features_version: str = FIT_FEATURES_VERSION


@dataclass(slots=True, frozen=True)
class ProductJobFitResult:
    """Structural fit evidence for one candidate's specification."""

    candidate_id: UUID
    state: DimensionState
    # Permanently None in 4G. No fit formula is approved, and a number here
    # would read as a probability of success, which is exactly the claim
    # this milestone refuses to make.
    value: float | None
    pattern: FitPattern
    pattern_boundary: str
    observations: tuple[FitObservation, ...]
    features: ProductJobFitFeatures | None

    # Capped against every 4C input this rests on, using 4C's own capper.
    # A fit claim is never stronger than its weakest link.
    fit_claim_class: SpecClaimClass
    # The assessment is a derivation: always INFERRED, even from OBSERVED
    # inputs, and never OBSERVED because generated text contributed to it.
    assessment_truth_class: TruthClass = TruthClass.INFERRED

    # Permanent UNKNOWN markers. Structural suitability is not demand.
    sales_probability: TruthClass = TruthClass.UNKNOWN
    conversion_probability: TruthClass = TruthClass.UNKNOWN
    product_market_fit: TruthClass = TruthClass.UNKNOWN
    willingness_to_pay: TruthClass = TruthClass.UNKNOWN
    market_size: TruthClass = TruthClass.UNKNOWN
    expected_revenue: TruthClass = TruthClass.UNKNOWN
    usefulness_to_buyer: TruthClass = TruthClass.UNKNOWN
    buyer_demand_proven: TruthClass = TruthClass.UNKNOWN

    missing_reason: str | None = None
    limitations: tuple[str, ...] = ()
    dimension_name: str = DIM_PRODUCT_JOB_FIT
    version: str = PRODUCT_JOB_FIT_VERSION
    pattern_version: str = FIT_PATTERN_VERSION
    interaction_mode_version: str = INTERACTION_MODE_VERSION


LIMITATIONS = (
    "Structural fit is not demand. A product can suit a job perfectly and "
    "sell nothing; this milestone makes no claim about sales.",
    "The job being fitted is Milestone 4C's INFERRED derivation, not an "
    "observed fact about buyers. Fit to an inferred job is inferred fit.",
    "The specification being assessed is generated. Consuming it never makes "
    "its text evidence, and no generated prose is read here.",
    "Interaction modes and substitution fidelities are an unvalidated V1 "
    "taxonomy describing structure, not a ranking of product quality.",
    "A recommendation that diverges from the observed market is reported as "
    "a fact. It is neither a defect nor an advantage.",
    "Search volume, views, review counts, listing counts and price magnitudes "
    "are deliberately not read: they cannot make a product structurally more "
    "suitable for a job.",
)


# ------------------------------------------------------------------ internals


def _observed_backed(specification: ProductSpecification) -> bool:
    """Did the job classification rest on observed provider text?

    Milestone 4C deliberately CAPS the job classification at INFERRED: a job
    read out of provider text is a derivation, never an observation, so 4C
    assigns INFERRED when stored evidence text matched its tokens and ASSUMED
    when only the candidate's own wording did. OBSERVED is unreachable there
    by design.

    So the distinction this milestone needs is INFERRED-with-citations versus
    ASSUMED, not OBSERVED versus everything else. Testing for OBSERVED would
    make this predicate permanently False and every pattern past the job step
    unreachable — which is exactly what an earlier version of this function
    did, and what a test against a real 4C specification caught.

    Reading 4C's own decision rather than re-deriving it keeps the two
    milestones from ever disagreeing about what supported the job.
    """
    classification = specification.job_classification
    return (
        classification.claim_class == SpecClaimClass.INFERRED
        and len(classification.evidence_ids) > 0
    )


def _job_margin(
    specification: ProductSpecification,
) -> tuple[int | None, tuple[str, ...]]:
    """(lead over the runner-up, jobs within that lead of the winner).

    4C's `scores` are (job name, token match count) pairs. The lead is the
    winner's margin; the competing jobs are every OTHER job whose score is
    within AMBIGUITY_MAX_LEAD of the winner's. Both are computed from 4C's
    own tally rather than re-tokenising anything.
    """
    scores = [(name, count) for name, count in specification.job_classification.scores]
    scoring = [(name, count) for name, count in scores if count > 0]
    if len(scoring) < 2:
        return None, ()
    ordered = sorted(scoring, key=lambda pair: (-pair[1], pair[0]))
    top_score = ordered[0][1]
    lead = top_score - ordered[1][1]
    competing = tuple(
        sorted(
            name
            for name, count in ordered[1:]
            if top_score - count <= AMBIGUITY_MAX_LEAD
        )
    )
    return lead, competing


def _fidelity(
    ideal: IdealFormat | None, buildable: ProductFormat | None
) -> tuple[SubstitutionFidelity, InteractionMode | None, InteractionMode | None]:
    """Derive substitution fidelity from the two formats' interaction modes.

    Derived, never asserted pair by pair: the modes are stated once per
    format and the comparison follows from them, so a reader can check the
    reasoning and an inconvenient pair cannot be quietly reclassified.
    """
    if ideal is None or buildable is None:
        return SubstitutionFidelity.UNKNOWN, None, None

    ideal_mode = IDEAL_FORMAT_MODE[ideal]
    buildable_mode = BUILDABLE_FORMAT_MODE[buildable]

    if ideal.value == buildable.value:
        return SubstitutionFidelity.DIRECT, ideal_mode, buildable_mode
    if ideal_mode in UNDELIVERABLE_BY_ARTEFACT:
        # Nothing V1 can build provides this mode, so the substitute does not
        # merely differ — it cannot do what the job needs.
        return SubstitutionFidelity.MODE_UNMET, ideal_mode, buildable_mode
    if ideal_mode == buildable_mode:
        return SubstitutionFidelity.MODE_PRESERVED, ideal_mode, buildable_mode
    return SubstitutionFidelity.MODE_CHANGED, ideal_mode, buildable_mode


# Specification fields 4G inspects for completeness. Only their claim classes
# are read; the generated values behind them never are.
_COMPLETENESS_FIELDS = ("product_name", "target_buyer", "buyer_job", "core_promise")


def _unknown_fields(specification: ProductSpecification) -> tuple[str, ...]:
    """Which specification fields 4C could not fill, by NAME only."""
    unknown = []
    for name in _COMPLETENESS_FIELDS:
        field = getattr(specification, name)
        if field.claim_class == SpecClaimClass.UNKNOWN:
            unknown.append(name)
    return tuple(sorted(unknown))


def _build_features(specification: ProductSpecification) -> ProductJobFitFeatures:
    classification = specification.job_classification
    recommendation = specification.format_recommendation

    lead, competing = _job_margin(specification)
    ideal = recommendation.ideal_format if recommendation else None
    buildable = recommendation.buildable_v1_format if recommendation else None
    fidelity, ideal_mode, buildable_mode = _fidelity(ideal, buildable)

    unknown = _unknown_fields(specification)
    conflicts = tuple(sorted({c.topic for c in specification.conflicts}))

    job_known = classification.job != JobToBeDone.UNKNOWN

    return ProductJobFitFeatures(
        job=classification.job.value if job_known else None,
        job_claim_class=classification.claim_class,
        job_supported_by_observed_evidence=_observed_backed(specification),
        job_citation_count=len(classification.evidence_ids),
        job_matched_token_count=len(classification.matched_tokens),
        job_score_lead=lead,
        job_is_ambiguous=(
            job_known and lead is not None and lead <= AMBIGUITY_MAX_LEAD
        ),
        competing_jobs=competing,
        ideal_format=ideal.value if ideal else None,
        buildable_format=buildable.value if buildable else None,
        outside_v1_build_capability=(
            recommendation.outside_v1_build_capability if recommendation else False
        ),
        ideal_interaction_mode=ideal_mode.value if ideal_mode else None,
        buildable_interaction_mode=buildable_mode.value if buildable_mode else None,
        substitution_fidelity=fidelity,
        unknown_specification_fields=unknown,
        known_specification_field_count=len(_COMPLETENESS_FIELDS) - len(unknown),
        specification_insufficient_evidence=specification.insufficient_evidence,
        conflict_count=len(specification.conflicts),
        conflict_topics=conflicts,
        diverges_from_observed_market=(
            recommendation.diverges_from_observed_market if recommendation else False
        ),
    )


def _observations(
    features: ProductJobFitFeatures, specification: ProductSpecification
) -> tuple[FitObservation, ...]:
    """Every finding about the chain, not only the weakest link.

    Emitted in a fixed order so the same specification always produces the
    same sequence.
    """
    found: list[FitObservation] = []

    if features.specification_insufficient_evidence:
        found.append(
            FitObservation(
                topic="specification_evidence",
                finding=(
                    "Milestone 4C flagged the underlying evidence as "
                    "insufficient for this specification."
                ),
                claim_class=SpecClaimClass.UNKNOWN,
                does_not_establish=(
                    "That the product is unsuitable. Too little was measured "
                    "to assess suitability at all."
                ),
            )
        )

    if features.job is None:
        found.append(
            FitObservation(
                topic="job_classification",
                finding="The buyer job could not be classified from evidence.",
                claim_class=SpecClaimClass.UNKNOWN,
                does_not_establish=(
                    "That no job exists. The classifier is a V1 keyword "
                    "heuristic and its silence is not an absence of need."
                ),
            )
        )
    else:
        if not features.job_supported_by_observed_evidence:
            found.append(
                FitObservation(
                    topic="job_support",
                    finding=(
                        "The job rests on the candidate's own wording rather "
                        "than on observed market evidence."
                    ),
                    claim_class=features.job_claim_class,
                    does_not_establish=(
                        "That the job is wrong. It establishes only that "
                        "nothing observed corroborates it."
                    ),
                )
            )
        if features.job_is_ambiguous:
            found.append(
                FitObservation(
                    topic="job_ambiguity",
                    finding=(
                        "Another job scored within one token of the winner, "
                        "and the recommended format follows the job."
                    ),
                    claim_class=SpecClaimClass.INFERRED,
                    does_not_establish=(
                        "That either job is correct. It establishes that the "
                        "choice between them was close."
                    ),
                )
            )

    if features.substitution_fidelity == SubstitutionFidelity.MODE_UNMET:
        found.append(
            FitObservation(
                topic="structural_substitution",
                finding=(
                    "The job's ideal format needs an interaction a one-off "
                    "artefact cannot provide, so the buildable substitute "
                    "cannot do what the job needs."
                ),
                claim_class=SpecClaimClass.INFERRED,
                does_not_establish=(
                    "That the substitute is worthless, or that the ideal "
                    "format would sell."
                ),
            )
        )
    elif features.substitution_fidelity == SubstitutionFidelity.MODE_CHANGED:
        found.append(
            FitObservation(
                topic="structural_substitution",
                finding=(
                    "A buildable substitute changes how the buyer interacts "
                    "with the product."
                ),
                claim_class=SpecClaimClass.INFERRED,
                does_not_establish=(
                    "That the substitute is worse. It establishes that the "
                    "interaction differs."
                ),
            )
        )

    if features.unknown_specification_fields:
        found.append(
            FitObservation(
                topic="specification_completeness",
                finding=(
                    "The specification left one or more core fields UNKNOWN: "
                    + ", ".join(features.unknown_specification_fields)
                ),
                claim_class=SpecClaimClass.UNKNOWN,
                does_not_establish=(
                    "That the product is ill-defined in principle. A field is "
                    "UNKNOWN when evidence did not support filling it."
                ),
            )
        )

    if features.conflict_count:
        found.append(
            FitObservation(
                topic="evidence_conflicts",
                finding=(
                    "Milestone 4C recorded conflicting signals: "
                    + ", ".join(features.conflict_topics)
                ),
                claim_class=SpecClaimClass.INFERRED,
                does_not_establish=(
                    "Which side of the conflict is right. Both were preserved "
                    "rather than resolved."
                ),
            )
        )

    if features.diverges_from_observed_market:
        found.append(
            FitObservation(
                topic="market_divergence",
                finding=(
                    "The recommended format differs from the format observed "
                    "competitors ship."
                ),
                claim_class=SpecClaimClass.INFERRED,
                does_not_establish=(
                    "That either format is better. Milestone 4C follows the "
                    "job deliberately rather than copying the market."
                ),
            )
        )

    return tuple(found)


def _classify(features: ProductJobFitFeatures) -> FitPattern:
    """fit_assessment_pattern_v1: the FIRST break in the support chain.

    Order is the chain's own order, not a ranking of the patterns. Each rule
    is reached only when the ones above it did not apply, which is what makes
    the summary explainable; the complete set of findings is emitted
    separately as `observations`, so summarizing loses nothing.
    """
    if features.specification_insufficient_evidence:
        return FitPattern.SPECIFICATION_INSUFFICIENT
    if features.job is None:
        return FitPattern.JOB_UNKNOWN
    if not features.job_supported_by_observed_evidence:
        return FitPattern.JOB_ASSUMED_NOT_OBSERVED
    if features.job_is_ambiguous:
        return FitPattern.JOB_AMBIGUOUS
    if features.substitution_fidelity == SubstitutionFidelity.MODE_UNMET:
        return FitPattern.STRUCTURAL_NEED_UNMET
    if features.substitution_fidelity == SubstitutionFidelity.MODE_CHANGED:
        return FitPattern.SUBSTITUTED_WITH_MODE_CHANGE
    return FitPattern.ALIGNED_WITH_OBSERVED_JOB


def _fit_claim_class(
    specification: ProductSpecification, features: ProductJobFitFeatures
) -> SpecClaimClass:
    """Weakest link, using Milestone 4C's own capper.

    The assessment rests on the job classification and, when a format was
    recommended, on both of its claim classes. It can never be stronger than
    any of them.
    """
    recommendation = specification.format_recommendation
    inputs = [specification.job_classification.claim_class]
    if recommendation is not None:
        inputs.append(recommendation.ideal_claim_class)
        inputs.append(recommendation.buildable_claim_class)
    if features.substitution_fidelity == SubstitutionFidelity.UNKNOWN:
        inputs.append(SpecClaimClass.UNKNOWN)
    return cap_claim_class(*inputs)


# ----------------------------------------------------------------- extraction


def _unassessable(
    candidate_id: UUID, pattern: FitPattern, missing_reason: str
) -> ProductJobFitResult:
    return ProductJobFitResult(
        candidate_id=candidate_id,
        state=DimensionState.MISSING,
        value=None,
        pattern=pattern,
        pattern_boundary=PATTERN_BOUNDARIES[pattern],
        observations=(),
        features=None,
        fit_claim_class=SpecClaimClass.UNKNOWN,
        missing_reason=missing_reason,
        limitations=LIMITATIONS,
    )


def extract_product_job_fit(
    candidate_id: UUID,
    specification: ProductSpecification | None,
) -> ProductJobFitResult:
    """Assess structural fit between a 4C specification and its own job.

    Deterministic and pure: the same specification yields the same result.
    No provider calls, no network, no LLM, no fabricated values, and no
    conversion of structural suitability into demand, sales, or intent.

    `specification` is None when Milestone 4C produced none. That is
    unassessable, which is never a finding of misfit.
    """
    if specification is None:
        return _unassessable(
            candidate_id,
            FitPattern.NO_SPECIFICATION,
            "no_product_specification_available",
        )
    if specification.state == SpecificationState.MISSING:
        return _unassessable(
            candidate_id,
            FitPattern.NO_SPECIFICATION,
            specification.missing_reason or "specification_state_missing",
        )

    features = _build_features(specification)
    pattern = _classify(features)
    observations = _observations(features, specification)
    claim_class = _fit_claim_class(specification, features)

    # An insufficient or unclassifiable chain is UNKNOWN, not a fit finding.
    if pattern in (
        FitPattern.SPECIFICATION_INSUFFICIENT,
        FitPattern.JOB_UNKNOWN,
    ):
        state = DimensionState.UNKNOWN
        missing_reason = (
            "specification_evidence_insufficient"
            if pattern == FitPattern.SPECIFICATION_INSUFFICIENT
            else "job_not_classified_from_evidence"
        )
    else:
        state = DimensionState.EVIDENCE_PRESENT_UNSCORED
        missing_reason = None

    return ProductJobFitResult(
        candidate_id=candidate_id,
        state=state,
        value=None,
        pattern=pattern,
        pattern_boundary=PATTERN_BOUNDARIES[pattern],
        observations=observations,
        features=features,
        fit_claim_class=claim_class,
        missing_reason=missing_reason,
        limitations=LIMITATIONS,
    )
