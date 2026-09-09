"""Public-content research orchestration (Milestone 3B — YouTube).

candidate list -> collect content queries -> normalize + dedupe
-> capped provider searches under a quota budget -> video dedupe by video_id
-> capped batched channel-stats enrichment -> immutable evidence records
-> evidence snapshot -> deterministic candidate summaries.

Truth model enforced here:

- Public metrics the provider actually returned are truth_class=OBSERVED.
- Metrics deterministically derived from OBSERVED values (engagement rate)
  are truth_class=INFERRED and name their formula.
- Hidden or absent metrics produce UNKNOWN evidence and stay None; missing
  data is reported, never zero-filled or estimated.
- Private platform metrics (watch time, retention, impressions, CTR,
  subscriber conversion, sales, revenue) are never estimated by anything,
  LLMs included.

A failed provider request degrades the run to PARTIAL/FAILED with explicit
missing-query records — it never destroys the research run. Quota units are
accounted explicitly because YouTube's cost model is quota, not dollars.
"""

import hashlib
import json
import os
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from uuid import UUID, uuid4

from app.domain.enums import EvidencePurpose, SnapshotStatus, TruthClass
from app.domain.models import Candidate, EvidenceItem, EvidenceSnapshot
from app.providers.base import (
    ProviderError,
    PublicContentProvider,
    VideoObservation,
)
from app.services.public_content_features import (
    PublicContentSummary,
    engagement_rate,
    summarize_public_content,
)
from app.services.search_demand import normalize_query
from app.storage.memory import ResearchStore

NORMALIZATION_VERSION = "public_content_norm_v1"

# Public-content evidence is not geography-filtered in V1.
PUBLIC_CONTENT_GEOGRAPHY = "GLOBAL"

VIEW_LIMITATIONS = [
    "View counts measure public content interest, not buyers, purchases, or revenue.",
    "Provider-supplied public metric: OBSERVED means the field was returned by the platform API, not independently audited.",
    "Private metrics (watch time, retention, impressions, CTR, subscriber conversion) are not public and remain UNKNOWN.",
]

ENGAGEMENT_LIMITATIONS = [
    "INFERRED: deterministically derived as like_count / view_count from OBSERVED public fields; not provider-reported.",
    "Engagement is content-interaction interest, never purchase intent or sales.",
]

ENGAGEMENT_UNKNOWN_LIMITATIONS = [
    "Engagement rate is not computable: like or view count was not publicly available.",
    "No inferred value exists; UNKNOWN is reported instead of a guess.",
    "Engagement is content-interaction interest, never purchase intent or sales.",
]

CONTENT_LIMITATIONS = [
    "Public metadata observation for content-pattern research; presence is not performance validation.",
    "Search-ranked results are a provider-ordered sample, not the full content field.",
]

ENV_MAX_PROVIDER_CALLS = "PUBLIC_CONTENT_MAX_PROVIDER_CALLS"
ENV_MAX_QUERIES = "PUBLIC_CONTENT_MAX_QUERIES"
ENV_MAX_VIDEOS_PER_QUERY = "PUBLIC_CONTENT_MAX_VIDEOS_PER_QUERY"
ENV_MAX_QUOTA_UNITS = "PUBLIC_CONTENT_MAX_QUOTA_UNITS"
ENV_MAX_CHANNEL_LOOKUPS = "PUBLIC_CONTENT_MAX_CHANNEL_LOOKUPS"
DEFAULT_MAX_PROVIDER_CALLS = 20
DEFAULT_MAX_QUERIES = 25
DEFAULT_MAX_VIDEOS_PER_QUERY = 10
DEFAULT_MAX_QUOTA_UNITS = 1000  # of YouTube's default 10,000/day project quota
DEFAULT_MAX_CHANNEL_LOOKUPS = 2  # batched calls; each covers up to 50 channels

# Reasons a requested content query ended up without videos.
MISSING_QUERY_CAP = "query_cap_reached"
MISSING_BUDGET = "provider_call_budget_exhausted"
MISSING_QUOTA = "quota_budget_exhausted"
MISSING_PROVIDER_ERROR = "provider_error"
MISSING_NO_RESULTS = "no_videos_returned"


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return max(0, int(raw))
    except ValueError:
        return default


def _payload_hash(payload: dict) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


@dataclass(slots=True)
class ContentQueryPlan:
    """Deduplicated content queries with candidate relationships preserved."""

    queries: list[str]
    query_to_candidates: dict[str, list[UUID]]


def plan_content_queries(candidates: list[Candidate]) -> ContentQueryPlan:
    queries: list[str] = []
    mapping: dict[str, list[UUID]] = {}
    for candidate in candidates:
        for raw_query in candidate.content_queries:
            query = normalize_query(raw_query)
            if not query:
                continue
            if query not in mapping:
                mapping[query] = []
                queries.append(query)
            if candidate.id not in mapping[query]:
                mapping[query].append(candidate.id)
    return ContentQueryPlan(queries=queries, query_to_candidates=mapping)


@dataclass(slots=True)
class MissingQuery:
    query: str
    reason: str
    candidate_ids: list[UUID]


@dataclass(slots=True)
class PublicContentRunResult:
    snapshot: EvidenceSnapshot
    summaries: dict[UUID, PublicContentSummary]
    evidence_ids_by_candidate: dict[UUID, list[UUID]]
    provider_errors: list[str] = field(default_factory=list)
    missing_queries: list[MissingQuery] = field(default_factory=list)
    cached_query_count: int = 0
    unique_video_count: int = 0
    quota_units_used: int = 0
    # False when a failure made exact quota consumption unknowable; the
    # total then includes conservative full-cost estimates for those calls.
    quota_units_is_exact: bool = True
    channel_stats_fetched: int = 0
    channel_stats_skipped: int = 0


def build_video_evidence(
    candidate_id: UUID,
    research_run_id: UUID,
    snapshot_id: UUID,
    video: VideoObservation,
    provider: PublicContentProvider,
    source_reference: str | None,
) -> list[EvidenceItem]:
    """Immutable evidence for one (candidate, video) pair.

    Three classifications per video, each reflecting only what the field
    actually proves:

    - AUDIENCE / views: the public view count (OBSERVED when returned,
      UNKNOWN when hidden or absent — never zero-filled).
    - AUDIENCE / engagement rate: deterministically derived like/view ratio
      (INFERRED when computable from OBSERVED fields, UNKNOWN otherwise).
    - CONTENT / observation: the video's presence and metadata as
      content-pattern research input (OBSERVED).
    """
    payload = asdict(video)
    payload_hash = _payload_hash(payload)
    common = dict(
        candidate_id=candidate_id,
        research_run_id=research_run_id,
        snapshot_id=snapshot_id,
        provider=provider.name,
        collection_method=provider.collection_method,
        source_reference=source_reference,
        source_url=video.url,
        retrieved_at=video.retrieved_at,
        geography=None,
        platform=provider.name,
        normalization_version=NORMALIZATION_VERSION,
        raw_payload=payload,
        raw_payload_hash=payload_hash,
    )

    views_observed = video.view_count is not None
    views_item = EvidenceItem(
        signal_type="public_video_view_count",
        purpose=EvidencePurpose.AUDIENCE,
        truth_class=TruthClass.OBSERVED if views_observed else TruthClass.UNKNOWN,
        raw_value=video.view_count,
        unit="views" if views_observed else None,
        known_limitations=list(VIEW_LIMITATIONS),
        **common,
    )

    rate = engagement_rate(video)
    engagement_item = EvidenceItem(
        signal_type="public_video_engagement_rate",
        purpose=EvidencePurpose.AUDIENCE,
        truth_class=TruthClass.INFERRED if rate is not None else TruthClass.UNKNOWN,
        raw_value=rate,
        unit="likes_per_view" if rate is not None else None,
        known_limitations=list(ENGAGEMENT_LIMITATIONS)
        if rate is not None
        else list(ENGAGEMENT_UNKNOWN_LIMITATIONS),
        **common,
    )

    content_item = EvidenceItem(
        signal_type="public_content_observation",
        purpose=EvidencePurpose.CONTENT,
        truth_class=TruthClass.OBSERVED,
        raw_value=None,
        unit=None,
        known_limitations=list(CONTENT_LIMITATIONS),
        **common,
    )

    return [views_item, engagement_item, content_item]


async def run_public_content_research(
    candidates: list[Candidate],
    provider: PublicContentProvider,
    store: ResearchStore,
    research_run_id: UUID | None = None,
    max_provider_calls: int | None = None,
    max_queries: int | None = None,
    max_videos_per_query: int | None = None,
    max_quota_units: int | None = None,
    max_channel_lookups: int | None = None,
) -> PublicContentRunResult:
    """Run one public-content research pass and persist an immutable snapshot."""
    run_id = research_run_id or uuid4()
    snapshot_id = uuid4()
    started_at = datetime.now(UTC)

    call_budget = (
        max_provider_calls
        if max_provider_calls is not None
        else _env_int(ENV_MAX_PROVIDER_CALLS, DEFAULT_MAX_PROVIDER_CALLS)
    )
    query_cap = (
        max_queries if max_queries is not None else _env_int(ENV_MAX_QUERIES, DEFAULT_MAX_QUERIES)
    )
    videos_cap = (
        max_videos_per_query
        if max_videos_per_query is not None
        else _env_int(ENV_MAX_VIDEOS_PER_QUERY, DEFAULT_MAX_VIDEOS_PER_QUERY)
    )
    quota_budget = (
        max_quota_units
        if max_quota_units is not None
        else _env_int(ENV_MAX_QUOTA_UNITS, DEFAULT_MAX_QUOTA_UNITS)
    )
    channel_lookup_budget = (
        max_channel_lookups
        if max_channel_lookups is not None
        else _env_int(ENV_MAX_CHANNEL_LOOKUPS, DEFAULT_MAX_CHANNEL_LOOKUPS)
    )

    plan = plan_content_queries(candidates)
    missing: list[MissingQuery] = []
    provider_errors: list[str] = []

    requested = plan.queries[:query_cap]
    for query in plan.queries[query_cap:]:
        missing.append(MissingQuery(query, MISSING_QUERY_CAP, plan.query_to_candidates[query]))

    # Cache first: identical (provider, query) result sets within the
    # freshness window are reused instead of re-spending quota.
    videos_by_query: dict[str, list[VideoObservation]] = {}
    to_fetch: list[str] = []
    cached_count = 0
    for query in requested:
        cached = store.cached_videos(provider.name, query)
        if cached is not None:
            videos_by_query[query] = cached
            cached_count += 1
        else:
            to_fetch.append(query)

    call_count = 0
    quota_used = 0
    provider_version: str | None = None
    source_reference: str | None = None
    fetched_queries: list[str] = []

    quota_is_exact = True
    for query in to_fetch:
        # A search pass costs the provider-declared call shape and quota;
        # never start one that would exceed either budget.
        if call_count + provider.calls_per_search > call_budget:
            missing.append(MissingQuery(query, MISSING_BUDGET, plan.query_to_candidates[query]))
            continue
        if quota_used + provider.quota_units_per_search > quota_budget:
            missing.append(MissingQuery(query, MISSING_QUOTA, plan.query_to_candidates[query]))
            continue
        try:
            result = await provider.search_videos(query, limit=videos_cap)
        except ProviderError as exc:
            # Honest accounting for the failed attempt: use what the adapter
            # reports it consumed; when exact quota consumption cannot be
            # known, budget the full search cost and flag the total inexact
            # rather than reporting zero.
            call_count += exc.calls_consumed
            if exc.quota_units_consumed is not None:
                quota_used += exc.quota_units_consumed
            else:
                quota_used += provider.quota_units_per_search
                quota_is_exact = False
            provider_errors.append(f"{type(exc).__name__}: {exc}")
            missing.append(
                MissingQuery(query, MISSING_PROVIDER_ERROR, plan.query_to_candidates[query])
            )
            continue

        call_count += result.call_count
        quota_used += result.quota_units
        provider_errors.extend(result.errors)
        provider_version = result.provider_version or provider_version
        source_reference = result.source_reference or source_reference
        videos_by_query[query] = result.videos
        fetched_queries.append(query)
        if not result.videos:
            missing.append(
                MissingQuery(query, MISSING_NO_RESULTS, plan.query_to_candidates[query])
            )

    # Dedupe videos across queries by video_id: one canonical observation per
    # video, shared by every candidate whose query matched it.
    canonical: dict[str, VideoObservation] = {}
    video_to_candidates: dict[str, list[UUID]] = {}
    video_order: list[str] = []
    for query in requested:
        for video in videos_by_query.get(query, []):
            if video.video_id not in canonical:
                canonical[video.video_id] = video
                video_to_candidates[video.video_id] = []
                video_order.append(video.video_id)
            for candidate_id in plan.query_to_candidates[query]:
                if candidate_id not in video_to_candidates[video.video_id]:
                    video_to_candidates[video.video_id].append(candidate_id)

    # Capped, batched channel-stats enrichment over deduplicated channels.
    channels_fetched = 0
    channels_skipped = 0
    if provider.supports_channel_stats:
        pending_channels: list[str] = []
        for video_id in video_order:
            video = canonical[video_id]
            # channel_stats_retrieved_at marks a real lookup, so a channel
            # whose stats are hidden (all None) is not re-fetched from cache.
            if video.channel_id is None or video.channel_stats_retrieved_at is not None:
                continue
            if video.channel_id not in pending_channels:
                pending_channels.append(video.channel_id)

        batch_size = provider.max_channels_per_stats_request
        batches = [
            pending_channels[i : i + batch_size]
            for i in range(0, len(pending_channels), batch_size)
        ]
        stats_by_channel = {}
        lookups = 0
        for batch in batches:
            if (
                lookups >= channel_lookup_budget
                or call_count + provider.calls_per_channel_stats > call_budget
                or quota_used + provider.quota_units_per_channel_stats > quota_budget
            ):
                channels_skipped += len(batch)
                continue
            lookups += 1
            try:
                stats_result = await provider.fetch_channel_stats(batch)
            except ProviderError as exc:
                call_count += exc.calls_consumed
                if exc.quota_units_consumed is not None:
                    quota_used += exc.quota_units_consumed
                else:
                    quota_used += provider.quota_units_per_channel_stats
                    quota_is_exact = False
                provider_errors.append(f"{type(exc).__name__}: {exc}")
                channels_skipped += len(batch)
                continue
            call_count += stats_result.call_count
            quota_used += stats_result.quota_units
            provider_errors.extend(stats_result.errors)
            stats_by_channel.update(stats_result.stats)
            channels_fetched += len(stats_result.stats)

        if stats_by_channel:
            for video_id in video_order:
                video = canonical[video_id]
                stats = stats_by_channel.get(video.channel_id or "")
                if stats is not None:
                    canonical[video_id] = replace(
                        video,
                        channel_subscriber_count=stats.subscriber_count,
                        channel_video_count=stats.video_count,
                        channel_view_count=stats.view_count,
                        channel_stats_retrieved_at=stats.retrieved_at,
                    )

    # Cache final (enriched) videos per fetched query so a later run within
    # the freshness window spends no quota at all.
    for query in fetched_queries:
        store.cache_videos(
            provider.name,
            query,
            [canonical[video.video_id] for video in videos_by_query[query]],
        )

    # Build immutable evidence: views + engagement + content records per
    # (candidate, video); the underlying observation (payload and hash) is
    # identical for every candidate sharing the video.
    evidence_ids: dict[UUID, list[UUID]] = {c.id: [] for c in candidates}
    videos_by_candidate: dict[UUID, list[VideoObservation]] = {c.id: [] for c in candidates}
    evidence_items: list[EvidenceItem] = []
    for video_id in video_order:
        video = canonical[video_id]
        for candidate_id in video_to_candidates[video_id]:
            items = build_video_evidence(
                candidate_id, run_id, snapshot_id, video, provider, source_reference
            )
            evidence_items.extend(items)
            evidence_ids[candidate_id].extend(item.id for item in items)
            videos_by_candidate[candidate_id].append(video)

    hard_failures = [m for m in missing if m.reason != MISSING_NO_RESULTS]
    if not canonical and (provider_errors or hard_failures):
        status = SnapshotStatus.FAILED
    elif provider_errors or hard_failures:
        status = SnapshotStatus.PARTIAL
    else:
        status = SnapshotStatus.COMPLETE

    snapshot = EvidenceSnapshot(
        snapshot_id=snapshot_id,
        research_run_id=run_id,
        provider=provider.name,
        started_at=started_at,
        completed_at=datetime.now(UTC),
        geography=PUBLIC_CONTENT_GEOGRAPHY,
        language="all",
        status=status,
        provider_call_count=call_count,
        provider_cost=None,  # YouTube's cost model is quota units, not dollars
        provider_cost_is_estimate=None,
        normalization_version=NORMALIZATION_VERSION,
    )
    store.add_snapshot(snapshot)
    for item in evidence_items:
        store.add_evidence(item)

    summaries = {
        candidate.id: summarize_public_content(candidate.id, videos_by_candidate[candidate.id])
        for candidate in candidates
    }

    return PublicContentRunResult(
        snapshot=snapshot,
        summaries=summaries,
        evidence_ids_by_candidate=evidence_ids,
        provider_errors=provider_errors,
        missing_queries=missing,
        cached_query_count=cached_count,
        unique_video_count=len(canonical),
        quota_units_used=quota_used,
        quota_units_is_exact=quota_is_exact,
        channel_stats_fetched=channels_fetched,
        channel_stats_skipped=channels_skipped,
    )
