"""2026-09-07 incident, item 5: regressions for the content defects visible
in the Sep 5 editions that were actually DELIVERED. Every string is copied
verbatim from the rendered production message in the digests table."""

from __future__ import annotations

import pytest

from news_intel.pipeline.editorial_validation import _BANNED_PATTERNS
from news_intel.pipeline.event_integrity import is_low_consequence_event_type, is_speculative_market_commentary
from news_intel.pipeline.event_integrity_zh import is_executive_forecast_zh, is_low_consequence_event_type_zh
from news_intel.pipeline.zh_render_readiness import what_happened_zh

# --- zh: rhetorical teaser occupied TOP #2 with no information ----------

TEASER = "如此“反骨”的方法，具体又是怎么实现的？"


def test_rhetorical_teaser_is_rejected_not_rendered():
    """The whole stored excerpt WAS this one question -- there was no better
    sentence to fall back to, so it must yield no summary at all (the story
    then fails render-readiness and cannot hold a TOP slot)."""
    assert what_happened_zh({"excerpt": TEASER}) is None


def test_a_sentence_containing_a_question_but_carrying_facts_still_passes():
    text = "鸿海 8 月营收超 9000 亿元新台币，同比增 51.98%，这意味着什么？"
    assert what_happened_zh({"excerpt": text}) is not None


# --- zh: executive forecast ranked TOP #1 -------------------------------

CHANGAN_TITLE = "长安汽车：智驾行业 2028 年完成首轮洗牌，我们和引望会留下来"
CHANGAN_EXCERPT = (
    "IT之家 9 月 5 日消息，长安汽车副总裁贺刚今日在接受《每日经济新闻》采访时表示，"
    "智能驾驶行业将在 2028 年左右完成首轮洗牌，届时中国市场上具备竞争力的智驾解决方案商可能仅剩五六家。"
    "贺刚表示：“我们和引望会留下来。”"
)


def test_executive_forecast_is_not_a_completed_event():
    assert is_executive_forecast_zh(CHANGAN_TITLE, CHANGAN_EXCERPT) is True
    assert is_low_consequence_event_type_zh(CHANGAN_TITLE, CHANGAN_EXCERPT) is True


@pytest.mark.parametrize(
    ("title", "excerpt"),
    [
        ("鸿海单月营收再超 9000 亿元新台币，8 月同比增 51.98%", "鸿海公布 8 月营收。"),
        ("比亚迪 2026 上半年归母净利润 123.25 亿元", "比亚迪今日发布 2026 年上半年报告：营业总收入 3448.15 亿元。"),
    ],
)
def test_real_completed_results_from_the_same_digest_still_qualify(title, excerpt):
    assert is_low_consequence_event_type_zh(title, excerpt) is False


# --- en: speculative stock commentary held TOP #2 -----------------------

IM_TITLE = "Two Chip - Equipment Stocks Just Crushed Nvidia . Is the AI Rally Broadening ?"
IM_EXCERPT = "Equipment stocks related to chip manufacturing reportedly outperformed NVIDIA."


def test_speculative_stock_commentary_is_gated():
    assert is_speculative_market_commentary(IM_TITLE, IM_EXCERPT) is True
    assert is_low_consequence_event_type(IM_TITLE, IM_EXCERPT) is True


def test_reportedly_is_not_mistaken_for_a_completed_report():
    """"reportedly" contains the concrete-event stem "reported" while
    meaning the opposite -- an unconfirmed claim."""
    assert is_speculative_market_commentary("Is the rally broadening?", "Stocks reportedly rose.") is True
    assert is_speculative_market_commentary("Is the rally broadening?", "Nvidia announced an acquisition.") is False


@pytest.mark.parametrize(
    ("title", "excerpt"),
    [
        ("The Seattle Times and Newsday sue OpenAI and Microsoft over AI training",
         "The parent company filed a lawsuit against OpenAI and Microsoft."),
        ("Nvidia acquired Run:ai. Is the AI rally broadening?",
         "Nvidia announced it acquired the company."),
        ("Fed holds rates steady", "The Federal Reserve announced it held rates steady."),
    ],
)
def test_genuine_events_are_not_gated_as_commentary(title, excerpt):
    assert is_low_consequence_event_type(title, excerpt) is False


# --- en: tautological market consequence --------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "This trend could indicate that investor interest in AI is spreading, potentially affecting valuations "
        "across chip manufacturing equipment and materials companies.",
        "The move could affect valuations across the sector.",
        "这一趋势或将影响芯片制造设备公司的估值。",
    ],
)
def test_generic_market_implication_is_banned(text):
    assert any(p.search(text) for p in _BANNED_PATTERNS), f"not caught: {text!r}"


def test_a_specific_consequence_is_still_allowed():
    text = (
        "The case could set precedents for how AI companies use copyrighted news content for model training, "
        "directly impacting the economics of both the AI industry and news publishers."
    )
    assert not any(p.search(text) for p in _BANNED_PATTERNS)
