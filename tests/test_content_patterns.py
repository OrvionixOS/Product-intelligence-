"""Milestone 5B tests: content pattern extraction.

No network, no live API calls, no new provider.

Five rules carry the milestone.

1. A recurring pattern is an observation about content that exists. It is
   never a formula, a hook, or a technique that works. No emitted string may
   carry causal or predictive vocabulary.

2. Prevalence and outlier co-occurrence are separate facts with separate
   denominators and are never combined into one strength measure.

3. One prolific creator is not a field. Every pattern carries a distinct
   creator count and a concentration state alongside its raw count.

4. Missing metadata is UNAVAILABLE for that field, never evidence a pattern
   is absent, and never a zero.

5. The evidence record is the authority. A payload value behind a record
   that is not OBSERVED is never read.
"""

import ast
import random
import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.domain.enums import EvidencePurpose, TruthClass
from app.domain.models import EvidenceItem
from app.services.content_patterns import (
    CONTENT_PATTERNS_VERSION,
    DIM_CONTENT_PATTERNS,
    MIN_INDEPENDENT_CREATORS_FOR_COOCCURRENCE,
    MIN_PATTERN_VIDEO_COUNT,
    STATE_BOUNDARIES,
    ConcentrationState,
    ContentPatternsState,
    CooccurrenceState,
    DurationBand,
    PatternKind,
    derive_content_patterns,
    duration_band,
    normalize_text,
    tokenize_title,
)
from app.services.faceless_content_intelligence import (
    derive_robust_content_intelligence,
)
from app.services.preliminary_dimensions import DimensionState

NOW = datetime(2026, 1, 1, tzinfo=UTC)
MODULE_PATH = Path("app/services/content_patterns.py")

NOT_REQUESTED = "capability_not_requested"
FAILED = "capability_provider_failed"
PUBLIC_CONTENT = "public_content"


# ----------------------------------------------------------------- fixtures

_UNSET = object()


def video_evidence(
    video_id: str,
    *,
    candidate_id: UUID,
    channel_id: str | None = "UC_A",
    title: str | None = "How to plan meals",
    tags: Any = _UNSET,
    category: str | None = "Education",
    duration_seconds: Any = 600,
    views: int | None = 1000,
    research_run_id: UUID | None = None,
    truth_class: TruthClass = TruthClass.OBSERVED,
    payload_hash: str | None = None,
    subscriber_count: int | None = 500_000,
) -> list[EvidenceItem]:
    """The evidence shape 3B emits for one (candidate, video) pair.

    Channel aggregates are deliberately present in the payload so the AST
    guard is refusing a real temptation rather than an absent one.
    """
    payload: dict[str, Any] = {
        "video_id": video_id,
        "channel_id": channel_id,
        "channel_title": "A Channel",
        "title": title,
        "description": "a description",
        "published_at": (NOW - timedelta(days=30)).isoformat(),
        "view_count": views,
        "like_count": 10,
        "comment_count": 3,
        "category": category,
        "channel_subscriber_count": subscriber_count,
        "channel_view_count": 9_000_000,
        "channel_video_count": 250,
        "url": f"https://youtu.be/{video_id}",
    }
    if tags is not _UNSET:
        payload["tags"] = tags
    if duration_seconds is not _UNSET:
        payload["duration_seconds"] = duration_seconds

    common = dict(
        candidate_id=candidate_id,
        research_run_id=research_run_id,
        provider="youtube",
        collection_method="official_api",
        platform="youtube",
        raw_payload=payload,
        raw_payload_hash=payload_hash or f"hash-{video_id}",
    )
    return [
        EvidenceItem(
            signal_type="public_content_observation",
            purpose=EvidencePurpose.CONTENT,
            truth_class=truth_class,
            raw_value=None,
            **common,
        ),
        EvidenceItem(
            signal_type="public_video_view_count",
            purpose=EvidencePurpose.AUDIENCE,
            truth_class=(
                TruthClass.OBSERVED if views is not None else TruthClass.UNKNOWN
            ),
            raw_value=views,
            **common,
        ),
    ]


def corpus(*specs, candidate_id: UUID | None = None) -> tuple[UUID, list[EvidenceItem]]:
    """Build a corpus from (video_id, channel_id, title) triples or dicts."""
    cid = candidate_id or uuid4()
    evidence: list[EvidenceItem] = []
    for spec in specs:
        if isinstance(spec, dict):
            evidence.extend(video_evidence(candidate_id=cid, **spec))
        else:
            video_id, channel_id, title = spec
            evidence.extend(
                video_evidence(
                    video_id, candidate_id=cid, channel_id=channel_id, title=title
                )
            )
    return cid, evidence


def run(evidence, candidate_id, **kwargs):
    return derive_content_patterns(
        candidate_id=candidate_id, evidence=evidence, **kwargs
    )


def pattern_for(result, kind: PatternKind, value: str):
    return next(
        (p for p in result.patterns if p.kind is kind and p.value == value), None
    )


# --------------------------------------------------------- normalization


def test_casing_differences_collapse_to_one_pattern():
    candidate_id, evidence = corpus(
        ("V1", "UC_A", "MEAL PLAN guide"),
        ("V2", "UC_B", "meal plan Guide"),
        ("V3", "UC_C", "Meal Plan GUIDE"),
    )
    result = run(evidence, candidate_id)
    token = pattern_for(result, PatternKind.TITLE_TOKEN, "meal")
    assert token is not None
    assert token.video_count == 3
    assert token.distinct_channel_count == 3


def test_punctuation_differences_collapse_to_one_pattern():
    candidate_id, evidence = corpus(
        ("V1", "UC_A", "meal-plan: guide!"),
        ("V2", "UC_B", "meal plan — guide"),
        ("V3", "UC_C", "meal   plan ... guide"),
    )
    result = run(evidence, candidate_id)
    bigram = pattern_for(result, PatternKind.TITLE_BIGRAM, "meal plan")
    assert bigram is not None
    assert bigram.video_count == 3


def test_unicode_equivalent_strings_collapse_to_one_pattern():
    """NFC and NFD spellings, and fullwidth forms, are one token."""
    candidate_id, evidence = corpus(
        ("V1", "UC_A", "café planner"),          # café precomposed
        ("V2", "UC_B", "café planner"),          # cafe + combining acute
        ("V3", "UC_C", "ｃａｆｅ́ planner"),  # fullwidth
    )
    result = run(evidence, candidate_id)
    values = {p.value for p in result.patterns if p.kind is PatternKind.TITLE_TOKEN}
    cafe_like = [v for v in values if v.startswith("caf")]
    assert len(cafe_like) == 1, cafe_like
    assert pattern_for(result, PatternKind.TITLE_TOKEN, cafe_like[0]).video_count == 3


def test_identical_titles_are_counted_per_video_not_per_word():
    """A word repeated inside one title is one video's use of it."""
    candidate_id, evidence = corpus(
        ("V1", "UC_A", "meal meal meal plan"),
        ("V2", "UC_B", "meal plan"),
    )
    result = run(evidence, candidate_id)
    token = pattern_for(result, PatternKind.TITLE_TOKEN, "meal")
    assert token.video_count == 2
    assert len(token.occurrences) == 2


def test_normalization_helpers_are_deterministic_and_versioned():
    assert normalize_text("  Ｈｏｗ  to PLAN—meals!! ") == "how to plan meals"
    assert tokenize_title("How to Plan Meals for the Week") == (
        "plan", "meals", "week",
    )
    # Stopwords and single characters are removed, order preserved.
    assert tokenize_title("a b the plan") == ("plan",)


def test_duration_bands_tile_the_line_without_gaps_or_overlaps():
    boundaries = [0, 59, 60, 299, 300, 899, 900, 1799, 1800, 10_000]
    bands = [duration_band(s) for s in boundaries]
    assert bands == [
        DurationBand.UNDER_1_MIN, DurationBand.UNDER_1_MIN,
        DurationBand.ONE_TO_5_MIN, DurationBand.ONE_TO_5_MIN,
        DurationBand.FIVE_TO_15_MIN, DurationBand.FIVE_TO_15_MIN,
        DurationBand.FIFTEEN_TO_30_MIN, DurationBand.FIFTEEN_TO_30_MIN,
        DurationBand.OVER_30_MIN, DurationBand.OVER_30_MIN,
    ]
    # Every second in a wide sweep lands in exactly one band.
    assert all(duration_band(s) in set(DurationBand) for s in range(0, 4000, 7))


# ------------------------------------------------ missing and absent states


def test_no_public_content_evidence_is_unknown_not_patternless():
    candidate_id = uuid4()
    result = run([], candidate_id)
    assert result.pattern_state is ContentPatternsState.NO_CONTENT_OBSERVED
    assert result.state is DimensionState.UNKNOWN
    assert result.patterns == ()
    assert result.missing_reason == "no_public_content_observed_for_candidate"
    assert "measures the sample, not the field" in result.state_boundary


@pytest.mark.parametrize("reason", [NOT_REQUESTED, FAILED])
def test_capability_not_requested_or_failed_is_unavailable(reason):
    candidate_id = uuid4()
    result = run([], candidate_id, missing_reasons={PUBLIC_CONTENT: reason})
    assert result.pattern_state is ContentPatternsState.CONTENT_EVIDENCE_UNAVAILABLE
    assert result.state is DimensionState.MISSING
    assert result.missing_reason == reason
    assert "not evidence that the content field lacks patterns" in (
        result.state_boundary
    )


def test_another_capabilitys_failure_does_not_hide_content_patterns():
    candidate_id, evidence = corpus(
        ("V1", "UC_A", "meal plan"), ("V2", "UC_B", "meal plan")
    )
    result = run(evidence, candidate_id, missing_reasons={"marketplace": FAILED})
    assert result.pattern_state is ContentPatternsState.PATTERNS_OBSERVED


def test_videos_exist_but_all_metadata_unknown():
    candidate_id = uuid4()
    evidence: list[EvidenceItem] = []
    for index in range(3):
        evidence.extend(
            video_evidence(
                f"V{index}",
                candidate_id=candidate_id,
                title=None,
                tags=_UNSET,
                category=None,
                duration_seconds=_UNSET,
            )
        )
    result = run(evidence, candidate_id)
    assert result.pattern_state is ContentPatternsState.METADATA_UNAVAILABLE
    assert result.state is DimensionState.UNKNOWN
    assert result.observed_video_count == 3
    assert result.patterns == ()
    for entry in result.field_availability:
        assert entry.available_video_count == 0
        assert entry.unavailable_video_count == 3
    assert "never evidence that a pattern does not exist" in result.state_boundary


def test_missing_metadata_shrinks_the_denominator_rather_than_counting_as_absence():
    """A video with no title cannot dilute a title pattern's prevalence."""
    candidate_id = uuid4()
    evidence: list[EvidenceItem] = []
    for index in range(2):
        evidence.extend(
            video_evidence(
                f"T{index}", candidate_id=candidate_id,
                channel_id=f"UC_{index}", title="meal plan",
            )
        )
    for index in range(8):
        evidence.extend(
            video_evidence(
                f"N{index}", candidate_id=candidate_id,
                channel_id=f"UCN{index}", title=None,
            )
        )
    result = run(evidence, candidate_id)
    token = pattern_for(result, PatternKind.TITLE_TOKEN, "meal")
    assert result.observed_video_count == 10
    # Denominator is the 2 videos that HAD a title, not all 10.
    assert token.field_available_video_count == 2
    assert token.prevalence_share == 1.0
    title_availability = next(
        e for e in result.field_availability if e.field == "title"
    )
    assert title_availability.unavailable_video_count == 8


def test_absent_tags_and_observed_empty_tags_are_different():
    candidate_id = uuid4()
    absent = video_evidence("A", candidate_id=candidate_id, tags=_UNSET)
    empty = video_evidence("B", candidate_id=candidate_id, tags=[])
    result = run(absent + empty, candidate_id)
    tags_availability = next(
        e for e in result.field_availability if e.field == "tags"
    )
    # Only the explicitly empty list counts as an observation of "no tags".
    assert tags_availability.available_video_count == 1
    assert tags_availability.unavailable_video_count == 1


def test_one_video_only_yields_no_recurring_pattern():
    candidate_id, evidence = corpus(("V1", "UC_A", "meal plan guide"))
    result = run(evidence, candidate_id)
    assert result.pattern_state is ContentPatternsState.NO_RECURRING_PATTERNS
    assert result.state is DimensionState.UNKNOWN
    assert result.observed_video_count == 1
    assert result.patterns == ()
    assert result.missing_reason == "no_feature_recurred_across_observed_content"


def test_a_feature_seen_once_is_not_a_pattern():
    candidate_id, evidence = corpus(
        ("V1", "UC_A", "meal plan"), ("V2", "UC_B", "meal plan"),
        ("V3", "UC_C", "unique singular phrasing"),
    )
    result = run(evidence, candidate_id)
    assert pattern_for(result, PatternKind.TITLE_TOKEN, "meal").video_count >= (
        MIN_PATTERN_VIDEO_COUNT
    )
    assert pattern_for(result, PatternKind.TITLE_TOKEN, "singular") is None


# --------------------------------------------------- creator concentration


def test_one_creator_supplying_all_occurrences_is_flagged_single_creator():
    candidate_id = uuid4()
    evidence: list[EvidenceItem] = []
    for index in range(6):
        evidence.extend(
            video_evidence(
                f"V{index}", candidate_id=candidate_id,
                channel_id="UC_PROLIFIC", title="meal plan hack",
            )
        )
    result = run(evidence, candidate_id)
    token = pattern_for(result, PatternKind.TITLE_TOKEN, "meal")
    assert token.video_count == 6
    # The count alone would read as broad support; the creator count does not.
    assert token.distinct_channel_count == 1
    assert token.concentration is ConcentrationState.SINGLE_CREATOR
    assert token.top_channel_share == 1.0


def test_one_creator_supplying_most_occurrences_is_flagged_concentrated():
    candidate_id = uuid4()
    evidence: list[EvidenceItem] = []
    for index in range(8):
        evidence.extend(
            video_evidence(
                f"P{index}", candidate_id=candidate_id,
                channel_id="UC_PROLIFIC", title="meal plan",
            )
        )
    evidence.extend(
        video_evidence("O1", candidate_id=candidate_id, channel_id="UC_B",
                       title="meal plan")
    )
    result = run(evidence, candidate_id)
    token = pattern_for(result, PatternKind.TITLE_TOKEN, "meal")
    assert token.video_count == 9
    assert token.distinct_channel_count == 2
    assert token.concentration is ConcentrationState.CREATOR_CONCENTRATED
    assert token.top_channel_share >= 0.6


def test_a_spread_pattern_is_multi_creator():
    candidate_id, evidence = corpus(
        ("V1", "UC_A", "meal plan"), ("V2", "UC_B", "meal plan"),
        ("V3", "UC_C", "meal plan"), ("V4", "UC_D", "meal plan"),
    )
    result = run(evidence, candidate_id)
    token = pattern_for(result, PatternKind.TITLE_TOKEN, "meal")
    assert token.distinct_channel_count == 4
    assert token.concentration is ConcentrationState.MULTI_CREATOR


def test_videos_without_a_channel_are_not_merged_into_one_creator():
    candidate_id = uuid4()
    evidence: list[EvidenceItem] = []
    for index in range(4):
        evidence.extend(
            video_evidence(
                f"V{index}", candidate_id=candidate_id,
                channel_id=None, title="meal plan",
            )
        )
    result = run(evidence, candidate_id)
    token = pattern_for(result, PatternKind.TITLE_TOKEN, "meal")
    assert token.distinct_channel_count == 0
    assert token.videos_without_channel_id == 4
    assert token.top_channel_share is None
    assert token.concentration is ConcentrationState.CREATOR_UNKNOWN


# ------------------------------------ the evidence record is the authority


def test_a_payload_value_behind_a_non_observed_record_is_never_read():
    """The 'payload has a value but the record says UNKNOWN' case."""
    candidate_id = uuid4()
    evidence: list[EvidenceItem] = []
    for index in range(3):
        evidence.extend(
            video_evidence(
                f"U{index}", candidate_id=candidate_id,
                channel_id=f"UC_{index}", title="meal plan hack",
                truth_class=TruthClass.UNKNOWN,
            )
        )
    result = run(evidence, candidate_id)
    # The titles exist in the payloads and must not have been read.
    assert result.pattern_state is ContentPatternsState.NO_CONTENT_OBSERVED
    assert result.observed_video_count == 0
    assert result.patterns == ()


@pytest.mark.parametrize(
    "truth_class", [TruthClass.INFERRED, TruthClass.ESTIMATED, TruthClass.UNKNOWN]
)
def test_only_observed_content_records_contribute(truth_class):
    candidate_id = uuid4()
    observed = video_evidence(
        "OK1", candidate_id=candidate_id, channel_id="UC_A", title="meal plan"
    ) + video_evidence(
        "OK2", candidate_id=candidate_id, channel_id="UC_B", title="meal plan"
    )
    weak = video_evidence(
        "WEAK", candidate_id=candidate_id, channel_id="UC_C",
        title="meal plan", truth_class=truth_class,
    )
    result = run(observed + weak, candidate_id)
    assert result.observed_video_count == 2
    assert pattern_for(result, PatternKind.TITLE_TOKEN, "meal").video_count == 2


def test_evidence_for_another_candidate_is_out_of_scope():
    mine = uuid4()
    theirs = uuid4()
    evidence = video_evidence("A", candidate_id=mine, title="meal plan")
    evidence += video_evidence("B", candidate_id=theirs, title="meal plan")
    result = run(evidence, mine)
    assert result.observed_video_count == 1


def test_run_scoping_matches_milestone_5a():
    """An omitted run id admits only explicitly runless evidence."""
    candidate_id = uuid4()
    run_id = uuid4()
    stored = video_evidence(
        "S", candidate_id=candidate_id, title="meal plan", research_run_id=run_id
    )
    inline = video_evidence("I", candidate_id=candidate_id, title="meal plan")
    both = stored + inline

    runless = run(both, candidate_id)
    assert runless.observed_video_count == 1
    assert runless.provenance.video_ids == ("I",)

    scoped = run(both, candidate_id, research_run_id=run_id)
    assert scoped.observed_video_count == 1
    assert scoped.provenance.video_ids == ("S",)


# ----------------------------------------------------- duplicates and order


def test_repeated_evidence_does_not_inflate_a_pattern():
    candidate_id, evidence = corpus(
        ("V1", "UC_A", "meal plan"), ("V2", "UC_B", "meal plan")
    )
    once = run(evidence, candidate_id)
    twice = run(evidence + evidence, candidate_id)
    assert once.observed_video_count == twice.observed_video_count == 2
    assert (
        pattern_for(once, PatternKind.TITLE_TOKEN, "meal").video_count
        == pattern_for(twice, PatternKind.TITLE_TOKEN, "meal").video_count
        == 2
    )
    assert twice.provenance.duplicate_evidence_suppressed > 0


def test_duplicate_video_identity_across_records_is_one_video():
    """Two distinct records describing the same video id are one video."""
    candidate_id = uuid4()
    first = video_evidence(
        "SAME", candidate_id=candidate_id, channel_id="UC_A",
        title="meal plan", payload_hash="hash-1",
    )
    second = video_evidence(
        "SAME", candidate_id=candidate_id, channel_id="UC_A",
        title="meal plan", payload_hash="hash-2",
    )
    other = video_evidence(
        "OTHER", candidate_id=candidate_id, channel_id="UC_B", title="meal plan"
    )
    result = run(first + second + other, candidate_id)
    assert result.observed_video_count == 2
    token = pattern_for(result, PatternKind.TITLE_TOKEN, "meal")
    assert token.video_count == 2
    assert {o.video_id for o in token.occurrences} == {"SAME", "OTHER"}


def test_later_records_never_overwrite_an_established_field():
    """Re-delivery cannot change a video's metadata."""
    candidate_id = uuid4()
    first = video_evidence(
        "V", candidate_id=candidate_id, title="original phrasing",
        payload_hash="h1",
    )
    conflicting = video_evidence(
        "V", candidate_id=candidate_id, title="completely different words",
        payload_hash="h2",
    )
    forward = run(first + conflicting, candidate_id)
    assert forward.observed_video_count == 1
    # Only the first OBSERVED record's title contributed.
    tokens = {p.value for p in forward.patterns if p.kind is PatternKind.TITLE_TOKEN}
    assert "different" not in tokens


def _identity(result) -> tuple:
    return (
        result.state,
        result.pattern_state,
        result.missing_reason,
        result.observed_video_count,
        result.distinct_channel_count,
        result.videos_without_channel_id,
        result.outlier_observation_count,
        result.outlier_distinct_channel_count,
        result.outlier_evidence_available,
        tuple(
            (e.field, e.available_video_count, e.unavailable_video_count)
            for e in result.field_availability
        ),
        tuple(
            (
                p.kind,
                p.value,
                p.video_count,
                p.field_available_video_count,
                p.prevalence_share,
                p.distinct_channel_count,
                p.videos_without_channel_id,
                p.top_channel_share,
                p.concentration,
                tuple((o.video_id, o.channel_id, o.evidence_ids) for o in p.occurrences),
                p.evidence_ids,
                (
                    p.cooccurrence.state,
                    p.cooccurrence.outlier_video_count,
                    p.cooccurrence.outlier_distinct_channel_count,
                    p.cooccurrence.comparable_outlier_total,
                    p.cooccurrence.share_among_outliers,
                    p.cooccurrence.share_in_corpus,
                ),
            )
            for p in result.patterns
        ),
        result.provenance.evidence_ids,
        result.provenance.video_ids,
        result.provenance.channel_ids,
        result.provenance.duplicate_evidence_suppressed,
    )


def _mixed_corpus(candidate_id: UUID) -> list[EvidenceItem]:
    plan = [
        ("V0", "UC_A", "meal plan guide for busy parents", 300, ["meal", "plan"]),
        ("V1", "UC_A", "meal plan guide part two", 600, ["meal", "plan"]),
        ("V2", "UC_B", "MEAL PLAN hacks!", 45, ["meal"]),
        ("V3", "UC_C", "weekly meal plan", 1200, []),
        ("V4", "UC_D", "budget meal plan", 2000, ["budget"]),
        ("V5", "UC_E", None, 700, ["meal"]),
        ("V6", "UC_F", "grocery list template", 500, None),
        ("V7", None, "meal plan template", 800, ["meal"]),
    ]
    evidence: list[EvidenceItem] = []
    for video_id, channel, title, duration, tags in plan:
        evidence.extend(
            video_evidence(
                video_id, candidate_id=candidate_id, channel_id=channel,
                title=title, duration_seconds=duration,
                tags=_UNSET if tags is None else tags,
            )
        )
    return evidence


def test_output_is_identical_under_1000_arrival_order_permutations():
    candidate_id = uuid4()
    evidence = _mixed_corpus(candidate_id)
    baseline = _identity(run(evidence, candidate_id))
    rng = random.Random(20260911)
    mismatches = 0
    for _ in range(1000):
        shuffled = list(evidence)
        rng.shuffle(shuffled)
        if _identity(run(shuffled, candidate_id)) != baseline:
            mismatches += 1
    assert mismatches == 0


def test_reversed_and_repeated_input_are_identical():
    candidate_id = uuid4()
    evidence = _mixed_corpus(candidate_id)
    baseline = _identity(run(evidence, candidate_id))
    assert _identity(run(list(reversed(evidence)), candidate_id)) == baseline
    for _ in range(5):
        assert _identity(run(evidence, candidate_id)) == baseline


def test_patterns_are_ordered_canonically_and_never_by_count():
    candidate_id = uuid4()
    result = run(_mixed_corpus(candidate_id), candidate_id)
    ordered = [(p.kind.value, p.value) for p in result.patterns]
    assert ordered == sorted(ordered)
    counts = [p.video_count for p in result.patterns]
    # If ordering were by count the sequence would be monotonic; a canonical
    # ordering by (kind, value) will not be for this corpus.
    assert counts != sorted(counts, reverse=True)


def test_provenance_is_canonical_and_traceable():
    candidate_id = uuid4()
    evidence = _mixed_corpus(candidate_id)
    result = run(evidence, candidate_id)
    provenance = result.provenance
    assert list(provenance.evidence_ids) == sorted(provenance.evidence_ids)
    assert list(provenance.video_ids) == sorted(provenance.video_ids)
    assert provenance.providers == ("youtube",)
    assert provenance.platforms == ("youtube",)
    assert provenance.source_truth_classes == ("OBSERVED",)
    # Every pattern traces back to real contributing records and videos.
    content_ids = {
        item.id for item in evidence
        if item.signal_type == "public_content_observation"
    }
    for pattern in result.patterns:
        assert pattern.evidence_ids
        assert set(pattern.evidence_ids) <= content_ids
        assert {o.video_id for o in pattern.occurrences} <= set(provenance.video_ids)
        assert len(pattern.occurrences) == pattern.video_count


# -------------------------------------------------- outlier co-occurrence


def outlier_corpus(candidate_id: UUID, *, winners: dict[str, int]) -> list[EvidenceItem]:
    """A corpus where named videos sit above their creator's own median.

    Each creator gets three videos so Milestone 5A can form a baseline.
    """
    evidence: list[EvidenceItem] = []
    for channel, (video_ids, views) in winners.items():
        for video_id, view_count, title in zip(video_ids, views, _TITLES[channel]):
            evidence.extend(
                video_evidence(
                    video_id, candidate_id=candidate_id, channel_id=channel,
                    title=title, views=view_count,
                )
            )
    return evidence


_TITLES: dict[str, list[str]] = {}


def _build_outlier_scenario(
    candidate_id: UUID, creators: int, hook_in_winner: bool
) -> list[EvidenceItem]:
    """Three videos per creator; the highest-view one optionally carries a hook."""
    evidence: list[EvidenceItem] = []
    for index in range(creators):
        channel = f"UC_{index}"
        winner_title = (
            "secret hook meal plan" if hook_in_winner else "ordinary meal plan"
        )
        rows = [
            (f"W{index}", 10_000, winner_title),
            (f"L{index}a", 100, "ordinary meal plan"),
            (f"L{index}b", 90, "ordinary meal plan"),
        ]
        for video_id, views, title in rows:
            evidence.extend(
                video_evidence(
                    video_id, candidate_id=candidate_id, channel_id=channel,
                    title=title, views=views,
                )
            )
    return evidence


def test_5a_unavailable_still_extracts_patterns():
    """Co-occurrence is a distinct reported state, never a blocker."""
    candidate_id, evidence = corpus(
        ("V1", "UC_A", "meal plan"), ("V2", "UC_B", "meal plan")
    )
    result = run(evidence, candidate_id, outlier_evidence=None)
    assert result.pattern_state is ContentPatternsState.PATTERNS_OBSERVED
    assert result.outlier_evidence_available is False
    token = pattern_for(result, PatternKind.TITLE_TOKEN, "meal")
    assert token.cooccurrence.state is CooccurrenceState.OUTLIER_EVIDENCE_UNAVAILABLE
    assert token.cooccurrence.share_among_outliers is None
    # Prevalence is unaffected by 5A's absence.
    assert token.cooccurrence.share_in_corpus == token.prevalence_share


def test_5a_available_but_no_qualifying_outliers():
    """Every video at its creator's median: nothing sits above baseline."""
    candidate_id = uuid4()
    evidence: list[EvidenceItem] = []
    for index in range(3):
        channel = f"UC_{index}"
        for suffix in ("a", "b", "c"):
            evidence.extend(
                video_evidence(
                    f"{channel}{suffix}", candidate_id=candidate_id,
                    channel_id=channel, title="meal plan", views=500,
                )
            )
    outlier = derive_robust_content_intelligence(
        candidate_id=candidate_id, evidence=evidence
    )
    result = run(evidence, candidate_id, outlier_evidence=outlier)
    assert result.outlier_evidence_available is True
    assert result.outlier_observation_count == 0
    token = pattern_for(result, PatternKind.TITLE_TOKEN, "meal")
    assert token.cooccurrence.state is CooccurrenceState.NO_QUALIFYING_OUTLIERS
    assert token.cooccurrence.outlier_video_count == 0


def test_cooccurrence_without_enough_independent_creators_is_withheld():
    """Two creators cannot establish anything about the sampled field."""
    candidate_id = uuid4()
    evidence = _build_outlier_scenario(candidate_id, creators=2, hook_in_winner=True)
    outlier = derive_robust_content_intelligence(
        candidate_id=candidate_id, evidence=evidence
    )
    result = run(evidence, candidate_id, outlier_evidence=outlier)
    hook = pattern_for(result, PatternKind.TITLE_TOKEN, "secret")
    assert hook is not None
    assert hook.cooccurrence.outlier_distinct_channel_count < (
        MIN_INDEPENDENT_CREATORS_FOR_COOCCURRENCE
    )
    assert hook.cooccurrence.state is (
        CooccurrenceState.INSUFFICIENT_INDEPENDENT_CREATORS
    )


def test_cooccurrence_reported_when_enough_independent_creators_carry_it():
    candidate_id = uuid4()
    evidence = _build_outlier_scenario(candidate_id, creators=4, hook_in_winner=True)
    outlier = derive_robust_content_intelligence(
        candidate_id=candidate_id, evidence=evidence
    )
    result = run(evidence, candidate_id, outlier_evidence=outlier)
    hook = pattern_for(result, PatternKind.TITLE_TOKEN, "secret")
    assert hook.cooccurrence.outlier_distinct_channel_count == 4
    assert hook.cooccurrence.state is CooccurrenceState.OVER_REPRESENTED
    # The two shares are reported side by side, never fused.
    assert hook.cooccurrence.share_among_outliers == 1.0
    assert hook.cooccurrence.share_in_corpus == round(4 / 12, 4)
    assert hook.cooccurrence.share_among_outliers != hook.cooccurrence.share_in_corpus


def test_a_pattern_absent_among_outliers_is_reported_as_such():
    candidate_id = uuid4()
    evidence = _build_outlier_scenario(candidate_id, creators=4, hook_in_winner=False)
    # Add a token that appears only on non-winners.
    for index in range(4):
        evidence.extend(
            video_evidence(
                f"X{index}", candidate_id=candidate_id, channel_id=f"UC_{index}",
                title="quiet rarely watched phrasing", views=50,
            )
        )
    outlier = derive_robust_content_intelligence(
        candidate_id=candidate_id, evidence=evidence
    )
    result = run(evidence, candidate_id, outlier_evidence=outlier)
    quiet = pattern_for(result, PatternKind.TITLE_TOKEN, "quiet")
    assert quiet is not None
    assert quiet.cooccurrence.state is CooccurrenceState.ABSENT_AMONG_OUTLIERS
    assert quiet.cooccurrence.outlier_video_count == 0


def test_prevalence_and_cooccurrence_are_separate_structures():
    """Neither is computable from the other, and neither is a combined score."""
    candidate_id = uuid4()
    evidence = _build_outlier_scenario(candidate_id, creators=4, hook_in_winner=True)
    outlier = derive_robust_content_intelligence(
        candidate_id=candidate_id, evidence=evidence
    )
    result = run(evidence, candidate_id, outlier_evidence=outlier)
    for pattern in result.patterns:
        # Prevalence lives on the pattern; co-occurrence lives on its own
        # record with its own denominator.
        assert pattern.field_available_video_count >= pattern.video_count
        assert pattern.cooccurrence.comparable_outlier_total <= (
            pattern.field_available_video_count
        )
        # No combined figure exists anywhere on either structure.
        names = set(type(pattern.cooccurrence).__dataclass_fields__)
        assert not any(
            word in name
            for name in names
            for word in ("score", "strength", "rank", "quality", "lift", "index")
        )


def test_the_outlier_denominator_excludes_videos_whose_field_was_unavailable():
    """An outlier with no title cannot dilute a title pattern's share."""
    candidate_id = uuid4()
    evidence = _build_outlier_scenario(candidate_id, creators=4, hook_in_winner=True)
    # A fifth creator whose winner has no title at all.
    for video_id, views, title in (
        ("WN", 10_000, None), ("LNa", 100, None), ("LNb", 90, None),
    ):
        evidence.extend(
            video_evidence(
                video_id, candidate_id=candidate_id, channel_id="UC_NOTITLE",
                title=title, views=views,
            )
        )
    outlier = derive_robust_content_intelligence(
        candidate_id=candidate_id, evidence=evidence
    )
    result = run(evidence, candidate_id, outlier_evidence=outlier)
    hook = pattern_for(result, PatternKind.TITLE_TOKEN, "secret")
    # Five winners exist, but only four could have carried a title.
    assert result.outlier_observation_count == 5
    assert hook.cooccurrence.comparable_outlier_total == 4
    assert hook.cooccurrence.share_among_outliers == 1.0


# ------------------------------------------------------- integrity guards


def _executable_source() -> str:
    tree = ast.parse(MODULE_PATH.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            node.value = ast.Constant(value="")
    return ast.unparse(tree)


def _names_read() -> set:
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


def test_module_cannot_read_subscriber_or_channel_aggregate_fields():
    forbidden = {
        "channel_subscriber_count",
        "channel_view_count",
        "channel_video_count",
        "subscriber_count",
        "subscribers",
    }
    read = _names_read()
    assert not (read & forbidden), sorted(read & forbidden)


def test_module_cannot_read_engagement_magnitudes():
    forbidden = {"view_count", "like_count", "comment_count", "views", "likes"}
    read = _names_read()
    assert not (read & forbidden), sorted(read & forbidden)


def test_module_reads_only_the_declared_payload_keys():
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
    assert keys == {"tags", "duration_seconds"}, keys


def test_module_does_not_reach_scoring_machinery():
    code = _executable_source()
    for token in ("RED", "YELLOW", "GREEN"):
        assert not re.search(rf"\b{token}\b", code), token
    for token in (
        "score_opportunity", "ScoreDimensions", "Classification",
        "opportunity_score", "evidence_confidence",
    ):
        assert token not in code, token


def test_module_imports_only_5a_and_shared_primitives():
    tree = ast.parse(MODULE_PATH.read_text())
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
    for banned in (
        "scoring", "purchase_evidence", "price_evidence", "buyer_reach",
        "competition_opportunity", "audience_attention", "product_specification",
        "product_job_fit", "search_demand", "marketplace", "public_content_features",
    ):
        assert not any(banned in module for module in modules), banned
    assert any("faceless_content_intelligence" in m for m in modules)


def test_no_emitted_string_makes_a_causal_or_predictive_claim():
    candidate_id = uuid4()
    scenarios = [
        ([], {}, None),
        ([], {PUBLIC_CONTENT: FAILED}, None),
        (_mixed_corpus(candidate_id), {}, None),
    ]
    outlier_case = _build_outlier_scenario(candidate_id, creators=4, hook_in_winner=True)
    scenarios.append(
        (
            outlier_case,
            {},
            derive_robust_content_intelligence(
                candidate_id=candidate_id, evidence=outlier_case
            ),
        )
    )
    banned = (
        r"performs better", r"drives views", r"causes?", r"virality",
        r"goes? viral", r"winning hook", r"increases? engagement", r"boosts?",
        r"will perform", r"proven", r"guarantee\w*", r"best[- ]performing",
        r"optimi[sz]e\w*", r"recommend\w*",
    )
    # Negators that turn a mention into a refusal. The prose is REQUIRED to
    # name these claims in order to disclaim them, so a blunt vocabulary scan
    # would fire on the disclaimers themselves. The property that actually
    # matters is that every causal term appears inside a negated clause.
    negators = r"(?:not|never|no|cannot|refus\w*|neither|nor)"

    for evidence, reasons, outlier in scenarios:
        result = run(
            evidence, candidate_id, missing_reasons=reasons, outlier_evidence=outlier
        )

        # 1. DERIVED VALUES are data and must carry no claim vocabulary at
        #    all: there is no legitimate reason for a pattern value, a
        #    concentration state or a co-occurrence state to contain one.
        derived_parts = (
            [p.value for p in result.patterns]
            + [p.kind.value for p in result.patterns]
            + [p.concentration.value for p in result.patterns]
            + [p.cooccurrence.state.value for p in result.patterns]
            + [state.value for state in CooccurrenceState]
            + [state.value for state in ConcentrationState]
            + [state.value for state in ContentPatternsState]
            + [kind.value for kind in PatternKind]
            + [result.pattern_state.value, result.dimension_name]
        )
        # Enum values are SCREAMING_SNAKE, so a two-word claim renamed into
        # PERFORMS_BETTER would slip past a scan for "performs better". A
        # mutation proved exactly that, so separators are folded to spaces
        # and the whole enum surface is scanned, not only the states this
        # particular corpus happened to produce.
        derived = " ".join(derived_parts).replace("_", " ")
        for term in banned:
            assert not re.search(rf"\b{term}\b", derived, re.IGNORECASE), (
                term, derived[:160],
            )

        # 2. PROSE may name a claim only to refuse it. Every occurrence must
        #    sit within a short window after a negator.
        for prose in [result.state_boundary, *result.limitations]:
            for term in banned:
                for match in re.finditer(rf"\b{term}\b", prose, re.IGNORECASE):
                    window = prose[max(0, match.start() - 90) : match.start()]
                    assert re.search(rf"\b{negators}\b", window, re.IGNORECASE), (
                        term,
                        prose,
                    )


def test_no_numeric_score_of_any_kind_is_emitted():
    candidate_id = uuid4()
    evidence = _build_outlier_scenario(candidate_id, creators=4, hook_in_winner=True)
    outlier = derive_robust_content_intelligence(
        candidate_id=candidate_id, evidence=evidence
    )
    result = run(evidence, candidate_id, outlier_evidence=outlier)
    assert result.value is None
    assert result.dimension_name == DIM_CONTENT_PATTERNS
    assert result.dimension_name == "preliminary_content_patterns"
    # Every emitted ratio is a share in [0, 1], never a 0-100 score.
    for pattern in result.patterns:
        assert 0.0 <= pattern.prevalence_share <= 1.0
        if pattern.top_channel_share is not None:
            assert 0.0 <= pattern.top_channel_share <= 1.0
        for share in (
            pattern.cooccurrence.share_among_outliers,
            pattern.cooccurrence.share_in_corpus,
        ):
            assert share is None or 0.0 <= share <= 1.0
    names = set(type(result).__dataclass_fields__)
    assert not any(
        word in name for name in names for word in ("score", "rank", "strength")
    )


def test_every_state_states_what_it_does_not_establish():
    for state in ContentPatternsState:
        assert state in STATE_BOUNDARIES, state
        assert STATE_BOUNDARIES[state].strip()


def test_versions_are_reported():
    candidate_id, evidence = corpus(
        ("V1", "UC_A", "meal plan"), ("V2", "UC_B", "meal plan")
    )
    result = run(evidence, candidate_id)
    assert result.version == CONTENT_PATTERNS_VERSION
    assert result.normalization_version == "content_normalization_v1"
    assert result.duration_band_version == "duration_band_v1"
    assert result.cooccurrence_version == "outlier_cooccurrence_v1"
    assert result.features_truth_class is TruthClass.INFERRED


def test_5a_semantics_are_untouched_by_5b():
    """Running 5B must not mutate the 5A result it consumed."""
    candidate_id = uuid4()
    evidence = _build_outlier_scenario(candidate_id, creators=4, hook_in_winner=True)
    outlier = derive_robust_content_intelligence(
        candidate_id=candidate_id, evidence=evidence
    )
    before = replace(outlier)
    run(evidence, candidate_id, outlier_evidence=outlier)
    assert outlier == before


# ---------------------------------------------------------------------- API


def test_no_endpoint_was_added_and_score_remains_gone():
    from fastapi.testclient import TestClient

    from app.main import app

    assert len({route.path for route in app.routes}) == 15
    assert TestClient(app).post("/score", json={}).status_code == 410


def test_5b_is_not_wired_into_any_route():
    """5B stays at the service layer, matching 5A's boundary."""
    import app.api.routes as routes

    assert not hasattr(routes, "derive_content_patterns")
    source = Path("app/api/routes.py").read_text()
    assert "content_patterns" not in source
