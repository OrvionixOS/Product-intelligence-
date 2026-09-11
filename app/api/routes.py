import logging
from datetime import datetime
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, model_validator

from app.domain.models import (
    Candidate,
    EvidenceItem,
    EvidenceSnapshot,
)
from app.providers.base import (
    MarketplaceProvider,
    MissingCredentialsError,
    PublicContentProvider,
    SearchDemandProvider,
)
from app.providers.dataforseo import DataForSeoSearchDemandProvider
from app.providers.etsy import EtsyMarketplaceProvider
from app.providers.youtube import YouTubeContentProvider
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
from app.services.marketplace import run_marketplace_research
from app.services.marketplace_features import (
    CompetitionSummary,
    MarketplaceCandidateSummary,
    PriceSummary,
    PurchaseProxySummary,
)
from app.services.preliminary_dimensions import (
    CandidatePreliminaryProfile,
    DimensionState,
    PreliminaryDimension,
)
from app.services.preliminary_ranking import (
    DEEP_RESEARCH_SELECTION_SIZE,
    RankedCandidate,
    explain_pairwise,
)
from app.services.public_content import run_public_content_research
from app.services.public_content_features import ContentOutlier, PublicContentSummary
from app.services.audience_attention import (
    AttentionClaim,
    AttentionFeatures,
    AttentionProvenance,
    AudienceAttentionResult,
)
from app.services.buyer_reach import BuyerReachProvenance, BuyerReachResult, ReachChannel
from app.services.competition_opportunity import (
    CompetitionFieldFeatures,
    CompetitionFieldProvenance,
    CompetitionOpportunityResult,
    CompetitionReadings,
)
from app.services.price_evidence import (
    PriceBand,
    PriceEvidenceFeatures,
    PriceEvidenceResult,
    extract_price_evidence,
)
from app.services.product_job_fit import (
    LIMITATIONS as FIT_LIMITATIONS,
    PATTERN_BOUNDARIES,
    FitObservation,
    FitPattern,
    ProductJobFitFeatures,
    ProductJobFitResult,
    extract_product_job_fit,
)
from app.services.product_specification import (
    EvidenceOwnershipError,
    ProductSpecification,
    SpecClaimClass,
    SpecField,
    generate_product_specification,
)
from app.services.purchase_evidence import (
    PurchaseEvidenceFeatures,
    PurchaseEvidenceProvenance,
    PurchaseEvidenceResult,
    extract_purchase_evidence,
)
from app.services.research_orchestration import (
    CAPABILITY_MARKETPLACE,
    CAPABILITY_PUBLIC_CONTENT,
    CAPABILITY_SEARCH_DEMAND,
    STATUS_UNEXPECTED_PROVIDER_ERROR,
    CapabilityCaps,
    safe_exception_message,
    sanitize_error_message,
    CapabilityOutcome,
    DerivationOutcome,
    run_preliminary_research,
)
from app.services.search_demand import run_search_demand_research
from app.services.search_demand_features import SearchDemandSummary
from app.storage.memory import ResearchStore

logger = logging.getLogger(__name__)

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

# Marketplace adapters; joinable by other official marketplace APIs later.
MARKETPLACE_PROVIDERS: dict[str, type[MarketplaceProvider]] = {
    "etsy": EtsyMarketplaceProvider,
}

# Public-content adapters; joinable by other official platform APIs later.
PUBLIC_CONTENT_PROVIDERS: dict[str, type[PublicContentProvider]] = {
    "youtube": YouTubeContentProvider,
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


class MarketplaceResearchRequest(BaseModel):
    candidates: list[Candidate] | None = None
    research_run_id: UUID | None = None
    marketplace: str = "etsy"
    max_provider_calls: int | None = Field(default=None, ge=0, le=200)
    max_queries: int | None = Field(default=None, ge=1, le=200)
    max_listings_per_query: int | None = Field(default=None, ge=1, le=100)
    max_review_lookups: int | None = Field(default=None, ge=0, le=200)

    @model_validator(mode="after")
    def _exactly_one_source(self) -> "MarketplaceResearchRequest":
        if (self.candidates is None) == (self.research_run_id is None):
            raise ValueError("provide exactly one of 'candidates' or 'research_run_id'")
        if self.candidates is not None and not self.candidates:
            raise ValueError("'candidates' must not be empty")
        return self


class PurchaseProxySummaryOut(BaseModel):
    """Purchase PROXY features. Not purchases, sales, or revenue."""

    relevant_listing_count: int
    unique_seller_count: int
    listings_with_reviews: int
    median_review_count: float | None
    upper_quartile_review_count: float | None
    review_concentration: float | None
    rating_distribution: dict[str, int]
    missing_review_data_count: int
    features_version: str

    @classmethod
    def from_summary(cls, s: PurchaseProxySummary) -> "PurchaseProxySummaryOut":
        return cls(
            relevant_listing_count=s.relevant_listing_count,
            unique_seller_count=s.unique_seller_count,
            listings_with_reviews=s.listings_with_reviews,
            median_review_count=s.median_review_count,
            upper_quartile_review_count=s.upper_quartile_review_count,
            review_concentration=s.review_concentration,
            rating_distribution=s.rating_distribution,
            missing_review_data_count=s.missing_review_data_count,
            features_version=s.features_version,
        )


class PriceSummaryOut(BaseModel):
    relevant_paid_comparable_count: int
    unique_seller_count: int
    min_price: float | None
    p25_price: float | None
    median_price: float | None
    p75_price: float | None
    max_price: float | None
    currency: str | None
    mixed_currencies: bool
    missing_price_data_count: int
    insufficient_evidence: bool
    features_version: str

    @classmethod
    def from_summary(cls, s: PriceSummary) -> "PriceSummaryOut":
        return cls(
            relevant_paid_comparable_count=s.relevant_paid_comparable_count,
            unique_seller_count=s.unique_seller_count,
            min_price=s.min_price,
            p25_price=s.p25_price,
            median_price=s.median_price,
            p75_price=s.p75_price,
            max_price=s.max_price,
            currency=s.currency,
            mixed_currencies=s.mixed_currencies,
            missing_price_data_count=s.missing_price_data_count,
            insufficient_evidence=s.insufficient_evidence,
            features_version=s.features_version,
        )


class CompetitionSummaryOut(BaseModel):
    relevant_listing_count: int
    unique_seller_count: int
    seller_concentration: float | None
    review_burden_median: float | None
    review_burden_p75: float | None
    price_dispersion: float | None
    rating_distribution: dict[str, int]
    listing_age_days_median: float | None
    listings_with_age_data: int
    missing_data_count: int
    features_version: str

    @classmethod
    def from_summary(cls, s: CompetitionSummary) -> "CompetitionSummaryOut":
        return cls(
            relevant_listing_count=s.relevant_listing_count,
            unique_seller_count=s.unique_seller_count,
            seller_concentration=s.seller_concentration,
            review_burden_median=s.review_burden_median,
            review_burden_p75=s.review_burden_p75,
            price_dispersion=s.price_dispersion,
            rating_distribution=s.rating_distribution,
            listing_age_days_median=s.listing_age_days_median,
            listings_with_age_data=s.listings_with_age_data,
            missing_data_count=s.missing_data_count,
            features_version=s.features_version,
        )


class MarketplaceCandidateSummaryOut(BaseModel):
    candidate_id: UUID
    purchase_proxy: PurchaseProxySummaryOut
    price: PriceSummaryOut
    competition: CompetitionSummaryOut

    @classmethod
    def from_summary(cls, s: MarketplaceCandidateSummary) -> "MarketplaceCandidateSummaryOut":
        return cls(
            candidate_id=s.candidate_id,
            purchase_proxy=PurchaseProxySummaryOut.from_summary(s.purchase_proxy),
            price=PriceSummaryOut.from_summary(s.price),
            competition=CompetitionSummaryOut.from_summary(s.competition),
        )


class MissingQueryOut(BaseModel):
    query: str
    reason: str
    candidate_ids: list[UUID]


class PublicContentResearchRequest(BaseModel):
    candidates: list[Candidate] | None = None
    research_run_id: UUID | None = None
    provider: str = "youtube"
    max_provider_calls: int | None = Field(default=None, ge=0, le=500)
    max_queries: int | None = Field(default=None, ge=1, le=200)
    max_videos_per_query: int | None = Field(default=None, ge=1, le=50)
    max_quota_units: int | None = Field(default=None, ge=0, le=10000)
    max_channel_lookups: int | None = Field(default=None, ge=0, le=100)

    @model_validator(mode="after")
    def _exactly_one_source(self) -> "PublicContentResearchRequest":
        if (self.candidates is None) == (self.research_run_id is None):
            raise ValueError("provide exactly one of 'candidates' or 'research_run_id'")
        if self.candidates is not None and not self.candidates:
            raise ValueError("'candidates' must not be empty")
        return self


class ContentOutlierOut(BaseModel):
    """A content-performance outlier observation — not a viral prediction."""

    video_id: str
    channel_id: str
    video_views: int
    channel_sample_size: int
    channel_baseline_median_views: float
    outlier_ratio: float
    formula_version: str

    @classmethod
    def from_outlier(cls, o: ContentOutlier) -> "ContentOutlierOut":
        return cls(
            video_id=o.video_id,
            channel_id=o.channel_id,
            video_views=o.video_views,
            channel_sample_size=o.channel_sample_size,
            channel_baseline_median_views=o.channel_baseline_median_views,
            outlier_ratio=o.outlier_ratio,
            formula_version=o.formula_version,
        )


class PublicContentSummaryOut(BaseModel):
    candidate_id: UUID
    relevant_video_count: int
    distinct_channel_count: int
    total_views: int | None
    median_views: float | None
    max_views: int | None
    median_likes: float | None
    median_comments: float | None
    median_engagement_rate: float | None
    median_days_since_publish: float | None
    videos_published_last_90_days: int
    missing_view_count: int
    missing_like_count: int
    missing_comment_count: int
    channels_with_baseline: int
    content_outliers: list[ContentOutlierOut]
    audience_interest_dimension: float | None
    dimension_version: str
    features_version: str

    @classmethod
    def from_summary(cls, s: PublicContentSummary) -> "PublicContentSummaryOut":
        return cls(
            candidate_id=s.candidate_id,
            relevant_video_count=s.relevant_video_count,
            distinct_channel_count=s.distinct_channel_count,
            total_views=s.total_views,
            median_views=s.median_views,
            max_views=s.max_views,
            median_likes=s.median_likes,
            median_comments=s.median_comments,
            median_engagement_rate=s.median_engagement_rate,
            median_days_since_publish=s.median_days_since_publish,
            videos_published_last_90_days=s.videos_published_last_90_days,
            missing_view_count=s.missing_view_count,
            missing_like_count=s.missing_like_count,
            missing_comment_count=s.missing_comment_count,
            channels_with_baseline=s.channels_with_baseline,
            content_outliers=[ContentOutlierOut.from_outlier(o) for o in s.content_outliers],
            audience_interest_dimension=s.audience_interest_dimension,
            dimension_version=s.dimension_version,
            features_version=s.features_version,
        )


class PublicContentResearchResponse(BaseModel):
    snapshot: EvidenceSnapshot
    summaries: list[PublicContentSummaryOut]
    evidence_ids_by_candidate: dict[UUID, list[UUID]]
    provider_errors: list[str]
    missing_queries: list[MissingQueryOut]
    cached_query_count: int
    unique_video_count: int
    quota_units_used: int
    quota_units_is_exact: bool
    channel_stats_fetched: int
    channel_stats_skipped: int


class MarketplaceResearchResponse(BaseModel):
    snapshot: EvidenceSnapshot
    summaries: list[MarketplaceCandidateSummaryOut]
    evidence_ids_by_candidate: dict[UUID, list[UUID]]
    provider_errors: list[str]
    missing_queries: list[MissingQueryOut]
    cached_query_count: int
    unique_listing_count: int
    review_lookups_performed: int
    review_lookups_skipped: int


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


@router.post("/research/marketplace", response_model=MarketplaceResearchResponse)
async def research_marketplace(
    request: MarketplaceResearchRequest,
    store: ResearchStore = Depends(get_research_store),
) -> MarketplaceResearchResponse:
    provider_cls = MARKETPLACE_PROVIDERS.get(request.marketplace)
    if provider_cls is None:
        raise HTTPException(
            status_code=422,
            detail=f"unknown marketplace '{request.marketplace}'; "
            f"available: {sorted(MARKETPLACE_PROVIDERS)}",
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

    result = await run_marketplace_research(
        candidates=candidates,
        provider=provider,
        store=store,
        research_run_id=research_run_id,
        max_provider_calls=request.max_provider_calls,
        max_queries=request.max_queries,
        max_listings_per_query=request.max_listings_per_query,
        max_review_lookups=request.max_review_lookups,
    )

    return MarketplaceResearchResponse(
        snapshot=result.snapshot,
        summaries=[
            MarketplaceCandidateSummaryOut.from_summary(s) for s in result.summaries.values()
        ],
        evidence_ids_by_candidate=result.evidence_ids_by_candidate,
        provider_errors=result.provider_errors,
        missing_queries=[
            MissingQueryOut(query=m.query, reason=m.reason, candidate_ids=m.candidate_ids)
            for m in result.missing_queries
        ],
        cached_query_count=result.cached_query_count,
        unique_listing_count=result.unique_listing_count,
        review_lookups_performed=result.review_lookups_performed,
        review_lookups_skipped=result.review_lookups_skipped,
    )


@router.get(
    "/research/marketplace/snapshots/{snapshot_id}",
    response_model=SnapshotEvidenceResponse,
)
def get_marketplace_snapshot(
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


@router.post("/research/public-content", response_model=PublicContentResearchResponse)
async def research_public_content(
    request: PublicContentResearchRequest,
    store: ResearchStore = Depends(get_research_store),
) -> PublicContentResearchResponse:
    provider_cls = PUBLIC_CONTENT_PROVIDERS.get(request.provider)
    if provider_cls is None:
        raise HTTPException(
            status_code=422,
            detail=f"unknown public-content provider '{request.provider}'; "
            f"available: {sorted(PUBLIC_CONTENT_PROVIDERS)}",
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

    result = await run_public_content_research(
        candidates=candidates,
        provider=provider,
        store=store,
        research_run_id=research_run_id,
        max_provider_calls=request.max_provider_calls,
        max_queries=request.max_queries,
        max_videos_per_query=request.max_videos_per_query,
        max_quota_units=request.max_quota_units,
        max_channel_lookups=request.max_channel_lookups,
    )

    return PublicContentResearchResponse(
        snapshot=result.snapshot,
        summaries=[
            PublicContentSummaryOut.from_summary(s) for s in result.summaries.values()
        ],
        evidence_ids_by_candidate=result.evidence_ids_by_candidate,
        provider_errors=result.provider_errors,
        missing_queries=[
            MissingQueryOut(query=m.query, reason=m.reason, candidate_ids=m.candidate_ids)
            for m in result.missing_queries
        ],
        cached_query_count=result.cached_query_count,
        unique_video_count=result.unique_video_count,
        quota_units_used=result.quota_units_used,
        quota_units_is_exact=result.quota_units_is_exact,
        channel_stats_fetched=result.channel_stats_fetched,
        channel_stats_skipped=result.channel_stats_skipped,
    )


@router.get(
    "/research/public-content/snapshots/{snapshot_id}",
    response_model=SnapshotEvidenceResponse,
)
def get_public_content_snapshot(
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


class PreliminaryResearchRequest(BaseModel):
    """Milestone 3C: research every candidate, then rank them preliminarily.

    Each capability is opt-out: a capability whose provider is disabled or
    whose credentials are missing is reported as unavailable rather than
    failing the whole run.
    """

    candidates: list[Candidate] | None = None
    research_run_id: UUID | None = None
    location: str = Field(default="US", min_length=2, max_length=60)
    language: str = Field(default="en", min_length=2, max_length=10)
    search_demand_provider: str | None = "dataforseo"
    marketplace: str | None = "etsy"
    public_content_provider: str | None = "youtube"
    selection_size: int = Field(default=DEEP_RESEARCH_SELECTION_SIZE, ge=1, le=20)

    @model_validator(mode="after")
    def _exactly_one_source(self) -> "PreliminaryResearchRequest":
        if (self.candidates is None) == (self.research_run_id is None):
            raise ValueError("provide exactly one of 'candidates' or 'research_run_id'")
        if self.candidates is not None and not self.candidates:
            raise ValueError("'candidates' must not be empty")
        return self


class CapabilityOutcomeOut(BaseModel):
    """One capability's honest outcome.

    `status` is `COMPLETE`/`PARTIAL`/`FAILED` from the evidence snapshot, or
    `NOT_REQUESTED`, `PROVIDER_FAILED` (anticipated provider failure), or
    `UNEXPECTED_PROVIDER_ERROR` (a defect outside the provider contract).
    `failure_reason` on an unexpected error carries the exception type, a
    sanitized message, and a correlation id matching the server-side log —
    never a stack trace, credential, or authorization header.
    """

    capability: str
    status: str
    provider: str | None
    snapshot_id: UUID | None
    provider_errors: list[str]
    missing_query_count: int
    provider_call_count: int
    provider_cost: float | None
    provider_cost_is_estimate: bool | None
    quota_units_used: int | None
    quota_units_is_exact: bool | None
    cached_query_count: int
    failure_reason: str | None
    # True only for a defect outside the provider contract, so a client can
    # distinguish "the provider failed as it may" from "something is broken".
    unexpected_error: bool = False

    @classmethod
    def from_outcome(cls, o: CapabilityOutcome) -> "CapabilityOutcomeOut":
        return cls(
            capability=o.capability,
            status=o.status,
            provider=o.provider,
            snapshot_id=o.snapshot_id,
            provider_errors=o.provider_errors,
            missing_query_count=o.missing_query_count,
            provider_call_count=o.provider_call_count,
            provider_cost=o.provider_cost,
            provider_cost_is_estimate=o.provider_cost_is_estimate,
            quota_units_used=o.quota_units_used,
            quota_units_is_exact=o.quota_units_is_exact,
            cached_query_count=o.cached_query_count,
            failure_reason=o.failure_reason,
            unexpected_error=o.status == STATUS_UNEXPECTED_PROVIDER_ERROR,
        )


class PreliminaryDimensionOut(BaseModel):
    """A preliminary dimension with its provenance and truth state intact."""

    name: str
    state: str
    value: float | None
    evidence_truth_basis: str | None
    value_truth_class: str | None
    formula_version: str
    observed_input_count: int
    unknown_input_count: int
    duplicate_evidence_suppressed: int
    contributing_evidence_ids: list[UUID]
    providers: list[str]
    source_references: list[str]
    limitations: list[str]
    missing_reason: str | None
    bridge_version: str

    @classmethod
    def from_dimension(cls, d: PreliminaryDimension) -> "PreliminaryDimensionOut":
        return cls(
            name=d.name,
            state=d.state.value,
            value=d.value,
            evidence_truth_basis=d.evidence_truth_basis.value if d.evidence_truth_basis else None,
            value_truth_class=d.value_truth_class.value if d.value_truth_class else None,
            formula_version=d.formula_version,
            observed_input_count=d.observed_input_count,
            unknown_input_count=d.unknown_input_count,
            duplicate_evidence_suppressed=d.duplicate_evidence_suppressed,
            contributing_evidence_ids=list(d.contributing_evidence_ids),
            providers=list(d.providers),
            source_references=list(d.source_references),
            limitations=list(d.limitations),
            missing_reason=d.missing_reason,
            bridge_version=d.bridge_version,
        )


class CriterionValueOut(BaseModel):
    name: str
    value: float | str | None
    direction: str


class RankedCandidateOut(BaseModel):
    candidate_id: UUID
    candidate_title: str
    rank: int
    selected_for_deep_research: bool
    criteria: list[CriterionValueOut]
    dimensions: list[PreliminaryDimensionOut]
    ranking_version: str

    @classmethod
    def from_ranked(cls, r: RankedCandidate) -> "RankedCandidateOut":
        profile: CandidatePreliminaryProfile = r.profile
        return cls(
            candidate_id=r.candidate_id,
            candidate_title=r.candidate_title,
            rank=r.rank,
            selected_for_deep_research=r.selected_for_deep_research,
            criteria=[
                CriterionValueOut(name=c.name, value=c.value, direction=c.direction)
                for c in r.criteria
            ],
            dimensions=[
                PreliminaryDimensionOut.from_dimension(profile.dimensions[name])
                for name in sorted(profile.dimensions)
            ],
            ranking_version=r.ranking_version,
        )


class RankExplanationOut(BaseModel):
    higher_candidate_id: UUID
    lower_candidate_id: UUID
    deciding_criterion: str
    higher_value: str
    lower_value: str
    reason: str


class PurchaseEvidenceFeaturesOut(BaseModel):
    """Deterministic purchase-PROXY features. None of these is a sales figure."""

    relevant_comparable_count: int
    paid_comparable_count: int
    distinct_seller_count: int
    excluded_physical_listing_count: int
    unknown_format_listing_count: int
    listings_with_observed_review_count: int
    listings_with_unknown_review_count: int
    listings_with_observed_zero_reviews: int
    listings_with_proxy_evidence: int
    proportion_of_comparables_with_proxy: float | None
    median_review_count: float | None
    upper_quartile_review_count: float | None
    max_review_count: int | None
    winsorized_mean_review_count: float | None
    sellers_with_proxy_evidence: int
    # Share of OBSERVED REVIEW COUNTS held by the largest seller. This is
    # not market share, not revenue share, and not a unit count.
    top_seller_proxy_share: float | None
    listings_with_creation_date: int
    listing_age_days_median: float | None
    established_listing_count: int
    established_seller_count: int
    currencies_observed: list[str]
    mixed_currencies: bool
    features_version: str

    @classmethod
    def from_features(cls, f: PurchaseEvidenceFeatures) -> "PurchaseEvidenceFeaturesOut":
        return cls(
            relevant_comparable_count=f.relevant_comparable_count,
            paid_comparable_count=f.paid_comparable_count,
            distinct_seller_count=f.distinct_seller_count,
            excluded_physical_listing_count=f.excluded_physical_listing_count,
            unknown_format_listing_count=f.unknown_format_listing_count,
            listings_with_observed_review_count=f.listings_with_observed_review_count,
            listings_with_unknown_review_count=f.listings_with_unknown_review_count,
            listings_with_observed_zero_reviews=f.listings_with_observed_zero_reviews,
            listings_with_proxy_evidence=f.listings_with_proxy_evidence,
            proportion_of_comparables_with_proxy=f.proportion_of_comparables_with_proxy,
            median_review_count=f.median_review_count,
            upper_quartile_review_count=f.upper_quartile_review_count,
            max_review_count=f.max_review_count,
            winsorized_mean_review_count=f.winsorized_mean_review_count,
            sellers_with_proxy_evidence=f.sellers_with_proxy_evidence,
            top_seller_proxy_share=f.top_seller_proxy_share,
            listings_with_creation_date=f.listings_with_creation_date,
            listing_age_days_median=f.listing_age_days_median,
            established_listing_count=f.established_listing_count,
            established_seller_count=f.established_seller_count,
            currencies_observed=list(f.currencies_observed),
            mixed_currencies=f.mixed_currencies,
            features_version=f.features_version,
        )


class PurchaseEvidenceProvenanceOut(BaseModel):
    """Evidence lineage. Shared by Purchase Evidence and Price Evidence,
    which reconstruct listings through the same helper and therefore carry
    identical provenance shapes."""

    evidence_ids: list[UUID]
    listing_ids: list[str]
    providers: list[str]
    marketplaces: list[str]
    source_references: list[str]
    source_truth_classes: list[str]
    source_purposes: list[str]
    earliest_retrieved_at: datetime | None
    latest_retrieved_at: datetime | None
    duplicate_evidence_suppressed: int

    @classmethod
    def from_provenance(
        cls, p: PurchaseEvidenceProvenance
    ) -> "PurchaseEvidenceProvenanceOut":
        return cls(
            evidence_ids=list(p.evidence_ids),
            listing_ids=list(p.listing_ids),
            providers=list(p.providers),
            marketplaces=list(p.marketplaces),
            source_references=list(p.source_references),
            source_truth_classes=list(p.source_truth_classes),
            source_purposes=list(p.source_purposes),
            earliest_retrieved_at=p.earliest_retrieved_at,
            latest_retrieved_at=p.latest_retrieved_at,
            duplicate_evidence_suppressed=p.duplicate_evidence_suppressed,
        )


class PurchaseEvidenceOut(BaseModel):
    """Purchase Evidence for one candidate (Milestone 4A).

    `value` is always null: no purchase-evidence formula is approved, so no
    0-100 score is produced. `exact_units_sold` and `exact_revenue` are
    always UNKNOWN — that data is not public and is never estimated.
    """

    candidate_id: UUID
    state: str
    value: float | None
    pattern: str
    source: str
    value_truth_class: str | None
    features_truth_class: str | None
    evidence_truth_basis: str | None
    exact_units_sold: str
    exact_revenue: str
    direct_authorized_evidence_available: bool
    missing_reason: str | None
    features: PurchaseEvidenceFeaturesOut | None
    provenance: PurchaseEvidenceProvenanceOut
    limitations: list[str]
    dimension_name: str
    version: str
    pattern_version: str

    @classmethod
    def from_result(cls, r: PurchaseEvidenceResult) -> "PurchaseEvidenceOut":
        return cls(
            candidate_id=r.candidate_id,
            state=r.state.value,
            value=r.value,
            pattern=r.pattern.value,
            source=r.source.value,
            value_truth_class=r.value_truth_class.value if r.value_truth_class else None,
            features_truth_class=(
                r.features_truth_class.value if r.features_truth_class else None
            ),
            evidence_truth_basis=(
                r.evidence_truth_basis.value if r.evidence_truth_basis else None
            ),
            exact_units_sold=r.exact_units_sold.value,
            exact_revenue=r.exact_revenue.value,
            direct_authorized_evidence_available=r.direct_authorized_evidence_available,
            missing_reason=r.missing_reason,
            features=(
                PurchaseEvidenceFeaturesOut.from_features(r.features) if r.features else None
            ),
            provenance=PurchaseEvidenceProvenanceOut.from_provenance(r.provenance),
            limitations=list(r.limitations),
            dimension_name=r.dimension_name,
            version=r.version,
            pattern_version=r.pattern_version,
        )


class PriceBandOut(BaseModel):
    """Observed ASKING-price statistics for ONE currency.

    Never compared against another currency: no approved FX source exists.
    Every figure is an asking price, not a verified transaction price, and
    none of it is a price recommendation.
    """

    currency: str
    paid_listing_count: int
    observed_prices: list[float]
    min_paid_asking_price: float
    p25_asking_price: float
    median_asking_price: float
    p75_asking_price: float
    p90_asking_price: float | None
    max_paid_asking_price: float
    interquartile_range: float
    coefficient_of_variation: float | None
    trimmed_mean_asking_price: float | None
    established_listing_median_asking_price: float | None
    established_listing_count: int
    purchase_proxy_median_asking_price: float | None
    purchase_proxy_listing_count: int
    listings_without_seller_id: int
    distinct_seller_count: int
    insufficient_evidence: bool
    band_version: str

    @classmethod
    def from_band(cls, b: PriceBand) -> "PriceBandOut":
        return cls(
            currency=b.currency,
            paid_listing_count=b.paid_listing_count,
            observed_prices=list(b.observed_prices),
            min_paid_asking_price=b.min_paid_asking_price,
            p25_asking_price=b.p25_asking_price,
            median_asking_price=b.median_asking_price,
            p75_asking_price=b.p75_asking_price,
            p90_asking_price=b.p90_asking_price,
            max_paid_asking_price=b.max_paid_asking_price,
            interquartile_range=b.interquartile_range,
            coefficient_of_variation=b.coefficient_of_variation,
            trimmed_mean_asking_price=b.trimmed_mean_asking_price,
            established_listing_median_asking_price=b.established_listing_median_asking_price,
            established_listing_count=b.established_listing_count,
            purchase_proxy_median_asking_price=b.purchase_proxy_median_asking_price,
            purchase_proxy_listing_count=b.purchase_proxy_listing_count,
            listings_without_seller_id=b.listings_without_seller_id,
            distinct_seller_count=b.distinct_seller_count,
            insufficient_evidence=b.insufficient_evidence,
            band_version=b.band_version,
        )


class PriceEvidenceFeaturesOut(BaseModel):
    total_relevant_listings: int
    listings_with_observed_price: int
    listings_with_unknown_price: int
    free_listing_count: int
    paid_comparable_count: int
    invalid_price_listing_count: int
    paid_listings_without_currency: int
    excluded_physical_listing_count: int
    unknown_format_listing_count: int
    free_proportion_of_priced_listings: float | None
    currencies_observed: list[str]
    bands: list[PriceBandOut]
    multiple_currencies_present: bool
    # Permanently false: bands are per-currency and never combined.
    cross_currency_comparison: bool
    features_version: str

    @classmethod
    def from_features(cls, f: PriceEvidenceFeatures) -> "PriceEvidenceFeaturesOut":
        return cls(
            total_relevant_listings=f.total_relevant_listings,
            listings_with_observed_price=f.listings_with_observed_price,
            listings_with_unknown_price=f.listings_with_unknown_price,
            free_listing_count=f.free_listing_count,
            paid_comparable_count=f.paid_comparable_count,
            invalid_price_listing_count=f.invalid_price_listing_count,
            paid_listings_without_currency=f.paid_listings_without_currency,
            excluded_physical_listing_count=f.excluded_physical_listing_count,
            unknown_format_listing_count=f.unknown_format_listing_count,
            free_proportion_of_priced_listings=f.free_proportion_of_priced_listings,
            currencies_observed=list(f.currencies_observed),
            bands=[PriceBandOut.from_band(b) for b in f.bands],
            multiple_currencies_present=f.multiple_currencies_present,
            cross_currency_comparison=f.cross_currency_comparison,
            features_version=f.features_version,
        )


class PriceEvidenceOut(BaseModel):
    """Price Evidence for one candidate (Milestone 4B).

    `value` is always null: no price-evidence formula is approved, so no
    0-100 score is produced. `transaction_prices`, `willingness_to_pay`, and
    `recommended_price` are always UNKNOWN — none is derivable from public
    listing data and none is ever estimated.
    """

    candidate_id: UUID
    state: str
    value: float | None
    basis: str
    value_truth_class: str | None
    features_truth_class: str | None
    evidence_truth_basis: str | None
    transaction_prices: str
    willingness_to_pay: str
    recommended_price: str
    missing_reason: str | None
    features: PriceEvidenceFeaturesOut | None
    provenance: PurchaseEvidenceProvenanceOut
    limitations: list[str]
    dimension_name: str
    version: str
    band_version: str

    @classmethod
    def from_result(cls, r: PriceEvidenceResult) -> "PriceEvidenceOut":
        return cls(
            candidate_id=r.candidate_id,
            state=r.state.value,
            value=r.value,
            basis=r.basis.value,
            value_truth_class=r.value_truth_class.value if r.value_truth_class else None,
            features_truth_class=(
                r.features_truth_class.value if r.features_truth_class else None
            ),
            evidence_truth_basis=(
                r.evidence_truth_basis.value if r.evidence_truth_basis else None
            ),
            transaction_prices=r.transaction_prices.value,
            willingness_to_pay=r.willingness_to_pay.value,
            recommended_price=r.recommended_price.value,
            missing_reason=r.missing_reason,
            features=(
                PriceEvidenceFeaturesOut.from_features(r.features) if r.features else None
            ),
            provenance=PurchaseEvidenceProvenanceOut.from_provenance(r.provenance),
            limitations=list(r.limitations),
            dimension_name=r.dimension_name,
            version=r.version,
            band_version=r.band_version,
        )


# --------------------------------------------------------------------------
# Milestone 4D: Buyer Reach.
#
# CHANNEL evidence, never buyer evidence. Nothing here is a buyer count, an
# audience size, a market size, or a conversion estimate, and no field is a
# score. The permanent-UNKNOWN markers below are structural so the refusal is
# visible in the payload rather than only in prose.
# --------------------------------------------------------------------------


class ReachEndpointOut(BaseModel):
    # Whatever type the provider payload carried, uncoerced.
    endpoint_id: str
    url: str | None
    # Observations of this endpoint, never buyers and never reach.
    observation_count: int
    evidence_ids: list[UUID]
    originating_queries: list[str]
    found_only_via_shared_queries: bool


class ReachChannelOut(BaseModel):
    channel_class: str
    platform: str | None
    # Four claim layers, never collapsed into one "reachable" verdict.
    existence: str
    relevance: str
    relevance_basis: str
    activity: str
    distinct_endpoint_count: int
    endpoint_sample: list[ReachEndpointOut]

    # Marketplace. Deliberately NOT named distinct_seller_count: Milestone 4A
    # uses that name for a different population (sellers carrying review-proxy
    # evidence, not sellers with any relevant listing).
    sellers_with_relevant_listing_count: int | None
    distinct_listing_count: int | None
    listings_without_seller_id: int | None
    sellers_with_multiple_listings: int | None
    established_listing_count: int | None
    listings_without_created_at: int | None
    excluded_physical_listing_count: int | None

    # Public content. An observable content audience around the topic; never
    # evidence that a viewer will buy.
    creator_channels_with_relevant_video_count: int | None
    distinct_video_count: int | None
    videos_without_channel_id: int | None
    channels_with_multiple_videos: int | None
    recent_video_count: int | None
    videos_without_published_at: int | None

    # Search. paid_auction_observed=null means UNKNOWN (no keyword carried
    # auction data), never "no auction". It means only that advertisers bid.
    keywords_with_observed_volume: int | None
    keywords_without_observed_volume: int | None
    paid_auction_observed: bool | None
    keywords_with_observed_auction: int | None
    # Geography is known only for search evidence and never inferred elsewhere.
    observed_locations: list[str]
    observed_languages: list[str]

    evidence_ids: list[UUID]
    missing_reason: str | None

    @classmethod
    def from_channel(cls, c: ReachChannel) -> "ReachChannelOut":
        return cls(
            channel_class=c.channel_class.value,
            platform=c.platform,
            existence=c.existence.value,
            relevance=c.relevance.value,
            relevance_basis=c.relevance_basis,
            activity=c.activity.value,
            distinct_endpoint_count=c.distinct_endpoint_count,
            endpoint_sample=[
                ReachEndpointOut(
                    endpoint_id=str(e.endpoint_id),
                    url=e.url,
                    observation_count=e.observation_count,
                    evidence_ids=list(e.evidence_ids),
                    originating_queries=list(e.originating_queries),
                    found_only_via_shared_queries=e.found_only_via_shared_queries,
                )
                for e in c.endpoint_sample
            ],
            sellers_with_relevant_listing_count=c.sellers_with_relevant_listing_count,
            distinct_listing_count=c.distinct_listing_count,
            listings_without_seller_id=c.listings_without_seller_id,
            sellers_with_multiple_listings=c.sellers_with_multiple_listings,
            established_listing_count=c.established_listing_count,
            listings_without_created_at=c.listings_without_created_at,
            excluded_physical_listing_count=c.excluded_physical_listing_count,
            creator_channels_with_relevant_video_count=(
                c.creator_channels_with_relevant_video_count
            ),
            distinct_video_count=c.distinct_video_count,
            videos_without_channel_id=c.videos_without_channel_id,
            channels_with_multiple_videos=c.channels_with_multiple_videos,
            recent_video_count=c.recent_video_count,
            videos_without_published_at=c.videos_without_published_at,
            keywords_with_observed_volume=c.keywords_with_observed_volume,
            keywords_without_observed_volume=c.keywords_without_observed_volume,
            paid_auction_observed=c.paid_auction_observed,
            keywords_with_observed_auction=c.keywords_with_observed_auction,
            observed_locations=list(c.observed_locations),
            observed_languages=list(c.observed_languages),
            evidence_ids=list(c.evidence_ids),
            missing_reason=c.missing_reason,
        )


class BuyerReachProvenanceOut(BaseModel):
    """Lineage, canonically ordered by Milestone 4D itself."""

    candidate_id: UUID
    research_run_id: UUID | None
    evidence_ids: list[UUID]
    providers: list[str]
    platforms: list[str]
    source_truth_classes: list[str]
    originating_queries: list[str]
    earliest_retrieved_at: datetime | None
    latest_retrieved_at: datetime | None

    @classmethod
    def from_provenance(cls, p: BuyerReachProvenance) -> "BuyerReachProvenanceOut":
        return cls(
            candidate_id=p.candidate_id,
            research_run_id=p.research_run_id,
            evidence_ids=list(p.evidence_ids),
            providers=list(p.providers),
            platforms=list(p.platforms),
            source_truth_classes=list(p.source_truth_classes),
            originating_queries=list(p.originating_queries),
            earliest_retrieved_at=p.earliest_retrieved_at,
            latest_retrieved_at=p.latest_retrieved_at,
        )


class BuyerReachOut(BaseModel):
    candidate_id: UUID
    state: str
    # Always null: no approved formula converts channels into a score.
    value: float | None
    # Evidence SHAPE only. MULTI_CHANNEL_CLASS is not "better".
    pattern: str
    channels: list[ReachChannelOut]
    provenance: BuyerReachProvenanceOut

    channel_classes_with_observed_evidence: int
    # Literally what it says. NOT "corroborated": different provider surfaces
    # observe different things, they do not verify one proposition.
    observed_across_multiple_providers: bool
    channels_found_only_via_shared_queries: int

    # Permanent UNKNOWN markers.
    buyer_count: str
    audience_size: str
    market_size: str
    conversion_probability: str
    addressability: str
    guaranteed_distribution: str

    missing_reason: str | None
    limitations: list[str]
    dimension_name: str
    version: str
    pattern_version: str

    @classmethod
    def from_result(cls, r: BuyerReachResult) -> "BuyerReachOut":
        return cls(
            candidate_id=r.candidate_id,
            state=r.state.value,
            value=r.value,
            pattern=r.pattern.value,
            channels=[ReachChannelOut.from_channel(c) for c in r.channels],
            provenance=BuyerReachProvenanceOut.from_provenance(r.provenance),
            channel_classes_with_observed_evidence=(
                r.channel_classes_with_observed_evidence
            ),
            observed_across_multiple_providers=r.observed_across_multiple_providers,
            channels_found_only_via_shared_queries=(
                r.channels_found_only_via_shared_queries
            ),
            buyer_count=r.buyer_count.value,
            audience_size=r.audience_size.value,
            market_size=r.market_size.value,
            conversion_probability=r.conversion_probability.value,
            addressability=r.addressability.value,
            guaranteed_distribution=r.guaranteed_distribution.value,
            missing_reason=r.missing_reason,
            limitations=list(r.limitations),
            dimension_name=r.dimension_name,
            version=r.version,
            pattern_version=r.pattern_version,
        )


class CompetitionReadingsOut(BaseModel):
    """The two opposed readings of one field pattern, permanently paired.

    Both are always present. Neither is preferred, and the pair is what
    encodes competition's non-monotonicity: the same evidence argues in both
    directions, and the evidence cannot settle which reading holds.
    """

    market_exists_reading: str
    entry_difficulty_reading: str
    unresolvable_because: str

    @classmethod
    def from_readings(cls, r: CompetitionReadings) -> "CompetitionReadingsOut":
        return cls(
            market_exists_reading=r.market_exists_reading,
            entry_difficulty_reading=r.entry_difficulty_reading,
            unresolvable_because=r.unresolvable_because,
        )


class CompetitionFieldFeaturesOut(BaseModel):
    """Structural features of marketplace supply. No price, review, or rating."""

    competing_listing_count: int
    excluded_physical_listing_count: int
    unknown_format_listing_count: int
    listings_with_seller_attribution: int
    listings_without_seller_attribution: int
    seller_count_in_field: int
    # How much of the field the concentration statistics below describe. Read
    # every share below as a statement about this share of the listings.
    seller_attribution_share: float | None
    # Shares of LISTINGS. Not Milestone 4A's top_seller_proxy_share, which is
    # a share of observed review volume over a different population.
    # Null means no listing carried a seller: unknown, never zero.
    top_seller_listing_share: float | None
    max_listings_per_seller: int | None
    single_listing_seller_count: int | None
    sellers_covering_half_of_listings: int | None
    features_version: str

    @classmethod
    def from_features(cls, f: CompetitionFieldFeatures) -> "CompetitionFieldFeaturesOut":
        return cls(
            competing_listing_count=f.competing_listing_count,
            excluded_physical_listing_count=f.excluded_physical_listing_count,
            unknown_format_listing_count=f.unknown_format_listing_count,
            listings_with_seller_attribution=f.listings_with_seller_attribution,
            listings_without_seller_attribution=f.listings_without_seller_attribution,
            seller_count_in_field=f.seller_count_in_field,
            seller_attribution_share=f.seller_attribution_share,
            top_seller_listing_share=f.top_seller_listing_share,
            max_listings_per_seller=f.max_listings_per_seller,
            single_listing_seller_count=f.single_listing_seller_count,
            sellers_covering_half_of_listings=f.sellers_covering_half_of_listings,
            features_version=f.features_version,
        )


class CompetitionFieldProvenanceOut(BaseModel):
    """Lineage, canonically ordered by Milestone 4E itself."""

    candidate_id: UUID
    evidence_ids: list[UUID]
    listing_ids: list[str]
    seller_ids: list[str]
    providers: list[str]
    marketplaces: list[str]
    source_truth_classes: list[str]
    duplicate_evidence_suppressed: int

    @classmethod
    def from_provenance(
        cls, p: CompetitionFieldProvenance
    ) -> "CompetitionFieldProvenanceOut":
        return cls(
            candidate_id=p.candidate_id,
            evidence_ids=list(p.evidence_ids),
            listing_ids=list(p.listing_ids),
            seller_ids=list(p.seller_ids),
            providers=list(p.providers),
            marketplaces=list(p.marketplaces),
            source_truth_classes=list(p.source_truth_classes),
            duplicate_evidence_suppressed=p.duplicate_evidence_suppressed,
        )


class CompetitionOpportunityOut(BaseModel):
    candidate_id: UUID
    state: str
    # Always null: competition is non-monotonic and no formula is approved.
    value: float | None
    # Field SHAPE only. The patterns are unordered: CROWDED_FIELD is not
    # worse than SPARSE_FIELD, and CONCENTRATED_FIELD is not a warning.
    pattern: str
    readings: CompetitionReadingsOut
    features: CompetitionFieldFeaturesOut | None
    provenance: CompetitionFieldProvenanceOut

    value_truth_class: str | None
    features_truth_class: str | None
    evidence_truth_basis: str | None

    # Permanent UNKNOWN markers: conclusions supply structure cannot support.
    saturation: str
    entry_difficulty: str
    win_probability: str
    differentiation_opportunity: str
    competitor_strength: str
    competitor_revenue: str
    market_share_available: str

    missing_reason: str | None
    limitations: list[str]
    dimension_name: str
    version: str
    pattern_version: str

    @classmethod
    def from_result(
        cls, r: CompetitionOpportunityResult
    ) -> "CompetitionOpportunityOut":
        return cls(
            candidate_id=r.candidate_id,
            state=r.state.value,
            value=r.value,
            pattern=r.pattern.value,
            readings=CompetitionReadingsOut.from_readings(r.readings),
            features=(
                CompetitionFieldFeaturesOut.from_features(r.features)
                if r.features is not None
                else None
            ),
            provenance=CompetitionFieldProvenanceOut.from_provenance(r.provenance),
            value_truth_class=(
                r.value_truth_class.value if r.value_truth_class else None
            ),
            features_truth_class=(
                r.features_truth_class.value if r.features_truth_class else None
            ),
            evidence_truth_basis=(
                r.evidence_truth_basis.value if r.evidence_truth_basis else None
            ),
            saturation=r.saturation.value,
            entry_difficulty=r.entry_difficulty.value,
            win_probability=r.win_probability.value,
            differentiation_opportunity=r.differentiation_opportunity.value,
            competitor_strength=r.competitor_strength.value,
            competitor_revenue=r.competitor_revenue.value,
            market_share_available=r.market_share_available.value,
            missing_reason=r.missing_reason,
            limitations=list(r.limitations),
            dimension_name=r.dimension_name,
            version=r.version,
            pattern_version=r.pattern_version,
        )


class AttentionClaimOut(BaseModel):
    """What a pattern observes, and the boundary of what it supports.

    Both halves are always present: a consumer cannot receive a pattern
    without also receiving what that pattern does not establish.
    """

    observes: str
    does_not_establish: str

    @classmethod
    def from_claim(cls, c: AttentionClaim) -> "AttentionClaimOut":
        return cls(observes=c.observes, does_not_establish=c.does_not_establish)


class AttentionFeaturesOut(BaseModel):
    """How observed attention was distributed. No total, by design.

    Nulls are UNKNOWN, never zero: a share of no observed attention has no
    denominator, and a video with no observed view count is not a video with
    zero views.
    """

    video_count: int
    videos_with_observed_views: int
    videos_with_unmeasured_views: int
    channels_in_sample: int
    # Not Milestone 4D's channel count: this one requires an OBSERVED view.
    channels_with_observed_attention: int

    median_views: float | None
    lower_quartile_views: float | None
    upper_quartile_views: float | None

    # The outlier is isolated rather than averaged away.
    top_video_attention_share: float | None
    median_views_excluding_top_video: float | None
    videos_covering_half_of_attention: int | None
    top_channel_attention_share: float | None

    # Consistency relative to THIS field's median. A uniformly ignored field
    # is perfectly consistent at a trivial level, so read these next to
    # median_views: they describe spread, never level.
    videos_within_band_of_median: int | None
    proportion_within_band_of_median: float | None

    videos_with_publish_date: int
    publish_span_days: int | None
    distinct_publish_months: int | None

    # Interaction with a video. Never willingness to pay.
    median_engagement_rate: float | None
    videos_with_engagement_rate: int
    features_version: str

    @classmethod
    def from_features(cls, f: AttentionFeatures) -> "AttentionFeaturesOut":
        return cls(
            video_count=f.video_count,
            videos_with_observed_views=f.videos_with_observed_views,
            videos_with_unmeasured_views=f.videos_with_unmeasured_views,
            channels_in_sample=f.channels_in_sample,
            channels_with_observed_attention=f.channels_with_observed_attention,
            median_views=f.median_views,
            lower_quartile_views=f.lower_quartile_views,
            upper_quartile_views=f.upper_quartile_views,
            top_video_attention_share=f.top_video_attention_share,
            median_views_excluding_top_video=f.median_views_excluding_top_video,
            videos_covering_half_of_attention=f.videos_covering_half_of_attention,
            top_channel_attention_share=f.top_channel_attention_share,
            videos_within_band_of_median=f.videos_within_band_of_median,
            proportion_within_band_of_median=f.proportion_within_band_of_median,
            videos_with_publish_date=f.videos_with_publish_date,
            publish_span_days=f.publish_span_days,
            distinct_publish_months=f.distinct_publish_months,
            median_engagement_rate=f.median_engagement_rate,
            videos_with_engagement_rate=f.videos_with_engagement_rate,
            features_version=f.features_version,
        )


class AttentionProvenanceOut(BaseModel):
    """Lineage, canonically ordered by Milestone 4F itself."""

    candidate_id: UUID
    evidence_ids: list[UUID]
    video_ids: list[str]
    channel_ids: list[str]
    providers: list[str]
    platforms: list[str]
    source_truth_classes: list[str]
    duplicate_evidence_suppressed: int

    @classmethod
    def from_provenance(cls, p: AttentionProvenance) -> "AttentionProvenanceOut":
        return cls(
            candidate_id=p.candidate_id,
            evidence_ids=list(p.evidence_ids),
            video_ids=list(p.video_ids),
            channel_ids=list(p.channel_ids),
            providers=list(p.providers),
            platforms=list(p.platforms),
            source_truth_classes=list(p.source_truth_classes),
            duplicate_evidence_suppressed=p.duplicate_evidence_suppressed,
        )


class AudienceAttentionOut(BaseModel):
    candidate_id: UUID
    state: str
    # Always null: a single number over views is monotone in views, which is
    # the reading this milestone exists to avoid.
    value: float | None
    # Distribution SHAPE only. No pattern is a level of demand.
    pattern: str
    claim: AttentionClaimOut
    features: AttentionFeaturesOut | None
    provenance: AttentionProvenanceOut

    value_truth_class: str | None
    features_truth_class: str | None
    evidence_truth_basis: str | None

    # Permanent UNKNOWN markers: conclusions attention cannot support.
    buyer_count: str
    purchase_intent: str
    candidate_audience_size: str
    demand_durability: str
    willingness_to_pay: str
    watch_time: str
    conversion_probability: str

    missing_reason: str | None
    limitations: list[str]
    dimension_name: str
    version: str
    pattern_version: str

    @classmethod
    def from_result(cls, r: AudienceAttentionResult) -> "AudienceAttentionOut":
        return cls(
            candidate_id=r.candidate_id,
            state=r.state.value,
            value=r.value,
            pattern=r.pattern.value,
            claim=AttentionClaimOut.from_claim(r.claim),
            features=(
                AttentionFeaturesOut.from_features(r.features)
                if r.features is not None
                else None
            ),
            provenance=AttentionProvenanceOut.from_provenance(r.provenance),
            value_truth_class=(
                r.value_truth_class.value if r.value_truth_class else None
            ),
            features_truth_class=(
                r.features_truth_class.value if r.features_truth_class else None
            ),
            evidence_truth_basis=(
                r.evidence_truth_basis.value if r.evidence_truth_basis else None
            ),
            buyer_count=r.buyer_count.value,
            purchase_intent=r.purchase_intent.value,
            candidate_audience_size=r.candidate_audience_size.value,
            demand_durability=r.demand_durability.value,
            willingness_to_pay=r.willingness_to_pay.value,
            watch_time=r.watch_time.value,
            conversion_probability=r.conversion_probability.value,
            missing_reason=r.missing_reason,
            limitations=list(r.limitations),
            dimension_name=r.dimension_name,
            version=r.version,
            pattern_version=r.pattern_version,
        )


class DerivationOutcomeOut(BaseModel):
    derivation: str
    status: str
    candidates_covered: int
    failure_reason: str | None

    @classmethod
    def from_outcome(cls, o: DerivationOutcome) -> "DerivationOutcomeOut":
        return cls(
            derivation=o.derivation,
            status=o.status,
            candidates_covered=o.candidates_covered,
            failure_reason=o.failure_reason,
        )


class PreliminaryResearchResponse(BaseModel):
    research_run_id: UUID
    candidate_count: int
    capabilities: list[CapabilityOutcomeOut]
    ranked: list[RankedCandidateOut]
    selected_candidate_ids: list[UUID]
    adjacent_rank_explanations: list[RankExplanationOut]
    criteria_order: list[str]
    # Milestone 4A: derived for the selected candidates only.
    purchase_evidence: list[PurchaseEvidenceOut]
    # Milestone 4B: derived for the selected candidates only.
    price_evidence: list[PriceEvidenceOut]
    # Milestone 4D: channel evidence for the selected candidates only.
    buyer_reach: list[BuyerReachOut]
    # Milestone 4E: competitive-field shape for the selected candidates only.
    competition_opportunity: list[CompetitionOpportunityOut]
    # Milestone 4F: attention distribution for the selected candidates only.
    audience_attention: list[AudienceAttentionOut]
    derivations: list[DerivationOutcomeOut]
    orchestration_version: str
    dimensions_version: str
    ranking_version: str


@router.post("/research/preliminary", response_model=PreliminaryResearchResponse)
async def research_preliminary(
    request: PreliminaryResearchRequest,
    store: ResearchStore = Depends(get_research_store),
) -> PreliminaryResearchResponse:
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

    # A capability whose provider cannot even be constructed (missing
    # credentials) is reported as unavailable; it never fails the run.
    unavailable: dict[str, str] = {}

    def build(registry: dict, key: str | None, capability: str):
        if key is None:
            return None
        provider_cls = registry.get(key)
        if provider_cls is None:
            raise HTTPException(
                status_code=422,
                detail=f"unknown {capability} provider '{key}'; available: {sorted(registry)}",
            )
        try:
            return provider_cls()
        except MissingCredentialsError as exc:
            unavailable[capability] = f"{type(exc).__name__}: {exc}"
            return None

    search_provider = build(
        SEARCH_DEMAND_PROVIDERS, request.search_demand_provider, CAPABILITY_SEARCH_DEMAND
    )
    marketplace_provider = build(
        MARKETPLACE_PROVIDERS, request.marketplace, CAPABILITY_MARKETPLACE
    )
    content_provider = build(
        PUBLIC_CONTENT_PROVIDERS, request.public_content_provider, CAPABILITY_PUBLIC_CONTENT
    )

    result = await run_preliminary_research(
        candidates=candidates,
        store=store,
        search_demand_provider=search_provider,
        marketplace_provider=marketplace_provider,
        public_content_provider=content_provider,
        research_run_id=research_run_id,
        location=request.location,
        language=request.language,
        search_demand_caps=CapabilityCaps(),
        marketplace_caps=CapabilityCaps(),
        public_content_caps=CapabilityCaps(),
        selection_size=request.selection_size,
        unavailable_capabilities=unavailable,
    )

    ranked = result.ranking.ranked
    explanations = [
        RankExplanationOut(
            higher_candidate_id=e.higher_candidate_id,
            lower_candidate_id=e.lower_candidate_id,
            deciding_criterion=e.deciding_criterion,
            higher_value=e.higher_value,
            lower_value=e.lower_value,
            reason=e.reason,
        )
        for e in (explain_pairwise(a, b) for a, b in zip(ranked, ranked[1:]))
    ]

    return PreliminaryResearchResponse(
        research_run_id=result.research_run_id,
        candidate_count=result.candidate_count,
        capabilities=[CapabilityOutcomeOut.from_outcome(o) for o in result.capabilities],
        ranked=[RankedCandidateOut.from_ranked(r) for r in ranked],
        selected_candidate_ids=result.selected_candidate_ids,
        adjacent_rank_explanations=explanations,
        criteria_order=list(result.ranking.criteria_order),
        purchase_evidence=[
            PurchaseEvidenceOut.from_result(result.purchase_evidence[r.candidate_id])
            for r in ranked
            if r.candidate_id in result.purchase_evidence
        ],
        price_evidence=[
            PriceEvidenceOut.from_result(result.price_evidence[r.candidate_id])
            for r in ranked
            if r.candidate_id in result.price_evidence
        ],
        buyer_reach=[
            BuyerReachOut.from_result(result.buyer_reach[r.candidate_id])
            for r in ranked
            if r.candidate_id in result.buyer_reach
        ],
        competition_opportunity=[
            CompetitionOpportunityOut.from_result(
                result.competition_opportunity[r.candidate_id]
            )
            for r in ranked
            if r.candidate_id in result.competition_opportunity
        ],
        audience_attention=[
            AudienceAttentionOut.from_result(result.audience_attention[r.candidate_id])
            for r in ranked
            if r.candidate_id in result.audience_attention
        ],
        derivations=[DerivationOutcomeOut.from_outcome(d) for d in result.derivations],
        orchestration_version=result.orchestration_version,
        dimensions_version=result.dimensions_version,
        ranking_version=result.ranking_version,
    )


# --------------------------------------------------------------------------
# REMOVED FROM THE API SURFACE: unapproved experimental scoring.
#
# `POST /score` used to call app.services.scoring, whose POS weights,
# Evidence Confidence weights, kill rules, and RED/YELLOW/GREEN thresholds
# are placeholder v0.1 values appearing in no approved repository
# specification. A caller could not tell that from the response, so the
# endpoint could present an unapproved classification as a product verdict.
#
# There is no configuration that turns it back on. This module no longer
# imports app.services.scoring at all, so the route cannot reach the legacy
# implementation even by mistake; tests/test_orchestration.py asserts that
# statically. The path still answers, with 410 Gone and an explanation,
# because a silent 404 would leave callers guessing why scoring vanished.
#
# The module itself is deliberately retained for reuse when the approved
# final scoring engine is specified. Nothing in this application depends on
# it, and no milestone consumes it.
# --------------------------------------------------------------------------

SCORING_REMOVED_DETAIL = (
    "POST /score has been removed. It exposed unapproved experimental "
    "scoring (placeholder POS weights, Evidence Confidence weights, kill "
    "rules, and RED/YELLOW/GREEN thresholds) that is not part of any "
    "approved specification, so its output was not a valid product result. "
    "There is no configuration that re-enables it. The Product Opportunity "
    "Score is not implemented yet. For deterministic preliminary triage of "
    "candidates, use POST /research/preliminary, which ranks without any "
    "final scoring."
)


@router.post("/score", deprecated=True, include_in_schema=False)
def score() -> None:
    """Permanently removed: unapproved scoring. Never returns a score.

    Takes no request body, returns no response model, and calls nothing —
    the only outcome is 410.
    """
    raise HTTPException(status_code=410, detail=SCORING_REMOVED_DETAIL)


# --------------------------------------------------------------------------
# Milestone 4C: product specification, on demand, for ONE candidate.
#
# Additive and opt-in. Nothing here runs during /research/preliminary: a
# specification is generated only when a caller selects a candidate and asks
# for one. It derives from evidence that already exists in the store; it
# makes no provider calls, calls no LLM, and produces no score.
# --------------------------------------------------------------------------


class ProductSpecificationRequest(BaseModel):
    """Ask for a specification for one candidate.

    Either supply `research_run_id` + `candidate_id` to use stored evidence,
    or supply `candidate` (with optional inline `evidence`) directly.
    """

    research_run_id: UUID | None = None
    candidate_id: UUID | None = None
    candidate: Candidate | None = None
    evidence: list[EvidenceItem] | None = None

    @model_validator(mode="after")
    def check_selection(self) -> "ProductSpecificationRequest":
        if self.candidate is None and (
            self.research_run_id is None or self.candidate_id is None
        ):
            raise ValueError(
                "supply either 'candidate', or both 'research_run_id' and 'candidate_id'"
            )
        return self


class SpecFieldOut(BaseModel):
    value: str | None
    claim_class: str
    basis: str
    evidence_ids: list[UUID]
    signal_types: list[str]

    @classmethod
    def from_field(cls, f: SpecField) -> "SpecFieldOut":
        return cls(
            value=f.value,
            claim_class=f.claim_class.value,
            basis=f.basis,
            evidence_ids=list(f.evidence_ids),
            signal_types=list(f.signal_types),
        )


class ProductModuleOut(BaseModel):
    name: str
    purpose: str
    claim_class: str


class FormatRecommendationOut(BaseModel):
    # The conceptually best format for the job. ALWAYS an assumption, and
    # possibly something this system cannot build.
    ideal_format: str | None
    ideal_claim_class: str
    outside_v1_build_capability: bool
    # What V1 can actually produce, from Milestone 1's approved formats.
    buildable_v1_format: str | None
    buildable_claim_class: str
    rationale: str
    driven_by_job: str
    observed_dominant_format: str | None
    diverges_from_observed_market: bool
    selection_version: str


class PriceEvidenceReferenceOut(BaseModel):
    """A citation of Milestone 4B. Never a price recommendation."""

    available: bool
    state: str | None
    currencies: list[str]
    observed_asking_bands: list[dict]
    free_listing_count: int | None
    price_evidence_version: str | None
    limitations: list[str]
    note: str


class EvidenceConflictOut(BaseModel):
    topic: str
    description: str
    resolution: str


class JobClassificationOut(BaseModel):
    job: str
    claim_class: str
    matched_tokens: list[str]
    scores: list[dict]
    basis: str
    evidence_ids: list[UUID]
    signal_types: list[str]
    taxonomy_version: str


class ProductSpecificationOut(BaseModel):
    candidate_id: UUID
    research_run_id: UUID | None
    state: str
    product_name: SpecFieldOut
    target_buyer: SpecFieldOut
    buyer_job: SpecFieldOut
    job_classification: JobClassificationOut
    format_recommendation: FormatRecommendationOut | None
    core_promise: SpecFieldOut
    structure: list[ProductModuleOut]
    required_assets: list[str]
    differentiation_opportunities: list[SpecFieldOut]
    observed_competitor_patterns: list[SpecFieldOut]
    price_evidence_reference: PriceEvidenceReferenceOut
    supporting_evidence_ids: list[UUID]
    assumptions: list[str]
    unknowns: list[str]
    conflicts: list[EvidenceConflictOut]
    limitations: list[str]
    insufficient_evidence: bool
    missing_reason: str | None
    version: str
    job_taxonomy_version: str
    format_selection_version: str

    @classmethod
    def from_specification(cls, s: ProductSpecification) -> "ProductSpecificationOut":
        rec = s.format_recommendation
        price = s.price_evidence_reference
        return cls(
            candidate_id=s.candidate_id,
            research_run_id=s.research_run_id,
            state=s.state.value,
            product_name=SpecFieldOut.from_field(s.product_name),
            target_buyer=SpecFieldOut.from_field(s.target_buyer),
            buyer_job=SpecFieldOut.from_field(s.buyer_job),
            job_classification=JobClassificationOut(
                job=s.job_classification.job.value,
                claim_class=s.job_classification.claim_class.value,
                matched_tokens=list(s.job_classification.matched_tokens),
                scores=[{"job": j, "matches": n} for j, n in s.job_classification.scores],
                basis=s.job_classification.basis,
                evidence_ids=list(s.job_classification.evidence_ids),
                signal_types=list(s.job_classification.signal_types),
                taxonomy_version=s.job_classification.taxonomy_version,
            ),
            format_recommendation=(
                None
                if rec is None
                else FormatRecommendationOut(
                    ideal_format=rec.ideal_format.value if rec.ideal_format else None,
                    ideal_claim_class=rec.ideal_claim_class.value,
                    outside_v1_build_capability=rec.outside_v1_build_capability,
                    buildable_v1_format=(
                        rec.buildable_v1_format.value if rec.buildable_v1_format else None
                    ),
                    buildable_claim_class=rec.buildable_claim_class.value,
                    rationale=rec.rationale,
                    driven_by_job=rec.driven_by_job.value,
                    observed_dominant_format=rec.observed_dominant_format,
                    diverges_from_observed_market=rec.diverges_from_observed_market,
                    selection_version=rec.selection_version,
                )
            ),
            core_promise=SpecFieldOut.from_field(s.core_promise),
            structure=[
                ProductModuleOut(
                    name=m.name, purpose=m.purpose, claim_class=m.claim_class.value
                )
                for m in s.structure
            ],
            required_assets=list(s.required_assets),
            differentiation_opportunities=[
                SpecFieldOut.from_field(f) for f in s.differentiation_opportunities
            ],
            observed_competitor_patterns=[
                SpecFieldOut.from_field(f) for f in s.observed_competitor_patterns
            ],
            price_evidence_reference=PriceEvidenceReferenceOut(
                available=price.available,
                state=price.state,
                currencies=list(price.currencies),
                observed_asking_bands=[
                    {
                        "currency": currency,
                        "paid_listing_count": count,
                        # Explicitly ASKING prices. Not transaction prices.
                        "min_paid_asking_price": low,
                        "median_asking_price": median,
                        "max_paid_asking_price": high,
                        "insufficient_evidence": insufficient,
                    }
                    for currency, count, low, median, high, insufficient in (
                        price.observed_asking_bands
                    )
                ],
                free_listing_count=price.free_listing_count,
                price_evidence_version=price.price_evidence_version,
                limitations=list(price.limitations),
                note=price.note,
            ),
            supporting_evidence_ids=list(s.supporting_evidence_ids),
            assumptions=list(s.assumptions),
            unknowns=list(s.unknowns),
            conflicts=[
                EvidenceConflictOut(
                    topic=c.topic, description=c.description, resolution=c.resolution
                )
                for c in s.conflicts
            ],
            limitations=list(s.limitations),
            insufficient_evidence=s.insufficient_evidence,
            missing_reason=s.missing_reason,
            version=s.version,
            job_taxonomy_version=s.job_taxonomy_version,
            format_selection_version=s.format_selection_version,
        )


class FitObservationOut(BaseModel):
    """One finding about the support chain, with its own claim class.

    `does_not_establish` is always present so no finding can be read as a
    commercial conclusion.
    """

    topic: str
    finding: str
    claim_class: str
    does_not_establish: str

    @classmethod
    def from_observation(cls, o: FitObservation) -> "FitObservationOut":
        return cls(
            topic=o.topic,
            finding=o.finding,
            claim_class=o.claim_class.value,
            does_not_establish=o.does_not_establish,
        )


class ProductJobFitFeaturesOut(BaseModel):
    """Structural properties of the support chain. No magnitudes anywhere.

    Counts here count CITED EVIDENCE RECORDS and TOKENS. None of them is a
    market size, an audience, or a demand figure.
    """

    job: str | None
    job_claim_class: str
    job_supported_by_observed_evidence: bool
    job_citation_count: int
    job_matched_token_count: int

    job_score_lead: int | None
    job_is_ambiguous: bool
    competing_jobs: list[str]

    ideal_format: str | None
    buildable_format: str | None
    outside_v1_build_capability: bool
    ideal_interaction_mode: str | None
    buildable_interaction_mode: str | None
    # Names which structural property survived substitution. Not a ranking,
    # and deliberately never a number.
    substitution_fidelity: str

    unknown_specification_fields: list[str]
    known_specification_field_count: int
    specification_insufficient_evidence: bool

    conflict_count: int
    conflict_topics: list[str]

    # Reported, never ranked: 4C follows the job rather than the market.
    diverges_from_observed_market: bool
    features_version: str

    @classmethod
    def from_features(cls, f: ProductJobFitFeatures) -> "ProductJobFitFeaturesOut":
        return cls(
            job=f.job,
            job_claim_class=f.job_claim_class.value,
            job_supported_by_observed_evidence=f.job_supported_by_observed_evidence,
            job_citation_count=f.job_citation_count,
            job_matched_token_count=f.job_matched_token_count,
            job_score_lead=f.job_score_lead,
            job_is_ambiguous=f.job_is_ambiguous,
            competing_jobs=list(f.competing_jobs),
            ideal_format=f.ideal_format,
            buildable_format=f.buildable_format,
            outside_v1_build_capability=f.outside_v1_build_capability,
            ideal_interaction_mode=f.ideal_interaction_mode,
            buildable_interaction_mode=f.buildable_interaction_mode,
            substitution_fidelity=f.substitution_fidelity.value,
            unknown_specification_fields=list(f.unknown_specification_fields),
            known_specification_field_count=f.known_specification_field_count,
            specification_insufficient_evidence=f.specification_insufficient_evidence,
            conflict_count=f.conflict_count,
            conflict_topics=list(f.conflict_topics),
            diverges_from_observed_market=f.diverges_from_observed_market,
            features_version=f.features_version,
        )


class ProductJobFitOut(BaseModel):
    candidate_id: UUID
    state: str
    # Always null: a number here would read as a probability of success,
    # which is exactly the claim this milestone refuses to make.
    value: float | None
    # Where the support chain first breaks. Not a ranking, and
    # ALIGNED_WITH_OBSERVED_JOB is not a recommendation.
    pattern: str
    pattern_boundary: str
    observations: list[FitObservationOut]
    features: ProductJobFitFeaturesOut | None

    # Capped against every 4C input; never stronger than its weakest link.
    fit_claim_class: str
    # Always INFERRED: a derivation that consumed generated text.
    assessment_truth_class: str

    # Permanent UNKNOWN markers. Structural suitability is not demand.
    sales_probability: str
    conversion_probability: str
    product_market_fit: str
    willingness_to_pay: str
    market_size: str
    expected_revenue: str
    usefulness_to_buyer: str
    buyer_demand_proven: str

    missing_reason: str | None
    limitations: list[str]
    dimension_name: str
    version: str
    pattern_version: str
    interaction_mode_version: str

    @classmethod
    def from_result(cls, r: ProductJobFitResult) -> "ProductJobFitOut":
        return cls(
            candidate_id=r.candidate_id,
            state=r.state.value,
            value=r.value,
            pattern=r.pattern.value,
            pattern_boundary=r.pattern_boundary,
            observations=[
                FitObservationOut.from_observation(o) for o in r.observations
            ],
            features=(
                ProductJobFitFeaturesOut.from_features(r.features)
                if r.features is not None
                else None
            ),
            fit_claim_class=r.fit_claim_class.value,
            assessment_truth_class=r.assessment_truth_class.value,
            sales_probability=r.sales_probability.value,
            conversion_probability=r.conversion_probability.value,
            product_market_fit=r.product_market_fit.value,
            willingness_to_pay=r.willingness_to_pay.value,
            market_size=r.market_size.value,
            expected_revenue=r.expected_revenue.value,
            usefulness_to_buyer=r.usefulness_to_buyer.value,
            buyer_demand_proven=r.buyer_demand_proven.value,
            missing_reason=r.missing_reason,
            limitations=list(r.limitations),
            dimension_name=r.dimension_name,
            version=r.version,
            pattern_version=r.pattern_version,
            interaction_mode_version=r.interaction_mode_version,
        )


class ProductSpecificationResponse(BaseModel):
    specification: ProductSpecificationOut
    # Milestone 4G: structural fit between the specification and the job its
    # own evidence supports. Never product-market fit.
    product_job_fit: ProductJobFitOut


@router.post("/product/specification", response_model=ProductSpecificationResponse)
def product_specification(
    request: ProductSpecificationRequest,
    store: ResearchStore = Depends(get_research_store),
) -> ProductSpecificationResponse:
    """Generate a product specification for one selected candidate.

    Deterministic: no provider calls, no LLM, no score, and no fabricated
    demand. Evidence that does not exist stays UNKNOWN or MISSING.
    """
    if request.candidate is not None:
        candidate = request.candidate
        evidence = list(request.evidence or [])
        # The run stamp is taken from the evidence itself, never from the
        # request: a caller must not be able to label a specification with a
        # research run its evidence did not come from.
        runs = {item.research_run_id for item in evidence if item.research_run_id}
        research_run_id = runs.pop() if len(runs) == 1 else None
    else:
        candidates = store.get_run_candidates(request.research_run_id)
        if candidates is None:
            raise HTTPException(
                status_code=404,
                detail=f"research run {request.research_run_id} not found",
            )
        match = [c for c in candidates if c.id == request.candidate_id]
        if not match:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"candidate {request.candidate_id} is not part of research run "
                    f"{request.research_run_id}"
                ),
            )
        candidate = match[0]
        # Scoped to the requested run: the store is append-only across runs,
        # so an unscoped read would mix runs and double-count any listing
        # observed in more than one of them.
        research_run_id = request.research_run_id
        evidence = store.evidence_for_candidate(candidate.id, research_run_id)

    # 4A/4B are re-derived from the same stored evidence so the specification
    # cites current values. Both are pure functions over existing records.
    price = extract_price_evidence(candidate.id, evidence)
    purchase = extract_purchase_evidence(candidate.id, evidence)

    try:
        specification = generate_product_specification(
            candidate=candidate,
            evidence=evidence,
            price_evidence=price,
            purchase_evidence=purchase,
            research_run_id=research_run_id,
        )
    except EvidenceOwnershipError as exc:
        # Inline evidence for a different candidate: a caller error, not a
        # data condition. Refuse rather than cite it.
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    # Milestone 4G assesses the specification just generated. It runs behind
    # its own boundary: a bug in the assessment degrades the assessment
    # alone, and must never take down an endpoint that already produced a
    # valid specification.
    try:
        fit = extract_product_job_fit(
            candidate_id=candidate.id, specification=specification
        )
    except Exception as exc:  # noqa: BLE001 - deliberate assessment boundary
        error_id = uuid4()
        logger.exception(
            "Unexpected error in product_job_fit assessment (error_id=%s)", error_id
        )
        fit = ProductJobFitResult(
            candidate_id=candidate.id,
            state=DimensionState.MISSING,
            value=None,
            pattern=FitPattern.ASSESSMENT_UNAVAILABLE,
            pattern_boundary=PATTERN_BOUNDARIES[FitPattern.ASSESSMENT_UNAVAILABLE],
            observations=(),
            features=None,
            fit_claim_class=SpecClaimClass.UNKNOWN,
            missing_reason=(
                f"{type(exc).__name__}: "
                f"{sanitize_error_message(safe_exception_message(exc))} "
                f"(error_id={error_id})"
            ),
            limitations=FIT_LIMITATIONS,
        )
    return ProductSpecificationResponse(
        specification=ProductSpecificationOut.from_specification(specification),
        product_job_fit=ProductJobFitOut.from_result(fit),
    )
