"""Event/source-integrity checks (2026-08-13 repair): a dashboard, "latest
numbers" landing page, continuously-updated table, topic page, calendar, or
feed index is not itself a news event. It may be used to discover or
verify a specific release, but selection requires a discrete, source-
supported change (a stated CPI/employment number, a Fed decision, a
company's reported results, an agency action) -- not a lumped list of many
different indicators with no single one distinguished as what happened.

2026-08-12 diagnosis: BLS's "latest numbers" source (bls_latest) always
produces one item titled "Major Economic Indicators Latest Numbers" whose
excerpt is a flat list of ~9 unrelated indicators (CPI, unemployment,
payrolls, ECI, productivity, import/export prices...) refreshed on each
poll -- not a single discrete release. It was selected as a TOP story, and
then falsely "corroborated" by an unrelated Asian-markets commentary
article that merely happened to mention one of the same generic indicator
names ("CPI").
"""

from __future__ import annotations

import re

from news_intel.pipeline.materiality import HIGH_IMPACT_TERMS, _count_hits, dollar_magnitude_bonus

_DASHBOARD_TITLE_PATTERNS = (
    "latest numbers",
    "at a glance",
    "key indicators",
    "economic indicators",
    "dashboard",
    "index page",
    "overview page",
    "summary page",
)

# A discrete release states ONE fact ("CPI rose 0.1% in July"); a dashboard
# lists MANY side by side ("CPI: +0.1% ... Unemployment: 4.1% ... Payroll:
# -23,000 ..."). Matches "Label: value" pairs like the ones bls_latest's
# excerpt actually contains.
_LABELED_METRIC_RE = re.compile(r"[A-Z][A-Za-z /\-()]{2,40}:\s*[+\-]?\$?\d")
_MIN_DISTINCT_METRICS_FOR_DASHBOARD = 3


def is_dashboard_like(title: str, excerpt: str | None) -> bool:
    """True if this looks like an aggregation/landing page rather than a
    discrete, single news event -- title matches a known dashboard-style
    phrase, OR the excerpt lists several distinct labeled metrics side by
    side with no one of them distinguished as the actual story."""
    lowered_title = (title or "").lower()
    if any(pattern in lowered_title for pattern in _DASHBOARD_TITLE_PATTERNS):
        return True
    metric_labels = _LABELED_METRIC_RE.findall(excerpt or "")
    return len(metric_labels) >= _MIN_DISTINCT_METRICS_FOR_DASHBOARD


# 2026-08-13 materiality gate: recognition/award/ranking/listicle language
# ("tops [a] list", "named to", "ranked No. 1", employee-approval-style
# "best CEO" pieces) is corporate self-promotion, not news, unless it comes
# with an independently material consequence. Reproduction: "NVIDIA CEO
# Tops Glassdoor's 2026 List of Best CEOs" (99% employee approval) scored
# competitively under the repaired materiality model purely on category
# base + a percent-magnitude hit from "99%" -- an approval rating, not a
# business result -- and, by outranking genuinely material candidates
# (Cerebras' earnings beat, Decart's acquisition talks) for the AI-category
# cap's two slots, crowded them out of the digest entirely.
_RECOGNITION_TERMS = frozenset(
    {
        "tops the list", "tops glassdoor", "best ceo", "best ceos", "list of the best",
        "named to the", "ranked no. 1", "ranked #1", "power list", "30 under 30",
        "best places to work", "most admired", "hall of fame", "best companies to work",
        "wins award", "receives award", "honored as", "recognized as",
    }
)


def _has_material_consequence_signal(text: str, lowered: str) -> bool:
    """Shared "is there an independently material consequence" check used
    by every event-type gate in this module: a real dollar-magnitude figure
    or a decision/consequence keyword (pipeline/materiality.HIGH_IMPACT_TERMS)
    -- deliberately NOT the percent-magnitude signal, which an approval-
    rating or satisfaction-score percentage (exactly the substance of a
    "best CEO" piece) would otherwise use to self-certify as material."""
    return dollar_magnitude_bonus(text) > 0 or _count_hits(lowered, HIGH_IMPACT_TERMS) > 0


def is_unqualified_recognition_story(title: str, excerpt: str | None) -> bool:
    """True if this is recognition/award/ranking language with no
    independently material consequence (leadership change, compensation/
    governance action, measurable business result, market reaction, or
    regulatory consequence) attached."""
    text = f"{title or ''} {excerpt or ''}"
    lowered = text.lower()
    if not any(term in lowered for term in _RECOGNITION_TERMS):
        return False
    return not _has_material_consequence_signal(text, lowered)


# 2026-08-14 event-type/consequence gate: a tutorial/builder-guide/how-to/
# customer-story is educational or promotional content about an ALREADY-
# KNOWN release, not a news event on its own -- reproduction: "The
# builder's guide to GPT-5.6" (a usage tutorial, not a new-model
# announcement) outranked a genuine, much more material semiconductor
# earnings beat (Applied Materials) for a digest slot.
_TUTORIAL_TERMS = frozenset(
    {
        "builder's guide", "builders guide", "guide to", "how to", "getting started",
        "customer story", "case study", "adoption guide", "tutorial", "walkthrough",
        "best practices for", "tips for",
    }
)

# An enforcement/regulatory action against one NAMED INDIVIDUAL (a former
# employee, a single officer) is not institutionally or systemically
# material unless it carries its own measurable consequence (a real dollar
# penalty, or decision/consequence language indicating broader reach).
# Reproduction: "Federal Reserve Board issues enforcement action with
# former employee of Regions Bank" -- an action against the *institution*
# itself, a large civil money penalty, or broader systemic language is
# deliberately NOT caught by this (the individual-subject terms below
# require a specifically individual, not institutional, subject).
_ENFORCEMENT_TRIGGER_TERMS = frozenset(
    {
        "enforcement action", "consent order", "cease and desist",
        "civil money penalty", "settles with regulators", "charged with",
    }
)
_INDIVIDUAL_ENFORCEMENT_SUBJECT_TERMS = frozenset(
    {
        "former employee", "a former officer", "an individual", "one employee",
        "single employee", "former officer of",
    }
)


def is_routine_tutorial_or_promotional_content(title: str, excerpt: str | None) -> bool:
    """True if this is instructional/promotional content about an
    already-known product, with no independently material new-capability
    or consequence signal attached."""
    text = f"{title or ''} {excerpt or ''}"
    lowered = text.lower()
    if not any(term in lowered for term in _TUTORIAL_TERMS):
        return False
    return not _has_material_consequence_signal(text, lowered)


def is_individual_enforcement_without_consequence(title: str, excerpt: str | None) -> bool:
    """True if this is an enforcement/regulatory action against a single
    named individual (not the institution itself) with no measurable
    consequence (dollar penalty, decision/consequence language) attached."""
    text = f"{title or ''} {excerpt or ''}"
    lowered = text.lower()
    is_enforcement = any(term in lowered for term in _ENFORCEMENT_TRIGGER_TERMS)
    is_individual_subject = any(term in lowered for term in _INDIVIDUAL_ENFORCEMENT_SUBJECT_TERMS)
    if not (is_enforcement and is_individual_subject):
        return False
    return not _has_material_consequence_signal(text, lowered)


# 2026-08-18 event-type/consequence gate: first-party corporate "why our
# category matters" vision/mission-statement prose -- industry-positioning
# essay language with no single dated, verifiable action -- is not itself a
# news event, unlike an actual announced deal, launch, or result.
# Reproduction: a corporate newsroom post ("AI factories are the defining
# infrastructure of the AI era ... powers every business, industry and
# country ... require a full stack of critical resources") was selected as
# a MORE item with nothing behind the headline but a slogan and a link --
# no transaction, figure, or dated action a reader could verify. General
# vision-language + absence-of-event-verb logic only, never a title- or
# publisher-specific exclusion.
_THOUGHT_LEADERSHIP_TERMS = frozenset(
    {
        "defining infrastructure", "the future of", "powers every business", "powers every industry",
        "full stack of", "reimagining", "redefining", "the new era of", "shaping the future",
        "at the heart of", "the backbone of", "unlocking the potential", "our vision for",
        "the next frontier", "powering the next",
    }
)
_CONCRETE_EVENT_VERB_TERMS = frozenset(
    {
        "announc", "launch", "unveil", "acqui", "merger", "merges", "invest", "raise", "raised",
        "signs", "signed", "agreement", "partnership with", "reported", "files", "filed",
        "opens", "opened", "completed", "wins", "won", "appoint", "resign", "layoff", "recall",
        "sues", "settlement", "ruling", "approve", "reject", "ban ", "sanction", "tariff",
    }
)


def is_corporate_vision_content_without_event(title: str, excerpt: str | None) -> bool:
    """True if this reads as first-party industry-positioning/mission prose
    with no independently material consequence and no concrete dated
    action verb attached -- see module-level rationale above."""
    text = f"{title or ''} {excerpt or ''}"
    lowered = text.lower()
    if not any(term in lowered for term in _THOUGHT_LEADERSHIP_TERMS):
        return False
    if _has_material_consequence_signal(text, lowered):
        return False
    return not any(term in lowered for term in _CONCRETE_EVENT_VERB_TERMS)


# 2026-09-07 production incident (Sep 5 en_global digest, DELIVERED): the
# TOP #2 slot was Insider Monkey's "Two Chip-Equipment Stocks Just Crushed
# Nvidia. Is the AI Rally Broadening?" -- speculative stock commentary
# posed as a question, with no transaction, filing, regulator, product or
# other concrete primary event behind it. Its "why it matters" was the
# tautology "could affect valuations", now separately banned.
#
# An interrogative headline is a strong, language-general signal that the
# piece is posing a question rather than reporting an answer. Gated only
# when NO concrete event verb and no material figure accompany it, so a
# genuine story that happens to end in a question ("Nvidia acquired X for
# $2B. What comes next?") still qualifies.
_SPECULATIVE_COMMENTARY_TERMS = frozenset(
    {
        "is the", "are the", "should you", "what to know", "what it means", "here's why",
        "heres why", "could this", "will this", "is it time", "what's next", "whats next",
        "outlook", "forecast", "prediction", "rally", "bull case", "bear case",
    }
)


def is_speculative_market_commentary(title: str, excerpt: str | None) -> bool:
    """True if this is question-framed market/stock commentary with no
    concrete primary event or material figure behind it."""
    title = title or ""
    text = f"{title} {excerpt or ''}"
    lowered = text.lower()
    if "?" not in title:
        return False
    if not any(term in lowered for term in _SPECULATIVE_COMMENTARY_TERMS):
        return False
    # _CONCRETE_EVENT_VERB_TERMS are deliberately STEMS ("acqui", "announc"),
    # so they must stay substring-matched. The one problem case is the hedge
    # "reportedly", which contains the stem "reported" while meaning the
    # opposite -- an unconfirmed claim, not a completed action. Neutralise
    # the hedge first rather than breaking stem matching for everything.
    deheged = lowered.replace("reportedly", " ")
    if any(term in deheged for term in _CONCRETE_EVENT_VERB_TERMS):
        return False
    # Deliberately NOT gated on _has_material_consequence_signal: that
    # measures MAGNITUDE, and stock commentary is saturated with market
    # vocabulary, so it always looks "material". What distinguishes
    # commentary from news is whether a concrete event happened at all.
    return True


def is_low_consequence_event_type(title: str, excerpt: str | None) -> bool:
    """Combined event-type/consequence gate (stage 2 of selection): true if
    this candidate is a normally non-material event type -- recognition/
    award/ranking, tutorial/promotional content, individual-employee
    enforcement, or corporate vision/mission-statement prose -- with no
    independently material consequence attached. General event-type logic
    only; never a title-specific exclusion."""
    return (
        is_unqualified_recognition_story(title, excerpt)
        or is_routine_tutorial_or_promotional_content(title, excerpt)
        or is_individual_enforcement_without_consequence(title, excerpt)
        or is_corporate_vision_content_without_event(title, excerpt)
        or is_speculative_market_commentary(title, excerpt)
    )
