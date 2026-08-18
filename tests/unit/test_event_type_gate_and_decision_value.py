"""2026-08-14 reconciliation: the event-type/consequence gate (tutorials,
individual-employee enforcement) and decision-value ranking (materiality
decoupled from confirmation-weighted final_score)."""

from __future__ import annotations

from news_intel.pipeline.daily_selection import _decision_value
from news_intel.pipeline.event_integrity import (
    is_corporate_vision_content_without_event,
    is_individual_enforcement_without_consequence,
    is_low_consequence_event_type,
    is_routine_tutorial_or_promotional_content,
)

_GPT_GUIDE_TITLE = "The builder's guide to GPT-5.6"
_GPT_GUIDE_EXCERPT = (
    "Learn how startups use GPT-5.6 to build faster, more cost-efficient AI agents with smarter "
    "model selection and new Responses API capabilities."
)
_REGIONS_BANK_TITLE = "Federal Reserve Board issues enforcement action with former employee of Regions Bank"
_REGIONS_BANK_EXCERPT = (
    "The Federal Reserve Board announced an enforcement action against a former employee of Regions Bank."
)


def test_builder_guide_fails_the_tutorial_gate():
    assert is_routine_tutorial_or_promotional_content(_GPT_GUIDE_TITLE, _GPT_GUIDE_EXCERPT) is True
    assert is_low_consequence_event_type(_GPT_GUIDE_TITLE, _GPT_GUIDE_EXCERPT) is True


def test_tutorial_with_a_genuine_new_capability_signal_is_not_excluded():
    """General event-type logic, not a title-specific carve-out: a "guide"
    that also reports a real dollar-scale or decision/consequence signal
    is not treated as routine."""
    title = "A guide to the new $2 billion enterprise licensing program"
    excerpt = "This guide explains the newly announced $2 billion licensing program available to customers."
    assert is_routine_tutorial_or_promotional_content(title, excerpt) is False


def test_regions_bank_individual_enforcement_fails_the_gate():
    assert is_individual_enforcement_without_consequence(_REGIONS_BANK_TITLE, _REGIONS_BANK_EXCERPT) is True
    assert is_low_consequence_event_type(_REGIONS_BANK_TITLE, _REGIONS_BANK_EXCERPT) is True


def test_systemic_enforcement_action_remains_eligible():
    """A genuinely systemic Fed action -- against the institution itself,
    with a real dollar penalty -- must NOT be excluded by the same gate
    that correctly excludes a single-employee action."""
    title = "Federal Reserve imposes $500 million civil money penalty on Bank of America"
    excerpt = (
        "The Fed announced a $500 million penalty against the bank for systemic risk management "
        "failures affecting millions of accounts."
    )
    assert is_individual_enforcement_without_consequence(title, excerpt) is False
    assert is_low_consequence_event_type(title, excerpt) is False


def test_enforcement_against_the_institution_itself_is_not_individual():
    """No "former employee"/individual-subject language at all -- an
    action against the institution, even without a stated dollar figure,
    must not match the individual-enforcement exclusion."""
    title = "Federal Reserve Board issues enforcement action against Example Bank"
    excerpt = "The Federal Reserve Board announced a consent order against Example Bank."
    assert is_individual_enforcement_without_consequence(title, excerpt) is False


def test_applied_materials_decision_value_exceeds_gpt_guide_and_regions_bank():
    """The exact 2026-08-13 regression, reproduced with real score
    decomposition values pulled from the live incident."""
    applied_materials = {
        "final_score": 65.55, "significance": 94.0, "user_relevance": 71.5, "market_impact": 65.0,
    }
    gpt_guide = {"final_score": 68.05, "significance": 65.0, "user_relevance": 71.5, "market_impact": 65.0}
    regions_bank = {"final_score": 66.4, "significance": 65.0, "user_relevance": 63.25, "market_impact": 65.0}

    # final_score alone (the OLD ranking key) got this backwards:
    assert applied_materials["final_score"] < gpt_guide["final_score"]

    # decision_value (materiality-first) gets it right:
    assert _decision_value(applied_materials) > _decision_value(gpt_guide)
    assert _decision_value(applied_materials) > _decision_value(regions_bank)


def test_decision_value_falls_back_to_final_score_without_decomposition():
    """Test fixtures / callers that don't set significance/user_relevance/
    market_impact still get a sensible ranking key."""
    item = {"final_score": 42.0}
    assert _decision_value(item) == 42.0


# 2026-08-18 repair: a first-party corporate newsroom essay ("Securing the
# Infrastructure of Intelligence" -- pure industry-positioning language,
# no transaction/figure/dated action) was selected as a MORE item with
# nothing behind the headline but a slogan and a link. General vision-
# language + absence-of-event-verb logic, not a per-publisher exclusion.
_VISION_ESSAY_TITLE = "Securing the Infrastructure of Intelligence"
_VISION_ESSAY_EXCERPT = (
    "AI factories are the defining infrastructure of the AI era—where compute transforms energy and "
    "data into intelligence that powers every business, industry and country. In the AI economy, "
    "compute is revenue. AI factories require a full stack of critical resources: advanced chips, "
    "packaging, memory, and networking – as well as land, power and shell."
)


def test_corporate_vision_essay_with_no_concrete_event_fails_the_gate():
    assert is_corporate_vision_content_without_event(_VISION_ESSAY_TITLE, _VISION_ESSAY_EXCERPT) is True
    assert is_low_consequence_event_type(_VISION_ESSAY_TITLE, _VISION_ESSAY_EXCERPT) is True


def test_vision_language_with_a_genuine_announced_deal_is_not_excluded():
    """General event-type logic, not a title-specific carve-out: vision
    language accompanying an actual announced action/figure is not routine."""
    title = "Powering the next era of AI infrastructure"
    excerpt = "The company announced a $4 billion agreement to build new data centers with a named partner."
    assert is_corporate_vision_content_without_event(title, excerpt) is False


def test_ordinary_news_without_vision_language_is_unaffected():
    title = "Fed holds rates steady amid market volatility"
    excerpt = "The Federal Reserve held interest rates steady, surprising some market participants."
    assert is_corporate_vision_content_without_event(title, excerpt) is False
