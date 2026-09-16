"""Milestone 6D — the three V1 POS sub-scores.

SPEC_STEP_7_8.md §5 closes the V1 POS surface at exactly three dimensions and
specifies structure without numbers. U-1 supplies the numbers: three named,
versioned, **explicitly uncalibrated** V1 policy formulas, adopted by decision
rather than derived from outcome data. None has been validated against
outcomes and none may be described as empirically validated. A later
recalibration ships as `_v2` rather than editing these in place, so a score
already produced stays attributable to the formula that produced it.

Four rules carry this slice.

**Missing never becomes zero.** A dimension that is MISSING or UNKNOWN, or one
whose required statistic was never reported, BLOCKS its sub-score. Blocked is
`None` with a reason, never `0.0`. Substituting zero would turn "we did not
find out" into "we found nothing", which is the single failure every milestone
since 3C has existed to prevent.

**Observed zero is a real measurement and scores.** A keyword measured at zero
searches, a comparable with zero observed reviews, a video with zero views —
each is an observation, each enters, and each scores 0 on its own scale.
`log10(0 + 1) = 0` is a score, not an absence.

**A proxy stays a proxy.** `pos_purchase_proxy` is observable evidence that
comparable things sell. It is never sales, revenue, buyers, units sold,
conversion, or probability of success, and the label travels with the number
wherever it is surfaced.

**Attention is attention.** `pos_audience_attention` measures observed public
attention. It is never buyer demand, purchase intent, or audience size.

No aggregation here — 6E combines these. No classification, no thresholds.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from math import log10

from app.services.deep_research import DeepDimension, DeepResearchResult
from app.services.evidence_confidence import EcsDimension

POS_SEARCH_DEMAND_VERSION = "pos_search_demand_v1"
POS_PURCHASE_PROXY_VERSION = "pos_purchase_proxy_v1"
POS_AUDIENCE_ATTENTION_VERSION = "pos_audience_attention_v1"

# ------------------------------------------------------- U-1 normalizations
#
# Each scale is a log10 saturation point: the observed magnitude at which the
# normalized value reaches 100 and stops rising. UNCALIBRATED V1 policy,
# chosen by decision. The saturation points are stated as exponents so the
# magnitude they represent is visible rather than buried in a constant.
SEARCH_SATURATION_EXPONENT = 5  # 100,000 monthly searches
REVIEW_SATURATION_EXPONENT = 3  # 1,000 observed reviews
ATTENTION_SATURATION_EXPONENT = 6  # 1,000,000 views

# The `+ 1` inside the logarithm shifts the curve by exactly one unit, so the
# FIRST observation reaching 100 is one below the stated saturation point:
# search_norm(99_999) is already 100.0, as is every larger value. The stated
# points are therefore saturated, and describing them as "saturates at
# 100,000" is accurate to within that single unit. Recorded rather than
# rounded over, because a test asserting "just below saturation scores below
# 100" is wrong by one if it does not know this.

SEARCH_SATURATION_VALUE = 10**SEARCH_SATURATION_EXPONENT
REVIEW_SATURATION_VALUE = 10**REVIEW_SATURATION_EXPONENT
ATTENTION_SATURATION_VALUE = 10**ATTENTION_SATURATION_EXPONENT

POS_SCALE_MIN = 0.0
POS_SCALE_MAX = 100.0
POS_ROUNDING = 2


class PosDimensionName(str, Enum):
    SEARCH_DEMAND = "pos_search_demand"
    PURCHASE_PROXY = "pos_purchase_proxy"
    AUDIENCE_ATTENTION = "pos_audience_attention"


# The closed V1 surface (§5), and which Step 7 dimension each sub-score reads.
POS_SOURCE_DIMENSION: dict[PosDimensionName, EcsDimension] = {
    PosDimensionName.SEARCH_DEMAND: EcsDimension.SEARCH_DEMAND,
    PosDimensionName.PURCHASE_PROXY: EcsDimension.PURCHASE_PROXY_EVIDENCE,
    PosDimensionName.AUDIENCE_ATTENTION: EcsDimension.AUDIENCE_ATTENTION,
}

POS_DIMENSION_ORDER: tuple[PosDimensionName, ...] = (
    PosDimensionName.SEARCH_DEMAND,
    PosDimensionName.PURCHASE_PROXY,
    PosDimensionName.AUDIENCE_ATTENTION,
)

POS_VERSIONS: dict[PosDimensionName, str] = {
    PosDimensionName.SEARCH_DEMAND: POS_SEARCH_DEMAND_VERSION,
    PosDimensionName.PURCHASE_PROXY: POS_PURCHASE_PROXY_VERSION,
    PosDimensionName.AUDIENCE_ATTENTION: POS_AUDIENCE_ATTENTION_VERSION,
}

# The statistics each formula reads, in the order it averages them.
POS_REQUIRED_STATISTICS: dict[PosDimensionName, tuple[str, ...]] = {
    PosDimensionName.SEARCH_DEMAND: (
        "q25_search_volume",
        "median_search_volume",
        "q75_search_volume",
    ),
    PosDimensionName.PURCHASE_PROXY: (
        "median_review_count",
        "upper_quartile_review_count",
        "proportion_of_comparables_with_proxy",
    ),
    PosDimensionName.AUDIENCE_ATTENTION: (
        "lower_quartile_views",
        "median_views",
        "upper_quartile_views",
    ),
}

BLOCKED_DIMENSION_NOT_SCOREABLE = "source_dimension_not_scoreable"
BLOCKED_STATISTIC_UNAVAILABLE = "required_statistic_unavailable"
BLOCKED_DIMENSION_ABSENT = "source_dimension_absent"

LIMITATIONS: tuple[str, ...] = (
    "The three POS formulas are unvalidated V1 policy assumptions chosen by "
    "decision, not calibrated against outcome data. They are not empirically "
    "validated, and a higher sub-score is not evidence that a candidate will "
    "sell.",
    "pos_purchase_proxy is a PURCHASE PROXY. It measures observable evidence "
    "that comparable things sell. It is never sales, revenue, buyers, units "
    "sold, conversion, or probability of success.",
    "pos_audience_attention measures observed public ATTENTION. It is never "
    "buyer demand, purchase intent, willingness to pay, or audience size.",
    "A blocked sub-score is the absence of a measurement, never a score of "
    "zero, and must never be rendered, stored or compared as one.",
    "Each scale saturates: beyond its saturation point a larger observation "
    "does not raise the sub-score, so these numbers rank nothing above it.",
)


class PosDimensionState(str, Enum):
    """Whether this sub-score exists."""

    SCORED = "SCORED"
    # A required input was missing or unknown. Not a low score: the absence of
    # one. §8 forbids substituting zero.
    BLOCKED = "BLOCKED"


@dataclass(slots=True, frozen=True)
class PosSubScore:
    """One V1 POS sub-score, with everything needed to recompute it by hand."""

    name: PosDimensionName
    source_dimension: EcsDimension
    state: PosDimensionState
    # None whenever state is BLOCKED. Never 0.0 to mean absent.
    value: float | None
    blocked_reason: str | None
    # The observed statistics read, and what each normalized to, in the order
    # they were averaged.
    inputs: tuple[tuple[str, float], ...]
    normalized_inputs: tuple[tuple[str, float], ...]
    version: str

    @property
    def scored(self) -> bool:
        return self.state is PosDimensionState.SCORED


@dataclass(slots=True, frozen=True)
class PosSubScoreSet:
    """The three sub-scores for one candidate and run."""

    sub_scores: tuple[PosSubScore, ...]
    limitations: tuple[str, ...] = field(default=LIMITATIONS)

    def by_name(self, name: PosDimensionName) -> PosSubScore:
        return next(s for s in self.sub_scores if s.name is name)

    @property
    def all_scored(self) -> bool:
        return all(s.scored for s in self.sub_scores)

    @property
    def blocked(self) -> tuple[PosSubScore, ...]:
        return tuple(s for s in self.sub_scores if not s.scored)


# ------------------------------------------------------------ normalization


def _log_saturating_norm(value: float, saturation_exponent: int) -> float:
    """`min(100, 100 * log10(v + 1) / exponent)`.

    The `+ 1` is what makes an observed zero scoreable: log10(1) is 0, so a
    real zero observation normalizes to 0 rather than to negative infinity.
    """
    if value < 0:
        # Not reachable from any observed count, which cannot be negative.
        # Refused rather than clamped, because a negative count is a defect
        # upstream and silently reading it as 0 would hide it.
        raise ValueError(f"negative observation cannot be normalized: {value}")
    return min(
        POS_SCALE_MAX, POS_SCALE_MAX * log10(value + 1) / saturation_exponent
    )


def search_norm(volume: float) -> float:
    """`search_norm(v)`, saturating at 100,000 monthly searches."""
    return _log_saturating_norm(volume, SEARCH_SATURATION_EXPONENT)


def review_norm(review_count: float) -> float:
    """`review_norm(r)`, saturating at 1,000 observed reviews."""
    return _log_saturating_norm(review_count, REVIEW_SATURATION_EXPONENT)


def attention_norm(views: float) -> float:
    """`attention_norm(v)`, saturating at 1,000,000 views."""
    return _log_saturating_norm(views, ATTENTION_SATURATION_EXPONENT)


def proxy_prevalence(proportion: float) -> float:
    """`100 * proportion_of_comparables_with_proxy`.

    A proportion, not a magnitude, so it is scaled rather than log-normalized.
    """
    if not 0.0 <= proportion <= 1.0:
        raise ValueError(f"proportion outside [0, 1]: {proportion}")
    return POS_SCALE_MAX * proportion


# Which normalization each statistic takes. Keyed by statistic so the mapping
# cannot drift from the statistic list above.
_NORMALIZERS = {
    "q25_search_volume": search_norm,
    "median_search_volume": search_norm,
    "q75_search_volume": search_norm,
    "median_review_count": review_norm,
    "upper_quartile_review_count": review_norm,
    "proportion_of_comparables_with_proxy": proxy_prevalence,
    "lower_quartile_views": attention_norm,
    "median_views": attention_norm,
    "upper_quartile_views": attention_norm,
}


# ------------------------------------------------------------- computation


def _blocked(
    name: PosDimensionName, reason: str
) -> PosSubScore:
    return PosSubScore(
        name=name,
        source_dimension=POS_SOURCE_DIMENSION[name],
        state=PosDimensionState.BLOCKED,
        value=None,
        blocked_reason=reason,
        inputs=(),
        normalized_inputs=(),
        version=POS_VERSIONS[name],
    )


def compute_sub_score(
    name: PosDimensionName, dimension: DeepDimension | None
) -> PosSubScore:
    """One sub-score from one Step 7 dimension, or a blocked result.

    Deterministic and pure. Reads only the dimension's declared observables;
    it cannot reach the evidence store and derives nothing of its own.
    """
    if dimension is None:
        return _blocked(name, BLOCKED_DIMENSION_ABSENT)
    if not dimension.scoreable:
        # MISSING or UNKNOWN. §8: the dimension is excluded and the candidate
        # POS is blocked; it never contributes a zero.
        return _blocked(name, BLOCKED_DIMENSION_NOT_SCOREABLE)

    observed: list[tuple[str, float]] = []
    normalized: list[tuple[str, float]] = []
    for statistic in POS_REQUIRED_STATISTICS[name]:
        value = dimension.observed_features.get(statistic)
        if value is None:
            # Reported nothing for a statistic the formula requires. Blocked,
            # never defaulted: an unreported quartile is not a quartile of 0.
            return _blocked(name, f"{BLOCKED_STATISTIC_UNAVAILABLE}:{statistic}")
        observed.append((statistic, float(value)))
        normalized.append((statistic, _NORMALIZERS[statistic](float(value))))

    value = round(
        sum(score for _, score in normalized) / len(normalized), POS_ROUNDING
    )
    return PosSubScore(
        name=name,
        source_dimension=POS_SOURCE_DIMENSION[name],
        state=PosDimensionState.SCORED,
        value=value,
        blocked_reason=None,
        inputs=tuple(observed),
        normalized_inputs=tuple(normalized),
        version=POS_VERSIONS[name],
    )


def compute_pos_sub_scores(result: DeepResearchResult) -> PosSubScoreSet:
    """The three V1 sub-scores for one Step 7 boundary object.

    The surface is closed at three: no other dimension contributes a POS
    magnitude, whatever else the dossier carries.
    """
    return PosSubScoreSet(
        sub_scores=tuple(
            compute_sub_score(
                name, result.dimensions.get(POS_SOURCE_DIMENSION[name].value)
            )
            for name in POS_DIMENSION_ORDER
        )
    )
