"""Deterministic preliminary ranking and top-five selection (Milestone 3C).

Purpose: decide which candidates are worth the cost of deep research. This
is a triage ordering, NOT the Product Opportunity Score, and it makes no
commercial claim about any candidate.

    preliminary_rank_v1

    Candidates are ordered by a fixed lexicographic sequence of criteria.
    There is no weighted composite, because no dimension weights are
    approved in the repository specification and 3C must not invent any.
    Each criterion is applied in order; the first one that differs decides
    the pair, which is what makes every ordering explainable.

    1. evidence_breadth       - count of preliminary dimensions backed by
                                real evidence, descending. Deep research is
                                expensive, so it goes where corroborating
                                evidence already exists.
    2. search_demand          - preliminary_search_demand value, descending
                                (search_demand_dimension_v1).
    3. audience_interest      - preliminary_audience_interest value,
                                descending (audience_interest_dimension_v1).
    4. price_comparables      - count of OBSERVED priced comparables,
                                descending. A count of evidence, not a score.
    5. purchase_proxy_signals - count of OBSERVED review-count proxies,
                                descending. A count of evidence, never sales.
    6. title                  - case-folded candidate title, ascending.
    7. candidate_id           - candidate UUID string, ascending.

    Criteria 6 and 7 are pure tie-breakers: they carry no evidential meaning
    and exist so that identical evidence always yields identical output.

Rules this module holds to:

- Deterministic and pure: same inputs, same order, every time. No clock, no
  randomness, no iteration-order dependence, no LLM anywhere in the path.
  An LLM never manufactures, adjusts, or assigns any number here.
- A missing or UNKNOWN dimension is never treated as 0. It sorts after every
  candidate that has a value on that criterion, via an explicit
  has-value flag in the sort key, and an observed 0 outranks an absent
  measurement.
- No POS weights, no ECS thresholds, no RED/YELLOW/GREEN, no kill rules, no
  "validated" verdict. Rank position is a research-priority ordering only.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from uuid import UUID

from app.services.preliminary_dimensions import (
    DIM_AUDIENCE_INTEREST,
    DIM_PRICE_EVIDENCE,
    DIM_PURCHASE_PROXY,
    DIM_SEARCH_DEMAND,
    CandidatePreliminaryProfile,
    DimensionState,
)

PRELIMINARY_RANKING_VERSION = "preliminary_rank_v1"

# How many candidates advance to the later deep-research stage.
DEEP_RESEARCH_SELECTION_SIZE = 5

# Sort-key sentinel: 0 marks "this candidate has a value on this criterion",
# 1 marks "it does not". Because the flag is compared before the value, a
# missing or UNKNOWN measurement always sorts last without being read as 0.
_HAS_VALUE = 0
_NO_VALUE = 1


@dataclass(slots=True, frozen=True)
class CriterionValue:
    """One candidate's standing on one ranking criterion."""

    name: str
    # None means the candidate has no value for this criterion. It is not 0.
    value: float | str | None
    direction: str  # "desc" or "asc"

    @property
    def display(self) -> str:
        return "no value" if self.value is None else str(self.value)


@dataclass(slots=True, frozen=True)
class RankedCandidate:
    candidate_id: UUID
    candidate_title: str
    rank: int
    criteria: tuple[CriterionValue, ...]
    profile: CandidatePreliminaryProfile
    selected_for_deep_research: bool = False
    ranking_version: str = PRELIMINARY_RANKING_VERSION

    def criterion(self, name: str) -> CriterionValue:
        for item in self.criteria:
            if item.name == name:
                return item
        raise KeyError(name)


@dataclass(slots=True, frozen=True)
class PreliminaryRanking:
    ranked: tuple[RankedCandidate, ...]
    selected: tuple[RankedCandidate, ...]
    ranking_version: str = PRELIMINARY_RANKING_VERSION
    selection_size: int = DEEP_RESEARCH_SELECTION_SIZE
    criteria_order: tuple[str, ...] = ()


def _scored_value(profile: CandidatePreliminaryProfile, name: str) -> float | None:
    """A dimension's value only when an approved formula actually scored it."""
    dimension = profile.dimensions.get(name)
    if dimension is None or dimension.state != DimensionState.SCORED:
        return None
    return dimension.value


def _observed_count(profile: CandidatePreliminaryProfile, name: str) -> float | None:
    """Observed-input count, or None when the dimension has no evidence."""
    dimension = profile.dimensions.get(name)
    if dimension is None or dimension.state == DimensionState.MISSING:
        return None
    return float(dimension.observed_input_count)


# Criterion extractors, applied in this exact order. Each returns the
# candidate's value, or None when the candidate has no value for it.
_CRITERIA: tuple[tuple[str, str, Callable[[CandidatePreliminaryProfile], float | str | None]], ...] = (
    ("evidence_breadth", "desc", lambda p: float(p.present_dimension_count)),
    ("search_demand", "desc", lambda p: _scored_value(p, DIM_SEARCH_DEMAND)),
    ("audience_interest", "desc", lambda p: _scored_value(p, DIM_AUDIENCE_INTEREST)),
    ("price_comparables", "desc", lambda p: _observed_count(p, DIM_PRICE_EVIDENCE)),
    ("purchase_proxy_signals", "desc", lambda p: _observed_count(p, DIM_PURCHASE_PROXY)),
    ("title", "asc", lambda p: p.candidate_title.casefold()),
    ("candidate_id", "asc", lambda p: str(p.candidate_id)),
)

CRITERIA_ORDER = tuple(name for name, _, _ in _CRITERIA)


def criterion_values(profile: CandidatePreliminaryProfile) -> tuple[CriterionValue, ...]:
    """Every criterion value for one candidate, in ranking order."""
    return tuple(
        CriterionValue(name=name, value=extract(profile), direction=direction)
        for name, direction, extract in _CRITERIA
    )


def _sort_key(criteria: Sequence[CriterionValue]) -> tuple:
    """Lexicographic key. Missing values sort last, never as zero."""
    key: list[tuple] = []
    for item in criteria:
        if item.value is None:
            # The has-value flag is compared first, so the placeholder that
            # follows is never interpreted as a measurement.
            key.append((_NO_VALUE, 0.0, ""))
            continue
        if isinstance(item.value, str):
            key.append((_HAS_VALUE, 0.0, item.value))
            continue
        numeric = -item.value if item.direction == "desc" else item.value
        key.append((_HAS_VALUE, numeric, ""))
    return tuple(key)


def rank_candidates(
    profiles: Sequence[CandidatePreliminaryProfile],
    selection_size: int = DEEP_RESEARCH_SELECTION_SIZE,
) -> PreliminaryRanking:
    """Order candidates by preliminary_rank_v1 and select the top N.

    Fewer candidates than `selection_size` simply selects all of them; the
    selection is never padded with invented entries.
    """
    scored = [(profile, criterion_values(profile)) for profile in profiles]
    scored.sort(key=lambda pair: _sort_key(pair[1]))

    ranked = tuple(
        RankedCandidate(
            candidate_id=profile.candidate_id,
            candidate_title=profile.candidate_title,
            rank=index + 1,
            criteria=criteria,
            profile=profile,
            selected_for_deep_research=index < selection_size,
        )
        for index, (profile, criteria) in enumerate(scored)
    )

    return PreliminaryRanking(
        ranked=ranked,
        selected=tuple(c for c in ranked if c.selected_for_deep_research),
        selection_size=selection_size,
        criteria_order=CRITERIA_ORDER,
    )


@dataclass(slots=True, frozen=True)
class RankExplanation:
    higher_candidate_id: UUID
    lower_candidate_id: UUID
    deciding_criterion: str
    higher_value: str
    lower_value: str
    reason: str
    ranking_version: str = PRELIMINARY_RANKING_VERSION


def explain_pairwise(a: RankedCandidate, b: RankedCandidate) -> RankExplanation:
    """State exactly which criterion put one candidate above the other."""
    higher, lower = (a, b) if a.rank < b.rank else (b, a)

    for name in CRITERIA_ORDER:
        high_value = higher.criterion(name)
        low_value = lower.criterion(name)
        if _sort_key([high_value]) == _sort_key([low_value]):
            continue
        if high_value.value is None:
            reason = (
                f"'{lower.candidate_title}' has a value for {name} and "
                f"'{higher.candidate_title}' does not, but an earlier criterion "
                f"had already decided the order"
            )
        elif low_value.value is None:
            reason = (
                f"'{higher.candidate_title}' has a value for {name} "
                f"({high_value.display}) and '{lower.candidate_title}' has none; "
                f"a measured value outranks an absent one, which is not read as zero"
            )
        else:
            comparison = "higher" if high_value.direction == "desc" else "earlier"
            reason = (
                f"'{higher.candidate_title}' ranked above '{lower.candidate_title}' "
                f"because {name} was {comparison} "
                f"({high_value.display} vs {low_value.display}); "
                f"all preceding criteria were equal"
            )
        return RankExplanation(
            higher_candidate_id=higher.candidate_id,
            lower_candidate_id=lower.candidate_id,
            deciding_criterion=name,
            higher_value=high_value.display,
            lower_value=low_value.display,
            reason=reason,
        )

    # Unreachable in practice: candidate_id is unique and decides every tie.
    return RankExplanation(
        higher_candidate_id=higher.candidate_id,
        lower_candidate_id=lower.candidate_id,
        deciding_criterion="none",
        higher_value="",
        lower_value="",
        reason="candidates are indistinguishable under preliminary_rank_v1",
    )
