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


def _normalize_amount(value: str | None) -> str:
    """$500 Billion / $500 billion / $ 500 billion must compare equal --
    pipeline/material_facts.py's own diff_snapshots() does a raw string
    compare and would treat those as different (a latent bug in that
    module, left unfixed here since it is shared with the live alerts
    pipeline, which this task must not modify; disclosed in the checkpoint
    report instead)."""
    if not value:
        return ""
    return re.sub(r"\s+", " ", value.replace("$", "").replace(",", "")).strip().lower()


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

    cand_text_lower = f"{candidate.get('title', '')} {candidate.get('excerpt', '')}".lower()
    prior_text_lower = f"{prior.get('title', '')} {prior.get('excerpt', '')}".lower()

    reasons: list[str] = []
    if snap_new.amount and _normalize_amount(snap_new.amount) != _normalize_amount(snap_old.amount):
        reasons.append(f"new figures ({snap_new.amount})")
    newly_named = set(snap_new.target_entities) - set(snap_old.target_entities)
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
