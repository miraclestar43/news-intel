"""Chinese-language event-type/consequence gates (2026-08-21 repair), the
zh_cn counterpart to pipeline/event_integrity.py.

Real production defect: jobs/daily.py's _has_material_consequence() calls
the shared, edition-agnostic event_integrity.is_low_consequence_event_type()
for every candidate regardless of edition -- but that function's phrase
lists (recognition/award language, tutorial/promotional language, vision-
statement language) are all ENGLISH words, so they never match Chinese
text at all. zh_cn candidates received zero event-type/consequence
filtering beyond classify_relevance's noise_exclusions and render-
readiness -- confirmed via a real live-collection replay where a
conference-appearance story ("章鱼动力亮相WRC 2026，携...探索具身智能未来
范式") and an industry-award-list story ("360入选中国AI50强") both reached
TOP/selection with nothing behind the headline but a conference booth
visit or an award-list inclusion.

Same floor as the English module: general event-type/consequence rules,
never a headline-specific exclusion."""

from __future__ import annotations

from news_intel.pipeline.zh_text import parse_zh_amounts

# 2026-08-21: conference/exhibition appearance language ("亮相"/"首次亮相"
# -- "made its debut at/appeared at [an event]", "探索...未来" -- "explores
# the future of...") is booth-visit/marketing language, not a news event on
# its own. Real reproduction: "章鱼动力亮相WRC 2026，携'脑-手-数据'技术体系
# 探索具身智能未来范式" (a robotics company appearing at the World Robot
# Conference, showcasing a vague "brain-hand-data" concept) reached TOP
# with no concrete launch/contract/funding/regulation/measurable result
# behind it at all.
_CONFERENCE_APPEARANCE_TERMS = frozenset(
    {"亮相", "首次亮相", "惊艳亮相", "闪耀亮相", "重磅亮相", "探索未来", "共探未来", "探索...未来范式"}
)

# 2026-08-21: award/ranking/list-inclusion language ("入选"/"荣获"/"获奖"/
# "位列"/"上榜"/"...强"/"...榜" -- "was selected into"/"won an award"/
# "ranked among") is corporate self-promotion, not news, unless it comes
# with an independently material consequence -- same reasoning as
# event_integrity.py's English is_unqualified_recognition_story. Real
# reproduction: "IDC发布2026中国AI50强：360以'智能体+安全'双轮驱动入选"
# (a research firm's annual "Top 50" list, with 360 highlighted as one
# inclusion) reached selection with no measurable business result at all.
_RECOGNITION_TERMS = frozenset(
    {"入选", "荣获", "获奖", "位列", "上榜", "摘得", "斩获", "蝉联", "评选", "50强", "百强", "十强", "排行榜"}
)

# 2026-08-21: the same Chinese event-type/consequence vocabulary
# zh_render_readiness._EVENT_TYPE_LABELS_ZH already curates (收购/并购/
# 财报/裁员/破产/制裁/关税/出口管制/诉讼/调查/反垄断/监管/停火/军事行动/
# 谈判/条约/选举/裁决/辞职/降息/加息/通胀/召回/黑客/泄露/上市/融资) --
# reused here rather than a second, divergent vocabulary -- as the
# Chinese-language proxy for materiality.HIGH_IMPACT_TERMS.
_CONSEQUENCE_TERMS_ZH = frozenset(
    {
        "收购", "并购", "财报", "营收", "裁员", "破产", "制裁", "关税", "出口管制", "实体清单", "诉讼", "起诉",
        "和解", "罚款", "调查", "反垄断", "监管", "停火", "停战", "军事行动", "空袭", "入侵", "谈判", "峰会",
        "大选", "选举", "裁决", "辞职", "降息", "降准", "加息", "通胀", "召回", "黑客", "泄露", "上市", "融资",
    }
)


def _has_material_consequence_signal_zh(text: str) -> bool:
    """Shared "is there an independently material consequence" check for
    Chinese text -- a real 万/亿-magnitude figure or a decision/consequence
    keyword, mirroring event_integrity.py's English equivalent."""
    if parse_zh_amounts(text):
        return True
    return any(term in text for term in _CONSEQUENCE_TERMS_ZH)


def is_conference_appearance_without_event_zh(title: str, excerpt: str | None) -> bool:
    """True if this is conference/exhibition-appearance or vague
    "exploring the future" language with no independently material
    consequence (a real launch, contract, funding round, regulation, or
    measurable deployment) attached."""
    text = f"{title or ''} {excerpt or ''}"
    if not any(term in text for term in _CONFERENCE_APPEARANCE_TERMS):
        return False
    return not _has_material_consequence_signal_zh(text)


def is_unqualified_recognition_story_zh(title: str, excerpt: str | None) -> bool:
    """True if this is award/ranking/list-inclusion language with no
    independently material consequence attached."""
    text = f"{title or ''} {excerpt or ''}"
    if not any(term in text for term in _RECOGNITION_TERMS):
        return False
    return not _has_material_consequence_signal_zh(text)


def is_low_consequence_event_type_zh(title: str, excerpt: str | None) -> bool:
    """Combined Chinese event-type/consequence gate: true if this
    candidate is a normally non-material event type -- conference/
    exhibition appearance, or award/ranking/list inclusion -- with no
    independently material consequence attached. General event-type logic
    only; never a title-specific exclusion."""
    return (
        is_conference_appearance_without_event_zh(title, excerpt)
        or is_unqualified_recognition_story_zh(title, excerpt)
    )
