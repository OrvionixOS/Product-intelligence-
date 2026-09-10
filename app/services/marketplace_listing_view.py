"""Shared reconstruction of marketplace listings from stored evidence.

Milestones 4A (Purchase Evidence) and 4B (Price Evidence) both derive
features from the marketplace evidence 3A collected and 3C resolved. They
must never disagree about what a listing is, which listings are relevant, or
what counts as a paid comparable — so those definitions live here once
rather than being restated in each module.

The canonical definitions are:

    format relevance    a physical listing is not a comparable for a digital
                        product. `is_digital is False` excludes; None means
                        the marketplace did not say, so the listing is kept
                        and counted as unknown-relevance, never dropped.

    paid comparable     the price was OBSERVED and is greater than zero.
                        A $0 listing is a free competitor: real evidence,
                        but never a paid comparable.

    free listing        the price was OBSERVED and equals zero. This is a
                        measurement, not missing data.

    unknown price       no OBSERVED price record. Stays unknown; it is never
                        read as zero and never as free.

The evidence record — not the payload snapshot — is the authority on what
was observed. A price is read only from a record whose truth_class is
OBSERVED, so a listing whose price was never returned keeps None.

Currency is deliberately NOT part of the paid-comparable definition. A
listing priced in any currency is a listing someone charges money for.
Whether it is eligible for a particular price band is a separate question,
because bands are per-currency and this repository has no approved FX
source (see app/services/price_evidence.py).
"""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from app.domain.enums import TruthClass
from app.domain.models import EvidenceItem

MARKETPLACE_LISTING_VIEW_VERSION = "marketplace_listing_view_v1"

# Signal types emitted by the 3A evidence builder.
SIGNAL_LISTING_PRICE = "marketplace_listing_price"
SIGNAL_REVIEW_PROXY = "marketplace_review_count_purchase_proxy"

PAID_COMPARABLE_DEFINITION = (
    "A paid comparable is a listing whose price was OBSERVED and is greater "
    "than zero. A $0 listing is a free competitor, never a paid comparable. "
    "A listing with no OBSERVED price is unknown, never zero and never free."
)


@dataclass(slots=True, frozen=True)
class ListingView:
    """One deduplicated marketplace listing, rebuilt from stored evidence.

    Every optional field is None unless an OBSERVED evidence record supplied
    it. Nothing here is inferred, defaulted, or zero-filled.
    """

    listing_id: str
    seller_id: str | None
    # Price is populated only from an OBSERVED price record. 0.0 is a real
    # observed price (a free listing); None means no price was observed.
    price: float | None
    price_observed: bool
    currency: str | None
    review_count: int | None
    review_observed: bool
    created_at: datetime | None
    is_digital: bool | None
    evidence_ids: tuple[UUID, ...]

    @property
    def is_format_relevant(self) -> bool:
        """False only for a listing the marketplace said is physical."""
        return self.is_digital is not False

    @property
    def is_paid_comparable(self) -> bool:
        """Observed price above zero. The single canonical definition."""
        return self.price_observed and self.price is not None and self.price > 0

    @property
    def is_free_listing(self) -> bool:
        """Observed price of exactly zero — a free competitor, not missing data."""
        return self.price_observed and self.price == 0

    @property
    def has_unknown_price(self) -> bool:
        return not self.price_observed


def as_datetime(value) -> datetime | None:
    """Parse a payload timestamp without inventing one."""
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def quantile(sorted_values: list[float], q: float) -> float:
    """Deterministic linear-interpolation quantile over a sorted list."""
    if not sorted_values:
        raise ValueError("quantile of an empty sequence")
    if len(sorted_values) == 1:
        return sorted_values[0]
    position = q * (len(sorted_values) - 1)
    low = int(position)
    high = min(low + 1, len(sorted_values) - 1)
    weight = position - low
    return sorted_values[low] * (1 - weight) + sorted_values[high] * weight


def collect_listing_views(
    evidence: list[EvidenceItem],
) -> tuple[list[ListingView], int, tuple[UUID, ...]]:
    """Rebuild deduplicated listings from stored marketplace evidence.

    Returns (views, duplicate_evidence_suppressed, contributing_evidence_ids).

    One canonical listing per listing_id; every evidence record that
    referenced it contributes its id to the lineage. Re-delivered copies of
    the same observation are collapsed and counted, never double-counted.
    """
    by_listing: dict[str, dict] = {}
    order: list[str] = []
    suppressed = 0
    seen_records: set[tuple] = set()
    contributing: list[UUID] = []

    for item in evidence:
        if item.signal_type not in (SIGNAL_REVIEW_PROXY, SIGNAL_LISTING_PRICE):
            continue
        payload = item.raw_payload or {}
        listing_id = payload.get("listing_id")
        if not listing_id:
            continue

        fingerprint = (
            item.signal_type,
            listing_id,
            item.raw_payload_hash,
            str(item.raw_value),
        )
        if item.raw_payload_hash is not None and fingerprint in seen_records:
            suppressed += 1
            continue
        seen_records.add(fingerprint)
        contributing.append(item.id)

        if listing_id not in by_listing:
            by_listing[listing_id] = {
                "seller_id": payload.get("seller_id"),
                "price": None,
                "price_observed": False,
                "currency": payload.get("currency"),
                "review_count": None,
                "review_observed": False,
                "created_at": payload.get("created_at"),
                "is_digital": payload.get("is_digital"),
                "evidence_ids": [],
            }
            order.append(listing_id)

        record = by_listing[listing_id]
        record["evidence_ids"].append(item.id)

        observed = item.truth_class == TruthClass.OBSERVED and item.raw_value is not None
        if item.signal_type == SIGNAL_REVIEW_PROXY and observed:
            record["review_count"] = int(item.raw_value)
            record["review_observed"] = True
        elif item.signal_type == SIGNAL_LISTING_PRICE and observed:
            record["price"] = float(item.raw_value)
            record["price_observed"] = True

    views = [
        ListingView(
            listing_id=listing_id,
            seller_id=by_listing[listing_id]["seller_id"],
            price=by_listing[listing_id]["price"],
            price_observed=by_listing[listing_id]["price_observed"],
            currency=by_listing[listing_id]["currency"],
            review_count=by_listing[listing_id]["review_count"],
            review_observed=by_listing[listing_id]["review_observed"],
            created_at=as_datetime(by_listing[listing_id]["created_at"]),
            is_digital=by_listing[listing_id]["is_digital"],
            evidence_ids=tuple(by_listing[listing_id]["evidence_ids"]),
        )
        for listing_id in order
    ]
    return views, suppressed, tuple(contributing)


def weakest_truth_class(items: list[EvidenceItem]) -> TruthClass | None:
    """Most conservative class among contributing evidence, or None if empty."""
    if not items:
        return None
    precedence = {
        TruthClass.UNKNOWN: 3,
        TruthClass.INFERRED: 2,
        TruthClass.ESTIMATED: 1,
        TruthClass.OBSERVED: 0,
    }
    return max((item.truth_class for item in items), key=lambda c: precedence[c])
