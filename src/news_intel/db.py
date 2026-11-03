"""SQLite connection management and migration runner.

WAL mode, foreign keys on, numbered SQL migration files applied in order
and tracked in schema_version.
"""

from __future__ import annotations

import sqlite3
import tempfile
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


_BUSY_ERRORS = ("SQLITE_BUSY", "SQLITE_LOCKED")


def _is_busy(exc: sqlite3.OperationalError) -> bool:
    """Lock contention (another writer holds the database) as opposed to a program error."""
    name = getattr(exc, "sqlite_errorname", "") or ""
    return name.startswith(_BUSY_ERRORS) or "database is locked" in str(exc) or "database table is locked" in str(exc)


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
    seconds instead of killing it.

    2026-11-01: returns False ONLY for real contention -- a live lock row, or
    SQLITE_BUSY/LOCKED after the busy timeout. Any other error is a program
    error and raises. Before, every OperationalError meant "not acquired",
    which hid a bug: inside a --dry-run's outer SAVEPOINT, BEGIN IMMEDIATE
    always fails ("cannot start a transaction within a transaction"), so
    every `digest daily --dry-run` reported skipped_lock and never ran. When
    the connection is already inside a transaction, the lock is now taken in
    a nested SAVEPOINT; it still sees (and yields to) any committed lock row.
    """
    conn.execute(
        "CREATE TABLE IF NOT EXISTS job_locks ("
        "job_name TEXT PRIMARY KEY, locked_at TEXT NOT NULL, expires_at TEXT NOT NULL)"
    )
    now_expr = "datetime('now')"
    nested = conn.in_transaction
    sp = f"lock_{uuid.uuid4().hex}"
    try:
        conn.execute(f"SAVEPOINT {sp}" if nested else "BEGIN IMMEDIATE")
    except sqlite3.OperationalError as exc:
        if _is_busy(exc):
            return False
        raise
    commit, abort = (f"RELEASE {sp}", f"ROLLBACK TO {sp}") if nested else ("COMMIT", "ROLLBACK")
    try:
        row = conn.execute(
            "SELECT expires_at FROM job_locks WHERE job_name = ?", (job_name,)
        ).fetchone()
        if row is not None:
            still_locked = conn.execute(
                f"SELECT 1 WHERE ? > {now_expr}", (row["expires_at"],)
            ).fetchone()
            if still_locked:
                conn.execute(abort)
                if nested:
                    conn.execute(f"RELEASE {sp}")
                return False
        conn.execute(
            "INSERT INTO job_locks (job_name, locked_at, expires_at) VALUES "
            f"(?, {now_expr}, datetime('now', ?)) "
            "ON CONFLICT(job_name) DO UPDATE SET locked_at=excluded.locked_at, "
            "expires_at=excluded.expires_at",
            (job_name, f"+{ttl_seconds} seconds"),
        )
        conn.execute(commit)
    except sqlite3.OperationalError as exc:
        conn.execute(abort)
        if nested:
            conn.execute(f"RELEASE {sp}")
        if _is_busy(exc):
            return False
        raise
    return True


@contextmanager
def private_copy(conn: sqlite3.Connection):
    """A throwaway copy of the database for --dry-run previews (2026-11-01).

    A dry run used to wrap the job in a SAVEPOINT on the live database. Once
    the job can actually run there (see acquire_job_lock), that savepoint
    would hold the production write lock for the whole preview -- minutes of
    model calls -- and block collect and the real dailies. The preview runs
    on an online-backup copy instead, inside a rolled-back savepoint as
    before. The copy carries the live job_locks rows, so a real run that is
    in progress still makes the preview stand aside."""
    with tempfile.TemporaryDirectory(prefix="news-intel-preview-") as tmp:
        path = Path(tmp) / "preview.db"
        dst = sqlite3.connect(str(path))
        try:
            conn.backup(dst)
        finally:
            dst.close()
        copy = connect(path)
        try:
            with rollback_only(copy):
                yield copy
        finally:
            copy.close()


def release_job_lock(conn: sqlite3.Connection, job_name: str) -> None:
    conn.execute("DELETE FROM job_locks WHERE job_name = ?", (job_name,))
