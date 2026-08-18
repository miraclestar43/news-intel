"""pipeline/zh_text.py: Chinese-aware text processing primitives -- no
existing dedicated coverage before this file (only indirectly exercised via
test_zh_render_readiness.py/test_materiality.py). Covers sentence
segmentation (full+half-width punctuation), preamble stripping, and
万/亿/万亿-aware amount parsing (including longest-prefix-first correctness
and mixed full-width digit handling)."""

from __future__ import annotations

from news_intel.pipeline.zh_text import (
    format_zh_amount,
    has_zh_text,
    parse_zh_amounts,
    strip_zh_preamble,
    zh_sentences,
)

# --- sentence segmentation ---------------------------------------------------


def test_zh_sentences_splits_on_full_width_period():
    result = zh_sentences("商务部今日发布公告。此举被视为重要政策信号。")
    assert result == ["商务部今日发布公告。", "此举被视为重要政策信号。"]


def test_zh_sentences_splits_on_full_width_question_and_exclamation():
    result = zh_sentences("这是真的吗？没错！确实如此。")
    assert result == ["这是真的吗？", "没错！", "确实如此。"]


def test_zh_sentences_splits_on_full_width_semicolon():
    result = zh_sentences("第一项措施已生效；第二项措施仍在评估中。")
    assert result == ["第一项措施已生效；", "第二项措施仍在评估中。"]


def test_zh_sentences_accepts_half_width_punctuation_too():
    """Mixed full/half-width punctuation is common in Chinese tech
    writing (see the ithome/qbitai live samples)."""
    result = zh_sentences("这是一个测试!真的很重要?")
    assert result == ["这是一个测试!", "真的很重要?"]


def test_zh_sentences_drops_trailing_ellipsis_without_fabricating_punctuation():
    result = zh_sentences("消息仍在持续更新中……")
    assert result == []  # no real sentence-ending punctuation remains


def test_zh_sentences_returns_empty_for_no_terminal_punctuation():
    assert zh_sentences("这是一段没有句末标点的文字") == []


def test_zh_sentences_returns_empty_for_empty_text():
    assert zh_sentences("") == []


def test_zh_sentences_strips_preamble_before_splitting():
    """strip_zh_preamble only strips ONE recognized preamble level -- "来源："
    is a recognized cue, but "新华社：" is not itself one of the recognized
    words (来源/转自/原标题/记者/编辑/撰文/作者), so it is correctly left as
    part of the sentence rather than guessed away."""
    result = zh_sentences("来源：新华社：商务部今日发布公告。")
    assert result == ["新华社：商务部今日发布公告。"]


# --- preamble stripping ------------------------------------------------------


def test_strip_zh_preamble_drops_source_cue():
    assert strip_zh_preamble("来源：新华社：商务部今日发布公告。") == "新华社：商务部今日发布公告。"


def test_strip_zh_preamble_drops_reporter_cue():
    assert strip_zh_preamble("记者：张三：商务部今日发布公告。") == "张三：商务部今日发布公告。"


def test_strip_zh_preamble_drops_original_title_cue():
    assert strip_zh_preamble("原标题：商务部今日发布公告") == "商务部今日发布公告"


def test_strip_zh_preamble_leaves_unrecognized_colon_usage_alone():
    """A genuine headline convention (e.g. "专题：...") must not be
    mistaken for a recognized aggregator/byline preamble."""
    text = "专题：中美贸易关系最新进展"
    assert strip_zh_preamble(text) == text


def test_strip_zh_preamble_returns_original_for_empty_text():
    assert strip_zh_preamble("") == ""


def test_strip_zh_preamble_never_returns_empty_string_when_only_preamble_present():
    """Never discards content it can't confidently identify as more than
    a bare preamble -- falls back to the original text unchanged."""
    result = strip_zh_preamble("来源：")
    assert result  # non-empty, some usable text remains (the original)


# --- amount parsing -----------------------------------------------------


def test_parse_zh_amounts_wan():
    assert parse_zh_amounts("融资规模达到500万元") == [5_000_000.0]


def test_parse_zh_amounts_yi():
    assert parse_zh_amounts("投资规模约50亿元") == [5_000_000_000.0]


def test_parse_zh_amounts_wanyi_longest_prefix_first():
    """万亿 (10^12) must never be mis-parsed as 万 (10^4) immediately
    followed by a stray 亿 -- the regex/multiplier table is checked
    longest-suffix-first specifically to prevent this."""
    assert parse_zh_amounts("市场规模预计达到2万亿元") == [2_000_000_000_000.0]


def test_parse_zh_amounts_yi_meiyuan():
    assert parse_zh_amounts("融资规模达到115亿美元") == [11_500_000_000.0]


def test_parse_zh_amounts_decimal_value():
    assert parse_zh_amounts("营收同比增长至3.5亿元") == [350_000_000.0]


def test_parse_zh_amounts_multiple_values_in_one_text():
    values = parse_zh_amounts("A轮融资5000万元，B轮融资2亿元")
    assert values == [50_000_000.0, 200_000_000.0]


def test_parse_zh_amounts_returns_empty_for_no_amount():
    assert parse_zh_amounts("这是一段没有金额的文字") == []


def test_parse_zh_amounts_does_not_parse_bare_dollar_figures():
    """Bare $ figures are materiality._parse_dollar_values'/
    parse_current_dollar_values' job, not zh_text's -- zh_cn text may mix
    both in the same story."""
    assert parse_zh_amounts("融资规模达到 $500 million") == []


def test_format_zh_amount_roundtrips_yi():
    assert format_zh_amount(5_000_000_000.0) == "50亿"


def test_format_zh_amount_roundtrips_wan():
    assert format_zh_amount(5_000_000.0) == "500万"


def test_format_zh_amount_roundtrips_wanyi():
    assert format_zh_amount(2_000_000_000_000.0) == "2万亿"


def test_format_zh_amount_falls_back_to_bare_number_below_wan():
    assert format_zh_amount(5_000.0) == "5,000"


# --- has_zh_text --------------------------------------------------------


def test_has_zh_text_true_for_chinese_characters():
    assert has_zh_text("商务部今日发布公告") is True


def test_has_zh_text_false_for_pure_english():
    assert has_zh_text("The Commerce Ministry issued a statement today.") is False


def test_has_zh_text_true_for_mixed_language_text():
    assert has_zh_text("该公司完成了 $500 million 的融资") is True


def test_has_zh_text_false_for_empty_or_none():
    assert has_zh_text("") is False
    assert has_zh_text(None) is False
