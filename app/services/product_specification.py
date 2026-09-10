"""Product Specification generation (Milestone 4C).

The questions this answers, for ONE selected candidate:

    What specific digital product could solve the buyer's problem?
    Who is it for?
    What job does it address?
    Which product format best fits that job?
    What should the product contain?
    What evidence supports each decision?
    What remains assumption or inference?

The questions it deliberately does NOT answer:

    Will this product sell?
    What should it be priced at?
    How much revenue would it make?
    What is the conversion rate?

Nothing here fabricates a customer problem, demand, purchase, sale, revenue,
market share, conversion rate, willingness to pay, or competitor performance.

What this milestone produces
----------------------------

A production-ready product SPECIFICATION — a blueprint. It does not render a
PDF, build a spreadsheet, or generate any finished artifact.

Claim classes
-------------

Every material field carries a `SpecClaimClass` recording how well evidence
supports it:

    OBSERVED   directly supported by collected evidence
    INFERRED   a reasonable derivation from observed evidence
    ASSUMED    a design hypothesis requiring validation
    UNKNOWN    evidence unavailable

This is deliberately LOCAL to 4C. The shared `TruthClass` has no ASSUMED
member, and adding one to support a single milestone would change a domain
enum every prior milestone left untouched. Mapping from TruthClass is
conservative and never upgrades: ESTIMATED maps to INFERRED, never OBSERVED.

The hard rule, enforced by `cap_claim_class`: **the weakest relevant source
class caps the derived field. No derivation may increase evidentiary
certainty.**

Ideal format vs. buildable format
---------------------------------

Format selection follows the buyer's job-to-be-done, NOT the dominant
competitor format. A job of repeated calculation favours a calculator or
spreadsheet even when the observed market ships long PDFs.

That produces two separate answers, which are never conflated:

    ideal_format        best conceptual fit for the job. May fall outside
                        what V1 can build. Always ASSUMED, and flagged
                        `outside_v1_build_capability` when it does.

    buildable_v1_format best fit among the seven formats Milestone 1
                        approves and this system can actually build.

Milestone 1's candidate validator is NOT modified, relaxed, or bypassed. A
broader ideal format is a design suggestion about fit; it is never evidence
that the market validated that format.

Generative boundary
-------------------

Every decision here is deterministic: format selection, job classification,
structure, assets, claim classes, evidence citation. A future
`ProductSpecificationProvider` may reword prose only. It can never select or
change a claim class, create or alter evidence, produce a number, change the
format decision, or turn an assumption into a finding — `apply_prose` accepts
only replacement strings for narrative fields and re-validates them.
"""

import re
import unicodedata
from dataclasses import dataclass, replace
from enum import Enum
from uuid import UUID

from app.domain.enums import ProductFormat, TruthClass
from app.domain.models import Candidate, EvidenceItem
from app.services.price_evidence import PriceEvidenceResult
from app.services.purchase_evidence import PurchaseEvidenceResult

PRODUCT_SPECIFICATION_VERSION = "product_specification_v1"
JOB_TAXONOMY_VERSION = "job_to_be_done_v1"
FORMAT_SELECTION_VERSION = "format_selection_v1"

SIGNAL_SEARCH_VOLUME = "search_volume"
SIGNAL_VIDEO_VIEWS = "public_video_view_count"
SIGNAL_CONTENT_OBSERVATION = "public_content_observation"
# The competing-listing record carries the listing payload and is always
# OBSERVED, unlike the price record, which is UNKNOWN when the provider
# returned no price. Listing titles and types are read from it so that a
# priceless listing does not silently lose its observed title.
SIGNAL_COMPETING_LISTING = "marketplace_competing_listing"


class SpecClaimClass(str, Enum):
    """How well evidence supports one specification field.

    Ordered from strongest to weakest. `cap_claim_class` uses that ordering
    to guarantee a derived field is never stronger than its weakest input.
    """

    OBSERVED = "OBSERVED"
    INFERRED = "INFERRED"
    ASSUMED = "ASSUMED"
    UNKNOWN = "UNKNOWN"


# Weakest wins. ASSUMED sits between INFERRED and UNKNOWN: a design
# hypothesis is weaker than a derivation from evidence, but it is still a
# deliberate claim rather than an absence of information.
_CLAIM_STRENGTH = {
    SpecClaimClass.OBSERVED: 0,
    SpecClaimClass.INFERRED: 1,
    SpecClaimClass.ASSUMED: 2,
    SpecClaimClass.UNKNOWN: 3,
}

# Conservative mapping from the shared evidence TruthClass. No class is ever
# upgraded: ESTIMATED becomes INFERRED, never OBSERVED.
_TRUTH_TO_CLAIM = {
    TruthClass.OBSERVED: SpecClaimClass.OBSERVED,
    TruthClass.ESTIMATED: SpecClaimClass.INFERRED,
    TruthClass.INFERRED: SpecClaimClass.INFERRED,
    TruthClass.UNKNOWN: SpecClaimClass.UNKNOWN,
}


def claim_from_truth(truth: TruthClass | None) -> SpecClaimClass:
    """Map an evidence truth class to a claim class, never upgrading."""
    if truth is None:
        return SpecClaimClass.UNKNOWN
    return _TRUTH_TO_CLAIM[truth]


def cap_claim_class(*classes: SpecClaimClass | None) -> SpecClaimClass:
    """Return the weakest class given. No derivation raises certainty."""
    present = [c for c in classes if c is not None]
    if not present:
        return SpecClaimClass.UNKNOWN
    return max(present, key=lambda c: _CLAIM_STRENGTH[c])


class JobToBeDone(str, Enum):
    """What the buyer is trying to get done.

    UNVALIDATED V1 TAXONOMY (`job_to_be_done_v1`). This set and its keyword
    mappings are a documented heuristic, not an empirically proven model of
    buyer behaviour. UNKNOWN is returned whenever signals are absent or
    conflict too evenly to classify confidently.
    """

    CALCULATE = "CALCULATE"
    DECIDE = "DECIDE"
    PLAN = "PLAN"
    TRACK = "TRACK"
    LEARN = "LEARN"
    EXECUTE = "EXECUTE"
    ASSESS = "ASSESS"
    ORGANISE = "ORGANISE"
    UNKNOWN = "UNKNOWN"


# Deterministic token -> job mapping. UNVALIDATED V1 HEURISTIC: these words
# were chosen by inspection of how buyers phrase intent, not by measurement.
# Tokens are matched on word boundaries against observed text only (keyword
# strings, listing titles, video titles/tags) — never against invented text.
JOB_TOKENS: dict[JobToBeDone, tuple[str, ...]] = {
    JobToBeDone.CALCULATE: (
        "calculator", "calculate", "calc", "how much", "cost", "pricing",
        "ratio", "conversion", "formula", "estimator", "estimate", "math",
        "percentage", "hydration", "macro", "dosage", "measurement",
    ),
    JobToBeDone.DECIDE: (
        # NOTE: "or", "which" and "best" were deliberately removed. They are
        # stopword-grade: "meal chart for mums or dads" is not a decision
        # product, and a superlative appears in most marketing copy. A token
        # must carry intent on its own, not merely occur in English.
        "vs", "versus", "compare", "comparison", "choose",
        "choosing", "decision", "should i", "alternative", "pros and cons",
    ),
    JobToBeDone.PLAN: (
        "planner", "plan", "planning", "schedule", "calendar", "roadmap",
        "itinerary", "timeline", "weekly", "monthly", "meal plan",
    ),
    JobToBeDone.TRACK: (
        "tracker", "track", "tracking", "log", "logger", "journal", "monitor",
        "progress", "habit", "record", "diary",
    ),
    JobToBeDone.LEARN: (
        "guide", "tutorial", "how to", "learn", "learning", "beginner",
        "basics", "explained", "introduction", "course", "lesson", "teach",
    ),
    JobToBeDone.EXECUTE: (
        "checklist", "steps", "step by step", "process", "workflow", "recipe",
        "instructions", "procedure", "setup", "template", "swipe",
    ),
    JobToBeDone.ASSESS: (
        "quiz", "assessment", "audit", "score", "scorecard", "test",
        "evaluate", "evaluation", "diagnostic", "readiness", "self assessment",
    ),
    JobToBeDone.ORGANISE: (
        "organizer", "organiser", "organize", "organise", "inventory",
        "catalog", "catalogue", "database", "library", "index", "system",
    ),
}

# Words too common in ordinary English or marketing copy to signal intent.
# A SINGLE-WORD job token must never be one of these: it would classify on
# grammar rather than on what the buyer is trying to get done. Multi-word
# tokens may contain them ("how much", "should i"), because the phrase
# carries intent that neither word carries alone.
JOB_TOKEN_STOPWORDS = frozenset(
    {
        "a", "an", "and", "or", "the", "for", "with", "your", "you", "my",
        "to", "of", "in", "on", "at", "by", "is", "it", "this", "that",
        "which", "what", "who", "how", "best", "top", "new", "free", "easy",
        "simple", "ultimate", "perfect", "great", "good",
    }
)

# A job must lead the runner-up by at least this margin to be claimed.
# Without a clear lead the result is UNKNOWN rather than a forced guess.
# UNVALIDATED V1 ASSUMPTION.
JOB_CLASSIFICATION_MIN_LEAD = 1

# Minimum matched tokens before any job is claimed at all.
# UNVALIDATED V1 ASSUMPTION.
JOB_CLASSIFICATION_MIN_MATCHES = 1


class IdealFormat(str, Enum):
    """Best conceptual fit for a job, independent of build capability.

    The first seven mirror `ProductFormat` and ARE buildable in V1. The rest
    are conceptual only: Milestone 1 rejects candidates of these kinds and
    this system cannot build them, so recommending one is always ASSUMED and
    always flagged `outside_v1_build_capability`.
    """

    # Buildable in V1
    PDF_GUIDE = "PDF_GUIDE"
    WORKBOOK = "WORKBOOK"
    CHECKLIST = "CHECKLIST"
    TEMPLATE_PACK = "TEMPLATE_PACK"
    SPREADSHEET_TOOL = "SPREADSHEET_TOOL"
    DATA_TEMPLATE = "DATA_TEMPLATE"
    PRINTABLE_BUNDLE = "PRINTABLE_BUNDLE"
    # Beyond V1 build capability
    CALCULATOR = "CALCULATOR"
    QUIZ_ASSESSMENT = "QUIZ_ASSESSMENT"
    GENERATOR = "GENERATOR"
    FIVE_DAY_CHALLENGE = "FIVE_DAY_CHALLENGE"
    MINI_COURSE = "MINI_COURSE"
    COMMUNITY = "COMMUNITY"
    COACHING_OFFER = "COACHING_OFFER"
    MICRO_SAAS = "MICRO_SAAS"


# The seven formats Milestone 1 approves. Kept as an explicit set so a
# reviewer can see exactly which ideal formats are buildable.
V1_BUILDABLE_IDEAL_FORMATS = frozenset(
    {
        IdealFormat.PDF_GUIDE,
        IdealFormat.WORKBOOK,
        IdealFormat.CHECKLIST,
        IdealFormat.TEMPLATE_PACK,
        IdealFormat.SPREADSHEET_TOOL,
        IdealFormat.DATA_TEMPLATE,
        IdealFormat.PRINTABLE_BUNDLE,
    }
)

# Job -> ideal format. UNVALIDATED V1 HEURISTIC (`format_selection_v1`).
# Driven by the job, deliberately NOT by the dominant competitor format: a
# repeated-calculation job favours a calculator over a long PDF even when
# every observed competitor ships a PDF.
JOB_TO_IDEAL_FORMAT: dict[JobToBeDone, IdealFormat] = {
    JobToBeDone.CALCULATE: IdealFormat.CALCULATOR,
    JobToBeDone.DECIDE: IdealFormat.QUIZ_ASSESSMENT,
    JobToBeDone.PLAN: IdealFormat.TEMPLATE_PACK,
    JobToBeDone.TRACK: IdealFormat.SPREADSHEET_TOOL,
    JobToBeDone.LEARN: IdealFormat.PDF_GUIDE,
    JobToBeDone.EXECUTE: IdealFormat.CHECKLIST,
    JobToBeDone.ASSESS: IdealFormat.QUIZ_ASSESSMENT,
    JobToBeDone.ORGANISE: IdealFormat.DATA_TEMPLATE,
}

# Ideal format -> nearest V1-buildable substitute. A calculator becomes a
# spreadsheet tool because a spreadsheet performs repeated calculation with
# the formats V1 can actually ship. UNVALIDATED V1 HEURISTIC.
IDEAL_TO_BUILDABLE: dict[IdealFormat, ProductFormat] = {
    IdealFormat.PDF_GUIDE: ProductFormat.PDF_GUIDE,
    IdealFormat.WORKBOOK: ProductFormat.WORKBOOK,
    IdealFormat.CHECKLIST: ProductFormat.CHECKLIST,
    IdealFormat.TEMPLATE_PACK: ProductFormat.TEMPLATE_PACK,
    IdealFormat.SPREADSHEET_TOOL: ProductFormat.SPREADSHEET_TOOL,
    IdealFormat.DATA_TEMPLATE: ProductFormat.DATA_TEMPLATE,
    IdealFormat.PRINTABLE_BUNDLE: ProductFormat.PRINTABLE_BUNDLE,
    IdealFormat.CALCULATOR: ProductFormat.SPREADSHEET_TOOL,
    IdealFormat.QUIZ_ASSESSMENT: ProductFormat.WORKBOOK,
    IdealFormat.GENERATOR: ProductFormat.TEMPLATE_PACK,
    IdealFormat.FIVE_DAY_CHALLENGE: ProductFormat.WORKBOOK,
    IdealFormat.MINI_COURSE: ProductFormat.WORKBOOK,
    IdealFormat.COMMUNITY: ProductFormat.PDF_GUIDE,
    IdealFormat.COACHING_OFFER: ProductFormat.WORKBOOK,
    IdealFormat.MICRO_SAAS: ProductFormat.SPREADSHEET_TOOL,
}

# Deterministic module structure per buildable format. UNVALIDATED V1
# TEMPLATE: a starting blueprint, not a validated content model.
FORMAT_STRUCTURE: dict[ProductFormat, tuple[tuple[str, str], ...]] = {
    ProductFormat.PDF_GUIDE: (
        ("Orientation", "Frame the problem and what the reader will be able to do."),
        ("Core explanation", "The central method, explained in order."),
        ("Worked example", "One end-to-end example the reader can follow."),
        ("Common mistakes", "Failure points and how to avoid them."),
        ("Next steps", "What to do immediately after reading."),
    ),
    ProductFormat.WORKBOOK: (
        ("Starting point", "Where the reader is now, captured in writing."),
        ("Guided exercises", "Prompts the reader completes in sequence."),
        ("Reflection checkpoints", "Short reviews between exercise blocks."),
        ("Output summary", "A filled-in artefact the reader keeps."),
    ),
    ProductFormat.CHECKLIST: (
        ("Preconditions", "What must be true before starting."),
        ("Ordered steps", "The sequence, each independently verifiable."),
        ("Verification", "How to confirm each step actually completed."),
        ("Troubleshooting", "What to do when a step fails."),
    ),
    ProductFormat.TEMPLATE_PACK: (
        ("Template index", "What each template is for and when to use it."),
        ("Core templates", "The reusable documents themselves."),
        ("Fill-in guidance", "How to adapt each template."),
        ("Worked sample", "One completed template as a reference."),
    ),
    ProductFormat.SPREADSHEET_TOOL: (
        ("Input sheet", "Fields the user enters, with units and ranges."),
        ("Calculation engine", "Formulas, documented and inspectable."),
        ("Results view", "Outputs presented for the decision at hand."),
        ("Scenario comparison", "Side-by-side alternatives."),
        ("Instructions tab", "How to use and adapt the workbook."),
    ),
    ProductFormat.DATA_TEMPLATE: (
        ("Schema definition", "Columns, types, and what each records."),
        ("Seed rows", "Example records showing intended use."),
        ("Validation rules", "Constraints that keep entries consistent."),
        ("Import/export notes", "How to move data in and out."),
    ),
    ProductFormat.PRINTABLE_BUNDLE: (
        ("Cover and index", "What the bundle contains."),
        ("Core printables", "The individual printable pages."),
        ("Print guidance", "Sizes, margins, and paper notes."),
        ("Usage suggestions", "How to combine the pages in practice."),
    ),
}

# Deterministic asset list per buildable format. UNVALIDATED V1 TEMPLATE.
FORMAT_ASSETS: dict[ProductFormat, tuple[str, ...]] = {
    ProductFormat.PDF_GUIDE: ("Written copy", "Page layout", "Cover design", "Diagrams"),
    ProductFormat.WORKBOOK: ("Exercise copy", "Fillable fields", "Page layout", "Cover design"),
    ProductFormat.CHECKLIST: ("Step copy", "Single-page layout", "Print-safe styling"),
    ProductFormat.TEMPLATE_PACK: ("Template files", "Fill-in guidance copy", "Sample completion"),
    ProductFormat.SPREADSHEET_TOOL: (
        "Spreadsheet file", "Documented formulas", "Input validation", "Instructions tab",
    ),
    ProductFormat.DATA_TEMPLATE: ("Data file", "Schema documentation", "Seed rows"),
    ProductFormat.PRINTABLE_BUNDLE: ("Printable pages", "Cover design", "Print guidance copy"),
}

LIMITATIONS = (
    "This is a product SPECIFICATION, not a finished product. No PDF, spreadsheet, "
    "course, or application is rendered by this milestone.",
    "No claim is made that this product will sell. Nothing here is a forecast, a "
    "probability of success, or a commercial guarantee.",
    "The buyer problem and target buyer originate in a Milestone 1 candidate "
    "hypothesis. No qualitative buyer research exists in this system, so they are "
    "never OBSERVED.",
    "The job-to-be-done taxonomy and its keyword mappings are an unvalidated V1 "
    "heuristic, not an empirically proven model of buyer behaviour.",
    "Format selection follows the job-to-be-done, not the dominant competitor "
    "format. It is a design judgment, and never proof that a format works.",
    "An ideal format outside V1 build capability is a conceptual suggestion only. "
    "The market did not validate it; the system inferred it.",
    "Module structure and asset lists are unvalidated V1 templates: a starting "
    "blueprint, not a validated content model.",
    "Price evidence is cited from Milestone 4B as OBSERVED ASKING prices. It is "
    "never a recommended price, an optimal price, a transaction price, or a "
    "willingness-to-pay estimate.",
    "Competitor patterns are observed listing titles and formats only. Competitor "
    "product CONTENTS are not collected and remain UNKNOWN.",
    "Differentiation opportunities are design hypotheses requiring validation, not "
    "observed gaps in competitor products.",
)


@dataclass(slots=True, frozen=True)
class SpecField:
    """One specification field with its claim class and provenance.

    `basis` states why the class was assigned, so a future UI can answer
    "why did the system recommend this?" per field rather than per document.
    """

    value: str | None
    claim_class: SpecClaimClass
    basis: str
    evidence_ids: tuple[UUID, ...] = ()
    signal_types: tuple[str, ...] = ()

    @property
    def is_known(self) -> bool:
        return self.claim_class != SpecClaimClass.UNKNOWN and self.value is not None


@dataclass(slots=True, frozen=True)
class ProductModule:
    name: str
    purpose: str
    claim_class: SpecClaimClass = SpecClaimClass.ASSUMED


@dataclass(slots=True, frozen=True)
class FormatRecommendation:
    """The two-answer format result. Never conflated.

    `ideal_format` is the best conceptual fit for the job and is always
    ASSUMED. `buildable_v1_format` is the best fit among Milestone 1's seven
    approved formats and is what this system can actually produce.
    """

    ideal_format: IdealFormat | None
    ideal_claim_class: SpecClaimClass
    outside_v1_build_capability: bool
    buildable_v1_format: ProductFormat | None
    buildable_claim_class: SpecClaimClass
    rationale: str
    driven_by_job: JobToBeDone
    # What the observed market actually ships, for contrast. Observing that
    # competitors use a different format is never a reason to copy it.
    observed_dominant_format: str | None
    diverges_from_observed_market: bool
    selection_version: str = FORMAT_SELECTION_VERSION


@dataclass(slots=True, frozen=True)
class PriceEvidenceReference:
    """A citation of 4B evidence. Never a recommended or optimal price."""

    available: bool
    state: str | None
    currencies: tuple[str, ...] = ()
    # (currency, paid_listing_count, min, median, max, insufficient_evidence)
    observed_asking_bands: tuple[tuple[str, int, float, float, float, bool], ...] = ()
    free_listing_count: int | None = None
    price_evidence_version: str | None = None
    limitations: tuple[str, ...] = ()
    note: str = (
        "Cited from Milestone 4B as OBSERVED ASKING prices. This is not a "
        "recommended price, an optimal price, a transaction price, or a "
        "willingness-to-pay estimate. This milestone recommends no price."
    )


@dataclass(slots=True, frozen=True)
class EvidenceConflict:
    """A disagreement between signals, preserved rather than hidden."""

    topic: str
    description: str
    resolution: str


@dataclass(slots=True, frozen=True)
class JobClassification:
    job: JobToBeDone
    claim_class: SpecClaimClass
    matched_tokens: tuple[str, ...]
    scores: tuple[tuple[str, int], ...]
    basis: str
    evidence_ids: tuple[UUID, ...] = ()
    signal_types: tuple[str, ...] = ()
    taxonomy_version: str = JOB_TAXONOMY_VERSION


class SpecificationState(str, Enum):
    MISSING = "MISSING"
    GENERATED = "GENERATED"


@dataclass(slots=True, frozen=True)
class ProductSpecification:
    candidate_id: UUID
    research_run_id: UUID | None
    state: SpecificationState
    product_name: SpecField
    target_buyer: SpecField
    buyer_job: SpecField
    job_classification: JobClassification
    format_recommendation: FormatRecommendation | None
    core_promise: SpecField
    structure: tuple[ProductModule, ...]
    required_assets: tuple[str, ...]
    differentiation_opportunities: tuple[SpecField, ...]
    observed_competitor_patterns: tuple[SpecField, ...]
    price_evidence_reference: PriceEvidenceReference
    supporting_evidence_ids: tuple[UUID, ...]
    assumptions: tuple[str, ...]
    unknowns: tuple[str, ...]
    conflicts: tuple[EvidenceConflict, ...]
    limitations: tuple[str, ...] = LIMITATIONS
    insufficient_evidence: bool = False
    missing_reason: str | None = None
    version: str = PRODUCT_SPECIFICATION_VERSION
    job_taxonomy_version: str = JOB_TAXONOMY_VERSION
    format_selection_version: str = FORMAT_SELECTION_VERSION


# ------------------------------------------------------------ text harvesting


class EvidenceOwnershipError(ValueError):
    """Raised when evidence for another candidate is passed in.

    A specification may only cite evidence collected for its own candidate;
    silently accepting a foreign record would attach real evidence ids to
    claims they do not support.
    """


_WORD_RE = re.compile(r"[a-z0-9]+")


@dataclass(slots=True)
class ObservedText:
    """Text actually returned by a provider, with its evidence lineage."""

    text: str
    evidence_id: UUID
    signal_type: str


def collect_observed_text(evidence: list[EvidenceItem]) -> list[ObservedText]:
    """Harvest OBSERVED text from stored evidence payloads.

    Only text a provider actually returned is harvested — keyword strings,
    listing titles, video titles and tags. Nothing is invented, and a record
    whose truth class is not OBSERVED contributes no text.
    """
    harvested: dict[tuple[str, str], ObservedText] = {}

    def add(key: tuple[str, str], text: str, item: EvidenceItem) -> None:
        """Record one observation, keeping the lowest evidence id per key.

        The same listing can be stored more than once — re-observed in a
        later research run, or returned by two of a candidate's marketplace
        queries. Counting one real listing twice would double-weight it in
        job classification, so each distinct observation is kept once.
        Choosing the lowest id (rather than the first seen) keeps the choice
        independent of the order evidence arrives in.
        """
        existing = harvested.get(key)
        if existing is None or item.id < existing.evidence_id:
            harvested[key] = ObservedText(text, item.id, item.signal_type)

    for item in evidence:
        if item.truth_class != TruthClass.OBSERVED:
            continue
        payload = item.raw_payload or {}
        if item.signal_type == SIGNAL_SEARCH_VOLUME:
            keyword = payload.get("keyword")
            if keyword:
                add((item.signal_type, str(keyword)), str(keyword), item)
        elif item.signal_type == SIGNAL_COMPETING_LISTING:
            # Read from the competing-listing record only. The price and
            # review records repeat the same payload; harvesting all three
            # would count one listing's words three times.
            # Keyed by listing id, so one listing counts once however many
            # times it was stored.
            listing_id = str(payload.get("listing_id") or item.id)
            for key in ("title", "taxonomy"):
                value = payload.get(key)
                if value:
                    add((f"listing:{key}", listing_id), str(value), item)
        elif item.signal_type in (SIGNAL_VIDEO_VIEWS, SIGNAL_CONTENT_OBSERVATION):
            video_id = str(payload.get("video_id") or item.id)
            for key in ("title", "description"):
                value = payload.get(key)
                if value:
                    add((f"video:{key}", video_id), str(value), item)
            for tag in payload.get("tags") or ():
                add((f"video:tag:{tag}", video_id), str(tag), item)
    # Canonical order, independent of the order evidence was stored in.
    return [harvested[key] for key in sorted(harvested)]


def classify_job(
    observed: list[ObservedText], candidate: Candidate
) -> JobClassification:
    """job_to_be_done_v1: deterministic token matching over OBSERVED text.

    The candidate's own problem/outcome text is scored too, but separately:
    it is a Milestone 1 hypothesis, not evidence, so a job supported only by
    candidate text is INFERRED at best and never OBSERVED.
    """
    scores: dict[JobToBeDone, int] = {job: 0 for job in JOB_TOKENS}
    # Matches from OBSERVED provider text only, tracked PER JOB. A record
    # that supported some other job is not evidence for the winning one, so
    # provenance is accumulated per job rather than globally.
    observed_matches: dict[JobToBeDone, int] = {job: 0 for job in JOB_TOKENS}
    matched: dict[JobToBeDone, list[str]] = {job: [] for job in JOB_TOKENS}
    contributing: dict[JobToBeDone, set[UUID]] = {job: set() for job in JOB_TOKENS}
    signals: dict[JobToBeDone, set[str]] = {job: set() for job in JOB_TOKENS}

    def score_text(text: str, evidence_id: UUID | None, signal: str | None) -> None:
        lowered = " ".join(_WORD_RE.findall(text.lower()))
        for job, tokens in JOB_TOKENS.items():
            for token in tokens:
                if re.search(rf"\b{re.escape(token)}\b", lowered):
                    scores[job] += 1
                    matched[job].append(token)
                    if evidence_id is not None:
                        observed_matches[job] += 1
                        contributing[job].add(evidence_id)
                        if signal:
                            signals[job].add(signal)

    for entry in observed:
        score_text(entry.text, entry.evidence_id, entry.signal_type)
    for text in (candidate.title, candidate.problem, candidate.buyer_outcome):
        score_text(text, None, None)

    ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0].value))
    top_job, top_score = ranked[0]
    runner_up_score = ranked[1][1] if len(ranked) > 1 else 0

    score_tuple = tuple((job.value, count) for job, count in ranked if count > 0)

    if top_score < JOB_CLASSIFICATION_MIN_MATCHES:
        return JobClassification(
            job=JobToBeDone.UNKNOWN,
            claim_class=SpecClaimClass.UNKNOWN,
            matched_tokens=(),
            scores=score_tuple,
            basis="no job tokens matched any observed text or candidate text",
        )

    if top_score - runner_up_score < JOB_CLASSIFICATION_MIN_LEAD:
        tied = [job.value for job, count in ranked if count == top_score]
        return JobClassification(
            job=JobToBeDone.UNKNOWN,
            claim_class=SpecClaimClass.UNKNOWN,
            matched_tokens=tuple(sorted(set(matched[top_job]))),
            scores=score_tuple,
            basis=(
                f"signals did not separate the leading jobs ({', '.join(sorted(tied))}); "
                f"reported UNKNOWN rather than forcing a confident classification"
            ),
            evidence_ids=tuple(
                sorted({eid for job, _ in ranked if _ == top_score for eid in contributing[job]})
            ),
            signal_types=tuple(
                sorted({s for job, _ in ranked if _ == top_score for s in signals[job]})
            ),
        )

    # A job derived from observed provider text is INFERRED — a derivation
    # from evidence, never an observation of the buyer's actual job. Without
    # observed support it rests on a Milestone 1 hypothesis, so ASSUMED.
    # Only evidence that matched THIS job supports it. Evidence backing a
    # different job neither raises this job's claim class nor is cited for it.
    own_evidence = contributing[top_job]
    if observed_matches[top_job] > 0:
        claim = SpecClaimClass.INFERRED
        basis = (
            f"derived from {len(own_evidence)} observed evidence record(s) carrying "
            f"{observed_matches[top_job]} {top_job.value.lower()} token match(es); "
            f"candidate text contributed a further "
            f"{top_score - observed_matches[top_job]}"
        )
    else:
        # The winning job rests entirely on the Milestone 1 hypothesis, even
        # if unrelated evidence happened to match some other job.
        claim = SpecClaimClass.ASSUMED
        basis = (
            "no observed evidence text matched this job; classified from the "
            "Milestone 1 candidate hypothesis alone, which is not evidence"
        )

    return JobClassification(
        job=top_job,
        claim_class=claim,
        matched_tokens=tuple(sorted(set(matched[top_job]))),
        scores=score_tuple,
        basis=basis,
        evidence_ids=tuple(sorted(own_evidence)),
        signal_types=tuple(sorted(signals[top_job])),
    )


# ------------------------------------------------------- format selection


def observed_dominant_format(evidence: list[EvidenceItem]) -> tuple[str | None, int]:
    """The listing_type the observed market ships most, and its count.

    Reported for contrast only. Divergence from it is not a defect: the job,
    not the incumbent format, drives selection.
    """
    counts: dict[str, int] = {}
    seen: set[str] = set()
    for item in evidence:
        if item.signal_type != SIGNAL_COMPETING_LISTING:
            continue
        if item.truth_class != TruthClass.OBSERVED:
            continue
        payload = item.raw_payload or {}
        listing_id = str(payload.get("listing_id") or item.id)
        if listing_id in seen:
            continue
        seen.add(listing_id)
        listing_type = payload.get("listing_type")
        if listing_type:
            counts[str(listing_type)] = counts.get(str(listing_type), 0) + 1
    if not counts:
        return None, 0
    dominant = max(counts.items(), key=lambda kv: (kv[1], kv[0]))
    return dominant[0], dominant[1]


def recommend_format(
    job: JobClassification,
    candidate: Candidate,
    evidence: list[EvidenceItem],
) -> FormatRecommendation:
    """format_selection_v1: choose by job-to-be-done, not by incumbent format.

    Returns two separate answers. `ideal_format` is the best conceptual fit
    and is ALWAYS ASSUMED — it is a design judgment, never a market finding.
    `buildable_v1_format` is the nearest of Milestone 1's seven approved
    formats, which is what this system can actually build.
    """
    dominant, dominant_count = observed_dominant_format(evidence)

    if job.job == JobToBeDone.UNKNOWN:
        # Without a job, no format claim is defensible. Fall back to the
        # Milestone 1 proposed format, clearly marked as the hypothesis it is.
        return FormatRecommendation(
            ideal_format=None,
            ideal_claim_class=SpecClaimClass.UNKNOWN,
            outside_v1_build_capability=False,
            buildable_v1_format=candidate.proposed_format,
            buildable_claim_class=SpecClaimClass.ASSUMED,
            rationale=(
                "The job-to-be-done could not be classified from available evidence, "
                "so no format is recommended on job grounds. The Milestone 1 proposed "
                "format is carried forward as an unvalidated hypothesis."
            ),
            driven_by_job=JobToBeDone.UNKNOWN,
            observed_dominant_format=dominant,
            diverges_from_observed_market=False,
        )

    ideal = JOB_TO_IDEAL_FORMAT[job.job]
    outside = ideal not in V1_BUILDABLE_IDEAL_FORMATS
    buildable = IDEAL_TO_BUILDABLE[ideal]

    # Divergence is measured against what the market ships, purely to make
    # the disagreement inspectable.
    diverges = bool(dominant) and outside

    rationale = (
        f"The buyer's job was classified as {job.job.value}, so the ideal format is "
        f"{ideal.value}: it fits the job directly."
    )
    if outside:
        rationale += (
            f" {ideal.value} is OUTSIDE current V1 build capability — Milestone 1 does "
            f"not accept candidates of this kind and this system cannot build one. It "
            f"is a conceptual suggestion, not a market-validated format. The nearest "
            f"buildable V1 format is {buildable.value}."
        )
    else:
        rationale += f" It is buildable in V1 as {buildable.value}."
    if dominant:
        rationale += (
            f" The observed market ships '{dominant}' most often ({dominant_count} "
            f"listing record(s)); format was chosen on the job, not by copying that."
        )

    return FormatRecommendation(
        ideal_format=ideal,
        # An ideal format is always a design judgment, never a finding.
        ideal_claim_class=SpecClaimClass.ASSUMED,
        outside_v1_build_capability=outside,
        buildable_v1_format=buildable,
        # Capped by the job's own class: a buildable format derived from an
        # ASSUMED job cannot be INFERRED.
        buildable_claim_class=cap_claim_class(job.claim_class, SpecClaimClass.INFERRED),
        rationale=rationale,
        driven_by_job=job.job,
        observed_dominant_format=dominant,
        diverges_from_observed_market=diverges,
    )


# --------------------------------------------------------- price reference


def build_price_reference(price: PriceEvidenceResult | None) -> PriceEvidenceReference:
    """Cite 4B's observed asking-price evidence. Never recommend a price."""
    if price is None or price.features is None:
        return PriceEvidenceReference(
            available=False,
            state=price.state.value if price else None,
            limitations=(
                "No price evidence was available for this candidate.",
            ),
        )
    bands = tuple(
        (
            band.currency,
            band.paid_listing_count,
            band.min_paid_asking_price,
            band.median_asking_price,
            band.max_paid_asking_price,
            band.insufficient_evidence,
        )
        for band in price.features.bands
    )
    return PriceEvidenceReference(
        available=bool(bands),
        state=price.state.value,
        currencies=price.features.currencies_observed,
        observed_asking_bands=bands,
        free_listing_count=price.features.free_listing_count,
        price_evidence_version=price.version,
        limitations=price.limitations,
    )


# ------------------------------------------------------- competitor patterns


def observed_competitor_patterns(evidence: list[EvidenceItem]) -> tuple[SpecField, ...]:
    """Listing titles and shipped formats actually returned by the provider.

    These are OBSERVED: the provider returned them. What is INSIDE those
    products is not collected and stays UNKNOWN.
    """
    titles: dict[str, UUID] = {}
    for item in evidence:
        if item.signal_type != SIGNAL_COMPETING_LISTING:
            continue
        if item.truth_class != TruthClass.OBSERVED:
            continue
        title = (item.raw_payload or {}).get("title")
        if title:
            seen_id = titles.get(str(title))
            if seen_id is None or item.id < seen_id:
                titles[str(title)] = item.id

    patterns = [
        SpecField(
            value=f"Observed competing listing title: {title}",
            claim_class=SpecClaimClass.OBSERVED,
            basis="listing title returned by the marketplace provider",
            evidence_ids=(evidence_id,),
            signal_types=(SIGNAL_COMPETING_LISTING,),
        )
        for title, evidence_id in sorted(titles.items())
    ]
    dominant, count = observed_dominant_format(evidence)
    if dominant:
        patterns.append(
            SpecField(
                value=f"Most common observed listing type: {dominant} ({count} record(s))",
                claim_class=SpecClaimClass.OBSERVED,
                basis="listing_type returned by the marketplace provider",
                evidence_ids=(),
                signal_types=(SIGNAL_COMPETING_LISTING,),
            )
        )
    return tuple(patterns)


# ------------------------------------------------- forbidden market claims

# Phrases that assert a market finding this milestone cannot support. They
# gate BOTH candidate-derived prose and any future provider prose: the
# deterministic generator is not exempt from its own rule.
FORBIDDEN_PROSE_PATTERNS = (
    r"\bproven\b",
    r"\bguarantee[ds]?\b",
    r"\bwill sell\b",
    r"\bbest[- ]sell(er|ing)\b",
    r"\b\d+\s*%\s*(conversion|of buyers|success)",
    r"\brevenue\b",
    r"\bunits sold\b",
    r"\bwillingness to pay\b",
    r"\bmarket share\b",
    r"\bvalidated by the market\b",
)

_FORBIDDEN_RE = tuple(re.compile(p, re.IGNORECASE) for p in FORBIDDEN_PROSE_PATTERNS)

# Characters that render as nothing but break a word-boundary match, letting
# "pro<zero-width-space>ven" slip past \bproven\b.
_INVISIBLE_RE = re.compile(r"[\u00ad\u200b-\u200f\u2028\u2029\ufeff]")


def _normalize_for_claim_scan(text: str) -> str:
    """Fold a string to its plainest form before pattern matching.

    Compatibility normalization (NFKC) collapses fullwidth and styled
    look-alikes, invisible characters are dropped, and every run of Unicode
    whitespace (non-breaking space included) becomes one plain space. Without
    this, a homoglyph or a zero-width character defeats every pattern above.
    """
    folded = unicodedata.normalize("NFKC", text)
    folded = _INVISIBLE_RE.sub("", folded)
    # Confusable Cyrillic/Greek look-alikes NFKC does not fold.
    folded = folded.translate(_CONFUSABLES)
    return " ".join(folded.split())


# Latin look-alikes for letters used by the forbidden patterns. NFKC treats
# these as distinct letters, so they are mapped explicitly.
_CONFUSABLES = str.maketrans(
    {
        "\u0430": "a", "\u0435": "e", "\u043e": "o", "\u0440": "p",
        "\u0441": "c", "\u0445": "x", "\u0443": "y", "\u0456": "i",
        "\u04bb": "h", "\u0455": "s", "\u0461": "w", "\u03bf": "o",
        "\u03b1": "a", "\u03c1": "p", "\u0501": "d", "\u0261": "g",
    }
)


# ---------------------------------------------------------- the generator


def unsupported_claim_in(text: str) -> str | None:
    """Return the forbidden-claim pattern a text asserts, or None.

    Applied to candidate-derived prose as well as provider prose. Milestone 1
    text is a generation hypothesis that may contain marketing language; this
    milestone must not restate "proven", "best-selling", or a revenue figure
    as though evidence supported it.
    """
    for pattern in _FORBIDDEN_RE:
        if pattern.search(_normalize_for_claim_scan(text)):
            return pattern.pattern
    return None


def _product_name(candidate: Candidate, recommendation: FormatRecommendation) -> SpecField:
    """A working product name. Always a design hypothesis."""
    if recommendation.buildable_v1_format is None:
        return SpecField(
            value=None,
            claim_class=SpecClaimClass.UNKNOWN,
            basis="no buildable format was determined, so no product name is proposed",
        )
    suffix = recommendation.buildable_v1_format.value.replace("_", " ").title()
    offending = unsupported_claim_in(candidate.title)
    if offending:
        # The candidate title asserts a market claim no evidence supports.
        # Restating it would launder a hypothesis into the product name.
        return SpecField(
            value=None,
            claim_class=SpecClaimClass.UNKNOWN,
            basis=(
                f"the Milestone 1 candidate title asserts an unsupported market "
                f"claim (matching {offending!r}), so it is not restated as a "
                f"product name; no name is proposed"
            ),
        )
    return SpecField(
        value=f"{candidate.title} — {suffix}",
        claim_class=SpecClaimClass.ASSUMED,
        basis=(
            "working title composed from the Milestone 1 candidate title and the "
            "buildable format; a naming hypothesis, not a tested product name"
        ),
    )


def _core_promise(candidate: Candidate, job: JobClassification) -> SpecField:
    """The outcome the product aims at. Carefully worded, no guarantee."""
    outcome = candidate.buyer_outcome.strip().rstrip(".")
    if not outcome:
        return SpecField(
            value=None,
            claim_class=SpecClaimClass.UNKNOWN,
            basis="the candidate carried no buyer outcome to restate",
        )
    offending = unsupported_claim_in(outcome)
    if offending:
        return SpecField(
            value=None,
            claim_class=SpecClaimClass.UNKNOWN,
            basis=(
                f"the Milestone 1 buyer outcome asserts an unsupported market claim "
                f"(matching {offending!r}); a trailing disclaimer would not undo it, "
                f"so no promise is stated"
            ),
        )
    return SpecField(
        value=(
            f"Aims to help the buyer {outcome.lower()}. This is a design intention, "
            f"not a guarantee of results."
        ),
        claim_class=SpecClaimClass.ASSUMED,
        basis=(
            "restated from the Milestone 1 candidate's buyer outcome, which is a "
            "hypothesis; no buyer research exists to observe the outcome"
        ),
    )


def _differentiation(
    recommendation: FormatRecommendation, patterns: tuple[SpecField, ...]
) -> tuple[SpecField, ...]:
    """Design hypotheses about how to differ. Never observed gaps.

    Competitor product CONTENTS are not collected, so a genuine gap cannot be
    observed. Everything here is explicitly a hypothesis to validate.
    """
    ideas: list[SpecField] = []
    if recommendation.diverges_from_observed_market and recommendation.ideal_format:
        ideas.append(
            SpecField(
                value=(
                    f"The job suggests a {recommendation.ideal_format.value} while the "
                    f"observed market ships '{recommendation.observed_dominant_format}'. "
                    f"Serving the job more directly may differentiate."
                ),
                claim_class=SpecClaimClass.ASSUMED,
                basis=(
                    "hypothesis from the format/market divergence; competitor product "
                    "contents were not collected, so no gap was observed"
                ),
            )
        )
    if patterns:
        ideas.append(
            SpecField(
                value=(
                    "Observed competitor titles are available for positioning contrast; "
                    "what those products actually contain was not collected."
                ),
                claim_class=SpecClaimClass.ASSUMED,
                basis="hypothesis; competitor contents remain UNKNOWN",
            )
        )
    if not ideas:
        ideas.append(
            SpecField(
                value=None,
                claim_class=SpecClaimClass.UNKNOWN,
                basis="no competitor evidence was collected, so no contrast can be drawn",
            )
        )
    return tuple(ideas)


def _detect_conflicts(
    job: JobClassification, recommendation: FormatRecommendation, candidate: Candidate
) -> tuple[EvidenceConflict, ...]:
    """Surface disagreements rather than hiding them."""
    conflicts: list[EvidenceConflict] = []

    if recommendation.diverges_from_observed_market:
        conflicts.append(
            EvidenceConflict(
                topic="format_vs_observed_market",
                description=(
                    f"The classified job ({job.job.value}) points to "
                    f"{recommendation.ideal_format.value if recommendation.ideal_format else 'no format'}, "
                    f"but the observed market ships "
                    f"'{recommendation.observed_dominant_format}' most often."
                ),
                resolution=(
                    "Format was selected on the job-to-be-done, per format_selection_v1. "
                    "The market observation is reported, not followed."
                ),
            )
        )

    if (
        recommendation.buildable_v1_format is not None
        and recommendation.buildable_v1_format != candidate.proposed_format
        and job.job != JobToBeDone.UNKNOWN
    ):
        conflicts.append(
            EvidenceConflict(
                topic="format_vs_candidate_hypothesis",
                description=(
                    f"Milestone 1 proposed {candidate.proposed_format.value}; the job "
                    f"({job.job.value}) points to {recommendation.buildable_v1_format.value}."
                ),
                resolution=(
                    "The job-derived format is recommended. The Milestone 1 proposal was "
                    "a generation hypothesis, not evidence."
                ),
            )
        )

    if job.job == JobToBeDone.UNKNOWN and job.scores:
        conflicts.append(
            EvidenceConflict(
                topic="job_signals_inconclusive",
                description=f"Job token scores did not separate cleanly: {job.scores}.",
                resolution=job.basis,
            )
        )
    return tuple(conflicts)


def generate_product_specification(
    candidate: Candidate,
    evidence: list[EvidenceItem],
    price_evidence: PriceEvidenceResult | None = None,
    purchase_evidence: PurchaseEvidenceResult | None = None,
    research_run_id: UUID | None = None,
) -> ProductSpecification:
    """Generate a product specification for ONE selected candidate.

    Deterministic and pure: identical inputs yield identical output. No
    provider calls, no LLM, no fabricated demand, and no numeric findings.
    """
    foreign = [
        item.id
        for item in evidence
        if item.candidate_id is not None and item.candidate_id != candidate.id
    ]
    if foreign:
        raise EvidenceOwnershipError(
            f"{len(foreign)} evidence record(s) belong to another candidate; a "
            f"specification may only cite evidence collected for candidate "
            f"{candidate.id}"
        )

    observed = collect_observed_text(evidence)
    job = classify_job(observed, candidate)
    recommendation = recommend_format(job, candidate, evidence)
    patterns = observed_competitor_patterns(evidence)
    price_reference = build_price_reference(price_evidence)
    conflicts = _detect_conflicts(job, recommendation, candidate)

    # Sorted, so an equivalent evidence set always yields the same document
    # regardless of the order the records were stored or supplied in.
    supporting = tuple(
        sorted(
            {entry.evidence_id for entry in observed}
            | {eid for pattern in patterns for eid in pattern.evidence_ids}
        )
    )

    if not evidence:
        # No evidence at all: refuse to fabricate a specification.
        return ProductSpecification(
            candidate_id=candidate.id,
            research_run_id=research_run_id,
            state=SpecificationState.MISSING,
            product_name=SpecField(None, SpecClaimClass.UNKNOWN, "no evidence collected"),
            target_buyer=SpecField(None, SpecClaimClass.UNKNOWN, "no evidence collected"),
            buyer_job=SpecField(None, SpecClaimClass.UNKNOWN, "no evidence collected"),
            job_classification=job,
            format_recommendation=None,
            core_promise=SpecField(None, SpecClaimClass.UNKNOWN, "no evidence collected"),
            structure=(),
            required_assets=(),
            differentiation_opportunities=(),
            observed_competitor_patterns=(),
            price_evidence_reference=price_reference,
            supporting_evidence_ids=(),
            assumptions=(),
            unknowns=(
                "No evidence was collected for this candidate, so nothing about the "
                "product could be responsibly specified.",
            ),
            conflicts=(),
            insufficient_evidence=True,
            missing_reason="no_evidence_collected_for_candidate",
        )

    buildable = recommendation.buildable_v1_format
    structure = tuple(
        ProductModule(name=name, purpose=purpose, claim_class=SpecClaimClass.ASSUMED)
        for name, purpose in FORMAT_STRUCTURE.get(buildable, ())
    ) if buildable else ()
    assets = FORMAT_ASSETS.get(buildable, ()) if buildable else ()

    unknowns = [
        "Competitor product contents were not collected and remain UNKNOWN.",
        "Buyer willingness to pay was not measured and remains UNKNOWN.",
        "Actual transaction prices are not public and remain UNKNOWN.",
        "Whether this product will sell is UNKNOWN and is not predicted here.",
    ]
    if not price_reference.available:
        unknowns.append("No observed asking-price band was available for this candidate.")
    if job.job == JobToBeDone.UNKNOWN:
        unknowns.append("The buyer's job-to-be-done could not be classified from evidence.")
    if purchase_evidence is not None:
        # Cited, never re-derived. A 4A pattern describes the STRUCTURE of the
        # observed listing set; it is not a statement that the product will sell.
        unknowns.append(
            f"Milestone 4A observed the market-structure pattern "
            f"{purchase_evidence.pattern.value} from review-count proxies. "
            f"Actual competitor units sold and revenue remain UNKNOWN, and this "
            f"pattern is not a prediction that this product will sell."
        )

    assumptions = [
        "The target buyer and buyer problem come from a Milestone 1 hypothesis, not "
        "from buyer research.",
        f"The job taxonomy ({JOB_TAXONOMY_VERSION}) and its keyword mappings are an "
        f"unvalidated V1 heuristic.",
        f"Format selection ({FORMAT_SELECTION_VERSION}) is a documented heuristic, not "
        f"a validated model.",
        "Module structure and asset lists are unvalidated V1 templates.",
    ]
    if recommendation.outside_v1_build_capability and recommendation.ideal_format:
        assumptions.append(
            f"The ideal format {recommendation.ideal_format.value} is a conceptual "
            f"suggestion OUTSIDE current V1 build capability. It was inferred from the "
            f"job, not validated by the market."
        )

    # Sparse evidence: a specification is still produced, but the state of the
    # evidence is reported rather than papered over.
    insufficient = (
        job.job == JobToBeDone.UNKNOWN
        or not patterns
        or not price_reference.available
    )

    return ProductSpecification(
        candidate_id=candidate.id,
        research_run_id=research_run_id,
        state=SpecificationState.GENERATED,
        product_name=_product_name(candidate, recommendation),
        target_buyer=SpecField(
            value=candidate.target_buyer,
            claim_class=SpecClaimClass.ASSUMED,
            basis=(
                "carried from the Milestone 1 candidate hypothesis; no qualitative "
                "buyer research exists in this system"
            ),
        ),
        buyer_job=SpecField(
            value=job.job.value if job.job != JobToBeDone.UNKNOWN else None,
            claim_class=job.claim_class,
            basis=job.basis,
            evidence_ids=job.evidence_ids,
            signal_types=job.signal_types,
        ),
        job_classification=job,
        format_recommendation=recommendation,
        core_promise=_core_promise(candidate, job),
        structure=structure,
        required_assets=assets,
        differentiation_opportunities=_differentiation(recommendation, patterns),
        observed_competitor_patterns=patterns,
        price_evidence_reference=price_reference,
        supporting_evidence_ids=supporting,
        assumptions=tuple(assumptions),
        unknowns=tuple(unknowns),
        conflicts=conflicts,
        insufficient_evidence=insufficient,
    )


# ------------------------------------------------- provider abstraction (prose only)


class ProseGenerationError(Exception):
    """Raised when proposed prose violates the 4C claim rules."""


# The only fields a prose provider may rewrite. Everything else — claim
# classes, evidence ids, formats, structure, numbers — is out of reach.
REWRITABLE_PROSE_FIELDS = ("product_name", "core_promise")


def validate_prose(text: str) -> None:
    """Reject prose that asserts a finding the evidence does not support.

    Anything that is not a plain non-empty string is rejected as malformed:
    a provider returning a number, a mapping, or None is a provider fault,
    never a value to substitute into the specification.
    """
    if not isinstance(text, str):
        raise ProseGenerationError(
            f"proposed prose must be a string, got {type(text).__name__}"
        )
    if not text.strip():
        raise ProseGenerationError("proposed prose is empty; nothing to substitute")
    offending = unsupported_claim_in(text)
    if offending:
        raise ProseGenerationError(
            f"proposed prose contains a forbidden claim matching {offending!r}"
        )


class ProductSpecificationProvider:
    """Optional prose refinement for an already-decided specification.

    A provider may ONLY reword the narrative fields named in
    `REWRITABLE_PROSE_FIELDS`. It cannot select or change a claim class,
    create or alter evidence, produce any number, change the format
    decision, or convert an assumption into a finding. The deterministic
    generator has already made every decision before a provider is consulted.

    No live-LLM implementation ships in 4C.
    """

    name: str = "unknown"

    def propose_prose(self, specification: ProductSpecification) -> dict[str, str]:
        """Return {field_name: replacement_text} for rewritable fields only."""
        raise NotImplementedError


class TemplateProseProvider(ProductSpecificationProvider):
    """The V1 default: returns nothing, leaving deterministic prose in place."""

    name = "template"

    def propose_prose(self, specification: ProductSpecification) -> dict[str, str]:
        return {}


def apply_prose(
    specification: ProductSpecification, proposed: dict[str, str]
) -> ProductSpecification:
    """Apply provider prose to narrative fields only.

    Claim class, basis, evidence ids and signal types are preserved exactly:
    rewording a sentence cannot change how well evidence supports it. Any
    field outside `REWRITABLE_PROSE_FIELDS` is rejected, and every proposed
    string is validated against the forbidden-claim patterns.
    """
    if not isinstance(proposed, dict):
        raise ProseGenerationError(
            f"provider output must be a mapping of field to text, got "
            f"{type(proposed).__name__}"
        )
    if not proposed:
        return specification

    updates: dict[str, SpecField] = {}
    for name, text in proposed.items():
        if name not in REWRITABLE_PROSE_FIELDS:
            raise ProseGenerationError(
                f"field {name!r} is not rewritable by a prose provider"
            )
        validate_prose(text)
        existing: SpecField = getattr(specification, name)
        # Only `value` changes. The claim class and provenance are untouched.
        updates[name] = replace(existing, value=text)
    return replace(specification, **updates)
