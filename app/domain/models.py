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
    # --- query provenance (Milestone 4D-0) -------------------------------
    #
    # How this observation was FOUND, kept strictly apart from what was
    # observed. These fields live OUTSIDE raw_payload and are deliberately
    # excluded from raw_payload_hash: the hash is the identity of the
    # observation itself, so the same listing reached by two different
    # queries must still hash identically. Recording provenance inside the
    # payload would break every downstream deduplicator.
    #
    # `originating_queries` holds the NORMALIZED query strings actually sent
    # to the provider — provider-request provenance, not necessarily the
    # user's or Milestone 1's original raw wording, which is never stored
    # here. Entries are canonicalized, deduplicated and sorted, so the value
    # is deterministic regardless of the order results arrived in.
    #
    # Attribution is candidate-filtered: only queries generated for THIS
    # candidate appear, even when another candidate's query returned the
    # same observation.
    #
    #   None  provenance UNKNOWN — evidence recorded before this milestone,
    #         or by a path that does not capture it. Never reconstructed and
    #         never inferred from candidate text.
    #   ()    provenance known, and known to contain zero originating
    #         queries. This is not the same as UNKNOWN.
    #
    # Provenance is metadata about retrieval. It never changes truth_class:
    # knowing how a record was found says nothing about how well it is
    # supported, so it can never upgrade UNKNOWN or INFERRED to OBSERVED.
    originating_queries: tuple[str, ...] | None = None
    # True when at least one of this candidate's originating queries was also
    # generated for another candidate in the same research run. A shared
    # query must never read as evidence that it was uniquely generated for
    # this candidate. Deliberately a boolean, not a count: a number here
    # could later be misread as popularity, demand, reach, or market
    # strength. None means UNKNOWN, matching originating_queries.
    originating_query_shared: bool | None = None


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
