"""Milestone 5A: robust creator-relative outlier evidence.

These tests use hand-built immutable evidence only. No provider, network, or
persistence path is involved in the derivation.
"""

import ast
import math
import random
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from app.domain.enums import EvidencePurpose, TruthClass
from app.domain.models import EvidenceItem
from app.services.faceless_content_intelligence import (
    DIM_ROBUST_CONTENT_OUTLIER,
    FACELESS_CONTENT_INTELLIGENCE_VERSION,
    MIN_CREATOR_SAMPLE,
    ROBUST_CONTENT_OUTLIER_VERSION,
    RobustOutlierObservationState,
    derive_robust_content_intelligence,
)
from app.services.preliminary_dimensions import DimensionState


CANDIDATE = UUID("00000000-0000-0000-0000-000000000001")
OTHER_CANDIDATE = UUID("00000000-0000-0000-0000-000000000002")
RUN = UUID("10000000-0000-0000-0000-000000000001")
OTHER_RUN = UUID("20000000-0000-0000-0000-000000000001")


def make_view_evidence(
    *,
    candidate_id: UUID = CANDIDATE,
    research_run_id: UUID | None = RUN,
    video_id: str | None = "video-1",
    channel_id: str | None = "channel-1",
    views: int | float | str | None = 100,
    truth_class: TruthClass = TruthClass.OBSERVED,
    evidence_id: UUID | None = None,
    payload_hash: str | None = None,
    payload_overrides: dict | None = None,
    purpose: EvidencePurpose = EvidencePurpose.AUDIENCE,
    signal_type: str = "public_video_view_count",
) -> EvidenceItem:
    payload = {
        "video_id": video_id,
        "channel_id": channel_id,
        # Deliberately present to prove the derivation uses raw_value and the
        # evidence truth class, not a payload value.
        "view_count": views,
        "published_at": (datetime.now(UTC) - timedelta(days=10)).isoformat(),
        "channel_subscriber_count": 999999,
        "channel_view_count": 888888,
        "channel_video_count": 777,
    }
    if payload_overrides:
        payload.update(payload_overrides)
    return EvidenceItem(
        id=evidence_id or uuid4(),
        candidate_id=candidate_id,
        research_run_id=research_run_id,
        snapshot_id=uuid4(),
        signal_type=signal_type,
        purpose=purpose,
        truth_class=truth_class,
        provider="fake-content",
        collection_method="official_api",
        source_reference="/fake/public-content",
        platform="youtube",
        raw_value=views,
        raw_payload=payload,
        raw_payload_hash=payload_hash,
    )


def result_for(values: list[int], *, channel_id: str = "channel-1"):
    evidence = [
        make_view_evidence(
            video_id=f"video-{index}",
            channel_id=channel_id,
            views=value,
            payload_hash=f"hash-{index}",
        )
        for index, value in enumerate(values, start=1)
    ]
    return derive_robust_content_intelligence(CANDIDATE, evidence, RUN)


# ---------------------------------------------------------------------------
# contract and formula


def test_has_separate_version_and_dimension_name():
    assert ROBUST_CONTENT_OUTLIER_VERSION == "robust_content_outlier_v1"
    assert FACELESS_CONTENT_INTELLIGENCE_VERSION == "faceless_content_intelligence_v1"
    assert DIM_ROBUST_CONTENT_OUTLIER == "preliminary_robust_content_outlier"
    assert ROBUST_CONTENT_OUTLIER_VERSION != "content_outlier_v1"


def test_median_creator_baseline_and_exact_log2_relative_score():
    result = result_for([100, 200, 1000])

    assert result.state == DimensionState.EVIDENCE_PRESENT_UNSCORED
    assert result.value is None
    assert result.creator_baselines[0].median_views == 200.0
    assert result.creator_baselines[0].sample_size == MIN_CREATOR_SAMPLE
    assert result.creators_with_baseline == 1
    assert result.observed_video_count == 3
    assert result.scoreable_observation_count == 3

    by_video = {observation.video_id: observation for observation in result.observations}
    assert by_video["video-1"].relative_score == pytest.approx(math.log2(100 / 200))
    assert by_video["video-2"].relative_score == pytest.approx(0.0)
    assert by_video["video-3"].relative_score == pytest.approx(math.log2(1000 / 200))
    assert all(
        observation.formula_version == ROBUST_CONTENT_OUTLIER_VERSION
        for observation in result.observations
    )


def test_score_is_relative_content_performance_not_a_candidate_score():
    result = result_for([100, 200, 1000])
    assert result.value is None
    assert result.dimension_name == DIM_ROBUST_CONTENT_OUTLIER
    assert result.scoreable_observation_count == 3
    assert all(observation.relative_score is not None for observation in result.observations)


def test_minimum_creator_sample_is_three_observed_videos():
    result = result_for([100, 200])

    assert result.creator_baselines[0].sample_size == 2
    assert result.creator_baselines[0].state == "BASELINE_UNAVAILABLE"
    assert result.creators_with_baseline == 0
    assert all(
        observation.state == RobustOutlierObservationState.BASELINE_UNAVAILABLE
        for observation in result.observations
    )
    assert all(observation.relative_score is None for observation in result.observations)


def test_observed_zero_counts_as_sample_but_zero_median_is_non_scoreable():
    result = result_for([0, 0, 0])

    assert result.observed_video_count == 3
    assert result.creator_baselines[0].median_views == 0.0
    assert result.creator_baselines[0].state == "BASELINE_ZERO"
    assert result.zero_baseline_creator_count == 1
    assert all(
        observation.state == RobustOutlierObservationState.BASELINE_ZERO
        for observation in result.observations
    )
    assert all(observation.relative_score is None for observation in result.observations)
    assert result.state == DimensionState.EVIDENCE_PRESENT_UNSCORED


def test_zero_video_count_is_not_fabricated_into_a_score_against_positive_baseline():
    result = result_for([0, 100, 100])
    zero = next(observation for observation in result.observations if observation.view_count == 0)

    assert zero.state == RobustOutlierObservationState.VIDEO_VIEW_COUNT_ZERO
    assert zero.relative_score is None
    assert zero.source_truth_class == TruthClass.OBSERVED
    assert result.creator_baselines[0].median_views == 100.0
    assert result.scoreable_observation_count == 2


# ---------------------------------------------------------------------------
# truth and identity boundaries


def test_unknown_view_does_not_become_zero_or_enter_baseline():
    evidence = [
        make_view_evidence(video_id="known-1", channel_id="channel-1", views=100, payload_hash="k1"),
        make_view_evidence(video_id="known-2", channel_id="channel-1", views=200, payload_hash="k2"),
        make_view_evidence(
            video_id="hidden",
            channel_id="channel-1",
            views=999999,
            truth_class=TruthClass.UNKNOWN,
            payload_hash="hidden",
        ),
        make_view_evidence(video_id="known-3", channel_id="channel-1", views=300, payload_hash="k3"),
    ]
    result = derive_robust_content_intelligence(CANDIDATE, evidence, RUN)

    hidden = next(observation for observation in result.observations if observation.video_id == "hidden")
    assert hidden.state == RobustOutlierObservationState.UNKNOWN_VIEW_COUNT
    assert hidden.view_count is None
    assert hidden.relative_score is None
    assert result.unknown_view_video_count == 1
    assert result.creator_baselines[0].sample_size == 3
    assert result.creator_baselines[0].median_views == 200.0


def test_payload_view_is_ignored_when_evidence_record_is_unknown():
    item = make_view_evidence(
        video_id="hidden",
        channel_id="channel-1",
        views=123456,
        truth_class=TruthClass.UNKNOWN,
    )
    result = derive_robust_content_intelligence(CANDIDATE, [item], RUN)

    assert result.observed_video_count == 0
    assert result.state == DimensionState.UNKNOWN
    assert result.observations[0].view_count is None
    assert result.observations[0].state == RobustOutlierObservationState.UNKNOWN_VIEW_COUNT


def test_observed_record_without_numeric_raw_value_is_not_repaired_from_payload():
    item = make_view_evidence(
        video_id="malformed",
        channel_id="channel-1",
        views=None,
        truth_class=TruthClass.OBSERVED,
        payload_overrides={"view_count": 999},
    )
    result = derive_robust_content_intelligence(CANDIDATE, [item], RUN)

    assert result.observed_video_count == 0
    assert result.unknown_view_video_count == 1
    assert result.observations[0].state == RobustOutlierObservationState.INVALID_OBSERVED_VIEW_COUNT
    assert result.observations[0].view_count is None


def test_missing_channel_id_is_not_grouped_into_a_synthetic_creator():
    evidence = [
        make_view_evidence(video_id="no-channel-1", channel_id=None, views=100, payload_hash="m1"),
        make_view_evidence(video_id="no-channel-2", channel_id=None, views=200, payload_hash="m2"),
        make_view_evidence(video_id="no-channel-3", channel_id=None, views=300, payload_hash="m3"),
    ]
    result = derive_robust_content_intelligence(CANDIDATE, evidence, RUN)

    assert result.creator_baselines == ()
    assert result.creators_with_baseline == 0
    assert result.videos_without_channel_id == 3
    assert all(
        observation.state == RobustOutlierObservationState.MISSING_CHANNEL_ID
        for observation in result.observations
    )
    assert all(observation.relative_score is None for observation in result.observations)


def test_missing_video_id_never_becomes_a_synthetic_video_or_creator():
    evidence = [
        make_view_evidence(video_id=None, channel_id="channel-1", views=100, payload_hash="m1"),
        make_view_evidence(video_id=None, channel_id="channel-1", views=200, payload_hash="m2"),
        make_view_evidence(video_id=None, channel_id="channel-1", views=300, payload_hash="m3"),
    ]
    result = derive_robust_content_intelligence(CANDIDATE, evidence, RUN)

    assert result.creator_baselines == ()
    assert result.videos_without_video_id == 3
    assert all(observation.video_id is None for observation in result.observations)
    assert all(
        observation.state == RobustOutlierObservationState.MISSING_VIDEO_ID
        for observation in result.observations
    )


def test_non_view_and_non_audience_evidence_is_not_consumed():
    evidence = [
        make_view_evidence(
            video_id="engagement",
            channel_id="channel-1",
            views=999,
            signal_type="public_video_engagement_rate",
            purpose=EvidencePurpose.AUDIENCE,
        ),
        make_view_evidence(
            video_id="content",
            channel_id="channel-1",
            views=999,
            signal_type="public_content_observation",
            purpose=EvidencePurpose.CONTENT,
        ),
    ]
    result = derive_robust_content_intelligence(CANDIDATE, evidence, RUN)

    assert result.state == DimensionState.MISSING
    assert result.observations == ()
    assert result.provenance.evidence_ids == ()


def test_candidate_and_research_run_are_exact_scopes():
    target = [
        make_view_evidence(video_id="target-1", views=100, payload_hash="t1"),
        make_view_evidence(video_id="target-2", views=200, payload_hash="t2"),
        make_view_evidence(video_id="target-3", views=300, payload_hash="t3"),
    ]
    other_candidate = [
        make_view_evidence(
            candidate_id=OTHER_CANDIDATE,
            video_id="other-candidate",
            views=100000,
            payload_hash="other-candidate",
        )
    ]
    other_run = [
        make_view_evidence(
            research_run_id=OTHER_RUN,
            video_id="other-run",
            views=100000,
            payload_hash="other-run",
        )
    ]
    runless = [
        make_view_evidence(
            research_run_id=None,
            video_id="runless",
            views=100000,
            payload_hash="runless",
        )
    ]
    result = derive_robust_content_intelligence(
        CANDIDATE, target + other_candidate + other_run + runless, RUN
    )

    assert result.provenance.research_run_id == RUN
    assert result.provenance.video_ids == ("target-1", "target-2", "target-3")
    assert result.observed_video_count == 3
    assert result.creator_baselines[0].median_views == 200.0


def test_omitted_run_id_only_accepts_explicitly_runless_inline_evidence():
    evidence = [
        make_view_evidence(
            research_run_id=None,
            video_id="inline-1",
            views=100,
            payload_hash="i1",
        ),
        make_view_evidence(
            research_run_id=None,
            video_id="inline-2",
            views=200,
            payload_hash="i2",
        ),
        make_view_evidence(
            research_run_id=None,
            video_id="inline-3",
            views=300,
            payload_hash="i3",
        ),
        make_view_evidence(video_id="stored", views=999999, payload_hash="stored"),
    ]
    result = derive_robust_content_intelligence(CANDIDATE, evidence)

    assert result.research_run_id is None
    assert result.provenance.video_ids == ("inline-1", "inline-2", "inline-3")
    assert result.creator_baselines[0].median_views == 200.0


# ---------------------------------------------------------------------------
# deterministic deduplication and provenance


def test_duplicate_payload_hash_is_suppressed_without_double_weighting():
    duplicate_id = UUID("30000000-0000-0000-0000-000000000001")
    duplicate = make_view_evidence(
        video_id="video-1",
        channel_id="channel-1",
        views=100,
        evidence_id=duplicate_id,
        payload_hash="same-payload",
    )
    evidence = [
        duplicate,
        duplicate,
        make_view_evidence(video_id="video-2", views=200, payload_hash="p2"),
        make_view_evidence(video_id="video-3", views=300, payload_hash="p3"),
    ]
    result = derive_robust_content_intelligence(CANDIDATE, evidence, RUN)

    assert result.provenance.duplicate_evidence_suppressed == 1
    first = next(observation for observation in result.observations if observation.video_id == "video-1")
    assert first.evidence_ids == (duplicate_id,)
    assert result.creator_baselines[0].sample_size == 3
    assert result.creator_baselines[0].median_views == 200.0


def test_records_without_payload_hash_keep_multiplicity_but_not_numeric_weight():
    first = make_view_evidence(video_id="video-1", views=100, payload_hash=None)
    second = make_view_evidence(video_id="video-1", views=100, payload_hash=None)
    evidence = [
        first,
        second,
        make_view_evidence(video_id="video-2", views=200, payload_hash="p2"),
        make_view_evidence(video_id="video-3", views=300, payload_hash="p3"),
    ]
    result = derive_robust_content_intelligence(CANDIDATE, evidence, RUN)

    assert result.provenance.duplicate_evidence_suppressed == 0
    observation = next(item for item in result.observations if item.video_id == "video-1")
    assert observation.evidence_ids == tuple(sorted((first.id, second.id)))
    assert result.creator_baselines[0].sample_size == 3


def test_conflicting_observed_counts_are_non_scoreable_not_arrival_dependent():
    first = make_view_evidence(video_id="video-1", views=100, payload_hash="first")
    second = make_view_evidence(video_id="video-1", views=999, payload_hash="second")
    peers = [
        make_view_evidence(video_id="video-2", views=200, payload_hash="p2"),
        make_view_evidence(video_id="video-3", views=300, payload_hash="p3"),
    ]
    result = derive_robust_content_intelligence(CANDIDATE, [first, second, *peers], RUN)

    observation = next(item for item in result.observations if item.video_id == "video-1")
    assert observation.state == RobustOutlierObservationState.CONFLICTING_OBSERVED_VIEW_COUNTS
    assert observation.view_count is None
    assert observation.relative_score is None
    assert result.creator_baselines[0].sample_size == 2


def test_shuffled_equivalent_evidence_is_byte_equal_at_the_model_level():
    evidence = [
        make_view_evidence(video_id="video-1", views=100, payload_hash="p1"),
        make_view_evidence(video_id="video-2", views=200, payload_hash="p2"),
        make_view_evidence(video_id="video-3", views=1000, payload_hash="p3"),
        make_view_evidence(
            video_id="hidden",
            views=1234,
            truth_class=TruthClass.UNKNOWN,
            payload_hash="hidden",
        ),
    ]
    expected = derive_robust_content_intelligence(CANDIDATE, evidence, RUN)
    shuffled = list(evidence)
    random.Random(42).shuffle(shuffled)
    actual = derive_robust_content_intelligence(CANDIDATE, shuffled, RUN)

    assert actual == expected


def test_provenance_is_canonical_and_keeps_truth_classes():
    evidence = [
        make_view_evidence(video_id="z-video", views=300, payload_hash="z"),
        make_view_evidence(
            video_id="unknown",
            views=999,
            truth_class=TruthClass.UNKNOWN,
            payload_hash="unknown",
        ),
        make_view_evidence(video_id="a-video", views=100, payload_hash="a"),
        make_view_evidence(video_id="m-video", views=200, payload_hash="m"),
    ]
    result = derive_robust_content_intelligence(CANDIDATE, evidence, RUN)

    assert result.provenance.video_ids == ("a-video", "m-video", "unknown", "z-video")
    assert result.provenance.channel_ids == ("channel-1",)
    assert result.provenance.evidence_ids == tuple(sorted(item.id for item in evidence))
    assert result.provenance.source_truth_classes == ("OBSERVED", "UNKNOWN")
    assert result.provenance.providers == ("fake-content",)
    assert result.provenance.platforms == ("youtube",)


# ---------------------------------------------------------------------------
# formula boundaries and forbidden fields


def test_publication_age_does_not_affect_score():
    old = make_view_evidence(
        video_id="old",
        views=1000,
        payload_hash="old",
        payload_overrides={"published_at": "1999-01-01T00:00:00+00:00"},
    )
    recent = make_view_evidence(
        video_id="recent",
        views=1000,
        payload_hash="recent",
        payload_overrides={"published_at": "2026-09-10T00:00:00+00:00"},
    )
    peers = [
        make_view_evidence(video_id="peer-1", views=100, payload_hash="p1"),
        make_view_evidence(video_id="peer-2", views=200, payload_hash="p2"),
    ]
    old_result = derive_robust_content_intelligence(CANDIDATE, [old, *peers], RUN)
    recent_result = derive_robust_content_intelligence(CANDIDATE, [recent, *peers], RUN)

    old_score = next(
        observation.relative_score
        for observation in old_result.observations
        if observation.video_id == "old"
    )
    recent_score = next(
        observation.relative_score
        for observation in recent_result.observations
        if observation.video_id == "recent"
    )
    assert old_score == recent_score


def test_module_does_not_read_forbidden_metrics_or_publication_time():
    source = open("app/services/faceless_content_intelligence.py", encoding="utf-8").read()
    tree = ast.parse(source)
    forbidden_attribute_reads = {
        "channel_subscriber_count",
        "channel_view_count",
        "channel_video_count",
        "watch_time",
        "retention",
        "ctr",
        "sales",
        "revenue",
        "buyer_count",
        "demand",
        "conversion",
        "market_size",
        "published_at",
    }
    accessed = {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
    }
    assert not accessed.intersection(forbidden_attribute_reads)

    payload_keys = {
        node.args[0].value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    }
    assert payload_keys == {"video_id", "channel_id"}


def test_module_has_no_provider_or_network_dependency():
    source = open("app/services/faceless_content_intelligence.py", encoding="utf-8").read()
    assert "httpx" not in source
    assert "requests" not in source
    assert "urllib" not in source
    assert "PublicContentProvider" not in source
