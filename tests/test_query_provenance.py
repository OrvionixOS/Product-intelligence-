"""Milestone 4D-0 tests: query provenance.

No network, no live API calls, no new provider.

The milestone exists to keep three concepts separate, and most of these
tests exist to prove they STAY separate:

    observation identity   raw_payload_hash
    candidate association  candidate_id / research_run_id
    how it was found       originating_queries

The dangerous failure is contamination: if provenance leaked into
raw_payload it would change raw_payload_hash, split the identity of one
observation reached by two queries, and inflate every downstream count.
Several tests below exist only to pin that it does not.
"""

import random
from uuid import uuid4

import pytest

from app.domain.enums import ProductFormat, TruthClass
from app.domain.models import Candidate, EvidenceItem
from app.services.marketplace import run_marketplace_research
from app.services.preliminary_dimensions import _dedupe_evidence
from app.services.marketplace_listing_view import collect_listing_views
from app.services.price_evidence import extract_price_evidence
from app.services.public_content import run_public_content_research
from app.services.purchase_evidence import extract_purchase_evidence
from app.services.query_provenance import (
    QUERY_PROVENANCE_VERSION,
    resolve_provenance,
)
from app.services.search_demand import run_search_demand_research
from app.storage.memory import ResearchStore
from tests.test_orchestration import (
    FakeContentProvider,
    FakeMarketplaceProvider,
    FakeSearchProvider,
    listing_for,
    video_for,
)
from tests.test_purchase_evidence import NOW

COMPETING = "marketplace_competing_listing"
CONTENT = "public_content_observation"


# ----------------------------------------------------------------- fixtures


def provenance_candidate(
    title: str = "Budget Planner",
    marketplace_queries: list[str] | None = None,
    content_queries: list[str] | None = None,
    search_queries: list[str] | None = None,
) -> Candidate:
    return Candidate(
        seed_keyword="planner",
        title=title,
        problem="Buyers cannot track a monthly budget",
        target_buyer="Households",
        proposed_format=ProductFormat.SPREADSHEET_TOOL,
        buyer_outcome="Track a monthly budget",
        search_queries=search_queries or ["budget planner"],
        marketplace_queries=marketplace_queries or ["printable planner"],
        content_queries=content_queries or ["budget planner tutorial"],
        generation_reason="test fixture",
    )


def records(store: ResearchStore, candidate: Candidate, signal: str) -> list[EvidenceItem]:
    return [
        item
        for item in store.evidence_for_candidate(candidate.id)
        if item.signal_type == signal
    ]


async def collect_marketplace(candidates, listings_by_query):
    store = ResearchStore()
    provider = FakeMarketplaceProvider(listings_by_query)
    await run_marketplace_research(candidates, provider, store)
    return store


# ---------------------------------------------- the resolver, in isolation


def test_candidate_filter_keeps_only_this_candidate_s_queries():
    mine, theirs = uuid4(), uuid4()
    queries, shared = resolve_provenance(
        mine,
        ["my query", "their query"],
        {"my query": [mine], "their query": [theirs]},
    )
    assert queries == ("my query",)
    assert shared is False


def test_shared_query_is_flagged_for_every_candidate_that_generated_it():
    a, b = uuid4(), uuid4()
    for candidate_id in (a, b):
        queries, shared = resolve_provenance(
            candidate_id, ["shared query"], {"shared query": [a, b]}
        )
        assert queries == ("shared query",)
        assert shared is True


def test_queries_are_sorted_and_deduplicated():
    me = uuid4()
    mapping = {"zebra": [me], "apple": [me], "mango": [me]}
    queries, _ = resolve_provenance(me, ["zebra", "apple", "zebra", "mango"], mapping)
    assert queries == ("apple", "mango", "zebra")


def test_resolution_is_order_independent():
    me = uuid4()
    mapping = {"a": [me], "b": [me], "c": [me]}
    base = resolve_provenance(me, ["a", "b", "c"], mapping)
    rng = random.Random(4)
    for _ in range(30):
        shuffled = ["a", "b", "c"]
        rng.shuffle(shuffled)
        assert resolve_provenance(me, shuffled, mapping) == base


def test_no_own_query_resolves_to_empty_not_unknown():
    """() is a fact: provenance known, containing none of this candidate's."""
    mine, theirs = uuid4(), uuid4()
    queries, shared = resolve_provenance(mine, ["their query"], {"their query": [theirs]})
    assert queries == ()
    assert queries is not None
    assert shared is False


def test_provenance_version_is_stamped():
    assert QUERY_PROVENANCE_VERSION == "query_provenance_v1"


# ------------------------------------------------ attribution, end to end


@pytest.mark.asyncio
async def test_one_candidate_one_query():
    candidate = provenance_candidate(marketplace_queries=["printable planner"])
    store = await collect_marketplace(
        [candidate], {"printable planner": [listing_for("L1")]}
    )
    item = records(store, candidate, COMPETING)[0]
    assert item.originating_queries == ("printable planner",)
    assert item.originating_query_shared is False


@pytest.mark.asyncio
async def test_one_candidate_multiple_queries_returning_one_listing():
    candidate = provenance_candidate(
        marketplace_queries=["printable planner", "budget planner"]
    )
    store = await collect_marketplace(
        [candidate],
        {
            "printable planner": [listing_for("L1")],
            "budget planner": [listing_for("L1")],
        },
    )
    item = records(store, candidate, COMPETING)[0]
    assert item.originating_queries == ("budget planner", "printable planner")
    assert item.originating_query_shared is False


@pytest.mark.asyncio
async def test_one_query_two_candidates_is_marked_shared():
    a = provenance_candidate("A", marketplace_queries=["printable planner"])
    b = provenance_candidate("B", marketplace_queries=["printable planner"])
    store = await collect_marketplace(
        [a, b], {"printable planner": [listing_for("L1")]}
    )
    for candidate in (a, b):
        item = records(store, candidate, COMPETING)[0]
        assert item.originating_queries == ("printable planner",)
        assert item.originating_query_shared is True


@pytest.mark.asyncio
async def test_no_cross_candidate_query_leakage():
    """The central attribution rule.

    Both candidates' queries return the same listing. Neither may be
    credited with the other's query.
    """
    a = provenance_candidate("A", marketplace_queries=["printable planner"])
    b = provenance_candidate("B", marketplace_queries=["meal planner"])
    store = await collect_marketplace(
        [a, b],
        {"printable planner": [listing_for("L1")], "meal planner": [listing_for("L1")]},
    )
    assert records(store, a, COMPETING)[0].originating_queries == ("printable planner",)
    assert records(store, b, COMPETING)[0].originating_queries == ("meal planner",)
    # Same observation, so it is NOT a shared QUERY.
    assert records(store, a, COMPETING)[0].originating_query_shared is False
    assert records(store, b, COMPETING)[0].originating_query_shared is False


@pytest.mark.asyncio
async def test_repeated_identical_queries_collapse_to_one_entry():
    candidate = provenance_candidate(
        marketplace_queries=["printable planner", "Printable  Planner", "PRINTABLE PLANNER"]
    )
    store = await collect_marketplace(
        [candidate], {"printable planner": [listing_for("L1")]}
    )
    item = records(store, candidate, COMPETING)[0]
    assert item.originating_queries == ("printable planner",)


@pytest.mark.asyncio
async def test_stored_query_is_normalized_not_the_raw_candidate_text():
    """Provider-request provenance, not the user's original wording."""
    raw = "   Printable   PLANNER   "
    candidate = provenance_candidate(marketplace_queries=[raw])
    store = await collect_marketplace(
        [candidate], {"printable planner": [listing_for("L1")]}
    )
    item = records(store, candidate, COMPETING)[0]
    assert item.originating_queries == ("printable planner",)
    assert raw not in item.originating_queries


@pytest.mark.asyncio
async def test_duplicate_provider_results_do_not_duplicate_provenance():
    candidate = provenance_candidate(marketplace_queries=["printable planner"])
    store = await collect_marketplace(
        [candidate],
        {"printable planner": [listing_for("L1"), listing_for("L1"), listing_for("L1")]},
    )
    items = records(store, candidate, COMPETING)
    assert len(items) == 1
    assert items[0].originating_queries == ("printable planner",)


@pytest.mark.asyncio
async def test_cache_hit_preserves_provenance():
    """A second run served from cache must still know its query."""
    candidate = provenance_candidate(marketplace_queries=["printable planner"])
    store = ResearchStore()
    provider = FakeMarketplaceProvider({"printable planner": [listing_for("L1")]})
    await run_marketplace_research([candidate], provider, store)
    first_calls = len(provider.search_calls)
    await run_marketplace_research([candidate], provider, store)
    assert len(provider.search_calls) == first_calls  # served from cache
    for item in records(store, candidate, COMPETING):
        assert item.originating_queries == ("printable planner",)


@pytest.mark.asyncio
async def test_repeated_runs_do_not_inflate_provenance():
    candidate = provenance_candidate(marketplace_queries=["printable planner"])
    store = ResearchStore()
    provider = FakeMarketplaceProvider({"printable planner": [listing_for("L1")]})
    for _ in range(3):
        await run_marketplace_research([candidate], provider, store)
    items = records(store, candidate, COMPETING)
    assert len(items) == 3  # one per run, as the append-only store intends
    assert {item.originating_queries for item in items} == {("printable planner",)}
    # The distinct observation is still ONE observation.
    assert len({item.raw_payload_hash for item in items}) == 1


# --------------------------------------------------- content and search


@pytest.mark.asyncio
async def test_content_evidence_carries_provenance():
    candidate = provenance_candidate(content_queries=["budget planner tutorial"])
    store = ResearchStore()
    provider = FakeContentProvider({"budget planner tutorial": [video_for("V1")]})
    await run_public_content_research([candidate], provider, store)
    item = records(store, candidate, CONTENT)[0]
    assert item.originating_queries == ("budget planner tutorial",)
    assert item.originating_query_shared is False


@pytest.mark.asyncio
async def test_content_shared_query_is_flagged():
    a = provenance_candidate("A", content_queries=["budget planner tutorial"])
    b = provenance_candidate("B", content_queries=["budget planner tutorial"])
    store = ResearchStore()
    provider = FakeContentProvider({"budget planner tutorial": [video_for("V1")]})
    await run_public_content_research([a, b], provider, store)
    for candidate in (a, b):
        assert records(store, candidate, CONTENT)[0].originating_query_shared is True


@pytest.mark.asyncio
async def test_search_demand_provenance_is_the_keyword_sent():
    candidate = provenance_candidate(search_queries=["budget planner"])
    store = ResearchStore()
    provider = FakeSearchProvider({"budget planner": 1000})
    await run_search_demand_research([candidate], provider, store)
    item = records(store, candidate, "search_volume")[0]
    assert item.originating_queries == ("budget planner",)
    # The keyword must remain in the payload: it is the measured subject and
    # removing it would change raw_payload_hash.
    assert item.raw_payload["keyword"] == "budget planner"


# -------------------------------------- CONTAMINATION: the critical group


@pytest.mark.asyncio
async def test_provenance_is_not_in_raw_payload():
    candidate = provenance_candidate(marketplace_queries=["printable planner"])
    store = await collect_marketplace(
        [candidate], {"printable planner": [listing_for("L1")]}
    )
    for item in store.evidence_for_candidate(candidate.id):
        payload = item.raw_payload or {}
        assert "originating_queries" not in payload
        assert "originating_query_shared" not in payload


@pytest.mark.asyncio
async def test_identical_observation_hashes_identically_under_different_queries():
    """The regression this milestone most needs.

    One listing found by two entirely different queries is still ONE
    observation and must carry ONE identity, or every downstream
    deduplicator splits it and inflates its counts.
    """
    a = provenance_candidate("A", marketplace_queries=["printable planner"])
    store_a = await collect_marketplace(
        [a], {"printable planner": [listing_for("L1")]}
    )
    b = provenance_candidate("B", marketplace_queries=["totally different words"])
    store_b = await collect_marketplace(
        [b], {"totally different words": [listing_for("L1")]}
    )
    hash_a = records(store_a, a, COMPETING)[0].raw_payload_hash
    hash_b = records(store_b, b, COMPETING)[0].raw_payload_hash
    assert hash_a == hash_b
    assert (
        records(store_a, a, COMPETING)[0].originating_queries
        != records(store_b, b, COMPETING)[0].originating_queries
    )


def test_changing_provenance_never_changes_observation_identity():
    """Explicit unit-level guarantee, independent of the collection path."""
    base = EvidenceItem(
        signal_type="marketplace_competing_listing",
        purpose=__import__("app.domain.enums", fromlist=["EvidencePurpose"]).EvidencePurpose.COMPETITION,
        truth_class=TruthClass.OBSERVED,
        provider="etsy",
        collection_method="official_api",
        raw_payload={"listing_id": "L1", "title": "Planner"},
        raw_payload_hash="fixed-hash",
    )
    with_provenance = base.model_copy(
        update={
            "originating_queries": ("printable planner",),
            "originating_query_shared": True,
        }
    )
    other_provenance = base.model_copy(
        update={"originating_queries": ("something else",), "originating_query_shared": False}
    )
    assert base.raw_payload_hash == with_provenance.raw_payload_hash
    assert with_provenance.raw_payload_hash == other_provenance.raw_payload_hash
    assert base.raw_payload == with_provenance.raw_payload == other_provenance.raw_payload
    assert base.truth_class == with_provenance.truth_class == other_provenance.truth_class


@pytest.mark.asyncio
async def test_downstream_deduplication_is_unchanged():
    candidate = provenance_candidate(
        marketplace_queries=["printable planner", "budget planner"]
    )
    store = await collect_marketplace(
        [candidate],
        {
            "printable planner": [listing_for("L1"), listing_for("L2")],
            "budget planner": [listing_for("L1")],
        },
    )
    evidence = store.evidence_for_candidate(candidate.id)
    views, suppressed, _ = collect_listing_views(evidence)
    assert {view.listing_id for view in views} == {"L1", "L2"}
    assert len(views) == 2
    kept, suppressed = _dedupe_evidence(evidence)
    assert len(kept) == len({(e.signal_type, e.raw_payload_hash, str(e.raw_value)) for e in kept})


# ----------------------------------- truth classes and 4A/4B/4C invariance


@pytest.mark.asyncio
async def test_provenance_never_upgrades_a_truth_class():
    """A listing with no price still has an UNKNOWN price record."""
    candidate = provenance_candidate(marketplace_queries=["printable planner"])
    store = await collect_marketplace(
        [candidate], {"printable planner": [listing_for("L1", price=None)]}
    )
    price_records = records(store, candidate, "marketplace_listing_price")
    assert price_records[0].truth_class == TruthClass.UNKNOWN
    assert price_records[0].originating_queries == ("printable planner",)
    assert price_records[0].truth_class != TruthClass.OBSERVED


@pytest.mark.asyncio
async def test_purchase_and_price_evidence_are_unchanged_by_provenance():
    """4A and 4B must not notice this milestone happened."""
    candidate = provenance_candidate(marketplace_queries=["printable planner"])
    store = await collect_marketplace(
        [candidate],
        {"printable planner": [listing_for(f"L{i}") for i in range(5)]},
    )
    evidence = store.evidence_for_candidate(candidate.id)
    stripped = [
        item.model_copy(
            update={"originating_queries": None, "originating_query_shared": None}
        )
        for item in evidence
    ]
    assert extract_purchase_evidence(candidate.id, evidence, now=NOW) == (
        extract_purchase_evidence(candidate.id, stripped, now=NOW)
    )
    assert extract_price_evidence(candidate.id, evidence, now=NOW) == (
        extract_price_evidence(candidate.id, stripped, now=NOW)
    )


@pytest.mark.asyncio
async def test_product_specification_is_unchanged_by_provenance():
    """4C must not notice either."""
    from app.services.product_specification import generate_product_specification

    candidate = provenance_candidate(marketplace_queries=["printable planner"])
    store = await collect_marketplace(
        [candidate], {"printable planner": [listing_for(f"L{i}") for i in range(3)]}
    )
    evidence = store.evidence_for_candidate(candidate.id)
    stripped = [
        item.model_copy(
            update={"originating_queries": None, "originating_query_shared": None}
        )
        for item in evidence
    ]
    assert generate_product_specification(
        candidate=candidate, evidence=evidence
    ) == generate_product_specification(candidate=candidate, evidence=stripped)


@pytest.mark.asyncio
async def test_provenance_does_not_change_how_4a_4b_respond_to_ordering():
    """4A/4B provenance tuples are order-dependent — on main, before this
    milestone, and still. That is pre-existing behaviour this milestone must
    neither fix nor worsen, so the invariant pinned here is equality between
    the with-provenance and without-provenance results for the SAME order.
    """
    candidate = provenance_candidate(marketplace_queries=["printable planner"])
    store = await collect_marketplace(
        [candidate], {"printable planner": [listing_for(f"L{i}") for i in range(4)]}
    )
    evidence = store.evidence_for_candidate(candidate.id)
    rng = random.Random(11)
    for _ in range(25):
        order = list(range(len(evidence)))
        rng.shuffle(order)
        shuffled = [evidence[i] for i in order]
        stripped = [
            item.model_copy(
                update={"originating_queries": None, "originating_query_shared": None}
            )
            for item in shuffled
        ]
        assert extract_price_evidence(candidate.id, shuffled, now=NOW) == (
            extract_price_evidence(candidate.id, stripped, now=NOW)
        )
        assert extract_purchase_evidence(candidate.id, shuffled, now=NOW) == (
            extract_purchase_evidence(candidate.id, stripped, now=NOW)
        )


@pytest.mark.asyncio
async def test_4c_stays_order_invariant_with_provenance_present():
    """4C was made order-invariant during its own review; provenance must
    not disturb that."""
    from app.services.product_specification import generate_product_specification

    candidate = provenance_candidate(marketplace_queries=["printable planner"])
    store = await collect_marketplace(
        [candidate], {"printable planner": [listing_for(f"L{i}") for i in range(4)]}
    )
    evidence = store.evidence_for_candidate(candidate.id)
    base = generate_product_specification(candidate=candidate, evidence=list(evidence))
    rng = random.Random(12)
    for _ in range(25):
        shuffled = list(evidence)
        rng.shuffle(shuffled)
        assert generate_product_specification(
            candidate=candidate, evidence=shuffled
        ) == base


# ------------------------------------- historical and missing provenance


def test_evidence_without_provenance_is_unknown_not_empty():
    """A record from before this milestone means UNKNOWN, never zero queries."""
    from app.domain.enums import EvidencePurpose

    historical = EvidenceItem(
        signal_type="marketplace_competing_listing",
        purpose=EvidencePurpose.COMPETITION,
        truth_class=TruthClass.OBSERVED,
        provider="etsy",
        collection_method="official_api",
    )
    assert historical.originating_queries is None
    assert historical.originating_queries != ()
    assert historical.originating_query_shared is None


def test_unknown_and_empty_provenance_are_distinguishable():
    from app.domain.enums import EvidencePurpose

    def item(queries):
        return EvidenceItem(
            signal_type="s",
            purpose=EvidencePurpose.COMPETITION,
            truth_class=TruthClass.OBSERVED,
            provider="p",
            collection_method="official_api",
            originating_queries=queries,
        )

    assert item(None).originating_queries is None
    assert item(()).originating_queries == ()
    assert item(None).originating_queries is not item(()).originating_queries


def test_nothing_reconstructs_a_query_from_candidate_text():
    """Provenance is captured at request time or it stays UNKNOWN."""
    import pathlib

    for name in ("marketplace", "public_content", "search_demand", "query_provenance"):
        source = pathlib.Path(f"app/services/{name}.py").read_text()
        for reconstructor in (
            "candidate.title",
            "candidate.problem",
            "candidate.buyer_outcome",
        ):
            snippet = source.split("originating_queries")
            for chunk in snippet[1:]:
                assert reconstructor not in chunk[:200], (name, reconstructor)


# --------------------------------------------------- API compatibility


def test_provenance_fields_are_optional_and_additive():
    from app.domain.enums import EvidencePurpose

    # Every pre-existing construction site still works untouched.
    item = EvidenceItem(
        signal_type="search_volume",
        purpose=EvidencePurpose.SEARCH_DEMAND,
        truth_class=TruthClass.OBSERVED,
        provider="dataforseo",
        collection_method="official_api",
    )
    assert item.originating_queries is None
    assert "originating_queries" in EvidenceItem.model_fields
    assert not EvidenceItem.model_fields["originating_queries"].is_required()
    assert not EvidenceItem.model_fields["originating_query_shared"].is_required()


def test_snapshot_endpoints_expose_the_new_fields():
    """Additive response change on the three snapshot endpoints."""
    from app.api.routes import SnapshotEvidenceResponse

    evidence_field = SnapshotEvidenceResponse.model_fields["evidence"]
    assert evidence_field.annotation == list[EvidenceItem]
    dumped = EvidenceItem(
        signal_type="s",
        purpose=__import__("app.domain.enums", fromlist=["EvidencePurpose"]).EvidencePurpose.COMPETITION,
        truth_class=TruthClass.OBSERVED,
        provider="p",
        collection_method="official_api",
        originating_queries=("printable planner",),
        originating_query_shared=False,
    ).model_dump(mode="json")
    assert dumped["originating_queries"] == ["printable planner"]
    assert dumped["originating_query_shared"] is False


def test_schema_columns_are_nullable_with_no_default():
    """NULL must keep meaning UNKNOWN for historical rows."""
    import pathlib
    import re

    schema = pathlib.Path("schema.sql").read_text()
    for column in ("originating_queries jsonb", "originating_query_shared boolean"):
        assert column in schema
        line = next(l for l in schema.splitlines() if l.strip().startswith(column))
        assert "not null" not in line.lower()
        assert "default" not in line.lower()
    assert not re.search(r"update\s+evidence_items", schema, re.IGNORECASE)
