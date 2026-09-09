"""Deterministic candidate-level marketplace feature extraction (Milestone 3A).

Produces robust summary features from public marketplace listing records.
This is NOT the Opportunity Score, makes no GREEN/YELLOW/RED decision, and
never converts a proxy into a sales figure:

- review counts are PURCHASE PROXIES: they show that purchases plausibly
  happened, never how many units were sold or for how much revenue;
- price statistics describe the observed comparable market, never an
  "optimal price";
- competition features are raw observables for a later competition-state
  calculation — "low competition = good" is NOT implemented here.

Quartiles use statistics.quantiles(method="inclusive") so results are
deterministic for the same inputs.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from statistics import median, quantiles
from uuid import UUID

from app.providers.base import MarketplaceListing

MARKETPLACE_FEATURES_VERSION = "marketplace_features_v1"

# Below this many priced comparables, price statistics are reported but the
# summary is flagged insufficient — thin evidence is named, not papered over.
MIN_PRICE_COMPARABLES = 5


def _quartiles(values: list[float]) -> tuple[float, float, float]:
    """(P25, P50, P75) with deterministic inclusive interpolation."""
    if len(values) == 1:
        v = values[0]
        return v, v, v
    q1, q2, q3 = quantiles(values, n=4, method="inclusive")
    return round(q1, 2), round(q2, 2), round(q3, 2)


def _concentration(counts: list[int]) -> float | None:
    """Share of the total held by the largest contributor (0-1)."""
    total = sum(counts)
    if total <= 0:
        return None
    return round(max(counts) / total, 4)


@dataclass(slots=True)
class PurchaseProxySummary:
    """Purchase PROXY features. Not purchases. Not sales. Not revenue."""

    candidate_id: UUID
    relevant_listing_count: int
    unique_seller_count: int
    listings_with_reviews: int
    median_review_count: float | None
    upper_quartile_review_count: float | None
    review_concentration: float | None
    rating_distribution: dict[str, int] = field(default_factory=dict)
    missing_review_data_count: int = 0
    features_version: str = MARKETPLACE_FEATURES_VERSION


@dataclass(slots=True)
class PriceSummary:
    candidate_id: UUID
    relevant_paid_comparable_count: int
    unique_seller_count: int
    min_price: float | None
    p25_price: float | None
    median_price: float | None
    p75_price: float | None
    max_price: float | None
    currency: str | None
    mixed_currencies: bool = False
    missing_price_data_count: int = 0
    insufficient_evidence: bool = True
    features_version: str = MARKETPLACE_FEATURES_VERSION


@dataclass(slots=True)
class CompetitionSummary:
    candidate_id: UUID
    relevant_listing_count: int
    unique_seller_count: int
    seller_concentration: float | None
    review_burden_median: float | None
    review_burden_p75: float | None
    price_dispersion: float | None
    rating_distribution: dict[str, int] = field(default_factory=dict)
    listing_age_days_median: float | None = None
    listings_with_age_data: int = 0
    missing_data_count: int = 0
    features_version: str = MARKETPLACE_FEATURES_VERSION


@dataclass(slots=True)
class MarketplaceCandidateSummary:
    candidate_id: UUID
    purchase_proxy: PurchaseProxySummary
    price: PriceSummary
    competition: CompetitionSummary


def _rating_distribution(listings: list[MarketplaceListing]) -> dict[str, int]:
    distribution: dict[str, int] = {}
    for listing in listings:
        if listing.rating is not None:
            bucket = str(round(listing.rating))
            distribution[bucket] = distribution.get(bucket, 0) + 1
    return distribution


def summarize_purchase_proxy(
    candidate_id: UUID, listings: list[MarketplaceListing]
) -> PurchaseProxySummary:
    sellers = {listing.seller_id for listing in listings if listing.seller_id is not None}
    review_counts = [
        listing.review_count for listing in listings if listing.review_count is not None
    ]
    with_reviews = [count for count in review_counts if count > 0]
    missing = sum(1 for listing in listings if listing.review_count is None)

    median_reviews: float | None = None
    p75_reviews: float | None = None
    if review_counts:
        floats = [float(count) for count in review_counts]
        _, median_reviews, p75_reviews = _quartiles(floats)

    return PurchaseProxySummary(
        candidate_id=candidate_id,
        relevant_listing_count=len(listings),
        unique_seller_count=len(sellers),
        listings_with_reviews=len(with_reviews),
        median_review_count=median_reviews,
        upper_quartile_review_count=p75_reviews,
        review_concentration=_concentration(review_counts) if review_counts else None,
        rating_distribution=_rating_distribution(listings),
        missing_review_data_count=missing,
    )


def summarize_price(candidate_id: UUID, listings: list[MarketplaceListing]) -> PriceSummary:
    priced = [listing for listing in listings if listing.price is not None and listing.price > 0]
    missing = sum(1 for listing in listings if listing.price is None)
    # A price whose currency was not returned has no comparable unit: it is
    # excluded from statistics and counted as missing data, never mixed in.
    missing += sum(1 for listing in priced if listing.currency is None)
    priced = [listing for listing in priced if listing.currency is not None]
    currencies = sorted({listing.currency for listing in priced})
    primary_currency = currencies[0] if len(currencies) == 1 else None

    # Mixed currencies are not silently averaged: statistics are computed only
    # over the dominant single currency when one exists, otherwise reported as
    # insufficient rather than mixing units.
    mixed = len(currencies) > 1
    if mixed:
        by_currency: dict[str, list[MarketplaceListing]] = {}
        for listing in priced:
            if listing.currency is not None:
                by_currency.setdefault(listing.currency, []).append(listing)
        dominant = max(by_currency.items(), key=lambda kv: (len(kv[1]), kv[0]))
        primary_currency = dominant[0]
        priced = dominant[1]

    prices = sorted(listing.price for listing in priced)  # type: ignore[misc]
    sellers = {listing.seller_id for listing in priced if listing.seller_id is not None}

    if prices:
        p25, p50, p75 = _quartiles(prices)
        return PriceSummary(
            candidate_id=candidate_id,
            relevant_paid_comparable_count=len(prices),
            unique_seller_count=len(sellers),
            min_price=round(prices[0], 2),
            p25_price=p25,
            median_price=p50,
            p75_price=p75,
            max_price=round(prices[-1], 2),
            currency=primary_currency,
            mixed_currencies=mixed,
            missing_price_data_count=missing,
            insufficient_evidence=len(prices) < MIN_PRICE_COMPARABLES,
        )
    return PriceSummary(
        candidate_id=candidate_id,
        relevant_paid_comparable_count=0,
        unique_seller_count=len(sellers),
        min_price=None,
        p25_price=None,
        median_price=None,
        p75_price=None,
        max_price=None,
        currency=primary_currency,
        mixed_currencies=mixed,
        missing_price_data_count=missing,
        insufficient_evidence=True,
    )


def summarize_competition(
    candidate_id: UUID, listings: list[MarketplaceListing], now: datetime | None = None
) -> CompetitionSummary:
    reference_time = now or datetime.now(UTC)
    sellers = [listing.seller_id for listing in listings if listing.seller_id is not None]
    seller_listing_counts: dict[str, int] = {}
    for seller in sellers:
        seller_listing_counts[seller] = seller_listing_counts.get(seller, 0) + 1

    review_counts = [
        float(listing.review_count) for listing in listings if listing.review_count is not None
    ]
    review_median: float | None = None
    review_p75: float | None = None
    if review_counts:
        _, review_median, review_p75 = _quartiles(review_counts)

    prices = [listing.price for listing in listings if listing.price is not None and listing.price > 0]
    dispersion: float | None = None
    if len(prices) >= 2:
        p25, p50, p75 = _quartiles(sorted(prices))
        if p50 > 0:
            dispersion = round((p75 - p25) / p50, 4)  # robust IQR/median coefficient

    ages = [
        (reference_time - listing.created_at).days
        for listing in listings
        if listing.created_at is not None
    ]

    missing = sum(
        1
        for listing in listings
        if listing.review_count is None or listing.price is None or listing.created_at is None
    )

    return CompetitionSummary(
        candidate_id=candidate_id,
        relevant_listing_count=len(listings),
        unique_seller_count=len(seller_listing_counts),
        seller_concentration=_concentration(list(seller_listing_counts.values()))
        if seller_listing_counts
        else None,
        review_burden_median=review_median,
        review_burden_p75=review_p75,
        price_dispersion=dispersion,
        rating_distribution=_rating_distribution(listings),
        listing_age_days_median=float(median(ages)) if ages else None,
        listings_with_age_data=len(ages),
        missing_data_count=missing,
    )


def summarize_marketplace(
    candidate_id: UUID, listings: list[MarketplaceListing], now: datetime | None = None
) -> MarketplaceCandidateSummary:
    return MarketplaceCandidateSummary(
        candidate_id=candidate_id,
        purchase_proxy=summarize_purchase_proxy(candidate_id, listings),
        price=summarize_price(candidate_id, listings),
        competition=summarize_competition(candidate_id, listings, now=now),
    )
