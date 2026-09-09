"""Etsy marketplace adapter (Milestone 3A).

Wraps the official Etsy Open API v3 (https://developers.etsy.com/documentation):

- GET /v3/application/listings/active   (public listing search by keywords)
- GET /v3/application/listings/{listing_id}/reviews   (listing review count)

No scraping. Only fields Etsy actually returns are mapped; anything absent
stays None. The API keystring comes only from environment variables (see
.env.example) and is never logged, echoed in errors, or included in returned
data. Public listing search needs the keystring only (no OAuth); OAuth 2.0 is
required solely for private seller data, which this milestone does not touch —
exact competitor sales/revenue therefore remain UNKNOWN by design.
"""

import os
from datetime import UTC, datetime
from typing import Any

import httpx

from app.providers.base import (
    ListingReviewStats,
    MarketplaceListing,
    MarketplaceProvider,
    MarketplaceQueryResult,
    MissingCredentialsError,
    ProviderAuthError,
    ProviderRateLimitError,
    ProviderResponseError,
    ProviderTimeoutError,
)

ENV_API_KEY = "ETSY_API_KEY"
ENV_BASE_URL = "ETSY_BASE_URL"

DEFAULT_BASE_URL = "https://openapi.etsy.com"
ACTIVE_LISTINGS_PATH = "/v3/application/listings/active"
LISTING_REVIEWS_PATH_TEMPLATE = "/v3/application/listings/{listing_id}/reviews"

# Etsy caps `limit` at 100 for listing search; stay lower by default so one
# query never returns an unbounded page.
DEFAULT_MAX_LISTINGS_PER_QUERY = 25
ETSY_HARD_LIMIT_PER_PAGE = 100


def _opt_int(value: Any) -> int | None:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _opt_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _money(value: Any) -> tuple[float | None, str | None]:
    """Map Etsy's Money object ({amount, divisor, currency_code}) to a float."""
    if not isinstance(value, dict):
        return None, None
    amount = _opt_int(value.get("amount"))
    divisor = _opt_int(value.get("divisor"))
    currency = _opt_str(value.get("currency_code"))
    if amount is None or divisor in (None, 0):
        return None, currency
    return round(amount / divisor, 2), currency


def _timestamp(value: Any) -> datetime | None:
    ts = _opt_int(value)
    if ts is None or ts <= 0:
        return None
    return datetime.fromtimestamp(ts, tz=UTC)


def _digital_indicator(listing_type: str | None) -> bool | None:
    """Derive a digital/download indicator only from Etsy's returned
    listing_type field ("physical" | "download" | "both"); otherwise None."""
    if listing_type == "download":
        return True
    if listing_type == "both":
        return True
    if listing_type == "physical":
        return False
    return None


def normalize_listing(item: dict[str, Any], retrieved_at: datetime) -> MarketplaceListing | None:
    """Map one Etsy ShopListing entry to a provider-agnostic record.

    Returns None when the entry has no usable listing_id. Etsy's listing
    search does not include review counts or ratings; those stay None here
    and may be filled by a separate capped review-stats lookup.
    """
    listing_id = _opt_int(item.get("listing_id"))
    if listing_id is None:
        return None

    price, currency = _money(item.get("price"))
    listing_type = _opt_str(item.get("listing_type"))
    taxonomy_id = _opt_int(item.get("taxonomy_id"))
    shop_id = _opt_int(item.get("shop_id"))
    created = _timestamp(item.get("original_creation_timestamp")) or _timestamp(
        item.get("created_timestamp")
    ) or _timestamp(item.get("creation_timestamp"))

    return MarketplaceListing(
        listing_id=str(listing_id),
        title=_opt_str(item.get("title")),
        url=_opt_str(item.get("url")),
        price=price,
        currency=currency,
        seller_id=str(shop_id) if shop_id is not None else None,
        rating=None,  # not returned by Etsy listing search; never invented
        review_count=None,  # filled only by an actual reviews lookup
        created_at=created,
        state=_opt_str(item.get("state")),
        taxonomy=str(taxonomy_id) if taxonomy_id is not None else None,
        listing_type=listing_type,
        is_digital=_digital_indicator(listing_type),
        retrieved_at=retrieved_at,
    )


class EtsyMarketplaceProvider(MarketplaceProvider):
    name = "etsy"
    collection_method = "official_api"
    supports_review_stats = True

    def __init__(
        self,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_seconds: float = 30.0,
        max_listings_per_query: int = DEFAULT_MAX_LISTINGS_PER_QUERY,
    ) -> None:
        api_key = os.environ.get(ENV_API_KEY, "").strip()
        if not api_key:
            raise MissingCredentialsError(f"Etsy credentials missing: set {ENV_API_KEY}")
        self._api_key = api_key
        self._base_url = os.environ.get(ENV_BASE_URL, DEFAULT_BASE_URL).rstrip("/")
        self._transport = transport
        self._timeout = timeout_seconds
        self.max_listings_per_query = min(max_listings_per_query, ETSY_HARD_LIMIT_PER_PAGE)

    def __repr__(self) -> str:  # never expose the API key
        return f"EtsyMarketplaceProvider(base_url={self._base_url!r})"

    async def _get(self, path: str, params: dict[str, Any]) -> httpx.Response:
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout,
                transport=self._transport,
                headers={"x-api-key": self._api_key},
            ) as client:
                return await client.get(self._base_url + path, params=params)
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError("Etsy request timed out") from exc
        except httpx.HTTPError as exc:
            raise ProviderResponseError(f"Etsy transport error: {type(exc).__name__}") from exc

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        if response.status_code in (401, 403):
            raise ProviderAuthError(
                "Etsy rejected the configured API key (check app approval status)"
            )
        if response.status_code == 429:
            raise ProviderRateLimitError("Etsy rate limit reached")
        if response.status_code >= 400:
            raise ProviderResponseError(f"Etsy returned HTTP {response.status_code}")

    async def search_listings(self, query: str, limit: int) -> MarketplaceQueryResult:
        page_limit = max(1, min(limit, self.max_listings_per_query))
        response = await self._get(
            ACTIVE_LISTINGS_PATH,
            params={"keywords": query, "limit": page_limit},
        )
        self._raise_for_status(response)

        try:
            body = response.json()
        except ValueError as exc:
            raise ProviderResponseError("Etsy returned a non-JSON response") from exc

        retrieved_at = datetime.now(UTC)
        errors: list[str] = []
        listings: list[MarketplaceListing] = []
        results = body.get("results")
        if not isinstance(results, list):
            raise ProviderResponseError("Etsy response contained no results list")
        for item in results:
            if not isinstance(item, dict):
                errors.append("malformed listing entry in Etsy response")
                continue
            normalized = normalize_listing(item, retrieved_at)
            if normalized is not None:
                listings.append(normalized)

        return MarketplaceQueryResult(
            provider=self.name,
            query=query,
            listings=listings,
            retrieved_at=retrieved_at,
            collection_method=self.collection_method,
            total_available=_opt_int(body.get("count")),
            source_reference=ACTIVE_LISTINGS_PATH,
            call_count=1,
            cost=None,  # Etsy's public API is quota-limited, not per-call priced
            cost_is_estimate=True,
            errors=errors,
        )

    async def fetch_review_stats(self, listing_id: str) -> ListingReviewStats:
        """Fetch the review count for one listing.

        Only the provider-reported total review count is used. The page of
        review bodies is a partial sample and is deliberately NOT turned into
        an average rating — a partial-sample rating would be an invented
        statistic. Review counts are purchase proxies, never sales counts.
        """
        path = LISTING_REVIEWS_PATH_TEMPLATE.format(listing_id=listing_id)
        response = await self._get(path, params={"limit": 1})
        if response.status_code == 404:
            # Listing has no review resource; report the absence, don't guess.
            return ListingReviewStats(
                listing_id=listing_id, review_count=None, retrieved_at=datetime.now(UTC)
            )
        self._raise_for_status(response)

        try:
            body = response.json()
        except ValueError as exc:
            raise ProviderResponseError("Etsy returned a non-JSON response") from exc

        return ListingReviewStats(
            listing_id=listing_id,
            review_count=_opt_int(body.get("count")),
            retrieved_at=datetime.now(UTC),
        )
