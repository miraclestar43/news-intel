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
    # 2026-08-23 repair: mirrors the zh_cn list's same-day addition
    # (Kakao's spin-off story had no matching event type at all in
    # either language's list) -- a general corporate-restructuring
    # category, checked before "earnings" so a spin-off story whose body
    # also mentions a revenue figure isn't mislabeled as an earnings report.
    ("spin off", "a corporate spin-off"),
    ("spin-off", "a corporate spin-off"),
    ("spins off", "a corporate spin-off"),
    ("splits into", "a corporate split"),
    ("split into", "a corporate split"),
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

# 2026-08-22: event-type labels that typically describe something that
# HAPPENS TO an entity (a struck location, a bombed target) rather than
# something an entity DOES -- see why_it_matters()'s entity+event_label
# branch above. General category, not tied to any specific label pair.
_VICTIM_PRONE_EVENT_LABELS = frozenset({
    "a strike", "a strike/labor action", "an attack", "a missile launch",
    "a drone strike", "shelling", "a bombardment",
})

# 2026-08-23 repair: chinese_takeaway() had the identical agent-ambiguity
# issue why_it_matters() was fixed for above -- "看点：Ukraine军事打击"
# (real production regression, Aug 22 en_global digest, not yet delivered)
# reads as Ukraine performing the strike, when Ukraine was struck. Unlike
# the English fix (a phrasing change alone was enough), the actor -- when
# the source text names one -- should be surfaced ("乌克兰遭俄军袭击"),
# not just described as "affected"; a demonym adjective ("Russian strike")
# immediately before the matched event-type term is a reliable signal of
# who acted, even though it is deliberately excluded from principal-entity
# selection (see _TITLE_LEAD_STOPWORDS above -- a modifier, not the
# subject). General mapping, not specific to this one story; only the
# demonyms most likely to appear in conflict/strike reporting are covered,
# consistent with this module's "floor, not exhaustive" design elsewhere.
_DEMONYM_ZH = {
    "russian": "俄", "ukrainian": "乌", "israeli": "以", "palestinian": "巴",
    "chinese": "中", "american": "美", "iranian": "伊朗", "north korean": "朝",
    "south korean": "韩", "japanese": "日", "indian": "印", "british": "英",
    "french": "法", "german": "德", "saudi": "沙特", "turkish": "土耳其",
    "syrian": "叙利亚", "lebanese": "黎巴嫩", "yemeni": "也门",
}
def _victim_prone_actor_zh(lowered_text: str) -> str | None:
    """Simplified to a whole-text search rather than a position-windowed
    one: victim-prone-label stories are short conflict/strike blurbs in
    practice, not long multi-topic articles, so the risk of picking up an
    unrelated demonym from elsewhere in the text is low -- consistent with
    this module's "floor, not exhaustive" design elsewhere."""
    for demonym, zh in _DEMONYM_ZH.items():
        if demonym in lowered_text:
            return zh
    return None

_EVENT_TYPE_LABELS_ZH: dict[str, str] = {
    "an acquisition": "收购交易",
    "a corporate spin-off": "公司分拆",
    "a corporate split": "公司分拆",
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
    "a strike": "军事打击",
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
    # 2026-08-22 repair: real production regression (Aug 22 en_global
    # replay, not yet delivered -- caught during the required verification
    # replay) -- "Rescuers dig through Ukraine mall wreckage..." (entities
    # list empty) rendered as "Rescuers's strike/labor action", since
    # "Rescuers" -- a generic role noun, capitalized only by sentence-
    # initial position, same category as "Officials" already denylisted
    # above -- won as the lead candidate. Skipping it lets the scan reach
    # "Ukraine" instead, the story's real subject.
    "Rescuers", "Survivors", "Witnesses", "Victims",
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

# 2026-08-22 repair: real production regression (Aug 21 en_global digest,
# already delivered) -- "The Shanghai Stock Exchange accepted ... YMTC's
# application ..." picked "Shanghai" as the principal entity, beating the
# real subject "YMTC" purely by appearing earlier in the text. "Shanghai"
# is not itself noise (unlike a demonym adjective or "-based" modifier) --
# it is the geographic qualifier of a MULTI-WORD INSTITUTION name ("Shanghai
# Stock Exchange"), and the single-capitalized-word scan has no way to see
# that without looking past the immediate word. General pattern: a small
# set of common institutional-name suffixes, not tied to Shanghai/YMTC
# specifically -- skips the ENTIRE institution phrase (not just the
# qualifying word) so scanning resumes after it, letting the real subject
# further in the title win instead.
_TITLE_LEAD_INSTITUTION_SUFFIXES = (
    " stock exchange", " stock market", " securities exchange", " commodities exchange",
)


# 2026-08-22 repair: real production regression (Aug 21 en_global digest,
# already delivered) -- "UK, Canada and Australia condemn Israel for
# refusing criminal probe into aid worker killings in Gaza" rendered as
# "Canada's regulatory probe is a material development in domestic
# policy" -- entities.py's extraction produced only a garbled fragment
# ("Gaza Seven World Central"), so _title_lead_entity's earliest-
# capitalized-word scan picked "Canada" (one of three co-actors doing the
# condemning) as if it were the story's subject, when the real subject is
# ISRAEL -- the entity being condemned, whose refusal is the actual
# consequence. General pattern, not specific to this headline: in an
# "[actor(s)] condemn/criticize/slam/rebuke/denounce [target] for ..."
# construction, the target (grammatical object of the verb) is the
# story's real subject, never one of the actor(s) performing the
# criticism -- tried ahead of the plain earliest-word heuristic below.
_CONDEMNATION_VERBS = (
    "condemn", "condemns", "condemned", "criticize", "criticizes", "criticized", "criticised",
    "slam", "slams", "slammed", "denounce", "denounces", "denounced",
    "rebuke", "rebukes", "rebuked", "blast", "blasts", "blasted",
)


def _condemnation_target_entity(text: str | None) -> str | None:
    if not text:
        return None
    lowered = text.lower()
    for verb in _CONDEMNATION_VERBS:
        idx = lowered.find(f" {verb} ")
        if idx == -1:
            continue
        tail = text[idx + len(verb) + 2 :].lstrip()
        match = _TITLE_LEAD_WORD_RE.match(tail)
        if not match:
            continue
        candidate = match.group(0)
        if candidate.upper() in _NON_ENTITY_ACRONYMS or candidate in _TITLE_LEAD_STOPWORDS:
            continue
        return candidate
    return None


# 2026-08-22 repair: real production regression (Aug 19 en_global digest,
# already delivered) -- "Nvidia has discussed an investment in Mercor as
# part of a round valuing the startup at $20B" rendered as "NVIDIA valued
# at $20 billion" -- NVIDIA is the grammatical subject (the one doing the
# discussing/investing), but Mercor is the startup being invested in AND
# the one whose valuation $20B actually describes ("valuing THE STARTUP
# at $20B" refers back to Mercor, not Nvidia). General pattern: "invest(s/
# ed/ing/ment) in X" marks X as the investment recipient -- the entity
# whose situation (valuation, funding round, business) the story is
# actually about, not the investor performing the action. Same shape of
# fix as _condemnation_target_entity above (actor vs. the party actually
# affected), tried with the same priority.
_INVESTMENT_TARGET_RE = re.compile(r"invest(?:s|ed|ing|ment)?\s+in\s+", re.IGNORECASE)


def _investment_target_entity(text: str | None) -> str | None:
    if not text:
        return None
    match = _INVESTMENT_TARGET_RE.search(text)
    if not match:
        return None
    tail = text[match.end() :].lstrip()
    word_match = _TITLE_LEAD_WORD_RE.match(tail)
    if not word_match:
        return None
    candidate = word_match.group(0)
    if candidate.upper() in _NON_ENTITY_ACRONYMS or candidate in _TITLE_LEAD_STOPWORDS:
        return None
    return candidate


def _title_lead_entity(title: str | None) -> str | None:
    stripped = strip_title_preamble(title or "").strip()
    pos = 0
    while True:
        match = _TITLE_LEAD_WORD_RE.search(stripped, pos)
        if not match:
            return None
        candidate = match.group(0)
        tail = stripped[match.end() : match.end() + 30]
        tail_lower = tail.lower()
        if any(tail_lower.startswith(suffix) for suffix in _TITLE_LEAD_MODIFIER_SUFFIXES):
            pos = match.end()
            continue  # an origin/ownership modifier, not the entity it describes
        institution_suffix = next(
            (suf for suf in _TITLE_LEAD_INSTITUTION_SUFFIXES if tail_lower.startswith(suf)), None
        )
        if institution_suffix is not None:
            pos = match.end() + len(institution_suffix)
            continue  # a geographic qualifier of an institution name, not the entity itself
        if candidate.upper() in _NON_ENTITY_ACRONYMS or candidate in _TITLE_LEAD_STOPWORDS:
            pos = match.end()
            continue
        return candidate


def _is_non_entity_candidate(candidate: str) -> bool:
    """True if `candidate` is itself a bare denylisted acronym/role OR a
    multi-word phrase that merely CONTAINS one as one of its words -- e.g.
    "Hong Kong IPO"/"Shanghai IPO" (a place name glued to an event-type
    word by entities.py's capitalized-sequence fallback, not a genuine
    entity). 2026-08-22 repair: real production regression (Aug 21
    en_global digest, already delivered) -- "Filings: The Shanghai Stock
    Exchange accepted ... YMTC's application ..." picked "Shanghai" (the
    grammatical subject of the sentence, via _title_lead_entity) over the
    known-vocabulary entity "YMTC" purely because "Shanghai" appears
    earlier in the text. The real fix is upstream of the position race:
    "Shanghai IPO" (a candidate already sitting in entities.py's output)
    should never have out-competed "YMTC" for a *different* reason than
    the Hong Kong IPO case below -- both are the same defect, an event-
    type word ("IPO") turning an incidental place mention into a noise
    candidate."""
    words = candidate.upper().split()
    return any(word in _NON_ENTITY_ACRONYMS for word in words)


def _principal_entity(entities: list[str] | None, text: str = "", title: str | None = None) -> str | None:
    # 2026-08-22 repair: real production regression (Aug 19 en_global
    # digest, already delivered) -- "Nvidia has discussed an investment in
    # Mercor as part of a round valuing the startup at $20B" rendered as
    # "NVIDIA valued at $20 billion", even though "Mercor" (the investment
    # recipient, whose valuation the figure actually describes) was simply
    # appended into the earliest-position race, where NVIDIA (the
    # grammatical subject, mentioned first) still won. These grammatical-
    # role patterns (investment target, condemnation target) are higher-
    # confidence, more specific signals than "which capitalized word/known
    # entity appears earliest" -- they must WIN outright, not just get a
    # seat in the position race a more prominently-placed actor would
    # still win.
    override = (
        _investment_target_entity(title) or _investment_target_entity(text)
        or _condemnation_target_entity(title) or _condemnation_target_entity(text)
    )
    if override and not _is_non_entity_candidate(override):
        return override

    candidates = [e for e in (entities or []) if not _is_non_entity_candidate(e)]
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


# 2026-08-22 repair: real production regression (Aug 21 en_global digest,
# already delivered) -- "UK, Canada and Australia condemn Israel for
# REFUSING criminal probe into aid worker killings in Gaza" matched the
# bare "probe" -> "a regulatory probe" label, producing "Israel's
# regulatory probe" -- the opposite of the actual event (Israel is
# refusing/blocking an investigation, not conducting one). General
# pattern: a refusal/rejection verb shortly before the matched event-type
# term flips its meaning to an obstruction of that action, never the
# action itself -- not specific to "probe"/investigation labels.
_EVENT_TYPE_REFUSAL_VERBS = ("refus", "reject", "declin", "block", "deny", "denies", "denied")
_EVENT_TYPE_REFUSAL_WINDOW_CHARS = 25


def _event_type_label(lowered_text: str, labels: list[tuple[str, str]]) -> str | None:
    """Ranks by EARLIEST OCCURRENCE IN THE TEXT, not the labels list's own
    fixed order. 2026-08-22 repair: real production regression (Logitech
    lawsuit, Aug 21 zh_cn digest -- the English list has the identical
    ordering defect, fixed here too) -- a story about a consumer LAWSUIT
    also had one background mention of an "earnings call" much later in
    the article; since "earnings" appears earlier in this priority list
    than "lawsuit"/"sues", the background mention won regardless of which
    term the article was actually ABOUT. A term appearing earlier in the
    text is the more prominent, more likely-to-be-the-actual-subject
    signal -- same principle already applied to entity selection."""
    best: tuple[int, str, str] | None = None
    for term, label in labels:
        idx = lowered_text.find(term)
        if idx == -1:
            continue
        if best is None or idx < best[0]:
            best = (idx, label, term)
    if best is None:
        return None
    idx, label, term = best
    window_start = max(0, idx - _EVENT_TYPE_REFUSAL_WINDOW_CHARS)
    preceding = lowered_text[window_start:idx]
    if any(verb in preceding for verb in _EVENT_TYPE_REFUSAL_VERBS):
        return f"a refusal to allow {label}"
    if term == "strike " and label == "a strike/labor action":
        label = _disambiguate_strike_label(lowered_text, idx)
    return label


# 2026-08-22 repair: real production regression (Aug 22 en_global replay,
# not yet delivered -- caught during the required verification replay) --
# "Rescuers dig through Ukraine mall wreckage ... killed 16 ... Russian
# strike" rendered as "a strike/labor action" for a MISSILE STRIKE, since
# bare "strike " is genuinely ambiguous between a labor strike and a
# military strike and the label list default assumed labor. General
# disambiguation, not specific to this one story: war-context words
# (war/military/missile/attack/killed/wreckage/troops/shelling) nearby
# indicate a military strike; labor-context words (union/workers/walkout/
# picket) indicate a labor action; with neither, the ambiguous default is
# kept unchanged (an honest floor, not a guess).
_STRIKE_WAR_CONTEXT_TERMS = (
    "war", "military", "missile", "attack", "killed", "wreckage", "troops", "shelling",
    "airstrike", "air strike", "bombard", "invasion", "combat",
)
_STRIKE_LABOR_CONTEXT_TERMS = ("union", "workers", "walkout", "picket", "labor", "labour")
_STRIKE_CONTEXT_WINDOW_CHARS = 80


def _disambiguate_strike_label(lowered_text: str, idx: int) -> str:
    window_start = max(0, idx - _STRIKE_CONTEXT_WINDOW_CHARS)
    window = lowered_text[window_start : idx + _STRIKE_CONTEXT_WINDOW_CHARS]
    if any(term in window for term in _STRIKE_WAR_CONTEXT_TERMS):
        return "a strike"
    if any(term in window for term in _STRIKE_LABOR_CONTEXT_TERMS):
        return "a strike/labor action"
    return "a strike/labor action"


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
_DOLLAR_ROLE_LOOKAHEAD_CHARS = 15
# 2026-08-21 repair: real production regression (Aug 20 en_global digest,
# already delivered) -- "Broadcom is in talks with a group of lenders to
# raise $60B+ in debt for an AI chip financing deal" rendered as "Broadcom
# an investment of $60 billion" instead of debt financing. The lookback-
# only window saw "raise" immediately before the figure and matched the
# invest/investment bucket, never getting a chance to see "in debt" and
# "financing", which both land immediately AFTER the figure in this (very
# common) "raise $X in debt/financing" phrasing. debt/financing is checked
# ahead of invest/investment, and both a lookback AND a lookahead window
# are searched, since "raise"/"raising"/"raised" alone is a generic verb
# that also describes a debt raise, not only an equity investment -- a
# nearby "debt"/"financing" mention is the more specific, decisive signal
# when both are present. Lookahead is intentionally much shorter than the
# lookback (15 vs 40 chars): title+excerpt are concatenated with a single
# space in _combined_text, so a wide lookahead on a figure near the end of
# the title bleeds into the excerpt's own, unrelated first sentence -- a
# real regression this caused: Xiaomi's $16.2B (revenue, correctly
# identified via the preceding "revenue down" text) was reclassified as
# "profit" because the excerpt's later, unrelated "posted profit that
# fell..." sentence fell inside a 40-char lookahead.
_DOLLAR_ROLE_PHRASES: list[tuple[tuple[str, ...], str]] = [
    (("loss", "losses", "deficit", "shortfall"), "posting losses of"),
    (("operating profit", "net income", "profit of"), "reporting profit of"),
    (("revenue", "sales"), "reporting revenue of"),
    # 2026-08-22: "which could value it at $2T"/"would value the company at"
    # -- pronoun/article-mediated inflections of "value...at" -- real
    # production regression (Anthropic IPO story, Aug 21 en_global digest,
    # already delivered) defaulted to the generic "worth" floor because
    # neither matched the original "valued at"/"valuing" phrasing.
    (("valuation", "valued at", "valuing", "value it at", "value them at", "value the"), "valued at"),
    (("debt", "financing"), "seeking financing of"),
    # 2026-08-23 repair: real production regression (Aug 22 en_global
    # digest, not yet delivered) -- "Nvidia plans to use its $6B deal with
    # Poolside to build..." rendered as "NVIDIA worth $6 billion" -- $6B is
    # the size of Nvidia's commercial deal WITH Poolside, not a valuation
    # of Nvidia itself. Checked before "invest" since "X's $N deal with Y"
    # is a common, more specific construction than the generic invest/
    # raise bucket.
    (("deal",), "striking a deal valued at"),
    (("invest", "investment", "funding", "raise", "raising", "raised"), "an investment of"),
]


# 2026-08-23 repair: real production regression (Aug 21 en_global digest,
# already delivered) -- "Anthropic's bankers said the company could raise
# $100B+ in its IPO, which could value it at $2T" rendered as "Anthropic
# valued at $2 trillion" -- stated as settled fact when the source is
# explicitly prospective (bankers' discussions with potential investors,
# not a completed raise or confirmed valuation). Only the "valued at" role
# currently has a modal-safe variant ("could be valued at"); this is
# deliberately narrow rather than a general modal-rewriting system for
# every role phrase, which would need per-phrase grammar this deployment's
# floor doesn't attempt.
_DOLLAR_UNCERTAINTY_MARKERS = (
    "could", "might", "may ", "discussed", "in talks", "considering", "exploring",
    "potential", "reportedly", "sources say", "sources said",
)
_DOLLAR_MODAL_VARIANTS = {"valued at": "could be valued at"}


def _dollar_role_phrase(text: str, position: int) -> str:
    window_start = max(0, position - _DOLLAR_ROLE_LOOKBACK_CHARS)
    preceding = text[window_start:position].lower()
    following = text[position : position + _DOLLAR_ROLE_LOOKAHEAD_CHARS].lower()
    nearby = preceding + " " + following
    for keywords, phrase in _DOLLAR_ROLE_PHRASES:
        if any(kw in nearby for kw in keywords):
            if phrase in _DOLLAR_MODAL_VARIANTS and any(m in nearby for m in _DOLLAR_UNCERTAINTY_MARKERS):
                return _DOLLAR_MODAL_VARIANTS[phrase]
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
    (("valuation", "valued at", "valuing", "value it at", "value them at", "value the"), "估值"),
    (("debt", "financing"), "债务融资"),
    (("deal",), "交易金额"),
    (("invest", "investment", "funding", "raise", "raising", "raised"), "融资"),
]


_ZH_DOLLAR_MODAL_VARIANTS = {"估值": "或将估值"}


def _dollar_role_phrase_zh(text: str, position: int) -> str:
    window_start = max(0, position - _DOLLAR_ROLE_LOOKBACK_CHARS)
    preceding = text[window_start:position].lower()
    following = text[position : position + _DOLLAR_ROLE_LOOKAHEAD_CHARS].lower()
    nearby = preceding + " " + following
    for keywords, phrase in _ZH_DOLLAR_ROLE_PHRASES:
        if any(kw in nearby for kw in keywords):
            if phrase in _ZH_DOLLAR_MODAL_VARIANTS and any(m in nearby for m in _DOLLAR_UNCERTAINTY_MARKERS):
                return _ZH_DOLLAR_MODAL_VARIANTS[phrase]
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
        # 2026-08-22 repair: real production regression (Aug 22 en_global
        # replay, not yet delivered -- caught during the required
        # verification replay) -- "Rescuers dig through Ukraine mall
        # wreckage ... Russian strike" (entities empty, no "Russia" noun
        # -- only the denylisted adjective "Russian" -- anywhere in the
        # text) fell back to "Ukraine" as entity, then rendered "Ukraine's
        # strike" -- backwards: Ukraine was struck, not the striker. A
        # possessive framing ("{entity}'s {label}") asserts entity
        # performed the action, which is only safe for label types an
        # entity typically DOES (acquisition/lawsuit/earnings report) --
        # never for conflict-event types that typically happen TO a
        # location/party (strike/attack/missile/bombardment/shelling),
        # where the extracted entity is at least as likely to be the
        # target as the actor. General category split, not specific to
        # Ukraine/Russia.
        if event_label in _VICTIM_PRONE_EVENT_LABELS:
            return (
                f"Why it matters: {event_label}{amount_clause} affecting {entity} "
                f"is a material development in {context}."
            )
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
        if event_label_en in _VICTIM_PRONE_EVENT_LABELS:
            actor_zh = _victim_prone_actor_zh(lowered) if primary is not None else None
            if actor_zh:
                return f"看点：{entity}遭{actor_zh}军袭击{amount_clause}，属{context}重要动态。"
            return f"看点：{entity}遭{event_label_zh}影响{amount_clause}，属{context}重要动态。"
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
