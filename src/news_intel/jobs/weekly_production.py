"""Production weekly entrypoint (2026-08-17): `pi-news production weekly`.

Distinct from `pi-news digest weekly` (jobs/weekly.py's run_weekly, still
the lower-level dry-run-capable command) the same way `production daily`
(jobs/daily.py) sits above `digest daily` -- this module adds the
production-specific safety envelope BEFORE ever calling run_weekly():

  1. an explicit Sunday-Pacific-window check, rejecting any invocation
     outside it with a clean skipped_late_catchup result -- ZERO database
     writes, ZERO Telegram contact -- computed and enforced in application
     code, not relied on from systemd's OnCalendar/Persistent behavior
     alone. This is what prevents a `Persistent=true` timer, installed
     days after a missed Sunday slot, from firing an immediate catch-up
     send for that missed week.
  2. records the deployed git HEAD in the job_runs detail for this run.
  3. delegates to run_weekly() for the actual generation/idempotency/
     delivery logic (weekly:<ISO-week>:<chat-hash> idempotency, at most one
     Telegram attempt per part, no automatic resend -- unchanged, already
     correct there) -- this module adds nothing on top of that contract
     except the pre-flight Sunday-window gate and git-HEAD recording.

Sunday never collides with daily: jobs/daily.py's run_daily() already
returns skipped_sunday=True (no DB writes, no Telegram) whenever `now`
falls on a Pacific Sunday, so the daily and weekly production timers can
never both deliver on the same day."""

from __future__ import annotations

import sqlite3
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from news_intel.jobs import job_runs
from news_intel.jobs.weekly import WeeklySummary, run_weekly
from news_intel.settings import AppConfig

PACKAGE_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = PACKAGE_DIR.parent.parent

# How close to the nominal Sunday 22:00 Pacific slot `now` must fall to be
# treated as a genuine, on-time firing rather than either a stray manual
# invocation on some other day/time or a systemd Persistent=true catch-up
# firing outside the real window. Wide enough to absorb ordinary timer
# jitter/AccuracySec and a slow start, narrow enough that a catch-up firing
# hours or days after a missed Sunday is still rejected.
_WEEKLY_WINDOW_TOLERANCE = timedelta(hours=2)
_WEEKLY_TARGET_HOUR = 22


def _is_valid_weekly_window(now_pt: datetime) -> bool:
    if now_pt.weekday() != 6:  # Sunday
        return False
    target = now_pt.replace(hour=_WEEKLY_TARGET_HOUR, minute=0, second=0, microsecond=0)
    return abs(now_pt - target) <= _WEEKLY_WINDOW_TOLERANCE


def _current_git_head() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(PROJECT_ROOT),
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return out.stdout.strip() if out.returncode == 0 else "unknown"


@dataclass
class WeeklyProductionSummary(WeeklySummary):
    skipped_late_catchup: bool = False
    git_head: str = ""
    pt_window_checked_at: str = ""


def run_weekly_production(
    config: AppConfig, conn: sqlite3.Connection, *, now: datetime | None = None
) -> WeeklyProductionSummary:
    now = now or datetime.now(UTC)
    tz = ZoneInfo(config.policy.timezone)
    now_pt = now.astimezone(tz)

    if not _is_valid_weekly_window(now_pt):
        # Deliberately BEFORE any job_runs.start()/DB write of any kind --
        # a late/Persistent catch-up firing must leave zero trace besides
        # this in-memory result.
        return WeeklyProductionSummary(
            skipped_late_catchup=True,
            git_head=_current_git_head(),
            pt_window_checked_at=now_pt.isoformat(),
        )

    git_head = _current_git_head()
    allowed = config.secrets.allowed_chat_id_list

    # Fail closed before ever contacting Telegram (mirrors jobs/daily.py's
    # production envelope): an ambiguous destination is never resolved by
    # guessing. No DB writes happen in this branch either.
    if len(allowed) != 1:
        return WeeklyProductionSummary(
            skipped_late_catchup=False,
            git_head=git_head,
            pt_window_checked_at=now_pt.isoformat(),
            week_label="",
            idempotency_key="",
        )
    chat_id = allowed[0]

    # The actual content/provenance fail-closed check (empty/over-length
    # part, etc.) runs INSIDE run_weekly() itself, before its send loop --
    # see jobs/weekly.py's fail_closed handling -- so it is guaranteed to
    # run before any Telegram contact, not after.
    result = run_weekly(config, conn, chat_id=chat_id, dry_run=False, now=now)

    # run_weekly() already ran its own job_runs.start()/finish() pair; this
    # is a second, clearly-labeled production-envelope record so "was this
    # a genuine production invocation, and what code was deployed" is
    # answerable without cross-referencing job_name="weekly" rows.
    run_id = job_runs.start(conn, "weekly_production")
    job_runs.finish(
        conn, run_id, "failed" if result.fail_closed else "success",
        {
            "git_head": git_head,
            "week_label": result.week_label,
            "sent": result.sent,
            "idempotency_key": result.idempotency_key,
            "pt_window_checked_at": now_pt.isoformat(),
            "fail_closed_reason": result.fail_reason,
        },
    )

    return WeeklyProductionSummary(
        rendered_parts=result.rendered_parts,
        character_count=result.character_count,
        trend_count=result.trend_count,
        fallback_used=result.fallback_used,
        model_used=result.model_used,
        idempotency_key=result.idempotency_key,
        sent_dry_run=result.sent_dry_run,
        sent=result.sent,
        skipped_lock=result.skipped_lock,
        skipped_duplicate=result.skipped_duplicate,
        week_label=result.week_label,
        fail_closed=result.fail_closed,
        fail_reason=result.fail_reason,
        skipped_late_catchup=False,
        git_head=git_head,
        pt_window_checked_at=now_pt.isoformat(),
    )
