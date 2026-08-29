"""2026-08-29 production incident: the Aug 27 zh_cn daily run crashed with
"OperationalError: database is locked" one second after starting, because
pi-news-collect.timer (every 5 minutes) held a write transaction at the
same instant. The run produced no digest, no delivery, and -- because it
died inside acquire_job_lock, before job_runs.start() -- no run record at
all, so the failure was invisible in the database and the next day's
digest silently covered a 48-hour window.

The connection's 30s busy_timeout did not help: acquire_job_lock opened a
DEFERRED transaction with a SELECT and then upgraded it to a write, and
SQLite returns SQLITE_BUSY immediately on that upgrade rather than
waiting. These tests pin the fix (BEGIN IMMEDIATE + busy-error handling).
"""

from __future__ import annotations

import sqlite3

import pytest

from news_intel import db


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "news-intel.db"
    conn = db.connect(path)
    db.run_migrations(conn)
    conn.close()
    return path


def test_acquire_job_lock_does_not_raise_while_another_writer_holds_the_db(db_path):
    """Reproduces the exact crash: a second connection is mid-write (as the
    collect job was) when the daily job tries to take its lock."""
    writer = db.connect(db_path)
    daily = db.connect(db_path)
    # Force a short busy timeout so the test fails fast rather than waiting
    # out the real 30s if the fix regresses.
    daily.execute("PRAGMA busy_timeout=250")
    writer.execute("BEGIN IMMEDIATE")
    writer.execute(
        "CREATE TABLE IF NOT EXISTS job_locks ("
        "job_name TEXT PRIMARY KEY, locked_at TEXT NOT NULL, expires_at TEXT NOT NULL)"
    )
    try:
        # Must return a bool, never raise OperationalError.
        acquired = db.acquire_job_lock(daily, "daily_zh_cn")
        assert acquired in (True, False)
    finally:
        writer.execute("ROLLBACK")
        writer.close()
        daily.close()


def test_acquire_job_lock_succeeds_once_the_other_writer_commits(db_path):
    writer = db.connect(db_path)
    daily = db.connect(db_path)
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("CREATE TABLE IF NOT EXISTS probe (x INTEGER)")
    writer.execute("COMMIT")
    writer.close()
    try:
        assert db.acquire_job_lock(daily, "daily_zh_cn") is True
    finally:
        daily.close()


def test_job_lock_still_excludes_a_genuine_overlapping_run(db_path):
    """The busy-error handling must not weaken the lock's real purpose."""
    first = db.connect(db_path)
    second = db.connect(db_path)
    try:
        assert db.acquire_job_lock(first, "daily") is True
        assert db.acquire_job_lock(second, "daily") is False
    finally:
        first.close()
        second.close()


def test_job_lock_is_edition_scoped(db_path):
    """A zh_cn daily run must never be blocked by an en_global one."""
    en = db.connect(db_path)
    zh = db.connect(db_path)
    try:
        assert db.acquire_job_lock(en, "daily") is True
        assert db.acquire_job_lock(zh, "daily_zh_cn") is True
    finally:
        en.close()
        zh.close()


def test_release_then_reacquire(db_path):
    conn = db.connect(db_path)
    try:
        assert db.acquire_job_lock(conn, "daily") is True
        db.release_job_lock(conn, "daily")
        assert db.acquire_job_lock(conn, "daily") is True
    finally:
        conn.close()


def test_connection_has_a_real_busy_timeout(db_path):
    conn = db.connect(db_path)
    try:
        timeout_ms = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        assert timeout_ms >= 1000, f"busy_timeout is {timeout_ms}ms -- too low to ride out a collect run"
    finally:
        conn.close()


def test_sqlite3_operational_error_is_importable_for_the_handler():
    assert issubclass(sqlite3.OperationalError, sqlite3.Error)
