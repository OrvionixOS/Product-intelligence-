"""Price Evidence extraction (Milestone 4B).

The question this answers:

    What prices are sellers ASKING for comparable products, and how coherent
    is that observed price field?

The questions it deliberately does NOT answer:

    What should this product be priced at?
    What will buyers pay?
    What is a competitor's revenue?
    What is the "optimal" or "right" price?

Naming
------

This module is Price EVIDENCE, not "price strength". It measures observed
market evidence; it asserts no strength, quality, or desirability. The
legacy `ScoreDimensions.price_strength` field name predates this milestone
and is left untouched — 4B neither populates nor activates it.

Asking price is not transaction price
-------------------------------------

Every figure here derives from a listing's public ASKING price: the number a
seller displays. It is not a verified transaction price. Discounts, coupons,
sales, and abandoned carts are invisible to public listing data. An asking
price is therefore never:

    a transaction price          a price a buyer actually paid
    willingness to pay           what a buyer would accept
    revenue                      asking price times anything
    an optimal price             a recommendation of any kind

Currencies are never mixed
--------------------------

This repository has no approved FX source, and inventing conversion rates
would fabricate comparisons. Price bands are therefore computed **per
currency** and reported separately. No currency is discarded because another
is more common, and cross-currency comparison is explicitly not performed.
Consumers are told so in `limitations` and by `cross_currency_comparison`
being permanently False.

Free listings
-------------

A $0 listing is a free competitor: real, OBSERVED evidence about the market.
It is counted as `free_listing_count`, reported as a proportion, and
**excluded from every paid-price statistic**. It is never treated as missing
data and never as a paid comparable. A listing with no OBSERVED price stays
unknown — neither free nor zero.

Bundle limitation
-----------------

A $45 "50-template pack" and a $5 single printable are each one listing with
one asking price. Public listing data carries no trustworthy structured unit
quantity, so prices are NOT normalized per item, template, or page. Both
remain separate observed listing prices, and the limitation is reported
rather than papered over with an invented divisor.

Output
------

Per-currency bands plus global counts. **No 0-100 score.** No
price-evidence formula is approved in this repository, so the dimension
returns EVIDENCE_PRESENT_UNSCORED rather than manufacturing a number, exactly
as 4A does. `DimensionState.SCORED` stays reserved.

Robustness
----------

Price distributions are skewed by premium bundles. Arithmetic means are
avoided as a headline figure: bands report quartiles, IQR, and a trimmed
central statistic, and every statistic that needs a minimum sample says so
rather than pretending a small sample is stable. Raw observed prices are
preserved alongside the derived statistics, so robust statistics never
destroy or overwrite the source observations.
"""

import statistics
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from uuid import UUID

from app.domain.enums import TruthClass
from app.domain.models import EvidenceItem
from app.services.marketplace_features import MIN_PRICE_COMPARABLES
from app.services.marketplace_listing_view import (
    PAID_COMPARABLE_DEFINITION,
    ListingView,
    collect_listing_views,
    quantile,
    weakest_truth_class,
)
from app.services.preliminary_dimensions import DimensionState
from app.services.purchase_evidence import (
    ESTABLISHED_LISTING_MIN_AGE_DAYS,
    MIN_REVIEWS_FOR_PROXY,
    MIN_SAMPLE_FOR_WINSORIZED_MEAN,
)

PRICE_EVIDENCE_VERSION = "price_evidence_v1"
PRICE_EVIDENCE_FEATURES_VERSION = "price_evidence_features_v1"
PRICE_BAND_VERSION = "price_band_v1"

DIM_PRICE_EVIDENCE_DERIVED = "price_evidence"

# --------------------------------------------------------------- thresholds
#
# 4B reuses existing repository constants wherever one already covers the
# question, so it introduces as few new assumptions as possible:
#
#   MIN_PRICE_COMPARABLES            (3A) paid comparables below which a band
#                                    is flagged insufficient_evidence
#   MIN_SAMPLE_FOR_WINSORIZED_MEAN   (4A) sample below which a trimmed
#                                    central statistic is not reported
#   ESTABLISHED_LISTING_MIN_AGE_DAYS (4A) "established" listing age
#   MIN_REVIEWS_FOR_PROXY            (4A) reviews that constitute proxy evidence
#
# Exactly one new threshold is introduced:

# P90 needs a real tail to mean anything. Below this many paid comparables
# the 90th percentile is barely distinguishable from the maximum and would
# misrepresent itself as a distribution statistic, so it is not reported.
# UNVALIDATED V1 ASSUMPTION — it appears in no approved specification.
MIN_SAMPLE_FOR_P90 = 10

# Trimming proportion for the robust central statistic: the lowest and
# highest 10% of paid prices are dropped before averaging, so one premium
# bundle cannot set the central figure. At least one value is always dropped
# from each end, so the statistic is never a plain mean wearing a trimmed
# label. UNVALIDATED V1 ASSUMPTION.
TRIMMED_MEAN_PROPORTION = 0.1

LIMITATIONS = (
    "Observed prices are public ASKING prices, not verified transaction prices. "
    "Discounts, coupons, and sales are not visible in public listing data.",
    "An asking price is never willingness to pay, never revenue, and never an "
    "optimal or recommended price. This module recommends no price.",
    "Price bands are computed PER CURRENCY. Cross-currency comparison was NOT "
    "performed: no approved FX source exists, and inventing rates would fabricate "
    "comparisons. No currency was discarded in favour of a more common one.",
    "Listing prices are NOT normalized per item, template, or page. A bundle and a "
    "single item are each one listing at one asking price; public listing data "
    "carries no trustworthy unit quantity to divide by.",
    "A $0 listing is a free competitor: OBSERVED evidence, excluded from paid-price "
    "statistics, never treated as missing data and never as a paid comparable.",
    "A listing with no OBSERVED price is UNKNOWN. It is never read as zero and "
    "never as free.",
    "An OBSERVED price that is neither positive nor zero is invalid provider data: "
    "excluded from every statistic and counted as invalid_price_listing_count, so "
    "paid + free + invalid always reconciles with the observed-price count.",
    "Derived statistics are INFERRED from OBSERVED public fields; they are not "
    "observations. Raw observed prices are preserved alongside them.",
    "Small samples are reported as insufficient rather than presented as stable "
    "distributions. Sample minimums are unvalidated V1 assumptions.",
    "Search-ranked marketplace results are a provider-ordered sample, not the full "
    "market, so an observed band describes what was sampled, not the whole market.",
    PAID_COMPARABLE_DEFINITION,
)


class PriceEvidenceBasis(str, Enum):
    """What the price figures are derived from.

    OBSERVED_ASKING_PRICE is the only basis 4B can produce. TRANSACTION_PRICE
    reserves the interface for a future authorized transactional source; no
    such source exists in this repository and nothing here can produce it.
    """

    OBSERVED_ASKING_PRICE = "OBSERVED_ASKING_PRICE"
    TRANSACTION_PRICE = "TRANSACTION_PRICE"


@dataclass(slots=True, frozen=True)
class PriceBand:
    """Observed asking-price statistics for ONE currency.

    Every figure is an asking-price statistic for the sampled listings in
    this currency alone. Nothing here is compared against another currency.
    """

    currency: str
    paid_listing_count: int
    # Raw observed asking prices, preserved so robust statistics never
    # destroy or overwrite the source observations.
    observed_prices: tuple[float, ...]
    min_paid_asking_price: float
    p25_asking_price: float
    median_asking_price: float
    p75_asking_price: float
    # None below MIN_SAMPLE_FOR_P90: a 90th percentile on a tiny sample is
    # not a distribution statistic.
    p90_asking_price: float | None
    max_paid_asking_price: float
    interquartile_range: float
    # None unless mathematically valid (at least two prices and a positive
    # mean). Reported as a dispersion measure, never as a quality judgment.
    coefficient_of_variation: float | None
    # Trimmed mean; None below MIN_SAMPLE_FOR_WINSORIZED_MEAN, where trimming
    # provides no real robustness.
    trimmed_mean_asking_price: float | None
    # Sub-population medians, each None when the evidence does not support one.
    established_listing_median_asking_price: float | None
    established_listing_count: int
    purchase_proxy_median_asking_price: float | None
    purchase_proxy_listing_count: int
    listings_without_seller_id: int
    distinct_seller_count: int
    insufficient_evidence: bool
    band_version: str = PRICE_BAND_VERSION


@dataclass(slots=True, frozen=True)
class PriceEvidenceProvenance:
    candidate_id: UUID
    evidence_ids: tuple[UUID, ...]
    listing_ids: tuple[str, ...]
    providers: tuple[str, ...]
    marketplaces: tuple[str, ...]
    source_references: tuple[str, ...]
    source_truth_classes: tuple[str, ...]
    source_purposes: tuple[str, ...]
    earliest_retrieved_at: datetime | None
    latest_retrieved_at: datetime | None
    duplicate_evidence_suppressed: int


@dataclass(slots=True, frozen=True)
class PriceEvidenceFeatures:
    """Global price-evidence counts plus one band per observed currency."""

    total_relevant_listings: int
    listings_with_observed_price: int
    listings_with_unknown_price: int
    free_listing_count: int
    paid_comparable_count: int
    # An OBSERVED price that is neither positive nor zero — invalid provider
    # data. Reported so that paid + free + invalid always reconciles with
    # listings_with_observed_price, rather than leaving a silent gap.
    invalid_price_listing_count: int
    # Paid listings the marketplace priced without naming a currency. They
    # are real paid comparables but belong to no band, so they are reported
    # rather than silently dropped or assigned a currency.
    paid_listings_without_currency: int
    excluded_physical_listing_count: int
    unknown_format_listing_count: int
    # Proportion of listings with an OBSERVED price that are free. None when
    # no price was observed at all.
    free_proportion_of_priced_listings: float | None
    currencies_observed: tuple[str, ...]
    # One band per currency, ordered by currency code for determinism.
    bands: tuple[PriceBand, ...]
    multiple_currencies_present: bool
    # Permanently False: 4B never compares across currencies.
    cross_currency_comparison: bool = False
    features_version: str = PRICE_EVIDENCE_FEATURES_VERSION


@dataclass(slots=True, frozen=True)
class PriceEvidenceResult:
    candidate_id: UUID
    state: DimensionState
    # Always None in 4B: no price-evidence formula is approved.
    value: float | None
    features: PriceEvidenceFeatures | None
    provenance: PriceEvidenceProvenance
    basis: PriceEvidenceBasis
    value_truth_class: TruthClass | None
    features_truth_class: TruthClass | None
    evidence_truth_basis: TruthClass | None
    # Permanently UNKNOWN: not derivable from public listing data.
    transaction_prices: TruthClass = TruthClass.UNKNOWN
    willingness_to_pay: TruthClass = TruthClass.UNKNOWN
    recommended_price: TruthClass = TruthClass.UNKNOWN
    missing_reason: str | None = None
    limitations: tuple[str, ...] = LIMITATIONS
    dimension_name: str = DIM_PRICE_EVIDENCE_DERIVED
    version: str = PRICE_EVIDENCE_VERSION
    band_version: str = PRICE_BAND_VERSION


# ------------------------------------------------------------------ helpers


def _trimmed_mean(prices: list[float], proportion: float = TRIMMED_MEAN_PROPORTION) -> float:
    """Mean after dropping the lowest and highest `proportion` of values.

    At least one value is always dropped from each end. A plain
    `int(n * proportion)` trims nothing below ten values, which would return
    an arithmetic mean while calling itself trimmed — one premium bundle
    would still set the figure. The effective trim is therefore wider than
    `proportion` on small samples, which is the honest trade: a statistic
    that says it is trimmed must actually be trimmed.
    """
    ordered = sorted(prices)
    cut = max(1, int(len(ordered) * proportion))
    kept = ordered[cut : len(ordered) - cut]
    if not kept:  # too few values to trim from both ends and keep anything
        kept = ordered
    return round(sum(kept) / len(kept), 2)


def _coefficient_of_variation(prices: list[float]) -> float | None:
    """Stdev / mean, only where mathematically valid.

    Requires at least two values (a standard deviation is undefined for one)
    and a positive mean. Paid prices are above zero by definition, so the
    mean is positive whenever any price exists.
    """
    if len(prices) < 2:
        return None
    mean = statistics.fmean(prices)
    if mean <= 0:
        return None
    return round(statistics.stdev(prices) / mean, 4)


def _build_band(currency: str, listings: list[ListingView], now: datetime) -> PriceBand:
    """One currency's observed asking-price band. No cross-currency input."""
    prices = sorted(float(listing.price) for listing in listings)  # type: ignore[arg-type]
    count = len(prices)

    p25 = round(quantile(prices, 0.25), 2)
    median = round(float(statistics.median(prices)), 2)
    p75 = round(quantile(prices, 0.75), 2)

    established = [
        listing
        for listing in listings
        if listing.created_at is not None
        and (now - listing.created_at).days >= ESTABLISHED_LISTING_MIN_AGE_DAYS
    ]
    with_proxy = [
        listing
        for listing in listings
        if listing.review_observed
        and listing.review_count is not None
        and listing.review_count >= MIN_REVIEWS_FOR_PROXY
    ]

    return PriceBand(
        currency=currency,
        paid_listing_count=count,
        observed_prices=tuple(prices),
        min_paid_asking_price=round(prices[0], 2),
        p25_asking_price=p25,
        median_asking_price=median,
        p75_asking_price=p75,
        p90_asking_price=(
            round(quantile(prices, 0.90), 2) if count >= MIN_SAMPLE_FOR_P90 else None
        ),
        max_paid_asking_price=round(prices[-1], 2),
        interquartile_range=round(p75 - p25, 2),
        coefficient_of_variation=_coefficient_of_variation(prices),
        trimmed_mean_asking_price=(
            _trimmed_mean(prices) if count >= MIN_SAMPLE_FOR_WINSORIZED_MEAN else None
        ),
        established_listing_median_asking_price=(
            round(float(statistics.median(sorted(float(l.price) for l in established))), 2)  # type: ignore[arg-type]
            if established
            else None
        ),
        established_listing_count=len(established),
        purchase_proxy_median_asking_price=(
            round(float(statistics.median(sorted(float(l.price) for l in with_proxy))), 2)  # type: ignore[arg-type]
            if with_proxy
            else None
        ),
        purchase_proxy_listing_count=len(with_proxy),
        listings_without_seller_id=sum(1 for l in listings if l.seller_id is None),
        distinct_seller_count=len({l.seller_id for l in listings if l.seller_id is not None}),
        insufficient_evidence=count < MIN_PRICE_COMPARABLES,
    )


def _build_features(listings: list[ListingView], now: datetime) -> PriceEvidenceFeatures:
    excluded_physical = sum(1 for listing in listings if not listing.is_format_relevant)
    relevant = [listing for listing in listings if listing.is_format_relevant]
    unknown_format = sum(1 for listing in relevant if listing.is_digital is None)

    priced = [listing for listing in relevant if listing.price_observed]
    free = [listing for listing in relevant if listing.is_free_listing]
    paid = [listing for listing in relevant if listing.is_paid_comparable]
    paid_without_currency = [listing for listing in paid if listing.currency is None]
    # A negative price cannot be a paid comparable or a free listing. It is
    # invalid provider data: excluded from every statistic, counted openly.
    invalid = [
        listing
        for listing in priced
        if not listing.is_paid_comparable and not listing.is_free_listing
    ]

    # Group paid listings by currency. Every currency gets its own band; none
    # is dropped because another is more common.
    by_currency: dict[str, list[ListingView]] = {}
    for listing in paid:
        if listing.currency is not None:
            by_currency.setdefault(listing.currency, []).append(listing)

    bands = tuple(
        _build_band(currency, by_currency[currency], now)
        for currency in sorted(by_currency)
    )

    return PriceEvidenceFeatures(
        total_relevant_listings=len(relevant),
        listings_with_observed_price=len(priced),
        listings_with_unknown_price=len(relevant) - len(priced),
        free_listing_count=len(free),
        paid_comparable_count=len(paid),
        invalid_price_listing_count=len(invalid),
        paid_listings_without_currency=len(paid_without_currency),
        excluded_physical_listing_count=excluded_physical,
        unknown_format_listing_count=unknown_format,
        free_proportion_of_priced_listings=(
            round(len(free) / len(priced), 4) if priced else None
        ),
        currencies_observed=tuple(sorted(by_currency)),
        bands=bands,
        multiple_currencies_present=len(by_currency) > 1,
    )


def _build_provenance(
    candidate_id: UUID,
    evidence: list[EvidenceItem],
    listings: list[ListingView],
    evidence_ids: tuple[UUID, ...],
    suppressed: int,
) -> PriceEvidenceProvenance:
    ids = set(evidence_ids)
    contributing = [item for item in evidence if item.id in ids]
    retrieved = sorted(i.retrieved_at for i in contributing if i.retrieved_at is not None)
    return PriceEvidenceProvenance(
        candidate_id=candidate_id,
        evidence_ids=evidence_ids,
        listing_ids=tuple(listing.listing_id for listing in listings),
        providers=tuple(sorted({i.provider for i in contributing})),
        marketplaces=tuple(sorted({i.marketplace for i in contributing if i.marketplace})),
        source_references=tuple(
            sorted({i.source_reference for i in contributing if i.source_reference})
        ),
        source_truth_classes=tuple(sorted({i.truth_class.value for i in contributing})),
        source_purposes=tuple(sorted({i.purpose.value for i in contributing})),
        earliest_retrieved_at=retrieved[0] if retrieved else None,
        latest_retrieved_at=retrieved[-1] if retrieved else None,
        duplicate_evidence_suppressed=suppressed,
    )


def extract_price_evidence(
    candidate_id: UUID,
    evidence: list[EvidenceItem],
    now: datetime | None = None,
) -> PriceEvidenceResult:
    """Derive Price Evidence for one candidate from stored evidence.

    Deterministic and pure: identical evidence yields identical output. No
    provider calls, no LLM, no fabricated values, no FX conversion, and no
    price recommendation of any kind.
    """
    reference_time = now or datetime.now(UTC)
    listings, suppressed, evidence_ids = collect_listing_views(evidence)
    provenance = _build_provenance(candidate_id, evidence, listings, evidence_ids, suppressed)
    contributing = [item for item in evidence if item.id in set(evidence_ids)]
    basis_class = weakest_truth_class(contributing)

    def result(state, features, missing_reason=None):
        return PriceEvidenceResult(
            candidate_id=candidate_id,
            state=state,
            value=None,
            features=features,
            provenance=provenance,
            basis=PriceEvidenceBasis.OBSERVED_ASKING_PRICE,
            value_truth_class=None,
            features_truth_class=TruthClass.INFERRED if features else None,
            evidence_truth_basis=basis_class,
            missing_reason=missing_reason,
        )

    if not listings:
        return result(
            DimensionState.MISSING, None, "no_marketplace_comparables_collected"
        )

    features = _build_features(listings, reference_time)

    if features.total_relevant_listings == 0:
        return result(
            DimensionState.MISSING,
            features,
            "all_comparables_excluded_as_unrelated_format",
        )

    if features.listings_with_observed_price == 0:
        # Comparables exist but no price was measured for any of them. The
        # price field is UNKNOWN, which is not the same as free or as zero.
        return result(
            DimensionState.UNKNOWN,
            features,
            "no_prices_observed_for_any_comparable",
        )

    # Prices were observed — including the case where every observed price is
    # zero (an all-free field), which is a measurement, not an absence.
    #
    # No 0-100 value: no price-evidence formula is approved, and 4B does not
    # invent one.
    return result(DimensionState.EVIDENCE_PRESENT_UNSCORED, features)
