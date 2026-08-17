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

from news_intel.pipeline.materiality import _parse_dollar_values, format_dollar_value
from news_intel.pipeline.summary_quality import best_available_summary

# HIGH_IMPACT_TERMS itself lives in materiality.py; the event-type labels
# below are this module's own curated, human-readable subset (see
# _EVENT_TYPE_LABELS_EN's docstring comment) -- not a re-export of the raw
# term set.

_CATEGORY_CONTEXT_EN = {
    "AI & Semiconductors": "AI/semiconductor infrastructure and competitiveness",
    "Markets & Economy": "corporate finances or the broader economy",
    "Politics": "domestic policy",
    "US / China / World": "US-China relations or global affairs",
    "Technology": "the technology industry",
}

_CATEGORY_CONTEXT_ZH = {
    "AI & Semiconductors": "AI/半导体产业格局",
    "Markets & Economy": "企业财务或宏观经济",
    "Politics": "国内政策",
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


def _principal_entity(entities: list[str] | None) -> str | None:
    return entities[0] if entities else None


def _event_type_label(lowered_text: str, labels: list[tuple[str, str]]) -> str | None:
    for term, label in labels:
        if term in lowered_text:
            return label
    return None


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
    values = _parse_dollar_values(text)
    amount_clause = f" worth {format_dollar_value(max(values))}" if values else ""
    entity = _principal_entity(entities)
    event_label = _event_type_label(lowered, _EVENT_TYPE_LABELS_EN)

    if entity and event_label:
        return f"Why it matters: {entity}'s {event_label}{amount_clause} is a material development in {context}."
    if entity:
        return (
            f"Why it matters: this development involving {entity}{amount_clause} "
            f"ranks among today's most material in {context}."
        )
    if event_label:
        return f"Why it matters: {event_label}{amount_clause} is a material development in {context}."
    if values:
        return f"Why it matters: a {format_dollar_value(max(values))} development in {context}."
    return f"Why it matters: ranks among today's most material developments in {context}."


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
    entity = _principal_entity(entities)

    event_label_zh = None
    amount = None
    if primary is not None:
        text = _combined_text(primary, secondary)
        lowered = text.lower()
        event_label_en = _event_type_label(lowered, _EVENT_TYPE_LABELS_EN)
        event_label_zh = _EVENT_TYPE_LABELS_ZH.get(event_label_en) if event_label_en else None
        values = _parse_dollar_values(text)
        if values:
            amount = format_dollar_value(max(values))

    amount_clause = f"（金额约{amount}）" if amount else ""
    if entity and event_label_zh:
        return f"看点：{entity}{event_label_zh}{amount_clause}，属{context}重要动态。"
    if entity:
        return f"看点：{entity}相关{context}重要动态{amount_clause}。"
    if event_label_zh:
        return f"看点：{context}领域{event_label_zh}{amount_clause}。"
    return f"看点：{context}的重要动态。"
