"""Phase 2: the deterministic two-key alert eligibility gate (design doc
section 7.3). Code owns every decision here -- the LLM never sets
confirmation_state, severity, or whether an alert may fire; this module only
reads what pipeline/confirmation.py and pipeline/scoring.py already computed.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from news_intel.models import ConfirmationState, Severity
from news_intel.pipeline.alert_triggers import TriggerMatch, match_trigger, matches_trigger_id
from news_intel.settings import AppConfig

STATEMENT_LABEL = "Statement — policy not confirmed"


@dataclass
class AlertEligibility:
    eligible: bool
    reason: str
    severity: Severity | None = None
    trigger_id: str | None = None
    is_statement_only: bool = False


def _event_combined_text(conn: sqlite3.Connection, event_id: str) -> str:
    rows = conn.execute(
        """
        SELECT r.title AS title, r.summary_excerpt AS excerpt
        FROM event_sources es JOIN raw_items r ON r.id = es.raw_item_id
        WHERE es.event_id = ?
        """,
        (event_id,),
    ).fetchall()
    return " ".join(f"{r['title']} {r['excerpt'] or ''}" for r in rows)


def _tier_signals(conn: sqlite3.Connection, event_id: str) -> tuple[bool, bool, set[str]]:
    """Returns (has_t0, has_t1, t1_origin_families)."""
    rows = conn.execute(
        """
        SELECT DISTINCT s.tier AS tier, r.origin_family AS origin_family
        FROM event_sources es JOIN raw_items r ON r.id = es.raw_item_id JOIN sources s ON s.id = r.source_id
        WHERE es.event_id = ?
        """,
        (event_id,),
    ).fetchall()
    has_t0 = any(r["tier"] == 0 for r in rows)
    has_t1 = any(r["tier"] == 1 for r in rows)
    t1_families = {r["origin_family"] for r in rows if r["tier"] == 1}
    return has_t0, has_t1, t1_families


def evaluate_eligibility(conn: sqlite3.Connection, event_row: sqlite3.Row, config: AppConfig) -> AlertEligibility:
    thresholds = config.policy.alerts

    final_score = event_row["final_score"]
    significance = event_row["significance"]
    market_impact = event_row["market_impact"]
    confirmation_state = ConfirmationState(event_row["confirmation_state"])

    # Gate 1: final_score
    if final_score < thresholds.candidate_score:
        return AlertEligibility(False, f"final_score {final_score:.1f} < {thresholds.candidate_score}")

    # Gate 2: significance
    if significance < thresholds.candidate_significance:
        return AlertEligibility(False, f"significance {significance:.1f} < {thresholds.candidate_significance}")

    # Gate 4: a deterministic trigger must match (checked before gate 3,
    # since whether gate 3's market_impact floor is exempt depends on it)
    combined_text = _event_combined_text(conn, event_row["id"])
    trigger: TriggerMatch | None = match_trigger(combined_text, config.alert_triggers)
    if trigger is None:
        return AlertEligibility(False, "no deterministic alert trigger matched")

    # Gate 3: market_impact >= floor, OR the trigger is itself a critical
    # policy/security/geopolitical trigger that's exempt from the floor.
    if market_impact < thresholds.candidate_market_impact and not trigger.market_impact_exempt:
        return AlertEligibility(
            False,
            f"market_impact {market_impact:.1f} < {thresholds.candidate_market_impact} "
            f"and trigger {trigger.trigger_id!r} is not exempt",
        )

    # Gate 5: confirmation official/confirmed, except the verified-statement
    # exception. A lone T3 source (e.g. a social mirror) never qualifies,
    # for either the normal path or the statement exception.
    #
    # Whether this event *describes a statement* is checked independently of
    # which trigger won gate 4: text can simultaneously match a policy-topic
    # trigger (e.g. "tariff") and statement language (e.g. "said", "posted"),
    # and the statement label must still apply when no T0 document backs it.
    has_t0, has_t1, t1_families = _tier_signals(conn, event_row["id"])
    statement_language_present = matches_trigger_id(
        combined_text, config.alert_triggers, "verified_official_statement"
    )
    is_statement_only = statement_language_present and confirmation_state != ConfirmationState.OFFICIAL

    if is_statement_only:
        statement_verified = has_t0 or has_t1 or len(t1_families) >= 2
        if not statement_verified:
            return AlertEligibility(
                False,
                "verified-statement exception requires a direct T0/T1 source or two independent "
                "T1 reports; a T3 mirror/discovery source alone never qualifies",
            )
    elif confirmation_state not in (ConfirmationState.OFFICIAL, ConfirmationState.CONFIRMED):
        return AlertEligibility(
            False, f"confirmation_state {confirmation_state.value!r} is neither official nor confirmed"
        )

    severity = Severity.S3 if trigger.severity_ceiling == "S3" else Severity.S2
    return AlertEligibility(
        True,
        f"trigger={trigger.trigger_id}",
        severity=severity,
        trigger_id=trigger.trigger_id,
        is_statement_only=is_statement_only,
    )
