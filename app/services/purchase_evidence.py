"""Purchase Evidence extraction (Milestone 4A).

The question this answers:

    What evidence exists that buyers actually spend money on this TYPE of
    product?

The questions it deliberately does NOT answer, because the data to answer
them is not public and is never fabricated here:

    How many units did this competitor sell?
    What is this competitor's revenue?
    Will this product sell?
    What is the probability of success?

Exact competitor sales and revenue remain UNKNOWN. Always.

Evidence hierarchy
------------------

DIRECT / OBSERVED purchase evidence requires legitimately authorized
transactional data for a seller or store (authorized orders, receipts,
transactions, refunds, revenue). This repository contains no approved
infrastructure for seller OAuth or connected-store transactions, so **4A
implements none of it**. `PurchaseEvidenceSource.DIRECT_AUTHORIZED` exists
only to reserve the interface; nothing in this milestone can produce it, and
a test asserts that.

PUBLIC PURCHASE PROXIES are what 4A actually works with: review counts,
review presence, listing longevity, active paid comparable listings,
distinct seller counts, and the breadth of established paid products in the
same problem/format area. These support purchase-related *inference*. They
are not sales.

Forbidden conversions, none of which appear anywhere in this module:

    review_count            -> sales
    reviews                 -> revenue
    listing presence        -> purchase count
    price x review_count    -> revenue

A review count is one step removed from a purchase: not every purchase
produces a review, review timing lags purchases, and the ratio between them
is unknown and unmeasured. Treating a review count as a unit count would be
a fabrication.

Input
-----

Stored evidence already collected by 3A and resolved by 3C. This is a pure
derivation over the append-only store: it makes no provider calls, spends no
quota, and creates no second marketplace research system.

Output
------

A structured feature vector plus a dimension state. No 0-100 score is
produced. The repository specification approves no purchase-evidence
formula, and 3A/3C both declined to invent one for marketplace evidence, so
4A returns EVIDENCE_PRESENT_UNSCORED rather than manufacturing a number.
`DimensionState.SCORED` remains available for when a formula is approved.

Robustness
----------

Review-count distributions on a marketplace are heavily skewed: one
long-running bestseller can hold more reviews than every other listing
combined. Arithmetic means are therefore avoided throughout. The module
reports medians, quartiles, a winsorized mean, and robust counts, so a
single extreme incumbent cannot dominate the picture.
"""

import statistics
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from uuid import UUID

from app.domain.enums import TruthClass
from app.domain.models import EvidenceItem
from app.services.preliminary_dimensions import (
    SIGNAL_LISTING_PRICE,
    SIGNAL_REVIEW_PROXY,
    DimensionState,
)

PURCHASE_EVIDENCE_VERSION = "purchase_evidence_v1"
PURCHASE_EVIDENCE_FEATURES_VERSION = "purchase_evidence_features_v1"
MARKET_VALIDATION_PATTERN_VERSION = "market_validation_pattern_v1"

DIM_PURCHASE_EVIDENCE = "purchase_evidence"

# ---------------------------------------------------------------- V1 thresholds
#
# Every value below is an explicit V1 ASSUMPTION. None of them appears in an
# approved repository specification, none has been validated against outcome
# data, and all are versioned by MARKET_VALIDATION_PATTERN_VERSION so that
# changing one is a visible, attributable change.

# A listing carries purchase-proxy evidence once it has at least this many
# OBSERVED reviews. One review is weak evidence, but it is evidence that at
# least one buyer completed a purchase and returned to review it.
MIN_REVIEWS_FOR_PROXY = 1

# A listing is "established" once it has existed at least this long. Six
# months is a judgment call: long enough that a listing is not a brand-new
# experiment, short enough not to exclude most of a live market.
ESTABLISHED_LISTING_MIN_AGE_DAYS = 180

# Distinct sellers carrying proxy evidence needed before evidence is called
# distributed rather than merely present across a few shops.
MIN_SELLERS_FOR_DISTRIBUTED = 4

# Below this many sellers with proxy evidence, the signal is called weak
# regardless of how many reviews a single shop has accumulated.
MIN_SELLERS_FOR_MULTIPLE = 2

# At or above this share of observed review-proxy volume held by one seller,
# the evidence is described as concentrated in an incumbent.
CONCENTRATION_DOMINANCE_THRESHOLD = 0.6

# At or below this many listings carrying proxy evidence, the signal is weak.
WEAK_PROXY_MAX_LISTINGS = 2

# Winsorization cap for the robust central measure: values above this
# quantile are pulled down to it, so one bestseller cannot dominate.
WINSORIZE_QUANTILE = 0.9

# Below this many observed review counts, winsorization provides no real
# robustness: with four values the 90th percentile sits beside the maximum,
# so a single extreme listing would still dominate a "robust" mean and the
# number would misrepresent itself. Below the minimum the winsorized mean is
# reported as None — not computed — rather than as a figure that looks
# typical and is not. The median and quartiles stay available at any size.
MIN_SAMPLE_FOR_WINSORIZED_MEAN = 5

LIMITATIONS = (
    "Review counts are a PURCHASE PROXY: they are never units sold, buyers, or revenue.",
    "Not every purchase produces a review, review timing lags purchases, and the "
    "ratio of reviews to purchases is unknown and is not estimated here.",
    "Exact competitor sales and revenue are not public and remain UNKNOWN.",
    "Listing presence shows an observable paid market, not competitor performance.",
    "Search-ranked marketplace results are a provider-ordered sample, not the full market.",
    "Derived statistics are INFERRED from OBSERVED public fields; they are not observations.",
    "Market-validation pattern thresholds are unvalidated V1 assumptions, not findings.",
    "This is a market-validation signal about a product TYPE. It is not proof that a "
    "new product will sell, and carries no probability of success.",
)

DIRECT_EVIDENCE_LIMITATION = (
    "No authorized transactional data source is connected. Direct purchase evidence "
    "(orders, receipts, transactions, refunds, revenue) requires seller authorization "
    "that this milestone does not implement, so none is available."
)


class PurchaseEvidenceSource(str, Enum):
    """Where purchase evidence came from.

    DIRECT_AUTHORIZED reserves the interface for legitimately authorized
    transactional data. Milestone 4A cannot produce it: no seller OAuth and
    no connected-store transactions exist in this repository.
    """

    DIRECT_AUTHORIZED = "DIRECT_AUTHORIZED"
    PUBLIC_PROXY = "PUBLIC_PROXY"


class MarketValidationPattern(str, Enum):
    """The SHAPE of public purchase-proxy evidence, not its desirability.

    These describe how proxy evidence is distributed across a market. They
    are deliberately not ranked: DISTRIBUTED is not "better" than
    CONCENTRATED, and CONCENTRATED is not a warning. A concentrated market
    may mean one incumbent proved demand others have not contested; a
    distributed one may mean many sellers compete for the same buyers.
    Interpreting that trade-off is not this module's job.
    """

    # No marketplace comparables were observed at all.
    NO_COMPARABLES = "NO_COMPARABLES"
    # Comparables exist, but no review count was observed for any of them.
    UNKNOWN_PROXY = "UNKNOWN_PROXY"
    # Review counts were OBSERVED and every one of them is zero. This is a
    # measured absence of proxy evidence, not missing data.
    NO_PUBLIC_PROXY = "NO_PUBLIC_PROXY"
    # Proxy evidence exists but sits on very few listings or one seller.
    WEAK_PROXY = "WEAK_PROXY"
    # Proxy volume is dominated by a single seller.
    CONCENTRATED = "CONCENTRATED"
    # Several sellers carry proxy evidence, none dominant.
    MULTIPLE_ESTABLISHED = "MULTIPLE_ESTABLISHED"
    # Proxy evidence spans many distinct sellers.
    DISTRIBUTED = "DISTRIBUTED"


@dataclass(slots=True, frozen=True)
class ProxyListing:
    """One deduplicated comparable listing, rebuilt from stored evidence.

    `review_count` is populated only when the stored evidence record was
    truth_class=OBSERVED. A listing whose review count was never looked up
    keeps None and is counted as unknown — never as zero.
    """

    listing_id: str
    seller_id: str | None
    review_count: int | None
    review_observed: bool
    price_observed: bool
    currency: str | None
    created_at: datetime | None
    is_digital: bool | None
    evidence_ids: tuple[UUID, ...]


@dataclass(slots=True, frozen=True)
class PurchaseEvidenceProvenance:
    """Lineage of every derived feature, preserved intact."""

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
class PurchaseEvidenceFeatures:
    """Deterministic purchase-PROXY features. None of these is a sales figure."""

    # Comparable inventory
    relevant_comparable_count: int
    paid_comparable_count: int
    distinct_seller_count: int
    excluded_physical_listing_count: int
    unknown_format_listing_count: int

    # Review proxy availability. Observed-zero and unknown are separate.
    listings_with_observed_review_count: int
    listings_with_unknown_review_count: int
    listings_with_observed_zero_reviews: int
    listings_with_proxy_evidence: int
    proportion_of_comparables_with_proxy: float | None

    # Robust central measures over OBSERVED review counts. No arithmetic
    # mean: the distribution is skewed and one bestseller must not dominate.
    # The winsorized mean is None below MIN_SAMPLE_FOR_WINSORIZED_MEAN, where
    # winsorizing would not actually deliver the robustness it implies.
    median_review_count: float | None
    upper_quartile_review_count: float | None
    max_review_count: int | None
    winsorized_mean_review_count: float | None

    # Distribution across sellers
    sellers_with_proxy_evidence: int
    top_seller_proxy_share: float | None

    # Longevity
    listings_with_creation_date: int
    listing_age_days_median: float | None
    established_listing_count: int
    established_seller_count: int

    # Currencies seen, recorded for transparency only. Review counts are
    # currency-independent, so a mixed-currency market cannot alter any
    # purchase-proxy statistic above.
    currencies_observed: tuple[str, ...]
    mixed_currencies: bool

    features_version: str = PURCHASE_EVIDENCE_FEATURES_VERSION


@dataclass(slots=True, frozen=True)
class PurchaseEvidenceResult:
    """Purchase Evidence for one candidate: features, state, provenance."""

    candidate_id: UUID
    state: DimensionState
    # Always None in 4A: no purchase-evidence formula is approved.
    value: float | None
    pattern: MarketValidationPattern
    features: PurchaseEvidenceFeatures | None
    provenance: PurchaseEvidenceProvenance
    source: PurchaseEvidenceSource
    # Derived numbers are INFERRED even when every input was OBSERVED.
    value_truth_class: TruthClass | None
    features_truth_class: TruthClass | None
    # The strongest class among contributing evidence, reported separately.
    evidence_truth_basis: TruthClass | None
    # Never anything but UNKNOWN. Exact sales/revenue are not public.
    exact_units_sold: TruthClass = TruthClass.UNKNOWN
    exact_revenue: TruthClass = TruthClass.UNKNOWN
    direct_authorized_evidence_available: bool = False
    missing_reason: str | None = None
    limitations: tuple[str, ...] = LIMITATIONS
    dimension_name: str = DIM_PURCHASE_EVIDENCE
    version: str = PURCHASE_EVIDENCE_VERSION
    pattern_version: str = MARKET_VALIDATION_PATTERN_VERSION


# ------------------------------------------------------------------ extraction


def _quantile(sorted_values: list[float], q: float) -> float:
    """Deterministic linear-interpolation quantile over a sorted list."""
    if not sorted_values:
        raise ValueError("empty")
    if len(sorted_values) == 1:
        return sorted_values[0]
    position = q * (len(sorted_values) - 1)
    low = int(position)
    high = min(low + 1, len(sorted_values) - 1)
    weight = position - low
    return sorted_values[low] * (1 - weight) + sorted_values[high] * weight


def _winsorized_mean(values: list[int], quantile: float = WINSORIZE_QUANTILE) -> float:
    """Mean after capping values at `quantile`, so one outlier cannot dominate."""
    ordered = sorted(float(v) for v in values)
    cap = _quantile(ordered, quantile)
    return round(sum(min(v, cap) for v in ordered) / len(ordered), 4)


def _collect_listings(
    evidence: list[EvidenceItem],
) -> tuple[list[ProxyListing], int, tuple[UUID, ...]]:
    """Rebuild deduplicated comparable listings from stored evidence.

    One canonical listing per listing_id; every evidence record that
    referenced it contributes its id to the lineage. Re-delivered copies of
    the same observation are collapsed and counted, never double-counted.

    The evidence record — not the payload snapshot — is the authority on
    what was observed: a review count is read only from a record whose
    truth_class is OBSERVED.
    """
    by_listing: dict[str, dict] = {}
    order: list[str] = []
    suppressed = 0
    seen_records: set[tuple] = set()
    all_ids: list[UUID] = []

    for item in evidence:
        if item.signal_type not in (SIGNAL_REVIEW_PROXY, SIGNAL_LISTING_PRICE):
            continue
        payload = item.raw_payload or {}
        listing_id = payload.get("listing_id")
        if not listing_id:
            continue

        # Collapse identical re-delivered observations of one measurement.
        fingerprint = (item.signal_type, listing_id, item.raw_payload_hash, str(item.raw_value))
        if item.raw_payload_hash is not None and fingerprint in seen_records:
            suppressed += 1
            continue
        seen_records.add(fingerprint)
        all_ids.append(item.id)

        if listing_id not in by_listing:
            by_listing[listing_id] = {
                "seller_id": payload.get("seller_id"),
                "review_count": None,
                "review_observed": False,
                "price_observed": False,
                "currency": payload.get("currency"),
                "created_at": payload.get("created_at"),
                "is_digital": payload.get("is_digital"),
                "evidence_ids": [],
            }
            order.append(listing_id)

        record = by_listing[listing_id]
        record["evidence_ids"].append(item.id)

        if item.signal_type == SIGNAL_REVIEW_PROXY:
            # OBSERVED is the only class that establishes a review count.
            # UNKNOWN means "not looked up", which stays None, not zero.
            if item.truth_class == TruthClass.OBSERVED and item.raw_value is not None:
                record["review_count"] = int(item.raw_value)
                record["review_observed"] = True
        elif item.signal_type == SIGNAL_LISTING_PRICE:
            if item.truth_class == TruthClass.OBSERVED and item.raw_value is not None:
                record["price_observed"] = True

    listings = [
        ProxyListing(
            listing_id=listing_id,
            seller_id=by_listing[listing_id]["seller_id"],
            review_count=by_listing[listing_id]["review_count"],
            review_observed=by_listing[listing_id]["review_observed"],
            price_observed=by_listing[listing_id]["price_observed"],
            currency=by_listing[listing_id]["currency"],
            created_at=_as_datetime(by_listing[listing_id]["created_at"]),
            is_digital=by_listing[listing_id]["is_digital"],
            evidence_ids=tuple(by_listing[listing_id]["evidence_ids"]),
        )
        for listing_id in order
    ]
    return listings, suppressed, tuple(all_ids)


def _as_datetime(value) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def _build_provenance(
    candidate_id: UUID,
    evidence: list[EvidenceItem],
    listings: list[ProxyListing],
    evidence_ids: tuple[UUID, ...],
    suppressed: int,
) -> PurchaseEvidenceProvenance:
    contributing = [item for item in evidence if item.id in set(evidence_ids)]
    retrieved = sorted(i.retrieved_at for i in contributing if i.retrieved_at is not None)
    return PurchaseEvidenceProvenance(
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


def _classify(features: PurchaseEvidenceFeatures) -> MarketValidationPattern:
    """market_validation_pattern_v1. Describes evidence shape, not quality.

    Order matters: each rule is only reached when the ones above it did not
    apply, which is what keeps the classification explainable.
    """
    if features.relevant_comparable_count == 0:
        return MarketValidationPattern.NO_COMPARABLES
    if features.listings_with_observed_review_count == 0:
        # Comparables exist but nothing was measured. Not an absence of
        # purchase evidence — an absence of measurement.
        return MarketValidationPattern.UNKNOWN_PROXY
    if features.listings_with_proxy_evidence == 0:
        # Review counts were measured and all were zero. A measured absence.
        return MarketValidationPattern.NO_PUBLIC_PROXY
    if (
        features.sellers_with_proxy_evidence < MIN_SELLERS_FOR_MULTIPLE
        or features.listings_with_proxy_evidence <= WEAK_PROXY_MAX_LISTINGS
    ):
        return MarketValidationPattern.WEAK_PROXY
    if (
        features.top_seller_proxy_share is not None
        and features.top_seller_proxy_share >= CONCENTRATION_DOMINANCE_THRESHOLD
    ):
        return MarketValidationPattern.CONCENTRATED
    if features.sellers_with_proxy_evidence >= MIN_SELLERS_FOR_DISTRIBUTED:
        return MarketValidationPattern.DISTRIBUTED
    return MarketValidationPattern.MULTIPLE_ESTABLISHED


def _build_features(
    listings: list[ProxyListing], now: datetime
) -> PurchaseEvidenceFeatures:
    # Format relevance: a physical listing is not a comparable for a digital
    # product. is_digital=None means the marketplace did not say, so the
    # listing is kept and counted as unknown-relevance rather than dropped.
    excluded_physical = sum(1 for listing in listings if listing.is_digital is False)
    relevant = [listing for listing in listings if listing.is_digital is not False]
    unknown_format = sum(1 for listing in relevant if listing.is_digital is None)

    observed = [listing for listing in relevant if listing.review_observed]
    unknown_reviews = len(relevant) - len(observed)
    review_counts = [listing.review_count for listing in observed]  # type: ignore[misc]
    observed_zero = sum(1 for count in review_counts if count == 0)
    with_proxy = [
        listing
        for listing in observed
        if listing.review_count is not None
        and listing.review_count >= MIN_REVIEWS_FOR_PROXY
    ]

    median = upper_quartile = winsorized = None
    max_reviews = None
    if review_counts:
        ordered = sorted(float(c) for c in review_counts)
        median = round(float(statistics.median(ordered)), 4)
        upper_quartile = round(_quantile(ordered, 0.75), 4)
        max_reviews = max(review_counts)
        if len(review_counts) >= MIN_SAMPLE_FOR_WINSORIZED_MEAN:
            winsorized = _winsorized_mean(review_counts)

    # Proxy volume by seller. This is a share of OBSERVED REVIEW COUNTS —
    # not market share, not revenue share, not units. Listings with no
    # seller id are excluded from the concentration statistic rather than
    # merged into a fictional single seller.
    by_seller: dict[str, int] = {}
    for listing in with_proxy:
        if listing.seller_id is not None:
            by_seller[listing.seller_id] = (
                by_seller.get(listing.seller_id, 0) + (listing.review_count or 0)
            )
    total_proxy = sum(by_seller.values())
    top_share = (
        round(max(by_seller.values()) / total_proxy, 4)
        if by_seller and total_proxy > 0
        else None
    )

    ages = [
        (now - listing.created_at).days
        for listing in relevant
        if listing.created_at is not None
    ]
    established = [
        listing
        for listing in relevant
        if listing.created_at is not None
        and (now - listing.created_at).days >= ESTABLISHED_LISTING_MIN_AGE_DAYS
    ]

    currencies = tuple(sorted({l.currency for l in relevant if l.currency is not None}))

    return PurchaseEvidenceFeatures(
        relevant_comparable_count=len(relevant),
        paid_comparable_count=sum(1 for listing in relevant if listing.price_observed),
        distinct_seller_count=len({l.seller_id for l in relevant if l.seller_id is not None}),
        excluded_physical_listing_count=excluded_physical,
        unknown_format_listing_count=unknown_format,
        listings_with_observed_review_count=len(observed),
        listings_with_unknown_review_count=unknown_reviews,
        listings_with_observed_zero_reviews=observed_zero,
        listings_with_proxy_evidence=len(with_proxy),
        proportion_of_comparables_with_proxy=(
            round(len(with_proxy) / len(observed), 4) if observed else None
        ),
        median_review_count=median,
        upper_quartile_review_count=upper_quartile,
        max_review_count=max_reviews,
        winsorized_mean_review_count=winsorized,
        sellers_with_proxy_evidence=len(by_seller),
        top_seller_proxy_share=top_share,
        listings_with_creation_date=len(ages),
        listing_age_days_median=round(float(statistics.median(ages)), 4) if ages else None,
        established_listing_count=len(established),
        established_seller_count=len(
            {l.seller_id for l in established if l.seller_id is not None}
        ),
        currencies_observed=currencies,
        mixed_currencies=len(currencies) > 1,
    )


def extract_purchase_evidence(
    candidate_id: UUID,
    evidence: list[EvidenceItem],
    now: datetime | None = None,
) -> PurchaseEvidenceResult:
    """Derive Purchase Evidence for one candidate from stored evidence.

    Deterministic and pure: identical evidence yields identical output. No
    provider calls, no LLM, no fabricated values, and no conversion of any
    proxy into a sales or revenue figure.
    """
    reference_time = now or datetime.now(UTC)
    listings, suppressed, evidence_ids = _collect_listings(evidence)
    provenance = _build_provenance(candidate_id, evidence, listings, evidence_ids, suppressed)

    contributing = [item for item in evidence if item.id in set(evidence_ids)]
    # Most conservative class among the sources that actually contributed.
    basis: TruthClass | None = None
    if contributing:
        precedence = {
            TruthClass.UNKNOWN: 3,
            TruthClass.INFERRED: 2,
            TruthClass.ESTIMATED: 1,
            TruthClass.OBSERVED: 0,
        }
        basis = max((i.truth_class for i in contributing), key=lambda c: precedence[c])

    if not listings:
        return PurchaseEvidenceResult(
            candidate_id=candidate_id,
            state=DimensionState.MISSING,
            value=None,
            pattern=MarketValidationPattern.NO_COMPARABLES,
            features=None,
            provenance=provenance,
            source=PurchaseEvidenceSource.PUBLIC_PROXY,
            value_truth_class=None,
            features_truth_class=None,
            evidence_truth_basis=basis,
            missing_reason="no_marketplace_comparables_collected",
        )

    features = _build_features(listings, reference_time)
    pattern = _classify(features)

    if features.relevant_comparable_count == 0:
        # Every comparable was excluded as an unrelated (physical) format.
        return PurchaseEvidenceResult(
            candidate_id=candidate_id,
            state=DimensionState.MISSING,
            value=None,
            pattern=MarketValidationPattern.NO_COMPARABLES,
            features=features,
            provenance=provenance,
            source=PurchaseEvidenceSource.PUBLIC_PROXY,
            value_truth_class=None,
            features_truth_class=TruthClass.INFERRED,
            evidence_truth_basis=basis,
            missing_reason="all_comparables_excluded_as_unrelated_format",
        )

    if pattern == MarketValidationPattern.UNKNOWN_PROXY:
        # Comparables were observed; their review counts were not. The proxy
        # is UNKNOWN, which is not the same as zero purchase evidence.
        return PurchaseEvidenceResult(
            candidate_id=candidate_id,
            state=DimensionState.UNKNOWN,
            value=None,
            pattern=pattern,
            features=features,
            provenance=provenance,
            source=PurchaseEvidenceSource.PUBLIC_PROXY,
            value_truth_class=None,
            features_truth_class=TruthClass.INFERRED,
            evidence_truth_basis=basis,
            missing_reason="no_review_counts_observed_for_any_comparable",
        )

    # Real proxy features exist — including the case where every observed
    # review count is zero, which is a measurement, not an absence.
    #
    # No 0-100 value: the repository specification approves no
    # purchase-evidence formula, and 4A does not invent one.
    return PurchaseEvidenceResult(
        candidate_id=candidate_id,
        state=DimensionState.EVIDENCE_PRESENT_UNSCORED,
        value=None,
        pattern=pattern,
        features=features,
        provenance=provenance,
        source=PurchaseEvidenceSource.PUBLIC_PROXY,
        value_truth_class=None,
        features_truth_class=TruthClass.INFERRED,
        evidence_truth_basis=basis,
    )
