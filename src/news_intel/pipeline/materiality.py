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

# Dollar-figure magnitude, captured with its numeric value and unit so the
# bonus can scale with actual SIZE, not just presence (2026-08-13 fix: a
# website's "$100/day" running cost and a "$6 billion" acquisition
# previously earned the identical magnitude bonus just for containing a
# "$<number>" pattern -- see _dollar_magnitude_bonus). Unit accepts both
# spelled-out words and the single-letter abbreviations Techmeme-style
# headlines actually use ("$575M", "$6B").
_DOLLAR_RE = re.compile(
    r"\$\s?(\d[\d,]*(?:\.\d+)?)\s?(billion|million|trillion|bn|mn|tn|b|m|t)?\b",
    re.IGNORECASE,
)
_PERCENT_RE = re.compile(r"\b\d[\d,]*(?:\.\d+)?\s?(?:percent|%)", re.IGNORECASE)

_UNIT_MULTIPLIERS = {
    "billion": 1_000_000_000, "bn": 1_000_000_000, "b": 1_000_000_000,
    "million": 1_000_000, "mn": 1_000_000, "m": 1_000_000,
    "trillion": 1_000_000_000_000, "tn": 1_000_000_000_000, "t": 1_000_000_000_000,
}

# (floor_usd, bonus) -- largest parsed dollar figure decides the band;
# anything under $1M (a website's daily running cost, a small fine) earns
# no magnitude credit at all.
_DOLLAR_MAGNITUDE_BANDS: tuple[tuple[float, float], ...] = (
    (10_000_000_000.0, 40.0),
    (1_000_000_000.0, 32.0),
    (100_000_000.0, 22.0),
    (10_000_000.0, 12.0),
    (1_000_000.0, 5.0),
)
_PERCENT_MAGNITUDE_WEIGHT = 4.0
_PERCENT_MAGNITUDE_CAP = 12.0


def _parse_dollar_values(text: str) -> list[float]:
    values: list[float] = []
    for amount_str, unit in _DOLLAR_RE.findall(text):
        try:
            amount = float(amount_str.replace(",", ""))
        except ValueError:
            continue
        values.append(amount * _UNIT_MULTIPLIERS.get(unit.lower(), 1))
    return values


def _dollar_magnitude_bonus(text: str) -> float:
    values = _parse_dollar_values(text)
    if not values:
        return 0.0
    largest = max(values)
    for floor, bonus in _DOLLAR_MAGNITUDE_BANDS:
        if largest >= floor:
            return bonus
    return 0.0


def _percent_magnitude_bonus(text: str) -> float:
    # Percent changes are not scaled by parsed value (a 0.1% CPI move and a
    # 50% stock swing are both "percent" syntactically but very differently
    # significant, and that judgment needs context this module doesn't
    # have) -- kept as a small, flat, capped signal rather than a precise
    # one; documented as a limitation, not fixed here.
    hits = len(_PERCENT_RE.findall(text))
    return min(_PERCENT_MAGNITUDE_CAP, hits * _PERCENT_MAGNITUDE_WEIGHT)

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
    high_impact_hits = _count_hits(lowered, HIGH_IMPACT_TERMS)
    routine_hits = _count_hits(lowered, ROUTINE_TERMS)

    score = category_base
    score += _dollar_magnitude_bonus(text)
    score += _percent_magnitude_bonus(text)
    score += min(_HIGH_IMPACT_CAP, high_impact_hits * _HIGH_IMPACT_WEIGHT)
    score -= min(_ROUTINE_CAP, routine_hits * _ROUTINE_WEIGHT)
    return max(0.0, min(100.0, score))
