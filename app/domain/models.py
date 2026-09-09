from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from .enums import (
    CandidateStatus,
    Classification,
    EvidencePurpose,
    ProductFormat,
    SnapshotStatus,
    TruthClass,
)


class Candidate(BaseModel):
    """A proposed digital-product opportunity.

    A candidate is a hypothesis, not evidence. Nothing on this model implies
    demand, validation, or market fit; those are established later by the
    research and scoring pipeline.
    """

    id: UUID = Field(default_factory=uuid4)
    seed_keyword: str
    title: str
    problem: str
    target_buyer: str
    proposed_format: ProductFormat
    buyer_outcome: str
    search_queries: list[str] = Field(min_length=1)
    marketplace_queries: list[str] = Field(min_length=1)
    content_queries: list[str] = Field(min_length=1)
    generation_reason: str
    status: CandidateStatus = CandidateStatus.UNRESEARCHED


class EvidenceItem(BaseModel):
    """An immutable observation attached to an opportunity or candidate.

    Frozen: once created, an evidence record is never edited. A later research
    run creates new records under a new snapshot instead of overwriting.
    """

    model_config = ConfigDict(frozen=True)

    id: UUID = Field(default_factory=uuid4)
    opportunity_id: UUID | None = None
    candidate_id: UUID | None = None
    research_run_id: UUID | None = None
    snapshot_id: UUID | None = None
    signal_type: str
    purpose: EvidencePurpose
    truth_class: TruthClass
    provider: str
    collection_method: str
    source_reference: str | None = None
    source_url: str | None = None
    collected_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    retrieved_at: datetime | None = None
    geography: str | None = None
    language: str | None = None
    platform: str | None = None
    marketplace: str | None = None
    raw_value: float | str | None = None
    unit: str | None = None
    normalized_value: float | None = Field(default=None, ge=0, le=100)
    sample_size: int | None = None
    directness: float = Field(default=0.5, ge=0, le=1)
    source_quality: float = Field(default=0.5, ge=0, le=1)
    sample_adequacy: float = Field(default=0.5, ge=0, le=1)
    freshness: float = Field(default=1.0, ge=0, le=1)
    independence: float = Field(default=0.5, ge=0, le=1)
    cross_source_agreement: float = Field(default=0.5, ge=0, le=1)
    geographic_relevance: float = Field(default=1.0, ge=0, le=1)
    marketplace_relevance: float = Field(default=1.0, ge=0, le=1)
    known_limitations: list[str] = Field(default_factory=list)
    provider_version: str | None = None
    normalization_version: str = "v0.1"
    raw_payload: dict[str, Any] | None = None
    raw_payload_hash: str | None = None


class EvidenceSnapshot(BaseModel):
    """Metadata for the evidence collected during one research run pass.

    Snapshots are immutable once finalized; a later run creates a new one.
    """

    model_config = ConfigDict(frozen=True)

    snapshot_id: UUID = Field(default_factory=uuid4)
    research_run_id: UUID
    provider: str
    started_at: datetime
    completed_at: datetime | None = None
    geography: str
    language: str
    status: SnapshotStatus
    provider_call_count: int = 0
    provider_cost: float | None = None
    provider_cost_is_estimate: bool | None = None
    normalization_version: str


class ScoreDimensions(BaseModel):
    purchase_evidence: float | None = Field(default=None, ge=0, le=100)
    search_demand: float | None = Field(default=None, ge=0, le=100)
    audience_interest: float | None = Field(default=None, ge=0, le=100)
    price_strength: float | None = Field(default=None, ge=0, le=100)
    competition_opportunity: float | None = Field(default=None, ge=0, le=100)
    buyer_reach: float | None = Field(default=None, ge=0, le=100)
    problem_product_fit: float | None = Field(default=None, ge=0, le=100)


class ScoreResult(BaseModel):
    opportunity_score: float
    evidence_confidence: float
    classification: Classification
    dimensions: ScoreDimensions
    kill_rules_triggered: list[str] = Field(default_factory=list)
    missing_dimensions: list[str] = Field(default_factory=list)
    scoring_version: str = "opportunity_score_v0.1"
    confidence_version: str = "evidence_confidence_v0.1"
