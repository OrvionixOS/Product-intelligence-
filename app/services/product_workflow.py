"""Milestone 7A — the end-to-end product workflow.

Every piece of this pipeline already existed and none of it was reachable.
`deep_collection`, `deep_research`, `evidence_confidence`, `pos_dimensions`,
`opportunity_scoring` and the whole 5A/5B/5C content chain were imported by no
route, so a user could discover candidates and jump straight to a product
specification without a score ever being computed. This module wires the
existing services together in the canonical order and gates the post-score
stages on a score actually existing. It introduces no derivation, no
provider, no score and no threshold of its own.

The canonical order, and why each gate is where it is:

    1-3  discovery and the cheap research pass   (existing)
      7  deep collection, then the deep dossier  (6B, 6C)
      8  Evidence Confidence, sub-scores, score  (6A-1, 6D, 6E)
      9  classification, when ECS permits        (6G)
     10  product generation, then job fit        (4C, 4G)
      -  content intelligence                    (5A, 5B, 5C)

**Scoring happens before product generation.** ARCHITECTURE.md orders step 8
before step 10, and SPEC_STEP_7_8.md §2 rejects problem/product fit as a Step 7
dimension precisely because at step 7 the product does not exist yet. Before
this module, `/product/specification` could run on any candidate in a run; now
4C runs only on a candidate that carries a persisted score.

**A blocked score is a gate, not a low score.** A candidate whose evidence was
too thin to score is not a bad candidate; it is an unmeasured one. It does not
proceed to product generation, and the reason travels with the refusal.

**Contextual evidence is surfaced beside the score, never inside it.** Price
evidence, competition structure and channel reach are carried into the output
so a user can read them, and every one of them is marked contextual. §5 keeps
them out of any magnitude, and nothing here changes that.

**Preliminary triage never crosses into Step 8.** The workflow takes candidates
and a run id. It never reads a preliminary rank or a preliminary 0-100
dimension, and the dossier it scores excludes rank by construction (§11).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from uuid import UUID, uuid4

from app.domain.models import Candidate, EvidenceItem
from app.services.content_experiments import (
    ContentExperimentsResult,
    derive_content_experiments,
)
from app.services.content_patterns import ContentPatternsResult, derive_content_patterns
from app.services.deep_collection import (
    DEEP_PASS_CAPS_VERSION,
    DeepCollectionResult,
    run_deep_collection,
)
from app.services.deep_research import (
    DEEP_RESEARCH_VERSION,
    CONTEXTUAL_ONLY,
    DeepDimension,
    DeepResearchResult,
    DeepResearchState,
    build_deep_research_result,
)
from app.services.evidence_confidence import EcsDimension
from app.services.faceless_content_intelligence import (
    RobustContentIntelligenceResult,
    derive_robust_content_intelligence,
)
from app.services.opportunity_scoring import (
    CLASSIFICATION_THRESHOLDS_VERSION,
    OPPORTUNITY_SCORE_VERSION,
    ScoringResult,
    ScoringState,
    score_candidate,
)
from app.services.research_orchestration import CapabilityCaps
from app.storage.memory import ResearchStore

WORKFLOW_VERSION = "product_workflow_v1"

# The dimensions surfaced as context beside a score. Exactly §5's
# contextual-only set: this module selects from it, it does not decide it.
CONTEXT_DIMENSIONS: tuple[EcsDimension, ...] = (
    EcsDimension.PRICE_EVIDENCE,
    EcsDimension.COMPETITION_STRUCTURE,
    EcsDimension.CHANNEL_REACH,
)

GATE_NOT_SCORED = "candidate_has_no_scoring_record"
GATE_INSUFFICIENT_EVIDENCE = "candidate_could_not_be_scored"
GATE_SCOPE_MISMATCH = "candidate_is_not_part_of_this_research_run"

LIMITATIONS: tuple[str, ...] = (
    "This workflow composes existing derivations. It computes no score, no "
    "threshold and no dimension of its own, and every number it carries was "
    "produced by a named, versioned formula upstream.",
    "Contextual evidence is surfaced beside a score, never inside one. "
    "Observed asking prices are asking prices: they are never willingness to "
    "pay, transaction prices, revenue, or a recommended price. Channel reach "
    "is where a seller could observably appear, never an audience size or a "
    "reachable buyer count.",
    "A candidate that could not be scored is unmeasured, not bad. It does not "
    "proceed to product generation, and that refusal is never a judgement "
    "about the opportunity.",
    "Content experiments are experiments. They are hypotheses to test, never "
    "predictions that a piece of content will perform.",
)


class WorkflowStage(str, Enum):
    """Where a candidate has reached. Ordered, and never skipped."""

    DEEP_RESEARCH = "DEEP_RESEARCH"
    SCORED = "SCORED"
    PRODUCT_GENERATED = "PRODUCT_GENERATED"
    CONTENT_PLANNED = "CONTENT_PLANNED"


@dataclass(slots=True, frozen=True)
class ContextualEvidence:
    """One contextual dimension, surfaced for reading and never for scoring."""

    dimension_name: str
    state: str
    missing_reason: str | None
    observed_features: dict
    contextual_only: bool
    formula_version: str

    @classmethod
    def from_dimension(cls, dimension: DeepDimension) -> "ContextualEvidence":
        return cls(
            dimension_name=dimension.dimension_name,
            state=dimension.state.value,
            missing_reason=dimension.missing_reason,
            observed_features=dict(dimension.observed_features),
            contextual_only=dimension.contextual_only,
            formula_version=dimension.formula_version,
        )


@dataclass(slots=True, frozen=True)
class CandidateWorkflowResult:
    """One candidate's position in the flow, with everything it has reached."""

    candidate_id: UUID
    research_run_id: UUID
    deep_pass_id: UUID
    stage: WorkflowStage
    dossier: DeepResearchResult
    scoring: ScoringResult
    # §5's contextual-only dimensions, carried for reading.
    context: tuple[ContextualEvidence, ...]
    version: str = WORKFLOW_VERSION
    limitations: tuple[str, ...] = field(default=LIMITATIONS)

    @property
    def may_generate_product(self) -> bool:
        """4C runs only on a candidate that carries an actual score."""
        return self.scoring.is_scored


@dataclass(slots=True, frozen=True)
class DeepResearchRunResult:
    """One deep pass plus the scored dossier of every selected candidate."""

    research_run_id: UUID
    deep_pass_id: UUID
    collection: DeepCollectionResult
    candidates: tuple[CandidateWorkflowResult, ...]
    component_versions: dict[str, str]
    version: str = WORKFLOW_VERSION

    @property
    def scored_candidate_ids(self) -> tuple[UUID, ...]:
        return tuple(
            c.candidate_id for c in self.candidates if c.scoring.is_scored
        )


class WorkflowGateError(RuntimeError):
    """A stage was requested for a candidate that has not reached it.

    Carries the reason so a caller can say WHY rather than only that it
    refused. Never raised for a low score — only for an absent one.
    """

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


@dataclass(slots=True, frozen=True)
class ContentPlan:
    """5A → 5B → 5C for one scored candidate."""

    candidate_id: UUID
    research_run_id: UUID
    outliers: RobustContentIntelligenceResult
    patterns: ContentPatternsResult
    experiments: ContentExperimentsResult


def _context_for(dossier: DeepResearchResult) -> tuple[ContextualEvidence, ...]:
    carried: list[ContextualEvidence] = []
    for dimension in CONTEXT_DIMENSIONS:
        found = dossier.dimensions.get(dimension.value)
        if found is None:
            continue
        # Guard rather than trust: anything surfaced here must be a dimension
        # §5 marked contextual, so a POS-eligible dimension can never be
        # rendered as context and read as though it were not in the score.
        if dimension not in CONTEXTUAL_ONLY or not found.contextual_only:
            raise ValueError(
                f"{dimension.value} is not contextual-only and must not be "
                "surfaced as context"
            )
        carried.append(ContextualEvidence.from_dimension(found))
    return tuple(carried)


async def run_deep_research_and_score(
    candidates: list[Candidate],
    store: ResearchStore,
    research_run_id: UUID,
    search_demand_provider=None,
    marketplace_provider=None,
    public_content_provider=None,
    location: str = "US",
    language: str = "en",
    caps: CapabilityCaps | None = None,
    unavailable_capabilities: dict[str, str] | None = None,
    deep_pass_id: UUID | None = None,
    now: datetime | None = None,
) -> DeepResearchRunResult:
    """Steps 7 to 9 for the selected candidates of one research run.

    Deep collection once for the whole selection, then per candidate: the
    dossier over the union of cheap-pass and deep-pass evidence, Evidence
    Confidence, the three sub-scores, the candidate score, and a colour when
    confidence permits. Every scoring record is persisted, including the ones
    that could not be scored.

    `research_run_id` is required: a deep pass extends an existing run and
    never invents a scope of its own.
    """
    pass_id = deep_pass_id or uuid4()

    collection = await run_deep_collection(
        candidates=candidates,
        store=store,
        research_run_id=research_run_id,
        search_demand_provider=search_demand_provider,
        marketplace_provider=marketplace_provider,
        public_content_provider=public_content_provider,
        location=location,
        language=language,
        caps=caps,
        unavailable_capabilities=unavailable_capabilities,
    )

    results: list[CandidateWorkflowResult] = []
    for candidate in candidates:
        # Scoped read: the store is append-only across runs, so an unscoped
        # read would mix runs and double-count anything observed in both.
        evidence = store.evidence_for_candidate(candidate.id, research_run_id)
        dossier = build_deep_research_result(
            candidate_id=candidate.id,
            research_run_id=research_run_id,
            deep_pass_id=pass_id,
            evidence=evidence,
            capability_outcomes=collection.capabilities,
            now=now,
        )
        scoring = score_candidate(dossier)
        store.add_scoring_result(scoring)
        results.append(
            CandidateWorkflowResult(
                candidate_id=candidate.id,
                research_run_id=research_run_id,
                deep_pass_id=pass_id,
                stage=(
                    WorkflowStage.SCORED
                    if scoring.is_scored
                    else WorkflowStage.DEEP_RESEARCH
                ),
                dossier=dossier,
                scoring=scoring,
                context=_context_for(dossier),
            )
        )

    return DeepResearchRunResult(
        research_run_id=research_run_id,
        deep_pass_id=pass_id,
        collection=collection,
        candidates=tuple(results),
        component_versions={
            "workflow": WORKFLOW_VERSION,
            "deep_pass_caps": DEEP_PASS_CAPS_VERSION,
            "deep_research": DEEP_RESEARCH_VERSION,
            "opportunity_score": OPPORTUNITY_SCORE_VERSION,
            "classification_thresholds": CLASSIFICATION_THRESHOLDS_VERSION,
        },
    )


def require_scored_candidate(
    store: ResearchStore, candidate_id: UUID, research_run_id: UUID
) -> ScoringResult:
    """The gate every post-score stage passes through.

    Raises rather than returning a sentinel, because a caller that forgets to
    check would otherwise generate a product for an unscored candidate, which
    is exactly the ordering this module exists to enforce.
    """
    scoring = store.latest_scoring_result(candidate_id, research_run_id)
    if scoring is None:
        raise WorkflowGateError(
            GATE_NOT_SCORED,
            f"candidate {candidate_id} has no scoring record for research run "
            f"{research_run_id}; run deep research and scoring first",
        )
    if scoring.scoring_state is ScoringState.NOT_SCORED:
        raise WorkflowGateError(
            GATE_NOT_SCORED,
            f"candidate {candidate_id} has not been scored for research run "
            f"{research_run_id}",
        )
    if not scoring.is_scored:
        # INSUFFICIENT_EVIDENCE. Not a low score: no score exists at all, and
        # the excluded dimensions say which evidence was missing.
        missing = ", ".join(name for name, _ in scoring.excluded_dimensions)
        raise WorkflowGateError(
            GATE_INSUFFICIENT_EVIDENCE,
            f"candidate {candidate_id} could not be scored: "
            f"{missing or 'required evidence unavailable'}. This is missing "
            "evidence, not a low score.",
        )
    return scoring


def derive_content_plan(
    store: ResearchStore,
    candidate_id: UUID,
    research_run_id: UUID,
    evidence: list[EvidenceItem] | None = None,
) -> ContentPlan:
    """5A → 5B → 5C for one scored candidate.

    Post-score by the same rule 4C follows. Each stage keeps its own existing
    boundaries: outliers are creator-relative, patterns are co-occurrence
    observations, and experiments are hypotheses to test.
    """
    require_scored_candidate(store, candidate_id, research_run_id)
    records = (
        evidence
        if evidence is not None
        else store.evidence_for_candidate(candidate_id, research_run_id)
    )
    outliers = derive_robust_content_intelligence(
        candidate_id=candidate_id,
        evidence=records,
        research_run_id=research_run_id,
    )
    patterns = derive_content_patterns(
        candidate_id=candidate_id,
        evidence=records,
        research_run_id=research_run_id,
        outlier_evidence=outliers,
    )
    experiments = derive_content_experiments(
        candidate_id=candidate_id,
        patterns=patterns,
        research_run_id=research_run_id,
    )
    return ContentPlan(
        candidate_id=candidate_id,
        research_run_id=research_run_id,
        outliers=outliers,
        patterns=patterns,
        experiments=experiments,
    )


def workflow_state(
    store: ResearchStore, candidate_id: UUID, research_run_id: UUID
) -> tuple[WorkflowStage | None, ScoringResult | None]:
    """How far this candidate has got, without running anything.

    A stage of None means deep research has not run. It is the absence of an
    attempt and says nothing about the candidate.
    """
    scoring = store.latest_scoring_result(candidate_id, research_run_id)
    if scoring is None or scoring.scoring_state is ScoringState.NOT_SCORED:
        return None, scoring
    return (
        WorkflowStage.SCORED if scoring.is_scored else WorkflowStage.DEEP_RESEARCH
    ), scoring


def unscoreable_reason(result: CandidateWorkflowResult) -> str | None:
    """Why this candidate carries no score, or None when it does.

    Reads the dossier rather than guessing: a SCOPE_MISMATCH dossier and a
    thin-evidence one are different failures and must not read alike.
    """
    if result.scoring.is_scored:
        return None
    if result.dossier.state is DeepResearchState.SCOPE_MISMATCH:
        return GATE_SCOPE_MISMATCH
    return GATE_INSUFFICIENT_EVIDENCE
