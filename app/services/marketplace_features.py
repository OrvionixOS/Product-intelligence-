"""Deterministic candidate-level marketplace feature extraction.

Version: marketplace_features_v1.

These summaries are robust descriptive statistics over observed listings.
They are NOT the Opportunity Score and encode no judgment such as
"low competition is good" or an "optimal price".

Naming rule enforced here: review-based features are a PURCHASE PROXY.
Reviews indicate that some purchases occurred; they are never sales counts,
units, or revenue — those remain UNKNOWN.
"""

import math
from dataclasses import dataclass, field
from datetime import UTC, datetime
from statistics import median
from uuid import UUID

from app.providers.base import MarketplaceListing

MARKETPLACE_FEATURES_VERSION = "marketplace_features_v1"

# Fewer paid comparables than this is reported as insufficient evidence
# rather than producing quartiles from too little data.
MIN_PRICE_COMPARABLES = 3


def percentile(values: list[float], p: float) -> float:
    """Deterministic linear-interpolation percentile (p in [0, 1])."""
    if not values:
        raise ValueError("percentile of empty list")
    ordered = sorted(values)
    k = (len(ordered) - 1) * p
    lower = math.floor(k)
    upper = math.ceil(k)
    if lower == upper:
        return float(ordered[int(k)])
    return round(ordered[lower] + (ordered[upper] - ordered[lower]) * (k - lower), 4)


def _rating_distribution(listings: list[MarketplaceListing]) -> dict[str, int]:
    """Counts of listings by rating rounded to the nearest half star."""
    distribution: dict[str, int] = {}
    for listing in listings:
        if listing.rating is not None:
            bucket = f"{round(listing.rating * 2) / 2:.1f}"
            distribution[bucket] = distribution.get(bucket, 0) + 1
    return distribution


@dataclass(slots=True)
class PurchaseProxySummary:
    """Review-based purchase PROXY features. Not purchases. Not sales."""

    candidate_id: UUID
    relevant_listing_count: int
    listings_with_review_data: int
    listings_with_reviews: int
    median_review_count: float | None
    upper_quartile_review_count: float | None
    review_concentration: float | None  # top listing's share of observed reviews
    rating_distribution: dict[str, int] = field(default_factory=dict)
    missing_data_count: int = 0
    features_version: str = MARKETPLACE_FEATURES_VERSION


@dataclass(slots=True)
class PriceSummary:
    candidate_id: UUID
    relevant_paid_comparable_count: int
    unique_seller_count: int
    currency: str | None
    min_price: float | None
    p25_price: float | None
    median_price: float | None
    p75_price: float | None
    max_price: float | None
    missing_data_count: int = 0
    insufficient_evidence: bool = False
    features_version: str = MARKETPLACE_FEATURES_VERSION


@dataclass(slots=True)
class CompetitionSummary:
    """Observable competition features. No opportunity judgment is made here."""

    candidate_id: UUID
    relevant_listing_count: int
    unique_seller_count: int
    seller_concentration: float | None  # top seller's share of listings
    review_burden_median: float | None  # median reviews among listings with review data
    review_burden_p75: float | None
    price_dispersion: float | None  # IQR / median price, when price evidence adequate
    rating_distribution: dict[str, int] = field(default_factory=dict)
    median_listing_age_days: float | None = None
    listings_with_age_data: int = 0
    missing_data_count: int = 0
    features_version: str = MARKETPLACE_FEATURES_VERSION


def summarize_purchase_proxy(
    candidate_id: UUID, listings: list[MarketplaceListing]
) -> PurchaseProxySummary:
    with_data = [l for l in listings if l.review_count is not None]
    counts = [l.review_count for l in with_data]
    with_reviews = [c for c in counts if c > 0]
    total_reviews = sum(counts)

    concentration = None
    if total_reviews > 0:
        concentration = round(max(counts) / total_reviews, 4)

    return PurchaseProxySummary(
        candidate_id=candidate_id,
        relevant_listing_count=len(listings),
        listings_with_review_data=len(with_data),
        listings_with_reviews=len(with_reviews),
        median_review_count=float(median(counts)) if counts else None,
        upper_quartile_review_count=percentile([float(c) for c in counts], 0.75) if counts else None,
        review_concentration=concentration,
        rating_distribution=_rating_distribution(listings),
        missing_data_count=len(listings) - len(with_data),
    )


def summarize_price(candidate_id: UUID, listings: list[MarketplaceListing]) -> PriceSummary:
    priced = [l for l in listings if l.price is not None and l.price > 0]
    currencies = sorted({l.currency for l in priced if l.currency})

    # Only compare like with like: restrict to the most common currency.
    currency: str | None = None
    if priced:
        by_currency: dict[str | None, int] = {}
        for l in priced:
            by_currency[l.currency] = by_currency.get(l.currency, 0) + 1
        currency = max(by_currency, key=lambda c: (by_currency[c], str(c)))
        priced = [l for l in priced if l.currency == currency]

    prices = [l.price for l in priced]
    sellers = {l.seller_id for l in priced if l.seller_id}
    insufficient = len(prices) < MIN_PRICE_COMPARABLES

    summary = PriceSummary(
        candidate_id=candidate_id,
        relevant_paid_comparable_count=len(prices),
        unique_seller_count=len(sellers),
        currency=currency,
        min_price=None,
        p25_price=None,
        median_price=None,
        p75_price=None,
        max_price=None,
        missing_data_count=len(listings) - len(prices),
        insufficient_evidence=insufficient,
    )
    if not insufficient:
        summary.min_price = min(prices)
        summary.p25_price = percentile(prices, 0.25)
        summary.median_price = float(median(prices))
        summary.p75_price = percentile(prices, 0.75)
        summary.max_price = max(prices)
    # Mixed currencies beyond the dominant one count as missing comparables,
    # already reflected in relevant_paid_comparable_count.
    del currencies
    return summary


def summarize_competition(
    candidate_id: UUID, listings: list[MarketplaceListing]
) -> CompetitionSummary:
    sellers = [l.seller_id for l in listings if l.seller_id]
    unique_sellers = set(sellers)

    seller_concentration = None
    if sellers:
        counts: dict[str, int] = {}
        for seller in sellers:
            counts[seller] = counts.get(seller, 0) + 1
        seller_concentration = round(max(counts.values()) / len(listings), 4)

    review_counts = [float(l.review_count) for l in listings if l.review_count is not None]

    price_summary = summarize_price(candidate_id, listings)
    price_dispersion = None
    if (
        not price_summary.insufficient_evidence
        and price_summary.median_price
        and price_summary.p75_price is not None
        and price_summary.p25_price is not None
    ):
        price_dispersion = round(
            (price_summary.p75_price - price_summary.p25_price) / price_summary.median_price, 4
        )

    now = datetime.now(UTC)
    ages = [
        (now - l.created_at).days for l in listings if l.created_at is not None
    ]

    missing = sum(
        1
        for l in listings
        if l.seller_id is None or l.review_count is None or l.created_at is None
    )

    return CompetitionSummary(
        candidate_id=candidate_id,
        relevant_listing_count=len(listings),
        unique_seller_count=len(unique_sellers),
        seller_concentration=seller_concentration,
        review_burden_median=float(median(review_counts)) if review_counts else None,
        review_burden_p75=percentile(review_counts, 0.75) if review_counts else None,
        price_dispersion=price_dispersion,
        rating_distribution=_rating_distribution(listings),
        median_listing_age_days=float(median(ages)) if ages else None,
        listings_with_age_data=len(ages),
        missing_data_count=missing,
    )
