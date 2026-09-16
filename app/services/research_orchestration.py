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

Each capability invocation is wrapped in a defensive boundary with two arms:

- ProviderError, anticipated by the provider contract, is reported as
  PROVIDER_FAILED exactly as before.
- Any other Exception — an adapter bug, a library error, anything
  unforeseen — is reported as UNEXPECTED_PROVIDER_ERROR with the capability
  name, exception type, a sanitized message, and a correlation id, while the
  full traceback goes to the server-side log. The remaining capabilities
  still run, and evidence already collected is preserved.

This is the only place in the application that catches Exception broadly.
BaseException still propagates, so cancellation and shutdown are never
mistaken for a provider failure. A failed capability contributes no
evidence, no fabricated value, and no zero substitution.
"""

import logging
import os
import re
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
from app.services.buyer_reach import BuyerReachResult, extract_buyer_reach
from app.services.audience_attention import (
    AudienceAttentionResult,
    extract_audience_attention,
)
from app.services.competition_opportunity import (
    CompetitionOpportunityResult,
    extract_competition_opportunity,
)
from app.services.price_evidence import (
    PriceEvidenceResult,
    extract_price_evidence,
)
from app.services.purchase_evidence import (
    PurchaseEvidenceResult,
    extract_purchase_evidence,
)
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

logger = logging.getLogger(__name__)

# Capability statuses beyond the snapshot statuses.
STATUS_NOT_REQUESTED = "NOT_REQUESTED"
# The provider failed in a way its own contract anticipates (ProviderError).
STATUS_PROVIDER_FAILED = "PROVIDER_FAILED"
# The provider raised something outside its contract — an adapter bug, a
# library error, anything unforeseen. Kept distinct from PROVIDER_FAILED so
# an operator can tell a handled provider failure from a defect.
STATUS_UNEXPECTED_PROVIDER_ERROR = "UNEXPECTED_PROVIDER_ERROR"

# A derivation is not a provider: it makes no calls and spends no quota, so
# it reports its own outcome rather than borrowing a capability status that
# would imply a provider was involved.
DERIVATION_PURCHASE_EVIDENCE = "purchase_evidence"
DERIVATION_PRICE_EVIDENCE = "price_evidence"
DERIVATION_BUYER_REACH = "buyer_reach"
DERIVATION_COMPETITION_OPPORTUNITY = "competition_opportunity"
DERIVATION_AUDIENCE_ATTENTION = "audience_attention"
STATUS_DERIVATION_COMPLETE = "COMPLETE"
STATUS_DERIVATION_NOT_REQUESTED = "NOT_REQUESTED"
STATUS_DERIVATION_ERROR = "DERIVATION_ERROR"

MISSING_CAPABILITY_NOT_REQUESTED = "capability_not_requested"
MISSING_CAPABILITY_FAILED = "capability_provider_failed"
MISSING_CAPABILITY_UNEXPECTED_ERROR = "capability_unexpected_error"
MISSING_NO_EVIDENCE_FOR_CANDIDATE = "no_evidence_returned_for_candidate"

# Longest sanitized message surfaced through the API. An unexpected
# exception's text is arbitrary; bounding it keeps a runaway message (or a
# formatted traceback) out of the response.
MAX_SANITIZED_ERROR_LENGTH = 200

# Environment variables holding provider credentials. If a raw credential
# value ever appears inside an exception message, it is replaced before the
# message reaches a response.
CREDENTIAL_ENV_VARS = (
    "DATAFORSEO_LOGIN",
    "DATAFORSEO_PASSWORD",
    "ETSY_API_KEY",
    "YOUTUBE_API_KEY",
)

REDACTED = "[REDACTED]"

# Secret-shaped text, each paired with what replaces it. The key or scheme
# is kept so a reader can tell what was redacted; the value never is.
# Order matters. The auth-scheme rule runs before the key=value rule:
# "Authorization: Bearer <token>" would otherwise have "Bearer" consumed as
# the value, leaving the token itself in the text.
_SECRET_SUBSTITUTIONS = (
    # Authorization header values
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]+"), r"\1 " + REDACTED),
    # URL userinfo: scheme://user:pass@host
    (re.compile(r"(?<=://)[^/\s:@]+:[^/\s@]+(?=@)"), REDACTED),
    # key=value / key: value query params and kwargs
    (
        re.compile(
            r"(?i)\b(api[_-]?key|apikey|key|access[_-]?token|token|password|passwd"
            r"|secret|auth|authorization|signature|sig)\s*[=:]\s*[^\s&,;'\"\)]+"
        ),
        r"\1=" + REDACTED,
    ),
)

_WHITESPACE = re.compile(r"\s+")

# Traceback-shaped text embedded in an exception message. Real exceptions do
# carry this (a wrapper re-raising a formatted trace, a subprocess error), and
# it exposes internal file paths and line numbers.
_TRACEBACK_MARKER = re.compile(
    r"(?i)(traceback \(most recent call last\)|\bFile \"[^\"]*\", line \d+)"
)

TRACE_OMITTED = "[trace omitted]"


def safe_exception_message(exc: BaseException) -> str:
    """Render an exception's message without trusting its __str__.

    A pathological exception whose __str__ raises would otherwise escape the
    very boundary meant to contain it. The type name is always available, so
    a failed render degrades to a placeholder rather than a lost run.
    """
    try:
        return str(exc)
    except Exception:  # noqa: BLE001 - a broken __str__ must not defeat the boundary
        return "<exception message could not be rendered>"


def sanitize_error_message(message: str) -> str:
    """Strip anything secret-shaped out of an arbitrary exception message.

    Applied only to unexpected exceptions, whose text answers to no contract
    and may embed a request URL, an auth header, or a raw credential.
    Newlines collapse to spaces, traceback-shaped text is cut at its first
    marker so internal paths and line numbers never survive, and the result
    is length-bounded.

    This is defence in depth, not a guarantee: an adapter that formats a
    secret into an exception in some shape not matched here would still leak
    it. Adapters must not put credentials in exception text in the first
    place; app/providers/* is written that way and tested for it.
    """
    text = _WHITESPACE.sub(" ", message).strip()

    # Cut at the first traceback marker: everything after it is internal
    # structure (file paths, line numbers), never information a caller needs.
    marker = _TRACEBACK_MARKER.search(text)
    if marker:
        text = (text[: marker.start()].strip() + " " + TRACE_OMITTED).strip()

    # Literal credential values first: the most direct leak, and one the
    # pattern rules would miss if the value appears on its own.
    for env_var in CREDENTIAL_ENV_VARS:
        value = os.environ.get(env_var, "").strip()
        if value:
            text = text.replace(value, REDACTED)

    for pattern, replacement in _SECRET_SUBSTITUTIONS:
        text = pattern.sub(replacement, text)

    if len(text) > MAX_SANITIZED_ERROR_LENGTH:
        text = text[:MAX_SANITIZED_ERROR_LENGTH] + "..."
    return text


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
    # A true cache hit: an entry that was present AND collected at least as
    # deep as this pass asked for.
    cached_query_count: int = 0
    # An entry that was present but too shallow, so it was re-fetched.
    # SPEC_STEP_7_8.md §1.1 requires these be distinguishable: counting a
    # depth miss as a hit would hide a deep pass that only looked deep.
    # Always 0 for search demand, whose keyword cache is depth-independent.
    depth_miss_query_count: int = 0
    failure_reason: str | None = None

    @property
    def produced_evidence(self) -> bool:
        return self.status in (
            SnapshotStatus.COMPLETE.value,
            SnapshotStatus.PARTIAL.value,
        )


@dataclass(slots=True)
class DerivationOutcome:
    """What one deterministic derivation over stored evidence achieved.

    Derivations make no provider calls, so there is no provider, cost, or
    quota to report — only whether the derivation ran, and why not if it
    did not. `failure_reason` on an error carries the exception type, a
    sanitized message, and a correlation id matching the server-side log.
    """

    derivation: str
    status: str
    candidates_covered: int = 0
    failure_reason: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.status == STATUS_DERIVATION_COMPLETE


@dataclass(slots=True)
class PreliminaryResearchResult:
    research_run_id: UUID
    candidate_count: int
    capabilities: list[CapabilityOutcome]
    profiles: list[CandidatePreliminaryProfile]
    ranking: PreliminaryRanking
    # Milestone 4A: Purchase Evidence derived for the selected candidates
    # only, from evidence already collected. Empty when not derived.
    purchase_evidence: dict[UUID, PurchaseEvidenceResult] = field(default_factory=dict)
    # Milestone 4B: Price Evidence, derived for the selected candidates only.
    price_evidence: dict[UUID, PriceEvidenceResult] = field(default_factory=dict)
    buyer_reach: dict[UUID, BuyerReachResult] = field(default_factory=dict)
    competition_opportunity: dict[UUID, CompetitionOpportunityResult] = field(
        default_factory=dict
    )
    audience_attention: dict[UUID, AudienceAttentionResult] = field(
        default_factory=dict
    )
    derivations: list[DerivationOutcome] = field(default_factory=list)
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


async def run_search_demand_capability(
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
        # The keyword cache is keyed per keyword and depth in search demand
        # means more keywords queried, not more results per keyword, so a
        # cached metric is equally valid at any depth (§1.1).
        depth_miss_query_count=0,
    )
    return outcome, result.summaries, snapshot.snapshot_id


async def run_marketplace_capability(
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
        depth_miss_query_count=result.depth_miss_query_count,
    )
    return outcome, result.summaries, snapshot.snapshot_id


async def run_public_content_capability(
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
        depth_miss_query_count=result.depth_miss_query_count,
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
    derive_purchase_evidence: bool = True,
    derive_price_evidence: bool = True,
    derive_buyer_reach: bool = True,
    derive_competition_opportunity: bool = True,
    derive_audience_attention: bool = True,
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
                outcome, summaries, snapshot_id = await run_search_demand_capability(
                    candidates, provider, store, run_id, location, language, caps
                )
                search_summaries = summaries
            elif capability == CAPABILITY_MARKETPLACE:
                outcome, summaries, snapshot_id = await run_marketplace_capability(
                    candidates, provider, store, run_id, caps
                )
                marketplace_summaries = summaries
            else:
                outcome, summaries, snapshot_id = await run_public_content_capability(
                    candidates, provider, store, run_id, caps
                )
                content_summaries = summaries
        except ProviderError as exc:
            # Anticipated by the provider contract; adapters are written and
            # tested not to put credentials in these messages, so the
            # existing reporting is unchanged.
            outcomes.append(
                CapabilityOutcome(
                    capability=capability,
                    status=STATUS_PROVIDER_FAILED,
                    provider=getattr(provider, "name", None),
                    failure_reason=f"{type(exc).__name__}: {exc}",
                )
            )
            continue
        except Exception as exc:  # noqa: BLE001 - deliberate capability boundary
            # Outside the provider contract: an adapter bug, a library
            # error, anything unforeseen. This is the ONLY place in the
            # application that catches broadly, and it exists so one
            # defective provider cannot destroy a run that other providers
            # have already contributed valid evidence to.
            #
            # BaseException (KeyboardInterrupt, SystemExit, and
            # asyncio.CancelledError) deliberately propagates: shutdown and
            # task cancellation must not be swallowed as a provider failure.
            #
            # Nothing here becomes evidence. The capability is recorded as
            # failed, its dimensions stay MISSING with a stated reason, and
            # no value is fabricated or defaulted to zero.
            error_id = uuid4()
            logger.exception(
                "Unexpected error in %s capability (provider=%s, error_id=%s)",
                capability,
                getattr(provider, "name", None),
                error_id,
            )
            outcomes.append(
                CapabilityOutcome(
                    capability=capability,
                    status=STATUS_UNEXPECTED_PROVIDER_ERROR,
                    provider=getattr(provider, "name", None),
                    failure_reason=(
                        f"{type(exc).__name__}: "
                        f"{sanitize_error_message(safe_exception_message(exc))} "
                        f"(error_id={error_id})"
                    ),
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

    # --- Milestone 4A: Purchase Evidence derivation -----------------------
    #
    # A pure derivation over evidence already collected: no provider calls,
    # no quota, no second marketplace research system. It runs only for the
    # selected candidates, since Purchase Evidence exists to inform the
    # later deep-research stage.
    #
    # It inherits the capability boundary: a failure here degrades this
    # derivation alone. Every dimension from 3C, and the ranking itself,
    # survive intact, and nothing is fabricated or defaulted to zero.
    def run_derivation(name, requested, extractor):
        """Run one derivation over already-collected evidence, safely.

        Same two-arm contract as the capability boundary above: a failure
        degrades this derivation alone, partial results are discarded rather
        than reported as complete, BaseException still propagates, the full
        traceback goes to the server-side log, and only a sanitized message
        with a correlation id is exposed.
        """
        if not requested:
            derivations.append(
                DerivationOutcome(derivation=name, status=STATUS_DERIVATION_NOT_REQUESTED)
            )
            return {}
        try:
            derived = {
                ranked.candidate_id: extractor(
                    candidate_id=ranked.candidate_id,
                    evidence=evidence[ranked.candidate_id],
                )
                for ranked in ranking.selected
            }
        except Exception as exc:  # noqa: BLE001 - deliberate derivation boundary
            error_id = uuid4()
            logger.exception(
                "Unexpected error in %s derivation (error_id=%s)", name, error_id
            )
            derivations.append(
                DerivationOutcome(
                    derivation=name,
                    status=STATUS_DERIVATION_ERROR,
                    failure_reason=(
                        f"{type(exc).__name__}: "
                        f"{sanitize_error_message(safe_exception_message(exc))} "
                        f"(error_id={error_id})"
                    ),
                )
            )
            return {}
        derivations.append(
            DerivationOutcome(
                derivation=name,
                status=STATUS_DERIVATION_COMPLETE,
                candidates_covered=len(derived),
            )
        )
        return derived

    derivations: list[DerivationOutcome] = []
    # Each derivation is independent: one failing must not stop the other.
    purchase_evidence = run_derivation(
        DERIVATION_PURCHASE_EVIDENCE, derive_purchase_evidence, extract_purchase_evidence
    )
    price_evidence = run_derivation(
        DERIVATION_PRICE_EVIDENCE, derive_price_evidence, extract_price_evidence
    )

    # Milestones 4D, 4E and 4F need one thing 4A and 4B do not: WHY a capability
    # produced nothing. Without it a failed capability would read as an absent
    # channel, an empty competitive field, or an absence of attention, and
    # missing evidence would silently become zero. One map serves all three,
    # so they can never disagree
    # about what a capability reason is. It is bound here rather than by
    # widening the generic runner, so 4A and 4B keep their two-argument
    # contract.
    capability_missing_reasons = _capability_missing_reasons(
        failed_capabilities, outcomes
    )

    def extract_reach(candidate_id, evidence):
        return extract_buyer_reach(
            candidate_id=candidate_id,
            evidence=evidence,
            missing_reasons=capability_missing_reasons,
            research_run_id=run_id,
        )

    buyer_reach = run_derivation(
        DERIVATION_BUYER_REACH, derive_buyer_reach, extract_reach
    )

    # 4E consumes only the marketplace capability, but takes the whole map:
    # one source of truth for both derivations.
    def extract_competition(candidate_id, evidence):
        return extract_competition_opportunity(
            candidate_id=candidate_id,
            evidence=evidence,
            missing_reasons=capability_missing_reasons,
        )

    competition_opportunity = run_derivation(
        DERIVATION_COMPETITION_OPPORTUNITY,
        derive_competition_opportunity,
        extract_competition,
    )

    # 4F consumes only the public-content capability, from the same map, so a
    # failed content call cannot read as an absence of attention.
    def extract_attention(candidate_id, evidence):
        return extract_audience_attention(
            candidate_id=candidate_id,
            evidence=evidence,
            missing_reasons=capability_missing_reasons,
        )

    audience_attention = run_derivation(
        DERIVATION_AUDIENCE_ATTENTION, derive_audience_attention, extract_attention
    )

    return PreliminaryResearchResult(
        research_run_id=run_id,
        candidate_count=len(candidates),
        capabilities=outcomes,
        profiles=profiles,
        ranking=ranking,
        purchase_evidence=purchase_evidence,
        price_evidence=price_evidence,
        buyer_reach=buyer_reach,
        competition_opportunity=competition_opportunity,
        audience_attention=audience_attention,
        derivations=derivations,
    )


def _capability_missing_reasons(
    failed_capabilities: set[str], outcomes: list[CapabilityOutcome]
) -> dict[str, str]:
    """Why each capability produced no evidence, keyed by CAPABILITY name.

    `_missing_reasons` answers the same question keyed by preliminary
    DIMENSION name, which is what 3C's profile builder needs. Buyer Reach
    reasons about capabilities directly — a failed marketplace call means an
    unknown marketplace channel — so it needs the capability key instead.
    Both read the same outcomes; neither changes the other.
    """
    by_capability = {o.capability: o for o in outcomes}
    reasons: dict[str, str] = {}
    for capability in CAPABILITY_ORDER:
        if capability not in failed_capabilities:
            continue
        outcome = by_capability.get(capability)
        if outcome is None:
            continue
        if outcome.status == STATUS_NOT_REQUESTED:
            reasons[capability] = MISSING_CAPABILITY_NOT_REQUESTED
        elif outcome.status == STATUS_UNEXPECTED_PROVIDER_ERROR:
            reasons[capability] = MISSING_CAPABILITY_UNEXPECTED_ERROR
        else:
            reasons[capability] = MISSING_CAPABILITY_FAILED
    return reasons


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
        elif outcome.status == STATUS_UNEXPECTED_PROVIDER_ERROR:
            reasons[dimension] = MISSING_CAPABILITY_UNEXPECTED_ERROR
        else:
            reasons[dimension] = MISSING_CAPABILITY_FAILED
    return reasons
