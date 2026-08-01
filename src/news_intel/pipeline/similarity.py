from __future__ import annotations

from rapidfuzz import fuzz


def title_similarity_score(a: str, b: str) -> float:
    """0-100 token-order-insensitive similarity."""
    return fuzz.token_sort_ratio(a, b)


def _trigrams(s: str) -> set[str]:
    s = s.replace(" ", "")
    if len(s) < 3:
        return {s} if s else set()
    return {s[i : i + 3] for i in range(len(s) - 2)}


def trigram_jaccard(a: str, b: str) -> float:
    ta, tb = _trigrams(a), _trigrams(b)
    if not ta or not tb:
        return 0.0
    intersection = len(ta & tb)
    union = len(ta | tb)
    return intersection / union if union else 0.0


def entity_overlap_ratio(entities_a: list[str], entities_b: list[str]) -> float:
    sa, sb = {e.lower() for e in entities_a}, {e.lower() for e in entities_b}
    if not sa or not sb:
        return 0.0
    smaller = min(len(sa), len(sb))
    return len(sa & sb) / smaller if smaller else 0.0
