from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, model_validator

from app.domain.models import (
    Candidate,
    EvidenceItem,
    EvidenceSnapshot,
    ScoreDimensions,
    ScoreResult,
)
from app.providers.base import MissingCredentialsError, SearchDemandProvider
from app.providers.dataforseo import DataForSeoSearchDemandProvider
from app.providers.llm import (
    CandidateGenerationError,
    CandidateGenerationProvider,
    TemplateCandidateProvider,
)
from app.services.candidate_discovery import (
    DEFAULT_TARGET_COUNT,
    InvalidSeedKeywordError,
    discover_candidates,
)
from app.services.scoring import score_opportunity
from app.services.search_demand import run_search_demand_research
from app.services.search_demand_features import SearchDemandSummary
from app.storage.memory import ResearchStore

router = APIRouter()

# Process-wide append-only research store (V1 persistence seam; see schema.sql
# for the eventual database shape).
_research_store = ResearchStore()


def get_research_store() -> ResearchStore:
    return _research_store


def get_candidate_provider() -> CandidateGenerationProvider:
    """Default candidate generator; swap for an LLM-backed provider later."""
    return TemplateCandidateProvider()


# Provider registry: adapters are constructed lazily so credentials are only
# required when a provider is actually used. Replaceable with Google Ads later.
SEARCH_DEMAND_PROVIDERS: dict[str, type[SearchDemandProvider]] = {
    "dataforseo": DataForSeoSearchDemandProvider,
}


class CandidateDiscoveryRequest(BaseModel):
    seed_keyword: str = Field(min_length=2, max_length=80)
    target_count: int = Field(default=DEFAULT_TARGET_COUNT, ge=1, le=40)


class RejectedCandidateOut(BaseModel):
    title: str
    reason: str


class CandidateDiscoveryResponse(BaseModel):
    research_run_id: UUID
    seed_keyword: str
    candidates: list[Candidate]
    generated_count: int
    duplicates_removed: int
    rejected: list[RejectedCandidateOut]


class SearchDemandResearchRequest(BaseModel):
    candidates: list[Candidate] | None = None
    research_run_id: UUID | None = None
    location: str = Field(default="US", min_length=2, max_length=60)
    language: str = Field(default="en", min_length=2, max_length=10)
    provider: str = "dataforseo"
    max_provider_calls: int | None = Field(default=None, ge=0, le=100)
    max_keywords: int | None = Field(default=None, ge=1, le=1000)

    @model_validator(mode="after")
    def _exactly_one_source(self) -> "SearchDemandResearchRequest":
        if (self.candidates is None) == (self.research_run_id is None):
            raise ValueError("provide exactly one of 'candidates' or 'research_run_id'")
        if self.candidates is not None and not self.candidates:
            raise ValueError("'candidates' must not be empty")
        return self


class SearchDemandSummaryOut(BaseModel):
    candidate_id: UUID
    relevant_keyword_count: int
    median_search_volume: float | None
    max_search_volume: int | None
    total_search_volume: int | None
    median_cpc: float | None
    competition_counts: dict[str, int]
    median_competition_index: float | None
    history_months_median: float | None
    missing_data_count: int
    search_demand_dimension: float | None
    dimension_version: str

    @classmethod
    def from_summary(cls, summary: SearchDemandSummary) -> "SearchDemandSummaryOut":
        return cls(
            candidate_id=summary.candidate_id,
            relevant_keyword_count=summary.relevant_keyword_count,
            median_search_volume=summary.median_search_volume,
            max_search_volume=summary.max_search_volume,
            total_search_volume=summary.total_search_volume,
            median_cpc=summary.median_cpc,
            competition_counts=summary.competition_counts,
            median_competition_index=summary.median_competition_index,
            history_months_median=summary.history_months_median,
            missing_data_count=summary.missing_data_count,
            search_demand_dimension=summary.search_demand_dimension,
            dimension_version=summary.dimension_version,
        )


class MissingKeywordOut(BaseModel):
    keyword: str
    reason: str
    candidate_ids: list[UUID]


class SearchDemandResearchResponse(BaseModel):
    snapshot: EvidenceSnapshot
    summaries: list[SearchDemandSummaryOut]
    evidence_ids_by_candidate: dict[UUID, list[UUID]]
    provider_errors: list[str]
    missing_keywords: list[MissingKeywordOut]
    cached_keyword_count: int


class SnapshotEvidenceResponse(BaseModel):
    snapshot: EvidenceSnapshot
    evidence: list[EvidenceItem]


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.post("/candidates/discover", response_model=CandidateDiscoveryResponse)
async def discover(
    request: CandidateDiscoveryRequest,
    provider: CandidateGenerationProvider = Depends(get_candidate_provider),
    store: ResearchStore = Depends(get_research_store),
) -> CandidateDiscoveryResponse:
    try:
        result = await discover_candidates(
            seed_keyword=request.seed_keyword,
            provider=provider,
            target_count=request.target_count,
        )
    except InvalidSeedKeywordError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except CandidateGenerationError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    if not result.candidates:
        raise HTTPException(
            status_code=502,
            detail="candidate generation produced no valid candidates for this seed keyword",
        )

    research_run_id = uuid4()
    store.register_run(research_run_id, result.candidates)

    return CandidateDiscoveryResponse(
        research_run_id=research_run_id,
        seed_keyword=result.seed_keyword,
        candidates=result.candidates,
        generated_count=result.generated_count,
        duplicates_removed=result.duplicates_removed,
        rejected=[RejectedCandidateOut(title=r.title, reason=r.reason) for r in result.rejected],
    )


@router.post("/research/search-demand", response_model=SearchDemandResearchResponse)
async def research_search_demand(
    request: SearchDemandResearchRequest,
    store: ResearchStore = Depends(get_research_store),
) -> SearchDemandResearchResponse:
    provider_cls = SEARCH_DEMAND_PROVIDERS.get(request.provider)
    if provider_cls is None:
        raise HTTPException(
            status_code=422,
            detail=f"unknown search-demand provider '{request.provider}'; "
            f"available: {sorted(SEARCH_DEMAND_PROVIDERS)}",
        )

    if request.candidates is not None:
        candidates = request.candidates
        research_run_id = None
    else:
        candidates_or_none = store.get_run_candidates(request.research_run_id)
        if candidates_or_none is None:
            raise HTTPException(
                status_code=404,
                detail=f"research run {request.research_run_id} not found",
            )
        candidates = candidates_or_none
        research_run_id = request.research_run_id

    try:
        provider = provider_cls()
    except MissingCredentialsError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    result = await run_search_demand_research(
        candidates=candidates,
        provider=provider,
        store=store,
        research_run_id=research_run_id,
        location=request.location,
        language=request.language,
        max_provider_calls=request.max_provider_calls,
        max_keywords=request.max_keywords,
    )

    return SearchDemandResearchResponse(
        snapshot=result.snapshot,
        summaries=[SearchDemandSummaryOut.from_summary(s) for s in result.summaries.values()],
        evidence_ids_by_candidate=result.evidence_ids_by_candidate,
        provider_errors=result.provider_errors,
        missing_keywords=[
            MissingKeywordOut(keyword=m.keyword, reason=m.reason, candidate_ids=m.candidate_ids)
            for m in result.missing_keywords
        ],
        cached_keyword_count=result.cached_keyword_count,
    )


@router.get(
    "/research/search-demand/snapshots/{snapshot_id}",
    response_model=SnapshotEvidenceResponse,
)
def get_snapshot(
    snapshot_id: UUID,
    store: ResearchStore = Depends(get_research_store),
) -> SnapshotEvidenceResponse:
    snapshot = store.get_snapshot(snapshot_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail=f"snapshot {snapshot_id} not found")
    return SnapshotEvidenceResponse(
        snapshot=snapshot,
        evidence=store.evidence_for_snapshot(snapshot_id),
    )


@router.post("/score", response_model=ScoreResult)
def score(
    dimensions: ScoreDimensions,
    evidence: list[EvidenceItem],
) -> ScoreResult:
    return score_opportunity(dimensions, evidence)
