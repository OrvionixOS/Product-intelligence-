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
    PreliminaryDimension,
)
from app.services.preliminary_ranking import (
    DEEP_RESEARCH_SELECTION_SIZE,
    RankedCandidate,
    explain_pairwise,
)
from app.services.public_content import run_public_content_research
from app.services.public_content_features import ContentOutlier, PublicContentSummary
from app.services.research_orchestration import (
    CAPABILITY_MARKETPLACE,
    CAPABILITY_PUBLIC_CONTENT,
    CAPABILITY_SEARCH_DEMAND,
    CapabilityCaps,
    CapabilityOutcome,
    run_preliminary_research,
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


class PreliminaryResearchResponse(BaseModel):
    research_run_id: UUID
    candidate_count: int
    capabilities: list[CapabilityOutcomeOut]
    ranked: list[RankedCandidateOut]
    selected_candidate_ids: list[UUID]
    adjacent_rank_explanations: list[RankExplanationOut]
    criteria_order: list[str]
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
        orchestration_version=result.orchestration_version,
        dimensions_version=result.dimensions_version,
        ranking_version=result.ranking_version,
    )


@router.post("/score", response_model=ScoreResult)
def score(
    dimensions: ScoreDimensions,
    evidence: list[EvidenceItem],
) -> ScoreResult:
    return score_opportunity(dimensions, evidence)
