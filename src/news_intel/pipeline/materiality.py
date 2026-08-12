"""Deterministic materiality signal (2026-08-12 scoring repair).

Before this module, `significance` (pipeline/scoring.py) was computed as
`40 + 25*has_t0 + confirmation_quality*0.25` -- a formula built entirely
from source tier and confirmation state, with zero signal about what
actually happened. That conflated source authority/evidence confidence
with event importance: a routine T0-sourced vendor blog post structurally
outranked a genuinely material single-sourced world/markets story, which
plateaued at exactly 47.5 regardless of substance (2026-08-11 diagnosis).

This module computes materiality from the event's own text -- a magnitude
signal (dollar figures, percentages, large numbers) and a keyword signal
(decision/consequence language vs. routine PR/announcement language) --
independent of source tier and confirmation state, which remain their own
separately-weighted components (confirmation_quality, corroboration) in
pipeline/scoring.py.

This is a deterministic, inspectable keyword/magnitude heuristic, not a
claim of true semantic understanding -- there is no LLM in this path. It is
deliberately conservative and documented so its effect on real candidates
can be shown, not just asserted.
"""

from __future__ import annotations

import re

_MAGNITUDE_RE = re.compile(
    r"\$\s?\d[\d,]*(?:\.\d+)?\s?(?:billion|million|trillion|bn|mn|tn)?"
    r"|\b\d[\d,]*(?:\.\d+)?\s?(?:billion|million|trillion|percent|%)\b",
    re.IGNORECASE,
)

# Decision/consequence language: something was actually decided, changed,
# or happened -- as opposed to a routine product/marketing update. Deliberate
# substring stems (e.g. "reschedul" matches "rescheduling"/"reschedules") so
# ordinary inflection doesn't require an exhaustive word list.
HIGH_IMPACT_TERMS: frozenset[str] = frozenset(
    {
        "sanction", "tariff", "export control", "import ban", "blacklist",
        "entity list", "embargo", "blockad",
        "ban ", "banned", "indict", "lawsuit", "sues", "sued", "settlement",
        "fine ", "penalt", "fraud", "antitrust", "monopoly", "convict",
        "acquit", "sentenc", "probe", "investigat",
        "resign", "oust", "depos", "dissolv", "impeach", "veto",
        "election", "referend", "inaugurat", "nomina", "confirm ",
        "coup", "ceasefire", "truce", "invas", "occupat", "war ",
        "strike ", "walkout", "protest", "riot", "unrest",
        "recall", "default", "bankrupt", "bailout", "collapse", "downgrade",
        "upgrade", "acquisition", "merger", "layoff", "shutdown",
        "surge", "plunge", "crash", "selloff", "rally", "volatil",
        "recession", "inflation", "rate hike", "rate cut", "hawkish",
        "dovish", "fed decision",
        "reschedul", "reclassif", "regulat",
        "earthquake", "hurricane", "wildfire", "flood", "outbreak",
        "pandemic", "epidemic", "emergency", "casualties", "killed",
        "died", "explosion", "hack", "breach", "leak",
    }
)

# Routine PR/marketing language -- present even in genuinely T0-sourced
# content, and on its own is evidence of a routine update, not an event.
ROUTINE_TERMS: frozenset[str] = frozenset(
    {
        "announces", "announced", "today announced", "unveils", "unveiled",
        "introduces", "introduced", "delivers", "brings", "adds ",
        "available now", "now available", "partners with", "celebrates",
        "showcases", "excited to", "proud to", "is becoming",
    }
)

_MAGNITUDE_WEIGHT = 25.0
_MAGNITUDE_CAP = 40.0
_HIGH_IMPACT_WEIGHT = 15.0
_HIGH_IMPACT_CAP = 30.0
_ROUTINE_WEIGHT = 6.0
_ROUTINE_CAP = 18.0

# A modest per-category prior (not a per-source-tier or confirmation-state
# multiplier): the product is intentionally weighted toward AI/Semiconductors
# and Markets/Politics/US-China, but the gap is small enough that real text
# content -- not category membership alone -- drives most of the score.
CATEGORY_MATERIALITY_BASE: dict[str, float] = {
    "ai_semiconductor_export_controls": 50.0,
    "markets_macro_corporate": 50.0,
    "us_china_policy_geopolitics": 50.0,
    "tech_platforms_datacenter": 40.0,
    "routine_politics_general": 40.0,
}
DEFAULT_MATERIALITY_BASE = 35.0


def _count_hits(text_lower: str, terms: frozenset[str]) -> int:
    return sum(1 for term in terms if term in text_lower)


def compute_materiality(text: str, bucket: str) -> float:
    """0-100. `text` should be the combined title+excerpt (all attached
    sources) so a magnitude/impact signal anywhere in the coverage counts."""
    lowered = text.lower()
    category_base = CATEGORY_MATERIALITY_BASE.get(bucket, DEFAULT_MATERIALITY_BASE)
    magnitude_hits = len(_MAGNITUDE_RE.findall(text))
    high_impact_hits = _count_hits(lowered, HIGH_IMPACT_TERMS)
    routine_hits = _count_hits(lowered, ROUTINE_TERMS)

    score = category_base
    score += min(_MAGNITUDE_CAP, magnitude_hits * _MAGNITUDE_WEIGHT)
    score += min(_HIGH_IMPACT_CAP, high_impact_hits * _HIGH_IMPACT_WEIGHT)
    score -= min(_ROUTINE_CAP, routine_hits * _ROUTINE_WEIGHT)
    return max(0.0, min(100.0, score))
