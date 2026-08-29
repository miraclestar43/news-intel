"""2026-08-11 content-quality repair: story-family deduplication, coverage-
aware selection with AI/source caps (pipeline/daily_selection.py), and the
clickable-link/summary-quality renderer (delivery/daily_production_formatter.py).
Reproduces the exact 2026-08-10 digest defects (Daybreak duplication, all-AI
selection, plain-text source labels, ellipsis-truncated summary) as focused
regressions."""

from __future__ import annotations

from news_intel.delivery.daily_production_formatter import (
    render_production_daily,
    summarize,
)
from news_intel.pipeline.daily_selection import (
    MAX_AI_STORIES,
    SelectionResult,
    Story,
    group_story_families,
    select_daily_stories,
)

_GENERIC_TERMS = {"OpenAI", "ChatGPT", "NVIDIA", "AI", "GPT"}


def _item(event_id, title, category, score, *, domain="example.test", excerpt="A complete sentence here.",
          published_at="2026-08-10T10:00:00+00:00", publisher="Test Publisher", entities=None):
    return {
        "event_id": event_id, "title": title, "category": category, "final_score": score,
        "excerpt": excerpt, "confirmation_state": "official", "entities": entities or [],
        "sources": [{"publisher": publisher, "url": f"https://{domain}/{event_id}", "published_at": published_at}],
    }


# --- story-family deduplication ---------------------------------------------


def test_daybreak_articles_reproduce_as_one_story_cluster():
    """The exact 2026-08-10 defect: two openai.com posts, same Pacific day,
    different titles/wording, both about the Daybreak program -- must
    become one digest slot, not two TOP slots."""
    a = _item(
        "E-0301", "Expanding Daybreak as the Cyber Defense Window Narrows", "AI & Semiconductors", 73.99,
        domain="openai.com", publisher="OpenAI News",
        excerpt="Meet GPT-5.6-Cyber, OpenAI's cybersecurity-specific model available through Daybreak Red.",
    )
    b = _item(
        "E-0296", "Putting frontier cyber models in more trusted hands", "AI & Semiconductors", 73.99,
        domain="openai.com", publisher="OpenAI News",
        excerpt="Approved Daybreak partners can use OpenAI's frontier cyber models to deliver services.",
    )
    families = group_story_families([a, b], generic_terms=_GENERIC_TERMS)
    assert len(families) == 1
    assert families[0].primary["event_id"] == "E-0301"
    assert families[0].secondary["event_id"] == "E-0296"


def test_unrelated_same_day_same_domain_posts_are_not_merged():
    a = _item("E-1", "OpenAI announces new model release", "AI & Semiconductors", 70, domain="openai.com")
    b = _item("E-2", "OpenAI opens a new office in Tokyo", "AI & Semiconductors", 65, domain="openai.com")
    families = group_story_families([a, b], generic_terms=_GENERIC_TERMS)
    assert len(families) == 2  # no shared distinctive token beyond generic "OpenAI"


def test_cross_publisher_same_event_is_merged():
    """2026-08-12 repair: the same underlying announcement, reported by two
    different publishers, must be recognized as one story -- the old
    same-domain requirement made this impossible. Verified against real
    2026-08-10 data too (nvidianews.nvidia.com's own release vs
    techmeme.com's Financial-Times-sourced report of the identical $500B
    financing deal)."""
    a = _item(
        "E-1", "OpenAI announces Daybreak Program expansion", "AI & Semiconductors", 70, domain="openai.com",
        entities=["Daybreak Program", "OpenAI"],
    )
    b = _item(
        "E-2", "Analyst reviews Daybreak Program impact", "AI & Semiconductors", 65, domain="techmeme.com",
        publisher="Techmeme", entities=["Daybreak Program"],
    )
    families = group_story_families([a, b], generic_terms=_GENERIC_TERMS)
    assert len(families) == 1
    assert families[0].primary["event_id"] == "E-1"
    assert families[0].secondary["event_id"] == "E-2"


def test_same_publisher_different_products_not_merged():
    """2026-08-12 repair reproduction: two unrelated posts from the SAME
    publisher (both mentioning only the company name, no shared specific
    product/entity) must stay separate -- the 2026-08-11 defect that merged
    NVIDIA's AI-financing story with an unrelated Nemotron release."""
    a = _item(
        "E-1", "NVIDIA Partners With Apollo and KKR on Financing", "AI & Semiconductors", 74,
        domain="blogs.nvidia.com", publisher="NVIDIA newsroom",
        excerpt="NVIDIA announced financing platforms with Apollo, BlackRock and KKR.",
    )
    b = _item(
        "E-2", "NVIDIA Nemotron 3.5 Lightning Delivers Fast Execution", "AI & Semiconductors", 74,
        domain="developer.nvidia.com", publisher="NVIDIA developer blog",
        excerpt="NVIDIA Nemotron 3.5 Lightning is a new open model for agentic workloads.",
    )
    families = group_story_families([a, b], generic_terms=_GENERIC_TERMS)
    assert len(families) == 2


def test_selection_selects_most_informative_as_primary_and_keeps_secondary():
    a = _item("E-lo", "Short headline", "AI & Semiconductors", 60, domain="openai.com",
              excerpt="Shared Daybreak news, brief version.")
    b = _item("E-hi", "A longer, more informative Daybreak headline", "AI & Semiconductors", 90, domain="openai.com",
              excerpt="Shared Daybreak news, the fuller and more informative version of events.")
    result = select_daily_stories([b, a], generic_terms=_GENERIC_TERMS)
    all_stories = result.all_stories
    assert len(all_stories) == 1
    assert all_stories[0].primary["event_id"] == "E-hi"  # higher-scoring member is primary
    assert all_stories[0].secondary["event_id"] == "E-lo"


# --- coverage-aware selection ------------------------------------------------


def test_markets_and_politics_selected_even_when_ai_candidates_score_higher():
    candidates = [
        _item(f"E-AI-{i}", f"AI story {i}", "AI & Semiconductors", 90 - i, domain=f"ai{i}.test")
        for i in range(5)
    ] + [
        _item("E-MKT", "Fed holds rates steady", "Markets & Economy", 60, domain="fed.gov"),
        _item("E-POL", "Senate passes new regulation bill", "Politics", 58, domain="senate.gov"),
    ]
    result = select_daily_stories(candidates, generic_terms=_GENERIC_TERMS)
    categories = {s.category for s in result.all_stories}
    assert "Markets & Economy" in categories
    assert "Politics" in categories
    ai_count = sum(1 for s in result.all_stories if s.category == "AI & Semiconductors")
    assert ai_count <= MAX_AI_STORIES


def test_china_us_china_selected_when_material_candidate_exists():
    candidates = [
        _item("E-AI", "AI story", "AI & Semiconductors", 90, domain="ai.test"),
        _item("E-CHINA", "US sanctions Chinese chip exporter", "US / China / World", 55, domain="state.gov"),
    ]
    result = select_daily_stories(candidates, generic_terms=_GENERIC_TERMS)
    categories = {s.category for s in result.all_stories}
    assert "US / China / World" in categories


def test_no_padding_when_category_has_no_qualified_candidate():
    """Only AI candidates exist -- Markets/Politics/China must stay
    unfilled, never padded with a weak or unrelated substitute."""
    candidates = [_item(f"E-{i}", f"AI story {i}", "AI & Semiconductors", 90 - i, domain=f"ai{i}.test")
                  for i in range(3)]
    result = select_daily_stories(candidates, generic_terms=_GENERIC_TERMS)
    categories = {s.category for s in result.all_stories}
    assert categories == {"AI & Semiconductors"}
    assert len(result.all_stories) <= MAX_AI_STORIES  # capped, not padded to fill positions


# --- caps ---------------------------------------------------------------


def test_ai_stories_capped_at_two_across_whole_digest():
    candidates = [_item(f"E-{i}", f"AI story {i}", "AI & Semiconductors", 90 - i, domain=f"ai{i}.test")
                  for i in range(6)]
    result = select_daily_stories(candidates, generic_terms=_GENERIC_TERMS)
    ai_count = sum(1 for s in result.all_stories if s.category == "AI & Semiconductors")
    assert ai_count == MAX_AI_STORIES


def test_single_domain_capped_at_two_total_stories():
    candidates = [
        _item("E-1", "Story one", "AI & Semiconductors", 90, domain="dominant.test"),
        _item("E-2", "Story two", "Markets & Economy", 85, domain="dominant.test"),
        _item("E-3", "Story three", "Politics", 80, domain="dominant.test"),
        _item("E-4", "Story four", "US / China / World", 75, domain="other.test"),
    ]
    result = select_daily_stories(candidates, generic_terms=_GENERIC_TERMS)
    dominant_count = sum(1 for s in result.all_stories if s.domain == "dominant.test")
    assert dominant_count <= 2


def test_at_most_one_domain_in_top_stories():
    candidates = [
        _item("E-1", "Alpha headline", "AI & Semiconductors", 90, domain="dominant.test"),
        _item("E-2", "Bravo headline", "Markets & Economy", 89, domain="dominant.test"),
        _item("E-3", "Charlie headline", "Politics", 70, domain="other.test"),
        _item("E-4", "Delta headline", "US / China / World", 60, domain="fourth.test"),
    ]
    result = select_daily_stories(candidates, generic_terms=_GENERIC_TERMS)
    top_domains = [s.domain for s in result.top_stories]
    assert len(top_domains) == len(set(top_domains))  # no domain repeats within TOP
    assert "fourth.test" in [s.domain for s in result.all_stories]  # the swapped-in story is still included


# --- hard publisher-family-in-TOP rule (2026-08-13) -------------------------


def test_second_same_family_story_demotes_to_more_to_know_when_no_swap_needed():
    """2026-08-12 defect reproduction: two Techmeme-discovered stories both
    qualified and both would have been TOP by score alone -- the second
    must move to MORE TO KNOW, never occupy TOP alongside the first."""
    candidates = [
        _item("E-1", "Cerebras reports quarterly results", "AI & Semiconductors", 90, domain="techmeme.com",
              publisher="Reuters"),
        _item("E-2", "SpaceXAI releases a new model", "AI & Semiconductors", 89, domain="techmeme.com",
              publisher="xAI"),
    ]
    result = select_daily_stories(candidates, generic_terms=_GENERIC_TERMS)
    assert len(result.top_stories) == 1
    assert result.top_stories[0].event_id == "E-1"
    assert len(result.more_to_know) == 1
    assert result.more_to_know[0].event_id == "E-2"


def test_top_may_have_fewer_than_three_stories_rather_than_violate_family_cap():
    """Only two candidates total, both same publisher family -- TOP must
    end up with exactly one story, never padded with the second."""
    candidates = [
        _item("E-1", "Cerebras reports quarterly revenue growth", "AI & Semiconductors", 90, domain="techmeme.com",
              publisher="Reuters"),
        _item("E-2", "Grok 4.6 model pricing announced by xAI", "AI & Semiconductors", 85, domain="techmeme.com",
              publisher="Bloomberg"),
    ]
    result = select_daily_stories(candidates, generic_terms=_GENERIC_TERMS)
    assert len(result.top_stories) == 1
    assert len(result.more_to_know) == 1  # still shown, just not in TOP


# --- TOP requires a complete source-supported summary (2026-08-13) ----------


def test_render_readiness_gate_drops_missing_summary_and_promotes_next():
    """2026-08-16 repair: a candidate with no complete-sentence excerpt
    (enrichment attempted -- daily_selection.Story.render_ready tries the
    primary source, exactly what happened here) must not occupy a TOP slot
    at all -- and, per the 2026-08-15 regression (three render-failed
    candidates left TOP entirely empty while still occupying MORE TO
    KNOW), it must not be silently demoted into MORE TO KNOW either. It is
    dropped from delivery entirely; E-2, the next render-ready candidate,
    is promoted into TOP."""
    candidates = [
        _item("E-1", "Headline with no supporting sentence", "AI & Semiconductors", 95, domain="a.test",
              excerpt="Headline with no supporting sentence"),  # no ending punctuation -> no complete sentence
        _item("E-2", "Second story with a real summary", "AI & Semiconductors", 80, domain="b.test",
              excerpt="This is a distinct, complete supporting sentence."),
    ]
    result = select_daily_stories(candidates, generic_terms=_GENERIC_TERMS)
    top_ids = [s.event_id for s in result.top_stories]
    more_ids = [s.event_id for s in result.more_to_know]
    assert top_ids == ["E-2"]
    assert "E-1" not in more_ids  # dropped, not silently demoted
    assert "E-1" not in top_ids


def test_render_ready_backfill_replaces_dropped_candidate_not_just_next_lower_family():
    """The render-readiness gate runs on the whole `selected` list before
    the TOP/MORE split, so decision-value ranking itself (which family
    wins a coverage/fill slot) is entirely unaffected by summary
    availability, matching "summary availability must not change the
    initial decision-value ordering before enrichment has been attempted."
    Here E-1 (highest score, no summary) still wins the AI-cap fill
    competition over E-3 (a much lower-score family) -- it just can't
    survive the render-readiness gate, and E-3 (render-ready, and now
    validly addable once E-1's AI-cap slot frees up) backfills in its
    place rather than E-1 being kept in MORE regardless of its missing
    summary."""
    candidates = [
        _item("E-1", "Headline with no supporting sentence", "AI & Semiconductors", 95, domain="a.test",
              excerpt="Headline with no supporting sentence"),
        _item("E-2", "Second story with a real summary", "AI & Semiconductors", 80, domain="b.test",
              excerpt="This is a distinct, complete supporting sentence."),
        _item("E-3", "Third, much lower-value story", "AI & Semiconductors", 10, domain="c.test",
              excerpt="This is also a distinct, complete supporting sentence."),
    ]
    result = select_daily_stories(candidates, generic_terms=_GENERIC_TERMS)
    all_ids = {s.event_id for s in result.all_stories}
    assert all_ids == {"E-2", "E-3"}  # E-1 dropped for failing render-readiness, not kept regardless
    assert [s.event_id for s in result.top_stories] == ["E-2", "E-3"]
    assert result.more_to_know == []


def test_more_to_know_never_populated_while_top_is_empty():
    """Explicit invariant (2026-08-16): TOP must never be empty while MORE
    TO KNOW is non-empty. When every candidate fails render-readiness,
    both sections are empty -- underfilling, not inversion."""
    candidates = [
        _item("E-1", "Headline with no supporting sentence", "AI & Semiconductors", 95, domain="a.test",
              excerpt="Headline with no supporting sentence"),
        _item("E-2", "Another headline with no supporting sentence", "Markets & Economy", 90, domain="b.test",
              excerpt="Another headline with no supporting sentence"),
    ]
    result = select_daily_stories(candidates, generic_terms=_GENERIC_TERMS)
    assert result.top_stories == []
    assert result.more_to_know == []


def test_top_gets_min_three_selected_count_render_ready_events():
    """Explicit invariant (2026-08-16): the highest-ranked min(3,
    selected_count) render-ready events belong in TOP. With five
    render-ready, distinct-domain AI candidates selected, TOP holds
    exactly 3 (MAX_TOP_STORIES) and the remaining 2 land in MORE."""
    candidates = [
        _item(f"E-{i}", f"Distinct headline number {i} for this story", "Technology", 90 - i,
              domain=f"domain{i}.test", excerpt=f"This is a distinct, complete supporting sentence number {i}.")
        for i in range(5)
    ]
    result = select_daily_stories(candidates, generic_terms=_GENERIC_TERMS, max_total=8)
    assert len(result.top_stories) == 3
    assert [s.event_id for s in result.top_stories] == ["E-0", "E-1", "E-2"]


# --- clickable links / safe escaping ----------------------------------------


def test_render_produces_clickable_title_and_source_links():
    story = Story(primary=_item("E-1", "A headline", "AI & Semiconductors", 90, domain="example.com",
                                 publisher="Example Pub"))
    rendered = render_production_daily(SelectionResult(top_stories=[story]), "Aug 10", [])
    assert '<a href="https://example.com/E-1">A headline</a>' in rendered
    assert '<a href="https://example.com/E-1">Example Pub</a>' in rendered
    assert "Source:" in rendered


def test_render_shows_up_to_two_linked_sources_for_a_family():
    primary = _item("E-1", "Primary headline", "AI & Semiconductors", 90, domain="openai.com",
                     publisher="OpenAI News")
    secondary = _item("E-2", "Secondary headline", "AI & Semiconductors", 80, domain="openai.com",
                       publisher="OpenAI News")
    story = Story(primary=primary, secondary=secondary)
    rendered = render_production_daily(SelectionResult(top_stories=[story]), "Aug 10", [])
    assert rendered.count('href="https://openai.com/E-1"') == 2  # title link + source link
    assert 'href="https://openai.com/E-2"' in rendered  # secondary source also linked
    # 2026-08-23 repair: real production regression -- both links used to
    # render with IDENTICAL display text ("Source: OpenAI News, OpenAI
    # News"), giving a reader no way to tell them apart even though the
    # hrefs differ. The second must be disambiguated, not dropped.
    assert "OpenAI News (related coverage)" in rendered
    assert rendered.count("OpenAI News</a>") == 1  # bare label appears exactly once (title-link text differs)


def test_html_special_characters_are_escaped_safely():
    story = Story(primary=_item(
        "E-1", "AT&T <partners> with \"Vendor\" & others", "Markets & Economy", 90, domain="att.com",
        excerpt="AT&T reported strong results.",
    ))
    rendered = render_production_daily(SelectionResult(top_stories=[story]), "Aug 10", [])
    assert "AT&amp;T" in rendered
    assert "&lt;partners&gt;" in rendered
    assert "<partners>" not in rendered
    # the anchor tags themselves must remain well-formed (not escaped)
    assert '<a href="https://att.com/E-1">' in rendered


def test_tomorrow_to_watch_links_when_url_available():
    result = SelectionResult()
    rendered = render_production_daily(
        result, "Aug 10", [{"title": "CPI release", "url": "https://bls.gov/cpi"}]
    )
    assert '<a href="https://bls.gov/cpi">CPI release</a>' in rendered


def test_tomorrow_to_watch_plain_text_when_no_url():
    result = SelectionResult()
    rendered = render_production_daily(result, "Aug 10", [{"title": "Real Earnings", "url": None}])
    assert "Real Earnings" in rendered
    assert "<a href=" not in rendered.split("TOMORROW TO WATCH")[1]


# --- summary quality: ellipsis / redundancy ---------------------------------


def test_summary_rejects_trailing_three_dots():
    excerpt = "NVIDIA announced strategic partnerships to mobilize over $500 ..."
    assert summarize(excerpt, "NVIDIA Partners announcement") is None


def test_summary_rejects_trailing_unicode_ellipsis():
    excerpt = "NVIDIA announced strategic partnerships to mobilize over $500 …"
    assert summarize(excerpt, "NVIDIA Partners announcement") is None


def test_summary_uses_earlier_complete_sentence_before_ellipsis():
    excerpt = "NVIDIA announced a new partnership today. More details are forthcoming ..."
    result = summarize(excerpt, "NVIDIA partnership headline")
    assert result == "NVIDIA announced a new partnership today."


def test_summary_fallback_clause_used_when_no_terminal_punctuation_at_all():
    """2026-08-16 repair, exact Aug 15 Manus reproduction: a real, genuine
    clause with no [.!?] anywhere (Techmeme's own formatting) is real,
    unaltered source content, not a truncated fragment -- the strict
    sentence-punctuation rule previously rejected this outright, leaving
    every TOP slot unfillable."""
    excerpt = (
        "Zijing Wu / Financial Times : Sources: Some restated headline here — Chief executive "
        "Xiao Hong plans to return to Singapore as AI company nears resolution of Meta separation"
    )
    result = summarize(excerpt, "China plans to lift a travel ban on Manus founders")
    assert result == (
        "Chief executive Xiao Hong plans to return to Singapore as AI company nears "
        "resolution of Meta separation."
    )


def test_summary_fallback_clause_strips_ellipsis_but_keeps_the_clause():
    """Exact Aug 15 Nvidia/SB Energy reproduction: the excerpt trails off
    behind an explicit '…' truncation marker, but the clause up to that
    point is genuine, substantive source text -- used verbatim with the
    truncation marker itself dropped, not the words before it."""
    excerpt = (
        "The Information : Sources: restated headline — Nvidia is in talks to invest as much as "
        "$3 billion in SB Energy, the Softbank Group-backed developer of a massive planned Ohio …"
    )
    result = summarize(excerpt, "Nvidia in talks to invest in SB Energy")
    assert result == (
        "Nvidia is in talks to invest as much as $3 billion in SB Energy, the Softbank "
        "Group-backed developer of a massive planned Ohio."
    )


def test_summary_fallback_clause_still_rejects_dangling_dollar_figure():
    """The fallback relaxation must not resurrect the original 2026-08-10
    defect: a truncation that cuts off immediately after a bare dollar
    amount hides the unit (million/billion/trillion) and is categorically
    misleading, not just inelegant -- still rejected even under the
    relaxed no-terminal-punctuation fallback."""
    excerpt = "NVIDIA announced strategic partnerships to mobilize over $500 ..."
    assert summarize(excerpt, "NVIDIA Partners announcement") is None


def test_summary_fallback_clause_rejects_short_remainder():
    excerpt = "Publication : Restated headline text goes here — OK"
    assert summarize(excerpt, "Restated headline text goes here") is None


def test_summary_omitted_when_title_redundant():
    title = "NVIDIA announces new partnership with major banks"
    excerpt = "NVIDIA announces new partnership with major banks."
    assert summarize(excerpt, title) is None


def test_summary_uses_distinct_sentence_when_first_is_redundant_with_title():
    title = "NVIDIA announces new partnership with major banks"
    excerpt = "NVIDIA announces new partnership with major banks. The deal is worth billions in financing."
    result = summarize(excerpt, title)
    assert result == "The deal is worth billions in financing."


def test_reproduces_nvidia_ellipsis_defect_end_to_end():
    """The exact 2026-08-10 NVIDIA item: excerpt truncated with a trailing
    '...' and no earlier complete sentence -- summary must be omitted
    entirely (title/category/source still shown), never rendered as a
    mid-sentence fragment."""
    excerpt = (
        "NVIDIA today announced strategic partnerships to establish independent compute "
        "financing platforms with Apollo, BlackRock, Blackstone, Brookfield, Goldman Sachs "
        "and KKR to mobilize over $500 ..."
    )
    story = Story(primary=_item(
        "E-1", "NVIDIA Partners With Apollo and Others", "AI & Semiconductors", 90,
        domain="nvidianews.nvidia.com", publisher="NVIDIA newsroom", excerpt=excerpt,
    ))
    rendered = render_production_daily(SelectionResult(top_stories=[story]), "Aug 10", [])
    assert "mobilize over" not in rendered
    assert "..." not in rendered
    assert "NVIDIA Partners With Apollo and Others" in rendered
    assert "Category: AI &amp; Semiconductors" in rendered


# --- summary quality: abbreviation periods and dangling clauses ------------
# 2026-08-21: real production incident regression fixtures (Aug 20
# en_global digest, already delivered to Telegram).


def test_summary_broadcom_regression_full_clause_not_bare_entity_fragment():
    """Real defect: "Broadcom Inc." was rendered as the entire summary --
    _SENTENCE_END_RE treated the period in the "Inc." abbreviation as a
    genuine sentence terminator, short-circuiting before the real,
    complete fallback clause ever got a chance to run."""
    title = (
        "Sources: Broadcom is in talks with a group of lenders to raise $60B+ in debt for an AI chip "
        "financing deal that will benefit Anthropic and other companies (Bloomberg)"
    )
    excerpt = (
        "Bloomberg : Sources: Broadcom is in talks with a group of lenders to raise $60B+ in debt for "
        "an AI chip financing deal that will benefit Anthropic and other companies — Broadcom Inc. is "
        "in talks with a group of lenders to raise more than $60 billion in debt for an AI chip "
        "financing deal …"
    )
    result = summarize(excerpt, title)
    assert result == (
        "Broadcom Inc. is in talks with a group of lenders to raise more than $60 billion in debt "
        "for an AI chip financing deal."
    )
    assert result != "Broadcom Inc."


def test_summary_supermicro_regression_dangling_that_clause_rejected():
    """Real defect: "Super Micro Computer said on Thursday that an
    independent investigation led by its board found no evidence that
    current members." was delivered as a complete-looking sentence, but
    the trailing "that current members" clause never resolves -- the
    source excerpt was truncated mid-thought and a period was appended
    mechanically. Must now yield no summary (render-readiness gate then
    drops and backfills the story, per pipeline/daily_selection.py's
    existing drop-and-backfill design)."""
    title = (
        "Super Micro says its independent probe found no evidence its CEO or senior management knew "
        "of an alleged scheme to smuggle $2.5B worth of Nvidia chips to China (Amanda Gerut/Fortune)"
    )
    excerpt = (
        "Amanda Gerut / Fortune : Super Micro says its independent probe found no evidence its CEO or "
        "senior management knew of an alleged scheme to smuggle $2.5B worth of Nvidia chips to China "
        "— Super Micro Computer said on Thursday that an independent investigation led by its board "
        "found no evidence that current members …"
    )
    assert summarize(excerpt, title) is None


def test_summary_generic_corporate_suffix_period_is_not_a_sentence_end():
    """General case, not specific to Broadcom: any "<Name> Corp./Ltd./Co."
    fragment must not be treated as a complete sentence on its own."""
    excerpt = "Some Publisher : Restated headline — Acme Semiconductor Corp. reported record quarterly bookings."
    result = summarize(excerpt, "Acme headline")
    assert result == "Acme Semiconductor Corp. reported record quarterly bookings."


def test_summary_entity_only_fragment_with_no_verb_yields_no_summary():
    excerpt = "Some Publisher : Restated headline — Acme Semiconductor Corp."
    assert summarize(excerpt, "Acme headline") is None


def test_summary_dangling_which_clause_rejected():
    excerpt = (
        "Some Publisher : Restated headline — Regulators opened a formal inquiry into the merger, "
        "which industry analysts"
    )
    assert summarize(excerpt, "Merger headline") is None


def test_summary_dangling_who_clause_rejected():
    excerpt = (
        "Some Publisher : Restated headline — The company appointed a new chief executive, who "
        "previously"
    )
    assert summarize(excerpt, "Executive headline") is None


def test_summary_that_clause_with_a_real_verb_is_not_flagged_dangling():
    """Non-regression: a "that" clause that DOES reach a verb must not be
    rejected just because a subordinating conjunction appears near the
    end of some earlier, unrelated clause."""
    excerpt = (
        "Some Publisher : Restated headline — The board said in a statement that the investigation "
        "found no wrongdoing by current executives."
    )
    result = summarize(excerpt, "Investigation headline")
    assert result is not None
    assert "found no wrongdoing by current executives" in result


# 2026-08-22: real production observation -- the Aug 19-21 zh_cn digests
# consistently shipped only 2-3 total stories with no indication to the
# reader that the qualified pool was genuinely this thin, indistinguishable
# from a normal, deliberately concise day. The system must never force
# low-quality/promotional filler to hit a target count, but silently
# shipping a thin digest is its own disclosure failure.
def test_underfilled_pool_marker_appears_below_threshold():
    story = Story(primary=_item("E1", "A real story happens", "Markets & Economy", 80.0))
    result = SelectionResult(top_stories=[story], more_to_know=[])
    rendered = render_production_daily(result, "Aug 22", [])
    assert "only" in rendered and "quality bar" in rendered


def test_underfilled_pool_marker_absent_at_full_strength():
    stories = [
        Story(primary=_item(f"E{i}", f"Story number {i} happens", "Markets & Economy", 80.0))
        for i in range(6)
    ]
    result = SelectionResult(top_stories=stories[:3], more_to_know=stories[3:])
    rendered = render_production_daily(result, "Aug 22", [])
    assert "quality bar" not in rendered
