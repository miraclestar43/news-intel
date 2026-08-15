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


def test_corporate_suffix_alone_does_not_merge_unrelated_ma_stories():
    """2026-08-14 defect: "PBC" (Public Benefit Corporation, a generic legal
    suffix) survived as a short ALL-CAPS "acronym" token and falsely merged
    the SpaceX/Cursor acquisition (where "Anthropic PBC" is named only as a
    competitor in passing) with the unrelated Anthropic/Decart acquisition
    (where Anthropic PBC is the actual acquirer). "Anthropic" itself is
    filtered as a generic anchor term (config/topics.yaml), isolating this
    test to the corporate-suffix fix specifically."""
    generic_terms = _GENERIC_TERMS | {"Anthropic"}
    spacex_cursor = _item(
        "SpaceX completes $60B acquisition of Cursor maker Anysphere", "AI & Semiconductors",
        excerpt=(
            "SpaceX has completed its acquisition of Anysphere, maker of Cursor, gaining ground on "
            "rivals Anthropic PBC and OpenAI."
        ),
        entities=["SpaceX", "Anysphere", "Cursor", "Anthropic PBC", "OpenAI"],
    )
    decart = _item(
        "Anthropic PBC acquires Decart in AI video push", "AI & Semiconductors",
        excerpt="Anthropic PBC has agreed to acquire Decart, a real-time AI video startup.",
        entities=["Anthropic PBC", "Decart"],
    )
    assert same_event_story(spacex_cursor, decart, generic_terms=generic_terms) is False


def test_competitive_context_entity_excluded_from_newly_named():
    """2026-08-14 defect: a competitor named only in a "rivals X and Y"
    clause was reported as a "newly named" TRANSACTION PARTY. OpenAI/
    Anthropic appearing as background competitive context in both the prior
    and candidate telling of a SpaceX/Cursor completion must not surface as
    a newly involved party -- only the legitimate status change should."""
    config = load_config()
    known = known_entities(config.topics.groups)
    prior = _item(
        "SpaceX agrees to acquire Cursor maker Anysphere in $60B stock deal", "AI & Semiconductors",
        excerpt=(
            "SpaceX has signed an agreement to acquire Anysphere, maker of Cursor, in a stock deal "
            "valued at $60B, as Elon Musk seeks to gain ground on rivals Anthropic PBC and OpenAI."
        ),
    )
    candidate = _item(
        "SpaceX completes $60B acquisition of Cursor maker Anysphere", "AI & Semiconductors",
        excerpt=(
            "SpaceX has completed its previously announced $60B acquisition of Anysphere, maker of "
            "Cursor, as Elon Musk seeks to gain ground on rivals Anthropic PBC and OpenAI."
        ),
    )
    result = detect_material_update(candidate, prior, known_entities=known, alert_triggers=config.alert_triggers)
    assert result.is_update is True
    assert "status change (completed)" in result.note
    assert "OpenAI" not in result.note
    assert "Anthropic" not in result.note
    assert "newly named" not in result.note


def test_prior_known_dollar_value_with_abbreviated_unit_not_reported_as_new():
    """2026-08-14 defect: material_facts.py's own amount field only
    recognizes spelled-out units, so an unchanged abbreviated "$60B" value
    was reported as a bare, unit-less "new figure ($60)" even though the
    dollar amount was already known from the prior telling. Unit-aware
    parsing (materiality._parse_dollar_values) must recognize $60B in both
    tellings as the same value and never surface it as new."""
    config = load_config()
    known = known_entities(config.topics.groups)
    prior = _item(
        "SpaceX agrees to acquire Cursor maker Anysphere in $60B stock deal", "AI & Semiconductors",
        excerpt="SpaceX has signed an agreement to acquire Anysphere in a stock deal valued at $60B.",
    )
    candidate = _item(
        "SpaceX completes $60B acquisition of Cursor maker Anysphere", "AI & Semiconductors",
        excerpt="SpaceX has completed its previously announced $60B acquisition of Anysphere.",
    )
    result = detect_material_update(candidate, prior, known_entities=known, alert_triggers=config.alert_triggers)
    assert result.is_update is True
    assert "new figures" not in result.note
    assert "$60)" not in result.note


def test_new_dollar_value_formatted_with_unit_not_bare_digits():
    """The 2026-08-14 formatting fix: a genuinely new dollar figure must
    render as a readable "$X million/billion" form, never bare digits like
    "$700,000,000", so the delivered note stays Telegram-readable."""
    config = load_config()
    known = known_entities(config.topics.groups)
    prior = _item(
        "NVIDIA in talks for a financing deal", "AI & Semiconductors",
        excerpt="NVIDIA is reportedly negotiating a $500 million partnership.",
        confirmation_state="unverified",
    )
    candidate = _item(
        "NVIDIA signs financing deal", "AI & Semiconductors",
        excerpt="NVIDIA has signed a $700 million agreement, finalizing the partnership.",
        confirmation_state="official",
    )
    result = detect_material_update(candidate, prior, known_entities=known, alert_triggers=config.alert_triggers)
    assert "$700 million" in result.note
    assert "$700,000,000" not in result.note



def test_generic_economic_word_alone_does_not_merge_unrelated_stories():
    """2026-08-15 defect (found during the Aug 14 as-of replay): "Demand"
    survived as a "distinctive" material token and falsely merged an
    unrelated SMIC earnings story ("Strong Chip Demand") with the Aug 12
    BLS "latest numbers" dashboard ("Producer Price Index - Final
    Demand") -- both entity phrases happen to end in this same generic
    economic-report word once split into individual tokens."""
    smic = _item(
        "SMIC Shares Rally as Q2 Profit Surges 262 % on Strong Chip Demand", "Markets & Economy",
        entities=["Q2 Profit Surges", "SMIC Shares Rally", "Strong Chip Demand"],
    )
    bls = _item(
        "Major Economic Indicators Latest Numbers", "Markets & Economy",
        entities=[
            "Average Hourly Earnings", "CPI", "ECI", "Employment Cost Index", "Final Demand",
            "Major Economic Indicators Latest", "News Release Historical Data",
            "Numbers Consumer Price Index", "Payroll Employment", "Producer Price Index",
            "U.S. Export Price Index", "U.S. Import Price Index", "Unemployment Rate",
        ],
    )
    assert same_event_story(smic, bls, generic_terms=_GENERIC_TERMS) is False