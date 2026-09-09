"""Candidate discovery: seed keyword -> structured, deduplicated candidates.

Generation is not evidence. This service only proposes opportunities; every
candidate leaves here UNRESEARCHED with no demand claims attached. Research
and scoring happen in later milestones.
"""

import re
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from app.domain.enums import CandidateStatus
from app.domain.models import Candidate
from app.providers.llm import CandidateGenerationError, CandidateGenerationProvider
from app.services.candidate_dedupe import dedupe_candidates
from app.services.candidate_validation import validate_candidate_format

DEFAULT_TARGET_COUNT = 20
# Ask providers for extra raw candidates so dedupe/rejection still leaves ~target.
_OVERGENERATION = 5

_MIN_SEED_LENGTH = 2
_MAX_SEED_LENGTH = 80
_SEED_HAS_WORD_RE = re.compile(r"[a-zA-Z0-9]")
_WHITESPACE_RE = re.compile(r"\s+")


class InvalidSeedKeywordError(ValueError):
    """The seed keyword cannot be used for discovery."""


@dataclass(slots=True)
class RejectedCandidate:
    title: str
    reason: str


@dataclass(slots=True)
class DiscoveryResult:
    seed_keyword: str
    candidates: list[Candidate]
    generated_count: int
    duplicates_removed: int
    rejected: list[RejectedCandidate] = field(default_factory=list)


def normalize_seed_keyword(seed_keyword: str) -> str:
    normalized = _WHITESPACE_RE.sub(" ", seed_keyword.strip()).lower()
    if len(normalized) < _MIN_SEED_LENGTH:
        raise InvalidSeedKeywordError("seed_keyword must be at least 2 characters")
    if len(normalized) > _MAX_SEED_LENGTH:
        raise InvalidSeedKeywordError("seed_keyword must be at most 80 characters")
    if not _SEED_HAS_WORD_RE.search(normalized):
        raise InvalidSeedKeywordError("seed_keyword must contain letters or digits")
    return normalized


def _normalize_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return _WHITESPACE_RE.sub(" ", value.strip())


def _normalize_queries(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    seen: set[str] = set()
    queries: list[str] = []
    for item in value:
        query = _normalize_text(item).lower()
        if query and query not in seen:
            seen.add(query)
            queries.append(query)
    return queries


def normalize_raw_candidate(raw: dict[str, Any], seed_keyword: str) -> dict[str, Any]:
    """Coerce a provider's raw dict into clean field values (format unresolved)."""
    return {
        "seed_keyword": seed_keyword,
        "title": _normalize_text(raw.get("title")),
        "problem": _normalize_text(raw.get("problem")),
        "target_buyer": _normalize_text(raw.get("target_buyer")),
        "proposed_format": raw.get("proposed_format"),
        "buyer_outcome": _normalize_text(raw.get("buyer_outcome")),
        "generation_reason": _normalize_text(raw.get("generation_reason")),
        "search_queries": _normalize_queries(raw.get("search_queries")),
        "marketplace_queries": _normalize_queries(raw.get("marketplace_queries")),
        "content_queries": _normalize_queries(raw.get("content_queries")),
    }


_REQUIRED_TEXT_FIELDS = ("title", "problem", "target_buyer", "buyer_outcome", "generation_reason")
_REQUIRED_QUERY_FIELDS = ("search_queries", "marketplace_queries", "content_queries")


async def discover_candidates(
    seed_keyword: str,
    provider: CandidateGenerationProvider,
    target_count: int = DEFAULT_TARGET_COUNT,
) -> DiscoveryResult:
    """Run the discovery pipeline: generate -> normalize -> validate -> dedupe."""
    normalized_seed = normalize_seed_keyword(seed_keyword)

    try:
        raw_candidates = await provider.generate(normalized_seed, target_count + _OVERGENERATION)
    except CandidateGenerationError:
        raise
    except Exception as exc:  # noqa: BLE001 - provider internals are untrusted
        raise CandidateGenerationError(
            f"provider '{provider.name}' failed while generating candidates: {exc}"
        ) from exc

    if not isinstance(raw_candidates, list):
        raise CandidateGenerationError(
            f"provider '{provider.name}' returned {type(raw_candidates).__name__}, expected a list"
        )

    candidates: list[Candidate] = []
    rejected: list[RejectedCandidate] = []

    for raw in raw_candidates:
        if not isinstance(raw, dict):
            rejected.append(RejectedCandidate(title="", reason="malformed_candidate: not an object"))
            continue

        normalized = normalize_raw_candidate(raw, normalized_seed)

        missing = [f for f in _REQUIRED_TEXT_FIELDS if not normalized[f]]
        missing += [f for f in _REQUIRED_QUERY_FIELDS if not normalized[f]]
        if missing:
            rejected.append(
                RejectedCandidate(
                    title=normalized["title"],
                    reason=f"missing_fields: {', '.join(missing)}",
                )
            )
            continue

        resolved_format, rejection_reason = validate_candidate_format(
            normalized, seed_keyword=normalized_seed
        )
        if rejection_reason:
            rejected.append(RejectedCandidate(title=normalized["title"], reason=rejection_reason))
            continue
        normalized["proposed_format"] = resolved_format

        try:
            candidate = Candidate(status=CandidateStatus.UNRESEARCHED, **normalized)
        except ValidationError as exc:
            rejected.append(
                RejectedCandidate(title=normalized["title"], reason=f"invalid_candidate: {exc.error_count()} field error(s)")
            )
            continue
        candidates.append(candidate)

    deduped, removed = dedupe_candidates(candidates)

    return DiscoveryResult(
        seed_keyword=normalized_seed,
        candidates=deduped[:target_count],
        generated_count=len(raw_candidates),
        duplicates_removed=len(removed),
        rejected=rejected,
    )
