"""Daily brief job (design doc sections 3.4, 4.2, 8.4). Runs Mon-Sat 22:00
America/Los_Angeles via systemd timer. Selection, scoring, and delivery
policy are deterministic; only composition text comes from the LLM, with a
documented deterministic fallback if it is unavailable."""

from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from news_intel import db
from news_intel.delivery.formatter import render_daily
from news_intel.delivery.telegram import ChatNotAllowedError, TelegramSender
from news_intel.event_repo import (
    event_as_digest_dict,
    last_digest_window_end,
    qualified_events_since,
    record_digest,
)
from news_intel.jobs import job_runs
from news_intel.llm.errors import LLMUnavailableError
from news_intel.llm.fallback import fallback_daily_digest
from news_intel.llm.provider import get_provider
from news_intel.models import DailyDigestInput
from news_intel.scheduled_repo import upcoming
from news_intel.settings import AppConfig


@dataclass
class DailySummary:
    rendered_text: str
    character_count: int
    top3_count: int
    quick_brief_count: int
    fallback_used: bool
    model_used: str
    idempotency_key: str
    sent_dry_run: bool
    sent: bool
    skipped_lock: bool = False


def _select_events(conn: sqlite3.Connection, config: AppConfig, now: datetime):
    thresholds = config.policy.scoring.thresholds
    window_start = last_digest_window_end(conn, "daily") or (now - timedelta(hours=24))
    qualified = qualified_events_since(conn, window_start=window_start, min_score=thresholds.daily_quick_brief)

    top3 = [r for r in qualified if r["final_score"] >= thresholds.daily_top3][:3]
    top3_ids = {r["id"] for r in top3}
    remaining_budget = max(0, config.policy.digests.daily_max_events - len(top3))
    quick = [r for r in qualified if r["id"] not in top3_ids][:remaining_budget]
    return window_start, top3, quick


def run_daily(
    config: AppConfig,
    conn: sqlite3.Connection,
    *,
    chat_id: int | None = None,
    dry_run: bool = True,
    now: datetime | None = None,
) -> DailySummary:
    now = now or datetime.now(UTC)

    if not db.acquire_job_lock(conn, "daily"):
        return DailySummary("", 0, 0, 0, False, "", "", True, False, skipped_lock=True)

    run_id = job_runs.start(conn, "daily")
    try:
        window_start, top3_rows, quick_rows = _select_events(conn, config, now)
        top3 = [event_as_digest_dict(conn, r) for r in top3_rows]
        quick = [event_as_digest_dict(conn, r) for r in quick_rows]
        calendar_items = [
            {"title": row["title"], "scheduled_at": row["scheduled_at"]} for row in upcoming(conn)
        ]

        pt_now = now.astimezone(ZoneInfo(config.policy.timezone))
        date_label = pt_now.strftime("%b %-d")
        date_iso = pt_now.strftime("%Y-%m-%d")

        provider = get_provider(config)
        model_used = "none (deterministic fallback)"
        fallback_used = False
        rendered_text: str

        try:
            digest_input = DailyDigestInput(
                date_label=date_label,
                top3_candidates=top3,
                quick_brief_candidates=quick,
                calendar_candidates=calendar_items,
            )
            output = provider.compose_daily(digest_input)
            for ev, source_dict in zip(output.structured.get("top3", []), top3, strict=False):
                ev.setdefault("sources", [s["publisher"] for s in source_dict["sources"]])
            for ev, source_dict in zip(output.structured.get("quick_briefs", []), quick, strict=False):
                ev.setdefault("sources", [s["publisher"] for s in source_dict["sources"]])
            rendered_text = render_daily(date_label, output.structured)
            model_used = getattr(provider, "model", "llm")
        except LLMUnavailableError:
            fallback_input = DailyDigestInput(
                date_label=date_label,
                top3_candidates=top3,
                quick_brief_candidates=quick,
                calendar_candidates=calendar_items,
            )
            fallback_output = fallback_daily_digest(fallback_input, date_label)
            rendered_text = fallback_output.rendered_text
            fallback_used = True

        chat_suffix = ""
        sent = False
        sent_dry_run = True
        if chat_id is not None:
            from news_intel.delivery.telegram import chat_id_hash

            chat_suffix = chat_id_hash(chat_id)
        idempotency_key = f"daily:{date_iso}:{chat_suffix or 'no-chat'}"

        if chat_id is not None:
            sender = TelegramSender(config.secrets.telegram_bot_token, config.secrets.allowed_chat_id_list)
            try:
                result = asyncio.run(
                    sender.send(
                        conn,
                        chat_id=chat_id,
                        text=rendered_text,
                        kind="daily",
                        idempotency_key=idempotency_key,
                        max_chars=config.policy.digests.telegram_hard_limit_chars,
                        dry_run=dry_run,
                    )
                )
                sent = not result.dry_run
                sent_dry_run = result.dry_run
            except ChatNotAllowedError:
                sent = False
                sent_dry_run = True

        record_digest(
            conn,
            kind="daily",
            window_start=window_start,
            window_end=now,
            rendered_text=rendered_text,
            model_used=model_used,
            fallback_used=fallback_used,
            event_ids_top3=[t["event_id"] for t in top3],
            event_ids_quick=[q["event_id"] for q in quick],
        )

        job_runs.finish(
            conn,
            run_id,
            "success",
            {"top3": len(top3), "quick_briefs": len(quick), "fallback_used": fallback_used},
        )

        return DailySummary(
            rendered_text=rendered_text,
            character_count=len(rendered_text),
            top3_count=len(top3),
            quick_brief_count=len(quick),
            fallback_used=fallback_used,
            model_used=model_used,
            idempotency_key=idempotency_key,
            sent_dry_run=sent_dry_run,
            sent=sent,
        )
    except Exception as exc:  # noqa: BLE001
        job_runs.finish(conn, run_id, "failed", {"error": str(exc)})
        raise
    finally:
        db.release_job_lock(conn, "daily")
