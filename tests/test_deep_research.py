"""Milestone 6C tests: the Step 7 deep research boundary object.

No network, no live API calls, no new provider.

Four rules carry this milestone.

1. Uniform, verified scope. One candidate, one run, both checked before any
   evidence is read, and a mixed-scope collection refused unread.

2. Evidence and explicit states, never precomputed composites. Step 8 cannot
   be handed a number this module invented.

3. Required and POS-eligible are different properties, and the sets overlap.
   Competition structure and channel reach are required AND contextual-only.

4. `preliminary_rank` appears nowhere. Rank is selection-only, and carrying it
   forward would let triage policy leak into scoring.
"""

import ast
import random
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from app.domain.enums import (
    COLLECTION_METHOD_OFFICIAL_API,
    EvidencePurpose,
    ProductFormat,
    SnapshotStatus,
    TruthClass,
)
from app.domain.models import Candidate, EvidenceItem
from app.services.deep_research import (
    CONTEXTUAL_ONLY,
    DEEP_RESEARCH_VERSION,
    DEEP_SEARCH_DEMAND_VERSION,
    DIMENSION_ORDER,
    LIMITATIONS,
    POS_ELIGIBLE,
    STATE_BOUNDARIES,
    DeepResearchResult,
    DeepResearchState,
    build_deep_research_result,
)
from app.services.evidence_confidence import (
    REQUIRED_DIMENSIONS,
    Capability,
    EcsDimension,
    EcsState,
)
from app.services.preliminary_dimensions import DimensionState
from app.services.research_orchestration import (
    CAPABILITY_MARKETPLACE,
    CAPABILITY_PUBLIC_CONTENT,
    CAPABILITY_SEARCH_DEMAND,
    STATUS_NOT_REQUESTED,
    STATUS_PROVIDER_FAILED,
    CapabilityOutcome,
)

MODULE_PATH = Path("app/services/deep_research.py")
NOW = datetime(2026, 9, 16, tzinfo=UTC)


# ----------------------------------------------------------------- fixtures


def make_candidate() -> Candidate:
    return Candidate(
        seed_keyword="sourdough baking",
        title="Sourdough Guide",
        problem="A problem statement.",
        target_buyer="A buyer",
        proposed_format=ProductFormat.PDF_GUIDE,
        buyer_outcome="An outcome",
        search_queries=["sourdough"],
        marketplace_queries=["sourdough guide"],
        content_queries=["sourdough guide"],
        generation_reason="A hypothesis",
    )


def keyword_evidence(
    candidate_id: UUID,
    research_run_id: UUID,
    keyword: str,
    volume: int | None,
    *,
    truth_class: TruthClass | None = None,
) -> EvidenceItem:
    observed = volume is not None
    return EvidenceItem(
        candidate_id=candidate_id,
        research_run_id=research_run_id,
        provider="fake-search",
        collection_method=COLLECTION_METHOD_OFFICIAL_API,
        signal_type="search_volume",
        purpose=EvidencePurpose.SEARCH_DEMAND,
        truth_class=truth_class
        or (TruthClass.OBSERVED if observed else TruthClass.UNKNOWN),
        raw_value=volume,
        unit="searches_per_month" if observed else None,
        retrieved_at=NOW,
        raw_payload={"keyword": keyword, "search_volume": volume},
        raw_payload_hash=f"kw-{keyword}-{volume}",
    )


def listing_evidence(
    candidate_id: UUID, research_run_id: UUID, listing_id: str, price: float
) -> EvidenceItem:
    return EvidenceItem(
        candidate_id=candidate_id,
        research_run_id=research_run_id,
        provider="fake-marketplace",
        marketplace="fake-marketplace",
        collection_method=COLLECTION_METHOD_OFFICIAL_API,
        signal_type="marketplace_listing_price",
        purpose=EvidencePurpose.PRICE,
        truth_class=TruthClass.OBSERVED,
        raw_value=price,
        unit="USD",
        retrieved_at=NOW,
        raw_payload={"listing_id": listing_id, "price": price},
        raw_payload_hash=f"lst-{listing_id}",
    )


def marketplace_listing(
    candidate_id: UUID,
    research_run_id: UUID,
    listing_id: str,
    *,
    price: float | None,
    review_count: int = 7,
) -> list[EvidenceItem]:
    """The three signals a real marketplace listing emits.

    `price=None` makes price evidence unavailable while purchase proxy and
    competition structure stay fully observed, which is the only way to
    exercise an optional dimension failing on its own.
    """
    payload = {"listing_id": listing_id, "price": price, "review_count": review_count}
    common = dict(
        candidate_id=candidate_id,
        research_run_id=research_run_id,
        provider="fake-marketplace",
        marketplace="fake-marketplace",
        collection_method=COLLECTION_METHOD_OFFICIAL_API,
        retrieved_at=NOW,
        raw_payload=payload,
        raw_payload_hash=f"lst-{listing_id}",
    )
    return [
        EvidenceItem(
            signal_type="marketplace_listing_price",
            purpose=EvidencePurpose.PRICE,
            truth_class=(
                TruthClass.OBSERVED if price is not None else TruthClass.UNKNOWN
            ),
            raw_value=price,
            unit="USD" if price is not None else None,
            **common,
        ),
        EvidenceItem(
            signal_type="marketplace_review_count_purchase_proxy",
            purpose=EvidencePurpose.PURCHASE,
            truth_class=TruthClass.OBSERVED,
            raw_value=review_count,
            **common,
        ),
        EvidenceItem(
            signal_type="marketplace_competing_listing",
            purpose=EvidencePurpose.COMPETITION,
            truth_class=TruthClass.OBSERVED,
            raw_value=1,
            **common,
        ),
    ]


def video_evidence(
    candidate_id: UUID, research_run_id: UUID, video_id: str, views: int
) -> EvidenceItem:
    return EvidenceItem(
        candidate_id=candidate_id,
        research_run_id=research_run_id,
        provider="fake-content",
        platform="fake-content",
        collection_method=COLLECTION_METHOD_OFFICIAL_API,
        signal_type="public_video_view_count",
        purpose=EvidencePurpose.AUDIENCE,
        truth_class=TruthClass.OBSERVED,
        raw_value=views,
        retrieved_at=NOW,
        raw_payload={"video_id": video_id, "view_count": views},
        raw_payload_hash=f"vid-{video_id}",
    )


def all_capabilities(status: str = SnapshotStatus.COMPLETE.value):
    return (
        CapabilityOutcome(capability=CAPABILITY_SEARCH_DEMAND, status=status),
        CapabilityOutcome(capability=CAPABILITY_MARKETPLACE, status=status),
        CapabilityOutcome(capability=CAPABILITY_PUBLIC_CONTENT, status=status),
    )


def build(candidate_id, run_id, evidence, capabilities=None, **kwargs):
    return build_deep_research_result(
        candidate_id=candidate_id,
        research_run_id=run_id,
        deep_pass_id=kwargs.pop("deep_pass_id", uuid4()),
        evidence=evidence,
        capability_outcomes=capabilities or all_capabilities(),
        now=kwargs.pop("now", NOW),
        **kwargs,
    )


def dimension(result, name: EcsDimension):
    return result.dimensions[name.value]


# ----------------------------------------------------------------- scope


def test_evidence_from_another_candidate_is_refused_unread():
    mine, theirs, run_id = uuid4(), uuid4(), uuid4()
    result = build(
        mine,
        run_id,
        [
            keyword_evidence(mine, run_id, "a", 100),
            keyword_evidence(theirs, run_id, "b", 200),
        ],
    )
    assert result.state is DeepResearchState.SCOPE_MISMATCH
    assert result.dimensions == {}
    assert result.evidence_ids == ()
    assert result.evidence_confidence is None
    assert "refused unread" in result.state_boundary


def test_evidence_from_another_run_is_refused_unread():
    candidate_id, run_a, run_b = uuid4(), uuid4(), uuid4()
    result = build(
        candidate_id,
        run_a,
        [
            keyword_evidence(candidate_id, run_a, "a", 100),
            keyword_evidence(candidate_id, run_b, "b", 200),
        ],
    )
    assert result.state is DeepResearchState.SCOPE_MISMATCH


def test_a_mixed_scope_is_refused_rather_than_filtered():
    """Filtering would silently answer a question nobody asked."""
    mine, theirs, run_id = uuid4(), uuid4(), uuid4()
    mixed = [
        keyword_evidence(mine, run_id, "a", 100),
        keyword_evidence(theirs, run_id, "a", 100),
    ]
    refused = build(mine, run_id, mixed)
    clean = build(mine, run_id, [mixed[0]])
    assert refused.state is DeepResearchState.SCOPE_MISMATCH
    assert clean.state is not DeepResearchState.SCOPE_MISMATCH
    assert refused.evidence_ids == (), "nothing was read at all"


def test_the_object_is_scoped_to_exactly_one_candidate_and_run():
    candidate_id, run_id, deep_pass_id = uuid4(), uuid4(), uuid4()
    result = build(
        candidate_id,
        run_id,
        [keyword_evidence(candidate_id, run_id, "a", 100)],
        deep_pass_id=deep_pass_id,
    )
    assert result.candidate_id == candidate_id
    assert result.research_run_id == run_id
    assert result.deep_pass_id == deep_pass_id


# -------------------------------------------- rank must not leak forward


def test_no_rank_appears_anywhere_on_the_boundary_object():
    """§11: rank is selection-only and must not appear in any form."""
    fields = set(DeepResearchResult.__dataclass_fields__)
    assert not any("rank" in name.lower() for name in fields), fields

    candidate_id, run_id = uuid4(), uuid4()
    result = build(
        candidate_id, run_id, [keyword_evidence(candidate_id, run_id, "a", 100)]
    )
    for dim in result.dimensions.values():
        assert not any("rank" in key.lower() for key in dim.observed_features)


def test_the_module_never_reaches_for_preliminary_ranking():
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    imported = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert "app.services.preliminary_ranking" not in imported
    names = {
        node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
    } | {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    for forbidden in ("preliminary_rank", "rank", "ranking", "selection_size"):
        assert forbidden not in names, forbidden


# ------------------------------ required is not the same as POS-eligible


def test_every_dimension_states_both_properties_separately():
    candidate_id, run_id = uuid4(), uuid4()
    result = build(
        candidate_id, run_id, [keyword_evidence(candidate_id, run_id, "a", 100)]
    )
    for name, dim in result.dimensions.items():
        assert isinstance(dim.required, bool), name
        assert isinstance(dim.contextual_only, bool), name
        assert isinstance(dim.pos_eligible, bool), name


def test_competition_structure_and_channel_reach_are_required_and_contextual():
    """The overlap that collapsing the two flags would destroy."""
    for name in (EcsDimension.COMPETITION_STRUCTURE, EcsDimension.CHANNEL_REACH):
        assert name in REQUIRED_DIMENSIONS, name
        assert name in CONTEXTUAL_ONLY, name
        assert name not in POS_ELIGIBLE, name


def test_price_evidence_is_optional_and_contextual():
    assert EcsDimension.PRICE_EVIDENCE not in REQUIRED_DIMENSIONS
    assert EcsDimension.PRICE_EVIDENCE in CONTEXTUAL_ONLY
    assert EcsDimension.PRICE_EVIDENCE not in POS_ELIGIBLE


def test_the_v1_pos_surface_is_closed_at_exactly_three():
    assert POS_ELIGIBLE == {
        EcsDimension.SEARCH_DEMAND,
        EcsDimension.PURCHASE_PROXY_EVIDENCE,
        EcsDimension.AUDIENCE_ATTENTION,
    }
    assert POS_ELIGIBLE & CONTEXTUAL_ONLY == set()
    assert POS_ELIGIBLE | CONTEXTUAL_ONLY == set(EcsDimension)


def test_contextual_and_pos_eligible_partition_the_dimension_surface():
    """No dimension is both, and none is neither."""
    for name in EcsDimension:
        assert (name in POS_ELIGIBLE) != (name in CONTEXTUAL_ONLY), name


def test_all_six_dimensions_are_emitted_in_a_fixed_order():
    candidate_id, run_id = uuid4(), uuid4()
    result = build(
        candidate_id, run_id, [keyword_evidence(candidate_id, run_id, "a", 100)]
    )
    assert tuple(result.dimensions) == tuple(d.value for d in DIMENSION_ORDER)
    assert set(DIMENSION_ORDER) == set(EcsDimension)


# ------------------------------------------- D1: a distribution, not a score


def test_deep_search_demand_emits_a_distribution_never_a_single_value():
    """§2 and §5 R-3: no single 0-100 value for D1."""
    candidate_id, run_id = uuid4(), uuid4()
    result = build(
        candidate_id,
        run_id,
        [
            keyword_evidence(candidate_id, run_id, "a", 100),
            keyword_evidence(candidate_id, run_id, "b", 300),
            keyword_evidence(candidate_id, run_id, "c", 200),
        ],
    )
    demand = dimension(result, EcsDimension.SEARCH_DEMAND)
    assert demand.state is DimensionState.EVIDENCE_PRESENT_UNSCORED
    assert demand.observed_features["median_observed_search_volume"] == 200
    assert demand.observed_features["keywords_measured"] == 3
    assert demand.observed_features["keywords_queried"] == 3
    assert demand.formula_version == DEEP_SEARCH_DEMAND_VERSION
    # No composite of any kind.
    for key in demand.observed_features:
        assert "dimension" not in key
        assert "score" not in key


def test_a_queried_keyword_with_no_measurement_is_unknown_not_zero():
    candidate_id, run_id = uuid4(), uuid4()
    result = build(
        candidate_id, run_id, [keyword_evidence(candidate_id, run_id, "a", None)]
    )
    demand = dimension(result, EcsDimension.SEARCH_DEMAND)
    assert demand.state is DimensionState.UNKNOWN
    assert demand.observed_features["keywords_queried"] == 1
    assert demand.observed_features["keywords_measured"] == 0
    assert "median_observed_search_volume" not in demand.observed_features


def test_an_observed_zero_volume_is_a_real_measurement():
    """§8's zero rule: an observed zero enters, an absent one never does."""
    candidate_id, run_id = uuid4(), uuid4()
    result = build(
        candidate_id, run_id, [keyword_evidence(candidate_id, run_id, "a", 0)]
    )
    demand = dimension(result, EcsDimension.SEARCH_DEMAND)
    assert demand.state is DimensionState.EVIDENCE_PRESENT_UNSCORED
    assert demand.observed_features["median_observed_search_volume"] == 0
    assert demand.observed_features["keywords_measured"] == 1
    assert demand.sample_size == 1


def test_a_capability_that_never_ran_is_missing_with_a_reason():
    candidate_id, run_id = uuid4(), uuid4()
    result = build(
        candidate_id,
        run_id,
        [],
        capabilities=(
            CapabilityOutcome(
                capability=CAPABILITY_SEARCH_DEMAND,
                status=STATUS_NOT_REQUESTED,
                failure_reason="capability_not_requested",
            ),
            CapabilityOutcome(
                capability=CAPABILITY_MARKETPLACE, status=STATUS_PROVIDER_FAILED
            ),
            CapabilityOutcome(
                capability=CAPABILITY_PUBLIC_CONTENT, status=STATUS_NOT_REQUESTED
            ),
        ),
    )
    demand = dimension(result, EcsDimension.SEARCH_DEMAND)
    assert demand.state is DimensionState.MISSING
    assert demand.missing_reason == "capability_not_requested"
    assert demand.sample_size is None


def test_missing_unknown_and_observed_zero_are_three_different_things():
    candidate_id, run_id = uuid4(), uuid4()
    missing = dimension(
        build(candidate_id, run_id, []), EcsDimension.SEARCH_DEMAND
    )
    unknown = dimension(
        build(candidate_id, run_id, [keyword_evidence(candidate_id, run_id, "a", None)]),
        EcsDimension.SEARCH_DEMAND,
    )
    zero = dimension(
        build(candidate_id, run_id, [keyword_evidence(candidate_id, run_id, "a", 0)]),
        EcsDimension.SEARCH_DEMAND,
    )
    states = {missing.state, unknown.state, zero.state}
    assert len(states) == 3
    assert missing.sample_size is None
    assert unknown.sample_size is None
    assert zero.sample_size == 1


# ----------------------------------------------------------------- conflicts


def test_two_observed_volumes_for_one_keyword_conflict():
    candidate_id, run_id = uuid4(), uuid4()
    result = build(
        candidate_id,
        run_id,
        [
            keyword_evidence(candidate_id, run_id, "a", 100),
            keyword_evidence(candidate_id, run_id, "a", 900),
            keyword_evidence(candidate_id, run_id, "b", 200),
        ],
    )
    demand = dimension(result, EcsDimension.SEARCH_DEMAND)
    assert demand.conflict_count == 2
    # Excluded from the median, never averaged away.
    assert demand.observed_features["median_observed_search_volume"] == 200
    assert demand.observed_features["keywords_measured"] == 1


def test_an_identical_repeat_observation_is_a_duplicate_not_a_conflict():
    """Disagreement is a conflict; agreement is the 5B fingerprint's job."""
    candidate_id, run_id = uuid4(), uuid4()
    result = build(
        candidate_id,
        run_id,
        [
            keyword_evidence(candidate_id, run_id, "a", 100),
            keyword_evidence(candidate_id, run_id, "a", 100),
        ],
    )
    demand = dimension(result, EcsDimension.SEARCH_DEMAND)
    assert demand.conflict_count == 0
    assert demand.observed_features["median_observed_search_volume"] == 100


def test_an_unknown_record_never_conflicts_with_an_observed_one():
    """Absence of a measurement is not a competing measurement."""
    candidate_id, run_id = uuid4(), uuid4()
    result = build(
        candidate_id,
        run_id,
        [
            keyword_evidence(candidate_id, run_id, "a", 100),
            keyword_evidence(candidate_id, run_id, "a", None),
        ],
    )
    assert dimension(result, EcsDimension.SEARCH_DEMAND).conflict_count == 0


def test_a_record_naming_no_entity_is_never_counted_as_conflicting():
    """Conflict needs an identity to disagree about."""
    candidate_id, run_id = uuid4(), uuid4()
    anonymous = keyword_evidence(candidate_id, run_id, "a", 100)
    object.__setattr__(anonymous, "raw_payload", {"unrelated": "shape"})
    result = build(candidate_id, run_id, [anonymous, anonymous])
    assert dimension(result, EcsDimension.SEARCH_DEMAND).conflict_count == 0


def test_conflicts_raise_the_ecs_conflict_rate():
    candidate_id, run_id = uuid4(), uuid4()
    clean = build(
        candidate_id,
        run_id,
        [keyword_evidence(candidate_id, run_id, f"k{i}", 100 + i) for i in range(4)],
    )
    contested = build(
        candidate_id,
        run_id,
        [keyword_evidence(candidate_id, run_id, f"k{i}", 100 + i) for i in range(4)]
        + [keyword_evidence(candidate_id, run_id, "k0", 9999)],
    )
    assert dimension(contested, EcsDimension.SEARCH_DEMAND).conflict_count > 0
    assert (
        contested.evidence_confidence.evidence_confidence
        < clean.evidence_confidence.evidence_confidence
    )


# ------------------------------------------------------------ ECS wiring


def test_evidence_confidence_is_computed_over_this_dossier():
    candidate_id, run_id = uuid4(), uuid4()
    result = build(
        candidate_id,
        run_id,
        [keyword_evidence(candidate_id, run_id, "a", 100)],
    )
    ecs = result.evidence_confidence
    assert ecs is not None
    assert ecs.state is EcsState.COMPUTED
    assert ecs.provenance.candidate_id == candidate_id
    assert ecs.provenance.research_run_id == run_id
    assert 0.0 <= ecs.evidence_confidence <= 100.0


def test_a_failed_capability_lowers_confidence_without_zeroing_a_dimension():
    candidate_id, run_id = uuid4(), uuid4()
    evidence = [keyword_evidence(candidate_id, run_id, "a", 100)]
    healthy = build(candidate_id, run_id, evidence)
    degraded = build(
        candidate_id,
        run_id,
        evidence,
        capabilities=(
            CapabilityOutcome(
                capability=CAPABILITY_SEARCH_DEMAND,
                status=SnapshotStatus.COMPLETE.value,
            ),
            CapabilityOutcome(
                capability=CAPABILITY_MARKETPLACE, status=STATUS_PROVIDER_FAILED
            ),
            CapabilityOutcome(
                capability=CAPABILITY_PUBLIC_CONTENT,
                status=SnapshotStatus.COMPLETE.value,
            ),
        ),
    )
    assert (
        degraded.evidence_confidence.evidence_confidence
        < healthy.evidence_confidence.evidence_confidence
    )
    # The dimension itself is still reported, not zeroed.
    assert dimension(degraded, EcsDimension.SEARCH_DEMAND).observed_features


def test_the_dossier_sample_counts_reach_ecs_per_capability():
    candidate_id, run_id = uuid4(), uuid4()
    result = build(
        candidate_id,
        run_id,
        [keyword_evidence(candidate_id, run_id, f"k{i}", 10) for i in range(3)]
        + [listing_evidence(candidate_id, run_id, f"L{i}", 10.0) for i in range(4)],
    )
    demand = dimension(result, EcsDimension.SEARCH_DEMAND)
    assert demand.sample_sizes_by_capability == ((Capability.SEARCH_DEMAND, 3),)
    reach = dimension(result, EcsDimension.CHANNEL_REACH)
    by_capability = dict(reach.sample_sizes_by_capability)
    assert by_capability[Capability.SEARCH_DEMAND] == 3
    assert by_capability[Capability.MARKETPLACE] == 4


# ------------------------------------------------------------------ state


def test_state_is_complete_only_when_every_required_dimension_is_scoreable():
    candidate_id, run_id = uuid4(), uuid4()
    thin = build(
        candidate_id, run_id, [keyword_evidence(candidate_id, run_id, "a", 100)]
    )
    assert thin.state is DeepResearchState.PARTIAL
    assert set(thin.missing_dimensions) >= {
        EcsDimension.PURCHASE_PROXY_EVIDENCE.value
    }


def test_a_partial_dossier_still_reports_everything_it_computed():
    """§8: retain what was computable, name what was not."""
    candidate_id, run_id = uuid4(), uuid4()
    result = build(
        candidate_id, run_id, [keyword_evidence(candidate_id, run_id, "a", 100)]
    )
    assert result.state is DeepResearchState.PARTIAL
    assert len(result.dimensions) == 6
    assert dimension(result, EcsDimension.SEARCH_DEMAND).observed_features
    assert result.evidence_confidence is not None


def test_partial_is_not_a_low_score():
    boundary = STATE_BOUNDARIES[DeepResearchState.PARTIAL]
    assert "not a low score" in boundary
    assert "never be rendered as one" in boundary


def test_every_state_says_what_it_does_not_establish():
    for state in DeepResearchState:
        assert state in STATE_BOUNDARIES
        assert STATE_BOUNDARIES[state].strip()


def test_every_deep_research_state_is_reachable():
    candidate_id, other, run_id = uuid4(), uuid4(), uuid4()
    observed = {
        build(candidate_id, run_id, []).state,
        build(
            candidate_id,
            run_id,
            [keyword_evidence(candidate_id, run_id, "a", 1)]
            + [keyword_evidence(other, run_id, "b", 1)],
        ).state,
    }
    assert DeepResearchState.PARTIAL in observed
    assert DeepResearchState.SCOPE_MISMATCH in observed
    # COMPLETE needs every required dimension scoreable, which the full
    # pipeline test below reaches.
    assert len(DeepResearchState) == 3


def test_an_unknown_required_dimension_is_not_scoreable():
    """UNKNOWN is not MISSING and is not scoreable either.

    Treating "ran and returned nothing" as scoreable would let a dimension
    with no measurement count toward a complete dossier and toward evidence
    coverage.
    """
    candidate_id, run_id = uuid4(), uuid4()
    result = build(
        candidate_id, run_id, [keyword_evidence(candidate_id, run_id, "a", None)]
    )
    demand = dimension(result, EcsDimension.SEARCH_DEMAND)
    assert demand.state is DimensionState.UNKNOWN
    assert demand.scoreable is False
    assert result.state is DeepResearchState.PARTIAL
    assert EcsDimension.SEARCH_DEMAND.value in result.missing_dimensions


def test_an_unscoreable_optional_dimension_does_not_block_completeness():
    """D3 is optional: a candidate with no priced comparables is scoreable."""
    candidate_id, run_id = uuid4(), uuid4()
    evidence: list[EvidenceItem] = []
    for index in range(6):
        evidence += marketplace_listing(
            candidate_id, run_id, f"L{index}", price=None
        )
    evidence += [
        keyword_evidence(candidate_id, run_id, f"k{i}", 40 + i) for i in range(6)
    ]
    evidence += [
        video_evidence(candidate_id, run_id, f"V{i}", 1000 + i) for i in range(6)
    ]
    result = build(candidate_id, run_id, evidence)
    price = dimension(result, EcsDimension.PRICE_EVIDENCE)
    assert price.scoreable is False, "no priced comparables were collected"
    assert price.required is False
    assert all(
        dimension(result, d).scoreable for d in REQUIRED_DIMENSIONS
    ), result.missing_dimensions
    assert result.state is DeepResearchState.COMPLETE


def test_a_composite_value_can_never_reach_observed_features():
    """The filter is forward-looking: no current features dataclass carries a
    composite, so it is tested directly rather than left unexercised."""
    from dataclasses import dataclass as _dataclass

    from app.services.deep_research import _features_of

    @_dataclass
    class FeaturesWithAScore:
        observed_count: int
        value: float
        score: float
        normalized_value: float

    class Result:
        features = FeaturesWithAScore(
            observed_count=3, value=88.0, score=88.0, normalized_value=88.0
        )

    assert _features_of(Result()) == {"observed_count": 3}


def test_richer_per_capability_samples_raise_evidence_confidence():
    """The dossier's sample counts really reach ECS, per capability."""
    candidate_id, run_id = uuid4(), uuid4()

    def dossier(keyword_count: int):
        evidence: list[EvidenceItem] = [
            keyword_evidence(candidate_id, run_id, f"k{i}", 50 + i)
            for i in range(keyword_count)
        ]
        for index in range(10):
            evidence += marketplace_listing(
                candidate_id, run_id, f"L{index}", price=10.0 + index
            )
        evidence += [
            video_evidence(candidate_id, run_id, f"V{i}", 1000 + i) for i in range(10)
        ]
        return build(candidate_id, run_id, evidence)

    thin = dossier(1)
    rich = dossier(8)
    assert dimension(thin, EcsDimension.SEARCH_DEMAND).sample_sizes_by_capability == (
        (Capability.SEARCH_DEMAND, 1),
    )
    assert (
        rich.evidence_confidence.evidence_confidence
        > thin.evidence_confidence.evidence_confidence
    )


def test_each_dimension_emits_canonically_ordered_evidence_ids():
    candidate_id, run_id = uuid4(), uuid4()
    evidence: list[EvidenceItem] = []
    for index in range(5):
        evidence += marketplace_listing(
            candidate_id, run_id, f"L{index}", price=10.0 + index
        )
    result = build(candidate_id, run_id, list(reversed(evidence)))
    for name, dim in result.dimensions.items():
        assert list(dim.evidence_ids) == sorted(
            dim.evidence_ids, key=lambda v: (str(v), type(v).__name__)
        ), name


# ------------------------------------------------------------ determinism


def _identity(result) -> tuple:
    return (
        result.state,
        result.evidence_ids,
        result.providers,
        result.platforms,
        tuple(result.component_versions.items()),
        tuple(
            (
                name,
                d.state,
                d.missing_reason,
                tuple(sorted(d.observed_features.items(), key=lambda kv: kv[0])),
                d.truth_basis,
                d.evidence_ids,
                d.formula_version,
                d.conflict_count,
                d.sample_size,
                d.sample_sizes_by_capability,
            )
            for name, d in result.dimensions.items()
        ),
        result.evidence_confidence.evidence_confidence,
    )


def test_output_is_identical_under_200_input_permutations():
    candidate_id, run_id = uuid4(), uuid4()
    deep_pass_id = uuid4()
    evidence = [
        keyword_evidence(candidate_id, run_id, f"k{i}", 100 + i * 10) for i in range(5)
    ] + [listing_evidence(candidate_id, run_id, f"L{i}", 10.0 + i) for i in range(5)]
    baseline = _identity(
        build(candidate_id, run_id, evidence, deep_pass_id=deep_pass_id)
    )
    rng = random.Random(6300)
    for _ in range(200):
        shuffled = list(evidence)
        rng.shuffle(shuffled)
        assert (
            _identity(build(candidate_id, run_id, shuffled, deep_pass_id=deep_pass_id))
            == baseline
        )


def test_the_same_evidence_always_produces_the_same_object():
    candidate_id, run_id, deep_pass_id = uuid4(), uuid4(), uuid4()
    evidence = [keyword_evidence(candidate_id, run_id, "a", 100)]
    first = _identity(build(candidate_id, run_id, evidence, deep_pass_id=deep_pass_id))
    for _ in range(10):
        assert (
            _identity(build(candidate_id, run_id, evidence, deep_pass_id=deep_pass_id))
            == first
        )


def test_evidence_ids_are_canonically_ordered():
    candidate_id, run_id = uuid4(), uuid4()
    evidence = [
        keyword_evidence(candidate_id, run_id, f"k{i}", 10) for i in range(6)
    ]
    result = build(candidate_id, run_id, list(reversed(evidence)))
    assert list(result.evidence_ids) == sorted(
        result.evidence_ids, key=lambda value: (str(value), type(value).__name__)
    )


# ------------------------------------------------- no score, no composite


def test_no_dimension_carries_an_opportunity_magnitude():
    candidate_id, run_id = uuid4(), uuid4()
    result = build(
        candidate_id,
        run_id,
        [keyword_evidence(candidate_id, run_id, "a", 100)]
        + [listing_evidence(candidate_id, run_id, "L1", 12.0)],
    )
    for name, dim in result.dimensions.items():
        for key in dim.observed_features:
            assert key not in ("value", "score", "normalized_value"), (name, key)


def test_the_module_computes_no_pos_and_no_classification():
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    symbols = {
        node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
    } | {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    symbols |= {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    for forbidden in (
        "app.services.scoring", "score_opportunity", "opportunity_score",
        "Classification", "classify", "apply_kill_rules", "WEIGHTS",
        "RED", "YELLOW", "GREEN", "pos_search_demand", "pos_purchase_proxy",
        "pos_audience_attention",
    ):
        assert forbidden not in symbols, forbidden


def test_the_module_is_provider_free_and_makes_no_network_call():
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        module = None
        if isinstance(node, ast.ImportFrom):
            module = node.module
        elif isinstance(node, ast.Import):
            module = node.names[0].name
        if not module:
            continue
        for forbidden in (
            "httpx", "requests", "aiohttp", "urllib", "socket",
            "app.providers", "app.api", "app.storage", "openai", "anthropic",
        ):
            assert not module.startswith(forbidden), module


def test_no_endpoint_was_added_and_score_remains_gone():
    from fastapi.testclient import TestClient

    from app.main import app

    paths = {route.path for route in app.routes if hasattr(route, "methods")}
    assert len(paths) == 15
    assert TestClient(app).post("/score", json={}).status_code == 410


def test_legacy_scoring_remains_quarantined():
    from app.services.scoring import SCORING_STATUS

    assert SCORING_STATUS == "UNAPPROVED_EXPERIMENTAL"


# --------------------------------------------------------------- contract


def test_every_dimension_satisfies_the_step_8_input_contract():
    candidate_id, run_id = uuid4(), uuid4()
    result = build(
        candidate_id,
        run_id,
        [keyword_evidence(candidate_id, run_id, "a", 100)]
        + [listing_evidence(candidate_id, run_id, "L1", 12.0)],
    )
    for name, dim in result.dimensions.items():
        assert dim.dimension_name == name
        assert dim.dimension_name.startswith("deep_")
        assert isinstance(dim.state, DimensionState)
        assert isinstance(dim.evidence_ids, tuple)
        assert dim.formula_version
        assert isinstance(dim.conflict_count, int)
        assert dim.sample_size is None or dim.sample_size > 0, "never 0"
        if dim.state is DimensionState.MISSING:
            assert dim.missing_reason, name


def test_every_dimension_records_its_own_formula_version():
    candidate_id, run_id = uuid4(), uuid4()
    result = build(
        candidate_id, run_id, [keyword_evidence(candidate_id, run_id, "a", 100)]
    )
    assert result.version == DEEP_RESEARCH_VERSION
    assert result.component_versions["deep_research"] == DEEP_RESEARCH_VERSION
    for name, dim in result.dimensions.items():
        assert result.component_versions[name] == dim.formula_version


def test_the_result_is_immutable():
    candidate_id, run_id = uuid4(), uuid4()
    result = build(
        candidate_id, run_id, [keyword_evidence(candidate_id, run_id, "a", 100)]
    )
    with pytest.raises(FrozenInstanceError):
        result.state = DeepResearchState.COMPLETE  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        dimension(result, EcsDimension.SEARCH_DEMAND).conflict_count = 99  # type: ignore[misc]


def test_the_limitations_state_what_this_object_is_not():
    joined = " ".join(LIMITATIONS).lower()
    assert "this is a dossier, not a score" in joined
    assert "required and pos-eligible are different properties" in joined
    assert "never zero" in joined


# -------------------------------------------- the union of both passes


async def test_the_dossier_is_derived_from_cheap_and_deep_evidence_together():
    """§1: Step 7 derives from the union, not from the deep pass alone."""
    from app.services.deep_collection import run_deep_collection
    from app.services.marketplace import run_marketplace_research
    from app.storage.memory import ResearchStore
    from tests.test_deep_collection import (
        TEN_LISTINGS,
        TEN_VIDEOS,
        FakeContent,
        FakeMarketplace,
    )

    store = ResearchStore()
    candidate = make_candidate()
    run_id = uuid4()
    marketplace = FakeMarketplace(TEN_LISTINGS)

    # Cheap pass: shallow.
    await run_marketplace_research(
        candidates=[candidate], provider=marketplace, store=store,
        research_run_id=run_id, max_listings_per_query=2,
    )
    cheap_only = build(
        candidate.id, run_id, store.evidence_for_candidate(candidate.id, run_id)
    )

    # Deep pass over the same run.
    deep = await run_deep_collection(
        candidates=[candidate], store=store, research_run_id=run_id,
        marketplace_provider=marketplace,
        public_content_provider=FakeContent(TEN_VIDEOS),
    )
    union = build(
        candidate.id,
        run_id,
        store.evidence_for_candidate(candidate.id, run_id),
        capabilities=deep.capabilities,
    )

    cheap_sample = dimension(cheap_only, EcsDimension.PURCHASE_PROXY_EVIDENCE)
    union_sample = dimension(union, EcsDimension.PURCHASE_PROXY_EVIDENCE)
    assert union_sample.sample_size > cheap_sample.sample_size
    assert len(union.evidence_ids) > len(cheap_only.evidence_ids)
    # Public content only exists after the deep pass.
    assert dimension(union, EcsDimension.AUDIENCE_ATTENTION).sample_size


async def test_a_full_pipeline_reaches_a_complete_dossier():
    from app.services.deep_collection import run_deep_collection
    from app.storage.memory import ResearchStore
    from tests.test_deep_collection import (
        TEN_LISTINGS,
        TEN_VIDEOS,
        FakeContent,
        FakeMarketplace,
    )

    store = ResearchStore()
    candidate = make_candidate()
    run_id = uuid4()
    deep = await run_deep_collection(
        candidates=[candidate], store=store, research_run_id=run_id,
        marketplace_provider=FakeMarketplace(TEN_LISTINGS),
        public_content_provider=FakeContent(TEN_VIDEOS),
    )
    evidence = list(store.evidence_for_candidate(candidate.id, run_id))
    # Search demand has no fake provider here, so supply its evidence directly
    # to complete the required set.
    evidence += [keyword_evidence(candidate.id, run_id, f"k{i}", 50 + i) for i in range(6)]
    result = build(
        candidate.id,
        run_id,
        evidence,
        capabilities=tuple(
            CapabilityOutcome(
                capability=o.capability, status=SnapshotStatus.COMPLETE.value
            )
            for o in deep.capabilities
        ),
    )
    assert result.state is DeepResearchState.COMPLETE
    assert result.missing_dimensions == ()
    assert result.evidence_confidence.state is EcsState.COMPUTED
