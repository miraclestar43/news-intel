"""Phase 2 completion item 2: structured material-fact snapshot + diff.

Case 1 ("formal confirmation after a statement is material") is already
proven end-to-end in tests/acceptance/test_phase2_alert_scenarios.py
(scenario 7) and tests/unit/test_burst_merge.py's cooldown interplay; this
file covers the remaining five required cases plus validation-failure
safe-defaulting.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from news_intel.jobs.alerts import process_event_for_alerts
from news_intel.models import ConfirmationState
from news_intel.pipeline.material_facts import (
    MaterialFactSnapshot,
    build_snapshot,
    diff_snapshots,
    validate_llm_snapshot,
)
from tests.acceptance.test_phase2_alert_scenarios import _deliver, _seed_event


def test_changed_effective_date_is_material(config):
    known = set()
    old = build_snapshot(
        "BIS export control rule takes effect next month",
        confirmation_state=ConfirmationState.OFFICIAL, known_entities=known, alert_triggers=config.alert_triggers,
    )
    new = build_snapshot(
        "BIS export control rule takes effect on January 15, 2027",
        confirmation_state=ConfirmationState.OFFICIAL, known_entities=known, alert_triggers=config.alert_triggers,
    )
    assert old.effective_date != new.effective_date
    diff = diff_snapshots(old, new)
    assert diff.is_material is True
    assert any("effective date" in r for r in diff.reasons)


def test_changed_amount_or_target_is_material(config):
    known = {"NVIDIA", "AMD"}
    old = build_snapshot(
        "NVIDIA export control rule affects 10% of chip shipments",
        confirmation_state=ConfirmationState.OFFICIAL, known_entities=known, alert_triggers=config.alert_triggers,
    )
    new_amount = build_snapshot(
        "NVIDIA export control rule affects 25% of chip shipments",
        confirmation_state=ConfirmationState.OFFICIAL, known_entities=known, alert_triggers=config.alert_triggers,
    )
    diff_amount = diff_snapshots(old, new_amount)
    assert diff_amount.is_material is True
    assert any("amount" in r for r in diff_amount.reasons)

    new_target = build_snapshot(
        "NVIDIA and AMD export control rule affects 10% of chip shipments",
        confirmation_state=ConfirmationState.OFFICIAL, known_entities=known, alert_triggers=config.alert_triggers,
    )
    diff_target = diff_snapshots(old, new_target)
    assert diff_target.is_material is True
    assert any("newly affected" in r for r in diff_target.reasons)


def test_reversal_and_escalation_are_material(config):
    known: set[str] = set()
    old = build_snapshot(
        "BIS export control rule on chips",
        confirmation_state=ConfirmationState.OFFICIAL, known_entities=known, alert_triggers=config.alert_triggers,
    )
    reversed_new = build_snapshot(
        "BIS export control rule on chips was revoked",
        confirmation_state=ConfirmationState.OFFICIAL, known_entities=known, alert_triggers=config.alert_triggers,
    )
    diff_reversal = diff_snapshots(old, reversed_new)
    assert diff_reversal.is_material is True
    assert any("reversal" in r for r in diff_reversal.reasons)

    old_s2 = build_snapshot(
        "BIS export control rule on chips",
        confirmation_state=ConfirmationState.OFFICIAL, known_entities=known, alert_triggers=config.alert_triggers,
    )
    escalated_new = build_snapshot(
        "Systemic financial instability triggers broad market shutdown following the rule",
        confirmation_state=ConfirmationState.OFFICIAL, known_entities=known, alert_triggers=config.alert_triggers,
    )
    diff_escalation = diff_snapshots(old_s2, escalated_new)
    assert diff_escalation.is_material is True
    assert any("escalat" in r for r in diff_escalation.reasons)


def test_cosmetic_wording_or_extra_source_is_not_material(config):
    known = {"NVIDIA"}
    old = build_snapshot(
        "NVIDIA export control rule on chips announced by BIS",
        confirmation_state=ConfirmationState.OFFICIAL, known_entities=known, alert_triggers=config.alert_triggers,
    )
    # Same structured facts, entirely different wording/extra corroborating
    # source text appended -- nothing that changes date/amount/target/
    # confirmation/reversal/severity.
    new = build_snapshot(
        "Wire report: sources confirm NVIDIA's chips are now subject to the "
        "previously announced BIS export control rule, per multiple outlets.",
        confirmation_state=ConfirmationState.OFFICIAL, known_entities=known, alert_triggers=config.alert_triggers,
    )
    diff = diff_snapshots(old, new)
    assert diff.is_material is False
    assert diff.reasons == []


def test_same_material_version_cannot_be_delivered_twice(config, conn):
    now = datetime(2026, 7, 31, 15, 0, tzinfo=UTC)
    _seed_event(
        conn, event_id="E-MATVER", title="BIS export control rule on chips",
        excerpt="BIS announced an export control rule on chips.",
        sources=[("bis_matver", 0, "bis.gov")], now=now,
    )
    row = conn.execute("SELECT * FROM events WHERE id='E-MATVER'").fetchone()
    first = _deliver(conn, config, row, now)
    assert first.action == "sent"

    # Nothing about the event actually changed; re-evaluate well within the
    # 6-hour cooldown. Even calling repeatedly must never deliver twice.
    later = now + timedelta(hours=1)
    for _ in range(3):
        row = conn.execute("SELECT * FROM events WHERE id='E-MATVER'").fetchone()
        result = process_event_for_alerts(conn, config, row, later)
        assert result.action == "suppressed_cooldown"

    sent_count = conn.execute(
        "SELECT COUNT(*) AS n FROM alert_queue WHERE event_id='E-MATVER' AND state='sent'"
    ).fetchone()["n"]
    assert sent_count == 1


def test_validate_llm_snapshot_always_uses_code_confirmation_state(config):
    proposed = {
        "action": "export_control_or_sanction",
        "actor": "NVIDIA",
        "target_entities": ["NVIDIA"],
        "amount": "10%",
        "effective_date": "next month",
        "confirmation_status": "official",  # LLM tries to claim official
        "reversed": False,
        "trigger_severity": "S2",
    }
    validated = validate_llm_snapshot(proposed, code_confirmation_state=ConfirmationState.REPORTED)
    assert validated is not None
    assert validated.confirmation_status == "reported"  # code's value always wins


def test_validate_llm_snapshot_rejects_malformed_proposal():
    malformed = {"reversed": "not-a-boolean-at-all-but-a-sentence"}
    result = validate_llm_snapshot(malformed, code_confirmation_state=ConfirmationState.OFFICIAL)
    assert result is None  # safe default: caller must treat as no material update


def test_identical_snapshots_hash_equal_and_are_not_material():
    a = MaterialFactSnapshot(confirmation_status="official", target_entities=["NVIDIA"])
    b = MaterialFactSnapshot(confirmation_status="official", target_entities=["NVIDIA"])
    assert a.version_hash() == b.version_hash()
    assert diff_snapshots(a, b).is_material is False
