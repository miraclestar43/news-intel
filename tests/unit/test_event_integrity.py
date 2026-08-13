"""2026-08-13 repair: a dashboard/"latest numbers" landing page is not a
discrete news event."""

from __future__ import annotations

from news_intel.pipeline.event_integrity import is_dashboard_like


def test_bls_latest_numbers_dashboard_is_detected():
    title = "Major Economic Indicators Latest Numbers"
    excerpt = (
        "Consumer Price Index (CPI): +0.1% in Jul 2026 News Release Historical Data "
        "Unemployment Rate: 4.1% in Jul 2026 News Release Historical Data "
        "Payroll Employment: -23,000(p) in Jul 2026 News Release Historical Data"
    )
    assert is_dashboard_like(title, excerpt) is True


def test_single_discrete_regulatory_release_is_not_a_dashboard():
    title = "Schedules of Controlled Substances: Rescheduling of Suvorexant Into Schedule V"
    excerpt = "The DEA is rescheduling the drug from Schedule IV into Schedule V effective immediately."
    assert is_dashboard_like(title, excerpt) is False


def test_single_fed_decision_is_not_a_dashboard():
    title = "Federal Reserve holds rates steady"
    excerpt = "The Fed held rates steady, surprising markets."
    assert is_dashboard_like(title, excerpt) is False


def test_title_matching_dashboard_phrase_is_flagged_even_with_short_excerpt():
    assert is_dashboard_like("Economic Indicators at a Glance", "See the latest figures.") is True
