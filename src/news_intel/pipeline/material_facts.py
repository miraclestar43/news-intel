"""Structured material-fact snapshot + deterministic diff (Phase 2
completion item 2). Code owns this entirely: fields are extracted with
stable, wording-insensitive rules (not raw sentence snippets) so a cosmetic
paraphrase, an extra corroborating source, or a reworded summary never
registers as a material change. An LLM may later *propose* a snapshot, but
validate_llm_snapshot() always overwrites confirmation_status with the
code-computed value and returns None (safe default: no repeated alert) on
any validation failure -- the caller must never authorize a repeat alert
from an unvalidated proposal.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass, field

from pydantic import BaseModel, ValidationError

from news_intel.models import ConfirmationState
from news_intel.pipeline.alert_triggers import match_trigger
from news_intel.pipeline.entities import extract_known_entities
from news_intel.settings import AlertTriggersConfig

_AMOUNT_RE = re.compile(
    r"(\d+(?:\.\d+)?\s?%|\$\s?\d[\d,]*(?:\.\d+)?\s?(?:billion|million|trillion)?)", re.IGNORECASE
)
_EFFECTIVE_DATE_RE = re.compile(
    r"(?:effective|starting|beginning|take effect|takes effect|takes hold)\s+(?:on\s+)?"
    r"([A-Za-z]+ \d{1,2},? \d{4}|\d{4}-\d{2}-\d{2}|next month|next week|next year|immediately)",
    re.IGNORECASE,
)
_REVERSAL_TERMS = ("reversed", "revoked", "rescinded", "withdrawn", "cancelled", "canceled", "struck down")


class MaterialFactSnapshot(BaseModel):
    action: str | None = None  # stable: matched trigger id, not raw headline wording
    actor: str | None = None  # stable: first known-vocabulary entity match
    target_entities: list[str] = []  # stable: known-vocabulary matches only
    amount: str | None = None
    effective_date: str | None = None
    confirmation_status: str  # code-owned/authoritative -- never trust an LLM value here
    reversed: bool = False
    trigger_severity: str | None = None  # "S2" / "S3", for escalation detection

    def version_hash(self) -> str:
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()


@dataclass
class MaterialDiffResult:
    is_material: bool
    reasons: list[str] = field(default_factory=list)


def build_snapshot(
    combined_text: str,
    *,
    confirmation_state: ConfirmationState,
    known_entities: set[str],
    alert_triggers: AlertTriggersConfig,
) -> MaterialFactSnapshot:
    trigger = match_trigger(combined_text, alert_triggers)
    known_matches = extract_known_entities(combined_text, known_entities)
    amount_match = _AMOUNT_RE.search(combined_text)
    date_match = _EFFECTIVE_DATE_RE.search(combined_text)
    is_reversed = any(term in combined_text.lower() for term in _REVERSAL_TERMS)

    return MaterialFactSnapshot(
        action=trigger.trigger_id if trigger else None,
        actor=known_matches[0] if known_matches else None,
        target_entities=known_matches,
        amount=amount_match.group(1) if amount_match else None,
        effective_date=date_match.group(1) if date_match else None,
        confirmation_status=confirmation_state.value,
        reversed=is_reversed,
        trigger_severity=trigger.severity_ceiling if trigger else None,
    )


def build_snapshot_for_event(
    conn: sqlite3.Connection,
    event_id: str,
    *,
    confirmation_state: ConfirmationState,
    known_entities: set[str],
    alert_triggers: AlertTriggersConfig,
) -> MaterialFactSnapshot:
    rows = conn.execute(
        """
        SELECT r.title AS title, r.summary_excerpt AS excerpt
        FROM event_sources es JOIN raw_items r ON r.id = es.raw_item_id
        WHERE es.event_id = ?
        """,
        (event_id,),
    ).fetchall()
    combined_text = " ".join(f"{r['title']} {r['excerpt'] or ''}" for r in rows)
    return build_snapshot(
        combined_text, confirmation_state=confirmation_state, known_entities=known_entities,
        alert_triggers=alert_triggers,
    )


def diff_snapshots(old: MaterialFactSnapshot, new: MaterialFactSnapshot) -> MaterialDiffResult:
    if old.version_hash() == new.version_hash():
        return MaterialDiffResult(False, [])

    reasons: list[str] = []

    if old.confirmation_status != new.confirmation_status:
        reasons.append(f"confirmation status changed: {old.confirmation_status} -> {new.confirmation_status}")

    if new.effective_date is not None and new.effective_date != old.effective_date:
        reasons.append(f"effective date changed: {old.effective_date!r} -> {new.effective_date!r}")

    if new.amount is not None and new.amount != old.amount:
        reasons.append(f"amount/impact changed: {old.amount!r} -> {new.amount!r}")

    newly_affected = set(new.target_entities) - set(old.target_entities)
    if newly_affected:
        reasons.append(f"newly affected entity/target: {sorted(newly_affected)}")

    if new.actor is not None and new.actor != old.actor:
        reasons.append(f"actor changed: {old.actor!r} -> {new.actor!r}")

    if new.reversed and not old.reversed:
        reasons.append("reversal/cancellation detected")

    if old.trigger_severity == "S2" and new.trigger_severity == "S3":
        reasons.append("severity escalated S2 -> S3")

    return MaterialDiffResult(is_material=bool(reasons), reasons=reasons)


def validate_llm_snapshot(raw: dict, *, code_confirmation_state: ConfirmationState) -> MaterialFactSnapshot | None:
    """Validates an LLM-proposed structured-fact dict. Always overwrites
    confirmation_status with the code-computed value -- the LLM never gets
    to elevate confirmation state or, by extension, authorize a repeated
    alert through it. Returns None (safe default: no repeated alert) if the
    proposal doesn't validate."""
    try:
        snapshot = MaterialFactSnapshot.model_validate(raw)
    except ValidationError:
        return None
    return snapshot.model_copy(update={"confirmation_status": code_confirmation_state.value})
