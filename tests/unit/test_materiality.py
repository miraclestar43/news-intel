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
