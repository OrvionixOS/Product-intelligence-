"""Milestone 4B tests: Price Evidence.

Every provider is an in-memory fake. No network, no live API calls.

Central invariants under test: observed prices are ASKING prices and are
never presented as transaction prices, willingness to pay, revenue, or a
recommendation; currencies are never combined; a $0 listing is a free
competitor rather than missing data or a paid comparable; and bundles are
never silently normalized to a per-unit price.
"""

from typing import Any
from uuid import uuid4

import pytest

from app.domain.enums import TruthClass
from app.services.marketplace_features import MIN_PRICE_COMPARABLES
from app.services.marketplace_listing_view import collect_listing_views
from app.services.preliminary_dimensions import DimensionState
from app.services.price_evidence import (
    MIN_SAMPLE_FOR_P90,
    PRICE_BAND_VERSION,
    PRICE_EVIDENCE_VERSION,
    PriceEvidenceBasis,
    extract_price_evidence,
)
from app.services.purchase_evidence import (
    MIN_SAMPLE_FOR_WINSORIZED_MEAN,
    extract_purchase_evidence,
)
from app.services.research_orchestration import (
    DERIVATION_PRICE_EVIDENCE,
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
from tests.test_purchase_evidence import NOW, comparable, evidence_for_listing


def extract(listings, candidate_id=None, review_observed=True):
    candidate_id = candidate_id or uuid4()
    evidence = [
        item
        for listing in listings
        for item in evidence_for_listing(listing, candidate_id, review_observed)
    ]
    return extract_price_evidence(candidate_id, evidence, now=NOW)


def band_for(result, currency):
    return next(b for b in result.features.bands if b.currency == currency)


# ------------------------------------------------- states: missing/unknown


def test_no_comparables_is_missing_not_zero():
    result = extract([])
    assert result.state == DimensionState.MISSING
    assert result.value is None
    assert result.features is None
    assert result.missing_reason == "no_marketplace_comparables_collected"


def test_comparables_without_observed_prices_are_unknown_not_free():
    """No observed price is UNKNOWN — never zero, never a free listing."""
    result = extract(
        [comparable("L1", price=None, currency=None), comparable("L2", price=None, currency=None)]
    )
    assert result.state == DimensionState.UNKNOWN
    assert result.missing_reason == "no_prices_observed_for_any_comparable"

    features = result.features
    assert features.listings_with_unknown_price == 2
    assert features.listings_with_observed_price == 0
    assert features.free_listing_count == 0, "unknown must never be counted as free"
    assert features.paid_comparable_count == 0
    assert features.bands == ()
    assert features.free_proportion_of_priced_listings is None


def test_all_physical_listings_excluded_is_missing():
    result = extract([comparable("P1", price=20.0, is_digital=False)])
    assert result.state == DimensionState.MISSING
    assert result.missing_reason == "all_comparables_excluded_as_unrelated_format"
    assert result.features.excluded_physical_listing_count == 1


# ------------------------------------------------------- currency handling


def test_currencies_are_never_combined_into_one_distribution():
    """The headline requirement: separate bands, no cross-currency stats."""
    listings = [
        comparable("U1", price=10.0, currency="USD", seller_id="a"),
        comparable("U2", price=20.0, currency="USD", seller_id="b"),
        comparable("E1", price=100.0, currency="EUR", seller_id="c"),
        comparable("E2", price=200.0, currency="EUR", seller_id="d"),
        comparable("G1", price=5.0, currency="GBP", seller_id="e"),
    ]
    result = extract(listings)
    features = result.features

    assert features.currencies_observed == ("EUR", "GBP", "USD")
    assert {b.currency for b in features.bands} == {"USD", "EUR", "GBP"}
    assert features.multiple_currencies_present is True
    assert features.cross_currency_comparison is False

    usd, eur, gbp = band_for(result, "USD"), band_for(result, "EUR"), band_for(result, "GBP")
    assert usd.observed_prices == (10.0, 20.0)
    assert eur.observed_prices == (100.0, 200.0)
    assert gbp.observed_prices == (5.0,)
    # Each band's statistics come only from its own currency.
    assert usd.median_asking_price == 15.0
    assert eur.median_asking_price == 150.0
    assert gbp.median_asking_price == 5.0
    # A combined median over all five prices would be 20.0 — it appears nowhere.
    assert all(b.median_asking_price != 20.0 for b in features.bands)


def test_no_currency_is_discarded_because_another_is_dominant():
    """A minority currency keeps its own band; 3A's dominant-currency
    collapse is deliberately NOT repeated here."""
    listings = [comparable(f"U{i}", price=10.0 + i, currency="USD", seller_id=f"s{i}") for i in range(8)]
    listings.append(comparable("E1", price=99.0, currency="EUR", seller_id="lonely"))
    result = extract(listings)

    currencies = {b.currency for b in result.features.bands}
    assert currencies == {"USD", "EUR"}, "the single EUR listing must not be dropped"
    eur = band_for(result, "EUR")
    assert eur.paid_listing_count == 1
    assert eur.observed_prices == (99.0,)
    # It is reported as insufficient rather than discarded or merged.
    assert eur.insufficient_evidence is True
    # Every paid listing is accounted for across the bands.
    assert sum(b.paid_listing_count for b in result.features.bands) == 9


def test_paid_listings_without_a_currency_are_reported_not_dropped():
    listings = [
        comparable("A", price=10.0, currency="USD", seller_id="a"),
        comparable("B", price=15.0, currency=None, seller_id="b"),
    ]
    features = extract(listings).features

    assert features.paid_comparable_count == 2
    assert features.paid_listings_without_currency == 1
    # The currency-less listing belongs to no band, and is not guessed into one.
    assert sum(b.paid_listing_count for b in features.bands) == 1
    assert features.currencies_observed == ("USD",)


def test_single_currency_market_reports_no_cross_currency_flag():
    result = extract(
        [comparable(f"L{i}", price=10.0 + i, currency="USD", seller_id=f"s{i}") for i in range(3)]
    )
    assert result.features.multiple_currencies_present is False
    assert result.features.cross_currency_comparison is False


# ------------------------------------------------------------ free listings


def test_free_listings_are_retained_but_excluded_from_paid_statistics():
    listings = [
        comparable("F1", price=0.0, currency="USD", seller_id="free1"),
        comparable("F2", price=0.0, currency="USD", seller_id="free2"),
        comparable("P1", price=12.0, currency="USD", seller_id="paid1"),
        comparable("P2", price=18.0, currency="USD", seller_id="paid2"),
    ]
    result = extract(listings)
    features = result.features

    assert features.free_listing_count == 2
    assert features.paid_comparable_count == 2
    assert features.listings_with_observed_price == 4
    assert features.free_proportion_of_priced_listings == 0.5

    usd = band_for(result, "USD")
    # $0 never enters a paid-price statistic.
    assert usd.paid_listing_count == 2
    assert usd.observed_prices == (12.0, 18.0)
    assert usd.min_paid_asking_price == 12.0
    assert usd.median_asking_price == 15.0
    assert 0.0 not in usd.observed_prices


def test_free_listing_is_never_missing_and_never_a_paid_comparable():
    free_only = extract([comparable("F1", price=0.0, currency="USD")])
    features = free_only.features

    # Evidence is present: we measured the market and found free competitors.
    assert free_only.state == DimensionState.EVIDENCE_PRESENT_UNSCORED
    assert features.free_listing_count == 1
    assert features.listings_with_unknown_price == 0, "free is not missing"
    assert features.paid_comparable_count == 0, "free is not paid"
    assert features.bands == (), "no paid band exists with no paid listings"
    assert features.free_proportion_of_priced_listings == 1.0


def test_free_and_unknown_prices_are_counted_separately():
    candidate_id = uuid4()
    evidence = evidence_for_listing(comparable("F", price=0.0, currency="USD"), candidate_id)
    evidence += evidence_for_listing(
        comparable("U", price=None, currency=None), candidate_id
    )
    features = extract_price_evidence(candidate_id, evidence, now=NOW).features

    assert features.free_listing_count == 1
    assert features.listings_with_unknown_price == 1
    assert features.listings_with_observed_price == 1


# ------------------------------------------------- asking-price semantics


def test_prices_are_labelled_asking_prices_never_transaction_prices():
    result = extract([comparable(f"L{i}", price=10.0 + i, seller_id=f"s{i}") for i in range(3)])

    assert result.basis == PriceEvidenceBasis.OBSERVED_ASKING_PRICE
    assert result.basis != PriceEvidenceBasis.TRANSACTION_PRICE
    # The three things public listing data cannot establish stay UNKNOWN.
    assert result.transaction_prices == TruthClass.UNKNOWN
    assert result.willingness_to_pay == TruthClass.UNKNOWN
    assert result.recommended_price == TruthClass.UNKNOWN

    band = band_for(result, "USD")
    # Every price field names itself an asking price.
    price_fields = [f for f in band.__slots__ if "price" in f and "count" not in f]
    for field in price_fields:
        assert "asking" in field or field == "observed_prices", field


def test_no_field_claims_transaction_revenue_or_recommendation_semantics():
    import re

    result = extract([comparable(f"L{i}", price=10.0 + i, seller_id=f"s{i}") for i in range(6)])
    for obj in (result.features, band_for(result, "USD")):
        for name in obj.__slots__:
            assert not re.search(
                r"revenue|transaction_price|willingness|optimal|recommended_price_value"
                r"|suggested|should_charge|units_sold",
                name,
            ), name


def test_limitations_state_the_asking_price_and_currency_caveats():
    text = " ".join(extract([comparable("L1", price=9.0)]).limitations).lower()
    assert "asking" in text and "not verified transaction prices" in text
    assert "never willingness to pay" in text
    assert "never an optimal or recommended price" in text
    assert "per currency" in text
    assert "cross-currency comparison was not" in text
    assert "no currency was discarded" in text
    assert "not normalized per item" in text


# --------------------------------------------------------- bundle handling


def test_bundles_are_not_silently_unit_normalized():
    """A $45 bundle and a $5 single item stay separate observed prices."""
    listings = [
        comparable("BUNDLE", price=45.0, currency="USD", seller_id="a"),
        comparable("SINGLE", price=5.0, currency="USD", seller_id="b"),
    ]
    band = band_for(extract(listings), "USD")

    assert band.observed_prices == (5.0, 45.0)
    assert band.min_paid_asking_price == 5.0
    assert band.max_paid_asking_price == 45.0
    # $45 / 50 templates = 0.9 — a per-unit figure must appear nowhere.
    numeric = [
        getattr(band, n)
        for n in band.__slots__
        if isinstance(getattr(band, n), (int, float))
    ]
    assert 0.9 not in numeric
    assert all(v != 45.0 / 50 for v in numeric)


# ------------------------------------------------------------- statistics


def test_band_statistics_are_correct_and_deterministic():
    prices = [10.0, 12.0, 14.0, 20.0, 30.0, 40.0]
    listings = [
        comparable(f"L{i}", price=p, currency="USD", seller_id=f"s{i}")
        for i, p in enumerate(prices)
    ]
    band = band_for(extract(listings), "USD")

    assert band.paid_listing_count == 6
    assert band.observed_prices == tuple(sorted(prices))
    assert band.min_paid_asking_price == 10.0
    assert band.max_paid_asking_price == 40.0
    assert band.median_asking_price == 17.0
    assert band.p25_asking_price == 12.5
    assert band.p75_asking_price == 27.5
    assert band.interquartile_range == 15.0
    assert band.coefficient_of_variation is not None
    assert band.band_version == PRICE_BAND_VERSION


def test_p90_is_withheld_below_the_minimum_sample():
    """A 90th percentile on a tiny sample is not a distribution statistic."""
    small = [
        comparable(f"L{i}", price=10.0 + i, currency="USD", seller_id=f"s{i}")
        for i in range(MIN_SAMPLE_FOR_P90 - 1)
    ]
    assert band_for(extract(small), "USD").p90_asking_price is None

    large = [
        comparable(f"L{i}", price=10.0 + i, currency="USD", seller_id=f"s{i}")
        for i in range(MIN_SAMPLE_FOR_P90)
    ]
    band = band_for(extract(large), "USD")
    assert band.p90_asking_price is not None
    assert band.median_asking_price <= band.p90_asking_price <= band.max_paid_asking_price


def test_trimmed_mean_is_withheld_below_the_minimum_sample():
    small = [
        comparable(f"L{i}", price=10.0 + i, currency="USD", seller_id=f"s{i}")
        for i in range(MIN_SAMPLE_FOR_WINSORIZED_MEAN - 1)
    ]
    assert band_for(extract(small), "USD").trimmed_mean_asking_price is None

    large = small + [comparable("X", price=99999.0, currency="USD", seller_id="premium")]
    band = band_for(extract(large), "USD")
    assert band.trimmed_mean_asking_price is not None
    # One premium bundle must not set the central figure. At this sample size
    # a plain int(n * 0.1) would trim nothing and return the arithmetic mean.
    arithmetic_mean = sum(band.observed_prices) / len(band.observed_prices)
    assert band.trimmed_mean_asking_price < arithmetic_mean
    assert band.trimmed_mean_asking_price < 100, (
        "a statistic labelled trimmed must actually be trimmed"
    )
    assert band.median_asking_price < 100
    # The raw observation survives untouched alongside it.
    assert 99999.0 in band.observed_prices


def test_coefficient_of_variation_only_when_mathematically_valid():
    one = band_for(extract([comparable("L1", price=10.0, currency="USD")]), "USD")
    assert one.coefficient_of_variation is None, "stdev is undefined for one value"

    two = band_for(
        extract(
            [
                comparable("L1", price=10.0, currency="USD", seller_id="a"),
                comparable("L2", price=20.0, currency="USD", seller_id="b"),
            ]
        ),
        "USD",
    )
    assert two.coefficient_of_variation is not None
    assert two.coefficient_of_variation > 0

    identical = band_for(
        extract(
            [
                comparable(f"L{i}", price=10.0, currency="USD", seller_id=f"s{i}")
                for i in range(4)
            ]
        ),
        "USD",
    )
    assert identical.coefficient_of_variation == 0.0, "no dispersion in a flat band"


def test_insufficient_evidence_flagged_below_the_shared_minimum():
    few = [
        comparable(f"L{i}", price=10.0, currency="USD", seller_id=f"s{i}")
        for i in range(MIN_PRICE_COMPARABLES - 1)
    ]
    assert band_for(extract(few), "USD").insufficient_evidence is True

    enough = [
        comparable(f"L{i}", price=10.0, currency="USD", seller_id=f"s{i}")
        for i in range(MIN_PRICE_COMPARABLES)
    ]
    assert band_for(extract(enough), "USD").insufficient_evidence is False


def test_sub_population_medians_only_where_evidence_supports_them():
    listings = [
        # Old and unreviewed vs. new and reviewed, so the two sub-populations
        # are distinguishable from each other and from the whole band.
        comparable("OLD", price=30.0, currency="USD", seller_id="a", age_days=400, review_count=0),
        comparable("NEW", price=10.0, currency="USD", seller_id="b", age_days=1, review_count=5),
    ]
    band = band_for(extract(listings), "USD")

    # Established: only the 400-day-old listing qualifies.
    assert band.established_listing_count == 1
    assert band.established_listing_median_asking_price == 30.0
    # Purchase proxy: only the reviewed listing qualifies.
    assert band.purchase_proxy_listing_count == 1
    assert band.purchase_proxy_median_asking_price == 10.0
    # Neither sub-population median equals the whole band's median.
    assert band.median_asking_price == 20.0

    # With no qualifying listings, the medians are withheld, not zeroed.
    none_qualify = [
        comparable("N1", price=10.0, currency="USD", seller_id="x", age_days=1, review_count=0)
    ]
    empty_band = band_for(extract(none_qualify), "USD")
    assert empty_band.established_listing_median_asking_price is None
    assert empty_band.purchase_proxy_median_asking_price is None
    assert empty_band.established_listing_count == 0
    assert empty_band.purchase_proxy_listing_count == 0


def test_raw_observations_survive_alongside_robust_statistics():
    prices = [1.0, 2.0, 3.0, 4.0, 5.0, 900.0]
    listings = [
        comparable(f"L{i}", price=p, currency="USD", seller_id=f"s{i}")
        for i, p in enumerate(prices)
    ]
    band = band_for(extract(listings), "USD")

    # Robust statistics never destroy or overwrite the source observations.
    assert band.observed_prices == tuple(sorted(prices))
    assert 900.0 in band.observed_prices
    assert band.max_paid_asking_price == 900.0
    assert band.median_asking_price == 3.5


# ---------------------------------------------------------- determinism


def test_identical_evidence_yields_identical_output():
    listings = [
        comparable(f"L{i}", price=float(i * 7 + 3), currency=["USD", "EUR"][i % 2], seller_id=f"s{i}")
        for i in range(10)
    ]
    candidate_id = uuid4()
    evidence = [
        item for listing in listings for item in evidence_for_listing(listing, candidate_id)
    ]
    first = extract_price_evidence(candidate_id, evidence, now=NOW)
    second = extract_price_evidence(candidate_id, evidence, now=NOW)

    assert first.features == second.features
    assert first.provenance == second.provenance


def test_evidence_order_does_not_change_bands():
    listings = [
        comparable(f"L{i}", price=float(i + 5), currency="USD", seller_id=f"s{i}")
        for i in range(6)
    ]
    candidate_id = uuid4()
    evidence = [
        item for listing in listings for item in evidence_for_listing(listing, candidate_id)
    ]
    forward = extract_price_evidence(candidate_id, evidence, now=NOW)
    backward = extract_price_evidence(candidate_id, list(reversed(evidence)), now=NOW)
    assert forward.features == backward.features


# --------------------------------------------------- provenance / duplicates


def test_provenance_and_truth_classes_survive_into_derived_features():
    candidate_id = uuid4()
    listing = comparable("L1", price=11.0, currency="USD")
    evidence = evidence_for_listing(listing, candidate_id)
    result = extract_price_evidence(candidate_id, evidence, now=NOW)

    provenance = result.provenance
    assert provenance.candidate_id == candidate_id
    assert provenance.listing_ids == ("L1",)
    assert provenance.providers == ("etsy",)
    assert provenance.marketplaces == ("etsy",)
    assert provenance.source_references == ("/v3/listings",)
    assert "PRICE" in provenance.source_purposes
    assert "OBSERVED" in provenance.source_truth_classes
    assert provenance.earliest_retrieved_at is not None
    price_ids = {i.id for i in evidence if i.signal_type == "marketplace_listing_price"}
    assert price_ids.issubset(set(provenance.evidence_ids))

    # OBSERVED sources, INFERRED derivation.
    assert result.evidence_truth_basis == TruthClass.OBSERVED
    assert result.features_truth_class == TruthClass.INFERRED
    assert result.value is None and result.value_truth_class is None
    assert result.version == PRICE_EVIDENCE_VERSION


def test_duplicate_listing_evidence_is_collapsed():
    candidate_id = uuid4()
    evidence = evidence_for_listing(comparable("D1", price=25.0, currency="USD"), candidate_id)
    result = extract_price_evidence(candidate_id, evidence * 4, now=NOW)

    assert result.features.paid_comparable_count == 1
    assert band_for(result, "USD").observed_prices == (25.0,)
    assert result.provenance.duplicate_evidence_suppressed > 0


def test_shared_listing_across_candidates_keeps_separate_provenance():
    listing = comparable("SHARED", price=33.0, currency="USD")
    a, b = uuid4(), uuid4()
    ra = extract_price_evidence(a, evidence_for_listing(listing, a), now=NOW)
    rb = extract_price_evidence(b, evidence_for_listing(listing, b), now=NOW)

    assert set(ra.provenance.evidence_ids).isdisjoint(rb.provenance.evidence_ids)
    assert ra.features == rb.features


# ------------------------------- consistency with Purchase Evidence (4A)


def test_price_and_purchase_evidence_agree_on_paid_comparables():
    """Both modules must derive paid comparables from one shared definition."""
    listings = [
        comparable("PAID1", price=10.0, currency="USD", seller_id="a"),
        comparable("PAID2", price=20.0, currency=None, seller_id="b"),
        comparable("FREE", price=0.0, currency="USD", seller_id="c"),
        comparable("UNKNOWN", price=None, currency=None, seller_id="d"),
        comparable("PHYSICAL", price=50.0, currency="USD", seller_id="e", is_digital=False),
    ]
    candidate_id = uuid4()
    evidence = [
        item for listing in listings for item in evidence_for_listing(listing, candidate_id)
    ]
    price = extract_price_evidence(candidate_id, evidence, now=NOW)
    purchase = extract_purchase_evidence(candidate_id, evidence, now=NOW)

    assert price.features.paid_comparable_count == purchase.features.paid_comparable_count == 2
    assert (
        price.features.total_relevant_listings
        == purchase.features.relevant_comparable_count
        == 4
    )
    assert (
        price.features.excluded_physical_listing_count
        == purchase.features.excluded_physical_listing_count
        == 1
    )


def test_free_listing_is_not_a_paid_comparable_in_either_module():
    """Regression: 4A previously counted a $0 listing as a paid comparable."""
    candidate_id = uuid4()
    evidence = evidence_for_listing(
        comparable("FREE", price=0.0, currency="USD"), candidate_id
    )
    price = extract_price_evidence(candidate_id, evidence, now=NOW)
    purchase = extract_purchase_evidence(candidate_id, evidence, now=NOW)

    assert price.features.paid_comparable_count == 0
    assert purchase.features.paid_comparable_count == 0
    assert price.features.free_listing_count == 1


def test_shared_listing_view_is_the_single_source_of_definitions():
    """Neither module may re-implement listing reconstruction."""
    import inspect

    from app.services import price_evidence as pe
    from app.services import purchase_evidence as pue

    for module in (pe, pue):
        source = inspect.getsource(module)
        assert "collect_listing_views" in source
        # No private re-implementation of the collector.
        assert "def _collect_listings" not in source

    views, _, _ = collect_listing_views([])
    assert views == []


# --------------------------------------------- orchestration integration


@pytest.mark.asyncio
async def test_price_evidence_derived_for_selected_candidates():
    candidates = [make_candidate(f"Cand {i}", marketplace_queries=[f"q{i}"]) for i in range(3)]
    marketplace = FakeMarketplaceProvider(
        {f"q{i}": [listing_for(f"L{i}", price=10.0 + i)] for i in range(3)}
    )
    result = await run_preliminary_research(
        candidates=candidates, store=ResearchStore(), marketplace_provider=marketplace
    )

    outcome = next(d for d in result.derivations if d.derivation == DERIVATION_PRICE_EVIDENCE)
    assert outcome.status == STATUS_DERIVATION_COMPLETE
    assert outcome.candidates_covered == 3
    assert set(result.price_evidence) == {c.candidate_id for c in result.ranking.selected}
    # A pure derivation: no extra provider calls beyond 3A's three queries.
    assert len(marketplace.search_calls) == 3


@pytest.mark.asyncio
async def test_price_evidence_failure_does_not_destroy_other_capabilities(monkeypatch):
    import app.services.research_orchestration as orchestration

    def boom(*args: Any, **kwargs: Any):
        raise RuntimeError("price-evidence derivation bug")

    monkeypatch.setattr(orchestration, "extract_price_evidence", boom)

    candidates = [make_candidate("Alpha", marketplace_queries=["q"])]
    marketplace = FakeMarketplaceProvider({"q": [listing_for("L1", price=12.0)]})
    result = await run_preliminary_research(
        candidates=candidates, store=ResearchStore(), marketplace_provider=marketplace
    )

    price_outcome = next(
        d for d in result.derivations if d.derivation == DERIVATION_PRICE_EVIDENCE
    )
    assert price_outcome.status == STATUS_DERIVATION_ERROR
    assert "RuntimeError" in price_outcome.failure_reason
    assert "error_id=" in price_outcome.failure_reason
    assert result.price_evidence == {}

    # Purchase Evidence, the dimensions, and the ranking all survive.
    purchase_outcome = next(
        d for d in result.derivations if d.derivation == DERIVATION_PURCHASE_EVIDENCE
    )
    assert purchase_outcome.status == STATUS_DERIVATION_COMPLETE
    assert result.purchase_evidence
    assert len(result.ranking.ranked) == 1
    assert result.capabilities


@pytest.mark.asyncio
async def test_purchase_evidence_failure_does_not_destroy_price_evidence(monkeypatch):
    """The two derivations are independent in both directions."""
    import app.services.research_orchestration as orchestration

    monkeypatch.setattr(
        orchestration,
        "extract_purchase_evidence",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("purchase bug")),
    )
    candidates = [make_candidate("Alpha", marketplace_queries=["q"])]
    marketplace = FakeMarketplaceProvider({"q": [listing_for("L1", price=12.0)]})
    result = await run_preliminary_research(
        candidates=candidates, store=ResearchStore(), marketplace_provider=marketplace
    )

    assert result.purchase_evidence == {}
    assert result.price_evidence, "price evidence must survive a purchase-evidence failure"
    assert len(result.ranking.ranked) == 1


@pytest.mark.asyncio
async def test_price_evidence_can_be_skipped():
    candidates = [make_candidate("Alpha", marketplace_queries=["q"])]
    marketplace = FakeMarketplaceProvider({"q": [listing_for("L1", price=12.0)]})
    result = await run_preliminary_research(
        candidates=candidates,
        store=ResearchStore(),
        marketplace_provider=marketplace,
        derive_price_evidence=False,
    )
    outcome = next(d for d in result.derivations if d.derivation == DERIVATION_PRICE_EVIDENCE)
    assert outcome.status == STATUS_DERIVATION_NOT_REQUESTED
    assert result.price_evidence == {}
    assert result.purchase_evidence, "the other derivation still runs"


@pytest.mark.asyncio
async def test_marketplace_failure_leaves_price_evidence_missing_not_zero():
    candidates = [make_candidate("Alpha", marketplace_queries=["q"])]
    marketplace = FakeMarketplaceProvider(
        {"q": [listing_for("L1", price=12.0)]}, fail_queries={"q"}
    )
    result = await run_preliminary_research(
        candidates=candidates, store=ResearchStore(), marketplace_provider=marketplace
    )
    evidence = result.price_evidence[candidates[0].id]
    assert evidence.state == DimensionState.MISSING
    assert evidence.value is None
    assert evidence.features is None


# ------------------------------------------------------------- API surface


def test_api_returns_price_evidence_without_scores_or_secrets(monkeypatch):
    from fastapi.testclient import TestClient

    from app.api import routes
    from app.main import app

    monkeypatch.setenv("ETSY_API_KEY", "etsy-secret-value-4b")
    candidates = [make_candidate("Alpha", marketplace_queries=["q"])]
    marketplace = FakeMarketplaceProvider(
        {
            "q": [
                listing_for("L1", price=10.0, review_count=4),
                listing_for("L2", price=30.0, review_count=2, seller_id="s2"),
            ]
        }
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

    assert len(body["price_evidence"]) == 1
    entry = body["price_evidence"][0]
    assert entry["value"] is None
    assert entry["basis"] == "OBSERVED_ASKING_PRICE"
    assert entry["transaction_prices"] == "UNKNOWN"
    assert entry["willingness_to_pay"] == "UNKNOWN"
    assert entry["recommended_price"] == "UNKNOWN"
    assert entry["features"]["cross_currency_comparison"] is False
    assert entry["features"]["bands"][0]["currency"] == "USD"
    assert entry["provenance"]["evidence_ids"]
    assert entry["limitations"]

    text = response.text
    assert "etsy-secret-value-4b" not in text
    for forbidden in (
        "opportunity_score",
        "evidence_confidence",
        "classification",
        "price_strength",
        "optimal_price",
        "recommended_price_value",
        "estimated_revenue",
    ):
        assert forbidden not in text


# ------------------------------------------------- no scoring leakage


def test_price_evidence_produces_no_numeric_score():
    for listings in (
        [comparable("L1", price=0.0)],
        [comparable(f"L{i}", price=10.0 + i, seller_id=f"s{i}") for i in range(8)],
    ):
        result = extract(listings)
        assert result.value is None
        assert result.state != DimensionState.SCORED
        assert result.value_truth_class is None


def test_price_evidence_module_cannot_reach_legacy_scoring():
    from tests.test_orchestration import LEGACY_SCORING_MODULE, _transitive_app_imports

    assert LEGACY_SCORING_MODULE not in _transitive_app_imports("app.services.price_evidence")
    assert LEGACY_SCORING_MODULE not in _transitive_app_imports(
        "app.services.marketplace_listing_view"
    )


def test_no_pos_ecs_or_classification_leaks_into_price_evidence():
    result = extract([comparable("L1", price=10.0)])
    forbidden = {
        "opportunity_score",
        "evidence_confidence",
        "classification",
        "kill_rules_triggered",
        "price_strength",
    }
    assert forbidden.isdisjoint(result.__slots__)
    assert forbidden.isdisjoint(result.features.__slots__)


def test_only_one_new_threshold_is_introduced_by_4b():
    """4B reuses existing repository constants wherever one already exists."""
    from app.services import price_evidence as pe

    assert pe.MIN_PRICE_COMPARABLES == MIN_PRICE_COMPARABLES  # reused from 3A
    assert pe.MIN_SAMPLE_FOR_WINSORIZED_MEAN == MIN_SAMPLE_FOR_WINSORIZED_MEAN  # 4A
    assert pe.ESTABLISHED_LISTING_MIN_AGE_DAYS == 180  # 4A
    assert pe.MIN_REVIEWS_FOR_PROXY == 1  # 4A
    # The only genuinely new sample minimum.
    assert pe.MIN_SAMPLE_FOR_P90 == 10


def test_no_fx_conversion_exists_anywhere_in_the_module():
    import pathlib
    import re

    source = pathlib.Path("app/services/price_evidence.py").read_text()
    assert not re.search(r"exchange_rate|fx_rate|convert_currency|to_usd|usd_equivalent", source)
    # No arithmetic that would combine two currencies' prices.
    assert "cross_currency_comparison: bool = False" in source


def test_observed_price_categories_always_reconcile():
    """paid + free + invalid must equal the observed-price count.

    A negative price is invalid provider data. Without an explicit category
    it would be counted as observed while belonging to neither paid nor
    free, leaving a silent gap a reader could not explain.
    """
    listings = [
        comparable("PAID", price=10.0, currency="USD", seller_id="a"),
        comparable("FREE", price=0.0, currency="USD", seller_id="b"),
        comparable("NEG", price=-5.0, currency="USD", seller_id="c"),
        comparable("UNKNOWN", price=None, currency=None, seller_id="d"),
    ]
    features = extract(listings).features

    assert features.paid_comparable_count == 1
    assert features.free_listing_count == 1
    assert features.invalid_price_listing_count == 1
    assert features.listings_with_unknown_price == 1
    assert (
        features.paid_comparable_count
        + features.free_listing_count
        + features.invalid_price_listing_count
        == features.listings_with_observed_price
    )
    # The invalid price never reaches a band.
    assert band_for(extract(listings), "USD").observed_prices == (10.0,)


def test_categories_reconcile_when_no_invalid_prices_exist():
    listings = [
        comparable("PAID", price=10.0, currency="USD", seller_id="a"),
        comparable("FREE", price=0.0, currency="USD", seller_id="b"),
    ]
    features = extract(listings).features
    assert features.invalid_price_listing_count == 0
    assert (
        features.paid_comparable_count
        + features.free_listing_count
        + features.invalid_price_listing_count
        == features.listings_with_observed_price
        == 2
    )
