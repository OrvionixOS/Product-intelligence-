"""Milestone 4E tests: Competition Opportunity.

No network, no live API calls, no new provider.

Three rules carry the milestone, and most of this file exists to keep them
enforced rather than merely documented.

1. Competition is NOT monotonic. Neither "less competition is better" nor
   "more competition is better" may be expressible. Every pattern carries two
   opposed readings, the patterns are unordered, and an AST guard proves no
   comparison, sort, or ranking expression consumes one.

2. Observable competitive evidence is not a conclusion. Saturation, entry
   difficulty, win probability, differentiation room, competitor strength and
   available market share are permanent UNKNOWN markers.

3. Missing evidence never becomes an empty field. "We did not look", "we
   looked and it failed", and "we looked and found nothing" are three facts
   and stay three states.

A fourth guard is anti-duplication: 4E must not restate 4A, 4B or 4D. An AST
guard proves the module never reads a price, review count, or rating.
"""

import ast
import random
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.domain.enums import EvidencePurpose, TruthClass
from app.domain.models import EvidenceItem
from app.services.competition_opportunity import (
    COMPETITION_FIELD_PATTERN_VERSION,
    COMPETITION_OPPORTUNITY_VERSION,
    CROWDED_MIN_LISTINGS,
    CROWDED_MIN_SELLERS,
    DIM_COMPETITION_OPPORTUNITY,
    DOMINANT_SELLER_LISTING_SHARE,
    MIN_ATTRIBUTED_SHARE_FOR_STRUCTURE,
    PATTERN_READINGS,
    SPARSE_MAX_LISTINGS,
    CompetitionFieldPattern,
    _sellers_covering_half,
    extract_competition_opportunity,
)
from app.services.preliminary_dimensions import DimensionState
from app.services.purchase_evidence import extract_purchase_evidence
from app.services.research_orchestration import (
    DERIVATION_COMPETITION_OPPORTUNITY,
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

from tests.route_surface import assert_openapi_surface_unchanged, assert_route_surface_unchanged

NOW = datetime(2026, 1, 1, tzinfo=UTC)
MODULE_PATH = Path("app/services/competition_opportunity.py")

NOT_REQUESTED = "capability_not_requested"
FAILED = "capability_provider_failed"
UNEXPECTED = "capability_unexpected_error"
MARKETPLACE = "marketplace"


# ----------------------------------------------------------------- fixtures


def listing_evidence(
    listing_id: str,
    *,
    seller_id: str | None = "shopA",
    candidate_id: UUID | None = None,
    is_digital: bool | None = True,
    price: float | None = 12.0,
    review_count: int | None = 4,
    created_days_ago: int | None = 400,
    provider: str = "etsy",
    payload_hash: str | None = None,
) -> list[EvidenceItem]:
    """The evidence shape 3A emits for one (candidate, listing) pair.

    Two records per listing, exactly as build_listing_evidence produces: a
    PRICE record and a PURCHASE-proxy record. Those are the two signals
    collect_listing_views reads, so this is what 4E actually sees.
    """
    created = (
        (NOW - timedelta(days=created_days_ago)).isoformat()
        if created_days_ago is not None
        else None
    )
    payload: dict[str, Any] = {
        "listing_id": listing_id,
        "seller_id": seller_id,
        "price": price,
        "currency": "USD" if price is not None else None,
        "review_count": review_count,
        "rating": 4.5,
        "created_at": created,
        "is_digital": is_digital,
        "title": f"Listing {listing_id}",
    }
    common = dict(
        candidate_id=candidate_id,
        provider=provider,
        collection_method="official_api",
        source_reference="/v3/listings",
        marketplace=provider,
        raw_payload=payload,
        raw_payload_hash=payload_hash or f"hash-{listing_id}",
    )
    return [
        EvidenceItem(
            signal_type="marketplace_listing_price",
            purpose=EvidencePurpose.PRICE,
            truth_class=(
                TruthClass.OBSERVED if price is not None else TruthClass.UNKNOWN
            ),
            raw_value=price,
            **common,
        ),
        EvidenceItem(
            signal_type="marketplace_review_count_purchase_proxy",
            purpose=EvidencePurpose.PURCHASE,
            truth_class=(
                TruthClass.OBSERVED if review_count is not None else TruthClass.UNKNOWN
            ),
            raw_value=review_count,
            **common,
        ),
    ]


def field_of(*sellers: str, candidate_id: UUID | None = None) -> list[EvidenceItem]:
    """One listing per positional seller, in the order given."""
    evidence: list[EvidenceItem] = []
    for index, seller in enumerate(sellers):
        evidence.extend(
            listing_evidence(
                f"L{index}", seller_id=seller, candidate_id=candidate_id
            )
        )
    return evidence


def run(evidence, **kwargs):
    return extract_competition_opportunity(
        candidate_id=kwargs.pop("candidate_id", uuid4()),
        evidence=evidence,
        **kwargs,
    )


# ------------------------------------------------- non-monotonicity, enforced


def test_every_pattern_has_both_readings():
    """The mapping is total and no reading may be empty.

    A future pattern cannot be added without stating what the same evidence
    argues in BOTH directions.
    """
    for pattern in CompetitionFieldPattern:
        assert pattern in PATTERN_READINGS, pattern
        readings = PATTERN_READINGS[pattern]
        assert readings.market_exists_reading.strip()
        assert readings.entry_difficulty_reading.strip()
        assert readings.unresolvable_because.strip()


def test_the_two_readings_are_never_the_same_text():
    """Opposed readings, not one statement duplicated into two fields."""
    for pattern, readings in PATTERN_READINGS.items():
        assert (
            readings.market_exists_reading != readings.entry_difficulty_reading
        ), pattern


def test_every_result_carries_both_readings():
    """No code path may emit one reading without the other."""
    cases = [
        ([], {}),
        ([], {MARKETPLACE: FAILED}),
        (field_of("a"), {}),
        (field_of(*[f"s{i}" for i in range(9)]), {}),
        (field_of(*(["solo"] * 9)), {}),
    ]
    for evidence, reasons in cases:
        result = run(evidence, missing_reasons=reasons)
        assert result.readings is PATTERN_READINGS[result.pattern]
        assert result.readings.market_exists_reading.strip()
        assert result.readings.entry_difficulty_reading.strip()


def test_pattern_is_never_compared_sorted_or_ranked():
    """AST guard: no ordering over CompetitionFieldPattern may exist.

    If the code cannot compare a pattern, it cannot express "less competition
    is better" however the prose later drifts.
    """
    tree = ast.parse(MODULE_PATH.read_text())
    order_ops = (ast.Lt, ast.LtE, ast.Gt, ast.GtE)

    def mentions_pattern(node: ast.AST) -> bool:
        for child in ast.walk(node):
            if isinstance(child, ast.Name) and "Pattern" in child.id:
                return True
            if isinstance(child, ast.Attribute) and child.attr in {
                p.name for p in CompetitionFieldPattern
            }:
                return True
        return False

    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            if any(isinstance(op, order_ops) for op in node.ops):
                assert not mentions_pattern(node), ast.dump(node)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in {"sorted", "min", "max"}:
                assert not mentions_pattern(node), ast.dump(node)


def test_no_pattern_maps_to_a_number():
    """AST guard: no dict literal in the module maps a pattern to a number.

    A pattern -> score table is exactly how a non-monotonic classification
    gets quietly linearized.
    """
    tree = ast.parse(MODULE_PATH.read_text())
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values):
            key_is_pattern = (
                isinstance(key, ast.Attribute)
                and isinstance(key.value, ast.Name)
                and key.value.id == "CompetitionFieldPattern"
            )
            if key_is_pattern:
                assert not (
                    isinstance(value, ast.Constant)
                    and isinstance(value.value, (int, float))
                ), ast.dump(node)


def test_more_competition_is_not_reported_as_worse():
    """A crowded field and a sparse field are different, never ranked.

    Both are EVIDENCE_PRESENT_UNSCORED with a null value: nothing in the
    result lets a consumer order them.
    """
    sparse = run(field_of("a", "b"))
    crowded = run(field_of(*[f"s{i}" for i in range(14)]))
    assert sparse.pattern is CompetitionFieldPattern.SPARSE_FIELD
    assert crowded.pattern is CompetitionFieldPattern.CROWDED_FIELD
    assert sparse.value is None and crowded.value is None
    assert sparse.state is DimensionState.EVIDENCE_PRESENT_UNSCORED
    assert crowded.state is DimensionState.EVIDENCE_PRESENT_UNSCORED


# ------------------------------------------------- conclusions stay UNKNOWN


@pytest.mark.parametrize(
    "marker",
    [
        "saturation",
        "entry_difficulty",
        "win_probability",
        "differentiation_opportunity",
        "competitor_strength",
        "competitor_revenue",
        "market_share_available",
    ],
)
def test_conclusion_markers_are_permanently_unknown(marker):
    for evidence, reasons in [
        ([], {}),
        ([], {MARKETPLACE: NOT_REQUESTED}),
        (field_of("a", "b", "c", "d", "e"), {}),
        (field_of(*(["one"] * 20)), {}),
    ]:
        result = run(evidence, missing_reasons=reasons)
        assert getattr(result, marker) is TruthClass.UNKNOWN


def test_value_is_permanently_none_and_dimension_cannot_reach_legacy_weights():
    result = run(field_of("a", "b", "c", "d", "e"))
    assert result.value is None
    assert result.value_truth_class is None
    assert result.dimension_name == DIM_COMPETITION_OPPORTUNITY
    assert result.dimension_name == "preliminary_competition_opportunity"
    # Never the ScoreDimensions field name feeding the unapproved weights.
    assert result.dimension_name != "competition_opportunity"


def test_derived_features_are_inferred_even_from_observed_inputs():
    result = run(field_of("a", "b", "c", "d", "e"))
    assert result.evidence_truth_basis is TruthClass.OBSERVED
    assert result.features_truth_class is TruthClass.INFERRED


def test_versions_are_reported():
    result = run(field_of("a", "b", "c", "d"))
    assert result.version == COMPETITION_OPPORTUNITY_VERSION
    assert result.pattern_version == COMPETITION_FIELD_PATTERN_VERSION
    assert result.features.features_version == "competition_field_features_v1"


# --------------------------------------------- missing never becomes empty


@pytest.mark.parametrize("reason", [NOT_REQUESTED, FAILED, UNEXPECTED])
def test_capability_failure_is_unknown_field_not_an_empty_one(reason):
    result = run([], missing_reasons={MARKETPLACE: reason})
    assert result.pattern is CompetitionFieldPattern.UNKNOWN_FIELD
    assert result.state is DimensionState.MISSING
    assert result.missing_reason == reason
    assert result.features is None


def test_ran_and_found_nothing_is_an_observed_absence():
    result = run([], missing_reasons={})
    assert result.pattern is CompetitionFieldPattern.NO_LISTINGS_OBSERVED
    assert result.state is DimensionState.UNKNOWN
    assert result.missing_reason == "no_marketplace_listings_returned_for_candidate"


def test_failed_capability_and_empty_result_are_different_states():
    failed = run([], missing_reasons={MARKETPLACE: FAILED})
    empty = run([], missing_reasons={})
    assert failed.state is not empty.state
    assert failed.pattern is not empty.pattern
    assert failed.missing_reason != empty.missing_reason


def test_another_capabilitys_failure_does_not_make_the_field_unknown():
    """4E reads the marketplace capability only."""
    result = run([], missing_reasons={"public_content": FAILED, "search_demand": FAILED})
    assert result.pattern is CompetitionFieldPattern.NO_LISTINGS_OBSERVED


def test_all_physical_listings_is_missing_not_an_empty_field():
    evidence = []
    for index in range(4):
        evidence.extend(
            listing_evidence(f"P{index}", seller_id=f"s{index}", is_digital=False)
        )
    result = run(evidence)
    assert result.state is DimensionState.MISSING
    assert result.missing_reason == "all_listings_excluded_as_unrelated_format"
    assert result.features.competing_listing_count == 0
    assert result.features.excluded_physical_listing_count == 4


def test_unknown_format_is_kept_not_dropped():
    """is_digital None means the marketplace did not say, never 'physical'."""
    evidence = []
    for index in range(5):
        evidence.extend(
            listing_evidence(f"U{index}", seller_id=f"s{index}", is_digital=None)
        )
    result = run(evidence)
    assert result.features.competing_listing_count == 5
    assert result.features.unknown_format_listing_count == 5
    assert result.features.excluded_physical_listing_count == 0


def test_listings_without_a_seller_are_not_merged_into_one_fictional_seller():
    evidence = []
    for index in range(6):
        evidence.extend(listing_evidence(f"N{index}", seller_id=None))
    result = run(evidence)
    assert result.pattern is CompetitionFieldPattern.FIELD_STRUCTURE_UNKNOWN
    assert result.features.competing_listing_count == 6
    assert result.features.listings_without_seller_attribution == 6
    assert result.features.seller_count_in_field == 0
    # Unknown, never zero and never 1.0.
    assert result.features.top_seller_listing_share is None
    assert result.features.max_listings_per_seller is None
    assert result.features.sellers_covering_half_of_listings is None


def test_partial_seller_attribution_excludes_only_the_unattributed():
    evidence = field_of("a", "a", "b")
    evidence.extend(listing_evidence("Lx", seller_id=None))
    evidence.extend(listing_evidence("Ly", seller_id=None))
    result = run(evidence)
    assert result.features.competing_listing_count == 5
    assert result.features.listings_with_seller_attribution == 3
    assert result.features.listings_without_seller_attribution == 2
    # Share is over ATTRIBUTED listings: 2 of 3, never 2 of 5.
    assert result.features.top_seller_listing_share == 0.6667


# ------------------------------------------------------------- classification


def test_sparse_field_at_the_threshold():
    assert (
        run(field_of(*[f"s{i}" for i in range(SPARSE_MAX_LISTINGS)])).pattern
        is CompetitionFieldPattern.SPARSE_FIELD
    )


def test_just_above_sparse_is_no_longer_sparse():
    result = run(field_of(*[f"s{i}" for i in range(SPARSE_MAX_LISTINGS + 1)]))
    assert result.pattern is not CompetitionFieldPattern.SPARSE_FIELD


def test_sparse_is_decided_without_seller_attribution():
    """Two listings are sparse whoever owns them."""
    evidence = []
    for index in range(2):
        evidence.extend(listing_evidence(f"S{index}", seller_id=None))
    assert run(evidence).pattern is CompetitionFieldPattern.SPARSE_FIELD


def test_concentrated_field():
    # 8 listings, 7 held by one seller: share 0.875.
    evidence = field_of(*(["big"] * 7 + ["small"]))
    result = run(evidence)
    assert result.pattern is CompetitionFieldPattern.CONCENTRATED_FIELD
    assert result.features.top_seller_listing_share >= DOMINANT_SELLER_LISTING_SHARE
    assert result.features.max_listings_per_seller == 7
    assert result.features.sellers_covering_half_of_listings == 1


def test_crowded_field_requires_both_axes():
    many_listings_one_seller = run(field_of(*(["solo"] * (CROWDED_MIN_LISTINGS + 4))))
    assert many_listings_one_seller.pattern is not CompetitionFieldPattern.CROWDED_FIELD

    both = run(
        field_of(*[f"s{i}" for i in range(max(CROWDED_MIN_LISTINGS, CROWDED_MIN_SELLERS))])
    )
    assert both.pattern is CompetitionFieldPattern.CROWDED_FIELD


def test_fragmented_field():
    """Several sellers, none dominant, without crowded scale."""
    result = run(field_of("a", "b", "c", "d", "e"))
    assert result.pattern is CompetitionFieldPattern.FRAGMENTED_FIELD
    assert result.features.single_listing_seller_count == 5
    assert result.features.top_seller_listing_share == 0.2


def test_sellers_covering_half_is_computed_over_attributed_listings():
    # 4 listings for "a", 1 each for b, c, d, e -> 4 of 8 is already half.
    evidence = field_of(*(["a"] * 4 + ["b", "c", "d", "e"]))
    result = run(evidence)
    assert result.features.sellers_covering_half_of_listings == 1
    # Ten equal sellers need five to reach half.
    even = run(field_of(*[f"s{i}" for i in range(10)]))
    assert even.features.sellers_covering_half_of_listings == 5


# ------------------------------------------------------------ anti-duplication


def test_module_never_reads_a_price_review_count_or_rating():
    """AST guard: 4E cannot restate a 4A, 4B or 4D finding.

    collect_listing_views hands 4E a ListingView carrying prices and review
    counts. This asserts the module never touches them, so a competition
    finding can never be a purchase or price finding wearing a new name.
    """
    forbidden = {
        "price",
        "price_observed",
        "is_paid_comparable",
        "is_free_listing",
        "has_unknown_price",
        "currency",
        "review_count",
        "review_observed",
        "rating",
        "search_volume",
        "view_count",
        "subscriber_count",
        "cpc",
    }
    tree = ast.parse(MODULE_PATH.read_text())
    read = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            read.add(node.attr)
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and node.args
            and isinstance(node.args[0], ast.Constant)
        ):
            read.add(node.args[0].value)
    assert not (read & forbidden), sorted(read & forbidden)


def test_field_size_is_pinned_to_4a_rather_than_independently_recomputed():
    """Same population, by construction. The test pins it so it stays so.

    competing_listing_count and seller_count_in_field are restated from the
    shared listing view, not corroboration of 4A. If either drifts from 4A,
    two milestones would disagree about what the marketplace showed.
    """
    candidate_id = uuid4()
    evidence = field_of(
        *(["a", "a", "b", "c", "d"]), candidate_id=candidate_id
    )
    evidence.extend(
        listing_evidence("Px", seller_id="e", is_digital=False, candidate_id=candidate_id)
    )
    competition = extract_competition_opportunity(
        candidate_id=candidate_id, evidence=evidence
    )
    purchase = extract_purchase_evidence(
        candidate_id=candidate_id, evidence=evidence, now=NOW
    )
    assert (
        competition.features.competing_listing_count
        == purchase.features.relevant_comparable_count
    )
    assert (
        competition.features.seller_count_in_field
        == purchase.features.distinct_seller_count
    )
    assert (
        competition.features.excluded_physical_listing_count
        == purchase.features.excluded_physical_listing_count
    )


def test_listing_share_is_not_4as_proxy_share():
    """Different quantity over a different population, and it must show.

    One seller holds most LISTINGS while another holds most review VOLUME.
    4E's concentration must follow listings; 4A's must follow reviews.
    """
    candidate_id = uuid4()
    evidence: list[EvidenceItem] = []
    for index in range(4):
        evidence.extend(
            listing_evidence(
                f"Q{index}",
                seller_id="catalog",
                review_count=1,
                candidate_id=candidate_id,
            )
        )
    evidence.extend(
        listing_evidence(
            "Q9", seller_id="bestseller", review_count=500, candidate_id=candidate_id
        )
    )
    competition = extract_competition_opportunity(
        candidate_id=candidate_id, evidence=evidence
    )
    purchase = extract_purchase_evidence(
        candidate_id=candidate_id, evidence=evidence, now=NOW
    )
    # 4 of 5 listings belong to "catalog".
    assert competition.features.top_seller_listing_share == 0.8
    # 500 of 504 review proxies belong to "bestseller".
    assert purchase.features.top_seller_proxy_share > 0.9
    assert (
        competition.features.top_seller_listing_share
        != purchase.features.top_seller_proxy_share
    )


def test_module_does_not_import_scoring_or_other_derivations():
    tree = ast.parse(MODULE_PATH.read_text())
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
    assert not any("scoring" in module for module in modules)
    assert not any("price_evidence" in module for module in modules)
    assert not any("buyer_reach" in module for module in modules)
    assert not any("product_specification" in module for module in modules)


# ----------------------------------------------------------------- determinism


def _identity(result) -> tuple:
    features = result.features
    return (
        result.state,
        result.pattern,
        result.missing_reason,
        result.readings,
        None
        if features is None
        else (
            features.competing_listing_count,
            features.seller_count_in_field,
            features.listings_with_seller_attribution,
            features.listings_without_seller_attribution,
            features.top_seller_listing_share,
            features.max_listings_per_seller,
            features.single_listing_seller_count,
            features.sellers_covering_half_of_listings,
        ),
        result.provenance.evidence_ids,
        result.provenance.listing_ids,
        result.provenance.seller_ids,
        result.provenance.providers,
        result.provenance.source_truth_classes,
        result.provenance.duplicate_evidence_suppressed,
    )


def _mixed_field(candidate_id: UUID) -> list[EvidenceItem]:
    evidence: list[EvidenceItem] = []
    layout = ["a", "a", "a", "b", "b", "c", "d", "e", "f", "g"]
    for index, seller in enumerate(layout):
        evidence.extend(
            listing_evidence(
                f"M{index}", seller_id=seller, candidate_id=candidate_id
            )
        )
    for index in range(3):
        evidence.extend(
            listing_evidence(f"X{index}", seller_id=None, candidate_id=candidate_id)
        )
    return evidence


def test_output_is_identical_under_1000_shuffles():
    candidate_id = uuid4()
    evidence = _mixed_field(candidate_id)
    baseline = _identity(
        extract_competition_opportunity(candidate_id=candidate_id, evidence=evidence)
    )
    rng = random.Random(20260911)
    mismatches = 0
    for _ in range(1000):
        shuffled = list(evidence)
        rng.shuffle(shuffled)
        if (
            _identity(
                extract_competition_opportunity(
                    candidate_id=candidate_id, evidence=shuffled
                )
            )
            != baseline
        ):
            mismatches += 1
    assert mismatches == 0


def test_reversed_and_repeated_input_are_identical():
    candidate_id = uuid4()
    evidence = _mixed_field(candidate_id)
    baseline = _identity(
        extract_competition_opportunity(candidate_id=candidate_id, evidence=evidence)
    )
    assert (
        _identity(
            extract_competition_opportunity(
                candidate_id=candidate_id, evidence=list(reversed(evidence))
            )
        )
        == baseline
    )
    for _ in range(5):
        assert (
            _identity(
                extract_competition_opportunity(
                    candidate_id=candidate_id, evidence=evidence
                )
            )
            == baseline
        )


def test_half_coverage_depends_only_on_the_multiset_of_seller_counts():
    """The real invariant, pinned directly.

    An input-shuffling test would pass here even with the tie-break removed,
    because permuting equal-count sellers cannot change a running sum. This
    asserts the property that actually holds: relabelling sellers and
    reordering the mapping never changes the answer.
    """
    rng = random.Random(4242)
    for _ in range(300):
        counts = [rng.randint(1, 9) for _ in range(rng.randint(1, 8))]
        baseline = _sellers_covering_half(
            {f"s{index}": count for index, count in enumerate(counts)}
        )
        for _ in range(5):
            shuffled = list(counts)
            rng.shuffle(shuffled)
            relabelled = {
                f"z{index}": count for index, count in enumerate(shuffled)
            }
            assert _sellers_covering_half(relabelled) == baseline


def test_equal_sellers_leave_coverage_independent_of_arrival_order():
    candidate_id = uuid4()
    evidence = field_of(*(["a", "a", "b", "b", "c", "c"]), candidate_id=candidate_id)
    baseline = _identity(
        extract_competition_opportunity(candidate_id=candidate_id, evidence=evidence)
    )
    rng = random.Random(7)
    for _ in range(200):
        shuffled = list(evidence)
        rng.shuffle(shuffled)
        assert (
            _identity(
                extract_competition_opportunity(
                    candidate_id=candidate_id, evidence=shuffled
                )
            )
            == baseline
        )


def test_mixed_identifier_types_order_without_coercion():
    """A mixed int/str seller id must not raise and must order stably."""
    candidate_id = uuid4()
    evidence: list[EvidenceItem] = []
    for index, seller in enumerate([1, "1", "shop", 2]):
        evidence.extend(
            listing_evidence(
                f"T{index}", seller_id=seller, candidate_id=candidate_id
            )
        )
    result = extract_competition_opportunity(
        candidate_id=candidate_id, evidence=evidence
    )
    assert result.features.seller_count_in_field == 4
    baseline = result.provenance.seller_ids
    rng = random.Random(11)
    for _ in range(100):
        shuffled = list(evidence)
        rng.shuffle(shuffled)
        assert (
            extract_competition_opportunity(
                candidate_id=candidate_id, evidence=shuffled
            ).provenance.seller_ids
            == baseline
        )
    # Ordering is canonical, never coercion: both types survive.
    assert {type(value) for value in baseline} == {int, str}


# ------------------------------------------------- duplicates cannot inflate


def test_re_delivered_evidence_does_not_inflate_the_field():
    candidate_id = uuid4()
    evidence = field_of("a", "b", "c", "d", "e", candidate_id=candidate_id)
    once = extract_competition_opportunity(
        candidate_id=candidate_id, evidence=evidence
    )
    twice = extract_competition_opportunity(
        candidate_id=candidate_id, evidence=evidence + evidence
    )
    assert (
        once.features.competing_listing_count
        == twice.features.competing_listing_count
        == 5
    )
    assert once.features.seller_count_in_field == twice.features.seller_count_in_field
    assert once.pattern is twice.pattern
    assert twice.provenance.duplicate_evidence_suppressed > 0


def test_one_seller_with_many_listings_is_one_seller():
    result = run(field_of(*(["solo"] * 20)))
    assert result.features.competing_listing_count == 20
    assert result.features.seller_count_in_field == 1
    assert result.features.top_seller_listing_share == 1.0
    assert result.pattern is CompetitionFieldPattern.CONCENTRATED_FIELD


# -------------------------------------------------------------- provenance


def test_provenance_is_canonically_ordered_and_preserves_multiplicity():
    candidate_id = uuid4()
    evidence = _mixed_field(candidate_id)
    result = extract_competition_opportunity(
        candidate_id=candidate_id, evidence=evidence
    )
    provenance = result.provenance
    assert list(provenance.evidence_ids) == sorted(provenance.evidence_ids)
    assert list(provenance.listing_ids) == sorted(provenance.listing_ids)
    assert provenance.candidate_id == candidate_id
    assert provenance.providers == ("etsy",)
    # Every contributing record is cited, none deduplicated away.
    assert len(provenance.evidence_ids) == len(evidence)


def test_provenance_survives_an_empty_field():
    result = run([], missing_reasons={MARKETPLACE: FAILED})
    assert result.provenance.evidence_ids == ()
    assert result.provenance.listing_ids == ()
    assert result.provenance.seller_ids == ()


# ----------------------------------------------------------- truth-class floor


def test_unknown_sources_do_not_produce_an_observed_basis():
    candidate_id = uuid4()
    evidence = listing_evidence(
        "L0", seller_id="a", price=None, review_count=None, candidate_id=candidate_id
    )
    evidence.extend(
        listing_evidence("L1", seller_id="b", candidate_id=candidate_id)
    )
    result = extract_competition_opportunity(
        candidate_id=candidate_id, evidence=evidence
    )
    # The weakest contributing class wins, never the strongest.
    assert result.evidence_truth_basis is TruthClass.UNKNOWN


def test_a_listing_with_no_observed_price_or_review_still_counts_as_supply():
    """Supply is presence. It does not require a price or a review count."""
    evidence = []
    for index in range(5):
        evidence.extend(
            listing_evidence(
                f"Z{index}", seller_id=f"s{index}", price=None, review_count=None
            )
        )
    result = run(evidence)
    assert result.features.competing_listing_count == 5
    assert result.features.seller_count_in_field == 5


# ------------------------------------------ structure needs enough attribution


def test_thin_attribution_cannot_fabricate_a_concentrated_field():
    """Regression: found by adversarial probing of this milestone's own code.

    A field of 20 listings where the marketplace attributed only 2 — both to
    one seller — previously classified as CONCENTRATED_FIELD. That is a
    structural finding built on 10% of the evidence, with the other 90%
    treated as if it did not exist. Missing attribution is missing.
    """
    candidate_id = uuid4()
    evidence: list[EvidenceItem] = []
    for index in range(2):
        evidence.extend(
            listing_evidence(f"A{index}", seller_id="solo", candidate_id=candidate_id)
        )
    for index in range(18):
        evidence.extend(
            listing_evidence(f"B{index}", seller_id=None, candidate_id=candidate_id)
        )
    result = extract_competition_opportunity(
        candidate_id=candidate_id, evidence=evidence
    )
    assert result.pattern is CompetitionFieldPattern.FIELD_STRUCTURE_UNKNOWN
    # The statistics are still reported, scoped by the share they describe.
    assert result.features.top_seller_listing_share == 1.0
    assert result.features.seller_attribution_share == 0.1


def test_attribution_floor_is_honoured_at_the_boundary():
    """At the floor a shape is reported; just below it, structure is unknown."""
    # 4 listings, 2 attributed -> exactly the floor.
    evidence: list[EvidenceItem] = []
    evidence.extend(listing_evidence("A0", seller_id="a"))
    evidence.extend(listing_evidence("A1", seller_id="b"))
    evidence.extend(listing_evidence("B0", seller_id=None))
    evidence.extend(listing_evidence("B1", seller_id=None))
    at_floor = run(evidence)
    assert at_floor.features.seller_attribution_share == MIN_ATTRIBUTED_SHARE_FOR_STRUCTURE
    assert at_floor.pattern is not CompetitionFieldPattern.FIELD_STRUCTURE_UNKNOWN

    evidence.extend(listing_evidence("B2", seller_id=None))
    below = run(evidence)
    assert below.features.seller_attribution_share < MIN_ATTRIBUTED_SHARE_FOR_STRUCTURE
    assert below.pattern is CompetitionFieldPattern.FIELD_STRUCTURE_UNKNOWN


def test_thin_attribution_does_not_read_as_uncontested_either():
    """The floor must not fail open in the other direction.

    Computing the share over ALL listings would report an unattributed field
    as not concentrated, which fabricates a shape just as surely.
    """
    evidence: list[EvidenceItem] = []
    for index in range(3):
        evidence.extend(listing_evidence(f"A{index}", seller_id="solo"))
    for index in range(15):
        evidence.extend(listing_evidence(f"B{index}", seller_id=None))
    result = run(evidence)
    assert result.pattern is CompetitionFieldPattern.FIELD_STRUCTURE_UNKNOWN
    assert result.pattern is not CompetitionFieldPattern.FRAGMENTED_FIELD
    assert result.pattern is not CompetitionFieldPattern.CROWDED_FIELD


def test_attribution_share_is_none_only_when_there_is_no_field():
    assert run(field_of("a", "b", "c", "d")).features.seller_attribution_share == 1.0
    physical = []
    for index in range(3):
        physical.extend(
            listing_evidence(f"P{index}", seller_id=f"s{index}", is_digital=False)
        )
    assert run(physical).features.seller_attribution_share is None


# ------------------------------------------------------------- orchestration


@pytest.mark.asyncio
async def test_competition_opportunity_runs_from_stored_evidence_without_new_calls():
    candidates = [
        make_candidate(f"Cand {index}", marketplace_queries=[f"q{index}"])
        for index in range(3)
    ]
    marketplace = FakeMarketplaceProvider(
        {f"q{index}": [listing_for(f"L{index}", review_count=5)] for index in range(3)}
    )
    result = await run_preliminary_research(
        candidates=candidates, store=ResearchStore(), marketplace_provider=marketplace
    )
    outcome = next(
        d
        for d in result.derivations
        if d.derivation == DERIVATION_COMPETITION_OPPORTUNITY
    )
    assert outcome.status == STATUS_DERIVATION_COMPLETE
    assert outcome.candidates_covered == 3
    assert set(result.competition_opportunity) == {
        c.candidate_id for c in result.ranking.selected
    }
    # The derivation costs nothing: it reuses evidence 3A already collected.
    assert len(marketplace.search_calls) == 3


@pytest.mark.asyncio
async def test_competition_failure_degrades_only_itself(monkeypatch):
    import app.services.research_orchestration as orchestration

    def boom(*args: Any, **kwargs: Any):
        raise RuntimeError("competition derivation bug")

    monkeypatch.setattr(orchestration, "extract_competition_opportunity", boom)

    candidates = [make_candidate("Alpha", marketplace_queries=["q"])]
    marketplace = FakeMarketplaceProvider({"q": [listing_for("L1")]})
    result = await run_preliminary_research(
        candidates=candidates, store=ResearchStore(), marketplace_provider=marketplace
    )

    outcome = next(
        d
        for d in result.derivations
        if d.derivation == DERIVATION_COMPETITION_OPPORTUNITY
    )
    assert outcome.status == STATUS_DERIVATION_ERROR
    assert "RuntimeError" in outcome.failure_reason
    assert "error_id=" in outcome.failure_reason
    # No partial or fabricated competition result survives the failure.
    assert result.competition_opportunity == {}
    # Every other derivation and the ranking are intact.
    assert result.purchase_evidence
    assert result.price_evidence
    assert result.buyer_reach
    assert len(result.ranking.ranked) == 1


@pytest.mark.asyncio
async def test_competition_opportunity_can_be_skipped():
    candidates = [make_candidate("Alpha", marketplace_queries=["q"])]
    marketplace = FakeMarketplaceProvider({"q": [listing_for("L1")]})
    result = await run_preliminary_research(
        candidates=candidates,
        store=ResearchStore(),
        marketplace_provider=marketplace,
        derive_competition_opportunity=False,
    )
    outcome = next(
        d
        for d in result.derivations
        if d.derivation == DERIVATION_COMPETITION_OPPORTUNITY
    )
    assert outcome.status == STATUS_DERIVATION_NOT_REQUESTED
    assert result.competition_opportunity == {}
    assert len(result.ranking.ranked) == 1


@pytest.mark.asyncio
async def test_marketplace_failure_leaves_the_field_unknown_not_empty():
    """End to end: a provider failure must not read as 'no competitors'."""
    candidates = [make_candidate("Alpha", marketplace_queries=["q"])]
    marketplace = FakeMarketplaceProvider({}, fail_queries={"q"})
    result = await run_preliminary_research(
        candidates=candidates, store=ResearchStore(), marketplace_provider=marketplace
    )
    competition = result.competition_opportunity[
        result.ranking.selected[0].candidate_id
    ]
    assert competition.pattern is CompetitionFieldPattern.UNKNOWN_FIELD
    assert competition.state is DimensionState.MISSING
    assert competition.missing_reason == FAILED
    assert competition.features is None


@pytest.mark.asyncio
async def test_marketplace_not_requested_is_distinct_from_a_failure():
    candidates = [make_candidate("Alpha", marketplace_queries=["q"])]
    result = await run_preliminary_research(
        candidates=candidates, store=ResearchStore(), marketplace_provider=None
    )
    competition = result.competition_opportunity[
        result.ranking.selected[0].candidate_id
    ]
    assert competition.pattern is CompetitionFieldPattern.UNKNOWN_FIELD
    assert competition.missing_reason == NOT_REQUESTED


# ---------------------------------------------------------------------- API


def test_response_exposes_competition_opportunity_additively():
    from app.main import app


    schema = app.openapi()
    response = schema["components"]["schemas"]["PreliminaryResearchResponse"]
    assert "competition_opportunity" in response["properties"]
    # Every field that existed before 4E is still present and still required.
    for field in ("purchase_evidence", "price_evidence", "buyer_reach", "ranked"):
        assert field in response["properties"]
        assert field in response["required"]
    # No endpoint was added.
    assert_openapi_surface_unchanged(schema)


def test_score_endpoint_remains_gone():
    from fastapi.testclient import TestClient

    from app.main import app

    assert TestClient(app).post("/score", json={}).status_code == 410


def test_route_count_is_unchanged():
    from app.main import app

    assert_route_surface_unchanged(app)


def test_no_scoring_vocabulary_is_introduced():
    """4E must not activate POS, ECS, or RED/YELLOW/GREEN."""
    # Checked against EXECUTABLE code with comments and docstrings stripped:
    # the module deliberately names ScoreDimensions in a comment to explain
    # which name it refuses to use, and that explanation is not an activation.
    tree = ast.parse(MODULE_PATH.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            node.value = ast.Constant(value="")
    code = ast.unparse(tree)
    # Word-boundary matched: a bare "RED" substring also appears inside
    # STORED and INFERRED, which are not classification vocabulary.
    for token in ("RED", "YELLOW", "GREEN"):
        assert not re.search(rf"\b{token}\b", code), token
    for token in ("score_opportunity", "ScoreDimensions", "Classification"):
        assert token not in code, token
