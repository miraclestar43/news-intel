"""SQLite connection management and migration runner.

WAL mode, foreign keys on, numbered SQL migration files applied in order
and tracked in schema_version.
"""

from __future__ import annotations

import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_DIR.parent.parent
MIGRATIONS_DIR = PROJECT_ROOT / "migrations"


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection):
    """SAVEPOINT-based so callers can nest safely (e.g. a CLI --dry-run
    wraps a whole job run in an outer savepoint that it rolls back, while
    the job's own internal transactions commit/release normally)."""
    name = f"sp_{uuid.uuid4().hex}"
    conn.execute(f"SAVEPOINT {name}")
    try:
        yield conn
        conn.execute(f"RELEASE {name}")
    except Exception:
        conn.execute(f"ROLLBACK TO {name}")
        conn.execute(f"RELEASE {name}")
        raise


@contextmanager
def rollback_only(conn: sqlite3.Connection):
    """Runs the wrapped block inside a savepoint that is always rolled back
    on exit, regardless of success — used for dry-run previews so a real
    fetch/scoring pass can run without persisting anything."""
    name = f"sp_{uuid.uuid4().hex}"
    conn.execute(f"SAVEPOINT {name}")
    try:
        yield conn
    finally:
        conn.execute(f"ROLLBACK TO {name}")
        conn.execute(f"RELEASE {name}")


def _migration_files() -> list[Path]:
    return sorted(MIGRATIONS_DIR.glob("*.sql"), key=lambda p: p.name)


def applied_versions(conn: sqlite3.Connection) -> set[str]:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        "version TEXT PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    rows = conn.execute("SELECT version FROM schema_version").fetchall()
    return {r["version"] for r in rows}


def run_migrations(conn: sqlite3.Connection) -> list[str]:
    """Apply pending migrations in order. Returns the list of newly applied versions.

    Note: sqlite3's executescript() implicitly commits any open transaction
    before running, so each migration script is not wrapped in our
    SAVEPOINT-based transaction() helper — every table/index in the shipped
    migrations uses IF NOT EXISTS, so re-applying a partially-applied script
    on a retried run is safe.
    """
    applied = applied_versions(conn)
    newly_applied: list[str] = []
    for path in _migration_files():
        version = path.stem
        if version in applied:
            continue
        sql = path.read_text(encoding="utf-8")
        conn.executescript(sql)
        conn.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
        newly_applied.append(version)
    return newly_applied


def acquire_job_lock(conn: sqlite3.Connection, job_name: str, ttl_seconds: int = 900) -> bool:
    """Best-effort application lock so overlapping timer runs exit cleanly.

    2026-08-29 production incident: the Aug 27 zh_cn daily run CRASHED here
    with "OperationalError: database is locked" one second after start,
    because pi-news-collect.timer (every 5 minutes) happened to hold a
    write transaction at exactly the same instant (collect ran
    14:00:20.937 -> 14:00:34.771 UTC; the zh_cn daily fired 14:00:20).
    That produced no digest, no delivery, AND no job_runs record at all --
    the run vanished silently, and the following day's digest silently
    covered a 48-hour window instead of 24.

    The connection's 30s busy_timeout did not help: this block opened a
    DEFERRED transaction with a SELECT and then upgraded it to a write.
    SQLite cannot safely wait on that upgrade (the deferred reader may be
    holding a snapshot another writer needs), so it returns SQLITE_BUSY
    IMMEDIATELY rather than honouring busy_timeout. Taking the write lock
    up front with BEGIN IMMEDIATE makes the busy_timeout apply normally,
    so a concurrent collect run now simply delays this job by a few
    seconds instead of killing it. Any residual busy error is caught and
    reported as "lock not acquired" so the caller takes its ordinary
    skipped-lock path instead of raising.
    """
    conn.execute(
        "CREATE TABLE IF NOT EXISTS job_locks ("
        "job_name TEXT PRIMARY KEY, locked_at TEXT NOT NULL, expires_at TEXT NOT NULL)"
    )
    now_expr = "datetime('now')"
    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError:
        return False
    try:
        row = conn.execute(
            "SELECT expires_at FROM job_locks WHERE job_name = ?", (job_name,)
        ).fetchone()
        if row is not None:
            still_locked = conn.execute(
                f"SELECT 1 WHERE ? > {now_expr}", (row["expires_at"],)
            ).fetchone()
            if still_locked:
                conn.execute("ROLLBACK")
                return False
        conn.execute(
            "INSERT INTO job_locks (job_name, locked_at, expires_at) VALUES "
            f"(?, {now_expr}, datetime('now', ?)) "
            "ON CONFLICT(job_name) DO UPDATE SET locked_at=excluded.locked_at, "
            "expires_at=excluded.expires_at",
            (job_name, f"+{ttl_seconds} seconds"),
        )
        conn.execute("COMMIT")
    except sqlite3.OperationalError:
        conn.execute("ROLLBACK")
        return False
    return True


def release_job_lock(conn: sqlite3.Connection, job_name: str) -> None:
    conn.execute("DELETE FROM job_locks WHERE job_name = ?", (job_name,))
