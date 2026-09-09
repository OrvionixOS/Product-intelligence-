"""Semantic deduplication for generated candidates.

Uses token-overlap similarity over normalized title + problem text. This is
deliberately dependency-free and deterministic; an embedding-based dedupe can
replace `similarity` later without changing callers.
"""

import re

from app.domain.models import Candidate

# Words too generic to distinguish candidates within a single seed keyword.
_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "because", "by", "for", "from",
    "get", "have", "how", "in", "into", "is", "it", "its", "of", "on", "or",
    "our", "so", "that", "the", "their", "they", "this", "to", "with", "without",
    "you", "your", "not", "do", "does", "no", "can", "cannot", "people", "who",
}

_TOKEN_RE = re.compile(r"[a-z0-9]+")

# Similarity at or above this threshold marks the later candidate a duplicate.
DUPLICATE_THRESHOLD = 0.62


def _stem(token: str) -> str:
    for suffix in ("ings", "ing", "ers", "er", "ies", "es", "s", "ed"):
        if token.endswith(suffix) and len(token) - len(suffix) >= 3:
            return token[: -len(suffix)]
    return token


def normalize_tokens(text: str) -> set[str]:
    tokens = _TOKEN_RE.findall(text.lower())
    return {_stem(t) for t in tokens if t not in _STOPWORDS}


def similarity(a: Candidate, b: Candidate) -> float:
    """Jaccard similarity over normalized title+problem tokens, with the seed
    keyword's own tokens removed so shared niche words don't inflate overlap."""
    seed_tokens = normalize_tokens(a.seed_keyword) | normalize_tokens(b.seed_keyword)
    tokens_a = (normalize_tokens(a.title) | normalize_tokens(a.problem)) - seed_tokens
    tokens_b = (normalize_tokens(b.title) | normalize_tokens(b.problem)) - seed_tokens
    if not tokens_a or not tokens_b:
        # Nothing distinctive beyond the seed keyword: same format means same product.
        return 1.0 if a.proposed_format == b.proposed_format else 0.0
    intersection = len(tokens_a & tokens_b)
    union = len(tokens_a | tokens_b)
    return intersection / union


def dedupe_candidates(
    candidates: list[Candidate],
    threshold: float = DUPLICATE_THRESHOLD,
) -> tuple[list[Candidate], list[Candidate]]:
    """Return (kept, removed). Earlier candidates win; later near-duplicates
    are removed. Identical normalized titles are duplicates regardless of score."""
    kept: list[Candidate] = []
    removed: list[Candidate] = []
    seen_titles: set[frozenset[str]] = set()

    for candidate in candidates:
        title_key = frozenset(normalize_tokens(candidate.title))
        if title_key and title_key in seen_titles:
            removed.append(candidate)
            continue
        if any(similarity(candidate, existing) >= threshold for existing in kept):
            removed.append(candidate)
            continue
        kept.append(candidate)
        if title_key:
            seen_titles.add(title_key)

    return kept, removed
