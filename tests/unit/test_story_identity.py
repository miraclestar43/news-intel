"""2026-08-12 repair: stable event signature replacing the 2026-08-11
same-domain + same-day + shared-capitalized-token heuristic, plus cross-day
material-update detection."""

from __future__ import annotations

from news_intel.pipeline.entities import known_entities
from news_intel.pipeline.story_identity import detect_material_update, same_event_story
from news_intel.settings import load_config

_GENERIC_TERMS = {"OpenAI", "ChatGPT", "NVIDIA", "AI", "GPT", "Google", "Microsoft"}


def _item(title, category, *, domain="example.com", excerpt="", publisher="Test Publisher",
          entities=None, published_at="2026-08-10T10:00:00+00:00", confirmation_state="official"):
    return {
        "title": title, "category": category, "excerpt": excerpt, "entities": entities or [],
        "confirmation_state": confirmation_state,
        "sources": [{"publisher": publisher, "url": f"https://{domain}/x", "published_at": published_at}],
    }


def test_same_publisher_shared_company_name_alone_does_not_merge():
    a = _item("NVIDIA financing platform announced", "AI & Semiconductors", publisher="NVIDIA newsroom",
              entities=["NVIDIA"])
    b = _item("NVIDIA Jetson update ships", "AI & Semiconductors", publisher="NVIDIA newsroom",
              entities=["NVIDIA"])
    assert same_event_story(a, b, generic_terms=_GENERIC_TERMS) is False


def test_shared_distinctive_entity_merges_across_different_publishers():
    a = _item("NVIDIA financing deal", "AI & Semiconductors", domain="nvidianews.nvidia.com",
              publisher="NVIDIA newsroom", entities=["Goldman Sachs", "KKR"])
    b = _item("Report on the NVIDIA-KKR deal", "AI & Semiconductors", domain="techmeme.com",
              publisher="Techmeme", entities=["KKR"])
    assert same_event_story(a, b, generic_terms=_GENERIC_TERMS) is True


def test_different_category_never_merges_even_with_shared_entity():
    a = _item("KKR-backed financing deal", "AI & Semiconductors", entities=["KKR"])
    b = _item("KKR quarterly earnings report", "Markets & Economy", entities=["KKR"])
    assert same_event_story(a, b, generic_terms=_GENERIC_TERMS) is False


def test_far_apart_in_time_does_not_merge():
    a = _item("Distinctive Program launches", "AI & Semiconductors", entities=["Distinctive Program"],
              published_at="2026-08-01T10:00:00+00:00")
    b = _item("Distinctive Program relaunches", "AI & Semiconductors", entities=["Distinctive Program"],
              published_at="2026-09-01T10:00:00+00:00")
    assert same_event_story(a, b, generic_terms=_GENERIC_TERMS, max_days=10) is False


def test_templated_titles_differing_only_by_number_do_not_merge_on_title_alone():
    """A quarterly-report-style title pair with no shared entities and no
    overlapping digits must not merge purely on trigram title similarity."""
    a = _item("Q2 2026 Earnings Call transcript", "Markets & Economy")
    b = _item("Q3 2026 Earnings Call transcript", "Markets & Economy")
    assert same_event_story(a, b, generic_terms=_GENERIC_TERMS) is False


def test_material_update_detected_for_new_figure_and_status_change():
    config = load_config()
    known = known_entities(config.topics.groups)
    prior = _item("NVIDIA in talks for a financing deal", "AI & Semiconductors",
                   excerpt="NVIDIA is reportedly negotiating a $500 million partnership.",
                   confirmation_state="unverified")
    candidate = _item("NVIDIA signs financing deal", "AI & Semiconductors",
                       excerpt="NVIDIA has signed a $700 million agreement, finalizing the partnership.",
                       confirmation_state="official")
    result = detect_material_update(candidate, prior, known_entities=known, alert_triggers=config.alert_triggers)
    assert result.is_update is True
    assert "700 million" in result.note


def test_pure_recap_is_not_a_material_update():
    """The exact 2026-08-10 -> 2026-08-12 NVIDIA financing recap: same
    figure, same partner list, restated under a new headline -- must not
    register as a material update (case/currency-formatting differences in
    the same figure must not count as a 'new' number)."""
    config = load_config()
    known = known_entities(config.topics.groups)
    prior = _item(
        "NVIDIA Partners With Apollo, BlackRock and KKR to Mobilize Over $500 Billion of Capital",
        "AI & Semiconductors",
        excerpt="NVIDIA today announced strategic partnerships to mobilize over $500 Billion.",
    )
    candidate = _item(
        "NVIDIA AI Factory Compute Is Becoming an Investable Asset Class",
        "AI & Semiconductors",
        excerpt="We announced partnerships to mobilize over $500 billion of third-party capital.",
    )
    result = detect_material_update(candidate, prior, known_entities=known, alert_triggers=config.alert_triggers)
    assert result.is_update is False
    assert result.note is None


def test_reversal_language_counts_as_material_update():
    config = load_config()
    known = known_entities(config.topics.groups)
    prior = _item("Regulator approves the merger", "Markets & Economy",
                   excerpt="Regulators approved the proposed merger between the two firms.")
    candidate = _item("Regulator blocks the merger", "Markets & Economy",
                       excerpt="Regulators have withdrawn approval and blocked the merger.")
    result = detect_material_update(candidate, prior, known_entities=known, alert_triggers=config.alert_triggers)
    assert result.is_update is True


def test_shared_macro_acronym_alone_does_not_merge():
    """2026-08-12 defect: a BLS "latest numbers" dashboard and an unrelated
    Asian-markets commentary article both mentioned "CPI" -- shared generic
    economic terminology, not a shared distinctive entity."""
    bls = _item(
        "Major Economic Indicators Latest Numbers", "Markets & Economy",
        excerpt="Consumer Price Index (CPI): +0.1% in Jul 2026. Unemployment Rate: 4.1% in Jul 2026.",
    )
    hellenic = _item(
        "Asian stocks rise as KOSPI surges; CPI, Hormuz risks temper tech rally", "Markets & Economy",
        excerpt="Asian markets rallied even as CPI data and Hormuz risks weighed on tech shares.",
    )
    assert same_event_story(bls, hellenic, generic_terms=_GENERIC_TERMS) is False
