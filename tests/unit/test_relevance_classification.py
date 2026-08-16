"""Gate A remediation Phase 3.1-3.3: AI vocabulary recognition with real
token/phrase boundaries, an honest "unclassified" default instead of a
silent "World" catch-all, and contextual (not bare-word) tariff matching.

The NAGPRA and "Electronic Tariff Filings" wording below reproduces the
actual Gate A false-positive examples as regression fixtures only -- none
of this wording is hardcoded into production logic (topics.yaml is entirely
config-driven; see config/topics.yaml's trade_policy_context and the
generic word-boundary matcher in pipeline/relevance.py)."""

from __future__ import annotations

from news_intel.pipeline.relevance import UNCLASSIFIED_BUCKET, classify_relevance
from news_intel.settings import load_config

CONFIG = load_config()
TOPICS = CONFIG.topics
MULTIPLIERS = CONFIG.policy.relevance_multipliers


def _classify(text: str):
    return classify_relevance(text, TOPICS, MULTIPLIERS)


def test_chatgpt_and_gpt_classify_as_ai_semiconductor():
    result = _classify("OpenAI released GPT-5, its newest ChatGPT model, to enterprise customers")
    assert result.bucket == "ai_semiconductor_export_controls"


def test_generative_ai_and_llm_phrases_classify_as_ai_semiconductor():
    assert _classify("A new generative AI startup raised funding for its platform").bucket == (
        "ai_semiconductor_export_controls"
    )
    assert _classify("The company's large language model beat internal benchmarks").bucket == (
        "ai_semiconductor_export_controls"
    )


def test_gpt_does_not_match_as_a_bare_substring_inside_another_word():
    """Word-boundary matching: "gpt" must not fire just because it appears
    as a substring inside an unrelated token (e.g. a ticker or SKU)."""
    result = _classify("Regional utility files SGPTX-9 rate schedule with the state commission")
    assert result.bucket != "ai_semiconductor_export_controls"


def test_nagpra_style_federal_register_notice_is_honestly_unclassified():
    """Reproduces the Gate A false positive: a Federal Register notice about
    Native American Graves Protection and Repatriation Act (NAGPRA)
    inventory completion has no AI/semiconductor/markets/policy vocabulary
    at all and must not be silently bucketed as "World" with full relevance
    weight."""
    text = (
        "Notice of Inventory Completion: National Park Service has completed an "
        "inventory of human remains under the Native American Graves Protection "
        "and Repatriation Act (NAGPRA)"
    )
    result = _classify(text)
    assert result.bucket == UNCLASSIFIED_BUCKET
    assert result.multiplier == 0.0


def test_bare_tariff_word_alone_has_no_contextual_evidence():
    result = _classify("Company announces new tariff plan for customers")
    assert result.bucket != "ai_semiconductor_export_controls"
    assert result.bucket == UNCLASSIFIED_BUCKET


def test_electronic_tariff_filing_is_excluded_even_with_nearby_context_words():
    """Reproduces the Gate A false positive: an FCC/utility "Electronic
    Tariff Filings" administrative notice must never qualify as an
    AI/semiconductor trade-policy story just because the word "tariff"
    appears."""
    text = "Electronic Tariff Filings docket update: carriers submitted revised import duty schedules"
    result = _classify(text)
    assert result.bucket != "ai_semiconductor_export_controls"


def test_tariffs_with_semiconductor_context_classify_as_ai_semiconductor():
    result = _classify("Section 301 tariffs target imports of Chinese semiconductor manufacturing equipment")
    assert result.bucket == "ai_semiconductor_export_controls"


def test_tariffs_with_generic_trade_context_classify_as_world_policy_not_ai():
    result = _classify("New tariffs on steel and aluminum imports take effect after trade war escalation")
    assert result.bucket == "us_china_policy_geopolitics"


def test_legal_notice_boilerplate_is_noise():
    text = (
        "ROSEN, A TOP RANKED LAW FIRM, Encourages Example Corp Investors to Secure "
        "Counsel Before Important Deadline in Securities Fraud Class Action -- "
        "if you purchased shares of Example Corp between January and March"
    )
    result = _classify(text)
    assert result.bucket == "explicit_noise"


def test_legal_notice_with_material_override_is_not_forced_to_noise():
    text = (
        "Shareholder rights firm law firm investigates Example Corp after the company "
        "disclosed a material weakness in its internal controls and restated financial results"
    )
    result = _classify(text)
    assert result.bucket != "explicit_noise"


def test_noise_exclusion_terms_use_word_boundaries_not_bare_substring():
    """2026-08-12 defect: "sale" is a legitimate noise_exclusions term (for
    retail-spam headlines like "flash sale"), but the check was a bare
    substring test that also matched "sales" -- a Nebius earnings report
    ("second-quarter sales") was silently zeroed out (noise_penalty=100,
    final_score=0) as a result. A genuine "sale" headline must still be
    caught; the "sales" false positive must not be."""
    genuine_noise = _classify("Huge flash sale this weekend on all electronics")
    assert genuine_noise.bucket == "explicit_noise"

    earnings_report = _classify(
        "Amsterdam-based neocloud Nebius reports Q2 cloud revenue up 514% YoY. "
        "Nebius Group NV reported a 514% jump in second-quarter sales for its cloud business "
        "following strong demand for AI computing power."
    )
    assert earnings_report.bucket != "explicit_noise"


def test_nebius_classifies_as_ai_semiconductor():
    """2026-08-12 diagnosis: Nebius (a major AI-infrastructure "neocloud")
    was outside the AI vocabulary entirely, so even after the noise-filter
    fix it would still have fallen to "unclassified" and been excluded."""
    result = _classify(
        "Amsterdam-based neocloud Nebius reports Q2 cloud revenue up 514% YoY to $575M, "
        "above estimates, following strong demand for AI computing power."
    )
    assert result.bucket == "ai_semiconductor_export_controls"


def test_china_group_has_english_vocabulary_coverage():
    """2026-08-14 fix: the "china" group previously had zero terms_en,
    despite feeding the same us_china_policy_geopolitics bucket as the
    geopolitics group -- English-language China/US-China coverage (BBC,
    Reuters-via-Techmeme, etc.) had no vocabulary coverage at all. Real
    Aug 13 reproduction: a genuine US-China trade-policy story classified
    as unclassified and was excluded before qualification."""
    result = _classify("US says dozens of countries helped China dodge Trump's tariffs")
    assert result.bucket == "us_china_policy_geopolitics"


def test_title_only_ai_mention_in_background_does_not_hijack_markets_category():
    """2026-08-14 defect reproduction: an Amazon/Alphabet "other income"
    earnings-quality analysis was classified AI & Semiconductors purely
    because "Anthropic" appeared once, in a background clause about what
    drove the investment gains -- not because the story is about Anthropic
    or AI. The title itself is a corporate financial-report story and must
    win once title-level vocabulary (see config/topics.yaml's macro_markets
    additions) can classify it on its own."""
    title = (
        'In Q2, "other income", mostly from investment gains, at Amazon and Alphabet totaled ~$121B '
        "after taxes and made up 66% and 71%, respectively, of profits"
    )
    excerpt = (
        title + " — Gains on investments in companies like Anthropic are boosting earnings at some of "
        "the biggest technology companies"
    )
    combined = f"{title} {excerpt}"
    result = classify_relevance(combined, TOPICS, MULTIPLIERS, title=title)
    assert result.bucket == "markets_macro_corporate"


def test_title_level_ai_vocabulary_still_wins_when_title_is_genuinely_about_ai():
    """The title-priority fix must not suppress a genuine AI story just
    because it also touches on markets/earnings language -- when the title
    itself carries AI-semiconductor vocabulary, that bucket still wins
    (unchanged from prior behavior; ai_semiconductor_export_controls is
    still checked ahead of markets_macro_corporate in _BUCKET_PRIORITY)."""
    title = "NVIDIA reports record quarterly revenue and profits on AI chip demand"
    result = _classify(title)
    assert result.bucket == "ai_semiconductor_export_controls"


def test_title_with_no_vocabulary_falls_back_to_full_text_classification():
    """When the title alone has no vocabulary match at all, classification
    falls back to the full combined text exactly as before the title-
    priority fix (2026-08-12's Cerebras-style case: the title is generic
    but the excerpt carries the real classification signal)."""
    title = "Cerebras reports Q2 revenue up 74% YoY to $180M and raises its forecast"
    excerpt = (
        "Cerebras Systems raised its annual revenue and gross margin forecasts on Wednesday, buoyed by "
        "robust demand for its chips from companies ramping up data-center capacity."
    )
    result = classify_relevance(f"{title} {excerpt}", TOPICS, MULTIPLIERS, title=title)
    assert result.bucket != UNCLASSIFIED_BUCKET



def test_bare_chips_word_does_not_match_chips_act_acronym():
    """2026-08-16 defect (found during the Aug 15 BBC World audit): a
    Taliban/Afghanistan feature mentioning "does the West have any
    bargaining chips left" was classified AI & Semiconductors purely
    because "chips" matched the CHIPS Act policy vocabulary term
    case-insensitively -- classify_relevance's word-boundary matcher is
    always case-insensitive, so a bare acronym that collides with an
    ordinary English word needs more context, same as bare "tariff"."""
    result = _classify(
        "Five years after the Taliban swept to power, does the West have any influence? "
        "many are asking whether the West has any bargaining chips left"
    )
    assert result.bucket == UNCLASSIFIED_BUCKET


def test_chips_act_phrase_still_classifies_as_ai_semiconductor():
    result = _classify("Congress debates funding levels for the CHIPS Act semiconductor subsidy program")
    assert result.bucket == "ai_semiconductor_export_controls"