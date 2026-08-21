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

# 2026-08-17 Politics/World classification repair (BBC audit: real Lebanon
# strike and Korean-peninsula diplomacy candidates remained Unclassified,
# since no bucket covered general world conflict/diplomacy outside a
# China-specific lens). The exact headline wording below is a
# representative reconstruction of that class of real BBC World story
# (not a verbatim transcript of the audited items, which were not
# preserved verbatim in this repo) -- the fix is general (country/actor
# vocabulary + event-type/consequence gate), never a title-specific
# Lebanon or Korea exception; see pipeline/relevance.py's
# world_politics_geopolitics bucket and materiality.HIGH_IMPACT_TERMS.

def test_lebanon_strike_classifies_as_world_politics():
    result = _classify(
        "Israeli airstrikes hit targets in southern Lebanon, killing several people, as tension with "
        "Hezbollah escalates despite an earlier ceasefire agreement"
    )
    assert result.bucket == "world_politics_geopolitics"


def test_korea_diplomacy_classifies_as_world_politics():
    result = _classify(
        "North Korea and South Korea agree to resume high-level talks in Seoul, in the first formal "
        "negotiation between Pyongyang and the South in over a year"
    )
    assert result.bucket == "world_politics_geopolitics"


def test_world_politics_bucket_maps_to_world_category_label():
    """2026-08-21 repair: world_politics_geopolitics (general international
    conflict/diplomacy) previously shared the "Politics" label with
    routine_politics_general (genuinely domestic stories) -- a real
    production defect, since render_readiness.py's why-it-matters context
    lookup then had no way to distinguish them and rendered a Russian
    strike on Kyiv as "a material development in domestic policy". Split
    into its own "World" category; routine_politics_general stays
    "Politics"."""
    from news_intel.pipeline.scoring import _CATEGORY_LABELS

    assert _CATEGORY_LABELS["world_politics_geopolitics"] == "World"
    assert _CATEGORY_LABELS["routine_politics_general"] == "Politics"


def test_north_korea_missile_launch_classifies_as_world_politics():
    """Real gap found via the Aug 15 replay: conflict-reporting vocabulary
    ("launches a new ballistic missile", "suspected drone attack") had no
    materiality.HIGH_IMPACT_TERMS hit at all under the original narrower
    term list (which only covered "invas"/"war "/"ceasefire"/"coup"), so a
    genuinely material missile story never reached the event-type gate
    despite a clean country/actor vocabulary hit."""
    result = _classify(
        "North Korea launches a new ballistic missile over the Sea of Japan, prompting an emergency "
        "response from South Korea and Japan"
    )
    assert result.bucket == "world_politics_geopolitics"


def test_lebanon_mention_without_event_type_signal_does_not_qualify():
    """The consequence gate: a bare country/actor mention with no
    conflict/diplomacy/event-type signal (e.g. a soft tourism or lifestyle
    story merely set in Lebanon) must still fail to classify as world
    politics -- classification is never automatic from a country name
    alone."""
    result = _classify(
        "A guide to the best beach restaurants in Beirut, Lebanon this summer season"
    )
    assert result.bucket != "world_politics_geopolitics"


def test_korea_sports_story_without_event_type_signal_does_not_qualify():
    result = _classify(
        "South Korea's national football team wins friendly match ahead of Seoul home tournament"
    )
    assert result.bucket != "world_politics_geopolitics"


def test_russia_ukraine_and_major_world_bodies_still_covered():
    """At-minimum coverage check: Russia/Ukraine and European security via
    a general negotiation/military signal (not just the pre-existing
    "invasion"/"missile" geopolitics-group vocabulary)."""
    result = _classify(
        "NATO and European Union leaders hold summit talks on European security after Russia's "
        "military operation near the Ukrainian border escalates further"
    )
    assert result.bucket in ("world_politics_geopolitics", "us_china_policy_geopolitics")


def test_world_politics_classification_is_independent_of_final_selection_materiality():
    """Classification alone must not force digest inclusion -- decision
    value/materiality still separately governs selection (jobs/daily.py's
    _has_material_consequence and daily_selection's own ranking, exercised
    elsewhere); this only asserts that a qualifying world-politics event
    produces a genuinely elevated (not automatically maxed-out) materiality
    score, consistent with how every other bucket is scored."""
    from news_intel.pipeline.materiality import compute_materiality

    text = (
        "Israeli airstrikes hit targets in southern Lebanon, killing several people, as tension with "
        "Hezbollah escalates despite an earlier ceasefire agreement"
    )
    score = compute_materiality(text, "world_politics_geopolitics")
    assert 0.0 < score < 100.0


def test_coverage_tags_are_separate_from_primary_category():
    """Multi-label coverage tags (RelevanceMatch.coverage_tags) are kept
    separate from the single-valued primary display category (`bucket`)."""
    result = _classify(
        "Israeli airstrikes hit targets in southern Lebanon, killing several people, as tension with "
        "Hezbollah escalates despite an earlier ceasefire agreement"
    )
    assert result.bucket == "world_politics_geopolitics"
    assert isinstance(result.coverage_tags, list)
    assert result.bucket in result.coverage_tags


# 2026-08-21 repair: "中国"/"China" (and "北京"/"Beijing") are the ISSUING-
# AUTHORITY/company-name-component letterhead on nearly every China-
# related item regardless of subject -- real defect found via a live Aug
# 19 zh_cn replay, a 100% domestic battery-supply story between two
# Chinese companies (BYD's FDI/弗迪 unit and FAW/一汽) classified as
# "US / China / World" purely because the text said "中国一汽" (FAW's own
# Chinese name literally contains "中国"). Also fixes the same class of
# defect for English ("China" alone). Regression cases per the six
# categories explicitly required: domestic Chinese company cooperation,
# international expansion, export restrictions, US-China policy,
# geopolitical events, Chinese government policy.


def test_domestic_chinese_company_cooperation_is_not_us_china_world():
    text = (
        "一汽弗迪新能源动力电池项目由中国一汽与比亚迪共同打造，正在推进弗迪二代刀片电池的导入适配，"
        "即将实现兆瓦闪充技术在中国一汽车型上的首次搭载。"
    )
    result = _classify(text)
    assert result.bucket != "us_china_policy_geopolitics"


def test_domestic_english_company_story_mentioning_china_alone_is_not_us_china_world():
    """General, not zh-specific: the same over-broad "China" mention issue
    applies to English text too."""
    text = "A Chinese domestic supplier signed a new manufacturing contract with a China-based automaker."
    result = _classify(text)
    assert result.bucket != "us_china_policy_geopolitics"


def test_international_expansion_still_classifies_as_us_china_world():
    text = (
        "China announced plans for a new automaker to expand into the Singapore market, "
        "competing with Japanese rivals."
    )
    result = _classify(text)
    assert result.bucket == "us_china_policy_geopolitics"


def test_export_restrictions_still_classify_as_us_china_world():
    """Note: a genuinely semiconductor-flavored export-control story (the
    overwhelmingly common real-world case) correctly classifies as
    ai_semiconductor_export_controls via the earlier-priority policy
    group -- unaffected by this fix, and arguably more accurate in
    practice. This case uses a non-semiconductor sanctions scenario to
    isolate the china-gate behavior specifically."""
    text = "The US imposed new sanctions on several state-owned shipping companies in China."
    result = _classify(text)
    assert result.bucket == "us_china_policy_geopolitics"


def test_us_china_trade_policy_still_classifies_correctly():
    result = _classify("US says dozens of countries helped China dodge Trump's tariffs")
    assert result.bucket == "us_china_policy_geopolitics"


def test_geopolitical_event_involving_china_still_classifies_correctly():
    text = "China and the United States held high-level diplomatic talks in Washington on regional security."
    result = _classify(text)
    assert result.bucket == "us_china_policy_geopolitics"


def test_chinese_government_domestic_policy_alone_is_not_us_china_world():
    """Chinese government policy with no cross-border angle at all -- e.g.
    Beijing issuing a purely domestic administrative notice -- should not
    win the international bucket merely by saying "China"/"Beijing"."""
    text = "Beijing issued new local rules for municipal waste sorting, effective next month."
    result = _classify(text)
    assert result.bucket != "us_china_policy_geopolitics"


def test_taiwan_hong_kong_xi_jinping_remain_sufficient_alone():
    """Non-regression: these china-group terms are specific enough that
    they still qualify without an additional cross-border signal."""
    assert _classify("Taiwan's government responded to the latest developments today.").bucket == (
        "us_china_policy_geopolitics"
    )
    assert _classify("Hong Kong authorities announced a new policy today.").bucket == (
        "us_china_policy_geopolitics"
    )
