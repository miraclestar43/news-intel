"""Renders the Breaking Alert Telegram template (design doc section 4.1).
Deterministic/rule-based -- Phase 2 alerts do not depend on an LLM call, so
alerting still works even during a MiMo outage."""

from __future__ import annotations

from news_intel.models import ConfirmationState, Severity
from news_intel.pipeline.alerts import STATEMENT_LABEL

_STATUS_BY_CONFIRMATION = {
    ConfirmationState.OFFICIAL: "Confirmed",
    ConfirmationState.CONFIRMED: "Confirmed",
    ConfirmationState.REPORTED: "Reported",
    ConfirmationState.UNVERIFIED: "Reported",
    ConfirmationState.DISPUTED: "Disputed",
}


def render_alert(
    *,
    event_id: str,
    category: str,
    english_headline: str,
    chinese_headline: str,
    what_happened: str,
    why_it_matters: str,
    chinese_takeaway: str,
    confirmation_state: ConfirmationState,
    severity: Severity,
    is_statement_only: bool,
    sources: list[str],
) -> str:
    marker = "🚨 CRITICAL" if severity == Severity.S3 else "🚨 MAJOR"
    status = STATEMENT_LABEL if is_statement_only else _STATUS_BY_CONFIRMATION[confirmation_state]

    lines = [
        f"{marker} — {category}",
        "",
        english_headline,
        chinese_headline,
        "",
        f"What happened: {what_happened}",
        f"Why it matters: {why_it_matters}",
        f"Status: {status}",
        "",
        f"中文要点：{chinese_takeaway}",
        f"Sources: {' · '.join(sources)}" if sources else "Sources: (none listed)",
        f"Event: {event_id}",
    ]
    return "\n".join(lines)


def render_breaking_brief(blocks: list[str]) -> str:
    """Combines multiple S2 alerts that coalesced within the 10-minute
    burst-merge window (design doc section 3.2) into one message. Each
    block must already identify its own Event ID and sources -- this
    function only adds the shared header, it never drops or renumbers an
    event."""
    if len(blocks) == 1:
        return blocks[0]
    header = "🚨 MAJOR — Breaking Brief (multiple developments)"
    return "\n\n".join([header, *blocks])
