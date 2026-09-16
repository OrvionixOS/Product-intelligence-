"""Marketplace research orchestration (Milestone 3A — Etsy).

candidate list -> collect marketplace queries -> normalize + dedupe
-> capped provider searches -> listing dedupe by listing_id
-> capped review-stats enrichment -> immutable evidence records
-> evidence snapshot -> deterministic candidate summaries.

Truth model enforced here:

- Public fields Etsy actually returned are truth_class=OBSERVED.
- A review count is OBSERVED marketplace data but only a PURCHASE PROXY:
  it is never converted into units sold or revenue.
- Exact competitor sales/revenue are not public and remain UNKNOWN.
- Fields the provider did not return produce UNKNOWN evidence or stay None;
  missing data is reported, never substituted with a guess.

A failed provider request degrades the run to PARTIAL/FAILED with explicit
missing-query records — it never destroys the research run.
"""

import hashlib
import json
import os
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from uuid import UUID, uuid4

from app.domain.enums import (
    COLLECTION_METHOD_CACHE,
    EvidencePurpose,
    SnapshotStatus,
    TruthClass,
)
from app.domain.models import Candidate, EvidenceItem, EvidenceSnapshot
from app.providers.base import (
    MarketplaceListing,
    MarketplaceProvider,
    ProviderError,
)
from app.services.query_provenance import resolve_provenance
from app.services.marketplace_features import (
    MarketplaceCandidateSummary,
    summarize_marketplace,
)
from app.services.search_demand import normalize_query
from app.storage.memory import CacheOutcome, ResearchStore

NORMALIZATION_VERSION = "marketplace_norm_v1"

# Marketplace evidence is not geography-filtered in V1: Etsy search returns
# globally visible listings.
MARKETPLACE_GEOGRAPHY = "GLOBAL"

PRICE_LIMITATIONS = [
    "Listing price is the public asking price, not a transaction price.",
    "Provider-supplied listing data: OBSERVED means the field was returned by the marketplace API, not independently audited.",
]

PURCHASE_PROXY_LIMITATIONS = [
    "Review count is a PURCHASE PROXY only: it is never units sold, buyers, or revenue.",
    "Exact competitor sales and revenue are not public and remain UNKNOWN.",
    "Not every purchase produces a review, and review timing lags purchases.",
]

COMPETITION_LIMITATIONS = [
    "Listing presence shows the observable competitive field, not competitor performance.",
    "Search-ranked results are a provider-ordered sample, not the full market.",
]

ENV_MAX_PROVIDER_CALLS = "MARKETPLACE_MAX_PROVIDER_CALLS"
ENV_MAX_QUERIES = "MARKETPLACE_MAX_QUERIES"
ENV_MAX_LISTINGS_PER_QUERY = "MARKETPLACE_MAX_LISTINGS_PER_QUERY"
ENV_MAX_REVIEW_LOOKUPS = "MARKETPLACE_MAX_REVIEW_LOOKUPS"
DEFAULT_MAX_PROVIDER_CALLS = 10
DEFAULT_MAX_QUERIES = 25
DEFAULT_MAX_LISTINGS_PER_QUERY = 25
DEFAULT_MAX_REVIEW_LOOKUPS = 20

# Reasons a requested marketplace query ended up without listings.
MISSING_QUERY_CAP = "query_cap_reached"
MISSING_BUDGET = "provider_call_budget_exhausted"
MISSING_PROVIDER_ERROR = "provider_error"
MISSING_NO_RESULTS = "no_listings_returned"

# Reason review data is absent for a listing that was found.
REVIEWS_NOT_FETCHED = "review_lookup_cap_reached"


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
class MarketplaceQueryPlan:
    """Deduplicated marketplace queries with candidate relationships preserved.

    One query can legitimately support multiple candidates.
    """

    queries: list[str]
    query_to_candidates: dict[str, list[UUID]]


def plan_marketplace_queries(candidates: list[Candidate]) -> MarketplaceQueryPlan:
    queries: list[str] = []
    mapping: dict[str, list[UUID]] = {}
    for candidate in candidates:
        for raw_query in candidate.marketplace_queries:
            query = normalize_query(raw_query)
            if not query:
                continue
            if query not in mapping:
                mapping[query] = []
                queries.append(query)
            if candidate.id not in mapping[query]:
                mapping[query].append(candidate.id)
    return MarketplaceQueryPlan(queries=queries, query_to_candidates=mapping)


@dataclass(slots=True)
class MissingQuery:
    query: str
    reason: str
    candidate_ids: list[UUID]


@dataclass(slots=True)
class MarketplaceRunResult:
    snapshot: EvidenceSnapshot
    summaries: dict[UUID, MarketplaceCandidateSummary]
    evidence_ids_by_candidate: dict[UUID, list[UUID]]
    provider_errors: list[str] = field(default_factory=list)
    missing_queries: list[MissingQuery] = field(default_factory=list)
    # A true cache hit: entry present AND collected at least as deep as this
    # request. SPEC_STEP_7_8.md §1.1 forbids counting a depth miss here.
    cached_query_count: int = 0
    # Entry present but collected under a smaller limit, so it was re-fetched.
    # Tracked separately so a deep pass that looks suspiciously cheap can be
    # diagnosed rather than trusted.
    depth_miss_query_count: int = 0
    unique_listing_count: int = 0
    review_lookups_performed: int = 0
    review_lookups_skipped: int = 0


def build_listing_evidence(
    candidate_id: UUID,
    research_run_id: UUID,
    snapshot_id: UUID,
    listing: MarketplaceListing,
    provider: MarketplaceProvider,
    source_reference: str | None,
    reviews_looked_up: bool,
    originating_queries: tuple[str, ...] | None = None,
    originating_query_shared: bool | None = None,
    collected_live: bool = True,
) -> list[EvidenceItem]:
    """Immutable evidence for one (candidate, listing) pair.

    Three classifications per listing, each reflecting only what the field
    actually proves:

    - PRICE: the public asking price (OBSERVED when returned).
    - PURCHASE: the review count, explicitly a proxy (OBSERVED when a real
      lookup returned it; UNKNOWN otherwise). Never converted to sales.
    - COMPETITION: the listing's presence in the searched field (OBSERVED).
    """
    payload = asdict(listing)
    payload_hash = _payload_hash(payload)
    common = dict(
        candidate_id=candidate_id,
        research_run_id=research_run_id,
        snapshot_id=snapshot_id,
        provider=provider.name,
        # Cache reuse is stated, not hidden behind the provider's own method.
        # Reporting a reused entry as a live API observation over-credits its
        # provenance, which Evidence Confidence then reads as directness it
        # never had (SPEC_STEP_7_8.md §1.1, "interaction with provenance").
        collection_method=(
            provider.collection_method if collected_live else COLLECTION_METHOD_CACHE
        ),
        source_reference=source_reference,
        source_url=listing.url,
        retrieved_at=listing.retrieved_at,
        geography=None,
        marketplace=provider.name,
        normalization_version=NORMALIZATION_VERSION,
        raw_payload=payload,
        raw_payload_hash=payload_hash,
        # Retrieval metadata only. Deliberately NOT part of `payload`, so it
        # cannot alter payload_hash or any downstream dedupe, and it never
        # affects the truth classes chosen below.
        originating_queries=originating_queries,
        originating_query_shared=originating_query_shared,
    )

    price_observed = listing.price is not None
    price_item = EvidenceItem(
        signal_type="marketplace_listing_price",
        purpose=EvidencePurpose.PRICE,
        truth_class=TruthClass.OBSERVED if price_observed else TruthClass.UNKNOWN,
        raw_value=listing.price,
        unit=listing.currency if price_observed else None,
        known_limitations=list(PRICE_LIMITATIONS),
        **common,
    )

    review_observed = reviews_looked_up and listing.review_count is not None
    review_limitations = list(PURCHASE_PROXY_LIMITATIONS)
    if not reviews_looked_up:
        review_limitations.append(
            "Review count was not fetched for this listing (lookup cap); it is UNKNOWN, not zero."
        )
    purchase_proxy_item = EvidenceItem(
        signal_type="marketplace_review_count_purchase_proxy",
        purpose=EvidencePurpose.PURCHASE,
        truth_class=TruthClass.OBSERVED if review_observed else TruthClass.UNKNOWN,
        raw_value=listing.review_count if review_observed else None,
        unit="reviews" if review_observed else None,
        # A review is one step removed from the purchase itself.
        directness=0.4,
        known_limitations=review_limitations,
        **common,
    )

    competition_item = EvidenceItem(
        signal_type="marketplace_competing_listing",
        purpose=EvidencePurpose.COMPETITION,
        truth_class=TruthClass.OBSERVED,
        raw_value=None,
        unit=None,
        known_limitations=list(COMPETITION_LIMITATIONS),
        **common,
    )

    return [price_item, purchase_proxy_item, competition_item]


async def run_marketplace_research(
    candidates: list[Candidate],
    provider: MarketplaceProvider,
    store: ResearchStore,
    research_run_id: UUID | None = None,
    max_provider_calls: int | None = None,
    max_queries: int | None = None,
    max_listings_per_query: int | None = None,
    max_review_lookups: int | None = None,
) -> MarketplaceRunResult:
    """Run one marketplace research pass and persist an immutable snapshot."""
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
    listings_cap = (
        max_listings_per_query
        if max_listings_per_query is not None
        else _env_int(ENV_MAX_LISTINGS_PER_QUERY, DEFAULT_MAX_LISTINGS_PER_QUERY)
    )
    review_lookup_budget = (
        max_review_lookups
        if max_review_lookups is not None
        else _env_int(ENV_MAX_REVIEW_LOOKUPS, DEFAULT_MAX_REVIEW_LOOKUPS)
    )

    plan = plan_marketplace_queries(candidates)
    missing: list[MissingQuery] = []
    provider_errors: list[str] = []

    requested = plan.queries[:query_cap]
    for query in plan.queries[query_cap:]:
        missing.append(MissingQuery(query, MISSING_QUERY_CAP, plan.query_to_candidates[query]))

    # Cache first: identical (provider, query) result sets within the
    # freshness window are reused instead of re-fetched -- but only when the
    # entry was collected at least as deep as this request asks for (§1.1).
    listings_by_query: dict[str, list[MarketplaceListing]] = {}
    to_fetch: list[str] = []
    cached_count = 0
    depth_miss_count = 0
    reused_queries: set[str] = set()
    for query in requested:
        lookup = store.cached_listings(provider.name, query, listings_cap)
        if lookup.outcome is CacheOutcome.HIT:
            listings_by_query[query] = lookup.items or []
            reused_queries.add(query)
            cached_count += 1
            continue
        if lookup.outcome is CacheOutcome.DEPTH_MISS:
            # Shallower than requested. Re-fetch rather than pass the shallow
            # set off as deep evidence.
            depth_miss_count += 1
        to_fetch.append(query)

    call_count = 0
    provider_version: str | None = None
    source_reference: str | None = None
    fetched_queries: list[str] = []

    for query in to_fetch:
        if call_count >= call_budget:
            missing.append(MissingQuery(query, MISSING_BUDGET, plan.query_to_candidates[query]))
            continue
        call_count += 1
        try:
            result = await provider.search_listings(query, limit=listings_cap)
        except ProviderError as exc:
            provider_errors.append(f"{type(exc).__name__}: {exc}")
            missing.append(
                MissingQuery(query, MISSING_PROVIDER_ERROR, plan.query_to_candidates[query])
            )
            continue

        provider_errors.extend(result.errors)
        provider_version = result.provider_version or provider_version
        source_reference = result.source_reference or source_reference
        listings_by_query[query] = result.listings
        fetched_queries.append(query)
        if not result.listings:
            missing.append(
                MissingQuery(query, MISSING_NO_RESULTS, plan.query_to_candidates[query])
            )

    # Dedupe listings across queries by listing_id: one canonical observation
    # per listing, shared by every candidate whose query matched it.
    #
    # listing_to_queries records HOW each listing was found (Milestone 4D-0).
    # It is captured here because this is the last point at which the query
    # is still in scope; the evidence loop below iterates the deduplicated
    # listings alone. Provenance is kept out of the payload and out of
    # raw_payload_hash, so one listing found by two queries is still one
    # listing with one hash.
    canonical: dict[str, MarketplaceListing] = {}
    listing_to_candidates: dict[str, list[UUID]] = {}
    listing_to_queries: dict[str, list[str]] = {}
    listing_order: list[str] = []
    for query in requested:
        for listing in listings_by_query.get(query, []):
            if listing.listing_id not in canonical:
                canonical[listing.listing_id] = listing
                listing_to_candidates[listing.listing_id] = []
                listing_to_queries[listing.listing_id] = []
                listing_order.append(listing.listing_id)
            if query not in listing_to_queries[listing.listing_id]:
                listing_to_queries[listing.listing_id].append(query)
            for candidate_id in plan.query_to_candidates[query]:
                if candidate_id not in listing_to_candidates[listing.listing_id]:
                    listing_to_candidates[listing.listing_id].append(candidate_id)

    # Capped review-stats enrichment over deduplicated listings only: the
    # same listing is never looked up twice, even when shared by candidates.
    reviews_fetched: set[str] = set()
    review_lookups = 0
    review_skipped = 0
    if provider.supports_review_stats:
        for listing_id in listing_order:
            listing = canonical[listing_id]
            if listing.review_count is not None:
                reviews_fetched.add(listing_id)  # cache already carried review data
                continue
            if review_lookups >= review_lookup_budget or call_count + 1 > call_budget:
                review_skipped += 1
                continue
            review_lookups += 1
            call_count += 1
            try:
                stats = await provider.fetch_review_stats(listing_id)
            except ProviderError as exc:
                provider_errors.append(f"{type(exc).__name__}: {exc}")
                continue
            canonical[listing_id] = replace(listing, review_count=stats.review_count)
            reviews_fetched.add(listing_id)
    else:
        review_skipped = len(listing_order)

    # Cache final (enriched) listings per fetched query so a later run within
    # the freshness window spends no calls at all.
    for query in fetched_queries:
        store.cache_listings(
            provider.name,
            query,
            [canonical[listing.listing_id] for listing in listings_by_query[query]],
            effective_limit=listings_cap,
        )

    # Build immutable evidence: price + purchase-proxy + competition records
    # per (candidate, listing); the underlying observation (payload and hash)
    # is identical for every candidate sharing the listing.
    evidence_ids: dict[UUID, list[UUID]] = {c.id: [] for c in candidates}
    listings_by_candidate: dict[UUID, list[MarketplaceListing]] = {c.id: [] for c in candidates}
    # A listing reached by at least one live fetch in this pass IS a live
    # observation, whatever else also returned it. Only a listing seen solely
    # through reused cache entries is cache-sourced.
    live_listing_ids = {
        listing.listing_id
        for query in fetched_queries
        for listing in listings_by_query.get(query, [])
    }
    evidence_items: list[EvidenceItem] = []
    for listing_id in listing_order:
        listing = canonical[listing_id]
        for candidate_id in listing_to_candidates[listing_id]:
            # Candidate-filtered: another candidate's query never lands on
            # this candidate's evidence just because both queries returned
            # the same listing.
            originating_queries, query_shared = resolve_provenance(
                candidate_id,
                listing_to_queries[listing_id],
                plan.query_to_candidates,
            )
            items = build_listing_evidence(
                candidate_id,
                run_id,
                snapshot_id,
                listing,
                provider,
                source_reference,
                reviews_looked_up=listing_id in reviews_fetched,
                originating_queries=originating_queries,
                originating_query_shared=query_shared,
                collected_live=listing_id in live_listing_ids,
            )
            evidence_items.extend(items)
            evidence_ids[candidate_id].extend(item.id for item in items)
            listings_by_candidate[candidate_id].append(listing)

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
        geography=MARKETPLACE_GEOGRAPHY,
        language="all",
        status=status,
        provider_call_count=call_count,
        provider_cost=None,  # Etsy public API is quota-limited, not per-call priced
        provider_cost_is_estimate=None,
        normalization_version=NORMALIZATION_VERSION,
    )
    store.add_snapshot(snapshot)
    for item in evidence_items:
        store.add_evidence(item)

    summaries = {
        candidate.id: summarize_marketplace(candidate.id, listings_by_candidate[candidate.id])
        for candidate in candidates
    }

    return MarketplaceRunResult(
        snapshot=snapshot,
        summaries=summaries,
        evidence_ids_by_candidate=evidence_ids,
        provider_errors=provider_errors,
        missing_queries=missing,
        cached_query_count=cached_count,
        depth_miss_query_count=depth_miss_count,
        unique_listing_count=len(canonical),
        review_lookups_performed=review_lookups,
        review_lookups_skipped=review_skipped,
    )
