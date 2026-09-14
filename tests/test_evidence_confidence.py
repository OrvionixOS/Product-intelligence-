"""Milestone 6A-1 tests: Evidence Confidence inputs and `ecs_v1`.

No network, no live API calls, no new provider.

Four rules carry the milestone.

1. No ECS input ever takes an invented constant. Missing evidence that was
   EXPECTED lowers confidence; it never vanishes from the denominator, so ECS
   can never rise because something went missing.

2. Blocked is not low. A blocked result is `None`, never `0.0`, and may not be
   rendered, stored or compared as a zero.

3. ECS measures the evidence, never the opportunity. No component reads the
   magnitude of any observation.

4. `ecs_v1` is the unweighted mean specified in SPEC_STEP_7_8.md §6.1, and the
   result carries enough to recompute it by hand.
"""

import ast
import random
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from app.domain.enums import EvidencePurpose, TruthClass
from app.domain.models import EvidenceItem
from app.services.evidence_confidence import (
    COMPONENT_ORDER,
    DIMENSION_CAPABILITIES,
    ECS_VERSION,
    EXPECTED_SURFACES,
    FRESHNESS_WINDOWS,
    FRESHNESS_WINDOWS_VERSION,
    LIMITATIONS,
    NEVER_EXCLUDABLE,
    PROVENANCE_DIRECTNESS,
    PROVENANCE_DIRECTNESS_VERSION,
    REQUIRED_DIMENSIONS,
    SAMPLE_FLOORS,
    SAMPLE_FLOORS_VERSION,
    STATE_BOUNDARIES,
    Applicability,
    Capability,
    CapabilityHealth,
    CapabilityReport,
    ComponentName,
    DimensionObservation,
    DirectnessClass,
    EcsDimension,
    EcsState,
    capability_health,
    compute_evidence_confidence,
    directness_class,
    provenance_directness,
    record_capability,
)

MODULE_PATH = Path("app/services/evidence_confidence.py")
NOW = datetime(2026, 9, 14, tzinfo=UTC)


# ----------------------------------------------------------------- fixtures


def evidence_item(
    *,
    candidate_id: UUID,
    signal_type: str = "search_volume",
    purpose: EvidencePurpose = EvidencePurpose.SEARCH_DEMAND,
    collection_method: str = "official_api",
    retrieved_at: datetime | None = NOW,
    research_run_id: UUID | None = None,
) -> EvidenceItem:
    return EvidenceItem(
        candidate_id=candidate_id,
        research_run_id=research_run_id,
        provider="test-provider",
        platform="test",
        collection_method=collection_method,
        signal_type=signal_type,
        purpose=purpose,
        truth_class=TruthClass.OBSERVED,
        retrieved_at=retrieved_at,
    )


def healthy_dimensions(
    *, sample_size: int | None = 100, conflicts: int = 0, observations: int = 50
) -> list[DimensionObservation]:
    """Every required dimension scoreable, fully corroborated, no conflicts."""
    return [
        DimensionObservation(
            dimension=dimension,
            scoreable=True,
            sample_size=sample_size,
            distinct_surfaces=EXPECTED_SURFACES[dimension],
            observation_count=observations,
            conflicting_observation_count=conflicts,
        )
        for dimension in REQUIRED_DIMENSIONS
    ]


def healthy_capabilities(status: str = "COMPLETE") -> list[CapabilityReport]:
    return [CapabilityReport(capability=c, status=status) for c in Capability]


def one_record_per_capability(
    candidate_id: UUID, **kwargs
) -> list[EvidenceItem]:
    return [
        evidence_item(
            candidate_id=candidate_id,
            signal_type="search_volume",
            purpose=EvidencePurpose.SEARCH_DEMAND,
            **kwargs,
        ),
        evidence_item(
            candidate_id=candidate_id,
            signal_type="marketplace_listing_price",
            purpose=EvidencePurpose.PRICE,
            **kwargs,
        ),
        evidence_item(
            candidate_id=candidate_id,
            signal_type="public_video_view_count",
            purpose=EvidencePurpose.AUDIENCE,
            **kwargs,
        ),
    ]


def run(candidate_id, dimensions, capabilities, evidence, **kwargs):
    return compute_evidence_confidence(
        candidate_id=candidate_id,
        dimensions=dimensions,
        capabilities=capabilities,
        evidence=evidence,
        now=kwargs.pop("now", NOW),
        **kwargs,
    )


def component(result, name: ComponentName):
    return next(c for c in result.components if c.name is name)


# ------------------------------------------------------- the happy baseline


def test_complete_fresh_direct_evidence_scores_full_confidence():
    candidate_id = uuid4()
    result = run(
        candidate_id,
        healthy_dimensions(),
        healthy_capabilities(),
        one_record_per_capability(candidate_id),
    )
    assert result.state is EcsState.COMPUTED
    assert result.evidence_confidence == 100.0
    assert result.blocked_reason is None
    assert len(result.components) == 7
    assert all(c.applicability is Applicability.COUNTED for c in result.components)


def test_every_component_is_emitted_in_a_fixed_order():
    candidate_id = uuid4()
    result = run(
        candidate_id,
        healthy_dimensions(),
        healthy_capabilities(),
        one_record_per_capability(candidate_id),
    )
    assert tuple(c.name for c in result.components) == COMPONENT_ORDER


# ------------------------------------------------ the anti-inflation rule


def test_a_missing_timestamp_lowers_confidence_and_stays_in_the_denominator():
    """Case (b): expected but unavailable. It may never leave the mean."""
    candidate_id = uuid4()
    full = run(
        candidate_id,
        healthy_dimensions(),
        healthy_capabilities(),
        one_record_per_capability(candidate_id),
    )
    stale_free = run(
        candidate_id,
        healthy_dimensions(),
        healthy_capabilities(),
        one_record_per_capability(candidate_id, retrieved_at=None),
    )
    freshness = component(stale_free, ComponentName.FRESHNESS)
    assert freshness.score == 0.0
    assert freshness.applicability is Applicability.COUNTED
    assert freshness.units_counted == 3
    # Same denominator, lower numerator: confidence FELL.
    assert stale_free.counted_components == full.counted_components
    assert stale_free.evidence_confidence < full.evidence_confidence


def test_a_missing_sample_size_lowers_confidence_and_stays_in_the_denominator():
    candidate_id = uuid4()
    full = run(
        candidate_id,
        healthy_dimensions(),
        healthy_capabilities(),
        one_record_per_capability(candidate_id),
    )
    unsampled = run(
        candidate_id,
        healthy_dimensions(sample_size=None),
        healthy_capabilities(),
        one_record_per_capability(candidate_id),
    )
    sample = component(unsampled, ComponentName.SAMPLE_ADEQUACY)
    assert sample.score == 0.0
    assert sample.applicability is Applicability.COUNTED
    assert sample.units_counted == len(REQUIRED_DIMENSIONS)
    assert unsampled.counted_components == full.counted_components
    assert unsampled.evidence_confidence < full.evidence_confidence


def test_missing_evidence_can_never_raise_confidence():
    """The property the anti-inflation rule exists to guarantee."""
    candidate_id = uuid4()
    baseline = run(
        candidate_id,
        healthy_dimensions(),
        healthy_capabilities(),
        one_record_per_capability(candidate_id),
    )
    degradations = [
        run(
            candidate_id,
            healthy_dimensions(sample_size=None),
            healthy_capabilities(),
            one_record_per_capability(candidate_id),
        ),
        run(
            candidate_id,
            healthy_dimensions(),
            healthy_capabilities(),
            one_record_per_capability(candidate_id, retrieved_at=None),
        ),
        run(
            candidate_id,
            healthy_dimensions(),
            healthy_capabilities(),
            one_record_per_capability(candidate_id, collection_method="???"),
        ),
        run(
            candidate_id,
            healthy_dimensions(),
            healthy_capabilities("PARTIAL"),
            one_record_per_capability(candidate_id),
        ),
        run(
            candidate_id,
            healthy_dimensions(conflicts=25, observations=50),
            healthy_capabilities(),
            one_record_per_capability(candidate_id),
        ),
    ]
    for degraded in degradations:
        assert degraded.evidence_confidence < baseline.evidence_confidence


LEGACY_CONFIDENCE_FIELDS = frozenset(
    {
        "directness",
        "source_quality",
        "sample_adequacy",
        "freshness",
        "independence",
        "cross_source_agreement",
        "geographic_relevance",
        "marketplace_relevance",
    }
)


def test_the_legacy_per_item_confidence_defaults_still_exist_and_are_never_read():
    """`EvidenceItem` carries eight confidence floats with invented defaults.

    They are the exact failure this milestone forbids: `freshness` defaults to
    1.0, so a record with no timestamp at all would read as perfectly current,
    and `directness` defaults to 0.5, a number no observation produced. 6A-1
    derives every ECS input itself and must never touch them.

    The first half of this test proves the trap is still there; without it the
    second half would keep passing after someone removed the fields, and would
    then be guarding nothing.
    """
    probe = evidence_item(candidate_id=uuid4())
    assert LEGACY_CONFIDENCE_FIELDS <= set(type(probe).model_fields)
    assert probe.freshness == 1.0
    assert probe.directness == 0.5
    assert probe.source_quality == 0.5

    assert _referenced_attributes() & LEGACY_CONFIDENCE_FIELDS == set()


# ------------------------------------------------------- capability health


@pytest.mark.parametrize(
    "status,expected",
    [
        ("COMPLETE", CapabilityHealth.CLEAN),
        ("PARTIAL", CapabilityHealth.PARTIAL),
        ("FAILED", CapabilityHealth.FAILED),
        ("PROVIDER_FAILED", CapabilityHealth.FAILED),
        ("UNEXPECTED_PROVIDER_ERROR", CapabilityHealth.FAILED),
        ("NOT_REQUESTED", CapabilityHealth.NOT_ATTEMPTED),
        ("PENDING", CapabilityHealth.NOT_ATTEMPTED),
    ],
)
def test_orchestration_statuses_map_to_the_three_health_states(status, expected):
    assert capability_health(status) is expected


def test_an_unrecognised_status_is_failed_never_healthy():
    """A status this module cannot interpret is not a clean bill of health."""
    assert capability_health("SOMETHING_NEW") is CapabilityHealth.FAILED
    assert capability_health("") is CapabilityHealth.FAILED


def test_partial_and_failed_capabilities_lower_confidence_by_the_fixed_ordinal():
    candidate_id = uuid4()
    scores = {}
    for status in ("COMPLETE", "PARTIAL", "FAILED"):
        result = run(
            candidate_id,
            healthy_dimensions(),
            healthy_capabilities(status),
            one_record_per_capability(candidate_id),
        )
        scores[status] = component(result, ComponentName.CAPABILITY_HEALTH).score
    # The §6.1 ordinal, fixed normatively: 1.0 / 0.5 / 0.0.
    assert scores == {"COMPLETE": 1.0, "PARTIAL": 0.5, "FAILED": 0.0}


def test_a_mix_of_capability_outcomes_averages_them():
    candidate_id = uuid4()
    result = run(
        candidate_id,
        healthy_dimensions(),
        [
            CapabilityReport(Capability.SEARCH_DEMAND, "COMPLETE"),
            CapabilityReport(Capability.MARKETPLACE, "PARTIAL"),
            CapabilityReport(Capability.PUBLIC_CONTENT, "PROVIDER_FAILED"),
        ],
        one_record_per_capability(candidate_id),
    )
    health = component(result, ComponentName.CAPABILITY_HEALTH)
    assert health.score == pytest.approx((1.0 + 0.5 + 0.0) / 3)
    assert health.applicability is Applicability.COUNTED


def test_a_not_requested_capability_is_excluded_from_health_not_counted_as_failure():
    """Nobody asked for it, so 'how did it go' does not apply to it."""
    candidate_id = uuid4()
    result = run(
        candidate_id,
        healthy_dimensions(),
        [
            CapabilityReport(Capability.SEARCH_DEMAND, "COMPLETE"),
            CapabilityReport(Capability.MARKETPLACE, "COMPLETE"),
            CapabilityReport(Capability.PUBLIC_CONTENT, "NOT_REQUESTED"),
        ],
        one_record_per_capability(candidate_id),
    )
    health = component(result, ComponentName.CAPABILITY_HEALTH)
    assert health.score == 1.0, "a non-attempt must not read as a failure"
    assert health.units_counted == 2
    assert health.units_excluded == 1
    assert health.applicability is Applicability.COUNTED


def test_capability_health_is_never_excludable_even_when_nothing_was_attempted():
    """§6.1: it can never leave the mean. With no attempt, it scores 0.0."""
    candidate_id = uuid4()
    result = run(
        candidate_id,
        healthy_dimensions(),
        [CapabilityReport(c, "NOT_REQUESTED") for c in Capability],
        one_record_per_capability(candidate_id),
    )
    health = component(result, ComponentName.CAPABILITY_HEALTH)
    assert health.applicability is Applicability.COUNTED
    assert health.score == 0.0
    assert ComponentName.CAPABILITY_HEALTH in result.counted_components
    assert ComponentName.CAPABILITY_HEALTH not in result.not_applicable_components


# ------------------------------------------------------------ blocked ECS


def test_an_absent_capability_outcome_blocks_ecs_rather_than_scoring_zero():
    candidate_id = uuid4()
    result = run(
        candidate_id,
        healthy_dimensions(),
        # Public content never reported at all.
        [
            CapabilityReport(Capability.SEARCH_DEMAND, "COMPLETE"),
            CapabilityReport(Capability.MARKETPLACE, "COMPLETE"),
        ],
        one_record_per_capability(candidate_id),
    )
    assert result.state is EcsState.BLOCKED
    assert result.evidence_confidence is None
    assert result.evidence_confidence != 0.0
    assert "public_content" in result.blocked_reason
    assert result.components == ()
    assert "never be rendered" in result.state_boundary


def test_blocked_is_distinct_from_the_lowest_computable_confidence():
    """A floor-value ECS is a measurement; a blocked ECS is the absence of one."""
    candidate_id = uuid4()
    worst = run(
        candidate_id,
        [
            DimensionObservation(d, False, None, 0, 0, 0)
            for d in REQUIRED_DIMENSIONS
        ],
        healthy_capabilities("FAILED"),
        [],
    )
    assert worst.state is EcsState.COMPUTED
    assert worst.evidence_confidence == 0.0
    assert worst.evidence_confidence is not None

    blocked = run(candidate_id, healthy_dimensions(), [], [])
    assert blocked.state is EcsState.BLOCKED
    assert blocked.evidence_confidence is None
    assert worst.evidence_confidence != blocked.evidence_confidence


def test_every_state_says_what_it_does_not_establish():
    for state in EcsState:
        assert state in STATE_BOUNDARIES
        assert STATE_BOUNDARIES[state].strip()


# ----------------------------------------------------------------- scope


def test_evidence_for_another_candidate_is_refused_unread():
    mine, theirs = uuid4(), uuid4()
    result = run(
        mine,
        healthy_dimensions(),
        healthy_capabilities(),
        one_record_per_capability(mine) + one_record_per_capability(theirs),
    )
    assert result.state is EcsState.SCOPE_MISMATCH
    assert result.evidence_confidence is None
    assert result.provenance.evidence_ids == ()
    assert result.components == ()


def test_evidence_from_another_research_run_is_refused():
    candidate_id = uuid4()
    run_a, run_b = uuid4(), uuid4()
    result = run(
        candidate_id,
        healthy_dimensions(),
        healthy_capabilities(),
        one_record_per_capability(candidate_id, research_run_id=run_b),
        research_run_id=run_a,
    )
    assert result.state is EcsState.SCOPE_MISMATCH


def test_a_runless_record_is_refused_for_a_run_scoped_computation():
    candidate_id = uuid4()
    result = run(
        candidate_id,
        healthy_dimensions(),
        healthy_capabilities(),
        one_record_per_capability(candidate_id),
        research_run_id=uuid4(),
    )
    assert result.state is EcsState.SCOPE_MISMATCH


def test_matching_scope_is_consumed():
    candidate_id = uuid4()
    run_id = uuid4()
    result = run(
        candidate_id,
        healthy_dimensions(),
        healthy_capabilities(),
        one_record_per_capability(candidate_id, research_run_id=run_id),
        research_run_id=run_id,
    )
    assert result.state is EcsState.COMPUTED
    assert len(result.provenance.evidence_ids) == 3


# ------------------------------------------------------------ provenance


def test_direct_api_evidence_is_the_most_direct_provenance():
    assert directness_class("official_api") is DirectnessClass.DIRECT_API
    assert PROVENANCE_DIRECTNESS[DirectnessClass.DIRECT_API] == 1.00


def test_cache_provenance_is_recognised_and_scores_below_a_live_call():
    """A validated cache reuse is real provenance, just not a fresh call."""
    assert directness_class("cache") is DirectnessClass.VALIDATED_CACHE
    assert PROVENANCE_DIRECTNESS[DirectnessClass.VALIDATED_CACHE] == 0.90

    candidate_id = uuid4()
    live = run(
        candidate_id,
        healthy_dimensions(),
        healthy_capabilities(),
        one_record_per_capability(candidate_id, collection_method="official_api"),
    )
    cached = run(
        candidate_id,
        healthy_dimensions(),
        healthy_capabilities(),
        one_record_per_capability(candidate_id, collection_method="cache"),
    )
    assert component(live, ComponentName.PROVENANCE_DIRECTNESS).score == 1.00
    assert component(cached, ComponentName.PROVENANCE_DIRECTNESS).score == 0.90
    assert cached.evidence_confidence < live.evidence_confidence


@pytest.mark.parametrize("method", ["scraped", "", "OFFICIAL_API", "guessed"])
def test_an_unknown_collection_method_is_the_weakest_provenance_never_a_waiver(method):
    candidate_id = uuid4()
    assert directness_class(method) is DirectnessClass.UNKNOWN
    result = run(
        candidate_id,
        healthy_dimensions(),
        healthy_capabilities(),
        one_record_per_capability(candidate_id, collection_method=method),
    )
    directness = component(result, ComponentName.PROVENANCE_DIRECTNESS)
    assert directness.score == 0.00
    # Stays in the denominator: unknown provenance is not an exemption.
    assert directness.applicability is Applicability.COUNTED
    assert directness.units_counted == 3


def test_a_missing_collection_method_is_unknown_not_absent():
    assert directness_class(None) is DirectnessClass.UNKNOWN
    assert provenance_directness("anything", EvidencePurpose.SEARCH_DEMAND, None) == 0.0


def test_the_v1_directness_table_is_degenerate_in_signal_and_purpose():
    """The approved V1 values are keyed by collection method alone."""
    for signal in ("search_volume", "marketplace_listing_price", "public_x"):
        for purpose in EvidencePurpose:
            assert provenance_directness(signal, purpose, "official_api") == 1.00
            assert provenance_directness(signal, purpose, "cache") == 0.90


def test_the_forward_looking_directness_classes_are_declared_but_unproduced():
    """Two approved classes have no current collection method.

    `provenance_directness_v1` declares five classes. Only two are reachable
    from production today, because only `official_api` and `cache` are emitted.
    Recording that here keeps the gap visible instead of letting the table look
    fully exercised.
    """
    assert PROVENANCE_DIRECTNESS[DirectnessClass.DETERMINISTIC_DERIVATION] == 0.80
    assert PROVENANCE_DIRECTNESS[DirectnessClass.INDIRECT_PROVIDER_MEDIATED] == 0.60
    reachable = {
        directness_class(m) for m in ("official_api", "cache", "anything-else", None)
    }
    assert DirectnessClass.DETERMINISTIC_DERIVATION not in reachable
    assert DirectnessClass.INDIRECT_PROVIDER_MEDIATED not in reachable
    assert set(PROVENANCE_DIRECTNESS) == set(DirectnessClass)


# ------------------------------------------------------------- freshness


@pytest.mark.parametrize("capability", list(Capability))
def test_freshness_decays_across_its_own_declared_window(capability):
    signal = {
        Capability.SEARCH_DEMAND: "search_volume",
        Capability.MARKETPLACE: "marketplace_listing_price",
        Capability.PUBLIC_CONTENT: "public_video_view_count",
    }[capability]
    purpose = {
        Capability.SEARCH_DEMAND: EvidencePurpose.SEARCH_DEMAND,
        Capability.MARKETPLACE: EvidencePurpose.PRICE,
        Capability.PUBLIC_CONTENT: EvidencePurpose.AUDIENCE,
    }[capability]
    window = FRESHNESS_WINDOWS[capability]
    candidate_id = uuid4()

    def freshness_for(age: timedelta) -> float:
        result = run(
            candidate_id,
            healthy_dimensions(),
            healthy_capabilities(),
            [
                evidence_item(
                    candidate_id=candidate_id,
                    signal_type=signal,
                    purpose=purpose,
                    retrieved_at=NOW - age,
                )
            ],
        )
        return component(result, ComponentName.FRESHNESS).score

    assert freshness_for(timedelta(0)) == 1.0
    assert freshness_for(window / 2) == pytest.approx(0.5)
    assert freshness_for(window) == 0.0
    assert freshness_for(window * 3) == 0.0


def test_freshness_reads_retrieval_not_storage_time():
    """`collected_at` is when we stored it, not when it was observed."""
    candidate_id = uuid4()
    stored_now_but_never_retrieved = EvidenceItem(
        candidate_id=candidate_id,
        research_run_id=None,
        provider="p",
        collection_method="official_api",
        signal_type="search_volume",
        purpose=EvidencePurpose.SEARCH_DEMAND,
        truth_class=TruthClass.OBSERVED,
        collected_at=NOW,
        retrieved_at=None,
    )
    result = run(
        candidate_id,
        healthy_dimensions(),
        healthy_capabilities(),
        [stored_now_but_never_retrieved],
    )
    # Storage time must not masquerade as evidence recency.
    assert component(result, ComponentName.FRESHNESS).score == 0.0


def test_an_unrecognised_signal_type_has_no_window_and_scores_zero():
    candidate_id = uuid4()
    assert record_capability(
        evidence_item(candidate_id=candidate_id, signal_type="mystery_signal")
    ) is None
    result = run(
        candidate_id,
        healthy_dimensions(),
        healthy_capabilities(),
        [evidence_item(candidate_id=candidate_id, signal_type="mystery_signal")],
    )
    freshness = component(result, ComponentName.FRESHNESS)
    assert freshness.score == 0.0
    assert freshness.applicability is Applicability.COUNTED


# --------------------------------------------------------------- conflicts


def test_conflicting_observations_lower_confidence_in_proportion():
    candidate_id = uuid4()
    clean = run(
        candidate_id,
        healthy_dimensions(observations=100, conflicts=0),
        healthy_capabilities(),
        one_record_per_capability(candidate_id),
    )
    contested = run(
        candidate_id,
        healthy_dimensions(observations=100, conflicts=40),
        healthy_capabilities(),
        one_record_per_capability(candidate_id),
    )
    assert component(clean, ComponentName.CONFLICT_RATE).score == 1.0
    assert component(contested, ComponentName.CONFLICT_RATE).score == pytest.approx(0.6)
    assert contested.evidence_confidence < clean.evidence_confidence


def test_total_conflict_is_zero_not_negative():
    candidate_id = uuid4()
    result = run(
        candidate_id,
        healthy_dimensions(observations=10, conflicts=10),
        healthy_capabilities(),
        one_record_per_capability(candidate_id),
    )
    assert component(result, ComponentName.CONFLICT_RATE).score == 0.0


def test_a_dimension_with_no_observations_leaves_the_conflict_average():
    """Case (a) inside the component: there is nothing to agree or disagree."""
    candidate_id = uuid4()
    dimensions = [
        DimensionObservation(d, True, 100, EXPECTED_SURFACES[d], 0, 0)
        for d in REQUIRED_DIMENSIONS
    ]
    result = run(
        candidate_id, dimensions, healthy_capabilities(),
        one_record_per_capability(candidate_id),
    )
    conflict = component(result, ComponentName.CONFLICT_RATE)
    assert conflict.applicability is Applicability.NOT_APPLICABLE
    assert conflict.score is None
    assert conflict.exclusion_clause
    assert ComponentName.CONFLICT_RATE in result.not_applicable_components
    assert ComponentName.CONFLICT_RATE not in result.counted_components


# ------------------------------------------------------------- coverage


def test_dimension_coverage_counts_only_required_dimensions():
    candidate_id = uuid4()
    assert EcsDimension.PRICE_EVIDENCE not in REQUIRED_DIMENSIONS
    partial = [
        DimensionObservation(
            d, d is not EcsDimension.CHANNEL_REACH, 100, EXPECTED_SURFACES[d], 50, 0
        )
        for d in REQUIRED_DIMENSIONS
    ]
    result = run(
        candidate_id, partial, healthy_capabilities(),
        one_record_per_capability(candidate_id),
    )
    coverage = component(result, ComponentName.DIMENSION_COVERAGE)
    assert coverage.score == pytest.approx(4 / 5)
    assert coverage.units_counted == 5


def test_an_optional_dimension_never_changes_coverage():
    candidate_id = uuid4()
    without = run(
        candidate_id, healthy_dimensions(), healthy_capabilities(),
        one_record_per_capability(candidate_id),
    )
    with_optional = run(
        candidate_id,
        healthy_dimensions()
        + [
            DimensionObservation(
                EcsDimension.PRICE_EVIDENCE, False, None, 0, 0, 0
            )
        ],
        healthy_capabilities(),
        one_record_per_capability(candidate_id),
    )
    assert (
        component(with_optional, ComponentName.DIMENSION_COVERAGE).score
        == component(without, ComponentName.DIMENSION_COVERAGE).score
    )


def test_dimension_coverage_is_never_excludable():
    candidate_id = uuid4()
    result = run(candidate_id, [], healthy_capabilities(), [])
    coverage = component(result, ComponentName.DIMENSION_COVERAGE)
    assert coverage.applicability is Applicability.COUNTED
    assert coverage.score == 0.0
    assert NEVER_EXCLUDABLE == {
        ComponentName.DIMENSION_COVERAGE,
        ComponentName.CAPABILITY_HEALTH,
    }


# --------------------------------------------------- sample and breadth


def test_sample_adequacy_saturates_at_the_declared_floor():
    candidate_id = uuid4()
    for multiple, expected in ((1, 1.0), (10, 1.0)):
        result = run(
            candidate_id,
            [
                DimensionObservation(
                    EcsDimension.SEARCH_DEMAND,
                    True,
                    SAMPLE_FLOORS[Capability.SEARCH_DEMAND] * multiple,
                    1,
                    10,
                    0,
                )
            ],
            healthy_capabilities(),
            one_record_per_capability(candidate_id),
        )
        assert component(result, ComponentName.SAMPLE_ADEQUACY).score == expected


def test_a_thin_sample_scores_proportionally_below_the_floor():
    candidate_id = uuid4()
    floor = SAMPLE_FLOORS[Capability.MARKETPLACE]
    result = run(
        candidate_id,
        [
            DimensionObservation(
                EcsDimension.PURCHASE_PROXY_EVIDENCE, True, floor // 2, 1, 10, 0
            )
        ],
        healthy_capabilities(),
        one_record_per_capability(candidate_id),
    )
    assert component(result, ComponentName.SAMPLE_ADEQUACY).score == pytest.approx(0.5)


def test_corroboration_breadth_uses_the_expected_surfaces_from_the_contract():
    """D6 expects three surfaces; the others expect one. Never a free parameter."""
    assert EXPECTED_SURFACES[EcsDimension.CHANNEL_REACH] == 3
    assert EXPECTED_SURFACES[EcsDimension.SEARCH_DEMAND] == 1
    assert all(
        EXPECTED_SURFACES[d] == len(DIMENSION_CAPABILITIES[d]) for d in EcsDimension
    )

    candidate_id = uuid4()
    result = run(
        candidate_id,
        [DimensionObservation(EcsDimension.CHANNEL_REACH, True, 100, 1, 10, 0)],
        healthy_capabilities(),
        one_record_per_capability(candidate_id),
    )
    # One surface of three expected.
    assert component(result, ComponentName.CORROBORATION_BREADTH).score == pytest.approx(
        1 / 3
    )


def test_extra_surfaces_do_not_push_breadth_above_one():
    candidate_id = uuid4()
    result = run(
        candidate_id,
        [DimensionObservation(EcsDimension.SEARCH_DEMAND, True, 100, 9, 10, 0)],
        healthy_capabilities(),
        one_record_per_capability(candidate_id),
    )
    assert component(result, ComponentName.CORROBORATION_BREADTH).score == 1.0


# ------------------------------------------------------------ aggregation


def test_the_aggregate_is_the_unweighted_mean_of_the_counted_components():
    """Recomputable by hand from the emitted components alone."""
    candidate_id = uuid4()
    result = run(
        candidate_id,
        healthy_dimensions(sample_size=None, conflicts=10, observations=100),
        healthy_capabilities("PARTIAL"),
        one_record_per_capability(candidate_id, collection_method="cache"),
    )
    counted = [
        c for c in result.components if c.applicability is Applicability.COUNTED
    ]
    by_hand = round(100 * sum(c.score for c in counted) / len(counted), 2)
    assert result.evidence_confidence == by_hand
    assert tuple(c.name for c in counted) == result.counted_components


def test_no_weight_is_defined_or_applied():
    """Prose may discuss weighting; no executed symbol may implement it."""
    offenders = sorted(s for s in _code_symbols() if "weight" in s.lower())
    assert offenders == []
    # And the mean is provably unweighted: permuting which component holds
    # which score cannot change the aggregate.
    candidate_id = uuid4()
    result = run(
        candidate_id,
        healthy_dimensions(sample_size=None),
        healthy_capabilities("PARTIAL"),
        one_record_per_capability(candidate_id, collection_method="cache"),
    )
    counted = [c.score for c in result.components if c.score is not None]
    rng = random.Random(6100)
    for _ in range(50):
        shuffled = list(counted)
        rng.shuffle(shuffled)
        assert round(100 * sum(shuffled) / len(shuffled), 2) == (
            result.evidence_confidence
        )


def test_confidence_is_bounded_and_rounded_to_two_places():
    candidate_id = uuid4()
    rng = random.Random(2026)
    for _ in range(200):
        dimensions = [
            DimensionObservation(
                d,
                rng.random() > 0.3,
                rng.choice([None, 0, 1, 7, 50]),
                rng.randint(0, 4),
                rng.randint(0, 40),
                rng.randint(0, 40),
            )
            for d in REQUIRED_DIMENSIONS
        ]
        result = run(
            candidate_id,
            dimensions,
            [
                CapabilityReport(
                    c, rng.choice(["COMPLETE", "PARTIAL", "FAILED", "NOT_REQUESTED"])
                )
                for c in Capability
            ],
            one_record_per_capability(
                candidate_id,
                collection_method=rng.choice(["official_api", "cache", "???"]),
                retrieved_at=rng.choice([NOW, NOW - timedelta(days=99), None]),
            ),
        )
        assert result.state is EcsState.COMPUTED
        assert 0.0 <= result.evidence_confidence <= 100.0
        assert round(result.evidence_confidence, 2) == result.evidence_confidence


# ------------------------------------------------------------ determinism


def _identity(result) -> tuple:
    return (
        result.state,
        result.evidence_confidence,
        result.blocked_reason,
        result.counted_components,
        result.not_applicable_components,
        tuple(
            (c.name, c.score, c.applicability, c.exclusion_clause,
             c.units_counted, c.units_excluded)
            for c in result.components
        ),
        result.provenance.evidence_ids,
        result.provenance.capabilities,
        result.provenance.dimensions,
    )


def test_output_is_identical_under_500_input_order_permutations():
    candidate_id = uuid4()
    dimensions = healthy_dimensions(sample_size=7, conflicts=3, observations=20)
    capabilities = [
        CapabilityReport(Capability.SEARCH_DEMAND, "COMPLETE"),
        CapabilityReport(Capability.MARKETPLACE, "PARTIAL"),
        CapabilityReport(Capability.PUBLIC_CONTENT, "NOT_REQUESTED"),
    ]
    evidence = one_record_per_capability(candidate_id) + one_record_per_capability(
        candidate_id, collection_method="cache", retrieved_at=NOW - timedelta(days=3)
    )
    baseline = _identity(run(candidate_id, dimensions, capabilities, evidence))
    rng = random.Random(20260914)
    for _ in range(500):
        d, c, e = list(dimensions), list(capabilities), list(evidence)
        rng.shuffle(d)
        rng.shuffle(c)
        rng.shuffle(e)
        assert _identity(run(candidate_id, d, c, e)) == baseline


def test_reversed_input_is_identical():
    candidate_id = uuid4()
    dimensions = healthy_dimensions(sample_size=12)
    capabilities = healthy_capabilities()
    evidence = one_record_per_capability(candidate_id)
    assert _identity(run(candidate_id, dimensions, capabilities, evidence)) == _identity(
        run(
            candidate_id,
            list(reversed(dimensions)),
            list(reversed(capabilities)),
            list(reversed(evidence)),
        )
    )


def test_the_same_inputs_always_produce_the_same_number():
    candidate_id = uuid4()
    args = (
        healthy_dimensions(sample_size=3, conflicts=1, observations=9),
        healthy_capabilities("PARTIAL"),
        one_record_per_capability(candidate_id, collection_method="cache"),
    )
    first = run(candidate_id, *args)
    for _ in range(20):
        assert run(candidate_id, *args).evidence_confidence == (
            first.evidence_confidence
        )


# ----------------------------------------------------------- reachability


def _sweep_results():
    """Every distinguishable outcome this module can reach from real inputs."""
    candidate_id = uuid4()
    other_id = uuid4()
    yield run(
        candidate_id, healthy_dimensions(), healthy_capabilities(),
        one_record_per_capability(candidate_id),
    )
    yield run(
        candidate_id,
        [
            DimensionObservation(d, True, 1, 0, 0, 0)
            for d in REQUIRED_DIMENSIONS
        ],
        [
            CapabilityReport(Capability.SEARCH_DEMAND, "PARTIAL"),
            CapabilityReport(Capability.MARKETPLACE, "FAILED"),
            CapabilityReport(Capability.PUBLIC_CONTENT, "NOT_REQUESTED"),
        ],
        one_record_per_capability(
            candidate_id, collection_method="cache", retrieved_at=None
        ),
    )
    yield run(candidate_id, healthy_dimensions(), [], [])
    yield run(
        candidate_id, healthy_dimensions(), healthy_capabilities(),
        one_record_per_capability(other_id),
    )


def test_every_ecs_state_is_reachable_from_real_input():
    observed = {result.state for result in _sweep_results()}
    assert observed == set(EcsState)


def test_every_applicability_is_reachable_from_real_input():
    observed = {
        c.applicability for result in _sweep_results() for c in result.components
    }
    assert observed == set(Applicability)


def test_every_capability_health_is_reachable_from_a_real_status():
    from app.services.research_orchestration import (
        STATUS_NOT_REQUESTED,
        STATUS_PROVIDER_FAILED,
        STATUS_UNEXPECTED_PROVIDER_ERROR,
    )
    from app.domain.enums import SnapshotStatus

    real_statuses = [s.value for s in SnapshotStatus] + [
        STATUS_NOT_REQUESTED,
        STATUS_PROVIDER_FAILED,
        STATUS_UNEXPECTED_PROVIDER_ERROR,
    ]
    observed = {capability_health(status) for status in real_statuses}
    assert observed == set(CapabilityHealth), (
        "a health state no orchestration status can produce is dead code"
    )


def test_every_component_is_reachable_and_the_set_is_closed_at_seven():
    emitted = {c.name for result in _sweep_results() for c in result.components}
    assert emitted == set(ComponentName) == set(COMPONENT_ORDER)
    assert len(COMPONENT_ORDER) == 7


def test_every_declared_dimension_has_a_capability_mapping():
    assert set(DIMENSION_CAPABILITIES) == set(EcsDimension)
    assert set(REQUIRED_DIMENSIONS) < set(EcsDimension), "D3 is optional"
    for capabilities in DIMENSION_CAPABILITIES.values():
        assert capabilities
        assert set(capabilities) <= set(Capability)


# ------------------------------------------------------ module boundaries


def _module_tree() -> ast.Module:
    return ast.parse(MODULE_PATH.read_text(encoding="utf-8"))


def _docstring_ids(tree: ast.Module) -> set[int]:
    holders = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    found = set()
    for node in ast.walk(tree):
        if not isinstance(node, holders):
            continue
        first = node.body[0] if node.body else None
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            found.add(id(first.value))
    return found


def _referenced_attributes() -> set[str]:
    """Every attribute the module actually reads or writes."""
    return {
        node.attr
        for node in ast.walk(_module_tree())
        if isinstance(node, ast.Attribute)
    }


def _code_symbols() -> set[str]:
    """Identifiers the module executes, plus its identifier-shaped literals.

    Prose is deliberately excluded — docstrings, comments, and the
    human-readable LIMITATIONS / STATE_BOUNDARIES strings. A guard that scans
    raw file text fires on prose that NAMES a forbidden concept in order to
    refuse it, which is the opposite of what it is for: this module's header
    says "no POS, no weights, no RED/YELLOW/GREEN", and a text scan reads that
    refusal as the violation. Only executed symbols are evidence of a reach.
    """
    tree = _module_tree()
    prose = _docstring_ids(tree)
    identifier = re.compile(r"[A-Za-z_][A-Za-z0-9_.]*")
    symbols: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            symbols.add(node.id)
        elif isinstance(node, ast.Attribute):
            symbols.add(node.attr)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            symbols.add(node.name)
        elif isinstance(node, ast.arg):
            symbols.add(node.arg)
        elif isinstance(node, ast.keyword) and node.arg:
            symbols.add(node.arg)
        elif isinstance(node, ast.Import):
            symbols.update(alias.asname or alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            symbols.add(node.module or "")
            symbols.update(alias.asname or alias.name for alias in node.names)
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in prose
            and identifier.fullmatch(node.value)
        ):
            symbols.add(node.value)
    return symbols


def test_the_symbol_scanner_sees_what_the_module_really_references():
    """The guards below are only worth their green if this holds."""
    symbols = _code_symbols()
    assert {"compute_evidence_confidence", "PROVENANCE_DIRECTNESS"} <= symbols
    assert "official_api" in symbols, "identifier-shaped literals are in scope"
    assert "retrieved_at" in _referenced_attributes()
    # Prose is out of scope, and the module's own refusals prove it.
    header = MODULE_PATH.read_text(encoding="utf-8")
    assert "RED/YELLOW/GREEN" in header, "the refusal text is still there"
    assert "opportunity" in header.lower()
    assert "RED" not in symbols
    assert "opportunity" not in symbols


def test_module_is_provider_free_and_service_layer_only():
    forbidden = (
        "httpx", "requests", "aiohttp", "urllib", "socket", "sqlalchemy",
        "psycopg", "asyncpg", "app.providers", "app.repositories", "app.api",
        "app.main", "openai", "anthropic", "app.storage",
    )
    for node in ast.walk(_module_tree()):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not any(alias.name.startswith(f) for f in forbidden), alias.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            assert not any(node.module.startswith(f) for f in forbidden), node.module


def test_module_does_not_reach_scoring_machinery():
    symbols = _code_symbols()
    for forbidden in (
        "app.services.scoring", "scoring", "score_opportunity", "Classification",
        "opportunity_score", "ScoreDimensions", "CONFIDENCE_WEIGHTS",
        "RED", "YELLOW", "GREEN",
    ):
        assert forbidden not in symbols, forbidden


def test_module_never_reads_an_observation_magnitude():
    """ECS measures the evidence, never the opportunity (§7).

    The split is the whole point of the component set: ECS may count records,
    surfaces and conflicts, but may never look at what any observation SAYS.
    """
    magnitudes = {
        "raw_value", "normalized_value", "raw_payload", "unit",
        "view_count", "review_count", "listing_price", "search_volume",
    }
    # No value-bearing field is ever read off a record.
    assert _referenced_attributes() & magnitudes == set()

    # Naming a signal type is not reading it: `record_capability` matches the
    # literal "search_volume" to route a record to its capability, and never
    # looks at the volume. Only an attribute access could read the magnitude,
    # so the literal is exempt and the attribute is not.
    assert "search_volume" in _code_symbols()
    assert "search_volume" not in _referenced_attributes()
    assert _code_symbols() & (magnitudes - {"search_volume"}) == set()


def test_no_pos_component_is_computed_here():
    named = {"opportunity_score", "classification", "kill_rule", "pos"}
    offenders = sorted(
        s
        for s in _code_symbols()
        if s.lower() in named or s.lower().startswith("pos_")
    )
    assert offenders == []


def test_no_endpoint_was_added_and_score_remains_gone():
    from fastapi.testclient import TestClient

    from app.main import app

    paths = {route.path for route in app.routes if hasattr(route, "methods")}
    assert len(paths) == 15
    assert TestClient(app).post("/score", json={}).status_code == 410


def test_6a1_is_not_wired_into_any_route():
    import app.api.routes as routes

    assert "evidence_confidence" not in Path(routes.__file__).read_text(
        encoding="utf-8"
    )


def test_legacy_scoring_remains_quarantined():
    from app.services.scoring import SCORING_STATUS

    assert SCORING_STATUS == "UNAPPROVED_EXPERIMENTAL"
    text = MODULE_PATH.read_text(encoding="utf-8")
    assert "CONFIDENCE_WEIGHTS" not in text


# ------------------------------------------------------- policy contract


def test_the_three_policy_sets_are_named_and_versioned():
    assert SAMPLE_FLOORS_VERSION == "sample_floors_v1"
    assert FRESHNESS_WINDOWS_VERSION == "freshness_windows_v1"
    assert PROVENANCE_DIRECTNESS_VERSION == "provenance_directness_v1"
    assert ECS_VERSION == "ecs_v1"


def test_the_approved_v1_policy_values_are_exactly_as_specified():
    assert SAMPLE_FLOORS == {
        Capability.SEARCH_DEMAND: 5,
        Capability.MARKETPLACE: 10,
        Capability.PUBLIC_CONTENT: 10,
    }
    assert FRESHNESS_WINDOWS == {
        Capability.SEARCH_DEMAND: timedelta(days=30),
        Capability.MARKETPLACE: timedelta(days=7),
        Capability.PUBLIC_CONTENT: timedelta(days=7),
    }
    assert PROVENANCE_DIRECTNESS == {
        DirectnessClass.DIRECT_API: 1.00,
        DirectnessClass.VALIDATED_CACHE: 0.90,
        DirectnessClass.DETERMINISTIC_DERIVATION: 0.80,
        DirectnessClass.INDIRECT_PROVIDER_MEDIATED: 0.60,
        DirectnessClass.UNKNOWN: 0.00,
    }


def test_every_result_carries_its_policy_versions():
    candidate_id = uuid4()
    result = run(
        candidate_id, healthy_dimensions(), healthy_capabilities(),
        one_record_per_capability(candidate_id),
    )
    assert result.version == ECS_VERSION
    assert result.sample_floors_version == SAMPLE_FLOORS_VERSION
    assert result.freshness_windows_version == FRESHNESS_WINDOWS_VERSION
    assert result.provenance_directness_version == PROVENANCE_DIRECTNESS_VERSION


def test_each_policy_table_is_annotated_uncalibrated():
    """Every approved-but-unvalidated table says so where it is defined."""
    lines = MODULE_PATH.read_text(encoding="utf-8").splitlines()
    for anchor in ("SAMPLE_FLOORS:", "FRESHNESS_WINDOWS:", "PROVENANCE_DIRECTNESS:"):
        index = next(i for i, line in enumerate(lines) if line.startswith(anchor))
        preamble = " ".join(lines[max(0, index - 6) : index])
        assert "UNCALIBRATED" in preamble, anchor


def test_the_policy_values_are_never_described_as_validated():
    """No unhedged validation claim, and the hedges are negation-aware.

    Word boundaries matter here: `PROVENANCE_DIRECTNESS` contains "proven",
    and a substring scan reads the module's own vocabulary as a claim.
    """
    text = MODULE_PATH.read_text(encoding="utf-8")
    claims = (
        r"empirically\s+validated",
        r"\bproven\b",
        r"validated\s+against",
        r"calibrated\s+against",
        r"\bstatistically\b",
        r"\bbenchmarked\b",
    )
    for claim in claims:
        for match in re.finditer(claim, text, re.IGNORECASE):
            window = text[max(0, match.start() - 80) : match.start()]
            assert re.search(
                r"\b(not|never|no|none|without|un\w+)\b", window, re.IGNORECASE
            ), f"unhedged claim: {text[match.start() - 80 : match.end() + 20]!r}"

    # The scanner must be able to fail, or its green means nothing.
    fake = "These floors were empirically validated against 2024 outcomes."
    assert re.search(claims[0], fake, re.IGNORECASE)
    assert not re.search(
        r"\b(not|never|no|none|without|un\w+)\b",
        fake[: re.search(claims[0], fake, re.IGNORECASE).start()],
        re.IGNORECASE,
    )


def test_limitations_state_what_ecs_is_not():
    joined = " ".join(LIMITATIONS).lower()
    assert "never a judgement about the opportunity" in joined
    assert "unvalidated v1 policy" in joined
    assert "not empirically validated" in joined
    assert "never a confidence of" in joined
