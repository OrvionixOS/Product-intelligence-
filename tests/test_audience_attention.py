"""Milestone 4F tests: Audience Attention.

No network, no live API calls, no new provider.

Four rules carry the milestone, and most of this file exists to keep them
enforced rather than merely documented.

1. Attention is not demand. Views are playback events, engagement is
   interaction, and neither is a buyer, a purchase intent, or a willingness
   to pay. Seven permanent UNKNOWN markers say so structurally.

2. One viral video must never make a whole opportunity look universally
   popular. The largest contributor is isolated, not averaged away, and a
   field whose attention is one video reports SINGLE_VIDEO_ATTENTION however
   large its total.

3. Subscriber counts are channel context, never a candidate's audience. An
   AST guard proves the module cannot read one.

4. Missing metrics stay UNKNOWN. "We did not look", "it failed", "it ran and
   found nothing", and "we found videos but no view counts" are four facts
   and stay four states.

A fifth guard is anti-duplication: 4F must not restate 3A search demand, 4A,
4B, 4D or 4E. An AST guard proves the module reads none of their fields.
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
from app.services.audience_attention import (
    ATTENTION_PATTERN_VERSION,
    AUDIENCE_ATTENTION_VERSION,
    CONCENTRATION_SHARE,
    DIM_AUDIENCE_ATTENTION,
    DISTRIBUTED_MIN_CHANNELS,
    PATTERN_CLAIMS,
    SINGLE_VIDEO_DOMINANCE_SHARE,
    SPARSE_SAMPLE_MAX_VIDEOS,
    TYPICAL_BAND_HIGH,
    TYPICAL_BAND_LOW,
    AttentionPattern,
    _covering_half,
    extract_audience_attention,
)
from app.services.preliminary_dimensions import DimensionState
from app.services.research_orchestration import (
    DERIVATION_AUDIENCE_ATTENTION,
    STATUS_DERIVATION_COMPLETE,
    STATUS_DERIVATION_ERROR,
    STATUS_DERIVATION_NOT_REQUESTED,
    run_preliminary_research,
)
from app.storage.memory import ResearchStore
from tests.test_orchestration import (
    FakeContentProvider,
    make_candidate,
    video_for,
)

from tests.route_surface import assert_openapi_surface_unchanged, assert_route_surface_unchanged

NOW = datetime(2026, 1, 1, tzinfo=UTC)
MODULE_PATH = Path("app/services/audience_attention.py")

NOT_REQUESTED = "capability_not_requested"
FAILED = "capability_provider_failed"
UNEXPECTED = "capability_unexpected_error"
PUBLIC_CONTENT = "public_content"


# ----------------------------------------------------------------- fixtures


def video_evidence(
    video_id: str,
    *,
    views: int | None = 1000,
    channel_id: str | None = "UC_A",
    candidate_id: UUID | None = None,
    engagement: float | None = 0.02,
    published_days_ago: int | None = 100,
    subscriber_count: int | None = 500_000,
    provider: str = "youtube",
    payload_hash: str | None = None,
) -> list[EvidenceItem]:
    """The evidence shape 3B emits for one (candidate, video) pair.

    Three records: a view-count record, an engagement-rate record, and a
    content observation. `channel_subscriber_count` is deliberately present
    in the payload so the AST guard is testing a real temptation.
    """
    published = (
        (NOW - timedelta(days=published_days_ago)).isoformat()
        if published_days_ago is not None
        else None
    )
    payload: dict[str, Any] = {
        "video_id": video_id,
        "channel_id": channel_id,
        "channel_title": "A Channel",
        "title": f"Video {video_id}",
        "published_at": published,
        "view_count": views,
        "like_count": None if views is None else int(views * (engagement or 0)),
        "comment_count": 3,
        "channel_subscriber_count": subscriber_count,
        "channel_view_count": 9_000_000,
        "channel_video_count": 250,
        "url": f"https://youtu.be/{video_id}",
    }
    common = dict(
        candidate_id=candidate_id,
        provider=provider,
        collection_method="official_api",
        source_reference="/youtube/v3/search",
        platform=provider,
        raw_payload=payload,
        raw_payload_hash=payload_hash or f"hash-{video_id}",
    )
    return [
        EvidenceItem(
            signal_type="public_video_view_count",
            purpose=EvidencePurpose.AUDIENCE,
            truth_class=TruthClass.OBSERVED if views is not None else TruthClass.UNKNOWN,
            raw_value=views,
            **common,
        ),
        EvidenceItem(
            signal_type="public_video_engagement_rate",
            purpose=EvidencePurpose.AUDIENCE,
            truth_class=(
                TruthClass.INFERRED if engagement is not None else TruthClass.UNKNOWN
            ),
            raw_value=engagement,
            **common,
        ),
        EvidenceItem(
            signal_type="public_content_observation",
            purpose=EvidencePurpose.CONTENT,
            truth_class=TruthClass.OBSERVED,
            raw_value=None,
            **common,
        ),
    ]


def field_of(
    *view_counts: int | None,
    channels: list[str | None] | None = None,
    candidate_id: UUID | None = None,
) -> list[EvidenceItem]:
    """One video per positional view count, round-robin across channels."""
    evidence: list[EvidenceItem] = []
    names = channels or ["UC_A"]
    for index, views in enumerate(view_counts):
        evidence.extend(
            video_evidence(
                f"V{index}",
                views=views,
                channel_id=names[index % len(names)],
                candidate_id=candidate_id,
            )
        )
    return evidence


def run(evidence, **kwargs):
    return extract_audience_attention(
        candidate_id=kwargs.pop("candidate_id", uuid4()),
        evidence=evidence,
        **kwargs,
    )


# -------------------------------------- one viral video is one viral video


def test_one_viral_video_does_not_make_the_field_look_broadly_watched():
    """The milestone's central guarantee.

    Ten million views on one video alongside nineteen videos at a hundred
    views each is one video's reach, not a popular topic. The total would say
    otherwise, which is exactly why no total is reported.
    """
    result = run(
        field_of(
            10_000_000,
            *([100] * 19),
            channels=[f"UC_{i}" for i in range(8)],
        )
    )
    assert result.pattern is AttentionPattern.SINGLE_VIDEO_ATTENTION
    features = result.features
    assert features.top_video_attention_share > 0.99
    # The field without the outlier: a hundred views.
    assert features.median_views_excluding_top_video == 100.0
    assert features.videos_covering_half_of_attention == 1
    # And nothing in the result offers a total to be misread.
    assert not any("total" in name for name in features.__dataclass_fields__)


@pytest.mark.parametrize("scale", [1, 100, 10_000])
def test_the_shape_is_scale_invariant(scale):
    """Multiplying every view count changes the totals, never the shape.

    Concentration is a ratio, so a field of the same proportions reports the
    same pattern and the same shares whether it drew a thousand views or ten
    million. A statistic that moved with scale would be a popularity measure
    wearing a distribution measure's name.
    """
    channels = [f"UC_{i}" for i in range(8)]
    baseline = run(field_of(1000, *([10] * 19), channels=channels))
    scaled = run(field_of(1000 * scale, *([10 * scale] * 19), channels=channels))
    assert baseline.pattern is scaled.pattern is AttentionPattern.SINGLE_VIDEO_ATTENTION
    assert (
        baseline.features.top_video_attention_share
        == scaled.features.top_video_attention_share
    )
    assert (
        baseline.features.videos_covering_half_of_attention
        == scaled.features.videos_covering_half_of_attention
    )
    assert (
        baseline.features.proportion_within_band_of_median
        == scaled.features.proportion_within_band_of_median
    )
    # The medians themselves scale, as they must.
    assert scaled.features.median_views == baseline.features.median_views * scale


def test_median_excluding_top_drops_exactly_one_copy_of_the_largest():
    """Three videos tied at the top: removing ONE must leave the other two.

    Chosen so the two possible implementations disagree. Dropping one copy
    leaves [500, 500, 10] with a median of 500; dropping every copy of the
    maximum leaves [10] with a median of 10. The first is right: the feature
    removes the single largest VIDEO, not every video that happens to match
    its view count, and equally-watched peers are evidence, not duplicates.
    """
    result = run(
        field_of(*([500, 500, 500, 10]), channels=["UC_A", "UC_B", "UC_C", "UC_D"])
    )
    assert result.features.median_views_excluding_top_video == 500.0

    # And with a single clear outlier the feature shows the field beneath it.
    spiky = run(field_of(*([9000, 10, 10, 10]), channels=["UC_A", "UC_B", "UC_C"]))
    assert spiky.features.median_views_excluding_top_video == 10.0


def test_the_dominance_threshold_is_the_boundary_it_claims_to_be():
    """Just over the threshold is one video's field; just under is not."""
    # 7 of 10 views in one video -> 0.7, above the threshold.
    over = run(field_of(*([70] + [10] * 3), channels=["UC_A", "UC_B", "UC_C"]))
    assert over.features.top_video_attention_share >= SINGLE_VIDEO_DOMINANCE_SHARE
    assert over.pattern is AttentionPattern.SINGLE_VIDEO_ATTENTION
    # 3 of 10 -> 0.3, below both thresholds.
    under = run(field_of(*([30] + [70] * 7), channels=["UC_A", "UC_B", "UC_C"]))
    assert under.features.top_video_attention_share < CONCENTRATION_SHARE
    assert under.pattern is AttentionPattern.DISTRIBUTED_ATTENTION


def test_band_bounds_are_the_documented_multiples_of_the_median():
    """Exactly at each bound is inside; a step beyond is outside."""
    median = 1000
    result = run(
        field_of(
            int(median * TYPICAL_BAND_LOW),
            int(median * TYPICAL_BAND_HIGH),
            median,
            median,
            median,
            int(median * TYPICAL_BAND_LOW) - 1,
            int(median * TYPICAL_BAND_HIGH) + 1,
            channels=["UC_A", "UC_B", "UC_C"],
        )
    )
    assert result.features.median_views == float(median)
    # Five inside (both bounds inclusive, three at the median), two outside.
    assert result.features.videos_within_band_of_median == 5


def test_evenly_watched_field_is_not_reported_as_one_video():
    result = run(field_of(*([1000] * 9), channels=[f"UC_{i}" for i in range(3)]))
    assert result.pattern is AttentionPattern.DISTRIBUTED_ATTENTION
    assert result.features.top_video_attention_share < CONCENTRATION_SHARE
    assert result.features.proportion_within_band_of_median == 1.0


def test_many_videos_by_one_creator_is_concentrated_not_distributed():
    """Breadth of videos is not breadth of creators."""
    result = run(field_of(*([1000] * 12), channels=["UC_ONLY"]))
    assert result.pattern is AttentionPattern.CONCENTRATED_ATTENTION
    assert result.features.top_channel_attention_share == 1.0
    assert result.features.channels_with_observed_attention == 1


def test_distributed_requires_several_creators():
    two = run(field_of(*([1000] * 10), channels=["UC_A", "UC_B"]))
    assert two.features.channels_with_observed_attention < DISTRIBUTED_MIN_CHANNELS
    assert two.pattern is AttentionPattern.CONCENTRATED_ATTENTION
    three = run(field_of(*([1000] * 12), channels=["UC_A", "UC_B", "UC_C"]))
    assert three.pattern is AttentionPattern.DISTRIBUTED_ATTENTION


# ------------------------------------------- attention is never a conclusion


@pytest.mark.parametrize(
    "marker",
    [
        "buyer_count",
        "purchase_intent",
        "candidate_audience_size",
        "demand_durability",
        "willingness_to_pay",
        "watch_time",
        "conversion_probability",
    ],
)
def test_conclusion_markers_are_permanently_unknown(marker):
    for evidence, reasons in [
        ([], {}),
        ([], {PUBLIC_CONTENT: NOT_REQUESTED}),
        (field_of(1000, 2000, 3000, 4000, 5000), {}),
        (field_of(10_000_000, *([1] * 30)), {}),
    ]:
        result = run(evidence, missing_reasons=reasons)
        assert getattr(result, marker) is TruthClass.UNKNOWN


def test_no_pattern_claims_demand_durability():
    """Even the broadest pattern must disclaim durability explicitly."""
    result = run(field_of(*([1000] * 12), channels=["UC_A", "UC_B", "UC_C"]))
    assert result.pattern is AttentionPattern.DISTRIBUTED_ATTENTION
    assert result.demand_durability is TruthClass.UNKNOWN
    assert "persist" in result.claim.does_not_establish


def test_every_pattern_states_what_it_does_not_establish():
    for pattern in AttentionPattern:
        assert pattern in PATTERN_CLAIMS, pattern
        claim = PATTERN_CLAIMS[pattern]
        assert claim.observes.strip()
        assert claim.does_not_establish.strip()
        assert claim.observes != claim.does_not_establish


def test_every_result_carries_the_claim_boundary():
    cases = [
        ([], {}),
        ([], {PUBLIC_CONTENT: FAILED}),
        (field_of(None, None), {}),
        (field_of(0, 0, 0, 0, 0), {}),
        (field_of(1000), {}),
        (field_of(*([1000] * 12), channels=["UC_A", "UC_B", "UC_C"]), {}),
    ]
    for evidence, reasons in cases:
        result = run(evidence, missing_reasons=reasons)
        assert result.claim is PATTERN_CLAIMS[result.pattern]
        assert result.claim.does_not_establish.strip()


def test_value_is_permanently_none_and_cannot_reach_legacy_scoring():
    result = run(field_of(1000, 2000, 3000, 4000, 5000))
    assert result.value is None
    assert result.value_truth_class is None
    assert result.dimension_name == DIM_AUDIENCE_ATTENTION
    assert result.dimension_name == "preliminary_audience_attention"
    # Neither the legacy ScoreDimensions field nor 3C's scored dimension.
    assert result.dimension_name != "audience_interest"
    assert result.dimension_name != "preliminary_audience_interest"


def test_derived_features_are_inferred_even_from_observed_inputs():
    result = run(field_of(1000, 2000, 3000, 4000, 5000))
    assert result.features_truth_class is TruthClass.INFERRED


def test_versions_are_reported():
    result = run(field_of(1000, 2000, 3000, 4000))
    assert result.version == AUDIENCE_ATTENTION_VERSION
    assert result.pattern_version == ATTENTION_PATTERN_VERSION
    assert result.features.features_version == "attention_distribution_features_v1"


# ------------------------------------------------ missing never becomes zero


@pytest.mark.parametrize("reason", [NOT_REQUESTED, FAILED, UNEXPECTED])
def test_capability_failure_is_unknown_attention(reason):
    result = run([], missing_reasons={PUBLIC_CONTENT: reason})
    assert result.pattern is AttentionPattern.UNKNOWN_ATTENTION
    assert result.state is DimensionState.MISSING
    assert result.missing_reason == reason
    assert result.features is None


def test_ran_and_found_nothing_is_an_observed_absence():
    result = run([], missing_reasons={})
    assert result.pattern is AttentionPattern.NO_CONTENT_OBSERVED
    assert result.state is DimensionState.UNKNOWN
    assert result.missing_reason == "no_public_content_returned_for_candidate"


def test_videos_without_view_counts_are_unmeasured_not_unwatched():
    """The fifth fact, kept separate from the other four."""
    result = run(field_of(None, None, None, None, None))
    assert result.pattern is AttentionPattern.ATTENTION_UNMEASURED
    assert result.state is DimensionState.UNKNOWN
    assert result.missing_reason == "no_view_count_observed_for_any_video"
    assert result.features.video_count == 5
    assert result.features.videos_with_unmeasured_views == 5
    assert result.features.videos_with_observed_views == 0
    # Unknown, never zero.
    assert result.features.median_views is None
    assert result.features.top_video_attention_share is None


def test_observed_zero_views_is_a_measured_absence_not_missing_data():
    result = run(field_of(0, 0, 0, 0, 0))
    assert result.pattern is AttentionPattern.NO_OBSERVED_ATTENTION
    assert result.features.videos_with_observed_views == 5
    assert result.features.videos_with_unmeasured_views == 0
    # Views WERE measured, and the median is a real zero.
    assert result.features.median_views == 0.0
    # But a share of no attention has no denominator.
    assert result.features.top_video_attention_share is None


def test_unmeasured_and_measured_zero_are_different_states():
    unmeasured = run(field_of(None, None, None, None, None))
    measured_zero = run(field_of(0, 0, 0, 0, 0))
    assert unmeasured.pattern is not measured_zero.pattern
    assert unmeasured.missing_reason != measured_zero.missing_reason
    assert unmeasured.features.median_views is None
    assert measured_zero.features.median_views == 0.0


def test_partially_measured_field_excludes_only_the_unmeasured():
    result = run(field_of(1000, 1000, 1000, 1000, None, None))
    assert result.features.video_count == 6
    assert result.features.videos_with_observed_views == 4
    assert result.features.videos_with_unmeasured_views == 2
    # Statistics describe the four measured videos, not six.
    assert result.features.median_views == 1000.0
    assert result.features.top_video_attention_share == 0.25


def test_another_capabilitys_failure_does_not_make_attention_unknown():
    result = run([], missing_reasons={"marketplace": FAILED, "search_demand": FAILED})
    assert result.pattern is AttentionPattern.NO_CONTENT_OBSERVED


def test_sparse_sample_is_reported_rather_than_characterized():
    result = run(field_of(*([1000] * SPARSE_SAMPLE_MAX_VIDEOS)))
    assert result.pattern is AttentionPattern.SPARSE_SAMPLE
    above = run(field_of(*([1000] * (SPARSE_SAMPLE_MAX_VIDEOS + 1))))
    assert above.pattern is not AttentionPattern.SPARSE_SAMPLE


def test_channels_without_ids_are_not_merged_into_one_fictional_creator():
    result = run(field_of(*([1000] * 8), channels=[None]))
    assert result.features.channels_in_sample == 0
    assert result.features.channels_with_observed_attention == 0
    # Unknown, never 1.0.
    assert result.features.top_channel_attention_share is None


# --------------------------------------------------------- anti-duplication


def test_module_cannot_read_a_subscriber_count():
    """AST guard: channel context must be unreachable, not merely unused.

    A subscriber count describes a creator's whole audience across every
    topic. Treating it as a candidate's audience is the easiest way to
    fabricate a market size, so the guarantee is code that cannot reach it.
    """
    forbidden = {
        "channel_subscriber_count",
        "channel_view_count",
        "channel_video_count",
        "subscriber_count",
        "subscribers",
    }
    read = _payload_fields_read()
    assert not (read & forbidden), sorted(read & forbidden)


def test_module_reads_no_search_marketplace_or_reach_field():
    """4F must not restate 3A search demand, 4A, 4B, 4D or 4E."""
    forbidden = {
        "search_volume",
        "cpc",
        "competition_index",
        "low_top_of_page_bid",
        "high_top_of_page_bid",
        "listing_id",
        "seller_id",
        "price",
        "currency",
        "review_count",
        "rating",
        "is_digital",
    }
    read = _payload_fields_read()
    assert not (read & forbidden), sorted(read & forbidden)


def _payload_fields_read() -> set:
    """Every name the module's EXECUTABLE code could be reading.

    Attribute accesses, `.get("...")` keys, subscript keys, and bare string
    constants alike. Docstrings are stripped first, so the module may still
    NAME a forbidden field in prose to explain why it refuses to read it —
    which it does for subscriber counts — while any executable mention
    trips the guard. A collector scanning only attributes and `.get` calls
    missed a literal slipped into a tuple, which is why constants are
    included here.
    """
    tree = ast.parse(MODULE_PATH.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            node.value = ast.Constant(value="")
    read = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            read.add(node.attr)
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            read.add(node.value)
        if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant):
            read.add(node.slice.value)
    return read


def _payload_get_keys() -> set:
    """Exactly which payload keys the reducer reads, via `payload.get(...)`.

    Narrower than _payload_fields_read on purpose: 4F owns an attribute
    called `view_count` on its own VideoView, so a broad name scan cannot
    answer "does this module take a view count from the payload snapshot?".
    This can, because it looks only at reads from `payload`.
    """
    tree = ast.parse(MODULE_PATH.read_text())
    keys = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "payload"
            and node.args
            and isinstance(node.args[0], ast.Constant)
        ):
            keys.add(node.args[0].value)
    return keys


def test_module_does_not_import_scoring_or_the_other_derivations():
    tree = ast.parse(MODULE_PATH.read_text())
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
    for banned in (
        "scoring",
        "purchase_evidence",
        "price_evidence",
        "buyer_reach",
        "competition_opportunity",
        "product_specification",
        "search_demand",
        "public_content_features",
    ):
        assert not any(banned in module for module in modules), banned


def test_no_scoring_vocabulary_is_introduced():
    """Checked against executable code, comments and docstrings stripped."""
    tree = ast.parse(MODULE_PATH.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            node.value = ast.Constant(value="")
    code = ast.unparse(tree)
    for token in ("RED", "YELLOW", "GREEN"):
        assert not re.search(rf"\b{token}\b", code), token
    for token in ("score_opportunity", "ScoreDimensions", "Classification"):
        assert token not in code, token


def test_no_total_views_is_computed_as_an_emitted_feature():
    """A total is the statistic one viral video corrupts, so none is emitted."""
    from app.services.audience_attention import AttentionFeatures

    names = set(AttentionFeatures.__dataclass_fields__)
    assert not any("total" in name for name in names), names
    assert not any(name.startswith("sum_") for name in names), names


def test_engagement_stays_inferred_and_is_never_promoted():
    """3B derives engagement and classes it INFERRED. 4F must not upgrade it."""
    candidate_id = uuid4()
    evidence = field_of(1000, 2000, 3000, 4000, candidate_id=candidate_id)
    result = extract_audience_attention(candidate_id=candidate_id, evidence=evidence)
    assert result.features.median_engagement_rate is not None
    assert result.features.videos_with_engagement_rate == 4
    assert "INFERRED" in result.provenance.source_truth_classes
    assert result.features_truth_class is TruthClass.INFERRED


def test_attention_count_differs_from_4d_channel_count_when_views_are_missing():
    """channels_with_observed_attention is not 4D's channel population.

    4D counts creators with a relevant video. 4F counts creators whose videos
    carry an OBSERVED view count; when the provider hid the metric, those are
    different numbers and the names must not be conflated.
    """
    evidence = []
    evidence.extend(video_evidence("A", views=1000, channel_id="UC_A"))
    evidence.extend(video_evidence("B", views=None, channel_id="UC_B"))
    evidence.extend(video_evidence("C", views=None, channel_id="UC_C"))
    result = run(evidence)
    assert result.features.channels_in_sample == 3
    assert result.features.channels_with_observed_attention == 1


# ----------------------------------------------------------------- determinism


def _identity(result) -> tuple:
    features = result.features
    return (
        result.state,
        result.pattern,
        result.missing_reason,
        result.claim,
        None
        if features is None
        else (
            features.video_count,
            features.videos_with_observed_views,
            features.videos_with_unmeasured_views,
            features.channels_in_sample,
            features.channels_with_observed_attention,
            features.median_views,
            features.lower_quartile_views,
            features.upper_quartile_views,
            features.top_video_attention_share,
            features.median_views_excluding_top_video,
            features.videos_covering_half_of_attention,
            features.top_channel_attention_share,
            features.videos_within_band_of_median,
            features.proportion_within_band_of_median,
            features.videos_with_publish_date,
            features.publish_span_days,
            features.distinct_publish_months,
            features.median_engagement_rate,
            features.videos_with_engagement_rate,
        ),
        result.provenance.evidence_ids,
        result.provenance.video_ids,
        result.provenance.channel_ids,
        result.provenance.providers,
        result.provenance.source_truth_classes,
        result.provenance.duplicate_evidence_suppressed,
    )


def _mixed_field(candidate_id: UUID) -> list[EvidenceItem]:
    evidence: list[EvidenceItem] = []
    plan = [
        ("V0", 50_000, "UC_A", 30),
        ("V1", 50_000, "UC_A", 200),
        ("V2", 12_000, "UC_B", 400),
        ("V3", 12_000, "UC_B", 10),
        ("V4", 900, "UC_C", 700),
        ("V5", 900, "UC_D", 60),
        ("V6", 40, "UC_E", 365),
        ("V7", None, "UC_F", None),
        ("V8", 0, "UC_G", 15),
        ("V9", 7_500, None, 90),
    ]
    for video_id, views, channel, age in plan:
        evidence.extend(
            video_evidence(
                video_id,
                views=views,
                channel_id=channel,
                published_days_ago=age,
                candidate_id=candidate_id,
            )
        )
    return evidence


def test_output_is_identical_under_1000_shuffles():
    candidate_id = uuid4()
    evidence = _mixed_field(candidate_id)
    baseline = _identity(
        extract_audience_attention(candidate_id=candidate_id, evidence=evidence)
    )
    rng = random.Random(20260911)
    mismatches = 0
    for _ in range(1000):
        shuffled = list(evidence)
        rng.shuffle(shuffled)
        if (
            _identity(
                extract_audience_attention(
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
        extract_audience_attention(candidate_id=candidate_id, evidence=evidence)
    )
    assert (
        _identity(
            extract_audience_attention(
                candidate_id=candidate_id, evidence=list(reversed(evidence))
            )
        )
        == baseline
    )
    for _ in range(5):
        assert (
            _identity(
                extract_audience_attention(
                    candidate_id=candidate_id, evidence=evidence
                )
            )
            == baseline
        )


def test_half_coverage_depends_only_on_the_multiset_of_view_counts():
    """The real invariant, pinned directly rather than by shuffling inputs."""
    rng = random.Random(31337)
    for _ in range(300):
        counts = [rng.randint(1, 5000) for _ in range(rng.randint(1, 9))]
        baseline = _covering_half(counts)
        for _ in range(5):
            shuffled = list(counts)
            rng.shuffle(shuffled)
            assert _covering_half(shuffled) == baseline


def test_mixed_identifier_types_order_without_coercion():
    candidate_id = uuid4()
    evidence: list[EvidenceItem] = []
    for index, channel in enumerate([1, "1", "UC", 2]):
        evidence.extend(
            video_evidence(
                f"T{index}", channel_id=channel, candidate_id=candidate_id
            )
        )
    result = extract_audience_attention(
        candidate_id=candidate_id, evidence=evidence
    )
    baseline = result.provenance.channel_ids
    assert len(baseline) == 4
    rng = random.Random(11)
    for _ in range(100):
        shuffled = list(evidence)
        rng.shuffle(shuffled)
        assert (
            extract_audience_attention(
                candidate_id=candidate_id, evidence=shuffled
            ).provenance.channel_ids
            == baseline
        )
    assert {type(value) for value in baseline} == {int, str}


def test_re_delivered_evidence_does_not_inflate_attention():
    candidate_id = uuid4()
    evidence = field_of(
        1000, 2000, 3000, 4000, 5000, channels=["UC_A", "UC_B", "UC_C"],
        candidate_id=candidate_id,
    )
    once = extract_audience_attention(candidate_id=candidate_id, evidence=evidence)
    twice = extract_audience_attention(
        candidate_id=candidate_id, evidence=evidence + evidence
    )
    assert once.features.video_count == twice.features.video_count == 5
    assert (
        once.features.median_views
        == twice.features.median_views
        == 3000.0
    )
    assert (
        once.features.top_video_attention_share
        == twice.features.top_video_attention_share
    )
    assert once.pattern is twice.pattern
    assert twice.provenance.duplicate_evidence_suppressed > 0


# --------------------------------------------------------------- provenance


def test_provenance_is_canonically_ordered_and_cites_every_record():
    candidate_id = uuid4()
    evidence = _mixed_field(candidate_id)
    result = extract_audience_attention(
        candidate_id=candidate_id, evidence=evidence
    )
    provenance = result.provenance
    assert list(provenance.evidence_ids) == sorted(provenance.evidence_ids)
    assert list(provenance.video_ids) == sorted(provenance.video_ids)
    assert provenance.candidate_id == candidate_id
    assert provenance.providers == ("youtube",)
    assert provenance.platforms == ("youtube",)
    assert len(provenance.evidence_ids) == len(evidence)


def test_provenance_survives_an_unknown_field():
    result = run([], missing_reasons={PUBLIC_CONTENT: FAILED})
    assert result.provenance.evidence_ids == ()
    assert result.provenance.video_ids == ()
    assert result.provenance.channel_ids == ()


def test_weakest_truth_class_wins_the_basis():
    candidate_id = uuid4()
    evidence = video_evidence(
        "A", views=None, engagement=None, candidate_id=candidate_id
    )
    evidence.extend(video_evidence("B", views=1000, candidate_id=candidate_id))
    result = extract_audience_attention(
        candidate_id=candidate_id, evidence=evidence
    )
    assert result.evidence_truth_basis is TruthClass.UNKNOWN


# ------------------------------------------------------------- orchestration


@pytest.mark.asyncio
async def test_audience_attention_runs_from_stored_evidence_without_new_calls():
    candidates = [
        make_candidate(f"Cand {index}", content_queries=[f"q{index}"])
        for index in range(3)
    ]
    content = FakeContentProvider(
        {f"q{index}": [video_for(f"V{index}")] for index in range(3)}
    )
    result = await run_preliminary_research(
        candidates=candidates, store=ResearchStore(), public_content_provider=content
    )
    outcome = next(
        d for d in result.derivations if d.derivation == DERIVATION_AUDIENCE_ATTENTION
    )
    assert outcome.status == STATUS_DERIVATION_COMPLETE
    assert outcome.candidates_covered == 3
    assert set(result.audience_attention) == {
        c.candidate_id for c in result.ranking.selected
    }


@pytest.mark.asyncio
async def test_attention_failure_degrades_only_itself(monkeypatch):
    import app.services.research_orchestration as orchestration

    def boom(*args: Any, **kwargs: Any):
        raise RuntimeError("attention derivation bug")

    monkeypatch.setattr(orchestration, "extract_audience_attention", boom)

    candidates = [make_candidate("Alpha", content_queries=["q"])]
    content = FakeContentProvider({"q": [video_for("V1")]})
    result = await run_preliminary_research(
        candidates=candidates, store=ResearchStore(), public_content_provider=content
    )
    outcome = next(
        d for d in result.derivations if d.derivation == DERIVATION_AUDIENCE_ATTENTION
    )
    assert outcome.status == STATUS_DERIVATION_ERROR
    assert "RuntimeError" in outcome.failure_reason
    assert result.audience_attention == {}
    # Every other derivation and the ranking survive.
    assert result.buyer_reach
    assert len(result.ranking.ranked) == 1


@pytest.mark.asyncio
async def test_audience_attention_can_be_skipped():
    candidates = [make_candidate("Alpha", content_queries=["q"])]
    content = FakeContentProvider({"q": [video_for("V1")]})
    result = await run_preliminary_research(
        candidates=candidates,
        store=ResearchStore(),
        public_content_provider=content,
        derive_audience_attention=False,
    )
    outcome = next(
        d for d in result.derivations if d.derivation == DERIVATION_AUDIENCE_ATTENTION
    )
    assert outcome.status == STATUS_DERIVATION_NOT_REQUESTED
    assert result.audience_attention == {}


@pytest.mark.asyncio
async def test_content_capability_not_requested_leaves_attention_unknown():
    candidates = [make_candidate("Alpha", content_queries=["q"])]
    result = await run_preliminary_research(
        candidates=candidates, store=ResearchStore(), public_content_provider=None
    )
    attention = result.audience_attention[result.ranking.selected[0].candidate_id]
    assert attention.pattern is AttentionPattern.UNKNOWN_ATTENTION
    assert attention.missing_reason == NOT_REQUESTED


@pytest.mark.asyncio
async def test_3c_scored_audience_dimension_is_untouched():
    """4F adds a derivation; it does not change 3C's scored dimension."""
    candidates = [make_candidate("Alpha", content_queries=["q"])]
    content = FakeContentProvider({"q": [video_for("V1", view_count=5000)]})
    result = await run_preliminary_research(
        candidates=candidates, store=ResearchStore(), public_content_provider=content
    )
    dimension = result.profiles[0].dimension("preliminary_audience_interest")
    assert dimension.state is DimensionState.SCORED
    assert dimension.value is not None
    # And 4F's own result carries no value at all.
    attention = result.audience_attention[result.ranking.selected[0].candidate_id]
    assert attention.value is None
    assert attention.dimension_name == "preliminary_audience_attention"


# ---------------------------------------------------------------------- API


def test_response_exposes_audience_attention_additively():
    from app.main import app


    schema = app.openapi()
    response = schema["components"]["schemas"]["PreliminaryResearchResponse"]
    assert "audience_attention" in response["properties"]
    for field in (
        "purchase_evidence",
        "price_evidence",
        "buyer_reach",
        "competition_opportunity",
        "ranked",
    ):
        assert field in response["properties"]
        assert field in response["required"]
    assert_openapi_surface_unchanged(schema)


def test_score_endpoint_remains_gone():
    from fastapi.testclient import TestClient

    from app.main import app

    assert TestClient(app).post("/score", json={}).status_code == 410


def test_route_count_is_unchanged():
    from app.main import app

    assert_route_surface_unchanged(app)


def test_a_view_count_is_read_from_the_evidence_record_not_the_payload():
    """The payload snapshot is not the authority on what was observed.

    A payload can carry a view_count the provider later reported as hidden,
    or that was never confirmed. Only a record whose truth_class is OBSERVED
    supplies a view count, so a payload claiming 999,999 views against an
    UNKNOWN record is unmeasured attention, never 999,999 views and never
    zero.
    """
    candidate_id = uuid4()
    payload = {
        "video_id": "V1",
        "channel_id": "UC_A",
        "published_at": (NOW - timedelta(days=10)).isoformat(),
        # Present in the payload, and deliberately never read.
        "view_count": 999_999,
        "like_count": 40_000,
    }
    common = dict(
        candidate_id=candidate_id,
        provider="youtube",
        collection_method="official_api",
        platform="youtube",
        raw_payload=payload,
        raw_payload_hash="hash-V1",
    )
    evidence = [
        EvidenceItem(
            signal_type="public_video_view_count",
            purpose=EvidencePurpose.AUDIENCE,
            truth_class=TruthClass.UNKNOWN,
            raw_value=None,
            **common,
        ),
        EvidenceItem(
            signal_type="public_content_observation",
            purpose=EvidencePurpose.CONTENT,
            truth_class=TruthClass.OBSERVED,
            raw_value=None,
            **common,
        ),
    ]
    result = extract_audience_attention(
        candidate_id=candidate_id, evidence=evidence
    )
    assert result.pattern is AttentionPattern.ATTENTION_UNMEASURED
    assert result.features.videos_with_observed_views == 0
    assert result.features.median_views is None
    assert _payload_get_keys() == {"video_id", "channel_id", "published_at"}


def test_a_view_count_on_a_non_observed_record_is_not_counted():
    """Truth class decides, not the presence of a number."""
    candidate_id = uuid4()
    common = dict(
        candidate_id=candidate_id,
        provider="youtube",
        collection_method="official_api",
        platform="youtube",
        raw_payload={"video_id": "V1", "channel_id": "UC_A"},
        raw_payload_hash="hash-V1",
    )
    evidence = [
        EvidenceItem(
            signal_type="public_video_view_count",
            purpose=EvidencePurpose.AUDIENCE,
            # A number IS present, but it was never an observation.
            truth_class=TruthClass.INFERRED,
            raw_value=5000,
            **common,
        ),
        EvidenceItem(
            signal_type="public_content_observation",
            purpose=EvidencePurpose.CONTENT,
            truth_class=TruthClass.OBSERVED,
            raw_value=None,
            **common,
        ),
    ]
    result = extract_audience_attention(
        candidate_id=candidate_id, evidence=evidence
    )
    assert result.pattern is AttentionPattern.ATTENTION_UNMEASURED
    assert result.features.videos_with_observed_views == 0


def test_the_typical_band_actually_excludes_atypical_videos():
    """The consistency measure must discriminate, not count everything.

    A field of 1000-view videos with one at 50 and one at 50,000: the median
    is 1000, the band is 500-2000, and exactly the two extremes fall outside
    it. A band that admitted everything would report perfect consistency for
    a wildly inconsistent field.
    """
    result = run(
        field_of(
            50, 1000, 1000, 1000, 1000, 50_000,
            channels=["UC_A", "UC_B", "UC_C"],
        )
    )
    assert result.features.median_views == 1000.0
    assert result.features.videos_within_band_of_median == 4
    assert result.features.proportion_within_band_of_median == round(4 / 6, 4)


def test_a_uniform_field_and_a_spread_field_report_different_consistency():
    uniform = run(field_of(*([1000] * 6), channels=["UC_A", "UC_B", "UC_C"]))
    spread = run(
        field_of(10, 100, 1000, 10_000, 100_000, 1_000_000,
                 channels=["UC_A", "UC_B", "UC_C"])
    )
    assert uniform.features.proportion_within_band_of_median == 1.0
    assert spread.features.proportion_within_band_of_median < 0.5


def test_consistency_is_relative_to_the_median_not_a_level_of_interest():
    """A uniformly ignored field is consistent at a trivial level.

    Found while running the pre-PR checks: 29 videos at 5 views each are
    genuinely consistent with one another, so a consumer reading only the
    consistency figure could mistake "uniform" for "uniformly interesting".
    The measure is honest — it describes spread, not level — and the result
    always carries the median it was measured around, the pattern naming the
    outlier, and a limitation saying so explicitly.
    """
    ignored = run(field_of(*([5] * 8), channels=["UC_A", "UC_B", "UC_C"]))
    watched = run(field_of(*([500_000] * 8), channels=["UC_A", "UC_B", "UC_C"]))
    # Identical consistency; the level is entirely in median_views.
    assert (
        ignored.features.proportion_within_band_of_median
        == watched.features.proportion_within_band_of_median
        == 1.0
    )
    assert ignored.features.median_views == 5.0
    assert watched.features.median_views == 500_000.0
    assert any(
        "read next to" in limitation and "median_views" in limitation
        for limitation in ignored.limitations
    )


def test_a_viral_field_reports_both_the_outlier_and_the_ignored_remainder():
    """Both facts are emitted together, so neither can be read alone."""
    result = run(
        field_of(50_000_000, *([5] * 29), channels=[f"UC_{i}" for i in range(12)])
    )
    assert result.pattern is AttentionPattern.SINGLE_VIDEO_ATTENTION
    # The remainder is consistent...
    assert result.features.proportion_within_band_of_median > 0.9
    # ...and consistently at five views.
    assert result.features.median_views == 5.0
    assert result.features.median_views_excluding_top_video == 5.0
    assert "broadly watched" in result.claim.does_not_establish
