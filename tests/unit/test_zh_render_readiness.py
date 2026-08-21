"""pipeline/zh_render_readiness.py: the zh_cn counterpart to
test_render_readiness.py -- Chinese sentence extraction, render-readiness,
and story-specific why-it-matters composition must never be a bare
category-only template when an entity, event type, or amount was
extractable, and must never invent a fact not present in the source text."""

from __future__ import annotations

from news_intel.pipeline.zh_render_readiness import (
    is_render_ready_zh,
    what_happened_zh,
    why_it_matters_zh,
)


def _primary(**overrides) -> dict:
    base = {
        "title": "商务部宣布对相关企业实施新一轮出口管制措施",
        "excerpt": "商务部新闻发言人表示，将对涉及先进制程的半导体制造设备实施新一轮出口管制。",
    }
    base.update(overrides)
    return base


def test_what_happened_zh_extracts_a_complete_punctuated_sentence():
    result = what_happened_zh(_primary())
    assert result == "商务部新闻发言人表示，将对涉及先进制程的半导体制造设备实施新一轮出口管制。"


def test_what_happened_zh_strips_known_preamble():
    primary = _primary(
        excerpt="来源：新华社：商务部新闻发言人表示，将对涉及先进制程的半导体制造设备实施新一轮出口管制。"
    )
    result = what_happened_zh(primary)
    assert result is not None
    assert not result.startswith("来源")


def test_what_happened_zh_returns_none_for_empty_excerpt():
    assert what_happened_zh({"title": "标题", "excerpt": ""}) is None


def test_is_render_ready_zh_true_when_summary_extractable():
    assert is_render_ready_zh(_primary()) is True


def test_is_render_ready_zh_false_with_no_usable_excerpt():
    assert is_render_ready_zh({"title": "标题", "excerpt": None}) is False


def test_why_it_matters_zh_names_entity_and_event_type():
    result = why_it_matters_zh(_primary(), None, "US / China / World", entities=["商务部"])
    assert "商务部" in result
    assert "出口管制" in result
    assert result != "看点：中美关系或全球局势的重要动态。"


def test_why_it_matters_zh_without_entity_or_event_type_falls_back_to_amount_tier():
    """Real defect found via a synthetic-data smoke replay: a genuine 万/亿
    amount was silently discarded when no entity/event-type matched --
    only the bare category-only floor remained. Must surface the amount."""
    primary = {
        "title": "国家发展改革委发布人工智能产业发展指导意见",
        "excerpt": (
            "国家发展改革委今日发布指导意见，明确提出将加大对人工智能大模型训练算力的政策支持力度，"
            "涉及资金规模约500亿元。"
        ),
    }
    result = why_it_matters_zh(primary, None, "Politics", entities=None)
    assert "500亿" in result
    assert result != "看点：国内政策的重要动态。"


def test_why_it_matters_zh_with_genuinely_nothing_extractable_uses_bare_category_floor():
    primary = {"title": "标题", "excerpt": "这是一段没有特定实体或金额的一般性描述文字。"}
    result = why_it_matters_zh(primary, None, "Markets & Economy", entities=None)
    assert result == "看点：企业财务或宏观经济的重要动态。"


def test_why_it_matters_zh_recognizes_mixed_language_dollar_figure():
    """zh_cn sources routinely mix Chinese and English/dollar amounts in
    the same story (e.g. a Chinese article quoting a USD deal size
    verbatim) -- a $ figure must not be silently dropped just because no
    Chinese-unit (万/亿) amount is present."""
    primary = {
        "title": "某科技公司完成新一轮融资",
        "excerpt": "该公司今日宣布完成新一轮融资，融资规模达到 $500 million，由多家国际投资机构参与。",
    }
    result = why_it_matters_zh(primary, None, "AI & Semiconductors", entities=None)
    assert "500 million" in result or "$500" in result


def test_why_it_matters_zh_never_fabricates_an_amount_not_in_source_text():
    primary = {"title": "标题", "excerpt": "商务部新闻发言人表示，将对相关企业实施新一轮出口管制。"}
    result = why_it_matters_zh(primary, None, "US / China / World", entities=None)
    assert "金额" not in result


# 2026-08-18: real defects found via an actual live-collection replay
# against the validated zh_cn sources (not a synthetic fixture) --
# a NAND-market report's excerpt embedded English chart captions
# ("Top Five NAND Flash Brands Rises", "Says TrendForce"), and entities[0]
# (alphabetically-sorted) picked a generic finance acronym / a garbled
# multi-word caption fragment over the genuine subject.
def test_why_it_matters_zh_prefers_single_word_entity_over_garbled_caption_fragment():
    entities = [
        "ASP", "Brands Rises", "Combined Revenue", "Micron", "Micron Moves Up",
        "NAND", "Says TrendForce", "Third Place", "Top Five NAND Flash",
    ]
    primary = {
        "title": "TrendForce：五大海外 NAND 闪存原厂 2026Q2 合计营收环比 +77%",
        "excerpt": "五大海外 NAND 闪存原厂 2026Q2 合计营收达 688.7 亿美元，环比增幅高达 77.0%。"
        "其中美光 NAND 营收环比增幅达 99.2%，几乎实现单季度翻倍。",
    }
    result = why_it_matters_zh(primary, None, "Markets & Economy", entities=entities)
    assert "Micron" in result or "NAND" in result
    assert "ASP" not in result
    assert "Brands Rises" not in result
    assert "Says TrendForce" not in result
    assert "Top Five NAND Flash" not in result


def test_what_happened_zh_rejects_an_unpunctuated_teaser_fragment_as_not_render_ready():
    """A bare RSS teaser hook with no terminal punctuation at all
    ("回头看，A社就这样完成了反超") must not pass as a genuine "what
    happened" summary merely by clearing a low character-count floor --
    it never actually states what happened (no entity, no amount, no
    action)."""
    primary = {"title": "650亿美元！IPO前夕，Anthropic营收底牌曝光", "excerpt": "回头看，A社就这样完成了反超"}
    assert is_render_ready_zh(primary) is False
    assert what_happened_zh(primary) is None


def test_what_happened_zh_still_accepts_a_genuinely_substantive_unpunctuated_fallback():
    """Non-regression: the raised floor must not reject a genuinely
    substantive fragment merely for lacking terminal punctuation."""
    primary = {
        "title": "标题",
        "excerpt": "商务部新闻发言人今日就近期贸易摩擦相关问题作出正式回应并公布具体应对措施安排",
    }
    assert is_render_ready_zh(primary) is True


# 2026-08-21: real production incident regression fixtures (Aug 20 zh_cn
# TCL/广州华星 digest). The delivered digest fabricated "金额约2.2万亿"
# (~2.2 trillion yuan) for a story whose own transaction value is 93.25亿元
# (9.325 billion yuan) -- traced to a "相关阅读" trailer linking to an
# UNRELATED LG Display/TCL transaction priced in Korean won
# ("2.2466万亿韩元"). ~236x too large, wrong currency, wrong story.
_TCL_TITLE = "100% 控股 t9 产线：TCL 科技 93.25 亿元收购广州华星 45% 股权获证监会注册"
_TCL_EXCERPT = (
    "IT之家 8 月 20 日消息，TCL 科技今日披露，公司发行股份及支付现金购买广州华星光电半导体显示技术有限公司"
    "（即 TCL 华星 t9 产线运营主体）45% 股权事项，已获中国证监会同意注册。"
    "本次交易总对价约 93.25 亿元，其中现金支付与股份支付各占约 46.62 亿元。"
    "交易完成后，TCL 科技将直接及间接控制广州华星半导体 100% 股权。"
    "t9 产线总投资 350 亿元，规划月产能 18 万片玻璃基板。"
    "相关阅读： 《 LG 显示以定价 2.2466 万亿韩元将广州 LCD 工厂出售给 TCL 华星 》"
)


def test_why_it_matters_zh_tcl_regression_no_fabricated_trillion_figure():
    primary = {"title": _TCL_TITLE, "excerpt": _TCL_EXCERPT}
    result = why_it_matters_zh(primary, None, "Markets & Economy", entities=["TCL"])
    assert "万亿" not in result


def test_why_it_matters_zh_tcl_regression_uses_the_labeled_deal_value_not_an_unrelated_larger_figure():
    """Prefers the explicitly-labeled "交易总对价约93.25亿元" over both the
    wrong-currency trailer figure AND the acquired production line's own
    unrelated (larger) total-investment figure mentioned later for
    background context."""
    primary = {"title": _TCL_TITLE, "excerpt": _TCL_EXCERPT}
    result = why_it_matters_zh(primary, None, "Markets & Economy", entities=["TCL"])
    assert "93.2亿" in result or "93.25亿" in result
    assert "350亿" not in result


def test_what_happened_zh_tcl_regression_excludes_trailer_from_summary():
    primary = {"title": _TCL_TITLE, "excerpt": _TCL_EXCERPT}
    result = what_happened_zh(primary)
    assert result is not None
    assert "相关阅读" not in result
    assert "韩元" not in result


# 2026-08-21: second real production fixture (Aug 19 zh_cn 江淮汽车 digest)
# -- the delivered summary was a raw dump of every reported financial-
# statement line item plus the source's own "AI 解读" commentary
# boilerplate, not an edited summary.
_JAC_TITLE = "江淮汽车 2026 年上半年归母亏损 7.49 亿元，同比亏损收窄 3.09%"
_JAC_EXCERPT = (
    "IT之家 8 月 19 日消息，江淮汽车今日发布 2026 年半年度报告：营业总收入：221.30 亿元，同比增长 14.31%"
    "归母净利润：-7.49 亿元，同比亏损收窄 3.09% 扣非净利润：-9.91 亿元，同比亏损扩大 8.22%"
    "经营现金流：-52.88 亿元，净流出扩大 68.03%"
    "AI 解读 本次财报营收表现符合行业增长趋势，尚未查到主流机构对本次财报的一致预期数据。"
)


def test_what_happened_zh_jac_regression_strips_ai_commentary_boilerplate():
    primary = {"title": _JAC_TITLE, "excerpt": _JAC_EXCERPT}
    result = what_happened_zh(primary)
    assert result is not None
    assert "AI 解读" not in result
    assert "尚未查到主流机构" not in result



def test_why_it_matters_zh_jac_regression_names_the_loss_not_revenue():
    """2026-08-21: why_it_matters_zh()'s amount selection was a bare
    max(zh_values) over the whole article, which for an earnings report
    always favors revenue (almost always the largest-magnitude figure)
    over a net loss, erasing the real tension -- 江淮汽车's 221.3亿元
    revenue was picked over its -7.49亿元 net loss purely because it's
    the bigger number."""
    primary = {"title": _JAC_TITLE, "excerpt": _JAC_EXCERPT}
    result = why_it_matters_zh(primary, None, "Markets & Economy")
    assert "净亏损约7.5亿" in result
    assert "221.3" not in result
    assert "221.30" not in result
