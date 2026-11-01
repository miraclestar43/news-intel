"""2026-11-01: `pi-news digest daily --dry-run` always reported skipped_lock.

The CLI wrapped the job in db.rollback_only (a SAVEPOINT), and acquire_job_lock's BEGIN IMMEDIATE then failed with
"cannot start a transaction within a transaction" -- which was caught as "lock not acquired". These tests reproduce
that, pin the fix (nested lock in a SAVEPOINT; dry runs on a private copy), and pin that REAL contention still makes a
run stand aside while a program error now raises instead of masquerading as contention.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

import pytest

from news_intel import db
from news_intel.jobs.daily import run_daily
from tests.unit.test_daily_production import _seed_event

_NOW = datetime(2026, 8, 18, 5, 0, tzinfo=UTC)


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "news-intel.db"
    conn = db.connect(path)
    db.run_migrations(conn)
    conn.close()
    return path


def test_lock_inside_an_outer_savepoint_is_acquired(db_path):
    conn = db.connect(db_path)
    with db.rollback_only(conn):
        assert conn.in_transaction
        assert db.acquire_job_lock(conn, "daily_zh_cn") is True
    # rolled back with the preview: nothing left behind
    assert conn.execute("SELECT COUNT(*) FROM job_locks").fetchone()[0] == 0
    conn.close()


def test_a_live_lock_held_by_a_real_run_still_wins_nested_or_not(db_path):
    real = db.connect(db_path)
    assert db.acquire_job_lock(real, "daily_zh_cn") is True  # committed, unexpired
    preview = db.connect(db_path)
    assert db.acquire_job_lock(preview, "daily_zh_cn") is False
    with db.rollback_only(preview):
        assert db.acquire_job_lock(preview, "daily_zh_cn") is False
        preview.execute("SELECT 1").fetchone()
    db.release_job_lock(real, "daily_zh_cn")
    assert db.acquire_job_lock(preview, "daily_zh_cn") is True
    real.close()
    preview.close()


def test_busy_database_is_contention_not_an_error(db_path):
    writer = db.connect(db_path)
    other = db.connect(db_path)
    other.execute("PRAGMA busy_timeout=200")
    writer.execute("BEGIN IMMEDIATE")
    try:
        assert db.acquire_job_lock(other, "collect") is False
        with db.rollback_only(other):  # nested path: the write upgrade is refused -> contention
            assert db.acquire_job_lock(other, "collect") is False
    finally:
        writer.execute("ROLLBACK")
        writer.close()
        other.close()


def test_a_program_error_raises_instead_of_reporting_skipped_lock(tmp_path):
    conn = db.connect(tmp_path / "broken.db")
    conn.execute("CREATE TABLE job_locks (job_name TEXT PRIMARY KEY, locked_at TEXT NOT NULL)")  # no expires_at
    conn.execute("INSERT INTO job_locks VALUES ('daily', 'x')")
    with pytest.raises(sqlite3.OperationalError, match="expires_at"):
        db.acquire_job_lock(conn, "daily")
    assert not conn.in_transaction
    conn.close()


def test_dry_run_on_a_private_copy_really_runs_and_leaves_the_database_untouched(config, conn):
    _seed_event(conn, config, event_id="E-1", title="Fed raises rates by 25 basis points", category="Markets & Economy")
    before = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("digests", "job_runs", "job_locks")}

    with db.private_copy(conn) as preview:
        summary = run_daily(config, preview, dry_run=True, now=_NOW)

    assert summary.skipped_lock is False
    assert summary.rendered_text  # a real brief was produced
    after = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("digests", "job_runs", "job_locks")}
    assert after == before


def test_dry_run_copy_still_stands_aside_for_a_run_in_progress(config, conn):
    _seed_event(conn, config, event_id="E-1", title="Fed raises rates by 25 basis points", category="Markets & Economy")
    assert db.acquire_job_lock(conn, "daily") is True  # the real en_global daily is running

    with db.private_copy(conn) as preview:
        summary = run_daily(config, preview, dry_run=True, now=_NOW)

    assert summary.skipped_lock is True
    db.release_job_lock(conn, "daily")
