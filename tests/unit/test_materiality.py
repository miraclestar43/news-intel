"""2026-08-12 scoring repair: materiality must be a real signal derived
from the event's own text, not a proxy for source tier/confirmation state,
and must not structurally cap single-source non-AI stories."""

from __future__ import annotations

from news_intel.pipeline.materiality import compute_materiality


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
