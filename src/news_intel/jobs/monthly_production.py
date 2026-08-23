"""Production monthly entrypoint: `pi-news production monthly`.

Mirrors jobs/weekly_production.py's structure exactly, with a first-Monday-
of-the-month window check instead of Sunday: rejects any invocation
outside a narrow window around the daily hour on the first Monday BEFORE
any job-run/digest/delivery record is created or Telegram is contacted --
the actual mechanism (not systemd's Persistent=true alone) that keeps a
timer installed after a missed first-Monday slot from firing a late
catch-up send.

Monthly replaces that Monday's daily (jobs/daily.py's run_daily returns
skipped_monthly_slot=True on the first Monday -- the late-catch-up guard
here, plus the production CLI choosing monthly over daily on that date, is
what enforces the precedence). Monthly does NOT replace the preceding
Sunday weekly."""

from __future__ import annotations

import sqlite3
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from news_intel.jobs import job_runs
from news_intel.jobs.delivery_authorization import ProductionDeliveryAuthorization
from news_intel.jobs.monthly import MonthlySummary, is_first_monday, run_monthly
from news_intel.settings import AppConfig

PACKAGE_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = PACKAGE_DIR.parent.parent

_MONTHLY_WINDOW_TOLERANCE = timedelta(hours=2)
_MONTHLY_TARGET_HOUR = 22


def _is_valid_monthly_window(now_pt: datetime, target_hour: int = _MONTHLY_TARGET_HOUR) -> bool:
    if not is_first_monday(now_pt.date()):
        return False
    target = now_pt.replace(hour=target_hour, minute=0, second=0, microsecond=0)
    return abs(now_pt - target) <= _MONTHLY_WINDOW_TOLERANCE


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
class MonthlyProductionSummary(MonthlySummary):
    skipped_late_catchup: bool = False
    git_head: str = ""
    pt_window_checked_at: str = ""


def run_monthly_production(
    config: AppConfig,
    conn: sqlite3.Connection,
    *,
    now: datetime | None = None,
    edition_id: str = "en_global",
    authorization: ProductionDeliveryAuthorization | None = None,
) -> MonthlyProductionSummary:
    """2026-08-23 repair: `authorization` is required for any real send --
    see jobs/delivery_authorization.py and weekly_production.py's mirrored
    docstring. Omitting it (the default) still fully constructs/renders the
    digest but returns before ever resolving a chat_id or constructing a
    TelegramSender, independent of what credentials are present."""
    now = now or datetime.now(UTC)
    edition = config.editions.get(edition_id)
    tz = ZoneInfo(edition.schedule.timezone)
    now_pt = now.astimezone(tz)

    if not _is_valid_monthly_window(now_pt, edition.schedule.monthly_hour):
        # Deliberately BEFORE any job_runs.start()/DB write of any kind --
        # a late/Persistent catch-up firing must leave zero trace besides
        # this in-memory result.
        return MonthlyProductionSummary(
            skipped_late_catchup=True, git_head=_current_git_head(), pt_window_checked_at=now_pt.isoformat(),
        )

    git_head = _current_git_head()

    if authorization is None:
        preview = run_monthly(config, conn, chat_id=None, dry_run=True, now=now, edition_id=edition_id)
        return MonthlyProductionSummary(
            rendered_parts=preview.rendered_parts, character_count=preview.character_count,
            trend_count=preview.trend_count, fallback_used=preview.fallback_used, model_used=preview.model_used,
            idempotency_key=preview.idempotency_key, sent_dry_run=True, sent=False,
            skipped_lock=preview.skipped_lock, skipped_duplicate=preview.skipped_duplicate,
            month_label=preview.month_label, fail_closed=preview.fail_closed, fail_reason=preview.fail_reason,
            skipped_late_catchup=False, git_head=git_head, pt_window_checked_at=now_pt.isoformat(),
        )

    allowed = config.secrets.allowed_chat_id_list
    if len(allowed) != 1:
        return MonthlyProductionSummary(
            skipped_late_catchup=False, git_head=git_head, pt_window_checked_at=now_pt.isoformat(),
            month_label="", idempotency_key="",
        )
    chat_id = allowed[0]

    result = run_monthly(config, conn, chat_id=chat_id, dry_run=False, now=now, edition_id=edition_id)

    run_id = job_runs.start(conn, "monthly_production", edition_id=edition_id)
    job_runs.finish(
        conn, run_id, "failed" if result.fail_closed else "success",
        {
            "git_head": git_head, "month_label": result.month_label, "sent": result.sent,
            "idempotency_key": result.idempotency_key, "pt_window_checked_at": now_pt.isoformat(),
            "fail_closed_reason": result.fail_reason,
        },
    )

    return MonthlyProductionSummary(
        rendered_parts=result.rendered_parts, character_count=result.character_count,
        trend_count=result.trend_count, fallback_used=result.fallback_used, model_used=result.model_used,
        idempotency_key=result.idempotency_key, sent_dry_run=result.sent_dry_run, sent=result.sent,
        skipped_lock=result.skipped_lock, skipped_duplicate=result.skipped_duplicate,
        month_label=result.month_label, fail_closed=result.fail_closed, fail_reason=result.fail_reason,
        skipped_late_catchup=False, git_head=git_head, pt_window_checked_at=now_pt.isoformat(),
    )
