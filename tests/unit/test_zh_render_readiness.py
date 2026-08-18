"""pipeline/zh_render_readiness.py: the zh_cn counterpart to
test_render_readiness.py -- Chinese sentence extraction, render-readiness,
and story-specific why-it-matters composition must never be a bare
category-only template when an entity, event type, or amount was
extractable, and must never invent a fact not present in the source text."""

from __future__ import annotations

from news_intel.pipeline.zh_render_readiness import is_render_ready_zh, what_happened_zh, why_it_matters_zh


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
