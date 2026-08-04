"""Phase 3: pure analysis functions over an already-fetched window of
events -- prior-week comparison, noise vs. signal, unresolved-story
tracking, and Trump-statement-vs-formal-policy classification. Kept DB-free
(besides the trend clusters passed in) so window logic is trivially
unit-testable."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from news_intel.pipeline.alert_triggers import matches_trigger_id
from news_intel.pipeline.trends import TrendCluster
from news_intel.settings import AlertTriggersConfig


@dataclass
class TrendComparison:
    new: list[TrendCluster] = field(default_factory=list)
    continuing: list[dict] = field(default_factory=list)  # {current, prior, strength_delta}
    resolved: list[TrendCluster] = field(default_factory=list)


def compare_with_prior_week(current: list[TrendCluster], prior: list[TrendCluster]) -> TrendComparison:
    """Continuity between weeks is judged on the same specific (non-generic)
    shared_anchor used to build the trend in the first place -- Gate A
    remediation Phase 4 -- never on the broader key_entities list, which
    can include generic terms that would otherwise link two genuinely
    unrelated week-over-week trends."""
    comparison = TrendComparison()
    matched_prior_ids: set[str] = set()

    for cluster in current:
        anchor = set(cluster.comparison_anchor)
        match = next(
            (
                p for p in prior
                if p.category == cluster.category and anchor and (set(p.comparison_anchor) & anchor)
            ),
            None,
        )
        if match is None:
            comparison.new.append(cluster)
        else:
            matched_prior_ids.add(match.id)
            comparison.continuing.append(
                {"current": cluster, "prior": match, "strength_delta": cluster.strength - match.strength}
            )

    comparison.resolved = [p for p in prior if p.id not in matched_prior_ids]
    return comparison


def identify_noise_vs_signal(
    events: list[sqlite3.Row], *, signal_threshold: float, noise_threshold: float
) -> tuple[list[sqlite3.Row], list[sqlite3.Row]]:
    """Noise: disputed, or scored below the noise floor and never
    corroborated further. Signal: cleared the daily Top-3 bar."""
    noise = [e for e in events if e["confirmation_state"] == "disputed" or e["final_score"] < noise_threshold]
    signal = [e for e in events if e["final_score"] >= signal_threshold]
    return noise, signal


def unresolved_stories(events: list[sqlite3.Row]) -> list[sqlite3.Row]:
    return [
        e for e in events
        if e["status"] == "developing" and e["confirmation_state"] in ("unverified", "reported")
    ]


def classify_statement_vs_policy(
    conn: sqlite3.Connection, events: list[sqlite3.Row], alert_triggers: AlertTriggersConfig
) -> tuple[list[sqlite3.Row], list[sqlite3.Row]]:
    """Returns (became_formal_policy, remained_statement_only) among events
    whose text ever matched statement language (e.g. a Trump statement)."""
    became_policy: list[sqlite3.Row] = []
    remained_statement: list[sqlite3.Row] = []

    for event in events:
        rows = conn.execute(
            """
            SELECT r.title AS title, r.summary_excerpt AS excerpt
            FROM event_sources es JOIN raw_items r ON r.id = es.raw_item_id
            WHERE es.event_id = ?
            """,
            (event["id"],),
        ).fetchall()
        text = " ".join(f"{r['title']} {r['excerpt'] or ''}" for r in rows)
        if not matches_trigger_id(text, alert_triggers, "verified_official_statement"):
            continue
        if event["confirmation_state"] == "official":
            became_policy.append(event)
        else:
            remained_statement.append(event)

    return became_policy, remained_statement
