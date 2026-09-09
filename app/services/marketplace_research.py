"""Marketplace research orchestration (Milestone 3A).

candidate list -> collect marketplace queries -> dedupe -> capped provider
search -> dedupe listings by listing id -> bounded review enrichment ->
immutable field-level evidence -> evidence snapshot -> deterministic summaries.

Truth model enforced here:
- Fields returned directly by the marketplace are truth_class=OBSERVED.
- A listing price is OBSERVED PRICE evidence (an asking price, not a sale).
- A review count is OBSERVED PURCHASE evidence but only a PURCHASE PROXY:
  it is never sales, units, or revenue. Exact competitor sales and revenue
  remain UNKNOWN and are never inferred.
- Listing presence/seller data is OBSERVED COMPETITION evidence.
Missing data is reported as missing — never substituted with a guess.
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
    MarketplaceListing,
    MarketplaceProvider,
    ProviderError,
)
from app.services.marketplace_features import (
    CompetitionSummary,
    PriceSummary,
    PurchaseProxySummary,
    summarize_competition,
    summarize_price,
    summarize_purchase_proxy,
)
from app.services.search_demand import MissingKeyword, normalize_query, plan_queries
from app.storage.memory import ResearchStore

NORMALIZATION_VERSION = "marketplace_norm_v1"

GEOGRAPHY_GLOBAL = "GLOBAL"  # Etsy keyword search is not geography-scoped

ENV_MAX_PROVIDER_CALLS = "MARKETPLACE_MAX_PROVIDER_CALLS"
ENV_MAX_QUERIES = "MARKETPLACE_MAX_QUERIES"
ENV_MAX_RESULTS_PER_QUERY = "MARKETPLACE_MAX_RESULTS_PER_QUERY"
ENV_MAX_REVIEW_LOOKUPS = "MARKETPLACE_MAX_REVIEW_LOOKUPS"
DEFAULT_MAX_PROVIDER_CALLS = 40
DEFAULT_MAX_QUERIES = 20
DEFAULT_MAX_RESULTS_PER_QUERY = 25
DEFAULT_MAX_REVIEW_LOOKUPS = 20

MISSING_REQUEST_CAP = "request_cap_reached"
MISSING_BUDGET = "provider_call_budget_exhausted"
MISSING_PROVIDER_ERROR = "provider_error"
MISSING_NO_LISTINGS = "query_returned_no_listings"

_BASE_LIMITATIONS = [
    "Marketplace fields are public asking data observed from the provider; they are not transaction records.",
    "Exact competitor sales, units sold, and revenue are UNKNOWN and are never inferred.",
]
_PURCHASE_LIMITATIONS = _BASE_LIMITATIONS + [
    "Review count is a PURCHASE PROXY: it reflects that some reviewed purchases occurred. It is not a sales count and must never be converted into sales or revenue figures.",
]
_PRICE_LIMITATIONS = _BASE_LIMITATIONS + [
    "Listed price is an asking price, not evidence that sales occur at this price.",
]
_COMPETITION_LIMITATIONS = _BASE_LIMITATIONS + [
    "Listing presence indicates competing supply only; it carries no judgment about market quality or opportunity.",
]


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
class MarketplaceRunResult:
    snapshot: EvidenceSnapshot
    purchase_proxy_summaries: dict[UUID, PurchaseProxySummary]
    price_summaries: dict[UUID, PriceSummary]
    competition_summaries: dict[UUID, CompetitionSummary]
    evidence_ids_by_candidate: dict[UUID, list[UUID]]
    provider_errors: list[str] = field(default_factory=list)
    missing_queries: list[MissingKeyword] = field(default_factory=list)
    cached_query_count: int = 0
    review_lookups_used: int = 0


def build_listing_evidence(
    candidate_id: UUID,
    research_run_id: UUID,
    snapshot_id: UUID,
    listing: MarketplaceListing,
    provider_name: str,
    collection_method: str,
    retrieved_at: datetime,
) -> list[EvidenceItem]:
    """Field-level immutable evidence for one (candidate, listing) pair.

    Classification reflects what each field actually proves; fields the
    provider did not return produce no evidence item (they stay UNKNOWN).
    """
    payload = asdict(listing)
    payload_hash = _payload_hash(payload)
    common = dict(
        candidate_id=candidate_id,
        research_run_id=research_run_id,
        snapshot_id=snapshot_id,
        truth_class=TruthClass.OBSERVED,
        provider=provider_name,
        collection_method=collection_method,
        source_reference=listing.source_reference,
        retrieved_at=listing.retrieved_at or retrieved_at,
        geography=None,  # marketplace search is not geography-scoped
        marketplace=provider_name,
        raw_payload=payload,
        raw_payload_hash=payload_hash,
        normalization_version=NORMALIZATION_VERSION,
        freshness=1.0,
    )

    items = [
        EvidenceItem(
            signal_type="marketplace_listing_presence",
            purpose=EvidencePurpose.COMPETITION,
            raw_value=listing.listing_id,
            unit="listing",
            known_limitations=list(_COMPETITION_LIMITATIONS),
            **common,
        )
    ]
    if listing.price is not None:
        items.append(
            EvidenceItem(
                signal_type="listing_price",
                purpose=EvidencePurpose.PRICE,
                raw_value=listing.price,
                unit=listing.currency,
                known_limitations=list(_PRICE_LIMITATIONS),
                **common,
            )
        )
    if listing.review_count is not None:
        items.append(
            EvidenceItem(
                signal_type="review_count_purchase_proxy",
                purpose=EvidencePurpose.PURCHASE,
                raw_value=listing.review_count,
                unit="reviews",
                known_limitations=list(_PURCHASE_LIMITATIONS),
                **common,
            )
        )
    return items


async def run_marketplace_research(
    candidates: list[Candidate],
    provider: MarketplaceProvider,
    store: ResearchStore,
    research_run_id: UUID | None = None,
    max_provider_calls: int | None = None,
    max_queries: int | None = None,
    max_results_per_query: int | None = None,
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
    result_cap = (
        max_results_per_query
        if max_results_per_query is not None
        else _env_int(ENV_MAX_RESULTS_PER_QUERY, DEFAULT_MAX_RESULTS_PER_QUERY)
    )
    review_cap = (
        max_review_lookups
        if max_review_lookups is not None
        else _env_int(ENV_MAX_REVIEW_LOOKUPS, DEFAULT_MAX_REVIEW_LOOKUPS)
    )

    plan = plan_queries(candidates, query_attr="marketplace_queries")
    missing: list[MissingKeyword] = []
    provider_errors: list[str] = []

    requested = plan.keywords[:query_cap]
    for query in plan.keywords[query_cap:]:
        missing.append(MissingKeyword(query, MISSING_REQUEST_CAP, plan.keyword_to_candidates[query]))

    call_count = 0
    cached_count = 0
    listings_by_query: dict[str, list[str]] = {}
    listing_by_id: dict[str, MarketplaceListing] = {}
    queries_fetched: list[str] = []  # queries whose results came from the provider this run

    for query in requested:
        cached = store.cached_listing_search(provider.name, query)
        if cached is not None:
            cached_count += 1
            listings_by_query[query] = [l.listing_id for l in cached]
            for listing in cached:
                listing_by_id.setdefault(listing.listing_id, listing)
            continue

        if call_count >= call_budget:
            missing.append(
                MissingKeyword(query, MISSING_BUDGET, plan.keyword_to_candidates[query])
            )
            continue
        call_count += 1
        try:
            result = await provider.search_listings(query, limit=result_cap)
        except ProviderError as exc:
            provider_errors.append(f"{type(exc).__name__}: {exc}")
            missing.append(
                MissingKeyword(query, MISSING_PROVIDER_ERROR, plan.keyword_to_candidates[query])
            )
            continue

        provider_errors.extend(result.errors)
        # Result dedupe by listing id: one observation per listing, shared
        # across every query and candidate that matched it.
        ids: list[str] = []
        for listing in result.listings[:result_cap]:
            if listing.listing_id not in ids:
                ids.append(listing.listing_id)
            listing_by_id.setdefault(listing.listing_id, listing)
        listings_by_query[query] = ids
        queries_fetched.append(query)
        if not ids:
            missing.append(
                MissingKeyword(query, MISSING_NO_LISTINGS, plan.keyword_to_candidates[query])
            )

    # Bounded review enrichment: review data costs one provider call per
    # listing, so enrich deterministically (query order, then result order)
    # up to the review cap and remaining call budget.
    review_lookups = 0
    for query in requested:
        for listing_id in listings_by_query.get(query, []):
            listing = listing_by_id[listing_id]
            if listing.review_count is not None:
                continue
            if review_lookups >= review_cap or call_count >= call_budget:
                break
            call_count += 1
            review_lookups += 1
            try:
                stats = await provider.fetch_listing_reviews(listing_id)
            except ProviderError as exc:
                provider_errors.append(f"{type(exc).__name__}: {exc}")
                continue
            listing_by_id[listing_id] = replace(
                listing,
                review_count=stats.review_count,
                rating=stats.average_rating,
                rating_sample_size=stats.rating_sample_size or None,
            )

    # Cache enriched results only for queries fetched this run.
    for query in queries_fetched:
        store.cache_listing_search(
            provider.name,
            query,
            [listing_by_id[lid] for lid in listings_by_query[query]],
        )

    # Immutable field-level evidence per (candidate, listing), listings
    # deduplicated per candidate across its queries.
    retrieved_at = datetime.now(UTC)
    evidence_ids: dict[UUID, list[UUID]] = {c.id: [] for c in candidates}
    listings_by_candidate: dict[UUID, list[MarketplaceListing]] = {c.id: [] for c in candidates}
    evidence_items: list[EvidenceItem] = []
    collection_method = getattr(provider, "collection_method", "official_api")

    for candidate in candidates:
        seen: set[str] = set()
        for query in candidate.marketplace_queries:
            normalized = normalize_query(query)
            for listing_id in listings_by_query.get(normalized, []):
                if listing_id in seen:
                    continue
                seen.add(listing_id)
                listing = listing_by_id[listing_id]
                listings_by_candidate[candidate.id].append(listing)
                for item in build_listing_evidence(
                    candidate.id, run_id, snapshot_id, listing,
                    provider.name, collection_method, retrieved_at,
                ):
                    evidence_items.append(item)
                    evidence_ids[candidate.id].append(item.id)

    hard_failures = [m for m in missing if m.reason not in (MISSING_NO_LISTINGS,)]
    if not listing_by_id and (provider_errors or hard_failures):
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
        geography=GEOGRAPHY_GLOBAL,
        language="en",
        status=status,
        provider_call_count=call_count,
        provider_cost=None,  # Etsy is rate-limited, not billed per call
        provider_cost_is_estimate=None,
        normalization_version=NORMALIZATION_VERSION,
    )
    store.add_snapshot(snapshot)
    for item in evidence_items:
        store.add_evidence(item)

    return MarketplaceRunResult(
        snapshot=snapshot,
        purchase_proxy_summaries={
            c.id: summarize_purchase_proxy(c.id, listings_by_candidate[c.id]) for c in candidates
        },
        price_summaries={
            c.id: summarize_price(c.id, listings_by_candidate[c.id]) for c in candidates
        },
        competition_summaries={
            c.id: summarize_competition(c.id, listings_by_candidate[c.id]) for c in candidates
        },
        evidence_ids_by_candidate=evidence_ids,
        provider_errors=provider_errors,
        missing_queries=missing,
        cached_query_count=cached_count,
        review_lookups_used=review_lookups,
    )
