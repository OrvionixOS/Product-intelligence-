from uuid import UUID

from fastapi import APIRouter

from app.domain.models import EvidenceItem, ScoreDimensions, ScoreResult
from app.services.scoring import score_opportunity

router = APIRouter()


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.post("/score", response_model=ScoreResult)
def score(
    dimensions: ScoreDimensions,
    evidence: list[EvidenceItem],
) -> ScoreResult:
    return score_opportunity(dimensions, evidence)
