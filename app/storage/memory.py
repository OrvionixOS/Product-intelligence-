"""In-memory, append-only research storage.

V1 has no database wiring yet; this store is the persistence seam. Its API is
deliberately append-only: evidence and snapshots can be added and read, never
updated or deleted, so historical evidence cannot be overwritten. schema.sql
documents the eventual Postgres shape of the same data.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import TYPE_CHECKING, Generic, TypeVar
from uuid import UUID

from app.domain.models import Candidate, EvidenceItem, EvidenceSnapshot
from app.providers.base import KeywordDemandMetrics, MarketplaceListing, VideoObservation

if TYPE_CHECKING:  # pragma: no cover - typing only
    # Imported for the annotation alone. Storage does not depend on the
    # service layer at runtime; the record arrives already validated by its
    # own constructor (SPEC_STEP_7_8.md §10 invariants).
    from app.services.opportunity_scoring import ScoringResult

DEFAULT_CACHE_TTL = timedelta(days=7)

T = TypeVar("T")


class CacheOutcome(str, Enum):
    """Why a result-count-limited cache lookup did or did not serve a request.

    SPEC_STEP_7_8.md §1.1: a deep pass must never treat a result collected
    under a smaller effective limit as satisfying a larger request. The three
    outcomes are kept distinct because a DEPTH_MISS looks exactly like a HIT
    from the outside -- an entry was present -- and counting it as one would
    hide the failure the rule exists to prevent.
    """

    # Entry present and collected at least as deep as this request.
    HIT = "HIT"
    # Entry present but collected under a smaller limit. Re-fetch, never reuse.
    # Not an error: this is the normal cost of going deeper.
    DEPTH_MISS = "DEPTH_MISS"
    # No entry, or the entry expired.
    MISS = "MISS"


@dataclass(slots=True, frozen=True)
class CacheLookup(Generic[T]):
    """The result of a limit-aware cache read."""

    outcome: CacheOutcome
    # Populated only on HIT. A DEPTH_MISS deliberately yields nothing: fewer
    # cached results is a property of the earlier budget, never an observation
    # about the field, so the shallow set must not leak into a deep pass.
    items: list[T] | None = None
    # What limit the stored entry was collected under. Present on HIT and
    # DEPTH_MISS, so a reuse decision is auditable against the budget that
    # authorized it.
    cached_effective_limit: int | None = None


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
        # Result-count-limited caches also store the effective limit each entry
        # was collected under (§1.1 rule 1).
        self._listing_cache: dict[
            tuple[str, str], tuple[datetime, list[MarketplaceListing], int]
        ] = {}
        self._video_cache: dict[
            tuple[str, str], tuple[datetime, list[VideoObservation], int]
        ] = {}
        # Milestone 6F: scoring records, append-only like everything else.
        # Keyed by (candidate_id, research_run_id); every write is retained so
        # a rescore never destroys what an earlier one recorded.
        self._scores: dict[tuple[UUID, UUID], list["ScoringResult"]] = {}
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

    def evidence_for_candidate(
        self, candidate_id: UUID, research_run_id: UUID | None = None
    ) -> list[EvidenceItem]:
        """Read-only view of stored evidence for one candidate.

        The store is append-only across runs, so a candidate researched more
        than once accumulates one set of records per run. Pass
        `research_run_id` to read a single run; without it every run's
        records are returned together, which mixes provenance and
        double-counts any listing observed in more than one run.

        A record carrying no run id is returned by every scoped read: it
        cannot belong to a different run, and dropping it would silently
        discard stored evidence rather than isolate provenance.

        Insertion-ordered, like every other read here: this resolves existing
        records, it never collects, mutates, or infers.
        """
        items = [self._evidence[eid] for eid in self._evidence_by_candidate.get(candidate_id, [])]
        if research_run_id is None:
            return items
        return [
            item
            for item in items
            if item.research_run_id in (research_run_id, None)
        ]

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

    def cached_listings(
        self, provider: str, query: str, requested_limit: int
    ) -> CacheLookup[MarketplaceListing]:
        """Read the listing cache, honouring §1.1 depth semantics."""
        return _limit_aware_lookup(
            self._listing_cache.get((provider, query)), requested_limit, self._cache_ttl
        )

    def cache_listings(
        self,
        provider: str,
        query: str,
        listings: list[MarketplaceListing],
        effective_limit: int,
    ) -> None:
        """Store listings with the limit they were collected under.

        `effective_limit` is what was REQUESTED, not how many came back. A
        query that returns three results under a limit of fifty was still
        collected deeply; recording three would make the next deep request a
        spurious depth miss, and would quietly turn a budget fact into a claim
        about the field.
        """
        # §1.1 rule 4: re-fetching replaces the entry, so a later shallow
        # request reuses it correctly and a later deeper one still misses.
        self._listing_cache[(provider, query)] = (
            datetime.now(UTC),
            list(listings),
            effective_limit,
        )

    # --------------------------------------------------------- scoring state

    def add_scoring_result(self, result: "ScoringResult") -> None:
        """Record one Step 8 result. Append-only, like evidence.

        Nothing is re-validated here: the §10 invariants are enforced by
        `ScoringResult` itself, so a record that reached this point cannot
        claim a score or a colour its state forbids.
        """
        key = (result.candidate_id, result.research_run_id)
        self._scores.setdefault(key, []).append(result)

    def scoring_history(
        self, candidate_id: UUID, research_run_id: UUID
    ) -> list["ScoringResult"]:
        """Every scoring record for this scope, oldest first."""
        return list(self._scores.get((candidate_id, research_run_id), []))

    def latest_scoring_result(
        self, candidate_id: UUID, research_run_id: UUID
    ) -> "ScoringResult | None":
        """The most recent record, or None when Step 8 has not run.

        None means not even NOT_SCORED was recorded. It is the absence of an
        attempt, and callers must not read it as a score of any kind.
        """
        history = self._scores.get((candidate_id, research_run_id))
        return history[-1] if history else None

    # ------------------------------------------------- public-content video cache

    def cached_videos(
        self, provider: str, query: str, requested_limit: int
    ) -> CacheLookup[VideoObservation]:
        """Read the video cache, honouring §1.1 depth semantics."""
        return _limit_aware_lookup(
            self._video_cache.get((provider, query)), requested_limit, self._cache_ttl
        )

    def cache_videos(
        self,
        provider: str,
        query: str,
        videos: list[VideoObservation],
        effective_limit: int,
    ) -> None:
        """Store videos with the limit they were collected under."""
        self._video_cache[(provider, query)] = (
            datetime.now(UTC),
            list(videos),
            effective_limit,
        )


def _limit_aware_lookup(
    entry: tuple[datetime, list[T], int] | None,
    requested_limit: int,
    cache_ttl: timedelta,
) -> CacheLookup[T]:
    """§1.1 rule 2: reuse requires cached_effective_limit >= requested."""
    if entry is None:
        return CacheLookup(outcome=CacheOutcome.MISS)
    stored_at, items, cached_effective_limit = entry
    if datetime.now(UTC) - stored_at > cache_ttl:
        return CacheLookup(outcome=CacheOutcome.MISS)
    if cached_effective_limit < requested_limit:
        return CacheLookup(
            outcome=CacheOutcome.DEPTH_MISS,
            cached_effective_limit=cached_effective_limit,
        )
    return CacheLookup(
        outcome=CacheOutcome.HIT,
        items=list(items),
        cached_effective_limit=cached_effective_limit,
    )
