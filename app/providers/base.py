from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


class ProviderError(Exception):
    """Base class for research-provider failures.

    Messages must never contain credentials.
    """


class MissingCredentialsError(ProviderError):
    pass


class ProviderAuthError(ProviderError):
    pass


class ProviderRateLimitError(ProviderError):
    pass


class ProviderTimeoutError(ProviderError):
    pass


class ProviderResponseError(ProviderError):
    pass


class BudgetExhaustedError(ProviderError):
    pass


@dataclass(slots=True, frozen=True)
class MonthlySearchVolume:
    year: int | None
    month: int | None
    search_volume: int | None


@dataclass(slots=True, frozen=True)
class KeywordDemandMetrics:
    """Provider-agnostic search-demand measurement for one keyword.

    Search demand is search interest only. It is NOT sales, buyers, revenue,
    purchase intent, or validation. Fields the provider did not return are
    None — never invented.
    """

    keyword: str
    search_volume: int | None = None
    monthly_history: tuple[MonthlySearchVolume, ...] = ()
    competition: str | None = None
    competition_index: int | None = None
    cpc: float | None = None
    low_top_of_page_bid: float | None = None
    high_top_of_page_bid: float | None = None
    location: str | None = None
    language: str | None = None
    retrieved_at: datetime | None = None


@dataclass(slots=True)
class SearchDemandBatchResult:
    """Result of one provider research pass over a batch of keywords."""

    provider: str
    metrics: list[KeywordDemandMetrics]
    retrieved_at: datetime
    collection_method: str
    source_reference: str | None = None
    provider_version: str | None = None
    call_count: int = 0
    cost: float | None = None
    cost_is_estimate: bool = True
    errors: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ProviderEnvelope:
    provider: str
    provider_version: str | None
    retrieved_at: str
    collection_method: str
    geography: str | None
    language: str | None
    source_reference: str | None
    raw_payload: dict[str, Any]
    limitations: list[str]


class SearchDemandProvider(ABC):
    """Batch search-demand research contract.

    Implementations translate their own response shapes into
    KeywordDemandMetrics; the domain layer never sees provider payloads.
    Designed so DataForSEO can be swapped for Google Ads API later.
    """

    name: str = "unknown"
    max_keywords_per_request: int = 100

    @abstractmethod
    async def fetch_keyword_metrics(
        self, keywords: list[str], location: str, language: str
    ) -> SearchDemandBatchResult:
        """Fetch demand metrics for up to max_keywords_per_request keywords."""
        raise NotImplementedError


@dataclass(slots=True, frozen=True)
class MarketplaceListing:
    """Provider-agnostic public marketplace listing observation.

    Every optional field is None unless the provider actually returned it —
    absent data is never invented. Review counts and favorites are purchase
    PROXIES only: they are never sales counts, units sold, or revenue. Exact
    competitor sales/revenue are not public and stay UNKNOWN.
    """

    listing_id: str
    title: str | None = None
    url: str | None = None
    price: float | None = None
    currency: str | None = None
    seller_id: str | None = None
    rating: float | None = None
    review_count: int | None = None
    created_at: datetime | None = None
    state: str | None = None
    taxonomy: str | None = None
    listing_type: str | None = None
    is_digital: bool | None = None
    retrieved_at: datetime | None = None


@dataclass(slots=True)
class MarketplaceQueryResult:
    """Result of one provider search pass for a single marketplace query."""

    provider: str
    query: str
    listings: list[MarketplaceListing]
    retrieved_at: datetime
    collection_method: str
    total_available: int | None = None
    source_reference: str | None = None
    provider_version: str | None = None
    call_count: int = 0
    cost: float | None = None
    cost_is_estimate: bool = True
    errors: list[str] = field(default_factory=list)


@dataclass(slots=True, frozen=True)
class ListingReviewStats:
    """Review statistics for one listing, as reported by the provider.

    review_count is a purchase proxy, never a sales count.
    """

    listing_id: str
    review_count: int | None
    retrieved_at: datetime
    call_count: int = 1


class MarketplaceProvider(ABC):
    """Batch marketplace research contract (mirrors SearchDemandProvider).

    Implementations translate their own response shapes into
    MarketplaceListing records; the domain layer never sees provider
    payloads. Designed so Etsy can be joined by other marketplaces later.
    """

    name: str = "unknown"
    collection_method: str = "official_api"
    max_listings_per_query: int = 25
    supports_review_stats: bool = False

    @abstractmethod
    async def search_listings(self, query: str, limit: int) -> MarketplaceQueryResult:
        """Search public listings for one normalized query."""
        raise NotImplementedError

    async def fetch_review_stats(self, listing_id: str) -> ListingReviewStats:
        """Fetch review stats for one listing. Optional capability."""
        raise NotImplementedError(f"{self.name} does not support review stats")


class PublicContentProvider(ABC):
    @abstractmethod
    async def research(self, query: str, geography: str, language: str) -> ProviderEnvelope:
        raise NotImplementedError
