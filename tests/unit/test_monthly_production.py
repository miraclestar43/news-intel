"""Production monthly entrypoint (`pi-news production monthly`) and its
late-catch-up guard. See jobs/monthly_production.py's docstring for the
full contract -- mirrors tests/unit/test_weekly_production.py."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from news_intel import db
from news_intel.jobs.delivery_authorization import PRODUCTION_DELIVERY_AUTHORIZED
from news_intel.jobs.monthly_production import _is_valid_monthly_window, run_monthly_production
from news_intel.settings import load_config, reset_config_cache
from tests.conftest import PROJECT_ROOT
from tests.integration.test_monthly_job import FIRST_MONDAY_PT, _seed_full_month

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


def test_first_monday_at_2200_pt_is_valid_window():
    assert _is_valid_monthly_window(FIRST_MONDAY_PT) is True


def test_first_monday_within_tolerance_is_valid_window():
    assert _is_valid_monthly_window(FIRST_MONDAY_PT.replace(hour=23, minute=30)) is True
    assert _is_valid_monthly_window(FIRST_MONDAY_PT.replace(hour=20, minute=30)) is True


def test_second_monday_is_never_a_valid_window():
    """The exact late-catch-up scenario: installing the timer a week after
    the missed first Monday must never look like a valid window."""
    second_monday = FIRST_MONDAY_PT + timedelta(days=7)
    assert _is_valid_monthly_window(second_monday) is False


def test_first_tuesday_is_never_a_valid_window():
    assert _is_valid_monthly_window(FIRST_MONDAY_PT + timedelta(days=1)) is False


def test_first_monday_far_outside_tolerance_is_not_a_valid_window():
    assert _is_valid_monthly_window(FIRST_MONDAY_PT.replace(hour=8, minute=0)) is False


def test_first_monday_window_valid_across_spring_forward_dst():
    # 2026-03-02 is the first Monday of March 2026 (spring-forward is
    # 2026-03-08) -- 22:00 America/Los_Angeles must still resolve correctly
    # (PST, UTC-8) on the PRE-transition side.
    first_monday_march = datetime(2026, 3, 2, 22, 0, tzinfo=PT)
    assert _is_valid_monthly_window(first_monday_march) is True
    assert first_monday_march.utcoffset().total_seconds() / 3600 == -8.0


def test_first_monday_window_valid_across_fall_back_dst():
    # 2026-11-02 is the first Monday of November 2026, the day AFTER
    # fall-back (2026-11-01) -- 22:00 America/Los_Angeles must resolve to
    # PST (UTC-8) here, not the pre-transition PDT offset.
    first_monday_november = datetime(2026, 11, 2, 22, 0, tzinfo=PT)
    assert _is_valid_monthly_window(first_monday_november) is True
    assert first_monday_november.utcoffset().total_seconds() / 3600 == -8.0


# --- run_monthly_production: late-catch-up guard end to end -----------------


def test_late_catchup_on_a_non_first_monday_makes_zero_database_writes(config, conn):
    _seed_full_month(conn)
    second_monday = FIRST_MONDAY_PT + timedelta(days=7)

    counts_before = conn.execute(
        "SELECT (SELECT COUNT(*) FROM job_runs) AS jr, (SELECT COUNT(*) FROM digests) AS d, "
        "(SELECT COUNT(*) FROM deliveries) AS dv"
    ).fetchone()

    summary = run_monthly_production(config, conn, now=second_monday.astimezone(UTC))

    assert summary.skipped_late_catchup is True
    assert summary.sent is False

    counts_after = conn.execute(
        "SELECT (SELECT COUNT(*) FROM job_runs) AS jr, (SELECT COUNT(*) FROM digests) AS d, "
        "(SELECT COUNT(*) FROM deliveries) AS dv"
    ).fetchone()
    assert dict(counts_after) == dict(counts_before)


def test_late_catchup_reports_git_head_even_when_skipped(config, conn):
    second_monday = FIRST_MONDAY_PT + timedelta(days=7)
    summary = run_monthly_production(config, conn, now=second_monday.astimezone(UTC))
    assert summary.skipped_late_catchup is True
    assert summary.git_head


def test_valid_first_monday_window_proceeds_and_records_git_head(config, conn):
    _seed_full_month(conn)
    summary = run_monthly_production(
        config, conn, now=FIRST_MONDAY_PT.astimezone(UTC), authorization=PRODUCTION_DELIVERY_AUTHORIZED
    )

    assert summary.skipped_late_catchup is False
    assert summary.git_head
    assert summary.month_label
    assert summary.idempotency_key.startswith("monthly:")

    job_run = conn.execute(
        "SELECT * FROM job_runs WHERE job_name = 'monthly_production' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert job_run is not None
    assert summary.git_head in job_run["detail_json"]


def test_valid_first_monday_window_creates_exactly_one_monthly_digest_row(config, conn):
    _seed_full_month(conn)
    run_monthly_production(
        config, conn, now=FIRST_MONDAY_PT.astimezone(UTC), authorization=PRODUCTION_DELIVERY_AUTHORIZED
    )
    count = conn.execute("SELECT COUNT(*) AS n FROM digests WHERE kind = 'monthly'").fetchone()["n"]
    assert count == 1


def test_repeated_invocation_within_the_same_month_is_idempotent_not_a_resend(config, conn):
    _seed_full_month(conn)
    first = run_monthly_production(
        config, conn, now=FIRST_MONDAY_PT.astimezone(UTC), authorization=PRODUCTION_DELIVERY_AUTHORIZED
    )
    second = run_monthly_production(
        config, conn, now=(FIRST_MONDAY_PT + timedelta(minutes=5)).astimezone(UTC),
        authorization=PRODUCTION_DELIVERY_AUTHORIZED,
    )

    assert first.skipped_late_catchup is False
    assert first.fail_closed is False
    assert first.sent_dry_run is True
    assert second.skipped_duplicate is True
    digest_count = conn.execute("SELECT COUNT(*) AS n FROM digests WHERE kind = 'monthly'").fetchone()["n"]
    assert digest_count == 1


def test_ambiguous_allowlist_fails_closed_before_any_write(config, conn, monkeypatch):
    monkeypatch.setattr(type(config.secrets), "allowed_chat_id_list", property(lambda self: [111, 222]))
    _seed_full_month(conn)
    counts_before = conn.execute("SELECT COUNT(*) AS n FROM job_runs").fetchone()["n"]
    summary = run_monthly_production(
        config, conn, now=FIRST_MONDAY_PT.astimezone(UTC), authorization=PRODUCTION_DELIVERY_AUTHORIZED
    )
    assert summary.skipped_late_catchup is False
    assert summary.sent is False
    counts_after = conn.execute("SELECT COUNT(*) AS n FROM job_runs").fetchone()["n"]
    assert counts_after == counts_before
