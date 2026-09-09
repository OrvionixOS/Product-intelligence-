"""Milestone 4A tests: Purchase Evidence.

Every provider is an in-memory fake. No network, no live API calls.

The central invariant under test: public marketplace proxies are never
converted into sales, units, or revenue, and a missing measurement is never
rewritten as zero.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.domain.enums import EvidencePurpose, TruthClass
from app.domain.models import EvidenceItem
from app.providers.base import MarketplaceListing
from app.services.preliminary_dimensions import DimensionState
from app.services.purchase_evidence import (
    CONCENTRATION_DOMINANCE_THRESHOLD,
    ESTABLISHED_LISTING_MIN_AGE_DAYS,
    MARKET_VALIDATION_PATTERN_VERSION,
    PURCHASE_EVIDENCE_VERSION,
    MarketValidationPattern,
    PurchaseEvidenceSource,
    extract_purchase_evidence,
)
from app.services.research_orchestration import (
    DERIVATION_PURCHASE_EVIDENCE,
    STATUS_DERIVATION_COMPLETE,
    STATUS_DERIVATION_ERROR,
    STATUS_DERIVATION_NOT_REQUESTED,
    run_preliminary_research,
)
from app.storage.memory import ResearchStore
from tests.test_orchestration import (
    FakeMarketplaceProvider,
    listing_for,
    make_candidate,
)

NOW = datetime(2026, 1, 1, tzinfo=UTC)


# ----------------------------------------------------------------- fixtures


def evidence_for_listing(
    listing: MarketplaceListing,
    candidate_id: UUID,
    review_observed: bool = True,
    provider: str = "etsy",
) -> list[EvidenceItem]:
    """Build the same evidence shape 3A emits for one (candidate, listing)."""
    payload: dict[str, Any] = {
        "listing_id": listing.listing_id,
        "title": listing.title,
        "url": listing.url,
        "price": listing.price,
        "currency": listing.currency,
        "seller_id": listing.seller_id,
        "rating": listing.rating,
        "review_count": listing.review_count,
        "created_at": listing.created_at,
        "state": listing.state,
        "taxonomy": listing.taxonomy,
        "listing_type": listing.listing_type,
        "is_digital": listing.is_digital,
        "retrieved_at": listing.retrieved_at,
    }
    common = dict(
        candidate_id=candidate_id,
        provider=provider,
        collection_method="official_api",
        source_reference="/v3/listings",
        marketplace=provider,
        retrieved_at=listing.retrieved_at,
        raw_payload=payload,
        raw_payload_hash=f"hash-{listing.listing_id}",
    )
    price_observed = listing.price is not None
    review_ok = review_observed and listing.review_count is not None
    return [
        EvidenceItem(
            signal_type="marketplace_listing_price",
            purpose=EvidencePurpose.PRICE,
            truth_class=TruthClass.OBSERVED if price_observed else TruthClass.UNKNOWN,
            raw_value=listing.price,
            **common,
        ),
        EvidenceItem(
            signal_type="marketplace_review_count_purchase_proxy",
            purpose=EvidencePurpose.PURCHASE,
            truth_class=TruthClass.OBSERVED if review_ok else TruthClass.UNKNOWN,
            raw_value=listing.review_count if review_ok else None,
            **common,
        ),
        EvidenceItem(
            signal_type="marketplace_competing_listing",
            purpose=EvidencePurpose.COMPETITION,
            truth_class=TruthClass.OBSERVED,
            raw_value=None,
            **common,
        ),
    ]


def comparable(
    listing_id: str,
    review_count: int | None = 10,
    seller_id: str | None = "shop-1",
    price: float | None = 12.5,
    currency: str | None = "USD",
    age_days: int | None = 400,
    is_digital: bool | None = True,
) -> MarketplaceListing:
    return MarketplaceListing(
        listing_id=listing_id,
        title=f"Listing {listing_id}",
        url=f"https://www.etsy.com/listing/{listing_id}",
        price=price,
        currency=currency,
        seller_id=seller_id,
        review_count=review_count,
        created_at=NOW - timedelta(days=age_days) if age_days is not None else None,
        state="active",
        listing_type="download",
        is_digital=is_digital,
        retrieved_at=datetime(2025, 12, 1, tzinfo=UTC),
    )


def extract(listings, candidate_id=None, review_observed=True):
    candidate_id = candidate_id or uuid4()
    evidence = [
        item
        for listing in listings
        for item in evidence_for_listing(listing, candidate_id, review_observed)
    ]
    return extract_purchase_evidence(candidate_id, evidence, now=NOW)


# --------------------------------------------------- 1. no comparable listings


def test_no_comparable_listings_is_missing_not_zero():
    result = extract([])
    assert result.state == DimensionState.MISSING
    assert result.pattern == MarketValidationPattern.NO_COMPARABLES
    assert result.value is None
    assert result.features is None
    assert result.missing_reason == "no_marketplace_comparables_collected"
    # Nothing was observed, so there is no truth basis to claim.
    assert result.evidence_truth_basis is None


# ------------------------------ 2. listings present with no review counts


def test_comparables_without_any_review_counts_are_unknown_not_zero():
    result = extract(
        [comparable("L1", review_count=None), comparable("L2", review_count=None)],
        review_observed=False,
    )
    assert result.state == DimensionState.UNKNOWN
    assert result.pattern == MarketValidationPattern.UNKNOWN_PROXY
    assert result.value is None
    assert result.missing_reason == "no_review_counts_observed_for_any_comparable"

    features = result.features
    assert features.relevant_comparable_count == 2
    assert features.listings_with_observed_review_count == 0
    assert features.listings_with_unknown_review_count == 2
    # Critically: unknown is not counted as an observed zero.
    assert features.listings_with_observed_zero_reviews == 0
    assert features.median_review_count is None
    assert features.proportion_of_comparables_with_proxy is None


# --------------------- 3. observed zero reviews vs missing review count


def test_observed_zero_reviews_is_distinguishable_from_missing():
    observed_zero = extract([comparable("L1", review_count=0), comparable("L2", review_count=0)])
    unknown = extract(
        [comparable("L1", review_count=None), comparable("L2", review_count=None)],
        review_observed=False,
    )

    # Observed zero is a measurement: the market was checked and no reviews
    # exist. That is evidence, and it is not the same as never looking.
    assert observed_zero.state == DimensionState.EVIDENCE_PRESENT_UNSCORED
    assert observed_zero.pattern == MarketValidationPattern.NO_PUBLIC_PROXY
    assert observed_zero.features.listings_with_observed_zero_reviews == 2
    assert observed_zero.features.listings_with_observed_review_count == 2
    assert observed_zero.features.listings_with_unknown_review_count == 0
    assert observed_zero.features.median_review_count == 0.0
    assert observed_zero.features.proportion_of_comparables_with_proxy == 0.0

    # Unknown is the absence of measurement.
    assert unknown.state == DimensionState.UNKNOWN
    assert unknown.pattern == MarketValidationPattern.UNKNOWN_PROXY
    assert unknown.features.median_review_count is None

    assert observed_zero.state != unknown.state
    assert observed_zero.pattern != unknown.pattern


def test_mixed_observed_zero_and_unknown_are_counted_separately():
    candidate_id = uuid4()
    evidence = evidence_for_listing(comparable("L1", review_count=0), candidate_id)
    evidence += evidence_for_listing(
        comparable("L2", review_count=None), candidate_id, review_observed=False
    )
    result = extract_purchase_evidence(candidate_id, evidence, now=NOW)

    features = result.features
    assert features.listings_with_observed_zero_reviews == 1
    assert features.listings_with_unknown_review_count == 1
    assert features.listings_with_observed_review_count == 1


# ------------------------------ 4. one dominant listing with many reviews


def test_single_dominant_incumbent_is_concentrated_and_does_not_skew_median():
    counts = [5000, 3, 4, 2, 3]
    listings = [
        comparable(f"L{i}", review_count=c, seller_id=f"shop-{i}")
        for i, c in enumerate(counts)
    ]
    result = extract(listings)

    assert result.pattern == MarketValidationPattern.CONCENTRATED
    features = result.features
    assert features.top_seller_proxy_share >= CONCENTRATION_DOMINANCE_THRESHOLD
    # The robust measures are not dragged up by the one bestseller.
    assert features.median_review_count == 3.0
    assert features.max_review_count == 5000
    arithmetic_mean = sum(counts) / len(counts)
    assert features.winsorized_mean_review_count < arithmetic_mean, (
        "winsorization must pull the central measure below the arithmetic mean"
    )
    # And no sales figure appears anywhere.
    assert result.value is None
    assert result.exact_units_sold == TruthClass.UNKNOWN
    assert result.exact_revenue == TruthClass.UNKNOWN


# ---------------------- 5. several sellers with distributed review evidence


def test_distributed_proxy_evidence_across_many_sellers():
    listings = [
        comparable(f"L{i}", review_count=20 + i, seller_id=f"shop-{i}") for i in range(5)
    ]
    result = extract(listings)

    assert result.pattern == MarketValidationPattern.DISTRIBUTED
    assert result.features.sellers_with_proxy_evidence == 5
    assert result.features.top_seller_proxy_share < CONCENTRATION_DOMINANCE_THRESHOLD
    assert result.state == DimensionState.EVIDENCE_PRESENT_UNSCORED


def test_few_sellers_with_proxy_is_multiple_established_not_distributed():
    listings = [
        comparable("L1", review_count=30, seller_id="shop-a"),
        comparable("L2", review_count=25, seller_id="shop-b"),
        comparable("L3", review_count=28, seller_id="shop-c"),
    ]
    assert extract(listings).pattern == MarketValidationPattern.MULTIPLE_ESTABLISHED


def test_single_seller_proxy_is_weak_not_distributed():
    listings = [
        comparable("L1", review_count=40, seller_id="only-shop"),
        comparable("L2", review_count=30, seller_id="only-shop"),
        comparable("L3", review_count=0, seller_id="other"),
    ]
    assert extract(listings).pattern == MarketValidationPattern.WEAK_PROXY


# ----------------------------------- 6. duplicate listing evidence collapses


def test_duplicate_listing_evidence_is_collapsed_not_double_counted():
    candidate_id = uuid4()
    listing = comparable("L1", review_count=12)
    evidence = evidence_for_listing(listing, candidate_id)
    # The identical observation delivered twice describes one listing.
    result = extract_purchase_evidence(candidate_id, evidence + evidence, now=NOW)

    assert result.features.relevant_comparable_count == 1
    assert result.features.listings_with_observed_review_count == 1
    assert result.provenance.duplicate_evidence_suppressed > 0
    assert result.provenance.listing_ids == ("L1",)


# ------------------ 7. shared listings across candidates keep provenance


def test_shared_listing_across_candidates_preserves_separate_provenance():
    listing = comparable("SHARED", review_count=9)
    candidate_a, candidate_b = uuid4(), uuid4()
    result_a = extract_purchase_evidence(
        candidate_a, evidence_for_listing(listing, candidate_a), now=NOW
    )
    result_b = extract_purchase_evidence(
        candidate_b, evidence_for_listing(listing, candidate_b), now=NOW
    )

    assert result_a.provenance.candidate_id == candidate_a
    assert result_b.provenance.candidate_id == candidate_b
    assert result_a.provenance.listing_ids == result_b.provenance.listing_ids == ("SHARED",)
    # Each candidate carries its own evidence records for that listing.
    assert set(result_a.provenance.evidence_ids).isdisjoint(result_b.provenance.evidence_ids)
    # The derived features are identical because the observation is identical.
    assert result_a.features == result_b.features


# ------------------------------------------- 8. missing creation dates


def test_missing_creation_dates_are_reported_not_assumed():
    listings = [
        comparable("L1", review_count=10, age_days=400),
        comparable("L2", review_count=10, age_days=None),
        comparable("L3", review_count=10, age_days=None),
    ]
    features = extract(listings).features

    assert features.listings_with_creation_date == 1
    assert features.established_listing_count == 1
    # No age was invented for the two listings without a creation date.
    assert features.listing_age_days_median == 400.0


def test_new_listings_are_not_counted_as_established():
    young = ESTABLISHED_LISTING_MIN_AGE_DAYS - 1
    listings = [comparable("L1", review_count=5, age_days=young)]
    features = extract(listings).features
    assert features.listings_with_creation_date == 1
    assert features.established_listing_count == 0


# ---------------------------------------------------- 9. mixed currencies


def test_mixed_currencies_do_not_contaminate_purchase_proxy_features():
    """Review counts are currency-independent; a mixed market cannot alter them."""
    single = [
        comparable("L1", review_count=10, currency="USD", seller_id="a"),
        comparable("L2", review_count=20, currency="USD", seller_id="b"),
        comparable("L3", review_count=30, currency="USD", seller_id="c"),
    ]
    mixed = [
        comparable("L1", review_count=10, currency="USD", seller_id="a"),
        comparable("L2", review_count=20, currency="EUR", seller_id="b"),
        comparable("L3", review_count=30, currency="GBP", seller_id="c"),
    ]
    single_features = extract(single).features
    mixed_features = extract(mixed).features

    for name in (
        "median_review_count",
        "upper_quartile_review_count",
        "max_review_count",
        "winsorized_mean_review_count",
        "listings_with_proxy_evidence",
        "sellers_with_proxy_evidence",
        "top_seller_proxy_share",
    ):
        assert getattr(single_features, name) == getattr(mixed_features, name), name

    assert mixed_features.mixed_currencies is True
    assert mixed_features.currencies_observed == ("EUR", "GBP", "USD")
    assert single_features.mixed_currencies is False
    assert extract(single).pattern == extract(mixed).pattern


# ------------------------- 10. unrelated product formats excluded


def test_physical_listings_are_excluded_from_digital_purchase_evidence():
    listings = [
        comparable("D1", review_count=10, seller_id="a", is_digital=True),
        comparable("D2", review_count=12, seller_id="b", is_digital=True),
        comparable("P1", review_count=900, seller_id="phys", is_digital=False),
    ]
    features = extract(listings).features

    assert features.excluded_physical_listing_count == 1
    assert features.relevant_comparable_count == 2
    # The physical bestseller's 900 reviews contribute to nothing.
    assert features.max_review_count == 12
    assert features.sellers_with_proxy_evidence == 2
    assert features.listings_with_proxy_evidence == 2
    numeric = [
        getattr(features, name)
        for name in dir(features)
        if not name.startswith("_") and isinstance(getattr(features, name), (int, float))
    ]
    assert 900 not in numeric, "excluded physical listing leaked into a statistic"


def test_unknown_format_listings_are_kept_and_counted_not_dropped():
    listings = [
        comparable("D1", review_count=10, seller_id="a", is_digital=True),
        comparable("U1", review_count=8, seller_id="b", is_digital=None),
    ]
    features = extract(listings).features
    assert features.relevant_comparable_count == 2
    assert features.unknown_format_listing_count == 1
    assert features.excluded_physical_listing_count == 0


def test_all_comparables_excluded_as_physical_is_missing():
    result = extract([comparable("P1", review_count=50, is_digital=False)])
    assert result.state == DimensionState.MISSING
    assert result.missing_reason == "all_comparables_excluded_as_unrelated_format"
    assert result.features.excluded_physical_listing_count == 1


# ------------------------- 11. exact competitor sales remain UNKNOWN


def test_exact_competitor_sales_and_revenue_always_unknown():
    for listings in (
        [],
        [comparable("L1", review_count=None)],
        [comparable("L1", review_count=0)],
        [comparable(f"L{i}", review_count=100 * i, seller_id=f"s{i}") for i in range(1, 6)],
    ):
        result = extract(listings)
        assert result.exact_units_sold == TruthClass.UNKNOWN
        assert result.exact_revenue == TruthClass.UNKNOWN
        assert result.direct_authorized_evidence_available is False
        assert result.source == PurchaseEvidenceSource.PUBLIC_PROXY


def test_direct_authorized_evidence_is_never_produced_in_4a():
    """The interface is reserved; 4A implements no seller OAuth."""
    result = extract([comparable("L1", review_count=42)])
    assert result.source != PurchaseEvidenceSource.DIRECT_AUTHORIZED
    assert result.direct_authorized_evidence_available is False


# -------------------- 12. no review-count-to-sales conversion anywhere


def test_no_review_to_sales_or_revenue_conversion_exists_in_the_module():
    """Static check: the forbidden arithmetic must not exist in source."""
    import pathlib
    import re

    source = pathlib.Path("app/services/purchase_evidence.py").read_text()

    # The only places a sales or revenue quantity may be named are the two
    # fields that declare it UNKNOWN. Both must be TruthClass-typed, so
    # neither can ever hold a number.
    assert "exact_units_sold: TruthClass = TruthClass.UNKNOWN" in source
    assert "exact_revenue: TruthClass = TruthClass.UNKNOWN" in source

    # No other identifier claims to be a sales, units, or revenue quantity.
    forbidden_names = [
        name
        for name in re.findall(
            r"^\s*(?:def\s+)?(\w*(?:units_sold|sales|revenue|earnings|turnover)\w*)\s*[=(:]",
            source,
            re.MULTILINE,
        )
        if name not in ("exact_units_sold", "exact_revenue")
    ]
    assert not forbidden_names, f"forbidden quantity names: {forbidden_names}"

    # No arithmetic multiplying price by a review count, in either order.
    assert not re.search(r"price\w*\s*\*\s*\w*review", source, re.IGNORECASE)
    assert not re.search(r"review\w*\s*\*\s*\w*price", source, re.IGNORECASE)
    # No multiplication of a review count by anything at all.
    assert not re.search(r"review_count\w*\s*\*", source)


def test_price_is_never_multiplied_into_a_proxy_figure():
    """A priced market with reviews yields no revenue-shaped number."""
    listings = [
        comparable("L1", review_count=100, price=50.0, seller_id="a"),
        comparable("L2", review_count=200, price=25.0, seller_id="b"),
        comparable("L3", review_count=150, price=30.0, seller_id="c"),
    ]
    features = extract(listings).features
    values = {
        name: getattr(features, name)
        for name in dir(features)
        if not name.startswith("_") and isinstance(getattr(features, name), (int, float))
    }
    # price x review_count would produce 5000 / 5000 / 4500 or their sum.
    for forbidden in (5000, 5000.0, 4500, 14500, 14500.0):
        assert forbidden not in values.values(), f"revenue-shaped value {forbidden} present"


# --------------------------------------- 13. deterministic for identical input


def test_identical_evidence_yields_identical_output():
    listings = [
        comparable(f"L{i}", review_count=i * 7, seller_id=f"shop-{i % 3}") for i in range(1, 9)
    ]
    candidate_id = uuid4()
    evidence = [
        item for listing in listings for item in evidence_for_listing(listing, candidate_id)
    ]

    first = extract_purchase_evidence(candidate_id, evidence, now=NOW)
    second = extract_purchase_evidence(candidate_id, evidence, now=NOW)

    assert first.features == second.features
    assert first.pattern == second.pattern
    assert first.state == second.state
    assert first.provenance == second.provenance


def test_evidence_order_does_not_change_derived_features():
    listings = [
        comparable(f"L{i}", review_count=i * 5, seller_id=f"shop-{i}") for i in range(1, 6)
    ]
    candidate_id = uuid4()
    evidence = [
        item for listing in listings for item in evidence_for_listing(listing, candidate_id)
    ]
    forward = extract_purchase_evidence(candidate_id, evidence, now=NOW)
    backward = extract_purchase_evidence(candidate_id, list(reversed(evidence)), now=NOW)

    assert forward.features == backward.features
    assert forward.pattern == backward.pattern


# ----------------------------- 14. source evidence IDs survive into features


def test_source_evidence_ids_and_lineage_survive_into_derived_features():
    candidate_id = uuid4()
    listing = comparable("L1", review_count=11)
    evidence = evidence_for_listing(listing, candidate_id)
    result = extract_purchase_evidence(candidate_id, evidence, now=NOW)

    provenance = result.provenance
    price_and_review_ids = {
        item.id
        for item in evidence
        if item.signal_type
        in ("marketplace_listing_price", "marketplace_review_count_purchase_proxy")
    }
    assert price_and_review_ids.issubset(set(provenance.evidence_ids))
    assert provenance.listing_ids == ("L1",)
    assert provenance.providers == ("etsy",)
    assert provenance.marketplaces == ("etsy",)
    assert provenance.source_references == ("/v3/listings",)
    assert "PURCHASE" in provenance.source_purposes
    assert "OBSERVED" in provenance.source_truth_classes
    assert provenance.earliest_retrieved_at is not None
    assert result.version == PURCHASE_EVIDENCE_VERSION
    assert result.pattern_version == MARKET_VALIDATION_PATTERN_VERSION
    assert result.limitations


# ---------------- 15. OBSERVED source -> INFERRED derived truth class


def test_derived_features_are_inferred_even_when_all_sources_are_observed():
    result = extract([comparable("L1", review_count=10), comparable("L2", review_count=20)])

    assert result.evidence_truth_basis == TruthClass.OBSERVED
    # The derivation is not an observation.
    assert result.features_truth_class == TruthClass.INFERRED
    assert result.features_truth_class != result.evidence_truth_basis
    # No value exists, so no value truth class is claimed.
    assert result.value is None
    assert result.value_truth_class is None


def test_unknown_source_evidence_downgrades_the_truth_basis():
    result = extract(
        [comparable("L1", review_count=None)],
        review_observed=False,
    )
    # A review record that was never looked up is UNKNOWN, and the basis
    # reports the weakest contributing class rather than the strongest.
    assert result.evidence_truth_basis == TruthClass.UNKNOWN


# ------------------- 16. failure does not destroy other capabilities


@pytest.mark.asyncio
async def test_purchase_evidence_failure_does_not_destroy_the_research_run(monkeypatch):
    import app.services.research_orchestration as orchestration

    def boom(*args: Any, **kwargs: Any):
        raise RuntimeError("purchase-evidence derivation bug")

    monkeypatch.setattr(orchestration, "extract_purchase_evidence", boom)

    candidates = [make_candidate("Alpha", marketplace_queries=["q"])]
    marketplace = FakeMarketplaceProvider({"q": [listing_for("L1")]})
    result = await run_preliminary_research(
        candidates=candidates, store=ResearchStore(), marketplace_provider=marketplace
    )

    outcome = next(
        d for d in result.derivations if d.derivation == DERIVATION_PURCHASE_EVIDENCE
    )
    assert outcome.status == STATUS_DERIVATION_ERROR
    assert "RuntimeError" in outcome.failure_reason
    assert "error_id=" in outcome.failure_reason
    # No partial or fabricated purchase evidence survives the failure.
    assert result.purchase_evidence == {}

    # Everything else is intact: dimensions, ranking, and selection.
    assert len(result.ranking.ranked) == 1
    profile = result.profiles[0]
    assert profile.dimension("preliminary_price_evidence").state == (
        DimensionState.EVIDENCE_PRESENT_UNSCORED
    )
    assert result.capabilities


@pytest.mark.asyncio
async def test_purchase_evidence_runs_for_selected_candidates_from_stored_evidence():
    candidates = [make_candidate(f"Cand {i}", marketplace_queries=[f"q{i}"]) for i in range(3)]
    marketplace = FakeMarketplaceProvider(
        {f"q{i}": [listing_for(f"L{i}", review_count=5)] for i in range(3)}
    )
    result = await run_preliminary_research(
        candidates=candidates, store=ResearchStore(), marketplace_provider=marketplace
    )

    outcome = next(
        d for d in result.derivations if d.derivation == DERIVATION_PURCHASE_EVIDENCE
    )
    assert outcome.status == STATUS_DERIVATION_COMPLETE
    assert outcome.candidates_covered == 3
    assert set(result.purchase_evidence) == {c.candidate_id for c in result.ranking.selected}
    # Derivation costs nothing: it reuses evidence 3A already collected.
    assert len(marketplace.search_calls) == 3


@pytest.mark.asyncio
async def test_purchase_evidence_can_be_skipped_without_affecting_the_run():
    candidates = [make_candidate("Alpha", marketplace_queries=["q"])]
    marketplace = FakeMarketplaceProvider({"q": [listing_for("L1")]})
    result = await run_preliminary_research(
        candidates=candidates,
        store=ResearchStore(),
        marketplace_provider=marketplace,
        derive_purchase_evidence=False,
    )
    outcome = next(
        d for d in result.derivations if d.derivation == DERIVATION_PURCHASE_EVIDENCE
    )
    assert outcome.status == STATUS_DERIVATION_NOT_REQUESTED
    assert result.purchase_evidence == {}
    assert len(result.ranking.ranked) == 1


@pytest.mark.asyncio
async def test_marketplace_provider_failure_leaves_purchase_evidence_missing_not_zero():
    candidates = [make_candidate("Alpha", marketplace_queries=["q"])]
    marketplace = FakeMarketplaceProvider(
        {"q": [listing_for("L1")]}, fail_queries={"q"}
    )
    result = await run_preliminary_research(
        candidates=candidates, store=ResearchStore(), marketplace_provider=marketplace
    )

    evidence = result.purchase_evidence[candidates[0].id]
    assert evidence.state == DimensionState.MISSING
    assert evidence.value is None
    assert evidence.features is None
    assert evidence.pattern == MarketValidationPattern.NO_COMPARABLES


# ------------------------------- 17. no final scoring leakage


def test_no_pos_ecs_or_classification_leaks_into_purchase_evidence():
    result = extract([comparable("L1", review_count=10, seller_id="a")])

    forbidden = {
        "opportunity_score",
        "evidence_confidence",
        "classification",
        "kill_rules_triggered",
        "RED",
        "YELLOW",
        "GREEN",
    }
    assert forbidden.isdisjoint(result.__slots__)
    assert forbidden.isdisjoint(result.features.__slots__)
    for value in (result.pattern.value, result.state.value, result.source.value):
        assert value not in ("RED", "YELLOW", "GREEN")


def test_purchase_evidence_module_cannot_reach_legacy_scoring():
    from tests.test_orchestration import (
        LEGACY_SCORING_MODULE,
        _transitive_app_imports,
    )

    reachable = _transitive_app_imports("app.services.purchase_evidence")
    assert LEGACY_SCORING_MODULE not in reachable


def test_purchase_evidence_produces_no_numeric_score():
    """4A intentionally withholds a 0-100 value: no formula is approved."""
    for listings in (
        [comparable("L1", review_count=0)],
        [comparable(f"L{i}", review_count=i * 10, seller_id=f"s{i}") for i in range(1, 6)],
    ):
        result = extract(listings)
        assert result.value is None
        assert result.state != DimensionState.SCORED
        assert result.value_truth_class is None


# --------------------- 18. no secrets or credentials in output or logs


def test_no_secrets_in_purchase_evidence_output(monkeypatch, caplog):
    import logging

    secret = "etsy-keystring-super-secret"
    monkeypatch.setenv("ETSY_API_KEY", secret)

    listing = comparable("L1", review_count=10)
    candidate_id = uuid4()
    evidence = evidence_for_listing(listing, candidate_id)
    with caplog.at_level(logging.DEBUG):
        result = extract_purchase_evidence(candidate_id, evidence, now=NOW)

    rendered = repr(result)
    assert secret not in rendered
    for pattern in ("api_key", "apikey", "password", "Bearer ", "x-api-key"):
        assert pattern.lower() not in rendered.lower()
    assert secret not in caplog.text


def test_api_response_carries_purchase_evidence_without_sales_or_secrets(monkeypatch):
    from fastapi.testclient import TestClient

    from app.api import routes
    from app.main import app

    monkeypatch.setenv("ETSY_API_KEY", "etsy-secret-value-xyz")
    candidates = [make_candidate("Alpha", marketplace_queries=["q"])]
    marketplace = FakeMarketplaceProvider(
        {"q": [listing_for("L1", review_count=12), listing_for("L2", review_count=8, seller_id="s2")]}
    )
    monkeypatch.setitem(routes.MARKETPLACE_PROVIDERS, "etsy", lambda: marketplace)

    response = TestClient(app).post(
        "/research/preliminary",
        json={
            "candidates": [c.model_dump(mode="json") for c in candidates],
            "search_demand_provider": None,
            "public_content_provider": None,
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()

    assert len(body["purchase_evidence"]) == 1
    entry = body["purchase_evidence"][0]
    assert entry["value"] is None
    assert entry["exact_units_sold"] == "UNKNOWN"
    assert entry["exact_revenue"] == "UNKNOWN"
    assert entry["direct_authorized_evidence_available"] is False
    assert entry["features_truth_class"] == "INFERRED"
    assert entry["provenance"]["evidence_ids"]
    assert entry["limitations"]

    text = response.text
    assert "etsy-secret-value-xyz" not in text
    for forbidden in (
        "units_sold_estimate",
        "estimated_sales",
        "revenue_estimate",
        "opportunity_score",
        "evidence_confidence",
    ):
        assert forbidden not in text


# ----------------------------------------------------------- extra coverage


def test_listings_without_seller_ids_do_not_form_a_fictional_single_seller():
    listings = [
        comparable("L1", review_count=10, seller_id=None),
        comparable("L2", review_count=20, seller_id=None),
    ]
    features = extract(listings).features
    assert features.distinct_seller_count == 0
    assert features.sellers_with_proxy_evidence == 0
    # With no attributable seller, no concentration claim is made.
    assert features.top_seller_proxy_share is None


def test_winsorized_mean_is_bounded_by_the_observed_range():
    listings = [
        comparable(f"L{i}", review_count=c, seller_id=f"s{i}")
        for i, c in enumerate([1, 2, 3, 4, 100000])
    ]
    features = extract(listings).features
    assert features.median_review_count == 3.0
    assert 3.0 <= features.winsorized_mean_review_count <= features.max_review_count
    assert features.winsorized_mean_review_count < 20000


def test_pattern_thresholds_are_versioned_and_exposed():
    from app.services import purchase_evidence as pe

    assert pe.MARKET_VALIDATION_PATTERN_VERSION == "market_validation_pattern_v1"
    assert pe.PURCHASE_EVIDENCE_FEATURES_VERSION == "purchase_evidence_features_v1"
    for threshold in (
        pe.MIN_REVIEWS_FOR_PROXY,
        pe.ESTABLISHED_LISTING_MIN_AGE_DAYS,
        pe.MIN_SELLERS_FOR_DISTRIBUTED,
        pe.MIN_SELLERS_FOR_MULTIPLE,
        pe.CONCENTRATION_DOMINANCE_THRESHOLD,
        pe.WEAK_PROXY_MAX_LISTINGS,
    ):
        assert threshold is not None


def test_extraction_ignores_unrelated_evidence_signals():
    """Search-demand and public-content evidence must not enter this path."""
    candidate_id = uuid4()
    unrelated = EvidenceItem(
        candidate_id=candidate_id,
        signal_type="public_video_view_count",
        purpose=EvidencePurpose.AUDIENCE,
        truth_class=TruthClass.OBSERVED,
        provider="youtube",
        collection_method="official_api",
        raw_value=100000,
        raw_payload={"video_id": "v1"},
        raw_payload_hash="hash-v1",
    )
    evidence = evidence_for_listing(comparable("L1", review_count=4), candidate_id)
    result = extract_purchase_evidence(candidate_id, evidence + [unrelated], now=NOW)

    assert result.features.relevant_comparable_count == 1
    assert unrelated.id not in result.provenance.evidence_ids
    assert result.provenance.providers == ("etsy",)


def test_asyncio_is_not_required_by_the_extractor():
    """The extractor is pure and synchronous; no event loop involvement."""
    assert not asyncio.iscoroutinefunction(extract_purchase_evidence)


def test_winsorized_mean_is_withheld_below_the_minimum_sample():
    """Below the minimum sample, winsorizing delivers no real robustness.

    With four values the 90th percentile sits beside the maximum, so a
    single extreme listing would still dominate a mean that calls itself
    robust. Reporting None is more honest than reporting that number.
    """
    from app.services.purchase_evidence import MIN_SAMPLE_FOR_WINSORIZED_MEAN

    small = [
        comparable("L1", review_count=999999, seller_id="a"),
        comparable("L2", review_count=1, seller_id="b"),
        comparable("L3", review_count=1, seller_id="c"),
        comparable("L4", review_count=1, seller_id="d"),
    ]
    assert len(small) < MIN_SAMPLE_FOR_WINSORIZED_MEAN
    features = extract(small).features
    assert features.winsorized_mean_review_count is None
    # The genuinely robust measures are still reported.
    assert features.median_review_count == 1.0
    assert features.max_review_count == 999999

    # At and above the minimum, it is computed and stays robust.
    large = small + [comparable("L5", review_count=1, seller_id="e")]
    large_features = extract(large).features
    assert large_features.winsorized_mean_review_count is not None
    assert large_features.winsorized_mean_review_count < 999999
