"""CLI edition wiring: `--edition` must actually reach the underlying job
functions (run_daily/run_weekly/run_monthly/run_weekly_production/
run_monthly_production) with the correct kwarg, for both the dry-run-capable
`digest` commands and the always-real `production` commands. Job-level
edition behavior (isolation, idempotency, scheduling) is already covered by
tests/unit/test_daily_production.py, test_weekly_production.py,
test_monthly_production.py, etc. -- this file only exercises the CLI layer
itself, where a wiring bug (wrong kwarg name, unedited copy-paste) would not
be caught by those job-level tests."""

from __future__ import annotations

import os

import pytest
from typer.testing import CliRunner

from news_intel import settings
from news_intel.cli import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    """cli.py's _load() calls settings.load_config() with no arguments,
    resolving against module-level DEFAULT_CONFIG_DIR/DEFAULT_DATA_DIR --
    redirect DEFAULT_DATA_DIR to an isolated tmp_path so this test never
    touches the worktree's own data/ directory, and never any Telegram
    credential (TELEGRAM_BOT_TOKEN is deliberately left unset)."""
    monkeypatch.setattr(settings, "DEFAULT_DATA_DIR", tmp_path)
    for var in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_ALLOWED_CHAT_IDS", "LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "555")
    settings.reset_config_cache()
    yield
    settings.reset_config_cache()


def test_digest_daily_echoes_the_requested_edition():
    result = runner.invoke(app, ["digest", "daily", "--dry-run", "--edition", "zh_cn"])
    assert result.exit_code == 0, result.output
    assert "edition=zh_cn" in result.output


def test_digest_daily_defaults_to_en_global():
    result = runner.invoke(app, ["digest", "daily", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "edition=en_global" in result.output


def test_digest_weekly_echoes_the_requested_edition():
    result = runner.invoke(app, ["digest", "weekly", "--dry-run", "--edition", "zh_cn"])
    assert result.exit_code == 0, result.output
    assert "edition=zh_cn" in result.output


def test_digest_monthly_echoes_the_requested_edition():
    result = runner.invoke(app, ["digest", "monthly", "--dry-run", "--edition", "zh_cn"])
    assert result.exit_code == 0, result.output
    assert "edition=zh_cn" in result.output


def test_production_weekly_reports_edition_on_late_catchup_skip():
    """No PRODUCTION_START_DATE gate on weekly/monthly (unlike daily) --
    the late-catch-up window check runs immediately, so an ordinary test
    invocation (not a real Sunday 22:00 PT) safely skips with zero writes
    and zero Telegram contact, while still proving `edition` reached
    run_weekly_production()."""
    result = runner.invoke(app, ["production", "weekly", "--edition", "zh_cn"])
    assert result.exit_code == 0, result.output
    assert "edition=zh_cn" in result.output
    assert "skipped_late_catchup" in result.output


def test_production_monthly_reports_edition_on_late_catchup_skip():
    result = runner.invoke(app, ["production", "monthly", "--edition", "zh_cn"])
    assert result.exit_code == 0, result.output
    assert "edition=zh_cn" in result.output
    assert "skipped_late_catchup" in result.output


def test_production_daily_reports_edition_before_start_date():
    """PRODUCTION_START_DATE set far in the future -- the pt_date < start
    safety-net skip fires immediately, before any Telegram contact, while
    still proving `edition` reached the Pacific-date computation itself
    (config.editions.get(edition).schedule.timezone, not config.policy)."""
    os.environ["PRODUCTION_START_DATE"] = "2099-01-01"
    try:
        result = runner.invoke(app, ["production", "daily", "--edition", "zh_cn"])
    finally:
        os.environ.pop("PRODUCTION_START_DATE", None)
    assert result.exit_code == 0, result.output
    assert "edition=zh_cn" in result.output
    assert "skipping, no Telegram contact" in result.output


def test_invalid_edition_id_fails_closed_not_silently_defaulting():
    """config.editions.get() must raise/error for an unknown edition id
    rather than silently falling back to en_global -- a typo in a systemd
    ExecStart line must be loud, not a silent en_global misfire."""
    result = runner.invoke(app, ["digest", "daily", "--dry-run", "--edition", "not_a_real_edition"])
    assert result.exit_code != 0
