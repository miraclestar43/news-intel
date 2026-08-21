"""2026-08-17 content-quality repair: why_it_matters()/chinese_takeaway()
must be story-specific (name the actual entity/action/amount), never a
bare per-category template."""

from __future__ import annotations

from news_intel.pipeline.render_readiness import chinese_takeaway, is_render_ready, why_it_matters


def _primary(**overrides) -> dict:
    base = {
        "title": "NVIDIA completes $8B acquisition of RunAI",
        "excerpt": "NVIDIA completes its previously announced $8 billion acquisition of RunAI, "
        "an Israeli AI orchestration startup, expanding its software stack.",
    }
    base.update(overrides)
    return base


def test_why_it_matters_names_entity_and_event_type_not_just_category():
    result = why_it_matters(_primary(), None, "AI & Semiconductors", entities=["NVIDIA"])
    assert "NVIDIA" in result
    assert "acquisition" in result.lower()
    generic = (
        "Why it matters: ranks among today's most material developments in "
        "AI/semiconductor infrastructure and competitiveness."
    )
    assert result != generic


def test_why_it_matters_includes_dollar_amount_when_present():
    result = why_it_matters(_primary(), None, "AI & Semiconductors", entities=["NVIDIA"])
    assert "$8" in result or "8" in result


def test_why_it_matters_names_missile_event_type():
    """Matches materiality.HIGH_IMPACT_TERMS' missile/drone/attack addition
    (Aug 15 replay gap) -- the event-type label side of the same fix."""
    primary = {
        "title": "North Korea launches a new ballistic missile over the Sea of Japan",
        "excerpt": "North Korea launched a ballistic missile, prompting an emergency response from Japan.",
    }
    result = why_it_matters(primary, None, "Politics", entities=["North Korea"])
    assert "missile" in result.lower()


def test_chinese_takeaway_names_entity_not_generic_category_template():
    """Reject the exact generic template this repair targets."""
    result = chinese_takeaway("AI & Semiconductors", entities=["NVIDIA"], primary=_primary())
    assert result != "看点：AI/半导体产业格局的重要动态。"
    assert "NVIDIA" in result


def test_chinese_takeaway_without_entities_falls_back_to_category_floor_only():
    """Documented, honest floor: with genuinely no extractable entity/
    event-type signal, the category-only line is still produced (never a
    reader-facing placeholder), but this is the exception, not the norm."""
    result = chinese_takeaway("AI & Semiconductors")
    assert result == "看点：AI/半导体产业格局的重要动态。"


def test_why_it_matters_with_geopolitical_event_names_the_specific_action():
    primary = {
        "title": "Israeli airstrikes hit targets in southern Lebanon amid Hezbollah tension",
        "excerpt": "Israeli airstrikes hit multiple targets in southern Lebanon on Sunday, killing "
        "several people, as tension with Hezbollah escalates despite an earlier ceasefire.",
    }
    result = why_it_matters(primary, None, "Politics", entities=["Lebanon"])
    assert "Lebanon" in result
    assert "ceasefire" in result.lower() or "conflict" in result.lower()


def test_chinese_takeaway_with_geopolitical_event_is_story_specific():
    primary = {
        "title": "Israeli airstrikes hit targets in southern Lebanon amid Hezbollah tension",
        "excerpt": "Israeli airstrikes hit multiple targets in southern Lebanon on Sunday, killing "
        "several people, as tension with Hezbollah escalates despite an earlier ceasefire.",
    }
    result = chinese_takeaway("Politics", entities=["Lebanon"], primary=primary)
    assert result != "看点：国内政策的重要动态。"
    assert "Lebanon" in result


def test_no_dangling_or_truncated_output():
    result = why_it_matters(_primary(), None, "AI & Semiconductors", entities=["NVIDIA"])
    assert not result.rstrip().endswith("...")
    assert not result.rstrip().endswith("…")
    assert result.rstrip().endswith(".")

    zh = chinese_takeaway("AI & Semiconductors", entities=["NVIDIA"], primary=_primary())
    assert not zh.rstrip().endswith("...")
    assert not zh.rstrip().endswith("…")
    assert zh.rstrip().endswith("。")


def test_is_render_ready_unchanged_by_this_repair():
    assert is_render_ready(_primary(), None) is True
    assert is_render_ready({"title": "", "excerpt": None}, None) is False


def test_principal_entity_prefers_earliest_prominent_mention_over_alphabetical():
    """Real defect found via the Aug 15 replay: extract_entities() returns
    entities sorted ALPHABETICALLY, so entities[0] picked "IPO" over
    "NVIDIA" for a story literally about Nvidia. The principal entity must
    be whichever known entity is most prominent (earliest in the title),
    not alphabetically first, and must never be a generic acronym like IPO/
    CEO that is an event-type/role word, not a company/person/country."""
    primary = {
        "title": "Nvidia in talks to invest up to $3B in SoftBank-backed data center developer SB Energy "
        "that aims to IPO soon",
        "excerpt": "Nvidia is in talks to invest as much as $3 billion in SB Energy.",
    }
    result = why_it_matters(primary, None, "AI & Semiconductors", entities=["IPO", "NVIDIA", "SB Energy"])
    assert "NVIDIA" in result
    assert "IPO" not in result.split("Why it matters:")[1].split(" is ")[0].split(" ranks ")[0]


def test_why_it_matters_possessive_phrasing_is_grammatical():
    """Real defect found via the Aug 15 replay: "Xiao Hong's an acquisition"
    -- the leading article must be dropped after a possessive."""
    primary = {
        "title": "CEO Xiao Hong unwinds Manus's $2B acquisition by Meta",
        "excerpt": "Chief executive Xiao Hong plans to return to Singapore as AI company nears "
        "resolution of Meta separation.",
    }
    result = why_it_matters(primary, None, "US / China / World", entities=["Xiao Hong"])
    assert "'s an acquisition" not in result
    assert "'s acquisition" in result


# 2026-08-18 real defect found via the Aug 17 production replay: a company
# name unrecognized by the known-entity vocabulary (e.g. "Crusoe") is
# filtered out by pipeline/entities.py's single-Title-Case-word rule, so
# `entities` contained only a byline/attribution/generic phrase -- the
# rendered TOP story named "Alan Neuhauser" (the journalist) instead of the
# company the story is actually about, or fell all the way through to the
# bare category template.
def test_why_it_matters_names_unrecognized_company_over_byline_attribution():
    primary = {
        "title": "Sources: Crusoe is in IPO talks with at least four Wall Street banks, including "
        "JPMorgan, which is advising on its $3B pre-IPO fundraise set to value it at $35B "
        "(Alan Neuhauser/Axios)",
        "excerpt": "Alan Neuhauser / Axios : Sources: Crusoe is in IPO talks with at least four Wall "
        "Street banks — Crusoe, a data center developer and power supplier, is in IPO talks with at "
        "least four Wall Street banks, Axios Pro has learned.",
    }
    result = why_it_matters(
        primary, None, "AI & Semiconductors", entities=["Alan Neuhauser", "Axios Pro", "IPO", "Wall Street"]
    )
    assert "Crusoe" in result
    assert "Alan Neuhauser" not in result
    assert "$35" in result or "35 billion" in result


def test_why_it_matters_skips_place_based_modifier_to_find_the_real_subject():
    """"Singapore-based Shein" -- the origin modifier must not itself be
    picked as the entity; "Shein" (immediately after it) is the subject."""
    primary = {
        "title": "Sources: Singapore-based Shein is aiming to hit a ~$25B valuation in its Hong Kong "
        "IPO, down from its $30B-$40B goal earlier in August after investor meetings (Reuters)",
        "excerpt": "Reuters : Sources: Singapore-based Shein is aiming to hit a ~$25B valuation — Online "
        "fast-fashion retailer Shein is eyeing a company valuation of around $25 billion in its Hong Kong IPO.",
    }
    result = why_it_matters(primary, None, "US / China / World", entities=["Hong Kong IPO"])
    assert "Shein" in result
    assert "Singapore" not in result.split("Why it matters:")[1].split(" is ")[0].split(" ranks ")[0]


def test_why_it_matters_anchors_on_current_value_not_a_comparison_baseline():
    """Real defect found via the Aug 17 replay: "down from its $30B-$40B
    goal" -- the generated line anchored on $40B (the discarded earlier
    ceiling) instead of the actual current $25B figure. max() over every
    dollar figure in the text is wrong; the figure must be role-aware."""
    primary = {
        "title": "Shein is aiming to hit a ~$25B valuation in its Hong Kong IPO, down from its "
        "$30B-$40B goal earlier in August after investor meetings",
        "excerpt": "Shein is eyeing a company valuation of around $25 billion in its Hong Kong IPO.",
    }
    result = why_it_matters(primary, None, "US / China / World", entities=["Shein"])
    assert "$25" in result or "25 billion" in result
    assert "$40" not in result and "40 billion" not in result


def test_why_it_matters_anchors_on_current_value_with_up_from_phrasing():
    primary = {
        "title": "Anthropic's revenue run rate reached $65B by the end of July, up from $47B in May "
        "and $9B in late 2025",
        "excerpt": "Anthropic PBC is on track to generate annualized revenue of more than $65 billion.",
    }
    result = why_it_matters(primary, None, "AI & Semiconductors", entities=["Anthropic"])
    assert "$65" in result or "65 billion" in result
    assert "$47" not in result and "47 billion" not in result
    assert "$9 billion" not in result


# 2026-08-21: real production incident regression fixtures (Aug 19
# en_global digest, already delivered to Telegram). "Why it matters:
# worth $X" implies a deal size or valuation regardless of what the
# figure actually measures -- these three stories mislabeled a widened
# LOSS and a quarterly REVENUE figure as if they were "worth" something.


def test_why_it_matters_openai_regression_names_the_loss_not_a_generic_worth():
    primary = {
        "title": "OpenAI's Q2 sales grew 18% QoQ to $6.7B as its losses widened from $9.3B to $12.3B; "
        "Anthropic's sales grew 2x+ to $11.6B, with a small operating profit",
        "excerpt": "The ChatGPT-maker's revenue disappointed some investors, although the company told "
        "them its growth accelerated in the third quarter.",
    }
    result = why_it_matters(primary, None, "AI & Semiconductors", entities=["OpenAI"])
    assert "$12.3 billion" in result
    assert "losses" in result
    assert "worth $12.3" not in result
    # 2026-08-21: real production regression -- this exact story (entity
    # extracted, no event-type label matched) hit why_it_matters()'s
    # "if entity:" branch, which independently still read "this
    # development involving X ... ranks among today's most material" even
    # after the amount-role fix above landed. The banned generic template
    # must be gone from every branch, not just the ones these three
    # regression tests happened to already assert on.
    assert "ranks among today's most material" not in result
    assert "this development involving" not in result


def test_why_it_matters_xiaomi_regression_names_revenue_not_generic_worth():
    primary = {
        "title": "Xiaomi reports Q2 revenue down 6.1% YoY to ~$16.2B and net income down 21% to ~$1.4B, "
        "above est., amid a persistent memory shortage and weaker smartphone demand",
        "excerpt": "Xiaomi Corp. posted profit that fell less than expected, raising hopes it can weather "
        "a persistent memory shortage that's weakened demand for smartphones.",
    }
    result = why_it_matters(primary, None, "Markets & Economy", entities=["Xiaomi"])
    assert "$16.2 billion" in result
    assert "revenue" in result
    assert "worth $16.2" not in result
    assert "ranks among today's most material" not in result
    assert "this development involving" not in result


def test_why_it_matters_mercor_regression_names_valuation_not_generic_worth():
    primary = {
        "title": "Nvidia has discussed an investment in Mercor as part of a round valuing the startup "
        "at $20B; Nvidia paid Mercor tens of millions of dollars last quarter",
        "excerpt": "Nvidia has discussed an investment in Mercor, a data labeling provider that helps "
        "the chip designer develop its open-source AI models.",
    }
    result = why_it_matters(primary, None, "AI & Semiconductors", entities=["NVIDIA"])
    assert "$20 billion" in result
    assert "valued at" in result
    assert "worth $20" not in result
    assert "ranks among today's most material" not in result
    assert "this development involving" not in result


def test_why_it_matters_role_phrase_defaults_to_worth_with_no_role_keyword_nearby():
    """Non-regression: when no role keyword is present, the honest floor
    ("worth") remains -- this is a documented floor, not a guess."""
    primary = {
        "title": "A startup announced a new product alongside a $500 million figure with no other context",
        "excerpt": "The company did not elaborate on what the figure represents.",
    }
    result = why_it_matters(primary, None, "AI & Semiconductors", entities=["Acme"])
    assert "worth $500 million" in result


# 2026-08-21: real production incident fixture (Aug 20 en_global digest,
# already delivered) -- a Russian strike on Kyiv killing 17 people
# rendered as "a material development in domestic policy" because the
# world_politics_geopolitics bucket shared the "Politics" display
# category (and its "domestic policy" context) with genuinely domestic
# political stories.
def test_why_it_matters_russia_ukraine_regression_is_world_not_domestic_policy():
    primary = {
        "title": "Russia 'purposefully' hit critical infrastructure in latest strikes, Kyiv mayor says",
        "excerpt": 'At least 17 people were killed in the "massive attack" overnight on Ukraine\'s '
        "capital, as mayor Vitali Klitschko warns hot water supply has been disrupted.",
    }
    result = why_it_matters(primary, None, "World", entities=["Russia"])
    assert "domestic policy" not in result
    assert "international conflict" in result or "global security" in result or "diplomacy" in result


# 2026-08-21: chinese_takeaway() had the same "{entity}相关{context}重要
# 动态" template (matches the "属AI/半导体产业格局重要动态" regression
# named in the Aug 20 instruction) for the entity-only branch, independent
# of why_it_matters()'s English fix above -- and computed its own amount
# via a bare max() rather than reusing the role-aware figure, so it could
# show a different number than the English line for the same story.
def test_chinese_takeaway_openai_regression_has_no_generic_relevant_template():
    primary = {
        "title": "OpenAI's Q2 sales grew 18% QoQ to $6.7B as its losses widened from $9.3B to $12.3B; "
        "Anthropic's sales grew 2x+ to $11.6B, with a small operating profit",
        "excerpt": "The ChatGPT-maker's revenue disappointed some investors, although the company told "
        "them its growth accelerated in the third quarter.",
    }
    result = chinese_takeaway("AI & Semiconductors", entities=["OpenAI"], primary=primary)
    assert "相关" + "AI/半导体产业格局" + "重要动态" not in result
    assert "$12.3 billion" in result
    assert "净亏损" in result


def test_why_it_matters_broadcom_regression_names_debt_financing_not_investment():
    """2026-08-21: real production regression (Aug 20 en_global digest,
    already delivered) -- "raise $60B+ in debt for an AI chip financing
    deal" rendered as "Broadcom an investment of $60 billion" because the
    role-lookback window only saw "raise" before the figure; "in debt" and
    "financing" both land immediately AFTER it in this common phrasing."""
    primary = {
        "title": "Broadcom is in talks with a group of lenders to raise $60B+ in debt for an AI chip "
        "financing deal that will benefit Anthropic and other companies",
        "excerpt": "Broadcom Inc. is in talks with a group of lenders to raise more than $60 billion in "
        "debt for an AI chip financing deal.",
    }
    result = why_it_matters(primary, None, "AI & Semiconductors", entities=["Broadcom"])
    assert "$60 billion" in result
    assert "financing" in result
    assert "an investment of $60" not in result
