"""Matches event text against the deterministic alert triggers declared in
config/alert_triggers.yaml (design doc section 7.3, gate condition 4). A
match is necessary but never sufficient for an alert -- pipeline/alerts.py
still applies the score and confirmation gates."""

from __future__ import annotations

from dataclasses import dataclass

from news_intel.settings import AlertTriggerEntry, AlertTriggersConfig

# Rank so that, when text matches multiple triggers, the more severe /
# more specific one wins deterministically.
_SEVERITY_RANK = {"S3": 1, "S2": 0}


@dataclass
class TriggerMatch:
    trigger_id: str
    severity_ceiling: str
    market_impact_exempt: bool
    is_statement: bool
    matched_term: str


def match_trigger(text: str, config: AlertTriggersConfig) -> TriggerMatch | None:
    lowered = text.lower()
    best: TriggerMatch | None = None

    for trigger in config.triggers:
        matched_term = _find_match(text, lowered, trigger)
        if matched_term is None:
            continue
        candidate = TriggerMatch(
            trigger_id=trigger.id,
            severity_ceiling=trigger.severity_ceiling,
            market_impact_exempt=trigger.market_impact_exempt,
            is_statement=trigger.is_statement,
            matched_term=matched_term,
        )
        if best is None or _SEVERITY_RANK[candidate.severity_ceiling] > _SEVERITY_RANK[best.severity_ceiling]:
            best = candidate

    return best


def _find_match(original: str, lowered: str, trigger: AlertTriggerEntry) -> str | None:
    for term in trigger.terms_en:
        if term.lower() in lowered:
            return term
    for term in trigger.terms_zh:
        if term in original:  # Chinese terms: exact substring, case not applicable
            return term
    return None


def matches_trigger_id(text: str, config: AlertTriggersConfig, trigger_id: str) -> bool:
    """Independent check for whether one specific trigger's terms appear in
    text, regardless of which trigger `match_trigger` picked as the
    eligibility/severity representative. Used to decide statement-vs-policy
    labeling: a Trump statement about tariffs can simultaneously match a
    policy-topic trigger (for eligibility) and the statement-language
    trigger (for labeling) -- the two questions are independent."""
    lowered = text.lower()
    for trigger in config.triggers:
        if trigger.id == trigger_id:
            return _find_match(text, lowered, trigger) is not None
    return False
