"""Phase 2 alert delivery-policy orchestration (design doc sections 3.2, 3.3,
7.3). All real Telegram sending stays behind the same dry_run default used
by the Phase 1 daily job -- this module decides the deterministic
pending/held/sent/suppressed state, independent of whether a message is
actually dispatched.

Known Phase 2 simplification: the 10-minute "burst merge" of unrelated S2
candidates arriving close together is not implemented as message-combining;
only the 30-minute minimum-spacing rule is enforced (an S2 arriving inside
the spacing window is held, not merged into another message).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from news_intel import alert_queue_repo as aq
from news_intel.event_repo import event_sources
from news_intel.jobs import job_runs
from news_intel.models import ConfirmationState, Severity
from news_intel.pipeline.alert_policy import is_quiet_hours, next_quiet_hours_end
from news_intel.pipeline.alerts import evaluate_eligibility
from news_intel.runtime_state import is_muted
from news_intel.settings import AppConfig


@dataclass
class AlertActionResult:
    event_id: str
    action: str  # not_eligible | suppressed_cooldown | held_quiet_hours | suppressed_budget | held_spacing | sent
    severity: str | None = None
    reason: str | None = None
    is_statement_only: bool = False


def process_event_for_alerts(
    conn: sqlite3.Connection, config: AppConfig, event_row: sqlite3.Row, now: datetime
) -> AlertActionResult:
    now = now.astimezone(UTC)  # all stored timestamps/comparisons assume UTC isoformat
    event_id = event_row["id"]
    eligibility = evaluate_eligibility(conn, event_row, config)
    if not eligibility.eligible:
        return AlertActionResult(event_id, "not_eligible", reason=eligibility.reason)

    severity = eligibility.severity
    assert severity is not None

    last_sent = aq.last_sent_alert_for_event(conn, event_id)
    if last_sent is not None:
        last_sent_at = datetime.fromisoformat(last_sent["sent_at"])
        if last_sent_at.tzinfo is None:
            last_sent_at = last_sent_at.replace(tzinfo=UTC)
        cooldown_end = last_sent_at + timedelta(hours=config.policy.alerts.same_event_cooldown_hours)
        if now < cooldown_end and not aq.has_confirmation_material_update_since(conn, event_id, last_sent_at):
            aq.record_alert(
                conn, event_id=event_id, severity=severity.value, state="suppressed",
                reason="same-event cooldown active, no material update", now=now,
            )
            return AlertActionResult(event_id, "suppressed_cooldown", severity=severity.value)

    if severity == Severity.S2 and is_muted(conn, now):
        aq.record_alert(
            conn, event_id=event_id, severity=severity.value, state="suppressed",
            reason="user /mute active; S3 would still interrupt", now=now,
        )
        return AlertActionResult(event_id, "suppressed_muted", severity=severity.value)

    now_pt = now.astimezone(ZoneInfo(config.policy.timezone))

    if severity == Severity.S2 and is_quiet_hours(now_pt.time(), config.policy.quiet_hours):
        eligible_at = next_quiet_hours_end(now_pt, config.policy.quiet_hours)
        aq.record_alert(
            conn, event_id=event_id, severity=severity.value, state="held",
            reason="quiet hours; deferred to consolidated overnight release", now=now,
            eligible_at=eligible_at.astimezone(UTC),
        )
        return AlertActionResult(event_id, "held_quiet_hours", severity=severity.value)

    if severity == Severity.S2:
        sent_today = aq.count_sent_s2_on_pt_date(conn, now_pt.date(), config.policy.timezone)
        if sent_today >= config.policy.alerts.s2_daily_budget:
            aq.record_alert(
                conn, event_id=event_id, severity=severity.value, state="suppressed",
                reason=f"daily S2 budget ({config.policy.alerts.s2_daily_budget}) reached; deferred to digest",
                now=now,
            )
            return AlertActionResult(event_id, "suppressed_budget", severity=severity.value)

        last_other = aq.last_sent_s2_excluding_event(conn, event_id)
        if last_other is not None:
            last_other_at = datetime.fromisoformat(last_other["sent_at"])
            if last_other_at.tzinfo is None:
                last_other_at = last_other_at.replace(tzinfo=UTC)
            spacing_end = last_other_at + timedelta(minutes=config.policy.alerts.minimum_spacing_minutes)
            if now < spacing_end:
                aq.record_alert(
                    conn, event_id=event_id, severity=severity.value, state="held",
                    reason="minimum inter-alert spacing not yet elapsed", now=now, eligible_at=spacing_end,
                )
                return AlertActionResult(event_id, "held_spacing", severity=severity.value)

    aq.record_alert(
        conn, event_id=event_id, severity=severity.value, state="sent",
        reason=eligibility.reason, now=now, sent_at=now,
    )
    return AlertActionResult(
        event_id, "sent", severity=severity.value, is_statement_only=eligibility.is_statement_only
    )


def run_alert_check(
    config: AppConfig, conn: sqlite3.Connection, *, now: datetime | None = None
) -> list[AlertActionResult]:
    """Evaluates every non-disputed, non-suppressed-forever event that hasn't
    already reached a terminal 'sent' decision this run. Real jobs call this
    right after collect/scoring; it never sends network requests itself."""
    now = (now or datetime.now(UTC)).astimezone(UTC)
    run_id = job_runs.start(conn, "alerts")
    results: list[AlertActionResult] = []
    try:
        rows = conn.execute(
            "SELECT * FROM events WHERE status != 'resolved' ORDER BY final_score DESC"
        ).fetchall()
        for row in rows:
            results.append(process_event_for_alerts(conn, config, row, now))
        job_runs.finish(
            conn, run_id, "success",
            {"evaluated": len(results), "sent": sum(1 for r in results if r.action == "sent")},
        )
    except Exception as exc:  # noqa: BLE001
        job_runs.finish(conn, run_id, "failed", {"error": str(exc)})
        raise
    return results


@dataclass
class ReleaseResult:
    released_event_ids: list[str]
    dropped_event_ids: list[str]
    rendered_text: str | None


def release_held_alerts(config: AppConfig, conn: sqlite3.Connection, *, now: datetime | None = None) -> ReleaseResult:
    """06:30 America/Los_Angeles consolidated release (design doc section
    3.3). Re-checks eligibility for every held alert so a reversed/corrected
    event is dropped rather than sent stale; the whole batch counts as one
    message toward the day's S2 budget via a shared message_hash."""
    now = (now or datetime.now(UTC)).astimezone(UTC)
    due = aq.held_alerts_due(conn, now)

    released_ids: list[str] = []
    dropped_ids: list[str] = []
    blocks: list[str] = []
    alert_ids_to_release: list[int] = []

    for held in due:
        event_id = held["event_id"]
        event_row = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
        if event_row is None or ConfirmationState(event_row["confirmation_state"]) == ConfirmationState.DISPUTED:
            dropped_ids.append(event_id)
            aq.mark_state(conn, held["id"], "suppressed")
            continue

        eligibility = evaluate_eligibility(conn, event_row, config)
        if not eligibility.eligible:
            dropped_ids.append(event_id)
            aq.mark_state(conn, held["id"], "suppressed")
            continue

        released_ids.append(event_id)
        alert_ids_to_release.append(held["id"])
        sources = [s["publisher"] for s in event_sources(conn, event_id)]
        blocks.append(
            f"- {event_row['primary_title']} (Event: {event_id}) — Sources: {', '.join(sources) or 'n/a'}"
        )

    rendered_text = None
    if alert_ids_to_release:
        pt_date = now.astimezone(ZoneInfo(config.policy.timezone)).strftime("%Y-%m-%d")
        message_hash = f"overnight:{pt_date}"
        aq.release_as_batch(conn, alert_ids_to_release, message_hash=message_hash, sent_at=now)
        rendered_text = "Overnight Important Update\n\n" + "\n".join(blocks)

    return ReleaseResult(released_event_ids=released_ids, dropped_event_ids=dropped_ids, rendered_text=rendered_text)
