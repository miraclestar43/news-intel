"""Lightweight, deterministic entity extraction for v1.

Not NLP: matches a known-entity vocabulary from config/topics.yaml (fast,
predictable, easy to fixture-test) plus a conservative fallback that pulls
out capitalized multi-word sequences as candidate proper nouns. This is
enough to drive dedupe/clustering and rule scoring; the LLM enrichment
stage may propose a richer entity list per event, but code never depends on
that for clustering decisions.
"""

from __future__ import annotations

import re

_CAP_SEQ_RE = re.compile(r"\b([A-Z][a-zA-Z0-9&.]*(?:\s+[A-Z][a-zA-Z0-9&.]*){0,3})\b")

# Words that are commonly capitalized (sentence starts, generic terms) and
# should not be treated as entities on their own.
_STOPWORDS = {
    "The",
    "A",
    "An",
    "This",
    "That",
    "It",
    "In",
    "On",
    "For",
    "New",
    "Why",
    "What",
    "How",
}


def known_entities(topics_groups: dict) -> set[str]:
    known: set[str] = set()
    for group in topics_groups.values():
        for ent in group.get("entities", []) or []:
            known.add(ent)
    return known


def extract_known_entities(text: str, known: set[str]) -> list[str]:
    """Known-vocabulary matches only, with no capitalized-phrase fallback.

    Used where extraction must be stable across cosmetic paraphrasing (e.g.
    material-fact snapshots for alert-repeat decisions) -- the capitalized-
    phrase heuristic in extract_entities() is wording-sensitive by design
    (good for clustering recall) and would misfire as a "new affected
    entity" every time a headline is merely reworded.
    """
    lowered = text.lower()
    return sorted(ent for ent in known if ent.lower() in lowered)


def extract_entities(text: str, known: set[str]) -> list[str]:
    found: set[str] = set()
    lowered = text.lower()
    for ent in known:
        if ent.lower() in lowered:
            found.add(ent)

    for match in _CAP_SEQ_RE.finditer(text):
        candidate = match.group(1).strip()
        if candidate in _STOPWORDS or len(candidate) < 3:
            continue
        if candidate.split()[0] in _STOPWORDS:
            continue
        found.add(candidate)

    return sorted(found)
