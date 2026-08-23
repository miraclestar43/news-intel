"""Production daily entrypoint: `pi-news production daily`.

2026-08-23 repair: this module didn't exist -- the production-specific
logic (PRODUCTION_START_DATE gate, real chat_id resolution, dry_run=False)
lived directly inline in cli.py's production_daily(), the only place that
enforced it. Extracted here to mirror jobs/weekly_production.py's and
jobs/monthly_production.py's shape exactly, and to gain the same
authorization-gated contract (jobs/delivery_authorization.py) -- daily,
weekly, and monthly now all require the same explicit, mandatory capability
before any of them will resolve a real chat_id or construct a
TelegramSender, independent of what credentials happen to be present."""

from __future__ import annotations

import sqlite3
import subprocess
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from news_intel.jobs.daily import DailySummary, run_daily
from news_intel.jobs.delivery_authorization import ProductionDeliveryAuthorization
from news_intel.settings import AppConfig

PACKAGE_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = PACKAGE_DIR.parent.parent


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
class DailyProductionSummary(DailySummary):
    skipped_before_start_date: bool = False
    git_head: str = ""
    pt_date_checked: str = ""


def run_daily_production(
    config: AppConfig,
    conn: sqlite3.Connection,
    *,
    start_date: date,
    now: datetime | None = None,
    edition_id: str = "en_global",
    authorization: ProductionDeliveryAuthorization | None = None,
) -> DailyProductionSummary:
    """`authorization` is required for any real send -- see
    jobs/delivery_authorization.py. Omitting it (the default) still fully
    constructs and renders the digest (chat_id=None is already structurally
    non-delivering inside run_daily()) but returns before ever resolving a
    chat_id or constructing a TelegramSender, independent of what
    credentials are present in `config`."""
    now = now or datetime.now(UTC)
    edition = config.editions.get(edition_id)
    tz = ZoneInfo(edition.schedule.timezone)
    pt_today = now.astimezone(tz).date()
    git_head = _current_git_head()

    if pt_today < start_date:
        # Deliberately before run_daily() is ever called -- no job_runs/
        # digest/delivery DB row, no Telegram contact.
        return DailyProductionSummary(
            rendered_text="", character_count=0, top3_count=0, quick_brief_count=0,
            fallback_used=False, model_used="", idempotency_key="", sent_dry_run=True, sent=False,
            skipped_before_start_date=True, git_head=git_head, pt_date_checked=pt_today.isoformat(),
        )

    if authorization is None:
        preview = run_daily(config, conn, chat_id=None, dry_run=True, now=now, edition_id=edition_id)
        return DailyProductionSummary(
            rendered_text=preview.rendered_text, character_count=preview.character_count,
            top3_count=preview.top3_count, quick_brief_count=preview.quick_brief_count,
            fallback_used=preview.fallback_used, model_used=preview.model_used,
            idempotency_key=preview.idempotency_key, sent_dry_run=True, sent=False,
            skipped_lock=preview.skipped_lock, skipped_sunday=preview.skipped_sunday,
            skipped_monthly_slot=preview.skipped_monthly_slot,
            fail_closed=preview.fail_closed, fail_reason=preview.fail_reason,
            skipped_before_start_date=False, git_head=git_head, pt_date_checked=pt_today.isoformat(),
        )

    allowed = config.secrets.allowed_chat_id_list
    chat_id = allowed[0] if len(allowed) == 1 else -1
    result = run_daily(config, conn, chat_id=chat_id, dry_run=False, now=now, edition_id=edition_id)

    return DailyProductionSummary(
        rendered_text=result.rendered_text, character_count=result.character_count,
        top3_count=result.top3_count, quick_brief_count=result.quick_brief_count,
        fallback_used=result.fallback_used, model_used=result.model_used,
        idempotency_key=result.idempotency_key, sent_dry_run=result.sent_dry_run, sent=result.sent,
        skipped_lock=result.skipped_lock, skipped_sunday=result.skipped_sunday,
        skipped_monthly_slot=result.skipped_monthly_slot,
        fail_closed=result.fail_closed, fail_reason=result.fail_reason,
        skipped_before_start_date=False, git_head=git_head, pt_date_checked=pt_today.isoformat(),
    )
