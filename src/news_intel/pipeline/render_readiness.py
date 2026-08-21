"""TOP-story render-readiness gate (2026-08-15 content-quality repair) and
story-specific why-it-matters/Chinese-takeaway composition (2026-08-17
content-quality repair).

2026-08-14 defect: "(No complete source-supported summary available -- see
original link.)" -- an internal diagnostic string, not reader-facing
content -- was rendered directly into a delivered TOP story. Round C
(2026-08-13) had deliberately made summary availability NOT a TOP-
*eligibility* gate, so a genuinely material story wouldn't be demoted
below a less material one just because its excerpt was poorly formatted;
that decision stands (decision-value ordering is computed and stays fixed
before enrichment is attempted -- see pipeline/daily_selection.py). What
was missing is a check *after* ordering, before rendering: if, having tried
every available source, no complete account can be produced at all, that
story must not occupy the slot with a placeholder -- it fails render-
readiness and the next qualified, render-ready story takes the slot
instead.

2026-08-17 defect: why_it_matters()/chinese_takeaway() previously took only
`category` and produced a PURE per-category template regardless of the
story's own content -- e.g. every AI & Semiconductors story got the exact
same "看点：AI/半导体产业格局的重要动态。" Chinese line, and the same
"ranks among today's most material developments in ..." English line. That
is exactly the generic, non-story-specific content this repair targets.
Both functions now also take `entities` (event_repo.event_as_digest_dict's
own `entities` field -- already-verified structured facts, matched
known-vocabulary or a conservative proper-noun fallback, see
pipeline/entities.py) and compose from: principal entity, detected event/
action type (materiality.HIGH_IMPACT_TERMS, mapped to a human-readable
label), and parsed dollar magnitude -- never inventing a fact not already
present in the combined source text. This remains a floor, not genuine
editorial analysis: with no LLM and no full-article fetch, this deployment
cannot produce analysis beyond synthesizing already-extracted structured
fields -- but it must always name the actual entity/transaction/action
when one was extracted, never fall back to a bare category label alone."""

from __future__ import annotations

import re

from news_intel.pipeline.citation import strip_title_preamble
from news_intel.pipeline.materiality import (
    dollar_values_with_position,
    format_dollar_value,
)
from news_intel.pipeline.summary_quality import best_available_summary

# HIGH_IMPACT_TERMS itself lives in materiality.py; the event-type labels
# below are this module's own curated, human-readable subset (see
# _EVENT_TYPE_LABELS_EN's docstring comment) -- not a re-export of the raw
# term set.

_CATEGORY_CONTEXT_EN = {
    "AI & Semiconductors": "AI/semiconductor infrastructure and competitiveness",
    "Markets & Economy": "corporate finances or the broader economy",
    "Politics": "domestic policy",
    "World": "international conflict, diplomacy, or global security",
    "US / China / World": "US-China relations or global affairs",
    "Technology": "the technology industry",
}

_CATEGORY_CONTEXT_ZH = {
    "AI & Semiconductors": "AI/半导体产业格局",
    "Markets & Economy": "企业财务或宏观经济",
    "Politics": "国内政策",
    "World": "国际冲突、外交或全球安全",
    "US / China / World": "中美关系或全球局势",
    "Technology": "科技行业",
}

# A representative human-readable label for the most common
# materiality.HIGH_IMPACT_TERMS hits -- checked in this fixed, deliberately
# ordered list (not the frozenset's arbitrary iteration order) so the
# result is deterministic across runs. Not exhaustive: an unmatched but
# still HIGH_IMPACT_TERMS-flagged story simply omits the event-type clause
# rather than guessing.
_EVENT_TYPE_LABELS_EN: list[tuple[str, str]] = [
    ("acquisition", "an acquisition"),
    ("merger", "a merger"),
    ("earnings", "an earnings report"),
    ("layoff", "layoffs"),
    ("bankrupt", "a bankruptcy"),
    ("bailout", "a bailout"),
    ("sanction", "a sanctions action"),
    ("tariff", "a tariff action"),
    ("export control", "an export-control action"),
    ("entity list", "an export-control listing"),
    ("lawsuit", "a lawsuit"),
    ("sues", "a lawsuit"),
    ("settlement", "a legal settlement"),
    ("fine ", "a regulatory fine"),
    ("penalt", "a regulatory penalty"),
    ("investigat", "a regulatory investigation"),
    ("probe", "a regulatory probe"),
    ("antitrust", "an antitrust action"),
    ("regulat", "a regulatory action"),
    ("ceasefire", "a ceasefire development"),
    ("truce", "a truce"),
    ("invas", "a military conflict development"),
    ("military operation", "a military operation"),
    ("military action", "a military action"),
    ("negotiat", "a negotiation"),
    ("peace talks", "peace talks"),
    ("treaty", "a treaty"),
    ("summit", "a diplomatic summit"),
    ("election", "an election development"),
    ("court rul", "a court ruling"),
    ("leadership change", "a leadership change"),
    ("resign", "a leadership resignation"),
    ("strike ", "a strike/labor action"),
    ("rate hike", "a Federal Reserve rate move"),
    ("rate cut", "a Federal Reserve rate move"),
    ("inflation", "an inflation report"),
    ("recession", "a recession signal"),
    ("recall", "a product recall"),
    ("hack", "a security breach"),
    ("breach", "a security breach"),
    ("missile", "a missile launch"),
    ("ballistic", "a missile launch"),
    ("drone strike", "a drone strike"),
    ("weapons test", "a weapons test"),
    ("nuclear test", "a nuclear test"),
    ("attack", "an attack"),
    ("shelling", "shelling"),
    ("bombard", "a bombardment"),
]

_EVENT_TYPE_LABELS_ZH: dict[str, str] = {
    "an acquisition": "收购交易",
    "a merger": "合并交易",
    "an earnings report": "财报结果",
    "layoffs": "裁员",
    "a bankruptcy": "破产",
    "a bailout": "救助计划",
    "a sanctions action": "制裁行动",
    "a tariff action": "关税措施",
    "an export-control action": "出口管制行动",
    "an export-control listing": "出口管制清单",
    "a lawsuit": "诉讼",
    "a legal settlement": "法律和解",
    "a regulatory fine": "监管罚款",
    "a regulatory penalty": "监管处罚",
    "a regulatory investigation": "监管调查",
    "a regulatory probe": "监管调查",
    "an antitrust action": "反垄断行动",
    "a regulatory action": "监管行动",
    "a ceasefire development": "停火进展",
    "a truce": "停战",
    "a military conflict development": "军事冲突进展",
    "a military operation": "军事行动",
    "a military action": "军事行动",
    "a negotiation": "谈判",
    "peace talks": "和谈",
    "a treaty": "条约",
    "a diplomatic summit": "外交峰会",
    "an election development": "选举进展",
    "a court ruling": "法院裁决",
    "a leadership change": "领导层变动",
    "a leadership resignation": "领导层辞职",
    "a strike/labor action": "罢工/劳工行动",
    "a Federal Reserve rate move": "美联储利率行动",
    "an inflation report": "通胀数据",
    "a recession signal": "经济衰退信号",
    "a product recall": "产品召回",
    "a security breach": "安全漏洞",
    "a missile launch": "导弹发射",
    "a drone strike": "无人机袭击",
    "a weapons test": "武器试验",
    "a nuclear test": "核试验",
    "an attack": "袭击事件",
    "shelling": "炮击",
    "a bombardment": "轰炸",
}


def _combined_text(primary: dict, secondary: dict | None) -> str:
    parts = [f"{primary.get('title', '')} {primary.get('excerpt', '') or ''}"]
    if secondary:
        parts.append(f"{secondary.get('title', '')} {secondary.get('excerpt', '') or ''}")
    return " ".join(parts)


def is_render_ready(primary: dict, secondary: dict | None) -> bool:
    """True once enrichment (primary source, then linked secondary source
    -- pipeline/summary_quality.best_available_summary, decision-value
    order) has been attempted and produced a complete, source-supported
    "what happened" sentence. False means every available source was tried
    and none yielded one -- the render-readiness gate then fails this
    story rather than rendering a placeholder."""
    return best_available_summary(primary, secondary) is not None


# pipeline/entities.py's extract_entities() returns entities SORTED
# ALPHABETICALLY (fine for its own clustering/dedup use, where only set
# membership matters) -- picking entities[0] directly would pick whichever
# match is alphabetically first, not the most prominent one. Real
# reproduction: "Nvidia in talks to invest up to $3B in ... that aims to
# IPO soon" picked "IPO" over "NVIDIA" this way. _principal_entity instead
# picks whichever candidate appears EARLIEST in the given text (title
# preferred -- the most prominent position), skipping a small denylist of
# generic acronyms/roles that the capitalized-sequence fallback in
# entities.py sometimes catches but that are never themselves the subject
# of a story (an event-type word, not a company/person/country name).
_NON_ENTITY_ACRONYMS = frozenset({
    "IPO", "CEO", "CFO", "CTO", "COO", "AI", "US", "UK", "EU", "UN", "GDP", "CPI",
    # 2026-08-18: "ASP" (Average Selling Price) added after a real zh_cn
    # replay picked it as the principal entity for a NAND-market report --
    # a generic finance acronym, never itself a story's subject.
    "ASP", "YOY", "QOQ", "ROI",
})

# 2026-08-18 defect: pipeline/entities.py's shared extraction (tuned for
# trend-cluster anchor precision, see its own module docstring) rejects a
# single Title-Case word as a candidate entity unless it's ALL-CAPS or
# contains a digit/hyphen -- correct for avoiding sentence-starter false
# positives ("Fine", "Building") in clustering, but it also silently drops
# genuine single-word company names ("Crusoe", "Shein") that aren't in the
# known-vocabulary list, letting an incidental multi-word capitalized
# phrase (a journalist byline, "Wall Street", "Hong Kong IPO") win the
# earliest-position tiebreak instead of the story's actual subject.
# Reproduction: the Aug 17 Crusoe/Shein IPO stories named "Alan Neuhauser"/
# "Hong Kong IPO" as the principal entity. Composition only needs recall
# here, never clustering precision, so this supplements (never replaces)
# the caller-supplied entities list with the FIRST capitalized word of the
# (aggregator-preamble-stripped) title -- English headlines are
# overwhelmingly subject-first ("Crusoe is in IPO talks...", "Shein is
# aiming to hit..."), a general pattern, not a per-story exception.
_TITLE_LEAD_STOPWORDS = frozenset({
    "The", "A", "An", "New", "Report", "Reports", "Reportedly", "Officials", "Company",
    "Companies", "Exclusive", "Breaking", "Analysis", "Update", "According",
    # Nationality/demonym adjectives: grammatically modifiers ("Israeli
    # airstrikes hit...", "Chinese regulators fine...") that are never
    # themselves the subject entity being modified -- excluding them is a
    # general linguistic category, not a per-story carve-out. Caught by a
    # Lebanon/Israel regression test where "Israeli" (position 0) beat the
    # real supplied entity "Lebanon" purely on earliest-position ordering.
    "Israeli", "Chinese", "American", "Russian", "British", "French", "German",
    "Iranian", "Korean", "Japanese", "Indian", "European", "Ukrainian", "Palestinian",
    "Saudi", "Turkish", "Mexican", "Canadian", "Australian", "Brazilian", "Egyptian",
    "Syrian", "Lebanese", "Pakistani", "Indonesian", "Vietnamese", "Taiwanese",
})
_TITLE_LEAD_WORD_RE = re.compile(r"[A-Z][a-zA-Z0-9&.]*")
# A "<Place>-based"/"-backed"/"-owned" prefix is the same grammatical
# pattern as a demonym adjective ("Singapore-based Shein", "Beijing-backed
# fund", "US-owned subsidiary") -- an origin/ownership modifier, never the
# entity itself. General hyphenated-modifier suffix list, not tied to any
# specific place name.
_TITLE_LEAD_MODIFIER_SUFFIXES = ("-based", "-owned", "-backed", "-led", "-founded", "-listed", "-headquartered")


def _title_lead_entity(title: str | None) -> str | None:
    stripped = strip_title_preamble(title or "").strip()
    for match in _TITLE_LEAD_WORD_RE.finditer(stripped):
        candidate = match.group(0)
        tail = stripped[match.end() : match.end() + 20].lower()
        if any(tail.startswith(suffix) for suffix in _TITLE_LEAD_MODIFIER_SUFFIXES):
            continue  # an origin/ownership modifier, not the entity it describes
        if candidate.upper() in _NON_ENTITY_ACRONYMS or candidate in _TITLE_LEAD_STOPWORDS:
            continue
        return candidate
    return None


def _principal_entity(entities: list[str] | None, text: str = "", title: str | None = None) -> str | None:
    candidates = [e for e in (entities or []) if e.upper() not in _NON_ENTITY_ACRONYMS]
    lead = _title_lead_entity(title)
    if lead and lead not in candidates:
        candidates.append(lead)
    if not candidates:
        candidates = list(entities) if entities else ([lead] if lead else [])
    if not candidates:
        return None
    if not text:
        return candidates[0]

    lowered_text = text.lower()

    def _position(entity: str) -> int:
        idx = lowered_text.find(entity.lower())
        return idx if idx != -1 else len(text)

    return min(candidates, key=_position)


def _event_type_label(lowered_text: str, labels: list[tuple[str, str]]) -> str | None:
    for term, label in labels:
        if term in lowered_text:
            return label
    return None


# 2026-08-21 repair: two real production defects (Aug 20 en_global
# digest, already delivered) -- "this development involving OpenAI worth
# $12.3 billion" for a story where $12.3B is OpenAI's WIDENED LOSS, and
# "worth $16.2 billion" for a story where $16.2B is Xiaomi's QUARTERLY
# REVENUE. The fixed "worth {amount}" phrasing implies a deal size or
# valuation regardless of what the figure actually measures. General,
# keyword-proximity role detection (not specific to OpenAI/Xiaomi/any
# other name) over the ~40 characters immediately preceding the winning
# figure -- when no role keyword is nearby, "worth" remains the
# documented floor, not a guess.
_DOLLAR_ROLE_LOOKBACK_CHARS = 40
_DOLLAR_ROLE_PHRASES: list[tuple[tuple[str, ...], str]] = [
    (("loss", "losses", "deficit", "shortfall"), "posting losses of"),
    (("operating profit", "net income", "profit of"), "reporting profit of"),
    (("revenue", "sales"), "reporting revenue of"),
    (("valuation", "valued at", "valuing"), "valued at"),
    (("invest", "investment", "funding", "raise", "raising", "raised"), "an investment of"),
    (("debt", "financing"), "seeking financing of"),
]


def _dollar_role_phrase(text: str, position: int) -> str:
    window_start = max(0, position - _DOLLAR_ROLE_LOOKBACK_CHARS)
    preceding = text[window_start:position].lower()
    for keywords, phrase in _DOLLAR_ROLE_PHRASES:
        if any(kw in preceding for kw in keywords):
            return phrase
    return "worth"


def _primary_dollar_clause(text: str) -> str:
    """The role-aware " {phrase} {amount}" clause for the winning
    (current, non-baseline) dollar figure, or "" if none was found."""
    matches = dollar_values_with_position(text)
    if not matches:
        return ""
    value, position = max(matches, key=lambda vp: vp[0])
    phrase = _dollar_role_phrase(text, position)
    return f" {phrase} {format_dollar_value(value)}"


# 2026-08-21 repair: chinese_takeaway() used to pick its amount via a bare
# max(parse_current_dollar_values(text)) -- the same "largest number in the
# article" anti-pattern banned elsewhere -- and could show a DIFFERENT
# figure than the English why_it_matters() line for the same story (each
# computed independently). Mirrors _dollar_role_phrase's keyword-proximity
# detection (translated) and reuses the SAME winning (value, position) as
# _primary_dollar_clause so one story's EN and ZH takeaway lines can never
# disagree on which number is "the" figure.
_ZH_DOLLAR_ROLE_PHRASES: list[tuple[tuple[str, ...], str]] = [
    (("loss", "losses", "deficit", "shortfall"), "净亏损"),
    (("operating profit", "net income", "profit of"), "净利润"),
    (("revenue", "sales"), "营收"),
    (("valuation", "valued at", "valuing"), "估值"),
    (("invest", "investment", "funding", "raise", "raising", "raised"), "融资"),
    (("debt", "financing"), "债务融资"),
]


def _dollar_role_phrase_zh(text: str, position: int) -> str:
    window_start = max(0, position - _DOLLAR_ROLE_LOOKBACK_CHARS)
    preceding = text[window_start:position].lower()
    for keywords, phrase in _ZH_DOLLAR_ROLE_PHRASES:
        if any(kw in preceding for kw in keywords):
            return phrase
    return "涉及金额"


def _primary_dollar_role_and_value(text: str) -> tuple[str, float] | None:
    matches = dollar_values_with_position(text)
    if not matches:
        return None
    value, position = max(matches, key=lambda vp: vp[0])
    return _dollar_role_phrase_zh(text, position), value


def why_it_matters(
    primary: dict, secondary: dict | None, category: str, entities: list[str] | None = None
) -> str:
    """A deterministic, template-based rationale composed from
    already-verified structured facts -- principal entity (the event's own
    already-extracted entities, see pipeline/entities.py), detected event/
    action type (materiality.HIGH_IMPACT_TERMS), and largest parsed dollar
    figure -- never invents a fact not already present in the combined
    source text. This is a floor, not genuine editorial analysis: with no
    LLM and no full-article fetch, a distinct rationale beyond synthesizing
    these already-extracted fields is not achievable here -- but it must
    always name the specific entity/action when one was extracted, never
    fall back to a bare category label alone (`entities=None`/empty is the
    only case that still degrades to the category-only floor)."""
    text = _combined_text(primary, secondary)
    lowered = text.lower()
    context = _CATEGORY_CONTEXT_EN.get(category, "this coverage area")
    amount_clause = _primary_dollar_clause(text)
    entity = _principal_entity(entities, text, title=primary.get("title"))
    event_label = _event_type_label(lowered, _EVENT_TYPE_LABELS_EN)

    if entity and event_label:
        # Drop the leading article ("an acquisition" -> "acquisition") after
        # a possessive -- "Entity's an acquisition" is not grammatical.
        possessive_label = event_label.removeprefix("a ").removeprefix("an ")
        return (
            f"Why it matters: {entity}'s {possessive_label}{amount_clause} "
            f"is a material development in {context}."
        )
    # 2026-08-21 repair: this branch (entity extracted, no event-type label
    # matched -- the common case for the OpenAI/Xiaomi/Mercor regressions)
    # and the final catch-all below used to read "this development
    # involving X ... ranks among today's most material in {context}" --
    # exactly the generic entity/category/amount template the user
    # explicitly named for removal from every production/fallback path.
    # Restructured to still lead with the specific entity and (now
    # numeric-role-aware, see _primary_dollar_clause) amount_clause without
    # the banned boilerplate phrasing.
    if entity:
        return f"Why it matters: {entity}{amount_clause}, a material development in {context}."
    if event_label:
        return f"Why it matters: {event_label}{amount_clause} is a material development in {context}."
    if amount_clause:
        return f"Why it matters: a development{amount_clause} in {context}."
    return f"Why it matters: a material development in {context}."


def chinese_takeaway(category: str, entities: list[str] | None = None, primary: dict | None = None,
                      secondary: dict | None = None) -> str:
    """Story-specific Chinese-language takeaway naming the actual entity/
    action/amount when extractable, not merely the event's category. Does
    not claim to be a literal translation (no LLM in this deployment) but
    must communicate the specific story -- see module docstring. Falls back
    to the category-only floor only when no entity or event-type/amount
    signal was extractable at all (e.g. `entities`/`primary` not supplied,
    or a story with genuinely no matched vocabulary)."""
    context = _CATEGORY_CONTEXT_ZH.get(category, "本次报道领域")
    entity_text = _combined_text(primary, secondary) if primary is not None else ""
    title = primary.get("title") if primary is not None else None
    entity = _principal_entity(entities, entity_text, title=title)

    event_label_zh = None
    amount_role_zh = None
    amount = None
    if primary is not None:
        text = entity_text
        lowered = text.lower()
        event_label_en = _event_type_label(lowered, _EVENT_TYPE_LABELS_EN)
        event_label_zh = _EVENT_TYPE_LABELS_ZH.get(event_label_en) if event_label_en else None
        role_and_value = _primary_dollar_role_and_value(text)
        if role_and_value:
            amount_role_zh, amount = role_and_value

    amount_clause = f"（{amount_role_zh}约{format_dollar_value(amount)}）" if amount else ""
    if entity and event_label_zh:
        return f"看点：{entity}{event_label_zh}{amount_clause}，属{context}重要动态。"
    # 2026-08-21 repair: this branch used to read "{entity}相关{context}
    # 重要动态" -- the entity/category-only template the user explicitly
    # named (matches the "属AI/半导体产业格局重要动态" regression case).
    # Now leads with entity + role-aware amount instead of a bare category
    # restatement.
    if entity:
        return f"看点：{entity}{amount_clause}，属{context}重要动态。"
    if event_label_zh:
        return f"看点：{context}领域{event_label_zh}{amount_clause}。"
    return f"看点：{context}的重要动态。"
