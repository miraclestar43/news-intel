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


# 2026-08-29 production incident (Aug 28 zh_cn digest, DELIVERED): the
# Siemens item "工业Agent不是「套壳」大模型！西门子百年经验灌进工业AI"
# occupied a TOP slot with body text that is pure vendor marketing --
# "西门子Xcelerator与普通软件货架最本质的区别... 要解决的是「让产品在真实
# 工业场景中持续生长」". A named commercial platform plus capability/
# value-proposition language, with no transaction, customer, regulator,
# number, or measurable outcome, is a vendor pitch rather than news.
# General marketing-register vocabulary, not a Siemens-specific rule.
_PROMOTIONAL_TERMS_ZH = frozenset(
    {
        "赋能", "助力", "打造", "全新升级", "重磅发布", "行业领先", "一站式", "解决方案", "生态",
        "深度融合", "全面布局", "持续生长", "本质区别", "核心竞争力", "护城河", "标杆", "引领行业",
        "开启新篇章", "新范式", "首发上线", "焕新", "加持",
        # Vendor value-proposition / differentiation framing ("the essential
        # difference is...", "what X solves is...", "a century of
        # experience") -- the register a marketing page uses to position a
        # named product against alternatives, rather than report an event.
        "本质的区别", "解决的是", "百年经验", "真实工业场景", "真实场景",
    }
)

# 2026-08-29 production incident (Aug 26 zh_cn digest, DELIVERED): the
# Nvidia item "华尔街高度关注英伟达第二财季业绩，盼其能继续引领 AI 板块"
# took the TOP slot. It is pre-earnings market anticipation -- "将在...
# 公布第二财季业绩", "市场对这份财报的期待很高" -- not a completed material
# event. Anticipation of a future disclosure is not itself a development;
# the disclosure is. General forward-looking/expectation vocabulary.
_ANTICIPATION_TERMS_ZH = frozenset(
    {
        "即将公布", "将公布", "将发布", "即将发布", "备受关注", "高度关注", "market期待", "市场期待",
        "期待很高", "预计将", "有望", "盼其", "拭目以待", "前瞻", "预热", "剧透", "曝光", "或将推出",
        "传将", "料将",
    }
)

# A completed-event signal strong enough to rescue a story that also
# happens to contain anticipation language (e.g. an article that reports
# actual results AND previews the next quarter).
# 2026-09-07 production incident (Sep 5 zh_cn digest, DELIVERED): the TOP
# #1 slot was "长安汽车：智驾行业 2028 年完成首轮洗牌，我们和引望会留下来" --
# a Changan vice-president PREDICTING industry consolidation by 2028 and
# asserting his own company would survive it. That is an executive forecast
# and self-positioning, not a completed AI/semiconductor event, and it
# outranked genuine same-day developments. General forward-looking
# executive-forecast vocabulary, not a Changan- or autos-specific rule.
_EXECUTIVE_FORECAST_TERMS_ZH = frozenset(
    {
        "预计", "预测", "将在", "有望在", "认为", "表示将", "预判", "展望",
        "洗牌", "格局将", "未来几年", "长期来看", "趋势将", "会留下来", "将迎来",
    }
)

# A concrete year far enough out that the statement is necessarily a
# projection rather than a report of something that happened.
_COMPLETED_EVENT_TERMS_ZH = frozenset(
    {"已发布", "今日发布", "正式发布", "宣布", "公布了", "已完成", "达成", "签署", "获批", "生效", "落地"}
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


def is_vendor_promotional_zh(title: str, excerpt: str | None) -> bool:
    """True if this reads as vendor/product marketing rather than news:
    marketing-register vocabulary with no transaction, regulator, measurable
    figure, or other independently material consequence behind it.

    Requires TWO distinct promotional markers so an ordinary story that
    happens to use one common business word ("解决方案", "生态") is not
    swept up -- a single marketing word is normal business writing; a
    cluster of them with nothing concrete attached is a pitch."""
    text = f"{title or ''} {excerpt or ''}"
    hits = sum(1 for term in _PROMOTIONAL_TERMS_ZH if term in text)
    if hits < 2:
        return False
    return not _has_material_consequence_signal_zh(text)


def is_pre_event_anticipation_zh(title: str, excerpt: str | None) -> bool:
    """True if the story is anticipation of a future disclosure/launch
    rather than a completed development. A story that ALSO reports a
    completed action (results actually published, deal actually signed) is
    not suppressed -- only pure "everyone is waiting for X" coverage is."""
    text = f"{title or ''} {excerpt or ''}"
    if not any(term in text for term in _ANTICIPATION_TERMS_ZH):
        return False
    if any(term in text for term in _COMPLETED_EVENT_TERMS_ZH):
        return False
    return True


def is_executive_forecast_zh(title: str, excerpt: str | None) -> bool:
    """True if the story is an executive's forward-looking prediction about
    how an industry will develop, with no completed action behind it.

    Requires BOTH forecast vocabulary AND a future year, so an ordinary
    report that merely mentions a plan ("预计本季度交付") is not swept up,
    while "2028 年完成首轮洗牌" is."""
    import re as _re

    text = f"{title or ''} {excerpt or ''}"
    if not any(term in text for term in _EXECUTIVE_FORECAST_TERMS_ZH):
        return False
    future_year = _re.search(r"20(2[7-9]|[3-9]\d)\s*年", text)
    if not future_year:
        return False
    if any(term in text for term in _COMPLETED_EVENT_TERMS_ZH):
        return False
    return not _has_material_consequence_signal_zh(text)


def is_low_consequence_event_type_zh(title: str, excerpt: str | None) -> bool:
    """Combined Chinese event-type/consequence gate: true if this
    candidate is a normally non-material event type -- conference/
    exhibition appearance, award/ranking/list inclusion, vendor-promotional
    copy, or pre-event anticipation -- with no independently material
    consequence attached. General event-type logic only; never a
    title-specific exclusion."""
    return (
        is_conference_appearance_without_event_zh(title, excerpt)
        or is_unqualified_recognition_story_zh(title, excerpt)
        or is_vendor_promotional_zh(title, excerpt)
        or is_pre_event_anticipation_zh(title, excerpt)
        or is_executive_forecast_zh(title, excerpt)
    )
