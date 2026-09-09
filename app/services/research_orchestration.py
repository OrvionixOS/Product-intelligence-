"""Preliminary research orchestration (Milestone 3C).

    candidate set (Milestone 1)
      -> collect/resolve existing evidence (M2 search demand, 3A marketplace,
         3B public content)
      -> deterministic preliminary dimensions (evidence bridge)
      -> deterministic preliminary ranking
      -> top-five selection for the later deep-research stage

This layer coordinates the capabilities already implemented; it adds no new
provider calls of its own, invents no evidence, and never calls an LLM.

Honest partial failure: each capability runs independently, and a provider
failure is recorded as a capability outcome rather than aborting the run. A
capability that produced nothing leaves its dimensions MISSING with a stated
reason — never zero. Every capability's own caps, cache, cost, and quota
accounting are preserved by delegating to its existing runner unchanged.
"""

from dataclasses import dataclass, field
from uuid import UUID, uuid4

from app.domain.enums import SnapshotStatus
from app.domain.models import Candidate, EvidenceItem
from app.providers.base import (
    MarketplaceProvider,
    ProviderError,
    PublicContentProvider,
    SearchDemandProvider,
)
from app.services.marketplace import run_marketplace_research
from app.services.preliminary_dimensions import (
    DIM_AUDIENCE_INTEREST,
    DIM_PRICE_EVIDENCE,
    DIM_SEARCH_DEMAND,
    PRELIMINARY_DIMENSIONS_VERSION,
    CandidatePreliminaryProfile,
    build_preliminary_profile,
)
from app.services.preliminary_ranking import (
    DEEP_RESEARCH_SELECTION_SIZE,
    PRELIMINARY_RANKING_VERSION,
    PreliminaryRanking,
    rank_candidates,
)
from app.services.public_content import run_public_content_research
from app.services.search_demand import run_search_demand_research
from app.storage.memory import ResearchStore

ORCHESTRATION_VERSION = "preliminary_orchestration_v1"

CAPABILITY_SEARCH_DEMAND = "search_demand"
CAPABILITY_MARKETPLACE = "marketplace"
CAPABILITY_PUBLIC_CONTENT = "public_content"

# Capabilities run in this fixed order so evidence is attached, and therefore
# ordered, identically on every run with the same inputs.
CAPABILITY_ORDER = (
    CAPABILITY_SEARCH_DEMAND,
    CAPABILITY_MARKETPLACE,
    CAPABILITY_PUBLIC_CONTENT,
)

# Capability statuses beyond the snapshot statuses.
STATUS_NOT_REQUESTED = "NOT_REQUESTED"
STATUS_PROVIDER_FAILED = "PROVIDER_FAILED"

MISSING_CAPABILITY_NOT_REQUESTED = "capability_not_requested"
MISSING_CAPABILITY_FAILED = "capability_provider_failed"
MISSING_NO_EVIDENCE_FOR_CANDIDATE = "no_evidence_returned_for_candidate"


@dataclass(slots=True)
class CapabilityCaps:
    """Per-capability caps forwarded verbatim to that capability's runner.

    None means "use the capability's own configured default", so existing
    provider caps and quota/cost budgets keep working untouched.
    """

    max_provider_calls: int | None = None
    max_queries: int | None = None
    max_keywords: int | None = None
    max_listings_per_query: int | None = None
    max_review_lookups: int | None = None
    max_videos_per_query: int | None = None
    max_quota_units: int | None = None
    max_channel_lookups: int | None = None


@dataclass(slots=True)
class CapabilityOutcome:
    """What one evidence capability actually achieved, reported honestly."""

    capability: str
    status: str
    provider: str | None = None
    snapshot_id: UUID | None = None
    provider_errors: list[str] = field(default_factory=list)
    missing_query_count: int = 0
    provider_call_count: int = 0
    provider_cost: float | None = None
    provider_cost_is_estimate: bool | None = None
    quota_units_used: int | None = None
    quota_units_is_exact: bool | None = None
    cached_query_count: int = 0
    failure_reason: str | None = None

    @property
    def produced_evidence(self) -> bool:
        return self.status in (
            SnapshotStatus.COMPLETE.value,
            SnapshotStatus.PARTIAL.value,
        )


@dataclass(slots=True)
class PreliminaryResearchResult:
    research_run_id: UUID
    candidate_count: int
    capabilities: list[CapabilityOutcome]
    profiles: list[CandidatePreliminaryProfile]
    ranking: PreliminaryRanking
    orchestration_version: str = ORCHESTRATION_VERSION
    dimensions_version: str = PRELIMINARY_DIMENSIONS_VERSION
    ranking_version: str = PRELIMINARY_RANKING_VERSION

    @property
    def selected_candidate_ids(self) -> list[UUID]:
        return [c.candidate_id for c in self.ranking.selected]


def _evidence_by_candidate(
    store: ResearchStore, snapshot_ids: list[UUID], candidates: list[Candidate]
) -> dict[UUID, list[EvidenceItem]]:
    """Resolve stored evidence back out of the append-only store.

    Reading from the store (rather than from in-flight objects) is what keeps
    the bridge working on evidence that already exists, and keeps each item's
    provenance, truth class, and source references intact.
    """
    grouped: dict[UUID, list[EvidenceItem]] = {c.id: [] for c in candidates}
    for snapshot_id in snapshot_ids:
        for item in store.evidence_for_snapshot(snapshot_id):
            if item.candidate_id in grouped:
                grouped[item.candidate_id].append(item)
    return grouped


async def _run_search_demand(
    candidates, provider, store, run_id, location, language, caps
) -> tuple[CapabilityOutcome, dict, UUID | None]:
    result = await run_search_demand_research(
        candidates=candidates,
        provider=provider,
        store=store,
        research_run_id=run_id,
        location=location,
        language=language,
        max_provider_calls=caps.max_provider_calls,
        max_keywords=caps.max_keywords,
    )
    snapshot = result.snapshot
    outcome = CapabilityOutcome(
        capability=CAPABILITY_SEARCH_DEMAND,
        status=snapshot.status.value,
        provider=snapshot.provider,
        snapshot_id=snapshot.snapshot_id,
        provider_errors=list(result.provider_errors),
        missing_query_count=len(result.missing_keywords),
        provider_call_count=snapshot.provider_call_count,
        provider_cost=snapshot.provider_cost,
        provider_cost_is_estimate=snapshot.provider_cost_is_estimate,
        cached_query_count=result.cached_keyword_count,
    )
    return outcome, result.summaries, snapshot.snapshot_id


async def _run_marketplace(
    candidates, provider, store, run_id, caps
) -> tuple[CapabilityOutcome, dict, UUID | None]:
    result = await run_marketplace_research(
        candidates=candidates,
        provider=provider,
        store=store,
        research_run_id=run_id,
        max_provider_calls=caps.max_provider_calls,
        max_queries=caps.max_queries,
        max_listings_per_query=caps.max_listings_per_query,
        max_review_lookups=caps.max_review_lookups,
    )
    snapshot = result.snapshot
    outcome = CapabilityOutcome(
        capability=CAPABILITY_MARKETPLACE,
        status=snapshot.status.value,
        provider=snapshot.provider,
        snapshot_id=snapshot.snapshot_id,
        provider_errors=list(result.provider_errors),
        missing_query_count=len(result.missing_queries),
        provider_call_count=snapshot.provider_call_count,
        provider_cost=snapshot.provider_cost,
        provider_cost_is_estimate=snapshot.provider_cost_is_estimate,
        cached_query_count=result.cached_query_count,
    )
    return outcome, result.summaries, snapshot.snapshot_id


async def _run_public_content(
    candidates, provider, store, run_id, caps
) -> tuple[CapabilityOutcome, dict, UUID | None]:
    result = await run_public_content_research(
        candidates=candidates,
        provider=provider,
        store=store,
        research_run_id=run_id,
        max_provider_calls=caps.max_provider_calls,
        max_queries=caps.max_queries,
        max_videos_per_query=caps.max_videos_per_query,
        max_quota_units=caps.max_quota_units,
        max_channel_lookups=caps.max_channel_lookups,
    )
    snapshot = result.snapshot
    outcome = CapabilityOutcome(
        capability=CAPABILITY_PUBLIC_CONTENT,
        status=snapshot.status.value,
        provider=snapshot.provider,
        snapshot_id=snapshot.snapshot_id,
        provider_errors=list(result.provider_errors),
        missing_query_count=len(result.missing_queries),
        provider_call_count=snapshot.provider_call_count,
        provider_cost=snapshot.provider_cost,
        provider_cost_is_estimate=snapshot.provider_cost_is_estimate,
        quota_units_used=result.quota_units_used,
        quota_units_is_exact=result.quota_units_is_exact,
        cached_query_count=result.cached_query_count,
    )
    return outcome, result.summaries, snapshot.snapshot_id


async def run_preliminary_research(
    candidates: list[Candidate],
    store: ResearchStore,
    search_demand_provider: SearchDemandProvider | None = None,
    marketplace_provider: MarketplaceProvider | None = None,
    public_content_provider: PublicContentProvider | None = None,
    research_run_id: UUID | None = None,
    location: str = "US",
    language: str = "en",
    search_demand_caps: CapabilityCaps | None = None,
    marketplace_caps: CapabilityCaps | None = None,
    public_content_caps: CapabilityCaps | None = None,
    selection_size: int = DEEP_RESEARCH_SELECTION_SIZE,
    unavailable_capabilities: dict[str, str] | None = None,
) -> PreliminaryResearchResult:
    """Coordinate every available evidence capability, then rank deterministically.

    A provider passed as None is simply not requested. A provider that fails
    is recorded and the run continues on whatever other evidence exists.
    `unavailable_capabilities` maps a capability name to the reason its
    provider could not even be constructed (e.g. missing credentials), so a
    setup failure is reported as honestly as a call failure.
    """
    unavailable = unavailable_capabilities or {}
    run_id = research_run_id or uuid4()
    outcomes: list[CapabilityOutcome] = []
    snapshot_ids: list[UUID] = []
    search_summaries: dict = {}
    marketplace_summaries: dict = {}
    content_summaries: dict = {}

    for capability in CAPABILITY_ORDER:
        if capability == CAPABILITY_SEARCH_DEMAND:
            provider = search_demand_provider
            caps = search_demand_caps or CapabilityCaps()
        elif capability == CAPABILITY_MARKETPLACE:
            provider = marketplace_provider
            caps = marketplace_caps or CapabilityCaps()
        else:
            provider = public_content_provider
            caps = public_content_caps or CapabilityCaps()

        if capability in unavailable:
            outcomes.append(
                CapabilityOutcome(
                    capability=capability,
                    status=STATUS_PROVIDER_FAILED,
                    failure_reason=unavailable[capability],
                )
            )
            continue

        if provider is None:
            outcomes.append(
                CapabilityOutcome(
                    capability=capability,
                    status=STATUS_NOT_REQUESTED,
                    failure_reason=MISSING_CAPABILITY_NOT_REQUESTED,
                )
            )
            continue

        # A provider failure degrades this capability only. Every other
        # capability's evidence, and the run itself, survive intact.
        try:
            if capability == CAPABILITY_SEARCH_DEMAND:
                outcome, summaries, snapshot_id = await _run_search_demand(
                    candidates, provider, store, run_id, location, language, caps
                )
                search_summaries = summaries
            elif capability == CAPABILITY_MARKETPLACE:
                outcome, summaries, snapshot_id = await _run_marketplace(
                    candidates, provider, store, run_id, caps
                )
                marketplace_summaries = summaries
            else:
                outcome, summaries, snapshot_id = await _run_public_content(
                    candidates, provider, store, run_id, caps
                )
                content_summaries = summaries
        except ProviderError as exc:
            outcomes.append(
                CapabilityOutcome(
                    capability=capability,
                    status=STATUS_PROVIDER_FAILED,
                    provider=getattr(provider, "name", None),
                    failure_reason=f"{type(exc).__name__}: {exc}",
                )
            )
            continue

        outcomes.append(outcome)
        if snapshot_id is not None:
            snapshot_ids.append(snapshot_id)

    evidence = _evidence_by_candidate(store, snapshot_ids, candidates)

    failed_capabilities = {
        o.capability for o in outcomes if not o.produced_evidence
    }
    missing_reasons = _missing_reasons(failed_capabilities, outcomes)
    profiles = [
        build_preliminary_profile(
            candidate_id=candidate.id,
            candidate_title=candidate.title,
            evidence=evidence[candidate.id],
            search_demand=search_summaries.get(candidate.id),
            marketplace=marketplace_summaries.get(candidate.id),
            public_content=content_summaries.get(candidate.id),
            missing_reasons=missing_reasons,
        )
        for candidate in candidates
    ]

    ranking = rank_candidates(profiles, selection_size=selection_size)

    return PreliminaryResearchResult(
        research_run_id=run_id,
        candidate_count=len(candidates),
        capabilities=outcomes,
        profiles=profiles,
        ranking=ranking,
    )


def _missing_reasons(
    failed_capabilities: set[str], outcomes: list[CapabilityOutcome]
) -> dict[str, str]:
    """Name why a dimension has no evidence, per capability that produced none."""
    by_capability = {o.capability: o for o in outcomes}
    reasons: dict[str, str] = {}
    mapping = {
        CAPABILITY_SEARCH_DEMAND: DIM_SEARCH_DEMAND,
        CAPABILITY_MARKETPLACE: DIM_PRICE_EVIDENCE,
        CAPABILITY_PUBLIC_CONTENT: DIM_AUDIENCE_INTEREST,
    }
    for capability, dimension in mapping.items():
        if capability not in failed_capabilities:
            reasons[dimension] = MISSING_NO_EVIDENCE_FOR_CANDIDATE
            continue
        outcome = by_capability.get(capability)
        if outcome is None:
            continue
        if outcome.status == STATUS_NOT_REQUESTED:
            reasons[dimension] = MISSING_CAPABILITY_NOT_REQUESTED
        else:
            reasons[dimension] = MISSING_CAPABILITY_FAILED
    return reasons
