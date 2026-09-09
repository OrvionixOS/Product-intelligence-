"""Deterministic candidate-level search-demand feature extraction.

Produces robust summary features from keyword metrics. This is NOT the
Opportunity Score and makes no GREEN/YELLOW/RED decision. The optional 0-100
search_demand_dimension is a documented, versioned V1 heuristic:

    search_demand_dimension_v1:
        0 when median search volume is unknown or 0,
        otherwise min(100, 100 * log10(median_volume + 1) / 6)
        (log scale where ~1,000,000 monthly searches saturates at 100).

Search demand measures search interest only — never purchases, buyers,
revenue, or validation.
"""

import math
from dataclasses import dataclass, field
from statistics import median
from uuid import UUID

from app.providers.base import KeywordDemandMetrics

SEARCH_DEMAND_DIMENSION_VERSION = "search_demand_dimension_v1"

_LOG10_SATURATION = 6.0  # 10**6 monthly searches -> 100


@dataclass(slots=True)
class SearchDemandSummary:
    candidate_id: UUID
    relevant_keyword_count: int
    median_search_volume: float | None
    max_search_volume: int | None
    total_search_volume: int | None
    median_cpc: float | None
    competition_counts: dict[str, int] = field(default_factory=dict)
    median_competition_index: float | None = None
    history_months_median: float | None = None
    missing_data_count: int = 0
    search_demand_dimension: float | None = None
    dimension_version: str = SEARCH_DEMAND_DIMENSION_VERSION


def search_demand_dimension(median_volume: float | None) -> float | None:
    """Versioned V1 heuristic mapping median volume to 0-100. Deterministic."""
    if median_volume is None:
        return None
    if median_volume <= 0:
        return 0.0
    return round(min(100.0, 100.0 * math.log10(median_volume + 1) / _LOG10_SATURATION), 2)


def summarize_search_demand(
    candidate_id: UUID, metrics: list[KeywordDemandMetrics]
) -> SearchDemandSummary:
    """Deterministic summary of a candidate's keyword-demand evidence.

    Keywords with no search_volume count toward missing_data_count and are
    excluded from volume statistics — missing data is reported, not invented.
    """
    volumes = [m.search_volume for m in metrics if m.search_volume is not None]
    cpcs = [m.cpc for m in metrics if m.cpc is not None]
    competition_indexes = [
        m.competition_index for m in metrics if m.competition_index is not None
    ]
    history_lengths = [len(m.monthly_history) for m in metrics if m.monthly_history]

    competition_counts: dict[str, int] = {}
    for m in metrics:
        if m.competition is not None:
            competition_counts[m.competition] = competition_counts.get(m.competition, 0) + 1

    missing = sum(1 for m in metrics if m.search_volume is None)
    median_volume = float(median(volumes)) if volumes else None

    return SearchDemandSummary(
        candidate_id=candidate_id,
        relevant_keyword_count=len(volumes),
        median_search_volume=median_volume,
        max_search_volume=max(volumes) if volumes else None,
        total_search_volume=sum(volumes) if volumes else None,
        median_cpc=round(float(median(cpcs)), 4) if cpcs else None,
        competition_counts=competition_counts,
        median_competition_index=float(median(competition_indexes)) if competition_indexes else None,
        history_months_median=float(median(history_lengths)) if history_lengths else None,
        missing_data_count=missing,
        search_demand_dimension=search_demand_dimension(median_volume),
    )
