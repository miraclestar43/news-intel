"""Chinese-edition (zh_cn) render-readiness and summary composition
(2026-08-17), the native-Chinese counterpart to pipeline/render_readiness.py.

The zh_cn edition is NOT a translation of en_global (see config/topics_zh.yaml's
docstring): a zh_cn event is clustered and scored entirely from Chinese-
language sources, so its own title/excerpt are already Chinese -- this
module's job is to extract a genuine "what happened" sentence and compose a
story-specific "why it matters" line FROM that Chinese text, using
pipeline/zh_text.py's Chinese-aware sentence splitting/preamble stripping/
amount parsing instead of the English module's [.!?]/\\b/$ assumptions.

Same floor as render_readiness.py: deterministic, template-based synthesis
of already-verified structured facts (principal entity, detected event/
action type, parsed 万/亿-magnitude) -- never invents a fact not already in
the combined source text, and is not a claim of genuine editorial analysis
(no LLM configured in this deployment)."""

from __future__ import annotations

from news_intel.pipeline.materiality import format_dollar_value, parse_current_dollar_values
from news_intel.pipeline.render_readiness import _NON_ENTITY_ACRONYMS
from news_intel.pipeline.zh_text import (
    format_zh_amount,
    parse_zh_amounts,
    strip_zh_preamble,
    strip_zh_trailer,
    zh_sentences,
)

# 2026-08-18 repair: found via a real live-collection replay -- a qbitai
# RSS excerpt was just a one-line teaser with no terminal punctuation at
# all ("回头看，A社就这样完成了反超", 13 chars, no 。！？；), which cleared
# the original 10-char floor and was accepted as a genuine "what happened"
# summary despite reading as an incomplete, uninformative fragment (never
# actually stating what happened -- no entity, no amount, no action).
# Raised to a length more consistent with a genuinely complete Chinese
# clause (subject + verb + object) rather than a bare teaser hook.
_FALLBACK_MIN_CHARS = 30

_CATEGORY_CONTEXT_ZH = {
    "AI & Semiconductors": "AI/半导体产业格局",
    "Markets & Economy": "企业财务或宏观经济",
    "Politics": "国内政策",
    "US / China / World": "中美关系或全球局势",
    "Technology": "科技行业",
}

# Same curated event-type label list as render_readiness.py's
# _EVENT_TYPE_LABELS_EN, in Chinese, matched directly against Chinese text
# (no word-boundary regex needed -- these are all multi-character phrases,
# which cannot false-positive-match inside an unrelated word the way a
# single Latin acronym could).
_EVENT_TYPE_LABELS_ZH: list[tuple[str, str]] = [
    ("收购", "收购交易"),
    ("并购", "并购交易"),
    ("财报", "财报业绩"),
    ("营收", "营收业绩"),
    ("裁员", "裁员"),
    ("破产", "破产"),
    ("制裁", "制裁行动"),
    ("关税", "关税措施"),
    ("出口管制", "出口管制行动"),
    ("实体清单", "出口管制清单"),
    ("诉讼", "诉讼"),
    ("起诉", "诉讼"),
    ("和解", "法律和解"),
    ("罚款", "监管罚款"),
    ("调查", "监管调查"),
    ("反垄断", "反垄断行动"),
    ("监管", "监管行动"),
    ("停火", "停火进展"),
    ("停战", "停战"),
    ("军事行动", "军事行动"),
    ("空袭", "军事行动"),
    ("入侵", "军事冲突进展"),
    ("谈判", "谈判"),
    ("峰会", "外交峰会"),
    ("大选", "选举进展"),
    ("选举", "选举进展"),
    ("裁决", "法院裁决"),
    ("辞职", "领导层辞职"),
    ("降息", "货币政策行动"),
    ("降准", "货币政策行动"),
    ("加息", "货币政策行动"),
    ("通胀", "通胀数据"),
    ("召回", "产品召回"),
    ("黑客", "安全漏洞"),
    ("泄露", "安全漏洞"),
    ("上市", "上市/IPO"),
    ("融资", "融资"),
]


def what_happened_zh(primary: dict, secondary: dict | None = None) -> str | None:
    """The Chinese-aware equivalent of summary_quality.best_available_summary:
    tries the primary source's excerpt first (a real, punctuation-terminated
    Chinese sentence, or -- when none is punctuated -- the preamble-stripped
    remainder verbatim, same "genuine unaltered text, not a truncated
    fragment" floor as the English fallback clause), then the linked
    secondary source."""
    for source in (primary, secondary):
        if not source:
            continue
        excerpt = source.get("excerpt")
        if not excerpt:
            continue
        # 2026-08-21: strip aggregator trailer furniture (相关阅读/AI 解读)
        # before extraction -- it describes OTHER stories or comments
        # ABOUT this one, never additional facts of the story itself.
        excerpt = strip_zh_trailer(excerpt)
        sentences = zh_sentences(excerpt)
        if sentences:
            return "".join(sentences) if len(sentences) == 1 else " ".join(sentences)
        fallback = strip_zh_preamble(excerpt).strip()
        if len(fallback) >= _FALLBACK_MIN_CHARS:
            return fallback if fallback[-1] in "。！？；" else fallback + "。"
    return None


def is_render_ready_zh(primary: dict, secondary: dict | None = None) -> bool:
    return what_happened_zh(primary, secondary) is not None


def _combined_text(primary: dict, secondary: dict | None) -> str:
    parts = [f"{primary.get('title', '')} {strip_zh_trailer(primary.get('excerpt', '') or '')}"]
    if secondary:
        parts.append(f"{secondary.get('title', '')} {strip_zh_trailer(secondary.get('excerpt', '') or '')}")
    return " ".join(parts)


def _event_type_label_zh(text: str) -> str | None:
    for term, label in _EVENT_TYPE_LABELS_ZH:
        if term in text:
            return label
    return None


# 2026-08-21 repair: even after excluding the wrong-currency trailer figure
# (see zh_text.strip_zh_trailer), max() over every remaining amount in a
# real article can still land on the wrong number -- a TCL/广州华星 share-
# acquisition story's own text ALSO mentions the acquired production
# line's unrelated total-investment figure ("t9 产线总投资350亿元") later,
# for background context, which is larger than the actual 93.25亿元 deal
# value and so won under plain max(). Standard Chinese M&A/deal reporting
# states its own headline figure using one of these conventional labels
# ("交易总对价约93.25亿元") -- preferring a labeled figure over an
# unlabeled max() is a general convention-following rule, not specific to
# this one story.
_ZH_DEAL_VALUE_LABELS = (
    "交易总对价约", "总对价约", "对价约", "交易金额约", "收购价约", "收购金额约", "融资金额约",
    "交易总对价", "总对价", "交易金额", "收购价", "收购金额", "融资金额", "融资规模约", "融资规模",
)


def _labeled_amount_zh(text: str) -> float | None:
    """The first amount immediately following a conventional Chinese deal-
    value label, or None if no such label is present -- callers fall back
    to an unlabeled max() over the whole text in that case."""
    for label in _ZH_DEAL_VALUE_LABELS:
        idx = text.find(label)
        if idx == -1:
            continue
        window = text[idx : idx + len(label) + 20]
        values = parse_zh_amounts(window)
        if values:
            return values[0]
    return None


# 2026-08-18 repair: found via a real live-collection replay -- entities[0]
# picked "ASP" (a generic finance acronym, alphabetically first) over the
# story's genuine subject ("NAND"/"Micron") for a NAND-market report whose
# excerpt embedded English chart captions ("Top Five NAND Flash Brands
# Rises", "Says TrendForce"). pipeline/entities.py's capitalized-sequence
# fallback is far noisier against Chinese text (which routinely embeds
# English chart labels/captions) than against English prose -- filters out
# the same acronym denylist render_readiness.py's English path already
# uses, plus multi-word (3+) candidates, which are reliably garbled
# caption fragments rather than genuine multi-word entity names.
def _principal_entity_zh(entities: list[str] | None) -> str | None:
    if not entities:
        return None
    # Single-word candidates only: real multi-word entity names DO exist
    # ("SK Hynix"), but pipeline/entities.py's capitalized-sequence
    # fallback against Chinese text (which routinely embeds English chart
    # captions/labels) produces far more 2-4-word GARBLED fragments
    # ("Brands Rises", "Says TrendForce", "Top Five NAND Flash") than
    # genuine multi-word names -- verified via a real live-collection
    # replay where a 2-word fragment ("Brands Rises") still won after an
    # earlier <=2-word filter. Single-word-only is a stricter but safer
    # bar for THIS fallback-heavy path than render_readiness.py's English
    # equivalent needs, since English text doesn't have this embedded-
    # caption noise problem.
    candidates = [
        e for e in entities
        if e.upper() not in _NON_ENTITY_ACRONYMS and " " not in e
    ]
    return candidates[0] if candidates else entities[0]


def why_it_matters_zh(
    primary: dict, secondary: dict | None, category: str, entities: list[str] | None = None
) -> str:
    """Story-specific Chinese rationale -- principal entity + detected
    event/action type + parsed 万/亿 magnitude, composed directly in
    Chinese (not a translation of the English why_it_matters). Never a
    bare category-only template when an entity or event type was
    extractable."""
    text = _combined_text(primary, secondary)
    context = _CATEGORY_CONTEXT_ZH.get(category, "本次报道领域")
    # Chinese-unit amounts (万/亿/万亿) plus role-aware $ figures -- zh_cn
    # sources routinely mix Chinese and English/dollar amounts in the same
    # story (e.g. a Chinese article quoting a USD deal size verbatim); zh
    # amounts take priority (native to the story) when both are present.
    labeled_amount = _labeled_amount_zh(text)
    if labeled_amount is not None:
        amount_clause = f"，金额约{format_zh_amount(labeled_amount)}"
    else:
        zh_values = parse_zh_amounts(text)
        amount_clause = f"，金额约{format_zh_amount(max(zh_values))}" if zh_values else ""
    if not amount_clause:
        dollar_values = parse_current_dollar_values(text)
        if dollar_values:
            amount_clause = f"，金额约{format_dollar_value(max(dollar_values))}"
    entity = _principal_entity_zh(entities)
    event_label = _event_type_label_zh(text)

    if entity and event_label:
        return f"看点：{entity}{event_label}{amount_clause}，属{context}重要动态。"
    if entity:
        return f"看点：{entity}相关动态{amount_clause}，属{context}重要发展。"
    if event_label:
        return f"看点：{context}领域{event_label}{amount_clause}。"
    if amount_clause:
        # 2026-08-18 repair: an amount-only signal (no entity/event-type
        # match) was previously silently discarded -- mirrors
        # render_readiness.py's English "if values:" amount-only tier so a
        # real extracted structured fact is never dropped from the floor.
        return f"看点：{context}领域一项重要动态{amount_clause}。"
    return f"看点：{context}的重要动态。"
