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


# 2026-08-22: real production fixture (Aug 21 zh_cn digest, already
# delivered) -- "CPM" (the law firm representing the plaintiffs) was
# picked as the principal entity, "财报业绩" (earnings) was picked as the
# event type over the story's real subject (a lawsuit), and the digest
# fabricated "1.12万亿元" from an unrelated later paragraph describing
# cumulative US tariff refunds across many companies, not anything about
# Logitech's own liability or this lawsuit.
_LOGITECH_TITLE = "产品涨价最高 25%：美国政府退税后罗技没退，罗技被消费者集体起诉"
_LOGITECH_EXCERPT = (
    "IT之家 8 月 21 日消息，当地时间 8 月 18 日，美国律师事务所 Cotchett, Pitre & McCarthy LLP"
    "（CPM）在加利福尼亚州北区联邦地区法院圣何塞分院提起集体诉讼，将罗技（Logitech）告上法庭。"
    "原告代表美国消费者，指控罗技在 2025 年以上调关税为理由涨价最高 25%，并在相关关税被美国最高法院裁定"
    "违法、政府向企业退款后，没有将消费者此前承担的相关成本退还给消费者。"
    "罗技管理层此前也曾在财报电话会议中明确提及此次涨价旨在抵消进口关税带来的成本压力。"
    "此次相关关税累计征收金额约 1,660 亿美元 （现汇率约合 1.12 万亿元人民币） 。"
)


def test_why_it_matters_zh_logitech_regression_names_defendant_and_lawsuit_no_fabricated_amount():
    primary = {"title": _LOGITECH_TITLE, "excerpt": _LOGITECH_EXCERPT}
    result = why_it_matters_zh(primary, None, "Markets & Economy", entities=[
        "CPM", "GAAP", "IEEPA", "Joe Cotchett", "K400 Plus Wireless Touch",
        "MX Master", "Matteo Anversa", "McCarthy LLP",
    ])
    assert "罗技" in result
    assert "CPM" not in result
    assert "诉讼" in result
    assert "财报" not in result
    assert "1.1万亿" not in result and "1.12万亿" not in result


def test_why_it_matters_zh_ymtc_regression_names_nand_not_dram():
    """2026-08-23: real production regression (长江存储/YMTC's Aug 22
    NAND-flash IPO story, not yet delivered) -- entities.py extracted
    ["Counterpoint Research", "DRAM", "IDM", "IPO", "NAND", "NAND Flash"]
    ("DRAM" is a background/comparison mention deep in the article; "NAND"
    is the company's own technology, named in the TITLE itself: "国产
    NAND 龙头长江存储..."), but _principal_entity_zh's old candidates[0]
    just took the first survivor in entities.py's own list order,
    picking "DRAM" over "NAND"."""
    primary = {
        "title": "国产 NAND 龙头长江存储冲刺科创板：IPO 审核状态变更为已受理，拟融资 330 亿元",
        "excerpt": "IT之家 8 月 21 日消息，上海证券交易所网站显示，长江存储控股股份有限公司首次公开发行股票并在"
        "科创板上市的 IPO 审核状态已变更为已受理。公司拟融资金额为 330 亿元，保荐机构为中信证券和中信建投。",
    }
    result = why_it_matters_zh(
        primary, None, "Markets & Economy",
        entities=["Counterpoint Research", "DRAM", "IDM", "IPO", "NAND", "NAND Flash"],
    )
    assert "NAND" in result
    assert "DRAM" not in result


_KAKAO_TITLE = "韩国 IT 巨头 Kakao 计划年内完成分拆，新实体将聚焦 AI 智能体"
_KAKAO_EXCERPT = (
    "IT之家 8 月 21 日消息，据韩媒《每日经济新闻》报道，韩国 IT 巨头 Kakao 今日召开董事会，"
    "宣布通过人事分割方式，将公司拆分为新设公司 Kakao AI 和存续公司 Kakao X。"
    "按照净资产账面价值计算，拆分比例为 Kakao AI 36%、Kakao X 64%。据报道，Kakao 给出的拆分理由是适应 "
    "AI 时代资本配置体系。新设立的 Kakao AI 将以 KakaoTalk 和 Kanana 为核心，专注于开发 AI 智能体。"
    "该公司目标将用户的停留时间提高 50% 以上，并力争在 2030 年实现年均约 20% 的营收增长，"
    "最终达到 6 万亿韩元（现汇率约合 291.06 亿元人民币）营收。"
)


def test_why_it_matters_zh_kakao_regression_names_split_not_earnings():
    """2026-08-23: real production regression (Kakao's Aug 21 zh_cn digest,
    not yet delivered) -- a corporate spin-off announcement had no
    matching event type at all, so ranking fell through to "营收"
    (revenue) -- a much-later, secondary mention that is a 2030
    aspirational TARGET, not a current fact -- rendering "Kakao AI营收
    业绩，金额约291.1亿" as if it were a reported earnings result."""
    primary = {"title": _KAKAO_TITLE, "excerpt": _KAKAO_EXCERPT}
    result = why_it_matters_zh(
        primary, None, "AI & Semiconductors",
        entities=["Kakao AI", "Kakao Bank", "Kakao Pay", "Kakao X", "SM Entertainment"],
    )
    assert "分拆" in result
    assert "营收业绩" not in result
    assert "291" not in result


def test_what_happened_zh_ymtc_regression_is_capped_not_a_full_dump():
    """2026-08-23: real production complaint (YMTC's Aug 22 zh_cn digest,
    not yet delivered) -- joining EVERY sentence from a long, detailed
    excerpt produced a "long source dump" reading as a copied paragraph
    rather than an edited summary."""
    excerpt = (
        "IT之家 8 月 21 日消息，上海证券交易所网站显示，长江存储控股股份有限公司首次公开发行股票并在科创板上市"
        "的 IPO 审核状态已变更为已受理。 公司拟融资金额为 330 亿元，保荐机构为中信证券和中信建投 。 "
        "此前 8 月 19 日，长江存储 IPO 辅导状态刚由证监会官网公示变更为辅导验收。 "
        "中信证券与中信建投于当日签署了辅导工作完成报告。 公司于 2026 年 5 月 19 日在湖北证监局完成 IPO "
        "辅导备案登记，至验收通过全程约三个月，创下半导体企业 IPO 辅导速度纪录。 "
        "招股书显示，长江存储自 2024 年扭亏后盈利水平快速提升， 2026 年 1-3 月实现营业收入 470.42 亿元，"
        "归母净利润 333.79 亿元 。"
    )
    primary = {"excerpt": excerpt}
    result = what_happened_zh(primary)
    assert result is not None
    assert "330 亿元" in result  # the key supported figure is preserved
    assert "归母净利润" not in result  # the tail (background detail) is dropped, not everything
    assert len(result) < len(excerpt)
