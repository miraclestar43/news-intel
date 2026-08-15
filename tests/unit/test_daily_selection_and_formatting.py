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


def test_render_readiness_gate_demotes_missing_summary_and_promotes_next():
    """2026-08-15 repair: a candidate with no complete-sentence excerpt
    (enrichment attempted -- daily_selection.Story.render_ready tries the
    primary source, exactly what happened here) must not occupy a TOP slot
    at all -- the old "stays in TOP with an honest placeholder note"
    behavior is what this round's render-readiness gate replaces, since
    that placeholder is not acceptable reader-facing content
    (delivery/daily_production_formatter.py no longer renders it). E-1 is
    demoted to MORE TO KNOW (still delivered -- MORE TO KNOW never needed
    a summary); E-2, the next render-ready candidate, is promoted into
    TOP."""
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
    assert "E-1" in more_ids  # demoted, not dropped


def test_decision_value_selection_is_computed_before_render_readiness():
    """The render-readiness gate only ever swaps stories *after* selection
    -- decision-value ranking itself (which family wins a coverage/fill
    slot) is entirely unaffected by summary availability, matching "summary
    availability must not change the initial decision-value ordering
    before enrichment has been attempted." Here E-1 (higher score, no
    summary) still wins the coverage/fill competition over E-3 (a lower-
    score, unrelated-domain family with a real summary) and is selected at
    all -- it just can't hold the TOP slot once render-readiness is
    checked, and is demoted rather than replaced by a family that never
    would have qualified for selection on its own."""
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
    assert all_ids == {"E-1", "E-2", "E-3"}  # AI cap is 2, but render-readiness demotion adds E-3 as a 3rd
    assert [s.event_id for s in result.top_stories] == ["E-2", "E-3"]
    assert [s.event_id for s in result.more_to_know] == ["E-1"]


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
