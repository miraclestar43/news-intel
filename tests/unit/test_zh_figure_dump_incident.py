"""2026-08-29 production incident (Aug 28 zh_cn digest, DELIVERED): the
BYD item rendered as an unedited dump of eight consecutive metric/value
pairs instead of a concise synthesis.

The excerpt below is the real production shape: an IT之家 earnings post is
a single run of "指标 ：数值，同比..." pairs with NO interior 。 at all, so
zh_sentences() returns ZERO sentences and what_happened_zh falls through to
its raw-remainder branch. Condensing only the sentence branch left the
delivered dump completely unchanged -- this test pins the branch that
production actually takes."""

from __future__ import annotations

from news_intel.pipeline.zh_render_readiness import what_happened_zh
from news_intel.pipeline.zh_text import zh_sentences

BYD_EXCERPT = (
    "IT之家 8 月 28 日消息，比亚迪今日发布 2026 年上半年（2026 年 1 月～2026 年 6 月）报告："
    " 营业总收入 ：3448.15 亿元，同比下降 7.13%"
    " 毛利润 ：649.89 亿元，同比下降 2.81%"
    " 毛利率 ：18.85%"
    " 归母净利润 ：123.25 亿元，同比下降 20.54%"
    " 经营现金流 ：373.34 亿元，同比增长 17.28%"
    " 基本每股收益 ：1.35 元，同比下降 21.05%"
    " 稀释每股收益 ：1.35 元，同比下降 21.05%。"
)


def test_both_extraction_paths_condense():
    """Pins WHY the first fix missed. The real 1072-char production row
    parses into ZERO zh_sentences (one unbroken run of metric/value pairs),
    so it takes what_happened_zh's raw-remainder branch -- while a shorter
    reconstruction of the same text parses into one sentence and takes the
    sentence branch. Condensing only the sentence branch left the delivered
    dump unchanged, so BOTH paths must condense."""
    sentence_path = BYD_EXCERPT                      # parses into >= 1 sentence
    remainder_path = BYD_EXCERPT.replace("。", "")    # parses into zero sentences
    assert zh_sentences(sentence_path) != []
    assert zh_sentences(remainder_path) == []
    for text in (sentence_path, remainder_path):
        out = what_happened_zh({"excerpt": text})
        assert out is not None and len(out) <= 170, f"un-condensed on {len(text)}-char input"


def test_financial_figure_dump_is_condensed_not_dumped():
    out = what_happened_zh({"excerpt": BYD_EXCERPT})
    assert out is not None
    assert len(out) < len(BYD_EXCERPT)
    assert len(out) <= 170
    # the headline metrics survive
    assert "比亚迪" in out
    assert "营业总收入" in out
    # the tail of the table is dropped
    assert "稀释每股收益" not in out


def test_condensed_output_ends_cleanly():
    out = what_happened_zh({"excerpt": BYD_EXCERPT})
    assert out.endswith(("。", "！", "？"))
    assert not out.endswith("，。")


def test_a_short_complete_sentence_is_left_untouched():
    short = "阿里巴巴宣布启动 800 亿港元配股，为人工智能相关发展提供资金。"
    assert what_happened_zh({"excerpt": short}) == short
