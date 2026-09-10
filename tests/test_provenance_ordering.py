"""Milestone 4D-0.1 tests: deterministic provenance ordering.

The defect these pin down was pre-existing and measured before the fix: on
main at 89d28d59, `extract_price_evidence` and `extract_purchase_evidence`
both produced a different result for 100/100 shuffled permutations of an
equivalent evidence set. Only `provenance` differed — `features` were
byte-identical — because `evidence_ids` and `listing_ids` were built in
evidence arrival order while every other provenance field already passed
through `sorted()`.

Two rules carry the whole fix, and most of this file exists to keep them
honest:

    sorted(), never set()   an evidence id may legitimately repeat, because a
                            record carrying no payload hash is never
                            collapsed. Deduplicating to achieve sorting would
                            silently change deduplication semantics.

    key=str, never str()    identifier values keep whatever type the provider
                            payload carried; only the ORDERING is canonical.

The `views` list itself is deliberately NOT sorted: it feeds feature
computation, so leaving it alone makes feature invariance true by
construction rather than by test.
"""

import random
from uuid import UUID, uuid4

from app.domain.enums import EvidencePurpose, ProductFormat, TruthClass
from app.domain.models import Candidate, EvidenceItem
from app.services.marketplace_listing_view import collect_listing_views
from app.services.price_evidence import extract_price_evidence
from app.services.product_specification import generate_product_specification
from app.services.purchase_evidence import extract_purchase_evidence
from tests.test_purchase_evidence import NOW, comparable, evidence_for_listing

SHUFFLES = 120


# ----------------------------------------------------------------- fixtures


def marketplace_evidence(count: int = 5, candidate_id: UUID | None = None):
    candidate_id = candidate_id or uuid4()
    listings = [
        comparable(f"L{k}", seller_id=f"s{k}", price=10.0 + k, review_count=5 + k)
        for k in range(count)
    ]
    evidence = [
        item for listing in listings for item in evidence_for_listing(listing, candidate_id)
    ]
    return candidate_id, evidence


def raw_record(
    listing_id,
    *,
    payload_hash: str | None = "h1",
    raw_value: float = 10.0,
    item_id: UUID | None = None,
    candidate_id: UUID | None = None,
) -> EvidenceItem:
    """A price record with full control over id, hash and listing_id type."""
    fields = dict(
        signal_type="marketplace_listing_price",
        purpose=EvidencePurpose.PRICE,
        truth_class=TruthClass.OBSERVED,
        provider="etsy",
        collection_method="official_api",
        candidate_id=candidate_id,
        raw_value=raw_value,
        raw_payload={"listing_id": listing_id, "seller_id": "s1", "is_digital": True},
        raw_payload_hash=payload_hash,
    )
    if item_id is not None:
        fields["id"] = item_id
    return EvidenceItem(**fields)


def shuffled_variants(evidence, seed: int = 4242, count: int = SHUFFLES):
    rng = random.Random(seed)
    for _ in range(count):
        variant = list(evidence)
        rng.shuffle(variant)
        yield variant


# ------------------------------------------- the core determinism guarantee


def test_price_evidence_is_identical_across_shuffled_permutations():
    """The whole result object, provenance included — not just features."""
    candidate_id, evidence = marketplace_evidence()
    baseline = extract_price_evidence(candidate_id, list(evidence), now=NOW)
    for variant in shuffled_variants(evidence):
        assert extract_price_evidence(candidate_id, variant, now=NOW) == baseline


def test_purchase_evidence_is_identical_across_shuffled_permutations():
    candidate_id, evidence = marketplace_evidence()
    baseline = extract_purchase_evidence(candidate_id, list(evidence), now=NOW)
    for variant in shuffled_variants(evidence, seed=99):
        assert extract_purchase_evidence(candidate_id, variant, now=NOW) == baseline


def test_listing_view_evidence_ids_are_identical_across_shuffles():
    candidate_id, evidence = marketplace_evidence()
    views, _, _ = collect_listing_views(list(evidence))
    baseline = {view.listing_id: view.evidence_ids for view in views}
    for variant in shuffled_variants(evidence, seed=7):
        shuffled_views, _, _ = collect_listing_views(variant)
        assert {v.listing_id: v.evidence_ids for v in shuffled_views} == baseline


def test_distinct_ids_that_render_identically_still_order_deterministically():
    """Regression from this milestone's own adversarial review.

    key=str alone is a total PREORDER, not a total order: 1 and "1" render
    the same, so Python's stable sort fell back to arrival order and the
    tuple stayed order-dependent for that pair. The type name breaks the tie.
    """
    candidate_id = uuid4()
    evidence = [
        raw_record(1, payload_hash="h1", candidate_id=candidate_id),
        raw_record("1", payload_hash="h2", candidate_id=candidate_id),
    ]
    forward = extract_price_evidence(candidate_id, list(evidence), now=NOW)
    backward = extract_price_evidence(candidate_id, list(reversed(evidence)), now=NOW)
    assert forward.provenance.listing_ids == backward.provenance.listing_ids
    # Still not coerced: both the int and the str survive as themselves.
    assert 1 in forward.provenance.listing_ids
    assert "1" in forward.provenance.listing_ids


def test_provenance_tuples_are_actually_sorted():
    candidate_id, evidence = marketplace_evidence()
    for result in (
        extract_price_evidence(candidate_id, list(evidence), now=NOW),
        extract_purchase_evidence(candidate_id, list(evidence), now=NOW),
    ):
        provenance = result.provenance
        assert list(provenance.evidence_ids) == sorted(provenance.evidence_ids)
        assert list(provenance.listing_ids) == sorted(
            provenance.listing_ids, key=lambda v: (str(v), type(v).__name__)
        )


def test_reversed_arrival_order_matches_baseline():
    candidate_id, evidence = marketplace_evidence()
    assert extract_price_evidence(
        candidate_id, list(reversed(evidence)), now=NOW
    ) == extract_price_evidence(candidate_id, list(evidence), now=NOW)
    assert extract_purchase_evidence(
        candidate_id, list(reversed(evidence)), now=NOW
    ) == extract_purchase_evidence(candidate_id, list(evidence), now=NOW)


def test_repeated_identical_execution_is_stable():
    candidate_id, evidence = marketplace_evidence()
    results = [extract_price_evidence(candidate_id, list(evidence), now=NOW) for _ in range(5)]
    assert all(result == results[0] for result in results)


# ------------------------------------------------ multiplicity preservation


def test_duplicate_hash_less_evidence_ids_keep_their_multiplicity():
    """sorted(), never set().

    A record with no payload hash is deliberately never collapsed — nothing
    proves two such records describe the same observation. Deduplicating to
    achieve sorting would change that, and would quietly contradict
    duplicate_evidence_suppressed.
    """
    duplicated = raw_record("L1", payload_hash=None, item_id=UUID(int=7))
    views, suppressed, contributing = collect_listing_views([duplicated, duplicated])

    assert len(contributing) == 2
    assert len(set(contributing)) == 1
    assert suppressed == 0
    assert views[0].evidence_ids == (UUID(int=7), UUID(int=7))


def test_hashed_duplicates_are_still_collapsed_and_counted():
    """The other half: real dedupe must keep working exactly as before."""
    record = raw_record("L1", payload_hash="h1", item_id=UUID(int=8))
    views, suppressed, contributing = collect_listing_views([record, record])
    assert len(contributing) == 1
    assert suppressed == 1
    assert views[0].evidence_ids == (UUID(int=8),)


def test_provenance_evidence_ids_preserve_duplicate_multiplicity():
    candidate_id = uuid4()
    duplicated = raw_record(
        "L1", payload_hash=None, item_id=UUID(int=11), candidate_id=candidate_id
    )
    other = raw_record(
        "L2", payload_hash=None, item_id=UUID(int=3), candidate_id=candidate_id
    )
    evidence = [duplicated, duplicated, other]
    result = extract_price_evidence(candidate_id, evidence, now=NOW)
    ids = result.provenance.evidence_ids

    assert ids == (UUID(int=3), UUID(int=11), UUID(int=11))
    assert len(ids) == 3
    assert list(ids) == sorted(ids)


# --------------------------------------------- identifier types and values


def test_mixed_type_listing_ids_do_not_crash_and_keep_their_values():
    """key=str orders them; it must not coerce them.

    Reachable through POST /product/specification, which runs both extractors
    over caller-supplied inline evidence. A bare sorted() would raise
    TypeError here and turn a working call into a derivation error.
    """
    candidate_id = uuid4()
    evidence = [
        raw_record("L1", payload_hash="h1", candidate_id=candidate_id),
        raw_record(5, payload_hash="h2", candidate_id=candidate_id),
        raw_record("10", payload_hash="h3", candidate_id=candidate_id),
    ]
    result = extract_price_evidence(candidate_id, evidence, now=NOW)
    listing_ids = result.provenance.listing_ids

    assert set(listing_ids) == {"L1", 5, "10"}
    assert 5 in listing_ids  # the int kept its type
    assert "5" not in listing_ids  # and was not coerced
    assert list(listing_ids) == sorted(listing_ids, key=lambda v: (str(v), type(v).__name__))


def test_mixed_type_listing_ids_are_order_invariant():
    candidate_id = uuid4()
    evidence = [
        raw_record("L1", payload_hash="h1", candidate_id=candidate_id),
        raw_record(5, payload_hash="h2", candidate_id=candidate_id),
        raw_record(True, payload_hash="h4", candidate_id=candidate_id),
    ]
    baseline = extract_price_evidence(candidate_id, list(evidence), now=NOW)
    for variant in shuffled_variants(evidence, seed=13, count=30):
        assert extract_price_evidence(candidate_id, variant, now=NOW) == baseline


def test_unicode_listing_ids_sort_deterministically():
    candidate_id = uuid4()
    evidence = [
        raw_record("Ｌ１", payload_hash="h1", candidate_id=candidate_id),
        raw_record("листинг", payload_hash="h2", candidate_id=candidate_id),
        raw_record("l-é-2", payload_hash="h3", candidate_id=candidate_id),
    ]
    baseline = extract_price_evidence(candidate_id, list(evidence), now=NOW)
    for variant in shuffled_variants(evidence, seed=21, count=30):
        assert extract_price_evidence(candidate_id, variant, now=NOW) == baseline
    assert set(baseline.provenance.listing_ids) == {"Ｌ１", "листинг", "l-é-2"}


def test_missing_listing_ids_are_skipped_not_sorted_in():
    candidate_id = uuid4()
    evidence = [
        raw_record(None, payload_hash="h1", candidate_id=candidate_id),
        raw_record("L1", payload_hash="h2", candidate_id=candidate_id),
    ]
    result = extract_price_evidence(candidate_id, evidence, now=NOW)
    assert result.provenance.listing_ids == ("L1",)
    assert None not in result.provenance.listing_ids


def test_empty_evidence_yields_empty_provenance():
    candidate_id = uuid4()
    views, suppressed, contributing = collect_listing_views([])
    assert (views, suppressed, contributing) == ([], 0, ())
    result = extract_price_evidence(candidate_id, [], now=NOW)
    assert result.provenance.evidence_ids == ()
    assert result.provenance.listing_ids == ()


def test_shared_listing_across_candidates_stays_disjoint():
    """Canonical ordering must not blur candidate boundaries."""
    a, b = uuid4(), uuid4()
    shared = comparable("SHARED", seller_id="s1")
    result_a = extract_purchase_evidence(a, evidence_for_listing(shared, a), now=NOW)
    result_b = extract_purchase_evidence(b, evidence_for_listing(shared, b), now=NOW)

    assert result_a.provenance.listing_ids == result_b.provenance.listing_ids == ("SHARED",)
    assert set(result_a.provenance.evidence_ids).isdisjoint(result_b.provenance.evidence_ids)


# ------------------------------- non-order semantics must be untouched


def test_features_and_statistics_survive_shuffling_unchanged():
    """Explicitly separate from the full-object test, so a future regression
    reports WHICH half broke."""
    candidate_id, evidence = marketplace_evidence()
    price = extract_price_evidence(candidate_id, list(evidence), now=NOW)
    purchase = extract_purchase_evidence(candidate_id, list(evidence), now=NOW)
    for variant in shuffled_variants(evidence, seed=55, count=40):
        shuffled_price = extract_price_evidence(candidate_id, variant, now=NOW)
        shuffled_purchase = extract_purchase_evidence(candidate_id, variant, now=NOW)
        assert shuffled_price.features == price.features
        assert shuffled_price.features.bands == price.features.bands
        assert shuffled_price.state == price.state
        assert shuffled_price.value == price.value
        assert shuffled_price.value_truth_class == price.value_truth_class
        assert shuffled_price.features_truth_class == price.features_truth_class
        assert shuffled_price.evidence_truth_basis == price.evidence_truth_basis
        assert shuffled_purchase.features == purchase.features
        assert shuffled_purchase.pattern == purchase.pattern
        assert shuffled_purchase.state == purchase.state


def test_deduplication_and_suppression_counts_are_unchanged():
    candidate_id, evidence = marketplace_evidence()
    doubled = list(evidence) + list(evidence)
    views, suppressed, contributing = collect_listing_views(doubled)
    # The helper only reads price and review-proxy records; competing-listing
    # records are outside its remit, so only the considered half is counted.
    considered = [
        item
        for item in evidence
        if item.signal_type
        in ("marketplace_listing_price", "marketplace_review_count_purchase_proxy")
    ]
    assert len(views) == 5
    assert suppressed == len(considered)
    assert len(contributing) == len(considered)
    result = extract_price_evidence(candidate_id, doubled, now=NOW)
    assert result.provenance.duplicate_evidence_suppressed == suppressed


def test_listing_view_list_order_is_deliberately_not_sorted():
    """Feature computation reads this list; reordering it is out of scope."""
    _, evidence = marketplace_evidence()
    forward, _, _ = collect_listing_views(list(evidence))
    backward, _, _ = collect_listing_views(list(reversed(evidence)))
    assert [v.listing_id for v in forward] == ["L0", "L1", "L2", "L3", "L4"]
    assert [v.listing_id for v in backward] == ["L4", "L3", "L2", "L1", "L0"]


def test_raw_payload_hashes_and_evidence_identity_are_untouched():
    _, evidence = marketplace_evidence()
    hashes = [item.raw_payload_hash for item in evidence]
    ids = [item.id for item in evidence]
    collect_listing_views(list(reversed(evidence)))
    assert [item.raw_payload_hash for item in evidence] == hashes
    assert [item.id for item in evidence] == ids


def test_query_provenance_from_4d_0_is_unchanged():
    _, evidence = marketplace_evidence()
    before = [(i.originating_queries, i.originating_query_shared) for i in evidence]
    collect_listing_views(list(reversed(evidence)))
    extract_price_evidence(uuid4(), list(reversed(evidence)), now=NOW)
    assert [(i.originating_queries, i.originating_query_shared) for i in evidence] == before


def test_product_specification_remains_deterministic_and_unchanged():
    candidate = Candidate(
        seed_keyword="planner",
        title="Budget Planner",
        problem="Buyers cannot track a budget",
        target_buyer="Households",
        proposed_format=ProductFormat.SPREADSHEET_TOOL,
        buyer_outcome="Track a monthly budget",
        search_queries=["budget planner"],
        marketplace_queries=["printable planner"],
        content_queries=["budget planner tutorial"],
        generation_reason="test fixture",
    )
    _, evidence = marketplace_evidence(candidate_id=candidate.id)
    baseline = generate_product_specification(candidate=candidate, evidence=list(evidence))
    for variant in shuffled_variants(evidence, seed=77, count=40):
        assert generate_product_specification(candidate=candidate, evidence=variant) == baseline


# ----------------------------------------------------------- API surface


def test_api_provenance_arrays_are_sorted_with_identical_membership():
    """The only observable API change: order. Same members, same count."""
    from app.api.routes import PurchaseEvidenceProvenanceOut

    candidate_id, evidence = marketplace_evidence()
    result = extract_purchase_evidence(candidate_id, list(evidence), now=NOW)
    out = PurchaseEvidenceProvenanceOut.from_provenance(result.provenance)

    assert out.evidence_ids == sorted(out.evidence_ids)
    assert out.listing_ids == sorted(out.listing_ids)
    assert len(out.evidence_ids) == len(result.provenance.evidence_ids)
    assert set(out.listing_ids) == set(result.provenance.listing_ids)


def test_score_endpoint_remains_gone():
    from fastapi.testclient import TestClient

    from app.main import app

    assert TestClient(app).post("/score").status_code == 410
