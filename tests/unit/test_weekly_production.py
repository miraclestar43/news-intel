"""2026-08-17: production weekly entrypoint (`pi-news production weekly`)
and its late-catch-up guard. See jobs/weekly_production.py's docstring for
the full contract."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from news_intel import db
from news_intel.jobs.delivery_authorization import PRODUCTION_DELIVERY_AUTHORIZED
from news_intel.jobs.weekly_production import (
    _is_valid_weekly_window,
    run_weekly_production,
)
from news_intel.settings import load_config, reset_config_cache
from tests.conftest import PROJECT_ROOT
from tests.integration.test_weekly_job import SUNDAY_PT, _seed_full_week

PT = ZoneInfo("America/Los_Angeles")


@pytest.fixture
def config(tmp_path, monkeypatch):
    for var in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_ALLOWED_CHAT_IDS", "LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "555")
    reset_config_cache()
    cfg = load_config(config_dir=PROJECT_ROOT / "config", data_dir=tmp_path / "data")
    yield cfg
    reset_config_cache()


@pytest.fixture
def conn(config):
    connection = db.connect(config.data_dir / "news-intel.db")
    db.run_migrations(connection)
    yield connection
    connection.close()


# --- window validation (pure function, no DB) -------------------------------


def test_sunday_at_2200_pt_is_valid_window():
    assert _is_valid_weekly_window(datetime(2026, 8, 2, 22, 0, tzinfo=PT)) is True


def test_sunday_within_tolerance_is_valid_window():
    assert _is_valid_weekly_window(datetime(2026, 8, 2, 23, 30, tzinfo=PT)) is True
    assert _is_valid_weekly_window(datetime(2026, 8, 2, 20, 30, tzinfo=PT)) is True


def test_monday_is_never_a_valid_window():
    """The exact late-catch-up scenario: installing the timer on a Monday
    (days after a missed Sunday) must never look like a valid window."""
    assert _is_valid_weekly_window(datetime(2026, 8, 3, 22, 0, tzinfo=PT)) is False


def test_saturday_is_never_a_valid_window():
    assert _is_valid_weekly_window(datetime(2026, 8, 1, 22, 0, tzinfo=PT)) is False


def test_sunday_far_outside_tolerance_is_not_a_valid_window():
    """A Persistent=true catch-up could in principle fire on the RIGHT
    weekday but hours off the real slot (e.g. very late Sunday night into
    early Monday scheduling edge cases) -- still rejected."""
    assert _is_valid_weekly_window(datetime(2026, 8, 2, 8, 0, tzinfo=PT)) is False


# --- run_weekly_production: late-catch-up guard end to end -------------------


def test_late_catchup_on_a_non_sunday_makes_zero_database_writes(config, conn):
    """The exact scenario this guard exists for: the timer is being
    installed/started days after the missed Aug 16 (2026-W33) Sunday slot
    -- must not create ANY job_runs/digests/deliveries row and must not
    contact Telegram."""
    _seed_full_week(conn)
    monday = SUNDAY_PT + timedelta(days=1)

    counts_before = conn.execute(
        "SELECT (SELECT COUNT(*) FROM job_runs) AS jr, (SELECT COUNT(*) FROM digests) AS d, "
        "(SELECT COUNT(*) FROM deliveries) AS dv"
    ).fetchone()

    summary = run_weekly_production(config, conn, now=monday.astimezone(UTC))

    assert summary.skipped_late_catchup is True
    assert summary.sent is False

    counts_after = conn.execute(
        "SELECT (SELECT COUNT(*) FROM job_runs) AS jr, (SELECT COUNT(*) FROM digests) AS d, "
        "(SELECT COUNT(*) FROM deliveries) AS dv"
    ).fetchone()
    assert dict(counts_after) == dict(counts_before)


def test_late_catchup_reports_git_head_even_when_skipped(config, conn):
    monday = SUNDAY_PT + timedelta(days=1)
    summary = run_weekly_production(config, conn, now=monday.astimezone(UTC))
    assert summary.skipped_late_catchup is True
    assert summary.git_head  # non-empty: "unknown" outside a git repo, else a real hash


def test_valid_sunday_window_proceeds_and_records_git_head(config, conn):
    _seed_full_week(conn)
    summary = run_weekly_production(
        config, conn, now=SUNDAY_PT.astimezone(UTC), authorization=PRODUCTION_DELIVERY_AUTHORIZED
    )

    assert summary.skipped_late_catchup is False
    assert summary.git_head
    assert summary.week_label
    assert summary.idempotency_key.startswith("weekly:")

    job_run = conn.execute(
        "SELECT * FROM job_runs WHERE job_name = 'weekly_production' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert job_run is not None
    assert summary.git_head in job_run["detail_json"]


def test_valid_sunday_window_creates_exactly_one_weekly_digest_row(config, conn):
    _seed_full_week(conn)
    run_weekly_production(
        config, conn, now=SUNDAY_PT.astimezone(UTC), authorization=PRODUCTION_DELIVERY_AUTHORIZED
    )
    count = conn.execute("SELECT COUNT(*) AS n FROM digests WHERE kind = 'weekly'").fetchone()["n"]
    assert count == 1


def test_repeated_invocation_within_the_same_week_is_idempotent_not_a_resend(config, conn):
    _seed_full_week(conn)
    first = run_weekly_production(
        config, conn, now=SUNDAY_PT.astimezone(UTC), authorization=PRODUCTION_DELIVERY_AUTHORIZED
    )
    second = run_weekly_production(
        config, conn, now=(SUNDAY_PT + timedelta(minutes=5)).astimezone(UTC),
        authorization=PRODUCTION_DELIVERY_AUTHORIZED,
    )

    # No real Telegram bot token configured in this test config, so the send
    # itself stays dry-run (sent_dry_run=True) even though dry_run=False was
    # requested -- matches jobs/daily.py's production tests' same pattern.
    assert first.skipped_late_catchup is False
    assert first.fail_closed is False
    assert first.sent_dry_run is True
    assert second.skipped_duplicate is True
    digest_count = conn.execute("SELECT COUNT(*) AS n FROM digests WHERE kind = 'weekly'").fetchone()["n"]
    assert digest_count == 1


def test_ambiguous_allowlist_fails_closed_before_any_write(config, conn, monkeypatch):
    monkeypatch.setattr(type(config.secrets), "allowed_chat_id_list", property(lambda self: [111, 222]))
    _seed_full_week(conn)
    counts_before = conn.execute("SELECT COUNT(*) AS n FROM job_runs").fetchone()["n"]
    summary = run_weekly_production(
        config, conn, now=SUNDAY_PT.astimezone(UTC), authorization=PRODUCTION_DELIVERY_AUTHORIZED
    )
    assert summary.skipped_late_catchup is False
    assert summary.sent is False
    counts_after = conn.execute("SELECT COUNT(*) AS n FROM job_runs").fetchone()["n"]
    assert counts_after == counts_before
