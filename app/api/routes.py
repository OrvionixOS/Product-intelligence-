from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.domain.models import Candidate, EvidenceItem, ScoreDimensions, ScoreResult
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

router = APIRouter()


def get_candidate_provider() -> CandidateGenerationProvider:
    """Default candidate generator; swap for an LLM-backed provider later."""
    return TemplateCandidateProvider()


class CandidateDiscoveryRequest(BaseModel):
    seed_keyword: str = Field(min_length=2, max_length=80)
    target_count: int = Field(default=DEFAULT_TARGET_COUNT, ge=1, le=40)


class RejectedCandidateOut(BaseModel):
    title: str
    reason: str


class CandidateDiscoveryResponse(BaseModel):
    seed_keyword: str
    candidates: list[Candidate]
    generated_count: int
    duplicates_removed: int
    rejected: list[RejectedCandidateOut]


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.post("/candidates/discover", response_model=CandidateDiscoveryResponse)
async def discover(
    request: CandidateDiscoveryRequest,
    provider: CandidateGenerationProvider = Depends(get_candidate_provider),
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

    return CandidateDiscoveryResponse(
        seed_keyword=result.seed_keyword,
        candidates=result.candidates,
        generated_count=result.generated_count,
        duplicates_removed=result.duplicates_removed,
        rejected=[RejectedCandidateOut(title=r.title, reason=r.reason) for r in result.rejected],
    )


@router.post("/score", response_model=ScoreResult)
def score(
    dimensions: ScoreDimensions,
    evidence: list[EvidenceItem],
) -> ScoreResult:
    return score_opportunity(dimensions, evidence)
