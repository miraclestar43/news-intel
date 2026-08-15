"""Stable event signature for digest-time story grouping and cross-day
novelty control (2026-08-12 content-quality repair).

Replaces the 2026-08-11 same-domain + same-Pacific-day + shared-capitalized-
token heuristic (pipeline/daily_selection.py's old `_same_story_family`),
which the 2026-08-12 diagnosis showed had two failures: (1) it falsely
merged two unrelated NVIDIA blog posts (the AI-financing story and a
Nemotron model release) because "NVIDIA" itself was never filtered as a
generic/publisher term -- any two same-day posts from the same publisher
that happened to both mention the company name would merge; (2) it could
never recognize the *same* announcement reported by two different
publishers, or reported again days later under a new headline/URL, because
it required an exact domain match and same-calendar-day.

This module instead extracts "material tokens" -- words from the event's
extracted entities (pipeline/entities.py, already stored as entities_json)
plus a capitalized-token fallback over title+excerpt -- with two kinds of
noise removed: generic industry terms (config/topics.yaml
trend_identity.generic_anchor_terms) and, critically, any word that is part
of *either* candidate's own publisher name. The publisher-name filter is
computed per-comparison (not a fixed company blocklist), so "NVIDIA" is
correctly dropped when comparing two NVIDIA-published articles but would
NOT be dropped if it showed up as a shared token between (say) a Reuters
article and an AP article that both happened to be about NVIDIA -- that
would still need the actual overlap of what the story is about ("Goldman
Sachs", "KKR", a specific transaction, a specific model name) to match, and
generic single-company mentions still won't manufacture a merge because
`_same_event_story` requires either (a) real material-token overlap or (b)
high title similarity -- never company-name overlap alone.

Two independent signals decide a match; either is sufficient (both must
survive the publisher/generic filtering):
  - shared material tokens (entities + capitalized words, filtered)
  - normalized title trigram similarity above a floor

A same-category prerequisite and a bounded time window (days) keep matches
plausible; there is no same-day or same-domain requirement, so the same
underlying event reported a day apart, or by two different outlets, both
group correctly -- and no domain requirement means cross-publisher
corroboration groups correctly too.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from news_intel.models import ConfirmationState
from news_intel.pipeline.entities import extract_entities
from news_intel.pipeline.material_facts import MaterialFactSnapshot, build_snapshot
from news_intel.pipeline.materiality import _parse_dollar_values, format_dollar_value
from news_intel.pipeline.normalize import normalize_title
from news_intel.pipeline.similarity import trigram_jaccard
from news_intel.settings import AlertTriggersConfig

TITLE_SIMILARITY_FLOOR = 0.5
DEFAULT_MAX_DAYS_APART = 10

# Connector/filler words that leak into capitalized-sequence entity
# extraction (pipeline/entities.py's multi-word proper-noun fallback) and
# are never themselves a meaningful anchor.
_CONNECTOR_WORDS = frozenset(
    {"With", "Over", "For", "And", "The", "This", "That", "New", "Now", "More", "Most", "From", "Into", "Its"}
)

# Cross-domain news/business boilerplate -- generic regardless of topic, so
# always filtered in addition to config's topic-specific generic_terms
# (which is oriented at AI/tech trend identity, not general news structure).
# Without this, two DIFFERENT quarterly reports sharing only "Earnings
# Call"/"Press Release"-style wording would falsely register as the same
# story.
_BOILERPLATE_TERMS = frozenset(
    {
        "Earnings", "Call", "Transcript", "Report", "Release", "Update",
        "Details", "Announcement", "Statement", "Press", "Conference",
        "Meeting", "Summary", "Overview", "Briefing", "Bulletin", "Notice",
        "Filing", "Presentation", "Schedule", "Results", "Quarter",
        "Quarterly", "Annual",
    }
)

# Standard macro/economic acronyms (2026-08-13 fix): a BLS "latest numbers"
# dashboard and an unrelated Asian-markets commentary piece both mention
# "CPI" -- shared generic economic terminology, not a shared distinctive
# entity. These are common enough, and short enough, that the ALL-CAPS
# acronym allowance (below) would otherwise treat any one of them as a
# valid anchor on its own.
_MACRO_ACRONYM_TERMS = frozenset(
    {
        "CPI", "GDP", "PPI", "FOMC", "ECI", "YOY", "YTD", "QOQ", "MOM",
        "GNP", "PCE", "ISM", "PMI", "CPIH", "NFP",
    }
)

# 2026-08-15 defect (found during the Aug 14 as-of replay): "Demand"
# survived as a "distinctive" material token and falsely merged a
# completely unrelated SMIC earnings story ("Strong Chip Demand") with the
# Aug 12 BLS "latest numbers" dashboard ("Producer Price Index - Final
# Demand") -- both entity phrases happen to end in this same generic
# economic-report word once split into individual tokens. Same root cause
# as the CPI/PBC fixes above: a single spelled-out generic economic term,
# not a shared distinctive entity. These are the words that make up
# _MACRO_ACRONYM_TERMS's spelled-out names (Price [Index], [Interest]
# Rate, [Chip] Demand, [Economic] Growth, [Money] Supply) and are exactly
# as generic on their own.
_GENERIC_ECONOMIC_TERMS = frozenset(
    {"Demand", "Price", "Prices", "Rate", "Rates", "Index", "Growth", "Supply", "Cost", "Costs", "Income"}
)

# 2026-08-14 defect: "PBC" (Public Benefit Corporation, a generic legal
# suffix -- Anthropic PBC, OpenAI's own entity is a PBC-adjacent structure
# too) survived as a short ALL-CAPS "acronym" token and matched between two
# completely unrelated M&A stories that both merely mentioned "Anthropic
# PBC" -- one as the actual acquirer (Anthropic buying Decart), the other
# as a competitor named in passing (SpaceX's Cursor acquisition, framed as
# catching up to "rivals Anthropic PBC and OpenAI"). A corporate-structure
# suffix is exactly the same class of noise as a macro acronym: shared
# terminology, not a shared distinctive entity.
_CORPORATE_SUFFIX_TERMS = frozenset(
    {"PBC", "INC", "LLC", "CORP", "LTD", "PLC", "NV", "SA", "AG", "CO"}
)


_WORD_SPLIT_RE = re.compile(r"[^A-Za-z0-9]+")


def _publisher_words(item: dict) -> set[str]:
    """Lowercased words from both the displayed publisher name (e.g. "NVIDIA
    newsroom") and the raw source id ("nvidia_newsroom") -- case-insensitive
    and split on any non-alphanumeric separator, so "NVIDIA" is filtered
    regardless of which form the publisher happens to be recorded in."""
    words: set[str] = set()
    for source in item.get("sources") or []:
        for raw in (source.get("publisher") or "", source.get("source_id") or ""):
            words.update(w.lower() for w in _WORD_SPLIT_RE.split(raw) if w)
    return words


def _material_tokens(item: dict, generic_terms: set[str]) -> set[str]:
    """Word-level tokens from entities_json (compound phrases split into
    individual words so "NVIDIA Nemotron" and "NVIDIA JetPack" contribute
    "Nemotron"/"JetPack", not two incomparable phrases) plus
    pipeline/entities.py's extract_entities() over title+excerpt as a
    fallback for events with no stored entities_json -- reused rather than
    a bespoke capitalized-word regex specifically because it already
    excludes ordinary sentence-initial capitalization ("Details", "Meet")
    from being treated as a proper noun, requiring a single-word candidate
    to be ALL-CAPS or contain a digit/hyphen. Filtered against generic
    industry terms, connector words, and this item's own publisher-name
    words (case-insensitively)."""
    publisher_words = _publisher_words(item)
    lowered_generic = {t.lower() for t in generic_terms}

    raw_tokens: set[str] = set()
    for phrase in item.get("entities") or []:
        raw_tokens.update(w.strip(".,()") for w in re.split(r"\s+", phrase) if w.strip(".,()"))
    text = f"{item.get('title', '')} {item.get('excerpt', '')}"
    for phrase in extract_entities(text, known=set()):
        raw_tokens.update(w.strip(".,()") for w in re.split(r"\s+", phrase) if w.strip(".,()"))

    return {
        t
        for t in raw_tokens
        if (len(t) >= 4 or t.isupper())  # short ALL-CAPS tokens (KKR, SEC, FDA) are acronyms, not noise
        and t not in _CONNECTOR_WORDS
        and t not in _BOILERPLATE_TERMS
        and t.upper() not in _MACRO_ACRONYM_TERMS
        and t.upper() not in _CORPORATE_SUFFIX_TERMS
        and t not in _GENERIC_ECONOMIC_TERMS
        and t.lower() not in lowered_generic
        and t.lower() not in publisher_words
    }


def _published_at(item: dict) -> datetime | None:
    sources = item.get("sources") or []
    if not sources or not sources[0].get("published_at"):
        return None
    try:
        return datetime.fromisoformat(sources[0]["published_at"])
    except ValueError:
        return None


def _within_time_window(a: dict, b: dict, max_days: int) -> bool:
    ta, tb = _published_at(a), _published_at(b)
    if ta is None or tb is None:
        return True  # unknown timestamps never block a match on their own
    return abs((ta - tb).days) <= max_days


_DIGIT_SEQ_RE = re.compile(r"\d+")


def _digit_sequences(title: str) -> set[str]:
    return set(_DIGIT_SEQ_RE.findall(title))


def same_event_story(
    a: dict, b: dict, *, generic_terms: set[str], max_days: int = DEFAULT_MAX_DAYS_APART
) -> bool:
    """True if `a` and `b` are the same underlying event/announcement
    (possibly different articles, different publishers, different dates).
    Deliberately has NO domain-equality and NO same-day requirement -- see
    module docstring."""
    if a["category"] != b["category"]:
        return False
    if not _within_time_window(a, b, max_days):
        return False

    tokens_a = _material_tokens(a, generic_terms)
    tokens_b = _material_tokens(b, generic_terms)
    if tokens_a & tokens_b:
        return True

    # Title-similarity is a fallback signal only, guarded against templated
    # titles that differ solely by a number (quarter, version, episode,
    # index) -- e.g. "Q2 2026 Earnings Call" vs "Q3 2026 Earnings Call" is
    # near-identical text but a materially different event. If both titles
    # contain digits and the digit sets disagree, title similarity alone
    # can never establish a match.
    digits_a, digits_b = _digit_sequences(a.get("title", "")), _digit_sequences(b.get("title", ""))
    if digits_a and digits_b and digits_a != digits_b:
        return False

    title_sim = trigram_jaccard(normalize_title(a.get("title", "")), normalize_title(b.get("title", "")))
    return title_sim >= TITLE_SIMILARITY_FLOOR


@dataclass
class MaterialUpdate:
    is_update: bool
    note: str | None = None


# 2026-08-14: amount comparison now uses materiality._parse_dollar_values
# (unit-aware numeric parsing) instead of a raw-string normalize/compare of
# material_facts.py's own `amount` field -- see detect_material_update.
# pipeline/material_facts.py's own diff_snapshots() still does a raw string
# compare there (a latent bug in that module, left unfixed since it is
# shared with the live alerts pipeline, which this task must not modify;
# disclosed in the checkpoint report instead).


# Status-change language beyond pipeline/alert_triggers.py's curated alert
# vocabulary (which only covers export-control/sanction-style breaking
# triggers) -- generic evidence that something concretely moved forward
# since the prior telling, as opposed to a recap/retrospective/explainer of
# the same already-reported facts. Matched with word boundaries so e.g.
# "designed" never matches "signed".
_STATUS_CHANGE_TERMS = frozenset(
    {
        "signed", "approved", "rejected", "postponed", "resigned", "completed",
        "finalized", "closed", "launched", "begins", "ends", "expands",
        "cancelled", "canceled", "confirmed", "denied", "sentenced", "convicted",
        "acquitted", "resolved", "ceasefire", "resumes", "delayed", "extended",
        "terminated", "withdrawn", "appealed", "overturned", "upheld",
    }
)


def _status_terms_in(text_lower: str) -> set[str]:
    return {t for t in _STATUS_CHANGE_TERMS if re.search(rf"\b{re.escape(t)}\b", text_lower)}


# 2026-08-14 defect: a competitor or background entity named in passing
# ("a key part of Elon Musk's bid to gain ground on rivals Anthropic PBC
# and OpenAI") was reported as a "newly named" TRANSACTION PARTY, when it
# is neither the buyer, the target, nor any other structural party to
# *this* event -- just context about the competitive landscape. Any
# known-vocabulary entity mentioned within a competitive/comparative clause
# is excluded from the "newly named" comparison (it may still legitimately
# be part of same_event_story's match itself, since that question -- "is
# this the same event" -- is different from "is this a new transaction
# party").
_COMPETITIVE_CONTEXT_RE = re.compile(
    r"\brivals?\b|\bcompetitors?\b|\bcompete[sd]?\s+with\b|\bcompared?\s+(?:to|with)\b|"
    r"\bversus\b|\bvs\.?\b|\bunlike\b",
    re.IGNORECASE,
)


def _competitive_context_entities(text: str, known_entities: set[str]) -> set[str]:
    """Known-vocabulary entities that appear only inside a competitive/
    comparative clause (the sentence-ish segment around a "rivals"/"vs."/
    "compared to" marker) -- see module docstring above."""
    found: set[str] = set()
    for match in _COMPETITIVE_CONTEXT_RE.finditer(text):
        start = max(0, match.start() - 60)
        end = min(len(text), match.end() + 60)
        window = text[start:end]
        for entity in known_entities:
            if re.search(rf"\b{re.escape(entity)}\b", window, re.IGNORECASE):
                found.add(entity)
    return found


def _snapshot_for(item: dict, *, known_entities: set[str], alert_triggers: AlertTriggersConfig) -> MaterialFactSnapshot:
    text = f"{item.get('title', '')} {item.get('excerpt', '')}"
    confirmation_state = ConfirmationState(item.get("confirmation_state") or ConfirmationState.UNVERIFIED.value)
    return build_snapshot(
        text, confirmation_state=confirmation_state, known_entities=known_entities, alert_triggers=alert_triggers
    )


def detect_material_update(
    candidate: dict, prior: dict, *, known_entities: set[str], alert_triggers: AlertTriggersConfig
) -> MaterialUpdate:
    """Compares a new candidate against a previously-DELIVERED story judged
    to be the same underlying event (same_event_story already returned
    True). Reuses pipeline/material_facts.py's snapshot extraction (known-
    vocabulary actor/target entities, amount, effective date, reversal
    detection -- already used for alert-repeat suppression) but with its own
    normalized comparison rather than diff_snapshots(), plus a generic
    status-change-language check. A material update requires a concrete,
    textually-grounded delta; a new URL, timestamp, headline, or purely
    restated recap is NOT by itself a material update -- never fabricates a
    delta that isn't in the text."""
    snap_new = _snapshot_for(candidate, known_entities=known_entities, alert_triggers=alert_triggers)
    snap_old = _snapshot_for(prior, known_entities=known_entities, alert_triggers=alert_triggers)

    cand_text = f"{candidate.get('title', '')} {candidate.get('excerpt', '')}"
    prior_text = f"{prior.get('title', '')} {prior.get('excerpt', '')}"
    cand_text_lower = cand_text.lower()
    prior_text_lower = prior_text.lower()

    reasons: list[str] = []
    # Compares actual parsed dollar VALUES (materiality._parse_dollar_values,
    # unit-aware: "$60B" and "$60 billion" both parse to 6e10), not
    # material_facts.py's own raw-string amount field -- that field's own
    # regex only recognizes spelled-out units, so an abbreviated "$60B"
    # loses its unit entirely and would otherwise be reported as a bare,
    # meaningless "new figure ($60)" even when $60B was already known from
    # the prior telling.
    new_dollar_values = set(_parse_dollar_values(cand_text)) - set(_parse_dollar_values(prior_text))
    if new_dollar_values:
        formatted = ", ".join(format_dollar_value(v) for v in sorted(new_dollar_values, reverse=True)[:3])
        reasons.append(f"new figures ({formatted})")
    competitive_context = _competitive_context_entities(cand_text, known_entities) | _competitive_context_entities(
        prior_text, known_entities
    )
    newly_named = (set(snap_new.target_entities) - set(snap_old.target_entities)) - competitive_context
    if newly_named:
        reasons.append(f"newly named ({', '.join(sorted(newly_named)[:3])})")
    if snap_new.effective_date and snap_new.effective_date != snap_old.effective_date:
        reasons.append(f"effective date changed to {snap_new.effective_date}")
    if snap_new.reversed and not snap_old.reversed:
        reasons.append("reversal/cancellation")
    if snap_new.action and snap_new.action != snap_old.action:
        reasons.append(f"new action ({snap_new.action})")
    new_status_terms = _status_terms_in(cand_text_lower) - _status_terms_in(prior_text_lower)
    if new_status_terms:
        reasons.append(f"status change ({', '.join(sorted(new_status_terms)[:3])})")

    if not reasons:
        return MaterialUpdate(is_update=False, note=None)
    return MaterialUpdate(is_update=True, note="Update since prior coverage: " + "; ".join(reasons) + ".")
