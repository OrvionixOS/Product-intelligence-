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
import unicodedata
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
    CONCENTRATION_SHARE,
    ConcentrationState,
    ContentPatternsState,
    CooccurrenceState,
    DurationBand,
    FieldState,
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

from tests.route_surface import assert_route_surface_unchanged

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


def test_unicode_equivalent_strings_collapse_to_one_preserved_token():
    """NFC, NFD and fullwidth spellings are ONE token, and it keeps its accent.

    The earlier version asserted only that the surviving token started with
    "caf", which passes even when normalization has DELETED the accent. It
    was written around a defect rather than against it.
    """
    candidate_id, evidence = corpus(
        ("V1", "UC_A", unicodedata.normalize("NFC", "café planner")),
        ("V2", "UC_B", unicodedata.normalize("NFD", "café planner")),
        ("V3", "UC_C", "ｃａｆｅ́ planner"),
    )
    result = run(evidence, candidate_id)
    token = pattern_for(result, PatternKind.TITLE_TOKEN, "café")
    assert token is not None, sorted(
        p.value for p in result.patterns if p.kind is PatternKind.TITLE_TOKEN
    )
    assert token.video_count == 3
    assert token.distinct_channel_count == 3


def test_accented_latin_text_is_preserved_not_stripped():
    """Accents are content. Deleting them changes what was observed."""
    assert normalize_text("café") == "café"
    assert normalize_text("naïve résumé") == "naïve résumé"
    assert normalize_text("Ünïcödé plan") == "ünïcödé plan"
    assert tokenize_title("naïve résumé guide") == (
        "naïve", "résumé", "guide",
    )


@pytest.mark.parametrize(
    ("script", "text", "expected"),
    [
        ("Japanese", "日本語のタイトル 計画",
         ("日本語のタイトル", "計画")),
        ("Arabic", "مرحبا بالعالم",
         ("مرحبا", "بالعالم")),
        # Greek final sigma casefolds to sigma by design, so the same word
        # in medial and final position collapses to one token.
        ("Greek", "Σχέδιο Γεύματος",
         ("σχέδιο", "γεύματοσ")),
        ("Cyrillic", "План питания",
         ("план", "питания")),
    ],
)
def test_non_latin_scripts_survive_normalization(script, text, expected):
    """A non-Latin title must not normalize to the empty string.

    An earlier version stripped every non-ASCII character, so Japanese and
    Arabic titles vanished entirely and those videos silently contributed no
    title pattern at all.
    """
    assert normalize_text(text) != ""
    assert tokenize_title(text) == expected, (script, tokenize_title(text))


def test_non_latin_patterns_are_extracted_and_order_independent():
    candidate_id = uuid4()
    titles = [
        "日本語のタイトル 計画",
        "日本語のタイトル 計画",
        "مرحبا بالعالم",
        "مرحبا بالعالم",
    ]
    evidence: list[EvidenceItem] = []
    for index, title in enumerate(titles):
        evidence.extend(
            video_evidence(
                f"V{index}", candidate_id=candidate_id,
                channel_id=f"UC_{index}", title=title,
            )
        )
    result = run(evidence, candidate_id)
    values = {p.value for p in result.patterns if p.kind is PatternKind.TITLE_TOKEN}
    assert "日本語のタイトル" in values
    assert "مرحبا" in values

    baseline = _identity(result)
    rng = random.Random(4)
    for _ in range(200):
        shuffled = list(evidence)
        rng.shuffle(shuffled)
        assert _identity(run(shuffled, candidate_id)) == baseline


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


@pytest.mark.parametrize(
    "purpose",
    [
        EvidencePurpose.AUDIENCE,
        EvidencePurpose.COMPETITION,
        EvidencePurpose.PURCHASE,
        EvidencePurpose.QUALITATIVE,
    ],
)
def test_a_content_signal_stored_under_another_purpose_is_not_read(purpose):
    """Signal type alone does not admit a record; the stored purpose must agree.

    Every fixture in this file pairs `public_content_observation` with
    `CONTENT`, so the purpose filter had nothing constraining it: a mutation
    deleting the check survived the whole suite. A record can legitimately
    carry a content signal type under a different purpose — 5A's own
    per-video records are AUDIENCE — and reading one here would take an
    observation collected for one question as evidence for another.
    """
    candidate_id = uuid4()
    observed = video_evidence(
        "OK1", candidate_id=candidate_id, channel_id="UC_A", title="meal plan"
    ) + video_evidence(
        "OK2", candidate_id=candidate_id, channel_id="UC_B", title="meal plan"
    )
    off_purpose = [
        item.model_copy(update={"purpose": purpose})
        for item in video_evidence(
            "OFF", candidate_id=candidate_id, channel_id="UC_C", title="meal plan"
        )
        if item.signal_type == "public_content_observation"
    ]
    assert off_purpose[0].truth_class is TruthClass.OBSERVED
    assert off_purpose[0].raw_payload["title"] == "meal plan"

    result = run(observed + off_purpose, candidate_id)
    assert result.observed_video_count == 2
    pattern = pattern_for(result, PatternKind.TITLE_TOKEN, "meal")
    assert pattern.video_count == 2
    assert [o.video_id for o in pattern.occurrences] == ["OK1", "OK2"]


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


# Fixed ids so "smallest" is unambiguous and independent of uuid4().
_ID_SMALL = UUID("00000000-0000-4000-8000-00000000000a")
_ID_MIDDLE = UUID("88888888-8888-4888-8888-88888888888b")
_ID_LARGE = UUID("ffffffff-ffff-4fff-8fff-ffffffffffff")


def _duplicate_content_rows(
    candidate_id: UUID, video_id: str, item_ids, *, hashed: bool = True, **kwargs
) -> list[EvidenceItem]:
    """Byte-identical OBSERVED content rows for one video, distinct row ids.

    The payload is the same object for every row, so the payload hash is the
    same too; only the evidence id differs. This is what re-collecting one
    video across two queries or snapshots actually produces.

    `hashed=False` strips the payload hash, since `video_evidence` always
    substitutes one and a record without a hash is never collapsed.
    """
    template = next(
        item
        for item in video_evidence(video_id, candidate_id=candidate_id, **kwargs)
        if item.signal_type == "public_content_observation"
    )
    update: dict[str, Any] = {} if hashed else {"raw_payload_hash": None}
    return [
        template.model_copy(update={**update, "id": item_id})
        for item_id in item_ids
    ]


def test_duplicate_rows_retain_the_smallest_evidence_id_not_the_first_seen():
    """Suppression must not make LINEAGE depend on arrival order.

    Identical payloads hash identically but occupy distinct rows. A
    first-wins rule left every count, share, state and pattern correct while
    the retained evidence id — and therefore the audit trail — flipped when
    the same records arrived in a different order.
    """
    candidate_id = uuid4()
    dupes = _duplicate_content_rows(
        candidate_id, "SAME", [_ID_LARGE, _ID_SMALL, _ID_MIDDLE],
        channel_id="UC_A", title="meal plan guide",
    )
    # One fingerprint, three rows.
    assert len({d.raw_payload_hash for d in dupes}) == 1
    assert len({d.id for d in dupes}) == 3
    peer = _duplicate_content_rows(
        candidate_id, "PEER", [uuid4()], channel_id="UC_B", title="meal plan guide"
    )
    evidence = dupes + peer

    result = run(evidence, candidate_id)
    assert result.provenance.duplicate_evidence_suppressed == 2
    assert result.observed_video_count == 2
    # The smallest id is retained; the other two appear nowhere in lineage.
    assert _ID_SMALL in result.provenance.evidence_ids
    assert _ID_MIDDLE not in result.provenance.evidence_ids
    assert _ID_LARGE not in result.provenance.evidence_ids
    token = pattern_for(result, PatternKind.TITLE_TOKEN, "meal")
    same = next(o for o in token.occurrences if o.video_id == "SAME")
    assert same.evidence_ids == (_ID_SMALL,)
    assert _ID_SMALL in token.evidence_ids


def test_duplicate_fingerprints_are_order_independent_forward_reversed_shuffled():
    candidate_id = uuid4()
    evidence = _duplicate_content_rows(
        candidate_id, "SAME", [_ID_LARGE, _ID_SMALL, _ID_MIDDLE],
        channel_id="UC_A", title="meal plan guide",
    ) + _duplicate_content_rows(
        candidate_id, "PEER", [uuid4(), uuid4()],
        channel_id="UC_B", title="meal plan guide",
    )
    baseline = _identity(run(evidence, candidate_id))
    assert _identity(run(list(reversed(evidence)), candidate_id)) == baseline

    rng = random.Random(5150)
    for _ in range(300):
        shuffled = list(evidence)
        rng.shuffle(shuffled)
        assert _identity(run(shuffled, candidate_id)) == baseline


def test_the_same_record_supplied_twice_is_still_one_record():
    """Identical ids, not merely identical payloads, stay a single record."""
    candidate_id = uuid4()
    row = _duplicate_content_rows(
        candidate_id, "SAME", [_ID_SMALL], channel_id="UC_A", title="meal plan"
    )
    peer = _duplicate_content_rows(
        candidate_id, "PEER", [_ID_LARGE], channel_id="UC_B", title="meal plan"
    )
    result = run(row + row + peer, candidate_id)
    assert result.observed_video_count == 2
    token = pattern_for(result, PatternKind.TITLE_TOKEN, "meal")
    same = next(o for o in token.occurrences if o.video_id == "SAME")
    assert same.evidence_ids == (_ID_SMALL,)
    assert result.provenance.evidence_ids == tuple(sorted((_ID_SMALL, _ID_LARGE)))


def test_records_without_a_payload_hash_are_never_collapsed():
    """No hash means no identity claim, so multiplicity stays in lineage.

    The fingerprint is only trustworthy when a payload hash exists. Without
    one, two records cannot be shown to describe the same observation, so
    neither may be suppressed: collapsing them would drop an evidence id and
    inflate `duplicate_evidence_suppressed` for records that still contribute.
    Milestone 5A keeps the same rule.
    """
    candidate_id = uuid4()
    rows = _duplicate_content_rows(
        candidate_id, "SAME", [_ID_SMALL, _ID_LARGE],
        channel_id="UC_A", title="meal plan", hashed=False,
    )
    peer = _duplicate_content_rows(
        candidate_id, "PEER", [_ID_MIDDLE], channel_id="UC_B", title="meal plan",
        hashed=False,
    )
    assert all(r.raw_payload_hash is None for r in rows + peer)

    result = run(rows + peer, candidate_id)
    assert result.provenance.duplicate_evidence_suppressed == 0
    assert result.observed_video_count == 2
    token = pattern_for(result, PatternKind.TITLE_TOKEN, "meal")
    same = next(o for o in token.occurrences if o.video_id == "SAME")
    # BOTH ids survive; neither record was treated as a duplicate of the other.
    assert same.evidence_ids == tuple(sorted((_ID_SMALL, _ID_LARGE)))
    assert set(result.provenance.evidence_ids) == {_ID_SMALL, _ID_LARGE, _ID_MIDDLE}


def test_hashless_records_that_disagree_still_conflict():
    """Un-hashed records are compared on content, not collapsed by identity."""
    candidate_id = uuid4()
    first = _duplicate_content_rows(
        candidate_id, "SAME", [_ID_SMALL], channel_id="UC_A",
        title="alpha bravo", hashed=False,
    )
    second = _duplicate_content_rows(
        candidate_id, "SAME", [_ID_LARGE], channel_id="UC_B",
        title="delta echo", hashed=False,
    )
    peer = _duplicate_content_rows(
        candidate_id, "PEER", [_ID_MIDDLE], channel_id="UC_C",
        title="alpha bravo delta echo", hashed=False,
    )
    result = run(first + second + peer, candidate_id)
    assert result.provenance.duplicate_evidence_suppressed == 0
    same = next(
        o for p in result.patterns for o in p.occurrences if o.video_id == "SAME"
    )
    assert same.channel_state is FieldState.CONFLICTING
    assert same.channel_id is None
    assert same.evidence_ids == tuple(sorted((_ID_SMALL, _ID_LARGE)))


def _conflicting_corpus(candidate_id: UUID) -> list[EvidenceItem]:
    """Two distinct OBSERVED records disagreeing about the SAME video.

    Both are non-duplicate (different payload hashes) so neither is
    suppressed, and they disagree on title, channel, tags, category and
    duration at once.
    """
    first = video_evidence(
        "SAME", candidate_id=candidate_id, channel_id="UC_A",
        title="alpha bravo charlie", tags=["alpha"], category="Education",
        duration_seconds=100, payload_hash="h1",
    )
    second = video_evidence(
        "SAME", candidate_id=candidate_id, channel_id="UC_B",
        title="delta echo foxtrot", tags=["delta"], category="Entertainment",
        duration_seconds=2000, payload_hash="h2",
    )
    peer = video_evidence(
        "PEER", candidate_id=candidate_id, channel_id="UC_C",
        title="alpha bravo delta echo", tags=["alpha", "delta"],
        category="Education", duration_seconds=100, payload_hash="h3",
    )
    return first + second + peer


def test_conflicting_observed_records_are_reported_not_silently_resolved():
    """Disagreeing OBSERVED records make the field CONFLICTING, not first-wins.

    An earlier version took the first record to supply a field, which made
    the selected metadata -- and therefore the patterns, the availability
    counts and the creator concentration -- depend on arrival order.
    """
    candidate_id = uuid4()
    result = run(_conflicting_corpus(candidate_id), candidate_id)

    assert result.observed_video_count == 2
    tokens = {p.value for p in result.patterns if p.kind is PatternKind.TITLE_TOKEN}
    assert "charlie" not in tokens
    assert "foxtrot" not in tokens

    for field in ("title", "tags", "category", "duration_seconds"):
        entry = next(e for e in result.field_availability if e.field == field)
        assert entry.conflicting_video_count == 1, field
        assert entry.available_video_count == 1, field
        assert entry.unavailable_video_count == 0, field
        assert (
            entry.available_video_count
            + entry.unavailable_video_count
            + entry.conflicting_video_count
            == result.observed_video_count
        )

    assert result.videos_with_conflicting_channel_id == 1
    assert result.videos_without_channel_id == 0
    assert result.provenance.channel_ids == ("UC_C",)


def test_conflicting_records_are_order_independent_forward_reversed_shuffled():
    """The blocker: same evidence, any order, byte-identical result."""
    candidate_id = uuid4()
    evidence = _conflicting_corpus(candidate_id)

    forward = _identity(run(evidence, candidate_id))
    assert _identity(run(list(reversed(evidence)), candidate_id)) == forward

    rng = random.Random(20260911)
    for _ in range(500):
        shuffled = list(evidence)
        rng.shuffle(shuffled)
        assert _identity(run(shuffled, candidate_id)) == forward


def test_conflict_differs_from_absence_and_from_agreement():
    """Three outcomes, three states, never collapsed into one."""
    candidate_id = uuid4()
    # Two videos carry the title, so it can recur; the first is described by
    # two AGREEING records, which must not read as a conflict.
    agreeing = video_evidence(
        "AGREE", candidate_id=candidate_id, channel_id="UC_A",
        title="alpha bravo", payload_hash="a1",
    ) + video_evidence(
        "AGREE", candidate_id=candidate_id, channel_id="UC_A",
        title="alpha bravo", payload_hash="a2",
    ) + video_evidence(
        "AGREE2", candidate_id=candidate_id, channel_id="UC_B",
        title="alpha bravo", payload_hash="a3",
    )
    absent = video_evidence("ABSENT", candidate_id=candidate_id, title=None)
    conflicting = video_evidence(
        "CONFLICT", candidate_id=candidate_id, title="alpha bravo", payload_hash="c1"
    ) + video_evidence(
        "CONFLICT", candidate_id=candidate_id, title="delta echo", payload_hash="c2"
    )
    result = run(agreeing + absent + conflicting, candidate_id)
    title = next(e for e in result.field_availability if e.field == "title")
    assert (
        title.available_video_count,
        title.unavailable_video_count,
        title.conflicting_video_count,
    ) == (2, 1, 1)
    # Agreement between two records is NOT a conflict: the field survives
    # and still contributes its tokens.
    assert FieldState.AVAILABLE is not FieldState.CONFLICTING
    tokens = {p.value for p in result.patterns if p.kind is PatternKind.TITLE_TOKEN}
    assert "alpha" in tokens and "bravo" in tokens
    # ...and the conflicted video's competing values contribute nothing.
    assert "charlie" not in tokens and "foxtrot" not in tokens



def _identity(result) -> tuple:
    return (
        result.state,
        result.pattern_state,
        result.missing_reason,
        result.observed_video_count,
        result.distinct_channel_count,
        result.videos_without_channel_id,
        result.videos_with_conflicting_channel_id,
        result.outlier_observation_count,
        result.outlier_distinct_channel_count,
        result.outlier_evidence_available,
        tuple(
            (
                e.field,
                e.available_video_count,
                e.unavailable_video_count,
                e.conflicting_video_count,
            )
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
                p.videos_with_conflicting_channel_id,
                p.top_channel_share,
                p.concentration,
                tuple(
                    (o.video_id, o.channel_id, o.channel_state, o.evidence_ids)
                    for o in p.occurrences
                ),
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
    # V0 was collected twice: byte-identical payloads, so one payload hash and
    # two distinct row ids. Without this the permutation sweep never touches
    # the deduplication path at all, because every other video has exactly one
    # record and nothing is ever suppressed.
    evidence.extend(
        video_evidence(
            "V0", candidate_id=candidate_id, channel_id="UC_A",
            title="meal plan guide for busy parents", duration_seconds=300,
            tags=["meal", "plan"],
        )
    )
    return evidence


def test_output_is_identical_under_1000_arrival_order_permutations():
    candidate_id = uuid4()
    evidence = _mixed_corpus(candidate_id)
    first = run(evidence, candidate_id)
    # The sweep is only meaningful if the corpus reaches the dedup path, so
    # pin that here rather than trusting the fixture to keep doing it.
    assert first.provenance.duplicate_evidence_suppressed > 0
    baseline = _identity(first)
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


def test_no_path_ever_claims_the_dimension_is_scored():
    """5B holds evidence, it never scores it — on EVERY path.

    The empty paths each assert their dimension state, but the populated
    path asserted nothing, so a mutation flipping it to SCORED survived the
    whole suite. That is the one claim this milestone is not allowed to
    make: no scoring formula for content patterns is approved, and a SCORED
    dimension would tell a downstream consumer one exists.
    """
    candidate_id = uuid4()
    evidence = _build_outlier_scenario(candidate_id, creators=4, hook_in_winner=True)
    outlier = derive_robust_content_intelligence(
        candidate_id=candidate_id, evidence=evidence
    )

    populated = run(evidence, candidate_id, outlier_evidence=outlier)
    assert populated.patterns
    assert populated.state is DimensionState.EVIDENCE_PRESENT_UNSCORED
    assert populated.value is None

    # Every other reachable path, for the same reason.
    single, single_ev = corpus(("ONLY", "UC_A", "meal plan"))
    bare_id = uuid4()
    bare: list[EvidenceItem] = []
    for index in range(2):
        bare.extend(
            video_evidence(
                f"B{index}",
                candidate_id=bare_id,
                title=None,
                tags=_UNSET,
                category=None,
                duration_seconds=_UNSET,
            )
        )
    paths = [
        run([], candidate_id),                                # NO_CONTENT_OBSERVED
        run(single_ev, single),                               # NO_RECURRING_PATTERNS
        run(bare, bare_id),                                   # METADATA_UNAVAILABLE
        run([], candidate_id, missing_reasons={PUBLIC_CONTENT: FAILED}),
    ]
    assert [r.pattern_state for r in paths] == [
        ContentPatternsState.NO_CONTENT_OBSERVED,
        ContentPatternsState.NO_RECURRING_PATTERNS,
        ContentPatternsState.METADATA_UNAVAILABLE,
        ContentPatternsState.CONTENT_EVIDENCE_UNAVAILABLE,
    ]
    for result in paths:
        assert result.state is not DimensionState.SCORED
        assert result.value is None
    assert {r.state for r in paths} == {
        DimensionState.UNKNOWN,
        DimensionState.MISSING,
    }


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


def test_derived_features_are_inferred_never_observed():
    """5B derives; it never re-observes.

    Every input record here is OBSERVED, and the features extracted from
    them are still INFERRED: normalizing, tokenizing and counting produce a
    reading of the evidence, not a new observation. Promoting them to
    OBSERVED would let a derived value claim the standing of something a
    provider actually returned.

    This invariant previously had exactly one assertion, as the last line of
    a test about version strings, so a mutation flipping it to OBSERVED was
    caught only incidentally.
    """
    # Populated: patterns extracted from OBSERVED records.
    candidate_id, evidence = corpus(
        ("V1", "UC_A", "meal plan"), ("V2", "UC_B", "meal plan")
    )
    populated = run(evidence, candidate_id)
    assert populated.patterns
    assert all(
        item.truth_class is TruthClass.OBSERVED
        for item in evidence
        if item.signal_type == "public_content_observation"
    )
    assert populated.features_truth_class is TruthClass.INFERRED

    # Observed videos, but nothing recurred.
    single_id, single = corpus(("ONLY", "UC_A", "meal plan"))
    lonely = run(single, single_id)
    assert lonely.pattern_state is ContentPatternsState.NO_RECURRING_PATTERNS
    assert lonely.observed_video_count == 1
    assert lonely.features_truth_class is TruthClass.INFERRED

    # Observed videos whose metadata the provider never returned.
    bare_id = uuid4()
    bare: list[EvidenceItem] = []
    for index in range(2):
        bare.extend(
            video_evidence(
                f"B{index}", candidate_id=bare_id, title=None, tags=_UNSET,
                category=None, duration_seconds=_UNSET,
            )
        )
    metadataless = run(bare, bare_id)
    assert metadataless.pattern_state is ContentPatternsState.METADATA_UNAVAILABLE
    assert metadataless.observed_video_count == 2
    assert metadataless.features_truth_class is TruthClass.INFERRED

    # Nothing observed at all: there is no derived feature to classify.
    empty = run([], uuid4())
    assert empty.pattern_state is ContentPatternsState.NO_CONTENT_OBSERVED
    assert empty.features_truth_class is None

    failed = run([], uuid4(), missing_reasons={PUBLIC_CONTENT: FAILED})
    assert failed.pattern_state is ContentPatternsState.CONTENT_EVIDENCE_UNAVAILABLE
    assert failed.features_truth_class is None

    # No path may claim the features were observed.
    for result in (populated, lonely, metadataless, empty, failed):
        assert result.features_truth_class is not TruthClass.OBSERVED


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


    assert_route_surface_unchanged(app)
    assert TestClient(app).post("/score", json={}).status_code == 410


def test_5b_is_not_wired_into_any_route():
    """5B stays at the service layer, matching 5A's boundary."""
    import app.api.routes as routes

    assert not hasattr(routes, "derive_content_patterns")
    source = Path("app/api/routes.py").read_text()
    assert "content_patterns" not in source

def test_greek_final_sigma_folds_to_one_token():
    """Casefolding exists so final and medial sigma compare equal."""
    assert normalize_text("Γεύματος") == normalize_text("γεύματοσ")
    assert tokenize_title("ΣΧΈΔΙΟ") == tokenize_title("σχέδιο")


# ------------------------------------------------- 5A scope verification


def _outlier_scope(candidate_id: UUID, research_run_id: UUID | None = None):
    """A 4-creator corpus plus the 5A result derived from the same scope."""
    evidence: list[EvidenceItem] = []
    for index in range(4):
        channel = f"UC_{index}"
        rows = [
            (f"W{index}", 10_000, "secret hook plan"),
            (f"L{index}a", 100, "ordinary plan"),
            (f"L{index}b", 90, "ordinary plan"),
        ]
        for video_id, views, title in rows:
            evidence.extend(
                video_evidence(
                    video_id, candidate_id=candidate_id, channel_id=channel,
                    title=title, views=views, research_run_id=research_run_id,
                )
            )
    outlier = derive_robust_content_intelligence(
        candidate_id=candidate_id, evidence=evidence,
        research_run_id=research_run_id,
    )
    return evidence, outlier


def test_matching_5a_scope_is_consumed():
    candidate_id = uuid4()
    evidence, outlier = _outlier_scope(candidate_id)
    result = run(evidence, candidate_id, outlier_evidence=outlier)
    hook = pattern_for(result, PatternKind.TITLE_TOKEN, "secret")
    assert result.outlier_evidence_available is True
    assert hook.cooccurrence.state is CooccurrenceState.OVER_REPRESENTED
    assert hook.cooccurrence.outlier_video_count == 4


def test_a_5a_result_for_another_candidate_is_refused_not_consumed():
    """Video ids only mean something within a scope.

    A 5A result from another candidate carrying colliding video ids would
    otherwise contaminate co-occurrence silently, with no trace in the
    output. It is refused, and pattern extraction continues normally.
    """
    mine = uuid4()
    theirs = uuid4()
    my_evidence, _ = _outlier_scope(mine)
    _, foreign = _outlier_scope(theirs)
    assert foreign.candidate_id != mine

    result = run(my_evidence, mine, outlier_evidence=foreign)
    hook = pattern_for(result, PatternKind.TITLE_TOKEN, "secret")
    assert hook.cooccurrence.state is CooccurrenceState.OUTLIER_SCOPE_MISMATCH
    assert hook.cooccurrence.outlier_video_count == 0
    assert hook.cooccurrence.outlier_distinct_channel_count == 0
    assert hook.cooccurrence.share_among_outliers is None
    assert result.outlier_evidence_available is False
    assert result.outlier_observation_count == 0
    # Pattern extraction is untouched by the refusal.
    assert result.pattern_state is ContentPatternsState.PATTERNS_OBSERVED
    assert hook.video_count == 4
    assert hook.cooccurrence.share_in_corpus == hook.prevalence_share


def test_a_5a_result_from_another_research_run_is_refused():
    candidate_id = uuid4()
    run_a = uuid4()
    run_b = uuid4()
    evidence_a, _ = _outlier_scope(candidate_id, research_run_id=run_a)
    _, outlier_b = _outlier_scope(candidate_id, research_run_id=run_b)
    assert outlier_b.candidate_id == candidate_id
    assert outlier_b.research_run_id != run_a

    result = run(
        evidence_a, candidate_id,
        research_run_id=run_a, outlier_evidence=outlier_b,
    )
    hook = pattern_for(result, PatternKind.TITLE_TOKEN, "secret")
    assert hook.cooccurrence.state is CooccurrenceState.OUTLIER_SCOPE_MISMATCH
    assert hook.cooccurrence.outlier_video_count == 0
    assert result.outlier_evidence_available is False
    assert result.pattern_state is ContentPatternsState.PATTERNS_OBSERVED


def test_a_runless_5a_result_is_refused_for_a_run_scoped_derivation():
    """None and a run id are different scopes, not compatible ones."""
    candidate_id = uuid4()
    research_run_id = uuid4()
    evidence, _ = _outlier_scope(candidate_id, research_run_id=research_run_id)
    _, runless = _outlier_scope(candidate_id)
    assert runless.research_run_id is None

    result = run(
        evidence, candidate_id,
        research_run_id=research_run_id, outlier_evidence=runless,
    )
    hook = pattern_for(result, PatternKind.TITLE_TOKEN, "secret")
    assert hook.cooccurrence.state is CooccurrenceState.OUTLIER_SCOPE_MISMATCH


def test_scope_mismatch_is_distinct_from_unavailable_and_from_no_outliers():
    """Three different facts, three different states."""
    candidate_id = uuid4()
    evidence, outlier = _outlier_scope(candidate_id)
    _, foreign = _outlier_scope(uuid4())

    states = {
        run(evidence, candidate_id, outlier_evidence=None)
        .patterns[0].cooccurrence.state,
        run(evidence, candidate_id, outlier_evidence=foreign)
        .patterns[0].cooccurrence.state,
    }
    assert CooccurrenceState.OUTLIER_EVIDENCE_UNAVAILABLE in states
    assert CooccurrenceState.OUTLIER_SCOPE_MISMATCH in states
    assert len(states) == 2
    # And the matching scope produces neither.
    matched = run(evidence, candidate_id, outlier_evidence=outlier)
    assert matched.patterns[0].cooccurrence.state not in states


def test_digits_are_content_and_are_preserved():
    """"30 day plan" and "5 meal plans" carry their numbers.

    A mutation that kept only letters passed every other test, because
    nothing exercised a numeric token.
    """
    assert normalize_text("30 day plan") == "30 day plan"
    assert tokenize_title("30 Day Meal Plan") == ("30", "day", "meal", "plan")
    assert tokenize_title("5 meal plans") == ("meal", "plans")  # "5" is 1 char
    candidate_id, evidence = corpus(
        ("V1", "UC_A", "30 day meal plan"),
        ("V2", "UC_B", "30 day plan"),
    )
    result = run(evidence, candidate_id)
    assert pattern_for(result, PatternKind.TITLE_TOKEN, "30") is not None


def test_combining_marks_are_preserved_for_scripts_nfkc_does_not_compose():
    """Devanagari matras and Arabic diacritics are letters' vowels.

    Dropping category-M characters would silently rewrite these words into
    different words, which is the same class of damage as stripping accents.
    """
    devanagari = "योजना बनाना"
    arabic_vocalised = "مُرَحَّبا"
    for text in (devanagari, arabic_vocalised):
        normalized = normalize_text(text)
        assert normalized != ""
        # Every combining mark present in the source survives.
        marks = {c for c in unicodedata.normalize("NFKC", text)
                 if unicodedata.category(c).startswith("M")}
        assert marks, text
        assert marks <= set(normalized), text
    assert tokenize_title(devanagari) == ("योजना", "बनाना")


# ------------------------------- creator-concentration denominator


def _concentration_corpus(
    candidate_id: UUID,
    known: dict[str, int],
    unknown: int = 0,
    conflicting: int = 0,
) -> list[EvidenceItem]:
    """A corpus where one pattern has a controlled creator composition."""
    evidence: list[EvidenceItem] = []
    for channel, count in known.items():
        for index in range(count):
            evidence.extend(
                video_evidence(
                    f"{channel}_{index}", candidate_id=candidate_id,
                    channel_id=channel, title="meal plan",
                )
            )
    for index in range(unknown):
        evidence.extend(
            video_evidence(
                f"U{index}", candidate_id=candidate_id,
                channel_id=None, title="meal plan",
            )
        )
    for index in range(conflicting):
        # Two OBSERVED records naming different creators for the same video.
        evidence.extend(
            video_evidence(
                f"C{index}", candidate_id=candidate_id, channel_id="UC_X",
                title="meal plan", payload_hash=f"c{index}a",
            )
        )
        evidence.extend(
            video_evidence(
                f"C{index}", candidate_id=candidate_id, channel_id="UC_Y",
                title="meal plan", payload_hash=f"c{index}b",
            )
        )
    return evidence


def _meal_pattern(candidate_id: UUID, evidence: list[EvidenceItem]):
    result = run(evidence, candidate_id)
    return pattern_for(result, PatternKind.TITLE_TOKEN, "meal")


def test_one_known_creator_among_nine_unknown_is_not_concentrated():
    """The finding: unknown identity must not inflate a dominance claim.

    An earlier version divided by the ATTRIBUTED occurrences alone, so one
    known creator among ten occurrences reported top_channel_share = 1.0 and
    CREATOR_CONCENTRATED. The documented contract is a share of the pattern's
    occurrences, and that is 1/10.
    """
    candidate_id = uuid4()
    pattern = _meal_pattern(
        candidate_id, _concentration_corpus(candidate_id, {"UC_A": 1}, unknown=9)
    )
    assert pattern.video_count == 10
    assert pattern.top_channel_share == 0.1
    assert pattern.top_channel_share < CONCENTRATION_SHARE
    assert pattern.concentration is ConcentrationState.CREATOR_PARTIALLY_UNKNOWN
    assert pattern.videos_without_channel_id == 9
    assert pattern.videos_with_conflicting_channel_id == 0


def test_six_from_one_creator_plus_four_unknown_is_concentrated_on_the_real_share():
    """At or above the threshold over ALL occurrences, dominance is a fact.

    Resolving the four unknowns can only raise that creator's count or add
    another creator, so the share is a lower bound and the state is safe.
    """
    candidate_id = uuid4()
    pattern = _meal_pattern(
        candidate_id, _concentration_corpus(candidate_id, {"UC_A": 6}, unknown=4)
    )
    assert pattern.video_count == 10
    assert pattern.top_channel_share == 0.6
    assert pattern.concentration is ConcentrationState.CREATOR_CONCENTRATED


def test_two_known_creators_with_a_large_unknown_remainder_claims_neither():
    """Breadth is not established either: the unknowns could be one creator."""
    candidate_id = uuid4()
    pattern = _meal_pattern(
        candidate_id,
        _concentration_corpus(candidate_id, {"UC_A": 1, "UC_B": 1}, unknown=8),
    )
    assert pattern.video_count == 10
    assert pattern.distinct_channel_count == 2
    assert pattern.top_channel_share == 0.1
    # Not MULTI_CREATOR: that would overstate how many creators carry it.
    assert pattern.concentration is ConcentrationState.CREATOR_PARTIALLY_UNKNOWN
    assert pattern.concentration is not ConcentrationState.MULTI_CREATOR


def test_conflicting_channel_ids_count_against_dominance_and_are_reported_apart():
    """A conflict is unattributed for the share, and never plain absence."""
    candidate_id = uuid4()
    pattern = _meal_pattern(
        candidate_id,
        _concentration_corpus(candidate_id, {"UC_A": 1}, conflicting=3),
    )
    assert pattern.video_count == 4
    # 1 of 4, not 1 of 1.
    assert pattern.top_channel_share == 0.25
    assert pattern.concentration is ConcentrationState.CREATOR_PARTIALLY_UNKNOWN
    # Conflict is reported in its own count, never folded into absence.
    assert pattern.videos_with_conflicting_channel_id == 3
    assert pattern.videos_without_channel_id == 0
    states = {o.channel_state for o in pattern.occurrences}
    assert FieldState.CONFLICTING in states
    assert FieldState.UNAVAILABLE not in states


def test_mixed_available_conflicting_and_absent_channel_ids():
    candidate_id = uuid4()
    pattern = _meal_pattern(
        candidate_id,
        _concentration_corpus(
            candidate_id, {"UC_A": 2}, unknown=2, conflicting=2
        ),
    )
    assert pattern.video_count == 6
    assert pattern.top_channel_share == round(2 / 6, 4)
    assert pattern.videos_without_channel_id == 2
    assert pattern.videos_with_conflicting_channel_id == 2
    assert pattern.concentration is ConcentrationState.CREATOR_PARTIALLY_UNKNOWN
    # Every occurrence is accounted for exactly once.
    attributed = sum(
        1 for o in pattern.occurrences if o.channel_state is FieldState.AVAILABLE
    )
    assert (
        attributed
        + pattern.videos_without_channel_id
        + pattern.videos_with_conflicting_channel_id
        == pattern.video_count
    )


def test_all_channel_ids_unknown_yields_no_share_at_all():
    candidate_id = uuid4()
    pattern = _meal_pattern(
        candidate_id, _concentration_corpus(candidate_id, {}, unknown=6)
    )
    assert pattern.video_count == 6
    assert pattern.distinct_channel_count == 0
    # Unknown, never zero and never 1.0.
    assert pattern.top_channel_share is None
    assert pattern.concentration is ConcentrationState.CREATOR_UNKNOWN


def test_all_channel_ids_known_reports_the_true_spread():
    candidate_id = uuid4()
    single = _meal_pattern(
        candidate_id, _concentration_corpus(candidate_id, {"UC_A": 10})
    )
    assert single.top_channel_share == 1.0
    assert single.concentration is ConcentrationState.SINGLE_CREATOR

    other = uuid4()
    spread = _meal_pattern(
        other, _concentration_corpus(other, {"UC_A": 3, "UC_B": 3, "UC_C": 4})
    )
    assert spread.video_count == 10
    assert spread.top_channel_share == 0.4
    assert spread.concentration is ConcentrationState.MULTI_CREATOR


def test_unattributed_occurrences_can_only_lower_the_top_share():
    """The invariant, swept: adding unknowns never raises the share.

    This is the property the finding violated. Whatever the composition,
    appending an occurrence with no usable creator must weaken — never
    strengthen — the claim that one creator dominates.
    """
    rng = random.Random(5150)
    for _ in range(60):
        known = {f"UC_{i}": rng.randint(1, 4) for i in range(rng.randint(1, 3))}
        # A single occurrence is not a recurring pattern, so the base corpus
        # must clear MIN_PATTERN_VIDEO_COUNT before dilution means anything.
        if sum(known.values()) < MIN_PATTERN_VIDEO_COUNT:
            known["UC_0"] = known.get("UC_0", 0) + MIN_PATTERN_VIDEO_COUNT
        candidate_id = uuid4()
        base = _meal_pattern(
            candidate_id, _concentration_corpus(candidate_id, known)
        )
        for extra in (1, 3, 7):
            diluted_id = uuid4()
            diluted = _meal_pattern(
                diluted_id,
                _concentration_corpus(diluted_id, known, unknown=extra),
            )
            assert diluted.top_channel_share <= base.top_channel_share, (
                known, extra, diluted.top_channel_share, base.top_channel_share,
            )
            assert diluted.video_count == base.video_count + extra


def test_concentration_states_are_order_independent():
    candidate_id = uuid4()
    evidence = _concentration_corpus(
        candidate_id, {"UC_A": 2, "UC_B": 1}, unknown=2, conflicting=2
    )
    baseline = _identity(run(evidence, candidate_id))
    rng = random.Random(31)
    for _ in range(300):
        shuffled = list(evidence)
        rng.shuffle(shuffled)
        assert _identity(run(shuffled, candidate_id)) == baseline
