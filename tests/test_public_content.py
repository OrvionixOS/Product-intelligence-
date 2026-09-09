"""Milestone 3B tests. All YouTube traffic goes through httpx.MockTransport or
fake providers — no automated test ever makes a real YouTube API request."""

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api import routes
from app.domain.enums import EvidencePurpose, ProductFormat, SnapshotStatus, TruthClass
from app.domain.models import Candidate
from app.main import app
from app.providers.base import (
    ChannelStats,
    ChannelStatsResult,
    MissingCredentialsError,
    ProviderAuthError,
    ProviderRateLimitError,
    ProviderResponseError,
    PublicContentProvider,
    PublicContentQueryResult,
    VideoObservation,
)
from app.providers.youtube import (
    ENV_API_KEY,
    QUOTA_CHANNELS,
    QUOTA_SEARCH,
    QUOTA_VIDEOS,
    YouTubeContentProvider,
    normalize_video,
    parse_duration_seconds,
)
from app.services.public_content import (
    MISSING_BUDGET,
    MISSING_PROVIDER_ERROR,
    MISSING_QUERY_CAP,
    MISSING_QUOTA,
    plan_content_queries,
    run_public_content_research,
)
from app.services.public_content_features import (
    MIN_CHANNEL_SAMPLE,
    extract_content_outliers,
    summarize_public_content,
)
from app.storage.memory import ImmutableEvidenceError, ResearchStore

client = TestClient(app)

FAKE_KEY = "test-youtube-key-not-a-real-secret"


def make_candidate(content_queries: list[str], title: str = "Sourdough Guide") -> Candidate:
    return Candidate(
        seed_keyword="sourdough baking",
        title=title,
        problem="A problem statement.",
        target_buyer="A buyer",
        proposed_format=ProductFormat.PDF_GUIDE,
        buyer_outcome="An outcome",
        search_queries=["s"],
        marketplace_queries=["m"],
        content_queries=content_queries,
        generation_reason="A hypothesis",
    )


def video_for(
    video_id: str,
    views: int | None = 1000,
    likes: int | None = 100,
    comments: int | None = 10,
    channel_id: str | None = "ch-1",
    published_days_ago: int | None = 30,
    **overrides: Any,
) -> VideoObservation:
    fields: dict[str, Any] = dict(
        video_id=video_id,
        title=f"Video {video_id}",
        channel_id=channel_id,
        channel_title=f"Channel {channel_id}",
        view_count=views,
        like_count=likes,
        comment_count=comments,
        duration_seconds=300,
        published_at=(datetime.now(UTC) - timedelta(days=published_days_ago))
        if published_days_ago is not None
        else None,
        url=f"https://www.youtube.com/watch?v={video_id}",
        retrieved_at=datetime.now(UTC),
    )
    fields.update(overrides)
    return VideoObservation(**fields)


class FakeContentProvider(PublicContentProvider):
    """Deterministic in-memory provider; records calls, never touches network."""

    name = "fake-content"
    collection_method = "official_api"
    supports_channel_stats = True
    quota_units_per_search = QUOTA_SEARCH + QUOTA_VIDEOS
    quota_units_per_channel_stats = QUOTA_CHANNELS

    def __init__(
        self,
        videos_by_query: dict[str, list[VideoObservation]],
        channel_stats: dict[str, ChannelStats] | None = None,
        fail_queries: set[str] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.videos_by_query = videos_by_query
        self.channel_stats = channel_stats or {}
        self.fail_queries = fail_queries or set()
        self.error = error
        self.search_calls: list[str] = []
        self.channel_calls: list[list[str]] = []

    async def search_videos(self, query: str, limit: int) -> PublicContentQueryResult:
        self.search_calls.append(query)
        if query in self.fail_queries:
            raise self.error or ProviderResponseError("simulated failure")
        videos = self.videos_by_query.get(query, [])[:limit]
        return PublicContentQueryResult(
            provider=self.name,
            query=query,
            videos=videos,
            retrieved_at=datetime.now(UTC),
            collection_method=self.collection_method,
            source_reference="/fake/search",
            call_count=2,
            quota_units=self.quota_units_per_search,
        )

    async def fetch_channel_stats(self, channel_ids: list[str]) -> ChannelStatsResult:
        self.channel_calls.append(list(channel_ids))
        return ChannelStatsResult(
            stats={cid: self.channel_stats[cid] for cid in channel_ids if cid in self.channel_stats},
            call_count=1,
            quota_units=QUOTA_CHANNELS,
        )


# --------------------------------------------------------------- query planning


def test_plan_content_queries_deduplicates_and_keeps_mapping():
    c1 = make_candidate(["Sourdough  Tips", "sourdough tips", "starter recipe"])
    c2 = make_candidate(["SOURDOUGH TIPS", "banneton review"], title="Other")
    plan = plan_content_queries([c1, c2])
    assert plan.queries == ["sourdough tips", "starter recipe", "banneton review"]
    assert plan.query_to_candidates["sourdough tips"] == [c1.id, c2.id]
    assert plan.query_to_candidates["banneton review"] == [c2.id]


# ----------------------------------------------------- YouTube normalization


def youtube_video_payload(video_id: str = "vid111", **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": video_id,
        "snippet": {
            "title": "Sourdough for Beginners",
            "description": "A full walkthrough.",
            "publishedAt": "2026-06-01T12:00:00Z",
            "channelId": "UCabc",
            "channelTitle": "Bread Channel",
            "tags": ["sourdough", "baking"],
            "categoryId": "26",
        },
        "statistics": {"viewCount": "125000", "likeCount": "4300", "commentCount": "210"},
        "contentDetails": {"duration": "PT12M30S"},
    }
    payload.update(overrides)
    return payload


def make_youtube_provider(monkeypatch, handler) -> YouTubeContentProvider:
    monkeypatch.setenv(ENV_API_KEY, FAKE_KEY)
    return YouTubeContentProvider(transport=httpx.MockTransport(handler))


def search_and_videos_handler(video_payloads: list[dict[str, Any]], channels: list[dict] | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["key"] == FAKE_KEY
        if request.url.path.endswith("/search"):
            items = [
                {"id": {"videoId": p["id"]}, "snippet": p.get("snippet", {})}
                for p in video_payloads
            ]
            return httpx.Response(
                200, json={"items": items, "pageInfo": {"totalResults": 98765}}
            )
        if request.url.path.endswith("/videos"):
            return httpx.Response(200, json={"items": video_payloads})
        if request.url.path.endswith("/channels"):
            return httpx.Response(200, json={"items": channels or []})
        raise AssertionError(f"unexpected path {request.url.path}")

    return handler


async def test_youtube_normalizes_returned_fields(monkeypatch):
    provider = make_youtube_provider(
        monkeypatch, search_and_videos_handler([youtube_video_payload()])
    )
    result = await provider.search_videos("sourdough for beginners", limit=5)

    assert result.total_available == 98765
    assert result.call_count == 2
    assert result.quota_units == QUOTA_SEARCH + QUOTA_VIDEOS
    assert len(result.videos) == 1
    video = result.videos[0]
    assert video.video_id == "vid111"
    assert video.title == "Sourdough for Beginners"
    assert video.view_count == 125000  # parsed from the API's string value
    assert video.like_count == 4300
    assert video.comment_count == 210
    assert video.duration_seconds == 750
    assert video.channel_id == "UCabc"
    assert video.channel_title == "Bread Channel"
    assert video.tags == ("sourdough", "baking")
    assert video.category == "26"
    assert video.published_at is not None and video.published_at.tzinfo is not None
    assert video.url == "https://www.youtube.com/watch?v=vid111"
    # Channel stats are not part of video search; they stay None here.
    assert video.channel_subscriber_count is None


def test_duration_parsing_deterministic():
    assert parse_duration_seconds("PT12M30S") == 750
    assert parse_duration_seconds("PT1H2M3S") == 3723
    assert parse_duration_seconds("P1DT1S") == 86401
    assert parse_duration_seconds("PT45S") == 45
    assert parse_duration_seconds("nonsense") is None
    assert parse_duration_seconds(None) is None


def test_youtube_missing_and_hidden_statistics_stay_none():
    # Likes hidden and comments disabled: fields absent from statistics.
    payload = youtube_video_payload(statistics={"viewCount": "500"})
    video = normalize_video(payload, datetime.now(UTC))
    assert video is not None
    assert video.view_count == 500
    assert video.like_count is None
    assert video.comment_count is None
    # Entirely missing statistics block.
    minimal = normalize_video({"id": "v9"}, datetime.now(UTC))
    assert minimal is not None
    for name in ("title", "description", "published_at", "channel_id", "view_count",
                 "like_count", "comment_count", "duration_seconds", "category"):
        assert getattr(minimal, name) is None
    assert minimal.tags == ()
    # No usable id -> no record, not a fabricated one.
    assert normalize_video({"snippet": {"title": "no id"}}, datetime.now(UTC)) is None


async def test_youtube_channel_stats_hidden_subscribers(monkeypatch):
    channels = [
        {"id": "UCabc", "statistics": {"subscriberCount": "50000", "videoCount": "120", "viewCount": "9000000"}},
        {"id": "UChidden", "statistics": {"hiddenSubscriberCount": True, "subscriberCount": "1", "videoCount": "5"}},
    ]
    provider = make_youtube_provider(monkeypatch, search_and_videos_handler([], channels))
    result = await provider.fetch_channel_stats(["UCabc", "UChidden"])
    assert result.quota_units == QUOTA_CHANNELS
    assert result.stats["UCabc"].subscriber_count == 50000
    assert result.stats["UCabc"].video_count == 120
    # Hidden subscriber counts are never surfaced, even if a value leaks.
    assert result.stats["UChidden"].subscriber_count is None
    assert result.stats["UChidden"].video_count == 5


async def test_youtube_quota_and_auth_error_mapping(monkeypatch):
    def quota_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403, json={"error": {"errors": [{"reason": "quotaExceeded"}]}}
        )

    provider = make_youtube_provider(monkeypatch, quota_handler)
    with pytest.raises(ProviderRateLimitError) as quota_exc:
        await provider.search_videos("q", limit=5)
    assert FAKE_KEY not in str(quota_exc.value)

    def auth_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"errors": [{"reason": "keyInvalid"}]}})

    provider = make_youtube_provider(monkeypatch, auth_handler)
    with pytest.raises(ProviderAuthError) as auth_exc:
        await provider.search_videos("q", limit=5)
    assert FAKE_KEY not in str(auth_exc.value)

    provider = make_youtube_provider(monkeypatch, lambda r: httpx.Response(403, json={}))
    with pytest.raises(ProviderAuthError):
        await provider.search_videos("q", limit=5)

    provider = make_youtube_provider(monkeypatch, lambda r: httpx.Response(500, json={}))
    with pytest.raises(ProviderResponseError) as server_exc:
        await provider.search_videos("q", limit=5)
    assert FAKE_KEY not in str(server_exc.value)


def test_youtube_missing_credentials_raise(monkeypatch):
    monkeypatch.delenv(ENV_API_KEY, raising=False)
    with pytest.raises(MissingCredentialsError):
        YouTubeContentProvider()


def test_youtube_secret_never_exposed(monkeypatch):
    monkeypatch.setenv(ENV_API_KEY, FAKE_KEY)
    provider = YouTubeContentProvider()
    assert FAKE_KEY not in repr(provider)
    assert FAKE_KEY not in str(provider)


async def test_youtube_respects_page_limit(monkeypatch):
    seen_params = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/search"):
            seen_params["maxResults"] = request.url.params["maxResults"]
            return httpx.Response(200, json={"items": []})
        raise AssertionError("videos should not be fetched with no ids")

    provider = make_youtube_provider(monkeypatch, handler)
    result = await provider.search_videos("q", limit=999)
    assert seen_params["maxResults"] == str(provider.max_videos_per_query)
    assert result.videos == []
    assert result.call_count == 1  # no /videos call without ids
    assert result.quota_units == QUOTA_SEARCH


# ------------------------------------------------------------ research service


async def test_video_dedupe_and_shared_videos_across_candidates():
    shared = video_for("V1", channel_id="chA")
    c1 = make_candidate(["query a"])
    c2 = make_candidate(["query b"], title="Other")
    provider = FakeContentProvider(
        {"query a": [shared, video_for("V2", channel_id="chB")], "query b": [shared]},
        channel_stats={
            "chA": ChannelStats("chA", 1000, 50, 500000, datetime.now(UTC)),
            "chB": ChannelStats("chB", None, 10, 1000, datetime.now(UTC)),
        },
    )
    store = ResearchStore()
    result = await run_public_content_research([c1, c2], provider, store)

    assert result.unique_video_count == 2
    # One batched channel lookup covering both deduped channels.
    assert provider.channel_calls == [["chA", "chB"]]
    assert result.channel_stats_fetched == 2
    evidence = store.evidence_for_snapshot(result.snapshot.snapshot_id)
    v1_items = [e for e in evidence if e.raw_payload and e.raw_payload.get("video_id") == "V1"]
    assert {e.candidate_id for e in v1_items} == {c1.id, c2.id}
    assert len({e.raw_payload_hash for e in v1_items}) == 1
    # Channel enrichment landed in the shared canonical observation.
    assert v1_items[0].raw_payload["channel_subscriber_count"] == 1000
    assert result.snapshot.status == SnapshotStatus.COMPLETE


async def test_truth_classes_observed_inferred_unknown():
    candidate = make_candidate(["query a"])
    provider = FakeContentProvider(
        {
            "query a": [
                video_for("V1", views=1000, likes=50),
                video_for("V2", views=None, likes=None, comments=None),
            ]
        }
    )
    store = ResearchStore()
    result = await run_public_content_research([candidate], provider, store)
    evidence = store.evidence_for_snapshot(result.snapshot.snapshot_id)
    by_key = {(e.raw_payload["video_id"], e.signal_type): e for e in evidence}

    views_v1 = by_key[("V1", "public_video_view_count")]
    assert views_v1.truth_class == TruthClass.OBSERVED
    assert views_v1.raw_value == 1000
    assert views_v1.purpose == EvidencePurpose.AUDIENCE

    engagement_v1 = by_key[("V1", "public_video_engagement_rate")]
    assert engagement_v1.truth_class == TruthClass.INFERRED
    assert engagement_v1.raw_value == 0.05  # exactly 50/1000, nothing invented
    assert any("INFERRED" in lim for lim in engagement_v1.known_limitations)

    content_v1 = by_key[("V1", "public_content_observation")]
    assert content_v1.truth_class == TruthClass.OBSERVED
    assert content_v1.purpose == EvidencePurpose.CONTENT

    # Hidden metrics: UNKNOWN, raw None — never zero-filled.
    views_v2 = by_key[("V2", "public_video_view_count")]
    assert views_v2.truth_class == TruthClass.UNKNOWN
    assert views_v2.raw_value is None
    engagement_v2 = by_key[("V2", "public_video_engagement_rate")]
    assert engagement_v2.truth_class == TruthClass.UNKNOWN
    assert engagement_v2.raw_value is None


async def test_no_fabricated_metrics():
    """Every numeric raw_value must be a provider value or an exact
    deterministic derivation — nothing else."""
    candidate = make_candidate(["query a"])
    provider = FakeContentProvider({"query a": [video_for("V1", views=400, likes=30)]})
    store = ResearchStore()
    result = await run_public_content_research([candidate], provider, store)
    evidence = store.evidence_for_snapshot(result.snapshot.snapshot_id)
    values = {e.raw_value for e in evidence if e.raw_value is not None}
    assert values == {400, round(30 / 400, 6)}
    for e in evidence:
        assert "watch_time" not in e.signal_type
        assert "retention" not in e.signal_type
        assert "revenue" not in e.signal_type
        assert e.truth_class in (TruthClass.OBSERVED, TruthClass.INFERRED, TruthClass.UNKNOWN)


# ------------------------------------------------------------- feature extraction


def test_summary_deterministic_and_robust():
    cid = uuid4()
    now = datetime(2026, 6, 1, tzinfo=UTC)
    videos = [
        video_for("V1", views=100, likes=10, comments=1, channel_id="a",
                  published_at=now - timedelta(days=10)),
        video_for("V2", views=1000, likes=50, comments=5, channel_id="a",
                  published_at=now - timedelta(days=100)),
        video_for("V3", views=None, likes=None, comments=None, channel_id="b",
                  published_at=None),
    ]
    summary = summarize_public_content(cid, videos, now=now)
    assert summary.relevant_video_count == 3
    assert summary.distinct_channel_count == 2
    assert summary.total_views == 1100
    assert summary.median_views == 550.0
    assert summary.max_views == 1000
    assert summary.median_likes == 30.0
    assert summary.median_engagement_rate == round((0.1 + 0.05) / 2, 6)
    assert summary.median_days_since_publish == 55.0
    assert summary.videos_published_last_90_days == 1
    assert summary.missing_view_count == 1
    assert summary.missing_like_count == 1
    # audience_interest_dimension_v1: 100*log10(551)/7
    assert summary.audience_interest_dimension == round(
        100 * __import__("math").log10(551) / 7, 2
    )
    assert summary.dimension_version == "audience_interest_dimension_v1"
    assert summarize_public_content(cid, videos, now=now) == summary


def test_summary_with_no_videos_reports_missing_not_zero_interest():
    summary = summarize_public_content(uuid4(), [])
    assert summary.relevant_video_count == 0
    assert summary.median_views is None
    assert summary.total_views is None
    assert summary.audience_interest_dimension is None  # unknown, not zero


def test_content_outliers_creator_relative_no_age_term():
    now = datetime(2026, 6, 1, tzinfo=UTC)
    # Channel "big" has 3 sampled videos: baseline = median(100, 200, 1000) = 200.
    # The 1000-view video is a 5.0x outlier REGARDLESS of its age.
    old_outlier = video_for("V3", views=1000, channel_id="big",
                            published_at=now - timedelta(days=2000))
    videos = [
        video_for("V1", views=100, channel_id="big", published_at=now - timedelta(days=10)),
        video_for("V2", views=200, channel_id="big", published_at=now - timedelta(days=20)),
        old_outlier,
        # Channel "small" has fewer than MIN_CHANNEL_SAMPLE videos: no baseline.
        video_for("V4", views=999999, channel_id="small"),
    ]
    outliers, channels_with_baseline = extract_content_outliers(videos)
    assert channels_with_baseline == 1
    assert {o.channel_id for o in outliers} == {"big"}
    top = outliers[0]
    assert top.video_id == "V3"
    assert top.channel_baseline_median_views == 200.0
    assert top.outlier_ratio == 5.0  # views/baseline; age plays no role
    assert top.channel_sample_size == 3
    assert top.formula_version == "content_outlier_v1"
    # A younger video with the same views gets the same ratio (no age reward).
    young = video_for("V3", views=1000, channel_id="big", published_at=now)
    videos_young = [videos[0], videos[1], young]
    outliers_young, _ = extract_content_outliers(videos_young)
    assert outliers_young[0].outlier_ratio == 5.0
    assert len(videos) - 1 >= MIN_CHANNEL_SAMPLE


# ------------------------------------------------------ failure and cost control


async def test_partial_provider_failure_keeps_run_alive():
    c1 = make_candidate(["good query"])
    c2 = make_candidate(["bad query"], title="Other")
    provider = FakeContentProvider(
        {"good query": [video_for("V1")]},
        fail_queries={"bad query"},
        error=ProviderResponseError("simulated outage"),
    )
    store = ResearchStore()
    result = await run_public_content_research([c1, c2], provider, store)
    assert result.snapshot.status == SnapshotStatus.PARTIAL
    assert any("simulated outage" in e for e in result.provider_errors)
    assert [m for m in result.missing_queries if m.reason == MISSING_PROVIDER_ERROR]
    assert len(result.evidence_ids_by_candidate[c1.id]) == 3


async def test_total_failure_marks_snapshot_failed_not_crash():
    candidate = make_candidate(["q1"])
    provider = FakeContentProvider(
        {}, fail_queries={"q1"}, error=ProviderAuthError("bad key")
    )
    store = ResearchStore()
    result = await run_public_content_research([candidate], provider, store)
    assert result.snapshot.status == SnapshotStatus.FAILED
    assert result.evidence_ids_by_candidate[candidate.id] == []


async def test_quota_budget_exhaustion_reported_not_guessed():
    candidate = make_candidate(["q1", "q2"])
    provider = FakeContentProvider({q: [video_for(f"V-{q}")] for q in ("q1", "q2")})
    store = ResearchStore()
    result = await run_public_content_research(
        [candidate], provider, store, max_quota_units=150, max_channel_lookups=0
    )
    # One search fits (101 units); the second would exceed the 150 budget.
    assert provider.search_calls == ["q1"]
    reasons = {m.query: m.reason for m in result.missing_queries}
    assert reasons["q2"] == MISSING_QUOTA
    assert result.quota_units_used == 101
    assert result.snapshot.status == SnapshotStatus.PARTIAL


async def test_query_and_call_caps_enforced():
    candidate = make_candidate(["q1", "q2", "q3"])
    provider = FakeContentProvider({q: [video_for(f"V-{q}")] for q in ("q1", "q2", "q3")})
    store = ResearchStore()
    result = await run_public_content_research(
        [candidate], provider, store,
        max_queries=2, max_provider_calls=2, max_channel_lookups=0,
    )
    assert provider.search_calls == ["q1"]
    reasons = {m.query: m.reason for m in result.missing_queries}
    assert reasons["q3"] == MISSING_QUERY_CAP
    assert reasons["q2"] == MISSING_BUDGET
    assert result.snapshot.provider_call_count == 2


async def test_channel_lookup_caps_enforced():
    candidate = make_candidate(["q1"])
    videos = [video_for(f"V{i}", channel_id=f"ch{i}") for i in range(3)]
    provider = FakeContentProvider(
        {"q1": videos},
        channel_stats={f"ch{i}": ChannelStats(f"ch{i}", 10, 1, 100, datetime.now(UTC)) for i in range(3)},
    )
    store = ResearchStore()
    result = await run_public_content_research(
        [candidate], provider, store, max_channel_lookups=0
    )
    assert provider.channel_calls == []
    assert result.channel_stats_skipped == 3
    evidence = store.evidence_for_snapshot(result.snapshot.snapshot_id)
    assert all(e.raw_payload["channel_subscriber_count"] is None for e in evidence)


async def test_cache_avoids_repeat_provider_calls():
    candidate = make_candidate(["q1"])
    provider = FakeContentProvider(
        {"q1": [video_for("V1", channel_id="chA")]},
        channel_stats={"chA": ChannelStats("chA", 777, 5, 100, datetime.now(UTC))},
    )
    store = ResearchStore()
    first = await run_public_content_research([candidate], provider, store)
    assert provider.search_calls == ["q1"]
    assert first.quota_units_used == 102  # 101 search + 1 channel batch
    second = await run_public_content_research([candidate], provider, store)
    assert provider.search_calls == ["q1"]  # cache hit: no new search
    assert provider.channel_calls == [["chA"]]  # enrichment cached too
    assert second.cached_query_count == 1
    assert second.quota_units_used == 0
    assert second.snapshot.provider_call_count == 0
    assert first.snapshot.snapshot_id != second.snapshot.snapshot_id
    # Cached run still carries the enriched channel stats.
    evidence = store.evidence_for_snapshot(second.snapshot.snapshot_id)
    assert evidence[0].raw_payload["channel_subscriber_count"] == 777


async def test_snapshots_immutable_and_history_preserved():
    candidate = make_candidate(["q1"])
    provider = FakeContentProvider({"q1": [video_for("V1")]})
    store = ResearchStore()
    result = await run_public_content_research([candidate], provider, store)
    with pytest.raises(ImmutableEvidenceError):
        store.add_snapshot(result.snapshot)
    evidence = store.evidence_for_snapshot(result.snapshot.snapshot_id)
    with pytest.raises(ImmutableEvidenceError):
        store.add_evidence(evidence[0])
    with pytest.raises(Exception):  # frozen model: no in-place edits
        evidence[0].raw_value = 999.0


# ---------------------------------------------------------------------- endpoint


def test_endpoint_runs_public_content_research(monkeypatch):
    provider = FakeContentProvider(
        {
            "sourdough tutorial": [
                video_for("V1", views=100, likes=10, channel_id="chA"),
                video_for("V2", views=300, likes=20, channel_id="chA"),
                video_for("V3", views=900, likes=30, channel_id="chA"),
            ]
        },
        channel_stats={"chA": ChannelStats("chA", 5000, 40, 100000, datetime.now(UTC))},
    )
    monkeypatch.setitem(routes.PUBLIC_CONTENT_PROVIDERS, "youtube", lambda: provider)
    candidate = make_candidate(["Sourdough  TUTORIAL"])
    response = client.post(
        "/research/public-content",
        json={"candidates": [json.loads(candidate.model_dump_json())]},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["snapshot"]["status"] == "COMPLETE"
    assert body["unique_video_count"] == 3
    assert body["quota_units_used"] == 102
    summary = body["summaries"][0]
    assert summary["relevant_video_count"] == 3
    assert summary["median_views"] == 300.0
    assert summary["channels_with_baseline"] == 1
    top_outlier = summary["content_outliers"][0]
    assert top_outlier["video_id"] == "V3"
    assert top_outlier["outlier_ratio"] == 3.0  # 900 / median(100,300,900)
    assert body["evidence_ids_by_candidate"][str(candidate.id)]

    snap = client.get(
        f"/research/public-content/snapshots/{body['snapshot']['snapshot_id']}"
    )
    assert snap.status_code == 200
    assert len(snap.json()["evidence"]) == 9  # 3 videos x 3 classifications
    purposes = {e["purpose"] for e in snap.json()["evidence"]}
    assert purposes == {"AUDIENCE", "CONTENT"}


def test_endpoint_requires_exactly_one_input_source():
    assert client.post("/research/public-content", json={}).status_code == 422
    candidate = make_candidate(["q"])
    both = {
        "candidates": [json.loads(candidate.model_dump_json())],
        "research_run_id": str(uuid4()),
    }
    assert client.post("/research/public-content", json=both).status_code == 422


def test_endpoint_unknown_run_and_unknown_provider():
    assert (
        client.post(
            "/research/public-content", json={"research_run_id": str(uuid4())}
        ).status_code
        == 404
    )
    candidate = make_candidate(["q"])
    unknown = client.post(
        "/research/public-content",
        json={
            "candidates": [json.loads(candidate.model_dump_json())],
            "provider": "tiktok",
        },
    )
    assert unknown.status_code == 422


def test_endpoint_missing_credentials_returns_503(monkeypatch):
    monkeypatch.delenv(ENV_API_KEY, raising=False)
    candidate = make_candidate(["q"])
    response = client.post(
        "/research/public-content",
        json={"candidates": [json.loads(candidate.model_dump_json())]},
    )
    assert response.status_code == 503
    assert "YOUTUBE_API_KEY" in response.json()["detail"]


def test_endpoint_response_never_contains_secret(monkeypatch):
    monkeypatch.setenv(ENV_API_KEY, FAKE_KEY)
    handler = search_and_videos_handler([youtube_video_payload()])
    monkeypatch.setitem(
        routes.PUBLIC_CONTENT_PROVIDERS,
        "youtube",
        lambda: YouTubeContentProvider(transport=httpx.MockTransport(handler)),
    )
    candidate = make_candidate(["q"])
    response = client.post(
        "/research/public-content",
        json={
            "candidates": [json.loads(candidate.model_dump_json())],
            "max_channel_lookups": 0,
        },
    )
    assert response.status_code == 200
    assert FAKE_KEY not in response.text
