"""Deterministic candidate-level public-content feature extraction (3B).

Produces robust audience-interest features and a creator-relative content
outlier foundation from public video observations. This is NOT the
Opportunity Score, makes no GREEN/YELLOW/RED decision, and predicts nothing:

- View/like/comment counts are OBSERVED public metrics: audience interest
  in content, never watch time, retention, buyers, sales, or revenue.
- Ratios computed here (engagement rate, outlier ratio) are deterministic
  derivations from OBSERVED values and are labeled derived/INFERRED.
- Missing metrics are excluded from statistics and counted as missing —
  never zero-filled.

Versioned V1 heuristics, documented as explicit assumptions:

    audience_interest_dimension_v1:
        None when median views are unknown,
        0 when the median is 0,
        otherwise min(100, 100 * log10(median_views + 1) / 7)
        (log scale where ~10,000,000 median views saturates at 100).
        This mirrors search_demand_dimension_v1's shape; the saturation
        point is an assumption, not a validated threshold.

    content_outlier_v1 (creator-relative, no age term):
        For each channel with >= MIN_CHANNEL_SAMPLE collected videos that
        have observed views, baseline = median view count of that channel's
        collected videos; outlier_ratio = video_views / baseline.
        Ratios are reported for later analysis — nothing here is a "viral
        prediction", and no universal outlier threshold is imposed.
"""

import math
from dataclasses import dataclass, field
from datetime import UTC, datetime
from statistics import median
from uuid import UUID

from app.providers.base import VideoObservation

PUBLIC_CONTENT_FEATURES_VERSION = "public_content_features_v1"
AUDIENCE_INTEREST_DIMENSION_VERSION = "audience_interest_dimension_v1"
CONTENT_OUTLIER_VERSION = "content_outlier_v1"

_LOG10_SATURATION = 7.0  # 10**7 median views -> 100 (documented V1 assumption)

# Minimum videos a channel must contribute before a creator-relative
# baseline is computed; below this the baseline would be noise.
MIN_CHANNEL_SAMPLE = 3

# Presentation cap on reported outlier entries per candidate (largest ratios
# first); a bound on payload size, not an analytical threshold.
MAX_OUTLIERS_REPORTED = 25

RECENT_WINDOW_DAYS = 90


def audience_interest_dimension(median_views: float | None) -> float | None:
    """Versioned V1 heuristic mapping median views to 0-100. Deterministic."""
    if median_views is None:
        return None
    if median_views <= 0:
        return 0.0
    return round(min(100.0, 100.0 * math.log10(median_views + 1) / _LOG10_SATURATION), 2)


def engagement_rate(video: VideoObservation) -> float | None:
    """like_count / view_count, derived only when both are OBSERVED."""
    if video.like_count is None or video.view_count is None or video.view_count <= 0:
        return None
    return round(video.like_count / video.view_count, 6)


@dataclass(slots=True)
class ContentOutlier:
    """A video performing above its own channel's collected-sample baseline.

    A content-performance outlier observation — NOT a viral prediction.
    """

    video_id: str
    channel_id: str
    video_views: int
    channel_sample_size: int
    channel_baseline_median_views: float
    outlier_ratio: float
    formula_version: str = CONTENT_OUTLIER_VERSION


@dataclass(slots=True)
class PublicContentSummary:
    candidate_id: UUID
    relevant_video_count: int
    distinct_channel_count: int
    total_views: int | None
    median_views: float | None
    max_views: int | None
    median_likes: float | None
    median_comments: float | None
    # Derived (INFERRED) from observed like/view pairs; not provider-reported.
    median_engagement_rate: float | None
    median_days_since_publish: float | None
    videos_published_last_90_days: int
    missing_view_count: int = 0
    missing_like_count: int = 0
    missing_comment_count: int = 0
    channels_with_baseline: int = 0
    content_outliers: list[ContentOutlier] = field(default_factory=list)
    audience_interest_dimension: float | None = None
    dimension_version: str = AUDIENCE_INTEREST_DIMENSION_VERSION
    features_version: str = PUBLIC_CONTENT_FEATURES_VERSION


def extract_content_outliers(videos: list[VideoObservation]) -> tuple[list[ContentOutlier], int]:
    """content_outlier_v1: creator-relative ratios over the collected sample.

    Returns (outliers sorted by ratio descending, channels_with_baseline).
    Only channels contributing >= MIN_CHANNEL_SAMPLE videos with observed
    views get a baseline; everything else is reported as no-baseline rather
    than compared against an invented one. No age term is used.
    """
    by_channel: dict[str, list[VideoObservation]] = {}
    for video in videos:
        if video.channel_id is not None and video.view_count is not None:
            by_channel.setdefault(video.channel_id, []).append(video)

    outliers: list[ContentOutlier] = []
    channels_with_baseline = 0
    for channel_id, channel_videos in by_channel.items():
        if len(channel_videos) < MIN_CHANNEL_SAMPLE:
            continue
        channels_with_baseline += 1
        baseline = float(median(v.view_count for v in channel_videos))  # type: ignore[misc]
        if baseline <= 0:
            continue
        for video in channel_videos:
            outliers.append(
                ContentOutlier(
                    video_id=video.video_id,
                    channel_id=channel_id,
                    video_views=video.view_count,  # type: ignore[arg-type]
                    channel_sample_size=len(channel_videos),
                    channel_baseline_median_views=baseline,
                    outlier_ratio=round(video.view_count / baseline, 4),  # type: ignore[operator]
                )
            )

    outliers.sort(key=lambda o: (-o.outlier_ratio, o.video_id))
    return outliers[:MAX_OUTLIERS_REPORTED], channels_with_baseline


def summarize_public_content(
    candidate_id: UUID, videos: list[VideoObservation], now: datetime | None = None
) -> PublicContentSummary:
    """Deterministic summary of a candidate's public-content evidence.

    Videos with a missing metric are excluded from that metric's statistics
    and counted as missing — reported, never invented.
    """
    reference_time = now or datetime.now(UTC)
    views = [v.view_count for v in videos if v.view_count is not None]
    likes = [v.like_count for v in videos if v.like_count is not None]
    comments = [v.comment_count for v in videos if v.comment_count is not None]
    engagement_rates = [r for r in (engagement_rate(v) for v in videos) if r is not None]
    ages_days = [
        (reference_time - v.published_at).days for v in videos if v.published_at is not None
    ]
    recent = sum(1 for age in ages_days if 0 <= age <= RECENT_WINDOW_DAYS)
    channels = {v.channel_id for v in videos if v.channel_id is not None}

    median_views = float(median(views)) if views else None
    outliers, channels_with_baseline = extract_content_outliers(videos)

    return PublicContentSummary(
        candidate_id=candidate_id,
        relevant_video_count=len(videos),
        distinct_channel_count=len(channels),
        total_views=sum(views) if views else None,
        median_views=median_views,
        max_views=max(views) if views else None,
        median_likes=float(median(likes)) if likes else None,
        median_comments=float(median(comments)) if comments else None,
        median_engagement_rate=round(float(median(engagement_rates)), 6)
        if engagement_rates
        else None,
        median_days_since_publish=float(median(ages_days)) if ages_days else None,
        videos_published_last_90_days=recent,
        missing_view_count=sum(1 for v in videos if v.view_count is None),
        missing_like_count=sum(1 for v in videos if v.like_count is None),
        missing_comment_count=sum(1 for v in videos if v.comment_count is None),
        channels_with_baseline=channels_with_baseline,
        content_outliers=outliers,
        audience_interest_dimension=audience_interest_dimension(median_views),
    )
