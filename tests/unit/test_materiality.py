"""2026-08-12 scoring repair: materiality must be a real signal derived
from the event's own text, not a proxy for source tier/confirmation state,
and must not structurally cap single-source non-AI stories."""

from __future__ import annotations

from news_intel.pipeline.materiality import compute_materiality, parse_current_dollar_values


def test_routine_vendor_announcement_scores_lower_than_magnitude_deal():
    routine = compute_materiality(
        "NVIDIA JetPack 7.2.1 Adds Agentic Video Skills. NVIDIA today introduced new features.",
        "ai_semiconductor_export_controls",
    )
    material = compute_materiality(
        "NVIDIA Partners With Apollo, BlackRock and KKR to Mobilize Over $500 Billion of Capital.",
        "ai_semiconductor_export_controls",
    )
    assert material > routine


def test_single_sourced_regulatory_action_is_not_capped_below_threshold():
    """The pre-repair formula (40 + 25*has_t0 + confirmation_quality*0.25)
    plateaued every single-source UNVERIFIED event at exactly 47.5
    regardless of substance. Materiality has no source-tier/confirmation
    input at all, so a genuinely material single-source regulatory action
    can clear a 58-point threshold on text content alone."""
    text = (
        "Schedules of Controlled Substances: Rescheduling of Suvorexant, Lemborexant, "
        "and Daridorexant From Schedule IV Into Schedule V."
    )
    score = compute_materiality(text, "us_china_policy_geopolitics")
    assert score >= 58


def test_materiality_has_no_source_tier_or_confirmation_input():
    """compute_materiality's signature takes only text and category bucket
    -- there is no has_t0/confirmation_state parameter, so it is
    structurally impossible for source authority or evidence confidence to
    influence this component (they remain their own separately-weighted
    scoring components)."""
    import inspect

    params = list(inspect.signature(compute_materiality).parameters)
    assert params == ["text", "bucket"]


def test_magnitude_signal_detects_dollar_and_percent_figures():
    with_magnitude = compute_materiality("Company reports a $2.3 billion writedown.", "markets_macro_corporate")
    without = compute_materiality("Company reports quarterly results.", "markets_macro_corporate")
    assert with_magnitude > without


def test_high_impact_keyword_raises_score_over_routine_language():
    sanction_text = "Government imposes new sanctions and export controls on the firm."
    routine_text = "Company unveils new product and celebrates the launch."
    assert compute_materiality(sanction_text, "us_china_policy_geopolitics") > compute_materiality(
        routine_text, "us_china_policy_geopolitics"
    )


def test_small_dollar_figure_earns_no_magnitude_bonus():
    """2026-08-12 defect: a website's "$100/day" running cost earned the
    same magnitude bonus as a multi-billion-dollar acquisition just for
    matching "$<number>" -- must now scale with actual parsed value."""
    small = compute_materiality(
        "A look at AI news website RuntimeWire, which costs $100/day to run.", "ai_semiconductor_export_controls"
    )
    large = compute_materiality(
        "Anthropic is in talks to buy Decart for about $6 billion.", "ai_semiconductor_export_controls"
    )
    assert large > small


def test_generic_ai_media_story_does_not_reach_maximum_materiality():
    """2026-08-12 defect reproduction: a media-industry story about an AI
    newsroom "beating" reporters to a security-conference disclosure
    reached materiality=100 purely from two trivial "$100" mentions plus
    one incidental "hacking" reference -- must no longer max out."""
    text = (
        'A look at AI news website RuntimeWire, which costs $100/day to run and "beat" human '
        "reporters to OpenAI's Black Hat disclosure, favoring quantity over quality. Last week, an "
        "AI newsroom beat mainstream journalists to a story about OpenAI and hacking."
    )
    score = compute_materiality(text, "ai_semiconductor_export_controls")
    assert score < 90


def test_abbreviated_dollar_units_are_parsed():
    """Techmeme-style headlines commonly abbreviate ("$575M", "$6B") rather
    than spelling out "million"/"billion" -- both must be recognized."""
    bucket = "ai_semiconductor_export_controls"
    abbreviated = compute_materiality("Nebius reports revenue of $575M this quarter.", bucket)
    baseline = compute_materiality("Nebius reports steady revenue this quarter.", bucket)
    assert abbreviated > baseline


def test_hyphenated_dollar_unit_is_parsed():
    """2026-08-25 repair: real live-model replay found "HK$80-billion" (a
    hyphen between the number and the spelled-out unit -- common
    journalistic style, e.g. "a $6-billion deal") matched only the bare
    "$80" with no unit, silently dropping the magnitude from $80B to $80 --
    the resulting evidence packet then couldn't corroborate the model's
    own (correct) $80,000,000,000 figure, rejecting real, traceable output
    as unsupported."""
    from news_intel.pipeline.materiality import dollar_values_with_position

    values = [v for v, _pos in dollar_values_with_position("HK$80-billion ($10.2 billion) share placement")]
    assert 80_000_000_000.0 in values
    assert 10_200_000_000.0 in values


# 2026-08-18 numeric-role repair: parse_current_dollar_values excludes a
# figure the text itself frames as a historical/comparison baseline
# ("down from", "up from", "compared to", "versus") -- real defect found
# via the Aug 17 replay, where a why-it-matters line anchored on the
# discarded $40B ceiling instead of the actual current $25B target.
def test_parse_current_dollar_values_excludes_down_from_baseline():
    text = "Shein is aiming to hit a ~$25B valuation, down from its $30B-$40B goal earlier in August."
    assert parse_current_dollar_values(text) == [25_000_000_000.0]


def test_parse_current_dollar_values_excludes_up_from_baseline():
    text = "Anthropic's revenue run rate reached $65B by the end of July, up from $47B in May and $9B in late 2025."
    assert parse_current_dollar_values(text) == [65_000_000_000.0]


def test_parse_current_dollar_values_keeps_a_later_unrelated_restatement():
    """A baseline marker earlier in the text must not blanket-exclude a
    later, unrelated repetition of the actual current figure -- the
    exclusion is a bounded lookback window, not "anywhere earlier"."""
    text = (
        "Shein is aiming to hit a ~$25B valuation, down from its $30B-$40B goal earlier in August "
        "after investor meetings. Online fast-fashion retailer Shein is eyeing a company valuation "
        "of around $25 billion in its Hong Kong IPO."
    )
    values = parse_current_dollar_values(text)
    assert 25_000_000_000.0 in values
    assert 40_000_000_000.0 not in values
    assert 30_000_000_000.0 not in values


def test_parse_current_dollar_values_with_no_baseline_marker_keeps_everything():
    text = "Crusoe's $3B pre-IPO fundraise is set to value it at $35B."
    assert set(parse_current_dollar_values(text)) == {3_000_000_000.0, 35_000_000_000.0}


def test_dollar_magnitude_bonus_scoring_is_unaffected_by_baseline_exclusion():
    """Scoring deliberately keeps using the raw maximum -- a story
    mentioning $40B anywhere is materially significant for ranking
    purposes regardless of which figure is "current"."""
    from news_intel.pipeline.materiality import dollar_magnitude_bonus

    text = "Shein is aiming to hit a ~$25B valuation, down from its $30B-$40B goal earlier in August."
    assert dollar_magnitude_bonus(text) == 40.0  # top band, driven by the $40B figure


def test_dollar_magnitude_bonus_recognizes_chinese_unit_amounts():
    """A Chinese-unit magnitude (万/亿/万亿) earns the same banded bonus as
    an equivalent-size $ figure -- zh_cn content has no $ sign at all."""
    from news_intel.pipeline.materiality import dollar_magnitude_bonus

    zh_text = "该公司完成了115亿美元的融资交易。"  # ~$11.5 billion financing deal
    en_equivalent = "The company completed an $11.5 billion financing deal."
    assert dollar_magnitude_bonus(zh_text) == dollar_magnitude_bonus(en_equivalent)
    assert dollar_magnitude_bonus(zh_text) > 0.0
