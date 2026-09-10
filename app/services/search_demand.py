"""Search-demand research orchestration (Milestone 2).

candidate list -> collect search queries -> dedupe -> batch provider calls
-> normalize -> immutable evidence records -> evidence snapshot -> summaries.

Product rule enforced here: search demand is NOT purchase evidence. Evidence
is classified purpose=SEARCH_DEMAND / truth_class=OBSERVED, where OBSERVED
means "the system observed the provider-supplied measurement", not that the
number is exact market truth. Missing data is reported as missing — never
substituted with a guess.
"""

import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from uuid import UUID, uuid4

from app.domain.enums import EvidencePurpose, SnapshotStatus, TruthClass
from app.domain.models import Candidate, EvidenceItem, EvidenceSnapshot
from app.providers.base import (
    KeywordDemandMetrics,
    ProviderError,
    SearchDemandBatchResult,
    SearchDemandProvider,
)
from app.services.query_provenance import resolve_provenance
from app.services.search_demand_features import (
    SearchDemandSummary,
    search_demand_dimension,
    summarize_search_demand,
)
from app.storage.memory import ResearchStore

NORMALIZATION_VERSION = "search_demand_norm_v1"

KNOWN_LIMITATIONS = [
    "Search volume measures search interest, not purchases, buyers, revenue, or purchase intent.",
    "Provider-supplied estimate: OBSERVED means the measurement was observed from the provider, not that it is exact market truth.",
    "Monthly values are rounded/bucketed by the provider and can lag recent trends.",
]

ENV_MAX_PROVIDER_CALLS = "SEARCH_DEMAND_MAX_PROVIDER_CALLS"
ENV_MAX_KEYWORDS = "SEARCH_DEMAND_MAX_KEYWORDS"
DEFAULT_MAX_PROVIDER_CALLS = 5
DEFAULT_MAX_KEYWORDS = 200

_WHITESPACE_RE = re.compile(r"\s+")

# Reasons a requested keyword ended up without a measurement.
MISSING_REQUEST_CAP = "request_cap_reached"
MISSING_BUDGET = "provider_call_budget_exhausted"
MISSING_PROVIDER_ERROR = "provider_error"
MISSING_NOT_RETURNED = "keyword_not_returned_by_provider"


def normalize_query(query: str) -> str:
    return _WHITESPACE_RE.sub(" ", query.strip().lower())


@dataclass(slots=True)
class QueryPlan:
    """Deduplicated keywords with the candidate relationships preserved.

    One keyword can legitimately support multiple candidates.
    """

    keywords: list[str]
    keyword_to_candidates: dict[str, list[UUID]]


def plan_queries(candidates: list[Candidate]) -> QueryPlan:
    keywords: list[str] = []
    mapping: dict[str, list[UUID]] = {}
    for candidate in candidates:
        for query in candidate.search_queries:
            keyword = normalize_query(query)
            if not keyword:
                continue
            if keyword not in mapping:
                mapping[keyword] = []
                keywords.append(keyword)
            if candidate.id not in mapping[keyword]:
                mapping[keyword].append(candidate.id)
    return QueryPlan(keywords=keywords, keyword_to_candidates=mapping)


def _chunk(items: list[str], size: int) -> list[list[str]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def _payload_hash(payload: dict) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


@dataclass(slots=True)
class MissingKeyword:
    keyword: str
    reason: str
    candidate_ids: list[UUID]


@dataclass(slots=True)
class SearchDemandRunResult:
    snapshot: EvidenceSnapshot
    summaries: dict[UUID, SearchDemandSummary]
    evidence_ids_by_candidate: dict[UUID, list[UUID]]
    provider_errors: list[str] = field(default_factory=list)
    missing_keywords: list[MissingKeyword] = field(default_factory=list)
    cached_keyword_count: int = 0


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return max(0, int(raw))
    except ValueError:
        return default


def build_evidence_item(
    candidate_id: UUID,
    research_run_id: UUID,
    snapshot_id: UUID,
    metrics: KeywordDemandMetrics,
    batch: SearchDemandBatchResult,
    location: str,
    language: str,
    originating_queries: tuple[str, ...] | None = None,
    originating_query_shared: bool | None = None,
) -> EvidenceItem:
    payload = asdict(metrics)
    observed = metrics.search_volume is not None
    return EvidenceItem(
        candidate_id=candidate_id,
        research_run_id=research_run_id,
        snapshot_id=snapshot_id,
        signal_type="search_volume",
        purpose=EvidencePurpose.SEARCH_DEMAND,
        truth_class=TruthClass.OBSERVED if observed else TruthClass.UNKNOWN,
        provider=batch.provider,
        provider_version=batch.provider_version,
        collection_method=batch.collection_method,
        source_reference=batch.source_reference,
        retrieved_at=metrics.retrieved_at or batch.retrieved_at,
        geography=location,
        language=language,
        raw_value=metrics.search_volume,
        unit="searches_per_month" if observed else None,
        normalized_value=search_demand_dimension(
            float(metrics.search_volume) if observed else None
        ),
        sample_size=None,
        freshness=1.0,
        known_limitations=list(KNOWN_LIMITATIONS),
        normalization_version=NORMALIZATION_VERSION,
        raw_payload=payload,
        raw_payload_hash=_payload_hash(payload),
        # Retrieval metadata only; outside the payload and therefore outside
        # the hash, and never affecting the truth class chosen above.
        originating_queries=originating_queries,
        originating_query_shared=originating_query_shared,
    )


async def run_search_demand_research(
    candidates: list[Candidate],
    provider: SearchDemandProvider,
    store: ResearchStore,
    research_run_id: UUID | None = None,
    location: str = "US",
    language: str = "en",
    max_provider_calls: int | None = None,
    max_keywords: int | None = None,
) -> SearchDemandRunResult:
    """Run one search-demand research pass and persist an immutable snapshot."""
    run_id = research_run_id or uuid4()
    snapshot_id = uuid4()
    started_at = datetime.now(UTC)

    call_budget = (
        max_provider_calls
        if max_provider_calls is not None
        else _env_int(ENV_MAX_PROVIDER_CALLS, DEFAULT_MAX_PROVIDER_CALLS)
    )
    keyword_cap = (
        max_keywords if max_keywords is not None else _env_int(ENV_MAX_KEYWORDS, DEFAULT_MAX_KEYWORDS)
    )

    plan = plan_queries(candidates)
    missing: list[MissingKeyword] = []
    provider_errors: list[str] = []

    requested = plan.keywords[:keyword_cap]
    for keyword in plan.keywords[keyword_cap:]:
        missing.append(
            MissingKeyword(keyword, MISSING_REQUEST_CAP, plan.keyword_to_candidates[keyword])
        )

    # Cache first: identical (provider, keyword, location, language) measurements
    # within the freshness window are reused instead of re-purchased.
    metrics_by_keyword: dict[str, tuple[KeywordDemandMetrics, SearchDemandBatchResult]] = {}
    cached_batch = SearchDemandBatchResult(
        provider=provider.name,
        metrics=[],
        retrieved_at=started_at,
        collection_method="cache",
        source_reference="research_store_cache",
    )
    to_fetch: list[str] = []
    cached_count = 0
    for keyword in requested:
        cached = store.cached_metrics(provider.name, keyword, location, language)
        if cached is not None:
            metrics_by_keyword[keyword] = (cached, cached_batch)
            cached_count += 1
        else:
            to_fetch.append(keyword)

    batches = _chunk(to_fetch, provider.max_keywords_per_request)
    call_count = 0
    total_cost: float | None = None
    cost_is_estimate = True
    provider_version: str | None = None

    for batch_keywords in batches:
        if call_count >= call_budget:
            for keyword in batch_keywords:
                missing.append(
                    MissingKeyword(keyword, MISSING_BUDGET, plan.keyword_to_candidates[keyword])
                )
            continue
        call_count += 1
        try:
            batch = await provider.fetch_keyword_metrics(batch_keywords, location, language)
        except ProviderError as exc:
            provider_errors.append(f"{type(exc).__name__}: {exc}")
            for keyword in batch_keywords:
                missing.append(
                    MissingKeyword(
                        keyword, MISSING_PROVIDER_ERROR, plan.keyword_to_candidates[keyword]
                    )
                )
            continue

        provider_errors.extend(batch.errors)
        provider_version = batch.provider_version or provider_version
        if batch.cost is not None:
            total_cost = (total_cost or 0.0) + batch.cost
            cost_is_estimate = cost_is_estimate and batch.cost_is_estimate

        returned = set()
        for metrics in batch.metrics:
            keyword = normalize_query(metrics.keyword)
            if keyword in plan.keyword_to_candidates:
                metrics_by_keyword[keyword] = (metrics, batch)
                returned.add(keyword)
                store.cache_metrics(provider.name, keyword, location, language, metrics)
        for keyword in batch_keywords:
            if keyword not in returned:
                missing.append(
                    MissingKeyword(
                        keyword, MISSING_NOT_RETURNED, plan.keyword_to_candidates[keyword]
                    )
                )

    # Build immutable evidence: one record per (candidate, keyword measurement).
    evidence_ids: dict[UUID, list[UUID]] = {c.id: [] for c in candidates}
    metrics_by_candidate: dict[UUID, list[KeywordDemandMetrics]] = {c.id: [] for c in candidates}
    evidence_items: list[EvidenceItem] = []
    for keyword, (metrics, batch) in metrics_by_keyword.items():
        for candidate_id in plan.keyword_to_candidates[keyword]:
            # For search demand the keyword IS the request, so provenance is
            # the single keyword that was sent. It already appears inside the
            # payload as the measured subject and must stay there: removing
            # it would change raw_payload_hash. Recording it here as well
            # keeps the provenance field uniform across capabilities.
            originating_queries, query_shared = resolve_provenance(
                candidate_id, [keyword], plan.keyword_to_candidates
            )
            item = build_evidence_item(
                candidate_id,
                run_id,
                snapshot_id,
                metrics,
                batch,
                location,
                language,
                originating_queries=originating_queries,
                originating_query_shared=query_shared,
            )
            evidence_items.append(item)
            evidence_ids[candidate_id].append(item.id)
            metrics_by_candidate[candidate_id].append(metrics)

    hard_failures = [m for m in missing if m.reason != MISSING_NOT_RETURNED]
    if not metrics_by_keyword and (provider_errors or hard_failures):
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
        geography=location,
        language=language,
        status=status,
        provider_call_count=call_count,
        provider_cost=total_cost,
        provider_cost_is_estimate=cost_is_estimate if total_cost is not None else None,
        normalization_version=NORMALIZATION_VERSION,
    )
    store.add_snapshot(snapshot)
    for item in evidence_items:
        store.add_evidence(item)

    summaries = {
        candidate.id: summarize_search_demand(candidate.id, metrics_by_candidate[candidate.id])
        for candidate in candidates
    }

    return SearchDemandRunResult(
        snapshot=snapshot,
        summaries=summaries,
        evidence_ids_by_candidate=evidence_ids,
        provider_errors=provider_errors,
        missing_keywords=missing,
        cached_keyword_count=cached_count,
    )
