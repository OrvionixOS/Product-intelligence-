"""Etsy official Open API v3 marketplace adapter.

Endpoints used (both are public v3 application endpoints authenticated with
the app keystring in the ``x-api-key`` header — no OAuth user grant needed):

- GET /v3/application/listings/active            (keyword search)
- GET /v3/application/listings/{id}/reviews      (review count + rating sample)

No scraping. Only fields the API actually returns are mapped; absent fields
stay None. The keystring comes only from the ETSY_API_KEY environment
variable and is never logged, echoed in errors, or included in returned data.

Note on access: Etsy issues a keystring per registered app. New apps start in
"personal" provisional access, which is sufficient for these endpoints at low
volume; "commercial" access requires Etsy's app-approval process. Rate limits
apply per key (per-second and per-day).
"""

import os
from datetime import UTC, datetime
from typing import Any

import httpx

from app.providers.base import (
    ListingReviewStats,
    MarketplaceListing,
    MarketplaceProvider,
    MarketplaceSearchResult,
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
LISTING_REVIEWS_PATH = "/v3/application/listings/{listing_id}/reviews"

# Etsy caps `limit` at 100 for both endpoints.
MAX_API_LIMIT = 100
DEFAULT_MAX_RESULTS_PER_QUERY = 25
REVIEW_SAMPLE_LIMIT = 100


def _opt_int(value: Any) -> int | None:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _opt_float(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _opt_str(value: Any) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None


def _epoch_to_datetime(value: Any) -> datetime | None:
    seconds = _opt_int(value)
    if seconds is None or seconds <= 0:
        return None
    return datetime.fromtimestamp(seconds, tz=UTC)


def _map_price(value: Any) -> tuple[float | None, str | None]:
    """Etsy v3 money object: {"amount": int, "divisor": int, "currency_code": str}."""
    if not isinstance(value, dict):
        return None, None
    amount = _opt_int(value.get("amount"))
    divisor = _opt_int(value.get("divisor"))
    currency = _opt_str(value.get("currency_code"))
    if amount is None or not divisor:
        return None, currency
    return round(amount / divisor, 2), currency


def _map_is_digital(value: Any) -> bool | None:
    """Etsy v3 listing type: "physical", "download", or "both"."""
    if value == "download" or value == "both":
        return True
    if value == "physical":
        return False
    return None


def normalize_listing(item: dict[str, Any], retrieved_at: datetime) -> MarketplaceListing | None:
    """Map one Etsy v3 listing object to a provider-agnostic record.

    Returns None when the entry has no usable listing_id. Review count and
    rating are NOT part of listing search responses and stay None here; they
    can only be attached via fetch_listing_reviews.
    """
    listing_id = _opt_int(item.get("listing_id"))
    if listing_id is None:
        return None

    price, currency = _map_price(item.get("price"))
    seller = _opt_int(item.get("shop_id"))
    if seller is None:
        seller = _opt_int(item.get("user_id"))
    taxonomy = _opt_int(item.get("taxonomy_id"))

    return MarketplaceListing(
        listing_id=str(listing_id),
        title=_opt_str(item.get("title")),
        source_reference=_opt_str(item.get("url")),
        price=price,
        currency=currency,
        seller_id=str(seller) if seller is not None else None,
        rating=None,
        rating_sample_size=None,
        review_count=None,
        created_at=_epoch_to_datetime(
            item.get("original_creation_timestamp") or item.get("creation_timestamp")
        ),
        state=_opt_str(item.get("state")),
        taxonomy=str(taxonomy) if taxonomy is not None else None,
        is_digital=_map_is_digital(item.get("listing_type") or item.get("type")),
        retrieved_at=retrieved_at,
    )


class EtsyMarketplaceProvider(MarketplaceProvider):
    name = "etsy"
    collection_method = "official_api"

    def __init__(
        self,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_seconds: float = 30.0,
        max_results_per_query: int = DEFAULT_MAX_RESULTS_PER_QUERY,
    ) -> None:
        api_key = os.environ.get(ENV_API_KEY, "").strip()
        if not api_key:
            raise MissingCredentialsError(
                f"Etsy credentials missing: set {ENV_API_KEY} (the app keystring)"
            )
        self._headers = {"x-api-key": api_key}
        self._base_url = os.environ.get(ENV_BASE_URL, DEFAULT_BASE_URL).rstrip("/")
        self._transport = transport
        self._timeout = timeout_seconds
        self.max_results_per_query = min(max_results_per_query, MAX_API_LIMIT)

    def __repr__(self) -> str:  # never expose the keystring
        return f"EtsyMarketplaceProvider(base_url={self._base_url!r})"

    async def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(
                headers=self._headers,
                timeout=self._timeout,
                transport=self._transport,
            ) as client:
                response = await client.get(self._base_url + path, params=params)
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError("Etsy request timed out") from exc
        except httpx.HTTPError as exc:
            raise ProviderResponseError(f"Etsy transport error: {type(exc).__name__}") from exc

        if response.status_code in (401, 403):
            raise ProviderAuthError(
                "Etsy rejected the configured API key (invalid, revoked, or app not approved)"
            )
        if response.status_code == 429:
            raise ProviderRateLimitError("Etsy rate limit reached")
        if response.status_code == 404:
            raise ProviderResponseError("Etsy resource not found")
        if response.status_code >= 400:
            raise ProviderResponseError(f"Etsy returned HTTP {response.status_code}")

        try:
            body = response.json()
        except ValueError as exc:
            raise ProviderResponseError("Etsy returned a non-JSON response") from exc
        if not isinstance(body, dict):
            raise ProviderResponseError("Etsy returned an unexpected response shape")
        return body

    async def search_listings(self, query: str, limit: int) -> MarketplaceSearchResult:
        limit = max(1, min(limit, self.max_results_per_query))
        body = await self._get(
            ACTIVE_LISTINGS_PATH, params={"keywords": query, "limit": limit}
        )

        retrieved_at = datetime.now(UTC)
        listings: list[MarketplaceListing] = []
        errors: list[str] = []
        results = body.get("results")
        if isinstance(results, list):
            for item in results:
                if isinstance(item, dict):
                    normalized = normalize_listing(item, retrieved_at)
                    if normalized is not None:
                        listings.append(normalized)
        else:
            errors.append("Etsy search response contained no results list")

        return MarketplaceSearchResult(
            provider=self.name,
            query=query,
            listings=listings,
            retrieved_at=retrieved_at,
            collection_method=self.collection_method,
            source_reference=ACTIVE_LISTINGS_PATH,
            call_count=1,
            cost=None,  # Etsy does not bill per call; rate limits apply instead
            errors=errors,
        )

    async def fetch_listing_reviews(self, listing_id: str) -> ListingReviewStats:
        body = await self._get(
            LISTING_REVIEWS_PATH.format(listing_id=listing_id),
            params={"limit": REVIEW_SAMPLE_LIMIT},
        )
        count = _opt_int(body.get("count"))
        ratings: list[int] = []
        results = body.get("results")
        if isinstance(results, list):
            for entry in results:
                if isinstance(entry, dict):
                    rating = _opt_int(entry.get("rating"))
                    if rating is not None:
                        ratings.append(rating)
        average = round(sum(ratings) / len(ratings), 2) if ratings else None
        return ListingReviewStats(
            listing_id=listing_id,
            review_count=count,
            average_rating=average,
            rating_sample_size=len(ratings),
        )
