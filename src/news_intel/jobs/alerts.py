"""Phase 2 alert delivery-policy orchestration (design doc sections 3.2, 3.3,
7.3). All real Telegram sending stays behind the same dry_run default used
by the Phase 1 daily job -- this module decides the deterministic
pending/held/sent/suppressed state, independent of whether a message is
actually dispatched.

S2 burst merge: every S2 candidate that clears mute/quiet-hours/budget opens
or joins a 'pending_merge' batch with a fixed 10-minute window (from the
first candidate's arrival). The batch is only finalized into 'sent' once its
window has elapsed (checked lazily at the top of process_event_for_alerts /
run_alert_check), so genuinely simultaneous unrelated S2s combine into one
"Breaking Brief" and count as one message toward the daily budget. This
never risks merging two articles about the *same* development: clustering
already collapsed those into a single event upstream, so two pending_merge
members are always distinct event_ids by construction.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from news_intel import alert_queue_repo as aq
from news_intel.delivery.alert_formatter import render_breaking_brief
from news_intel.event_repo import event_sources
from news_intel.jobs import job_runs
from news_intel.models import ConfirmationState, Severity
from news_intel.pipeline.alert_policy import is_quiet_hours, next_quiet_hours_end
from news_intel.pipeline.alerts import evaluate_eligibility
from news_intel.pipeline.entities import known_entities
from news_intel.pipeline.material_facts import MaterialFactSnapshot, build_snapshot_for_event, diff_snapshots
from news_intel.runtime_state import is_muted
from news_intel.settings import AppConfig


@dataclass
class AlertActionResult:
    event_id: str
    # not_eligible | suppressed_cooldown | held_quiet_hours | suppressed_budget |
    # held_spacing | suppressed_muted | pending_merge
    action: str
    severity: str | None = None
    reason: str | None = None
    is_statement_only: bool = False
    message_hash: str | None = None


def _current_snapshot(conn: sqlite3.Connection, config: AppConfig, event_row: sqlite3.Row) -> MaterialFactSnapshot:
    known = known_entities(config.topics.groups)
    return build_snapshot_for_event(
        conn, event_row["id"],
        confirmation_state=ConfirmationState(event_row["confirmation_state"]),
        known_entities=known, alert_triggers=config.alert_triggers,
    )


def _has_material_update(last_sent: sqlite3.Row, new_snapshot: MaterialFactSnapshot) -> tuple[bool, list[str]]:
    """Safe-default: any missing/unparseable prior snapshot means 'not
    material' (no repeated alert), per the Phase 2 completion requirement."""
    raw = last_sent["material_snapshot_json"] if "material_snapshot_json" in last_sent.keys() else None
    if not raw:
        return False, ["no prior material snapshot recorded; defaulting to no repeated alert"]
    try:
        old_snapshot = MaterialFactSnapshot.model_validate(json.loads(raw))
    except (json.JSONDecodeError, ValueError):
        return False, ["prior material snapshot failed validation; defaulting to no repeated alert"]
    diff = diff_snapshots(old_snapshot, new_snapshot)
    return diff.is_material, diff.reasons


def finalize_expired_merge_batches(config: AppConfig, conn: sqlite3.Connection, now: datetime) -> list[dict]:
    """Closes any S2 burst-merge window whose 10-minute deadline has passed.
    A single-member batch finalizes as an ordinary solo alert; a multi-member
    batch is rendered as one consolidated 'Breaking Brief', preserving every
    event id and its sources, and consumes exactly one S2 budget slot."""
    finalized: list[dict] = []
    for message_hash in aq.pending_merge_batches_due(conn, now):
        members = aq.batch_members(conn, message_hash)
        if not members:
            continue
        deadline = datetime.fromisoformat(members[0]["eligible_at"])
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=UTC)
        aq.finalize_merge_batch(conn, message_hash, sent_at=deadline)

        event_ids = [m["event_id"] for m in members]
        blocks = []
        for event_id in event_ids:
            event_row = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
            sources = [s["publisher"] for s in event_sources(conn, event_id)]
            title = event_row["primary_title"] if event_row else event_id
            blocks.append(f"- {title} (Event: {event_id}) — Sources: {', '.join(sources) or 'n/a'}")
        finalized.append(
            {"message_hash": message_hash, "event_ids": event_ids, "rendered_text": render_breaking_brief(blocks)}
        )
    return finalized


def process_event_for_alerts(
    conn: sqlite3.Connection, config: AppConfig, event_row: sqlite3.Row, now: datetime
) -> AlertActionResult:
    now = now.astimezone(UTC)  # all stored timestamps/comparisons assume UTC isoformat
    event_id = event_row["id"]

    # Close out any burst-merge windows whose deadline has already passed
    # before evaluating this candidate, so budget/spacing reflect them.
    finalize_expired_merge_batches(config, conn, now)

    # Replay/restart safety: if this event already has an open (not yet
    # finalized) pending_merge row -- e.g. a re-triggered collect+alert-check
    # cycle re-evaluating the same event inside its own 10-minute window --
    # report that existing state rather than recording a second row, which
    # would double-count the event when its batch finalizes.
    already_pending = aq.open_pending_merge_for_event(conn, event_id)
    if already_pending is not None:
        return AlertActionResult(
            event_id, "pending_merge", severity=already_pending["severity"],
            message_hash=already_pending["message_hash"],
        )

    eligibility = evaluate_eligibility(conn, event_row, config)
    if not eligibility.eligible:
        return AlertActionResult(event_id, "not_eligible", reason=eligibility.reason)

    severity = eligibility.severity
    assert severity is not None
    new_snapshot = _current_snapshot(conn, config, event_row)

    last_sent = aq.last_sent_alert_for_event(conn, event_id)
    if last_sent is not None:
        last_sent_at = datetime.fromisoformat(last_sent["sent_at"])
        if last_sent_at.tzinfo is None:
            last_sent_at = last_sent_at.replace(tzinfo=UTC)
        cooldown_end = last_sent_at + timedelta(hours=config.policy.alerts.same_event_cooldown_hours)
        if now < cooldown_end:
            is_material, reasons = _has_material_update(last_sent, new_snapshot)
            if not is_material:
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
            material_snapshot_json=new_snapshot.model_dump_json(), material_version_hash=new_snapshot.version_hash(),
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

        open_batch = aq.find_open_merge_batch(conn, now)
        if open_batch is not None:
            message_hash = open_batch["message_hash"]
            deadline = datetime.fromisoformat(open_batch["eligible_at"])
            if deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=UTC)
            aq.record_alert(
                conn, event_id=event_id, severity=severity.value, state="pending_merge",
                reason=f"joined S2 burst-merge window ({message_hash})", now=now, eligible_at=deadline,
                message_hash=message_hash, material_snapshot_json=new_snapshot.model_dump_json(),
                material_version_hash=new_snapshot.version_hash(),
            )
            return AlertActionResult(
                event_id, "pending_merge", severity=severity.value,
                is_statement_only=eligibility.is_statement_only, message_hash=message_hash,
            )

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
                    material_snapshot_json=new_snapshot.model_dump_json(),
                    material_version_hash=new_snapshot.version_hash(),
                )
                return AlertActionResult(event_id, "held_spacing", severity=severity.value)

        # No open batch and clear of spacing: open a new burst-merge window.
        # It finalizes (as a solo alert, unless something else joins) once
        # its 10-minute deadline elapses -- see finalize_expired_merge_batches.
        deadline = now + timedelta(minutes=config.policy.alerts.burst_merge_minutes)
        message_hash = f"merge:{event_id}:{now.isoformat()}"
        aq.record_alert(
            conn, event_id=event_id, severity=severity.value, state="pending_merge",
            reason=eligibility.reason, now=now, eligible_at=deadline, message_hash=message_hash,
            material_snapshot_json=new_snapshot.model_dump_json(), material_version_hash=new_snapshot.version_hash(),
        )
        return AlertActionResult(
            event_id, "pending_merge", severity=severity.value,
            is_statement_only=eligibility.is_statement_only, message_hash=message_hash,
        )

    # S3: always immediate, never merged, never budget/spacing/quiet-hours gated.
    aq.record_alert(
        conn, event_id=event_id, severity=severity.value, state="sent",
        reason=eligibility.reason, now=now, sent_at=now,
        material_snapshot_json=new_snapshot.model_dump_json(), material_version_hash=new_snapshot.version_hash(),
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
        finalize_expired_merge_batches(config, conn, now)
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
