"""Content pattern extraction (Milestone 5B).

A deterministic derivation over public-content evidence Milestone 3B already
collected. No provider, no provider call, no network, no persistence, no
endpoint, and no score.

What this milestone observes
-----------------------------

Which surface features RECUR across the content that was collected for one
candidate: title tokens, adjacent title token pairs, tags, categories, and
duration bands. Every emitted pattern is a statement about content that
exists, with a count, a distinct-creator count, and the evidence that
produced it.

Two facts, never collapsed into one
------------------------------------

    prevalence      how often a pattern occurs across the collected corpus
    co-occurrence   how often it occurs among the observations Milestone 5A
                    scored above their own creator's baseline

These are reported as separate structures with separate denominators. They
are deliberately NOT combined into a strength, quality, or performance
figure, because combining them is precisely how a co-occurrence becomes a
claim that a pattern works.

Co-occurrence is not causation, and not prediction
---------------------------------------------------

"This pattern appeared among observed creator-relative outliers" is a
statement about the sample. It is never evidence that the pattern caused the
performance, that copying it would reproduce the performance, or that any
future video carrying it would perform at all. Videos that outperform their
creator's median differ in countless ways this module cannot see — topic,
timing, thumbnail, the creator's existing audience, platform distribution —
and nothing here isolates the pattern from any of them.

The module therefore emits no performance, virality, engagement, or
expected-view figure of any kind, and a test scans every emitted string for
causal and predictive vocabulary.

One prolific creator is not a field
------------------------------------

A single creator who publishes forty videos with the same title formula
would otherwise make that formula look like a property of the whole field.
Every pattern therefore carries `distinct_channel_count` alongside
`video_count`, plus `top_channel_share` and a concentration state, so a
pattern supported by one creator can never read as broad support.

`top_channel_share` is the largest known creator's share of ALL the
pattern's occurrences, not of the attributed ones. Dividing by the attributed
subset would let unknown creator identity STRENGTHEN the evidence that a
known creator dominates — one known creator among ten occurrences would
report 1.0 — which inverts the safeguard. Occurrences with no creator, and
occurrences whose records name different creators, are reported separately
and both count against a dominance claim.

The same control applies to outlier co-occurrence, which additionally
requires a minimum number of INDEPENDENT creators before any comparison is
reported.

Missing metadata is missing, never absence
-------------------------------------------

A video whose title, tags, category or duration the provider did not return
is UNAVAILABLE for that field. It is excluded from that field's denominator
and counted, never treated as evidence that a pattern is absent. Each field
reports its own availability, so a pattern's prevalence is always a share of
the videos that could have carried it.

The evidence record is the authority
-------------------------------------

Metadata is read from a stored payload only when the content-observation
record carrying it is OBSERVED. A payload value sitting behind a record that
is not OBSERVED is never read, exactly as Milestone 5A refuses to read a
view count from a payload snapshot.

What this module never reads
-----------------------------

View counts, like counts, comment counts, and every channel aggregate
(`channel_subscriber_count`, `channel_view_count`, `channel_video_count`).
Magnitudes belong to 4F and 5A; subscriber counts describe a creator's whole
cross-topic audience and are not a property of this candidate's content. An
AST guard asserts none of them is reachable from this module.
"""

import re
import unicodedata
from dataclasses import dataclass
from enum import Enum
from uuid import UUID

from app.domain.enums import EvidencePurpose, TruthClass
from app.domain.models import EvidenceItem
from app.services.faceless_content_intelligence import (
    RobustContentIntelligenceResult,
    RobustOutlierObservationState,
)
from app.services.preliminary_dimensions import DimensionState

CONTENT_PATTERNS_VERSION = "content_patterns_v1"
CONTENT_NORMALIZATION_VERSION = "content_normalization_v1"
DURATION_BAND_VERSION = "duration_band_v1"
OUTLIER_COOCCURRENCE_VERSION = "outlier_cooccurrence_v1"

DIM_CONTENT_PATTERNS = "preliminary_content_patterns"

SIGNAL_CONTENT_OBSERVATION = "public_content_observation"
CAPABILITY_PUBLIC_CONTENT = "public_content"

# ---------------------------------------------------------------- thresholds
#
# UNVALIDATED V1 ASSUMPTIONS chosen by inspection, calibrated against no
# outcome data. They decide what is REPORTED, never whether it is good. No
# threshold here is a quality bar and none produces a score.

# A feature seen once is not recurring. Two occurrences is the minimum that
# can be called a pattern at all.
MIN_PATTERN_VIDEO_COUNT = 2

# Tokens shorter than this carry no distinguishing content.
MIN_TOKEN_LENGTH = 2

# Share of a pattern's occurrences held by its largest contributing creator,
# at or above which the pattern is reported as creator-concentrated.
CONCENTRATION_SHARE = 0.6

# Independent creators required among co-occurring outliers before any
# comparison against corpus prevalence is reported. Below it the comparison
# would describe one or two creators' habits, not the sampled field.
MIN_INDEPENDENT_CREATORS_FOR_COOCCURRENCE = 3

# A 5A observation joins the comparison set when its creator-relative score
# is strictly above its own creator's median, i.e. log2(views/median) > 0.
# This is a median split defining WHICH observations are compared, not a
# performance bar and not a claim that the split is meaningful.
RELATIVE_SCORE_ABOVE_BASELINE = 0.0

# Shares this far apart are reported as differing. Purely a reporting cut
# point for a categorical state; no ratio is emitted as a figure to rank.
SHARE_DIVERGENCE_RATIO = 1.5

# Structural words carry no content. Kept explicit and versioned so a
# reviewer can see exactly which words are discarded.
TITLE_STOPWORDS = frozenset(
    {
        "a", "an", "and", "are", "as", "at", "be", "but", "by", "can", "do",
        "for", "from", "get", "has", "have", "how", "i", "in", "is", "it",
        "its", "me", "my", "not", "of", "on", "or", "s", "so", "t", "that",
        "the", "their", "them", "then", "there", "these", "they", "this",
        "to", "up", "was", "we", "were", "what", "when", "which", "who",
        "why", "will", "with", "you", "your",
    }
)

LIMITATIONS = (
    "A recurring pattern is an observation about content that exists. It is "
    "never a formula, a hook, or a technique that works.",
    "Co-occurrence with creator-relative outliers describes the sample only. "
    "It is never evidence that a pattern caused, or would reproduce, any "
    "performance.",
    "Videos differ in topic, timing, thumbnail, creator audience and platform "
    "distribution. This module isolates none of those, so no pattern can be "
    "credited with an outcome.",
    "The sampled videos are provider-ordered public-content observations, not "
    "the whole content field.",
    "Missing title, tag, category or duration metadata stays UNAVAILABLE for "
    "that field and is never counted as evidence a pattern is absent.",
    "Prevalence and outlier co-occurrence are separate facts and are never "
    "combined into a strength or quality measure.",
    "Normalization and duration bands are deterministic V1 definitions, not "
    "validated linguistic or editorial models.",
    "Normalization preserves observed text: accents and non-Latin scripts "
    "survive. Scripts without word separators yield one token per run, "
    "because no segmentation is performed.",
    "Where two OBSERVED records disagree about the same video's metadata, "
    "that field is reported CONFLICTING and excluded, never resolved by "
    "picking whichever record arrived first.",
    "Milestone 5A evidence is consumed only when its candidate and research "
    "run match this derivation's scope; a mismatch is reported and no "
    "observation is read.",
    "No view, engagement, subscriber or channel aggregate is read here, and "
    "no demand, conversion, market-size, sales or revenue claim is produced.",
)


class PatternKind(str, Enum):
    """Which surface feature a pattern describes. Unordered."""

    TITLE_TOKEN = "TITLE_TOKEN"
    TITLE_BIGRAM = "TITLE_BIGRAM"
    TAG = "TAG"
    CATEGORY = "CATEGORY"
    DURATION_BAND = "DURATION_BAND"


class DurationBand(str, Enum):
    """Deterministic, non-overlapping duration buckets (`duration_band_v1`).

    Boundaries are stated in seconds below. They are a reporting convenience
    and carry no claim that any band performs differently from another.
    """

    UNDER_1_MIN = "UNDER_1_MIN"
    ONE_TO_5_MIN = "ONE_TO_5_MIN"
    FIVE_TO_15_MIN = "FIVE_TO_15_MIN"
    FIFTEEN_TO_30_MIN = "FIFTEEN_TO_30_MIN"
    OVER_30_MIN = "OVER_30_MIN"


# Upper bound in seconds, exclusive, largest-last. A test asserts the bands
# tile the non-negative line without gaps or overlaps.
_DURATION_BOUNDS: tuple[tuple[int | None, DurationBand], ...] = (
    (60, DurationBand.UNDER_1_MIN),
    (300, DurationBand.ONE_TO_5_MIN),
    (900, DurationBand.FIVE_TO_15_MIN),
    (1800, DurationBand.FIFTEEN_TO_30_MIN),
    (None, DurationBand.OVER_30_MIN),
)


class FieldState(str, Enum):
    """Why one video does or does not supply one metadata field.

    CONFLICTING is kept separate from UNAVAILABLE on purpose: "two OBSERVED
    records disagree about this video's title" is a different fact from "no
    record carried a title", and collapsing them would hide a data-quality
    problem behind a data-absence one.
    """

    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"
    CONFLICTING = "CONFLICTING"


class ConcentrationState(str, Enum):
    """How a pattern's occurrences spread across creators. Unordered."""

    # Every occurrence belongs to one creator.
    SINGLE_CREATOR = "SINGLE_CREATOR"
    # One creator holds at least CONCENTRATION_SHARE of the occurrences.
    CREATOR_CONCENTRATED = "CREATOR_CONCENTRATED"
    # No single creator dominates.
    MULTI_CREATOR = "MULTI_CREATOR"
    # Some occurrences carry no usable creator identity, and the known
    # creators do not reach the concentration threshold across ALL
    # occurrences. Neither concentration nor breadth is establishable: the
    # unattributed occurrences could all belong to the largest known creator,
    # or to none of them.
    CREATOR_PARTIALLY_UNKNOWN = "CREATOR_PARTIALLY_UNKNOWN"
    # No occurrence carried a creator identity.
    CREATOR_UNKNOWN = "CREATOR_UNKNOWN"


class CooccurrenceState(str, Enum):
    """How a pattern's share among 5A outliers compares to its corpus share.

    A description of two shares, never a performance verdict. OVER_REPRESENTED
    does not mean the pattern works, and UNDER_REPRESENTED does not mean it
    fails.
    """

    # Milestone 5A produced no result for this scope.
    OUTLIER_EVIDENCE_UNAVAILABLE = "OUTLIER_EVIDENCE_UNAVAILABLE"
    # A 5A result was supplied, but for a different candidate or research
    # run. Video ids can collide across scopes, so consuming it would let
    # another candidate's observations contaminate this one.
    OUTLIER_SCOPE_MISMATCH = "OUTLIER_SCOPE_MISMATCH"
    # 5A ran and scored nothing above its own baselines.
    NO_QUALIFYING_OUTLIERS = "NO_QUALIFYING_OUTLIERS"
    # The pattern occurs among outliers, but too few independent creators
    # carry it for the comparison to describe anything but their habits.
    INSUFFICIENT_INDEPENDENT_CREATORS = "INSUFFICIENT_INDEPENDENT_CREATORS"
    # Outliers exist and none of them carries this pattern.
    ABSENT_AMONG_OUTLIERS = "ABSENT_AMONG_OUTLIERS"
    OVER_REPRESENTED = "OVER_REPRESENTED"
    UNDER_REPRESENTED = "UNDER_REPRESENTED"
    COMPARABLE = "COMPARABLE"


class ContentPatternsState(str, Enum):
    """Why the derivation does or does not describe a corpus."""

    # The capability was not requested, or the provider failed.
    CONTENT_EVIDENCE_UNAVAILABLE = "CONTENT_EVIDENCE_UNAVAILABLE"
    # The capability ran and returned no content for this candidate.
    NO_CONTENT_OBSERVED = "NO_CONTENT_OBSERVED"
    # Videos were observed, but no field carried usable metadata.
    METADATA_UNAVAILABLE = "METADATA_UNAVAILABLE"
    # Metadata exists, but nothing recurred.
    NO_RECURRING_PATTERNS = "NO_RECURRING_PATTERNS"
    PATTERNS_OBSERVED = "PATTERNS_OBSERVED"


# Every state's boundary statement, emitted with the result. A test asserts
# the mapping is total and that no entry is empty.
STATE_BOUNDARIES: dict[ContentPatternsState, str] = {
    ContentPatternsState.CONTENT_EVIDENCE_UNAVAILABLE: (
        "Nothing was measured. This is not evidence that the content field "
        "lacks patterns."
    ),
    ContentPatternsState.NO_CONTENT_OBSERVED: (
        "No content was returned for this candidate. This measures the "
        "sample, not the field."
    ),
    ContentPatternsState.METADATA_UNAVAILABLE: (
        "Videos were observed and their metadata was not. Absent metadata is "
        "never evidence that a pattern does not exist."
    ),
    ContentPatternsState.NO_RECURRING_PATTERNS: (
        "Metadata was available and nothing recurred across the sampled "
        "videos. This describes the sample only."
    ),
    ContentPatternsState.PATTERNS_OBSERVED: (
        "These features recur in the content that was collected. Recurrence "
        "is not evidence that a feature works, causes performance, or would "
        "reproduce any result."
    ),
}


def _id_sort_key(value: object) -> tuple[str, str]:
    """Total order for identifiers without coercing their stored values."""
    return (str(value), type(value).__name__)


# ------------------------------------------------------------ normalization

_INVISIBLE_RE = re.compile(r"[­​-‏  ﻿]")
# Unicode general categories that carry observed content. Letters (L*),
# numbers (N*) and combining marks (M*) are kept; every other character
# becomes a separator.
#
# An earlier version used `[^a-z0-9]+`, which silently DESTROYED observed
# content: "café" became "caf", "naïve résumé" became "na ve r sum", and
# Japanese or Arabic titles became the empty string entirely. A derivation
# that deletes what a provider actually returned is not normalizing it, and
# this milestone's own test had been written around the damage rather than
# against it. Marks are kept because scripts such as Arabic and Devanagari
# use combining marks NFKC does not compose away.
_KEPT_CATEGORIES = ("L", "N", "M")


def _is_content_char(char: str) -> bool:
    return unicodedata.category(char)[0] in _KEPT_CATEGORIES


def normalize_text(value: str) -> str:
    """Deterministic text normalization (`content_normalization_v1`).

    NFKC folds compatibility forms so a fullwidth and an ASCII rendering of
    the same word are one token, and composes decomposed sequences so NFC and
    NFD spellings agree. Invisible characters are stripped, case is folded,
    every run of non-content characters becomes a single space, and the
    result is trimmed.

    Observed content is PRESERVED, never deleted: accented Latin keeps its
    accents and non-Latin scripts survive intact. Scripts that do not
    separate words with spaces therefore yield one token per run, which is an
    honest consequence of performing no segmentation rather than a silent
    loss. No stemming, no lemmatization, no semantic clustering: this
    milestone introduces no LLM and no similarity model.
    """
    folded = unicodedata.normalize("NFKC", value)
    folded = _INVISIBLE_RE.sub("", folded)
    folded = folded.casefold()
    # Casefolding can produce new compatibility forms, so normalize again.
    folded = unicodedata.normalize("NFKC", folded)
    kept = "".join(char if _is_content_char(char) else " " for char in folded)
    return " ".join(kept.split())


def tokenize_title(value: str) -> tuple[str, ...]:
    """Normalized content tokens, stopwords and very short tokens removed."""
    return tuple(
        token
        for token in normalize_text(value).split()
        if len(token) >= MIN_TOKEN_LENGTH and token not in TITLE_STOPWORDS
    )


def duration_band(seconds: int) -> DurationBand:
    """Bucket a non-negative duration. Bands tile the line exactly."""
    for upper, band in _DURATION_BOUNDS:
        if upper is None or seconds < upper:
            return band
    raise AssertionError("duration bands must be exhaustive")  # pragma: no cover


# ------------------------------------------------------------------ records


@dataclass(slots=True, frozen=True)
class PatternOccurrence:
    """One video contributing one occurrence of one pattern.

    `channel_state` keeps "no record named a creator" apart from "two
    OBSERVED records named different creators". Both leave `channel_id` None,
    but they are different facts and a conflict must not be presented as an
    ordinary absence.
    """

    video_id: str
    channel_id: str | None
    channel_state: FieldState
    evidence_ids: tuple[UUID, ...]


@dataclass(slots=True, frozen=True)
class OutlierCooccurrence:
    """How a pattern sits among Milestone 5A's above-baseline observations.

    Separate from prevalence by construction: it has its own counts, its own
    denominator, and its own state. Nothing here is combined with corpus
    prevalence into a single figure.
    """

    state: CooccurrenceState
    # Videos carrying this pattern that 5A scored above their own baseline.
    outlier_video_count: int
    # Distinct creators among them. The control that stops one creator's
    # repetitions reading as independent field-wide support.
    outlier_distinct_channel_count: int
    # Denominator: all above-baseline observations whose video is in scope
    # and carries this pattern's field.
    comparable_outlier_total: int
    # The two shares, reported side by side and never multiplied together.
    share_among_outliers: float | None
    share_in_corpus: float | None
    version: str = OUTLIER_COOCCURRENCE_VERSION


@dataclass(slots=True, frozen=True)
class ContentPattern:
    """One recurring surface feature of the collected content."""

    kind: PatternKind
    value: str
    # Prevalence. The denominator is videos whose field was AVAILABLE, never
    # every video in the corpus.
    video_count: int
    field_available_video_count: int
    prevalence_share: float
    # Creator spread, always alongside the raw count.
    distinct_channel_count: int
    # Occurrences no record attributed to a creator.
    videos_without_channel_id: int
    # Occurrences whose OBSERVED records named different creators. Kept apart
    # from plain absence so a data-quality problem is never reported as one.
    videos_with_conflicting_channel_id: int
    # Share of ALL this pattern's occurrences held by its largest KNOWN
    # creator. The denominator is every occurrence, not only the attributed
    # ones: an unknown creator identity must never be able to inflate the
    # evidence that a known creator dominates.
    top_channel_share: float | None
    concentration: ConcentrationState
    # Lineage back to the contributing records and videos.
    occurrences: tuple[PatternOccurrence, ...]
    evidence_ids: tuple[UUID, ...]
    cooccurrence: OutlierCooccurrence


@dataclass(slots=True, frozen=True)
class FieldAvailability:
    """How many videos could have carried one metadata field.

    The three counts are disjoint and sum to the observed video count, so a
    conflict can never be mistaken for an absence or for a usable value.
    """

    field: str
    available_video_count: int
    unavailable_video_count: int
    conflicting_video_count: int


@dataclass(slots=True, frozen=True)
class ContentPatternProvenance:
    """Canonical lineage for one candidate/run derivation."""

    candidate_id: UUID
    research_run_id: UUID | None
    evidence_ids: tuple[UUID, ...]
    video_ids: tuple[str, ...]
    channel_ids: tuple[str, ...]
    providers: tuple[str, ...]
    platforms: tuple[str, ...]
    source_truth_classes: tuple[str, ...]
    duplicate_evidence_suppressed: int


@dataclass(slots=True, frozen=True)
class ContentPatternsResult:
    """Milestone 5B result for one candidate and one evidence scope."""

    candidate_id: UUID
    research_run_id: UUID | None
    state: DimensionState
    pattern_state: ContentPatternsState
    state_boundary: str
    # This derivation has no candidate-level numeric value, by design.
    value: None
    patterns: tuple[ContentPattern, ...]
    field_availability: tuple[FieldAvailability, ...]
    provenance: ContentPatternProvenance

    observed_video_count: int
    videos_without_channel_id: int
    videos_with_conflicting_channel_id: int
    distinct_channel_count: int
    # Above-baseline observations 5A supplied for this scope, if any.
    outlier_observation_count: int
    outlier_distinct_channel_count: int
    outlier_evidence_available: bool

    # Derived from OBSERVED records, but itself a derivation.
    features_truth_class: TruthClass | None = TruthClass.INFERRED
    missing_reason: str | None = None
    limitations: tuple[str, ...] = LIMITATIONS
    dimension_name: str = DIM_CONTENT_PATTERNS
    version: str = CONTENT_PATTERNS_VERSION
    normalization_version: str = CONTENT_NORMALIZATION_VERSION
    duration_band_version: str = DURATION_BAND_VERSION
    cooccurrence_version: str = OUTLIER_COOCCURRENCE_VERSION


# ------------------------------------------------------------------ internals


_FIELDS = ("channel_id", "title", "tags", "category", "duration_seconds")


@dataclass(slots=True)
class _VideoAccumulator:
    """Every OBSERVED value each field received, before resolution.

    Values are COLLECTED rather than first-wins. Two distinct OBSERVED
    records can disagree about the same video, and a first-wins rule makes
    the winner depend on arrival order — so the same evidence delivered in a
    different order would yield different patterns, different availability
    and different creator concentration. Collecting and resolving makes the
    outcome a function of the SET of records, not of their sequence.
    """

    video_id: str
    values: dict[str, set] = None  # type: ignore[assignment]
    evidence_ids: list[UUID] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.values is None:
            self.values = {field: set() for field in _FIELDS}
        if self.evidence_ids is None:
            self.evidence_ids = []


@dataclass(slots=True, frozen=True)
class _VideoRecord:
    """One deduplicated video's RESOLVED metadata.

    A field is present only when every OBSERVED record that supplied it
    agreed. Disagreement yields CONFLICTING, which is reported and excluded
    from pattern extraction rather than silently resolved in favour of
    whichever record happened to arrive first.
    """

    video_id: str
    channel_id: str | None
    title: str | None
    tags: tuple[str, ...] | None
    category: str | None
    duration_seconds: int | None
    states: dict[str, FieldState]
    evidence_ids: tuple[UUID, ...]


def _in_scope(
    item: EvidenceItem, candidate_id: UUID, research_run_id: UUID | None
) -> bool:
    """Candidate and run ownership, matching Milestone 5A exactly.

    With no run id, only explicitly runless inline evidence is in scope, so
    an omitted run stamp cannot silently mix historical runs.
    """
    if item.candidate_id != candidate_id:
        return False
    if research_run_id is not None and item.research_run_id != research_run_id:
        return False
    if research_run_id is None and item.research_run_id is not None:
        return False
    return True


def _payload_string(payload: dict, key: str) -> str | None:
    value = payload.get(key)
    if isinstance(value, str) and value.strip():
        return value
    return None


def _payload_tags(payload: dict) -> tuple[str, ...] | None:
    """Tags only when the payload actually carried a tag sequence.

    An absent key is UNAVAILABLE. An empty sequence is a real observation of
    "no tags" and is kept distinct from it.
    """
    value = payload.get("tags")
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        return tuple(item for item in value if isinstance(item, str) and item.strip())
    return None


def _payload_duration(payload: dict) -> int | None:
    value = payload.get("duration_seconds")
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, float) and value.is_integer() and value >= 0:
        return int(value)
    return None


def _collect_videos(
    candidate_id: UUID,
    evidence: list[EvidenceItem],
    research_run_id: UUID | None,
) -> tuple[list[_VideoRecord], int, list[EvidenceItem]]:
    """Rebuild deduplicated videos from OBSERVED content-observation records.

    Only `public_content_observation` / `CONTENT` records are in scope, and
    only when the RECORD is OBSERVED. A payload sitting behind a record that
    is not OBSERVED is never read, so a stored value cannot outrank the
    evidence's own statement about what was observed.

    A record with no payload hash is never collapsed; its multiplicity stays
    visible in lineage exactly as Milestone 5A keeps it.

    Values are COLLECTED per field and resolved afterwards by `_resolve`, so
    two distinct OBSERVED records that disagree about the same video produce
    a CONFLICTING field rather than whichever value happened to arrive first.
    """
    by_video: dict[str, _VideoAccumulator] = {}
    order: list[str] = []
    seen: set[tuple] = set()
    suppressed = 0
    contributing: list[EvidenceItem] = []

    for item in evidence:
        if not _in_scope(item, candidate_id, research_run_id):
            continue
        if item.signal_type != SIGNAL_CONTENT_OBSERVATION:
            continue
        if item.purpose != EvidencePurpose.CONTENT:
            continue
        # The record, not the payload, decides whether anything was observed.
        if item.truth_class != TruthClass.OBSERVED:
            continue

        payload = item.raw_payload or {}
        video_id = _payload_string(payload, "video_id")
        if video_id is None:
            continue

        fingerprint = (item.signal_type, video_id, item.raw_payload_hash)
        if item.raw_payload_hash is not None and fingerprint in seen:
            suppressed += 1
            continue
        seen.add(fingerprint)
        contributing.append(item)

        if video_id not in by_video:
            by_video[video_id] = _VideoAccumulator(video_id=video_id)
            order.append(video_id)
        accumulator = by_video[video_id]
        accumulator.evidence_ids.append(item.id)

        # COLLECT every observed value rather than letting the first arrival
        # win. Resolution happens afterwards, so the outcome is a function of
        # the set of records and not of the order they arrived in.
        for field, reader in (
            ("channel_id", lambda pl: _payload_string(pl, "channel_id")),
            ("title", lambda pl: _payload_string(pl, "title")),
            ("tags", _payload_tags),
            ("category", lambda pl: _payload_string(pl, "category")),
            ("duration_seconds", _payload_duration),
        ):
            value = reader(payload)
            if value is not None:
                accumulator.values[field].add(value)

    records = [_resolve(by_video[video_id]) for video_id in order]
    return records, suppressed, contributing


def _resolve(accumulator: _VideoAccumulator) -> _VideoRecord:
    """Turn collected values into one record, flagging any disagreement.

    Exactly one distinct observed value makes a field AVAILABLE. None makes
    it UNAVAILABLE. Two or more makes it CONFLICTING: the field is excluded
    from pattern extraction and reported, because picking a winner between
    disagreeing OBSERVED records would be arbitrary, and picking the first
    would make the result depend on arrival order.
    """
    resolved: dict[str, object] = {}
    states: dict[str, FieldState] = {}
    for field in _FIELDS:
        values = accumulator.values[field]
        if not values:
            resolved[field] = None
            states[field] = FieldState.UNAVAILABLE
        elif len(values) == 1:
            resolved[field] = next(iter(values))
            states[field] = FieldState.AVAILABLE
        else:
            resolved[field] = None
            states[field] = FieldState.CONFLICTING
    return _VideoRecord(
        video_id=accumulator.video_id,
        channel_id=resolved["channel_id"],  # type: ignore[arg-type]
        title=resolved["title"],  # type: ignore[arg-type]
        tags=resolved["tags"],  # type: ignore[arg-type]
        category=resolved["category"],  # type: ignore[arg-type]
        duration_seconds=resolved["duration_seconds"],  # type: ignore[arg-type]
        states=states,
        evidence_ids=tuple(sorted(accumulator.evidence_ids)),
    )


def _feature_values(record: _VideoRecord) -> dict[PatternKind, tuple[str, ...]]:
    """Every pattern value this video contributes, per kind.

    A field the provider did not return contributes NOTHING and is separately
    counted as unavailable; it never contributes an empty value.
    """
    values: dict[PatternKind, tuple[str, ...]] = {}

    if record.title is not None:
        tokens = tokenize_title(record.title)
        # Deduplicated within one video: a word repeated in one title is one
        # video's use of it, never two independent occurrences.
        values[PatternKind.TITLE_TOKEN] = tuple(sorted(set(tokens)))
        bigrams = {
            f"{first} {second}" for first, second in zip(tokens, tokens[1:])
        }
        values[PatternKind.TITLE_BIGRAM] = tuple(sorted(bigrams))

    if record.tags is not None:
        normalized = {normalize_text(tag) for tag in record.tags}
        values[PatternKind.TAG] = tuple(sorted(tag for tag in normalized if tag))

    if record.category is not None:
        normalized_category = normalize_text(record.category)
        if normalized_category:
            values[PatternKind.CATEGORY] = (normalized_category,)

    if record.duration_seconds is not None:
        values[PatternKind.DURATION_BAND] = (
            duration_band(record.duration_seconds).value,
        )

    return values


# Which record attribute backs each kind, for availability accounting.
_KIND_FIELD: dict[PatternKind, str] = {
    PatternKind.TITLE_TOKEN: "title",
    PatternKind.TITLE_BIGRAM: "title",
    PatternKind.TAG: "tags",
    PatternKind.CATEGORY: "category",
    PatternKind.DURATION_BAND: "duration_seconds",
}


def _concentration(
    channel_counts: dict[str, int], occurrence_count: int, unattributed: int
) -> tuple[float | None, ConcentrationState]:
    """Largest known creator's share of ALL this pattern's occurrences.

    The denominator is `occurrence_count`, never the attributed subset. An
    earlier version divided by the attributed occurrences alone, so one known
    creator among ten occurrences — the other nine unattributed — reported a
    share of 1.0 and a state of CREATOR_CONCENTRATED. Unknown creator
    identity was strengthening the evidence that a known creator dominated,
    which inverts the safeguard this figure exists to provide.

    The state is conservative in BOTH directions when identity is incomplete:

    - A share at or above the threshold is established whatever the
      unattributed occurrences turn out to be, since resolving them can only
      raise that creator's count or add another; it is a lower bound, so
      CREATOR_CONCENTRATED is safe to report.
    - Below the threshold, breadth is NOT established either: every
      unattributed occurrence could belong to the largest known creator. The
      pattern is therefore CREATOR_PARTIALLY_UNKNOWN rather than
      MULTI_CREATOR, which would overstate how many creators carry it.

    SINGLE_CREATOR and MULTI_CREATOR are reserved for fully attributed
    patterns, where the spread is actually known.
    """
    if not channel_counts or occurrence_count <= 0:
        return None, ConcentrationState.CREATOR_UNKNOWN

    top_share = round(max(channel_counts.values()) / occurrence_count, 4)

    if unattributed > 0:
        if top_share >= CONCENTRATION_SHARE:
            return top_share, ConcentrationState.CREATOR_CONCENTRATED
        return top_share, ConcentrationState.CREATOR_PARTIALLY_UNKNOWN

    if len(channel_counts) == 1:
        return top_share, ConcentrationState.SINGLE_CREATOR
    if top_share >= CONCENTRATION_SHARE:
        return top_share, ConcentrationState.CREATOR_CONCENTRATED
    return top_share, ConcentrationState.MULTI_CREATOR


def _outlier_video_ids(
    outlier_evidence: RobustContentIntelligenceResult | None,
    candidate_id: UUID,
    research_run_id: UUID | None,
) -> tuple[dict[str, str | None] | None, bool]:
    """Video ids Milestone 5A scored above their own creator's baseline.

    Returns (video ids, scope_mismatch). The ids are None when 5A supplied
    nothing, which is different from 5A having run and found no qualifying
    observation, and different again from a scope mismatch.

    The 5A result carries its own candidate_id and research_run_id, and both
    are VERIFIED against this derivation's scope before a single observation
    is consumed. Video ids are only unique within a scope, so a 5A result
    from another candidate or run could otherwise contaminate co-occurrence
    through colliding ids — silently, and with no trace in the output.
    """
    if outlier_evidence is None:
        return None, False
    if (
        outlier_evidence.candidate_id != candidate_id
        or outlier_evidence.research_run_id != research_run_id
    ):
        return None, True
    above: dict[str, str | None] = {}
    for observation in outlier_evidence.observations:
        if observation.state != RobustOutlierObservationState.SCORED:
            continue
        if observation.video_id is None:
            continue
        if observation.relative_score is None:
            continue
        if observation.relative_score > RELATIVE_SCORE_ABOVE_BASELINE:
            above[observation.video_id] = observation.channel_id
    return above, False


def _cooccurrence(
    kind: PatternKind,
    occurrences: tuple[PatternOccurrence, ...],
    outliers: dict[str, str | None] | None,
    comparable_outlier_total: int,
    prevalence_share: float,
    scope_mismatch: bool = False,
) -> OutlierCooccurrence:
    """Compare two shares. Never multiply, rank, or score them."""
    if outliers is None:
        return OutlierCooccurrence(
            state=(
                CooccurrenceState.OUTLIER_SCOPE_MISMATCH
                if scope_mismatch
                else CooccurrenceState.OUTLIER_EVIDENCE_UNAVAILABLE
            ),
            outlier_video_count=0,
            outlier_distinct_channel_count=0,
            comparable_outlier_total=0,
            share_among_outliers=None,
            share_in_corpus=prevalence_share,
        )
    if comparable_outlier_total == 0:
        return OutlierCooccurrence(
            state=CooccurrenceState.NO_QUALIFYING_OUTLIERS,
            outlier_video_count=0,
            outlier_distinct_channel_count=0,
            comparable_outlier_total=0,
            share_among_outliers=None,
            share_in_corpus=prevalence_share,
        )

    matching = [o for o in occurrences if o.video_id in outliers]
    channels = {o.channel_id for o in matching if o.channel_id is not None}
    share = round(len(matching) / comparable_outlier_total, 4)

    if not matching:
        state = CooccurrenceState.ABSENT_AMONG_OUTLIERS
    elif len(channels) < MIN_INDEPENDENT_CREATORS_FOR_COOCCURRENCE:
        state = CooccurrenceState.INSUFFICIENT_INDEPENDENT_CREATORS
    elif share >= prevalence_share * SHARE_DIVERGENCE_RATIO:
        state = CooccurrenceState.OVER_REPRESENTED
    elif prevalence_share >= share * SHARE_DIVERGENCE_RATIO:
        state = CooccurrenceState.UNDER_REPRESENTED
    else:
        state = CooccurrenceState.COMPARABLE

    return OutlierCooccurrence(
        state=state,
        outlier_video_count=len(matching),
        outlier_distinct_channel_count=len(channels),
        comparable_outlier_total=comparable_outlier_total,
        share_among_outliers=share,
        share_in_corpus=prevalence_share,
    )


def _build_provenance(
    candidate_id: UUID,
    research_run_id: UUID | None,
    records: list[_VideoRecord],
    contributing: list[EvidenceItem],
    suppressed: int,
) -> ContentPatternProvenance:
    return ContentPatternProvenance(
        candidate_id=candidate_id,
        research_run_id=research_run_id,
        evidence_ids=tuple(sorted(item.id for item in contributing)),
        video_ids=tuple(sorted((r.video_id for r in records), key=_id_sort_key)),
        channel_ids=tuple(
            sorted(
                {r.channel_id for r in records if r.channel_id is not None},
                key=_id_sort_key,
            )
        ),
        providers=tuple(sorted({item.provider for item in contributing})),
        platforms=tuple(
            sorted({item.platform for item in contributing if item.platform})
        ),
        source_truth_classes=tuple(
            sorted({item.truth_class.value for item in contributing})
        ),
        duplicate_evidence_suppressed=suppressed,
    )


def _empty(
    candidate_id: UUID,
    research_run_id: UUID | None,
    pattern_state: ContentPatternsState,
    dimension_state: DimensionState,
    provenance: ContentPatternProvenance,
    missing_reason: str,
    *,
    field_availability: tuple[FieldAvailability, ...] = (),
    observed_video_count: int = 0,
    videos_without_channel_id: int = 0,
    videos_with_conflicting_channel_id: int = 0,
    distinct_channel_count: int = 0,
    outlier_observation_count: int = 0,
    outlier_distinct_channel_count: int = 0,
    outlier_evidence_available: bool = False,
) -> ContentPatternsResult:
    return ContentPatternsResult(
        candidate_id=candidate_id,
        research_run_id=research_run_id,
        state=dimension_state,
        pattern_state=pattern_state,
        state_boundary=STATE_BOUNDARIES[pattern_state],
        value=None,
        patterns=(),
        field_availability=field_availability,
        provenance=provenance,
        observed_video_count=observed_video_count,
        videos_without_channel_id=videos_without_channel_id,
        videos_with_conflicting_channel_id=videos_with_conflicting_channel_id,
        distinct_channel_count=distinct_channel_count,
        outlier_observation_count=outlier_observation_count,
        outlier_distinct_channel_count=outlier_distinct_channel_count,
        outlier_evidence_available=outlier_evidence_available,
        features_truth_class=TruthClass.INFERRED if observed_video_count else None,
        missing_reason=missing_reason,
    )


# ----------------------------------------------------------------- extraction


def derive_content_patterns(
    candidate_id: UUID,
    evidence: list[EvidenceItem],
    research_run_id: UUID | None = None,
    outlier_evidence: RobustContentIntelligenceResult | None = None,
    missing_reasons: dict[str, str] | None = None,
) -> ContentPatternsResult:
    """Extract recurring content patterns for one candidate and one scope.

    Deterministic and pure: the same evidence yields the same result whatever
    order it arrives in. No provider calls, no network I/O, no store mutation,
    no LLM, and no value that could be read as performance.

    `outlier_evidence` is Milestone 5A's result for the SAME scope, used only
    to report co-occurrence. Supplying None is a distinct, reported state and
    never suppresses pattern extraction.

    `missing_reasons` maps a capability name to why it produced nothing, so a
    failed public-content call can never read as a field without patterns.
    """
    reasons = missing_reasons or {}
    records, suppressed, contributing = _collect_videos(
        candidate_id, evidence, research_run_id
    )
    provenance = _build_provenance(
        candidate_id, research_run_id, records, contributing, suppressed
    )

    capability_reason = reasons.get(CAPABILITY_PUBLIC_CONTENT)
    if not records:
        if capability_reason is not None:
            return _empty(
                candidate_id,
                research_run_id,
                ContentPatternsState.CONTENT_EVIDENCE_UNAVAILABLE,
                DimensionState.MISSING,
                provenance,
                capability_reason,
            )
        return _empty(
            candidate_id,
            research_run_id,
            ContentPatternsState.NO_CONTENT_OBSERVED,
            DimensionState.UNKNOWN,
            provenance,
            "no_public_content_observed_for_candidate",
        )

    videos_without_channel = sum(
        1 for r in records if r.states["channel_id"] is FieldState.UNAVAILABLE
    )
    videos_with_conflicting_channel = sum(
        1 for r in records if r.states["channel_id"] is FieldState.CONFLICTING
    )
    distinct_channels = len({r.channel_id for r in records if r.channel_id is not None})

    per_video = {r.video_id: _feature_values(r) for r in records}

    availability = tuple(
        FieldAvailability(
            field=field,
            available_video_count=sum(
                1 for r in records if r.states[field] is FieldState.AVAILABLE
            ),
            unavailable_video_count=sum(
                1 for r in records if r.states[field] is FieldState.UNAVAILABLE
            ),
            conflicting_video_count=sum(
                1 for r in records if r.states[field] is FieldState.CONFLICTING
            ),
        )
        for field in ("title", "tags", "category", "duration_seconds")
    )

    outliers, outlier_scope_mismatch = _outlier_video_ids(
        outlier_evidence, candidate_id, research_run_id
    )
    outlier_channels = (
        len({channel for channel in outliers.values() if channel is not None})
        if outliers is not None
        else 0
    )

    if all(entry.available_video_count == 0 for entry in availability):
        return _empty(
            candidate_id,
            research_run_id,
            ContentPatternsState.METADATA_UNAVAILABLE,
            DimensionState.UNKNOWN,
            provenance,
            "no_content_metadata_observed_for_candidate",
            field_availability=availability,
            observed_video_count=len(records),
            videos_without_channel_id=videos_without_channel,
            videos_with_conflicting_channel_id=videos_with_conflicting_channel,
            distinct_channel_count=distinct_channels,
            outlier_observation_count=len(outliers) if outliers is not None else 0,
            outlier_distinct_channel_count=outlier_channels,
            outlier_evidence_available=outliers is not None,
        )

    by_field_available: dict[PatternKind, int] = {}
    for kind, field in _KIND_FIELD.items():
        by_field_available[kind] = sum(
            1 for r in records if r.states[field] is FieldState.AVAILABLE
        )

    # Comparable outlier denominators are per FIELD: an outlier whose title
    # the provider never returned cannot carry a title pattern, so counting
    # it in the denominator would understate every title pattern's share.
    comparable_outlier_totals: dict[PatternKind, int] = {}
    for kind, field in _KIND_FIELD.items():
        if outliers is None:
            comparable_outlier_totals[kind] = 0
            continue
        comparable_outlier_totals[kind] = sum(
            1
            for r in records
            if r.video_id in outliers
            and r.states[field] is FieldState.AVAILABLE
        )

    grouped: dict[tuple[PatternKind, str], list[_VideoRecord]] = {}
    for record in records:
        for kind, values in per_video[record.video_id].items():
            for value in values:
                grouped.setdefault((kind, value), []).append(record)

    patterns: list[ContentPattern] = []
    for (kind, value), contributors in grouped.items():
        if len(contributors) < MIN_PATTERN_VIDEO_COUNT:
            continue
        ordered = sorted(contributors, key=lambda r: _id_sort_key(r.video_id))
        occurrences = tuple(
            PatternOccurrence(
                video_id=r.video_id,
                channel_id=r.channel_id,
                channel_state=r.states["channel_id"],
                evidence_ids=r.evidence_ids,
            )
            for r in ordered
        )
        channel_counts: dict[str, int] = {}
        for r in ordered:
            if r.channel_id is not None:
                channel_counts[r.channel_id] = channel_counts.get(r.channel_id, 0) + 1
        without_channel = sum(
            1 for r in ordered if r.states["channel_id"] is FieldState.UNAVAILABLE
        )
        conflicting_channel = sum(
            1 for r in ordered if r.states["channel_id"] is FieldState.CONFLICTING
        )
        # Both kinds leave the occurrence unattributed, and both must count
        # against a dominance claim; only their REPORTING is kept separate.
        top_share, concentration = _concentration(
            channel_counts, len(ordered), without_channel + conflicting_channel
        )
        available = by_field_available[kind]
        prevalence = round(len(ordered) / available, 4) if available else 0.0

        patterns.append(
            ContentPattern(
                kind=kind,
                value=value,
                video_count=len(ordered),
                field_available_video_count=available,
                prevalence_share=prevalence,
                distinct_channel_count=len(channel_counts),
                videos_without_channel_id=without_channel,
                videos_with_conflicting_channel_id=conflicting_channel,
                top_channel_share=top_share,
                concentration=concentration,
                occurrences=occurrences,
                evidence_ids=tuple(
                    sorted({eid for o in occurrences for eid in o.evidence_ids})
                ),
                cooccurrence=_cooccurrence(
                    kind,
                    occurrences,
                    outliers,
                    comparable_outlier_totals[kind],
                    prevalence,
                    outlier_scope_mismatch,
                ),
            )
        )

    # Canonical order: kind, then value. Never by count, which would be an
    # implicit ranking of patterns against each other.
    patterns.sort(key=lambda p: (p.kind.value, p.value))

    if not patterns:
        return _empty(
            candidate_id,
            research_run_id,
            ContentPatternsState.NO_RECURRING_PATTERNS,
            DimensionState.UNKNOWN,
            provenance,
            "no_feature_recurred_across_observed_content",
            field_availability=availability,
            observed_video_count=len(records),
            videos_without_channel_id=videos_without_channel,
            videos_with_conflicting_channel_id=videos_with_conflicting_channel,
            distinct_channel_count=distinct_channels,
            outlier_observation_count=len(outliers) if outliers is not None else 0,
            outlier_distinct_channel_count=outlier_channels,
            outlier_evidence_available=outliers is not None,
        )

    state = ContentPatternsState.PATTERNS_OBSERVED
    return ContentPatternsResult(
        candidate_id=candidate_id,
        research_run_id=research_run_id,
        state=DimensionState.EVIDENCE_PRESENT_UNSCORED,
        pattern_state=state,
        state_boundary=STATE_BOUNDARIES[state],
        value=None,
        patterns=tuple(patterns),
        field_availability=availability,
        provenance=provenance,
        observed_video_count=len(records),
        videos_without_channel_id=videos_without_channel,
        videos_with_conflicting_channel_id=videos_with_conflicting_channel,
        distinct_channel_count=distinct_channels,
        outlier_observation_count=len(outliers) if outliers is not None else 0,
        outlier_distinct_channel_count=outlier_channels,
        outlier_evidence_available=outliers is not None,
        features_truth_class=TruthClass.INFERRED,
    )
