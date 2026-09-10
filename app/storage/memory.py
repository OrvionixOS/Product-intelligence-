"""In-memory, append-only research storage.

V1 has no database wiring yet; this store is the persistence seam. Its API is
deliberately append-only: evidence and snapshots can be added and read, never
updated or deleted, so historical evidence cannot be overwritten. schema.sql
documents the eventual Postgres shape of the same data.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from app.domain.models import Candidate, EvidenceItem, EvidenceSnapshot
from app.providers.base import KeywordDemandMetrics, MarketplaceListing, VideoObservation

DEFAULT_CACHE_TTL = timedelta(days=7)


class ImmutableEvidenceError(RuntimeError):
    pass


class ResearchStore:
    """Append-only store for research runs, snapshots, evidence, and a
    keyword-metrics cache used to avoid re-buying identical measurements."""

    def __init__(self, cache_ttl: timedelta = DEFAULT_CACHE_TTL) -> None:
        self._runs: dict[UUID, list[Candidate]] = {}
        self._snapshots: dict[UUID, EvidenceSnapshot] = {}
        self._evidence: dict[UUID, EvidenceItem] = {}
        self._evidence_by_snapshot: dict[UUID, list[UUID]] = {}
        self._evidence_by_candidate: dict[UUID, list[UUID]] = {}
        self._keyword_cache: dict[tuple[str, str, str, str], tuple[datetime, KeywordDemandMetrics]] = {}
        self._listing_cache: dict[tuple[str, str], tuple[datetime, list[MarketplaceListing]]] = {}
        self._video_cache: dict[tuple[str, str], tuple[datetime, list[VideoObservation]]] = {}
        self._cache_ttl = cache_ttl

    # ------------------------------------------------------------- research runs

    def register_run(self, run_id: UUID, candidates: list[Candidate]) -> None:
        if run_id in self._runs:
            raise ImmutableEvidenceError(f"research run {run_id} already registered")
        self._runs[run_id] = list(candidates)

    def get_run_candidates(self, run_id: UUID) -> list[Candidate] | None:
        candidates = self._runs.get(run_id)
        return list(candidates) if candidates is not None else None

    # ---------------------------------------------------------------- snapshots

    def add_snapshot(self, snapshot: EvidenceSnapshot) -> None:
        if snapshot.snapshot_id in self._snapshots:
            raise ImmutableEvidenceError(
                f"snapshot {snapshot.snapshot_id} already stored; snapshots are immutable"
            )
        self._snapshots[snapshot.snapshot_id] = snapshot
        self._evidence_by_snapshot.setdefault(snapshot.snapshot_id, [])

    def get_snapshot(self, snapshot_id: UUID) -> EvidenceSnapshot | None:
        return self._snapshots.get(snapshot_id)

    # ----------------------------------------------------------------- evidence

    def add_evidence(self, item: EvidenceItem) -> None:
        if item.id in self._evidence:
            raise ImmutableEvidenceError(
                f"evidence {item.id} already stored; evidence records are immutable"
            )
        self._evidence[item.id] = item
        if item.snapshot_id is not None:
            self._evidence_by_snapshot.setdefault(item.snapshot_id, []).append(item.id)
        if item.candidate_id is not None:
            self._evidence_by_candidate.setdefault(item.candidate_id, []).append(item.id)

    def get_evidence(self, evidence_id: UUID) -> EvidenceItem | None:
        return self._evidence.get(evidence_id)

    def evidence_for_snapshot(self, snapshot_id: UUID) -> list[EvidenceItem]:
        return [self._evidence[eid] for eid in self._evidence_by_snapshot.get(snapshot_id, [])]

    def evidence_for_candidate(self, candidate_id: UUID) -> list[EvidenceItem]:
        """Read-only view of everything stored for one candidate.

        Insertion-ordered and append-only, like every other read here: this
        resolves existing records, it never collects, mutates, or infers.
        """
        return [self._evidence[eid] for eid in self._evidence_by_candidate.get(candidate_id, [])]

    # ------------------------------------------------------------ keyword cache

    def cached_metrics(
        self, provider: str, keyword: str, location: str, language: str
    ) -> KeywordDemandMetrics | None:
        entry = self._keyword_cache.get((provider, keyword, location, language))
        if entry is None:
            return None
        stored_at, metrics = entry
        if datetime.now(UTC) - stored_at > self._cache_ttl:
            return None
        return metrics

    def cache_metrics(
        self, provider: str, keyword: str, location: str, language: str, metrics: KeywordDemandMetrics
    ) -> None:
        self._keyword_cache[(provider, keyword, location, language)] = (
            datetime.now(UTC),
            metrics,
        )

    # -------------------------------------------------- marketplace listing cache

    def cached_listings(self, provider: str, query: str) -> list[MarketplaceListing] | None:
        entry = self._listing_cache.get((provider, query))
        if entry is None:
            return None
        stored_at, listings = entry
        if datetime.now(UTC) - stored_at > self._cache_ttl:
            return None
        return list(listings)

    def cache_listings(
        self, provider: str, query: str, listings: list[MarketplaceListing]
    ) -> None:
        self._listing_cache[(provider, query)] = (datetime.now(UTC), list(listings))

    # ------------------------------------------------- public-content video cache

    def cached_videos(self, provider: str, query: str) -> list[VideoObservation] | None:
        entry = self._video_cache.get((provider, query))
        if entry is None:
            return None
        stored_at, videos = entry
        if datetime.now(UTC) - stored_at > self._cache_ttl:
            return None
        return list(videos)

    def cache_videos(self, provider: str, query: str, videos: list[VideoObservation]) -> None:
        self._video_cache[(provider, query)] = (datetime.now(UTC), list(videos))
