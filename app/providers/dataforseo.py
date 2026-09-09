"""DataForSEO search-demand adapter.

Wraps the DataForSEO Google Ads search-volume live endpoint:
POST /v3/keywords_data/google_ads/search_volume/live

Credentials come only from environment variables (see .env.example) and are
never logged, echoed in errors, or included in returned data. Only fields the
provider actually returns are mapped; anything absent stays None.
"""

import os
from datetime import UTC, datetime
from typing import Any

import httpx

from app.providers.base import (
    KeywordDemandMetrics,
    MissingCredentialsError,
    MonthlySearchVolume,
    ProviderAuthError,
    ProviderRateLimitError,
    ProviderResponseError,
    ProviderTimeoutError,
    SearchDemandBatchResult,
    SearchDemandProvider,
)

ENV_LOGIN = "DATAFORSEO_LOGIN"
ENV_PASSWORD = "DATAFORSEO_PASSWORD"
ENV_BASE_URL = "DATAFORSEO_BASE_URL"

DEFAULT_BASE_URL = "https://api.dataforseo.com"
SEARCH_VOLUME_PATH = "/v3/keywords_data/google_ads/search_volume/live"

# DataForSEO accepts up to 1000 keywords per search_volume task; stay lower to
# keep individual requests predictable.
DEFAULT_MAX_KEYWORDS_PER_REQUEST = 200

_TASK_OK_STATUS = 20000

# Minimal ISO-3166 alpha-2 -> DataForSEO location_name map for common markets.
# Anything not listed is passed through as a location_name verbatim.
_LOCATION_NAMES = {
    "US": "United States",
    "GB": "United Kingdom",
    "CA": "Canada",
    "AU": "Australia",
    "DE": "Germany",
    "FR": "France",
    "ES": "Spain",
    "IT": "Italy",
    "NL": "Netherlands",
}


def resolve_location_name(location: str) -> str:
    key = location.strip()
    if len(key) == 2:
        return _LOCATION_NAMES.get(key.upper(), key)
    return key


def _opt_int(value: Any) -> int | None:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _opt_float(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _opt_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def normalize_result_item(
    item: dict[str, Any], retrieved_at: datetime
) -> KeywordDemandMetrics | None:
    """Map one DataForSEO result entry to provider-agnostic metrics.

    Returns None when the entry has no usable keyword.
    """
    keyword = _opt_str(item.get("keyword"))
    if keyword is None:
        return None

    history: list[MonthlySearchVolume] = []
    monthly = item.get("monthly_searches")
    if isinstance(monthly, list):
        for entry in monthly:
            if isinstance(entry, dict):
                history.append(
                    MonthlySearchVolume(
                        year=_opt_int(entry.get("year")),
                        month=_opt_int(entry.get("month")),
                        search_volume=_opt_int(entry.get("search_volume")),
                    )
                )

    location = item.get("location_code")
    return KeywordDemandMetrics(
        keyword=keyword.strip().lower(),
        search_volume=_opt_int(item.get("search_volume")),
        monthly_history=tuple(history),
        competition=_opt_str(item.get("competition")),
        competition_index=_opt_int(item.get("competition_index")),
        cpc=_opt_float(item.get("cpc")),
        low_top_of_page_bid=_opt_float(item.get("low_top_of_page_bid")),
        high_top_of_page_bid=_opt_float(item.get("high_top_of_page_bid")),
        location=str(location) if location is not None else None,
        language=_opt_str(item.get("language_code")),
        retrieved_at=retrieved_at,
    )


class DataForSeoSearchDemandProvider(SearchDemandProvider):
    name = "dataforseo"
    collection_method = "official_api"

    def __init__(
        self,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_seconds: float = 60.0,
        max_keywords_per_request: int = DEFAULT_MAX_KEYWORDS_PER_REQUEST,
    ) -> None:
        login = os.environ.get(ENV_LOGIN, "").strip()
        password = os.environ.get(ENV_PASSWORD, "").strip()
        if not login or not password:
            raise MissingCredentialsError(
                f"DataForSEO credentials missing: set {ENV_LOGIN} and {ENV_PASSWORD}"
            )
        self._auth = httpx.BasicAuth(login, password)
        self._base_url = os.environ.get(ENV_BASE_URL, DEFAULT_BASE_URL).rstrip("/")
        self._transport = transport
        self._timeout = timeout_seconds
        self.max_keywords_per_request = max_keywords_per_request

    def __repr__(self) -> str:  # never expose auth material
        return f"DataForSeoSearchDemandProvider(base_url={self._base_url!r})"

    async def fetch_keyword_metrics(
        self, keywords: list[str], location: str, language: str
    ) -> SearchDemandBatchResult:
        if not keywords:
            return SearchDemandBatchResult(
                provider=self.name,
                metrics=[],
                retrieved_at=datetime.now(UTC),
                collection_method=self.collection_method,
            )
        if len(keywords) > self.max_keywords_per_request:
            raise ProviderResponseError(
                f"batch of {len(keywords)} exceeds max_keywords_per_request={self.max_keywords_per_request}"
            )

        payload = [
            {
                "keywords": keywords,
                "location_name": resolve_location_name(location),
                "language_code": language,
            }
        ]

        try:
            async with httpx.AsyncClient(
                auth=self._auth,
                timeout=self._timeout,
                transport=self._transport,
            ) as client:
                response = await client.post(self._base_url + SEARCH_VOLUME_PATH, json=payload)
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError("DataForSEO request timed out") from exc
        except httpx.HTTPError as exc:
            raise ProviderResponseError(f"DataForSEO transport error: {type(exc).__name__}") from exc

        if response.status_code in (401, 403):
            raise ProviderAuthError("DataForSEO rejected the configured credentials")
        if response.status_code == 429:
            raise ProviderRateLimitError("DataForSEO rate limit reached")
        if response.status_code >= 400:
            raise ProviderResponseError(f"DataForSEO returned HTTP {response.status_code}")

        try:
            body = response.json()
        except ValueError as exc:
            raise ProviderResponseError("DataForSEO returned a non-JSON response") from exc

        retrieved_at = datetime.now(UTC)
        metrics: list[KeywordDemandMetrics] = []
        errors: list[str] = []
        cost = _opt_float(body.get("cost"))
        provider_version = _opt_str(body.get("version"))

        tasks = body.get("tasks")
        if not isinstance(tasks, list) or not tasks:
            raise ProviderResponseError("DataForSEO response contained no tasks")

        for task in tasks:
            if not isinstance(task, dict):
                errors.append("malformed task entry in DataForSEO response")
                continue
            status_code = task.get("status_code")
            if status_code != _TASK_OK_STATUS:
                errors.append(
                    f"DataForSEO task failed: status_code={status_code} "
                    f"status_message={_opt_str(task.get('status_message')) or 'unknown'}"
                )
                continue
            result = task.get("result")
            if not isinstance(result, list):
                errors.append("DataForSEO task succeeded but returned no result list")
                continue
            for item in result:
                if isinstance(item, dict):
                    normalized = normalize_result_item(item, retrieved_at)
                    if normalized is not None:
                        metrics.append(normalized)

        return SearchDemandBatchResult(
            provider=self.name,
            metrics=metrics,
            retrieved_at=retrieved_at,
            collection_method=self.collection_method,
            source_reference=SEARCH_VOLUME_PATH,
            provider_version=provider_version,
            call_count=1,
            cost=cost,
            cost_is_estimate=cost is None,
            errors=errors,
        )
