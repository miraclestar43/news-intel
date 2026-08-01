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
    """Best-effort application lock so overlapping timer runs exit cleanly."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS job_locks ("
        "job_name TEXT PRIMARY KEY, locked_at TEXT NOT NULL, expires_at TEXT NOT NULL)"
    )
    with transaction(conn):
        row = conn.execute(
            "SELECT expires_at FROM job_locks WHERE job_name = ?", (job_name,)
        ).fetchone()
        now_expr = "datetime('now')"
        if row is not None:
            still_locked = conn.execute(
                f"SELECT 1 WHERE ? > {now_expr}", (row["expires_at"],)
            ).fetchone()
            if still_locked:
                return False
        conn.execute(
            "INSERT INTO job_locks (job_name, locked_at, expires_at) VALUES "
            f"(?, {now_expr}, datetime('now', ?)) "
            "ON CONFLICT(job_name) DO UPDATE SET locked_at=excluded.locked_at, "
            "expires_at=excluded.expires_at",
            (job_name, f"+{ttl_seconds} seconds"),
        )
    return True


def release_job_lock(conn: sqlite3.Connection, job_name: str) -> None:
    conn.execute("DELETE FROM job_locks WHERE job_name = ?", (job_name,))
