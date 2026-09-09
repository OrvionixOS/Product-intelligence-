"""YouTube public-content adapter (Milestone 3B).

Wraps the official YouTube Data API v3 (https://developers.google.com/youtube/v3):

- GET /search    (public video search by query; quota cost 100 units)
- GET /videos    (snippet + statistics + contentDetails for up to 50 ids; 1 unit)
- GET /channels  (public statistics for up to 50 channel ids; 1 unit)

Only public fields the API actually returns are mapped; hidden statistics
(disabled like counts, disabled comments, hidden subscriber counts) and any
absent field stay None — never zero-filled. Private metrics (watch time,
retention, impressions, CTR, subscriber conversion, sales, revenue) are not
available through this API and are never estimated.

The API key comes only from environment variables (see .env.example) and is
never logged, echoed in errors, included in returned data, or stored in
payloads. Quota is the cost model: every result reports the units consumed.
"""

import os
import re
from datetime import UTC, datetime
from typing import Any

import httpx

from app.providers.base import (
    ChannelStats,
    ChannelStatsResult,
    MissingCredentialsError,
    ProviderAuthError,
    ProviderError,
    ProviderRateLimitError,
    ProviderResponseError,
    ProviderTimeoutError,
    PublicContentProvider,
    PublicContentQueryResult,
    VideoObservation,
)

ENV_API_KEY = "YOUTUBE_API_KEY"
ENV_BASE_URL = "YOUTUBE_BASE_URL"

DEFAULT_BASE_URL = "https://www.googleapis.com/youtube/v3"
SEARCH_PATH = "/search"
VIDEOS_PATH = "/videos"
CHANNELS_PATH = "/channels"

# Official quota costs per https://developers.google.com/youtube/v3/determine_quota_cost
QUOTA_SEARCH = 100
QUOTA_VIDEOS = 1
QUOTA_CHANNELS = 1

# YouTube caps maxResults at 50 for these endpoints; stay lower by default.
DEFAULT_MAX_VIDEOS_PER_QUERY = 10
YOUTUBE_HARD_LIMIT_PER_PAGE = 50

_DURATION_RE = re.compile(
    r"^P(?:(?P<days>\d+)D)?(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?$"
)


def _opt_int(value: Any) -> int | None:
    """Parse ints the API returns either as numbers or as decimal strings."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _opt_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def parse_duration_seconds(value: Any) -> int | None:
    """Deterministically parse an ISO-8601 duration like PT1H2M3S."""
    if not isinstance(value, str):
        return None
    match = _DURATION_RE.match(value)
    if match is None:
        return None
    parts = {k: int(v) for k, v in match.groupdict().items() if v is not None}
    if not parts:
        return None
    return (
        parts.get("days", 0) * 86400
        + parts.get("hours", 0) * 3600
        + parts.get("minutes", 0) * 60
        + parts.get("seconds", 0)
    )


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def normalize_video(item: dict[str, Any], retrieved_at: datetime) -> VideoObservation | None:
    """Map one /videos item to a provider-agnostic record.

    Returns None when the entry has no usable video id. Statistics YouTube
    hides (likes disabled, comments disabled) are simply absent from the
    payload and stay None here.
    """
    video_id = _opt_str(item.get("id"))
    if video_id is None:
        return None
    snippet = item.get("snippet") if isinstance(item.get("snippet"), dict) else {}
    statistics = item.get("statistics") if isinstance(item.get("statistics"), dict) else {}
    content = item.get("contentDetails") if isinstance(item.get("contentDetails"), dict) else {}

    raw_tags = snippet.get("tags")
    tags = tuple(t for t in raw_tags if isinstance(t, str)) if isinstance(raw_tags, list) else ()

    return VideoObservation(
        video_id=video_id,
        title=_opt_str(snippet.get("title")),
        description=_opt_str(snippet.get("description")),
        published_at=_timestamp(snippet.get("publishedAt")),
        channel_id=_opt_str(snippet.get("channelId")),
        channel_title=_opt_str(snippet.get("channelTitle")),
        view_count=_opt_int(statistics.get("viewCount")),
        like_count=_opt_int(statistics.get("likeCount")),
        comment_count=_opt_int(statistics.get("commentCount")),
        duration_seconds=parse_duration_seconds(content.get("duration")),
        tags=tags,
        category=_opt_str(snippet.get("categoryId")),
        url=f"https://www.youtube.com/watch?v={video_id}",
        retrieved_at=retrieved_at,
    )


class YouTubeContentProvider(PublicContentProvider):
    name = "youtube"
    collection_method = "official_api"
    supports_channel_stats = True
    calls_per_search = 2  # /search + /videos
    calls_per_channel_stats = 1
    quota_units_per_search = QUOTA_SEARCH + QUOTA_VIDEOS  # search + stats batch
    quota_units_per_channel_stats = QUOTA_CHANNELS

    def __init__(
        self,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_seconds: float = 30.0,
        max_videos_per_query: int = DEFAULT_MAX_VIDEOS_PER_QUERY,
    ) -> None:
        api_key = os.environ.get(ENV_API_KEY, "").strip()
        if not api_key:
            raise MissingCredentialsError(f"YouTube credentials missing: set {ENV_API_KEY}")
        self._api_key = api_key
        self._base_url = os.environ.get(ENV_BASE_URL, DEFAULT_BASE_URL).rstrip("/")
        self._transport = transport
        self._timeout = timeout_seconds
        self.max_videos_per_query = min(max_videos_per_query, YOUTUBE_HARD_LIMIT_PER_PAGE)

    def __repr__(self) -> str:  # never expose the API key
        return f"YouTubeContentProvider(base_url={self._base_url!r})"

    async def _get(self, path: str, params: dict[str, Any], quota_units: int) -> dict[str, Any]:
        """One API GET. Errors are annotated with what the attempt consumed:
        a request YouTube processed (any HTTP status) is assumed charged its
        quota_units; a transport-level failure may never have reached the
        API, so its quota consumption is honestly unknown (None)."""
        request_params = dict(params)
        request_params["key"] = self._api_key
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport
            ) as client:
                response = await client.get(self._base_url + path, params=request_params)
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError("YouTube request timed out") from exc
        except httpx.HTTPError as exc:
            # Never include the URL: it carries the API key as a parameter.
            raise ProviderResponseError(f"YouTube transport error: {type(exc).__name__}") from exc

        try:
            self._raise_for_status(response)
            body = response.json()
        except ProviderError as exc:
            exc.quota_units_consumed = quota_units
            raise
        except ValueError as exc:
            raise ProviderResponseError(
                "YouTube returned a non-JSON response", quota_units_consumed=quota_units
            ) from exc
        if not isinstance(body, dict):
            raise ProviderResponseError(
                "YouTube returned an unexpected response shape",
                quota_units_consumed=quota_units,
            )
        return body

    @staticmethod
    def _error_reasons(response: httpx.Response) -> list[str]:
        try:
            body = response.json()
        except ValueError:
            return []
        errors = body.get("error", {}).get("errors") if isinstance(body, dict) else None
        if not isinstance(errors, list):
            return []
        return [e.get("reason") for e in errors if isinstance(e, dict) and e.get("reason")]

    @classmethod
    def _raise_for_status(cls, response: httpx.Response) -> None:
        if response.status_code < 400:
            return
        reasons = cls._error_reasons(response)
        # YouTube reports quota exhaustion as HTTP 403 with a reason code.
        if any(r in ("quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded") for r in reasons):
            raise ProviderRateLimitError(f"YouTube quota/rate limit reached ({reasons[0]})")
        if response.status_code == 429:
            raise ProviderRateLimitError("YouTube rate limit reached")
        if response.status_code in (401, 403) or "keyInvalid" in reasons:
            raise ProviderAuthError("YouTube rejected the configured API key")
        if response.status_code == 400 and reasons:
            raise ProviderResponseError(f"YouTube rejected the request ({reasons[0]})")
        raise ProviderResponseError(f"YouTube returned HTTP {response.status_code}")

    async def search_videos(self, query: str, limit: int) -> PublicContentQueryResult:
        """One search pass: /search for ids, then /videos for public stats.

        Consumes QUOTA_SEARCH + QUOTA_VIDEOS units in two HTTP calls.
        """
        page_limit = max(1, min(limit, self.max_videos_per_query))
        search_body = await self._get(
            SEARCH_PATH,
            params={"part": "snippet", "q": query, "type": "video", "maxResults": page_limit},
            quota_units=QUOTA_SEARCH,
        )
        errors: list[str] = []
        items = search_body.get("items")
        if not isinstance(items, list):
            raise ProviderResponseError("YouTube search response contained no items list")
        video_ids: list[str] = []
        for item in items:
            if not isinstance(item, dict):
                errors.append("malformed search entry in YouTube response")
                continue
            vid = item.get("id", {}).get("videoId") if isinstance(item.get("id"), dict) else None
            if isinstance(vid, str) and vid and vid not in video_ids:
                video_ids.append(vid)

        total_available = None
        page_info = search_body.get("pageInfo")
        if isinstance(page_info, dict):
            total_available = _opt_int(page_info.get("totalResults"))

        retrieved_at = datetime.now(UTC)
        videos: list[VideoObservation] = []
        call_count = 1
        quota_units = QUOTA_SEARCH
        if video_ids:
            try:
                videos_body = await self._get(
                    VIDEOS_PATH,
                    params={
                        "part": "snippet,statistics,contentDetails",
                        "id": ",".join(video_ids),
                        "maxResults": len(video_ids),
                    },
                    quota_units=QUOTA_VIDEOS,
                )
            except ProviderError as exc:
                # The successful /search call before this failure was charged.
                exc.calls_consumed = 2
                if exc.quota_units_consumed is not None:
                    exc.quota_units_consumed += QUOTA_SEARCH
                raise
            call_count += 1
            quota_units += QUOTA_VIDEOS
            retrieved_at = datetime.now(UTC)
            video_items = videos_body.get("items")
            if not isinstance(video_items, list):
                raise ProviderResponseError("YouTube videos response contained no items list")
            for item in video_items:
                if not isinstance(item, dict):
                    errors.append("malformed video entry in YouTube response")
                    continue
                normalized = normalize_video(item, retrieved_at)
                if normalized is not None:
                    videos.append(normalized)

        return PublicContentQueryResult(
            provider=self.name,
            query=query,
            videos=videos,
            retrieved_at=retrieved_at,
            collection_method=self.collection_method,
            total_available=total_available,
            source_reference=f"{SEARCH_PATH}+{VIDEOS_PATH}",
            call_count=call_count,
            quota_units=quota_units,
            errors=errors,
        )

    async def fetch_channel_stats(self, channel_ids: list[str]) -> ChannelStatsResult:
        """Fetch public statistics for up to 50 channels in one call (1 unit).

        Channels with hidden subscriber counts come back with that field
        absent or flagged hidden; it stays None — never zero.
        """
        if not channel_ids:
            return ChannelStatsResult(stats={}, call_count=0, quota_units=0)
        if len(channel_ids) > self.max_channels_per_stats_request:
            raise ProviderResponseError(
                f"batch of {len(channel_ids)} exceeds "
                f"max_channels_per_stats_request={self.max_channels_per_stats_request}"
            )
        body = await self._get(
            CHANNELS_PATH,
            params={
                "part": "statistics",
                "id": ",".join(channel_ids),
                "maxResults": len(channel_ids),
            },
            quota_units=QUOTA_CHANNELS,
        )
        retrieved_at = datetime.now(UTC)
        stats: dict[str, ChannelStats] = {}
        errors: list[str] = []
        items = body.get("items")
        if not isinstance(items, list):
            raise ProviderResponseError("YouTube channels response contained no items list")
        for item in items:
            if not isinstance(item, dict):
                errors.append("malformed channel entry in YouTube response")
                continue
            channel_id = _opt_str(item.get("id"))
            if channel_id is None:
                continue
            statistics = (
                item.get("statistics") if isinstance(item.get("statistics"), dict) else {}
            )
            hidden = statistics.get("hiddenSubscriberCount") is True
            stats[channel_id] = ChannelStats(
                channel_id=channel_id,
                subscriber_count=None if hidden else _opt_int(statistics.get("subscriberCount")),
                video_count=_opt_int(statistics.get("videoCount")),
                view_count=_opt_int(statistics.get("viewCount")),
                retrieved_at=retrieved_at,
            )

        return ChannelStatsResult(
            stats=stats, call_count=1, quota_units=QUOTA_CHANNELS, errors=errors
        )
