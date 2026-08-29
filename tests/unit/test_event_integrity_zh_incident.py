"""2026-08-29 production incident: real-text regressions for the two
content gates that were missing when the Aug 26 and Aug 28 zh_cn digests
were actually DELIVERED to the reader. Every string below is copied
verbatim from the rendered production message stored in the digests table
-- not paraphrased -- so these tests fail if either regression returns.

The negative cases matter as much as the positive ones: the same gates
must not suppress the genuine completed-event stories that appeared in the
same digests (BYD/Tongwei/Luxshare results, the Alibaba placement, the
MFA press conference)."""

from __future__ import annotations

import pytest

from news_intel.pipeline.event_integrity_zh import (
    is_low_consequence_event_type_zh,
    is_pre_event_anticipation_zh,
    is_vendor_promotional_zh,
)

# --- verbatim production text ------------------------------------------

SIEMENS_TITLE = "工业Agent不是“套壳”大模型！西门子百年经验灌进工业AI"
SIEMENS_EXCERPT = (
    "西门子Xcelerator与普通软件货架最本质的区别。 货架解决的是「把产品卖出去」，"
    "西门子Xcelerator要解决的是「让产品在真实工业场景中持续生长」。"
)

NVIDIA_TITLE = "华尔街高度关注英伟达第二财季业绩，盼其能继续引领 AI 板块"
NVIDIA_EXCERPT = (
    "IT之家 8 月 26 日消息，英伟达将在当地时间周三（26 日）压轴公布第二财季业绩，"
    "为本轮科技公司财报季收官。 市场对这份财报的期待很高，压力同样不小。"
)

BYD_TITLE = "比亚迪 2026 上半年归母净利润 123.25 亿元，同比下降 20.54%"
BYD_EXCERPT = (
    "IT之家 8 月 28 日消息，比亚迪今日发布 2026 年上半年（2026 年 1 月～2026 年 6 月）报告："
    " 营业总收入 ：3448.15 亿元，同比下降 7.13%"
)

TONGWEI_TITLE = "光伏企业通威股份 2026 年上半年归母净亏损 51.19 亿元，同比扩大 3.31%"
LUXSHARE_TITLE = "立讯精密 2026 年上半年归母净利润 78.43 亿元，同比增长 18.04%"


# --- vendor-promotional gate (Aug 28 TOP slot) --------------------------


def test_siemens_xcelerator_item_is_rejected_as_vendor_promotional():
    """This occupied a TOP slot in the DELIVERED Aug 28 zh_cn digest."""
    assert is_vendor_promotional_zh(SIEMENS_TITLE, SIEMENS_EXCERPT) is True
    assert is_low_consequence_event_type_zh(SIEMENS_TITLE, SIEMENS_EXCERPT) is True


def test_a_single_common_business_word_is_not_promotional():
    """One ordinary business word must not sweep up a real story -- the
    gate requires a CLUSTER of marketing markers with nothing concrete."""
    assert is_vendor_promotional_zh(
        "某公司发布新一代解决方案并获得 10 亿元订单", "该公司宣布签署合同，金额 10 亿元。"
    ) is False


# --- pre-event anticipation gate (Aug 26 TOP slot) ----------------------


def test_nvidia_pre_earnings_item_is_rejected_as_anticipation():
    """This occupied the TOP slot in the DELIVERED Aug 26 zh_cn digest:
    market anticipation of a not-yet-published earnings report."""
    assert is_pre_event_anticipation_zh(NVIDIA_TITLE, NVIDIA_EXCERPT) is True
    assert is_low_consequence_event_type_zh(NVIDIA_TITLE, NVIDIA_EXCERPT) is True


def test_actual_results_are_not_treated_as_anticipation():
    """A story reporting results that WERE published must pass, even if it
    also previews what comes next."""
    assert is_pre_event_anticipation_zh(
        "英伟达公布第二财季业绩", "英伟达今日发布财报，营收 300 亿美元，并预计下季度将继续增长。"
    ) is False


# --- the genuine stories from the same digests must still qualify -------


@pytest.mark.parametrize(
    ("title", "excerpt"),
    [
        (BYD_TITLE, BYD_EXCERPT),
        (TONGWEI_TITLE, "通威股份发布半年报，归母净亏损 51.19 亿元。"),
        (LUXSHARE_TITLE, "立讯精密今日发布 2026 年上半年报告：营业总收入 1745.04 亿元，同比增长 40.16%"),
        ("阿里巴巴计划配股融资约100亿美元", "阿里巴巴宣布启动 800 亿港元配股，为人工智能相关发展提供资金。"),
        ("2026年8月24日外交部发言人林剑主持例行记者会", "外交部发言人林剑主持例行记者会，就多项国际议题答记者问。"),
    ],
)
def test_genuine_completed_events_from_the_same_digests_still_qualify(title, excerpt):
    assert is_low_consequence_event_type_zh(title, excerpt) is False
