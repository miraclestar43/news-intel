"""Stage 6: rule scoring. Produces the ScoreComponents that feed final_score
(models.ScoreComponents.final_score). MiMo may later propose significance/
market_impact values with reasons, but code always clamps and this module
owns the numbers actually stored."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

from news_intel.models import ConfirmationState, ScoreComponents, Severity
from news_intel.pipeline.relevance import classify_relevance
from news_intel.settings import AppConfig

_CATEGORY_LABELS = {
    "ai_semiconductor_export_controls": "AI & Semiconductors",
    "tech_platforms_datacenter": "Technology",
    "markets_macro_corporate": "Markets & Economy",
    "us_china_policy_geopolitics": "US / China / World",
    "other_consequential_world": "World",
    "routine_politics_general": "Politics",
    "explicit_noise": "noise",
}

_CONFIRMATION_QUALITY = {
    ConfirmationState.OFFICIAL: 95,
    ConfirmationState.CONFIRMED: 80,
    ConfirmationState.REPORTED: 55,
    ConfirmationState.UNVERIFIED: 30,
    ConfirmationState.DISPUTED: 20,
}


def _event_text_and_signals(conn: sqlite3.Connection, event_id: str) -> tuple[str, bool, set[str], float, dict]:
    rows = conn.execute(
        """
        SELECT r.title AS title, r.summary_excerpt AS excerpt, r.origin_family AS origin_family,
               s.tier AS tier, r.metadata_json AS metadata_json
        FROM event_sources es
        JOIN raw_items r ON r.id = es.raw_item_id
        JOIN sources s ON s.id = r.source_id
        WHERE es.event_id = ?
        """,
        (event_id,),
    ).fetchall()

    texts = [f"{r['title']} {r['excerpt'] or ''}" for r in rows]
    combined = " ".join(texts)
    has_t0 = any(r["tier"] == 0 for r in rows)
    origin_families = {r["origin_family"] for r in rows}

    max_popularity_signal = 0.0
    for r in rows:
        try:
            meta = json.loads(r["metadata_json"] or "{}")
        except json.JSONDecodeError:
            meta = {}
        if "hn_score" in meta:
            max_popularity_signal = max(max_popularity_signal, min(100.0, meta["hn_score"] / 5))

    return combined, has_t0, origin_families, max_popularity_signal, {}


def score_event(
    conn: sqlite3.Connection,
    event_id: str,
    config: AppConfig,
    *,
    is_new_event: bool,
    now: datetime | None = None,
) -> ScoreComponents:
    now = now or datetime.now(UTC)
    event = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
    combined_text, has_t0, origin_families, popularity_signal, _ = _event_text_and_signals(conn, event_id)

    relevance = classify_relevance(combined_text, config.topics, config.policy.relevance_multipliers)
    conn.execute(
        "UPDATE events SET category = ? WHERE id = ?",
        (_CATEGORY_LABELS.get(relevance.bucket, "World"), event_id),
    )

    confirmation_state = ConfirmationState(event["confirmation_state"])
    confirmation_quality = _CONFIRMATION_QUALITY[confirmation_state]

    significance_base = 40 + (25 if has_t0 else 0) + (confirmation_quality * 0.25)
    significance = max(0.0, min(100.0, significance_base))

    user_relevance_base = 55.0
    user_relevance = max(0.0, min(100.0, user_relevance_base * relevance.multiplier))

    market_impact_base = 30.0
    if relevance.bucket in ("ai_semiconductor_export_controls", "markets_macro_corporate"):
        market_impact_base = 65.0
    elif relevance.bucket == "us_china_policy_geopolitics":
        market_impact_base = 50.0
    market_impact = max(0.0, min(100.0, market_impact_base))

    n_families = len(origin_families)
    corroboration = {0: 0.0, 1: 25.0, 2: 60.0}.get(n_families, 85.0)

    first_seen = datetime.fromisoformat(event["first_seen_at"])
    if first_seen.tzinfo is None:
        first_seen = first_seen.replace(tzinfo=UTC)
    hours_old = max(0.0, (now - first_seen).total_seconds() / 3600.0)
    freshness = max(0.0, 100.0 - (hours_old / 48.0) * 100.0)

    novelty = 100.0 if is_new_event else 55.0

    popularity = popularity_signal if popularity_signal else 20.0

    noise_penalty = 100.0 if relevance.bucket == "explicit_noise" else 0.0
    repetition_penalty = 0.0 if is_new_event else 10.0

    return ScoreComponents(
        significance=significance,
        user_relevance=user_relevance,
        market_impact=market_impact,
        confirmation_quality=confirmation_quality,
        corroboration=corroboration,
        novelty=novelty,
        popularity=popularity,
        freshness=freshness,
        noise_penalty=noise_penalty,
        repetition_penalty=repetition_penalty,
    )


def classify_severity_candidate(final_score: float, thresholds) -> Severity:
    """Score-only classification for storage/reporting (data model only).

    This is NOT the Phase 2 alert gate (design doc section 7.3): it does not
    check the significance/market_impact floors, confirmation-state
    requirement, cooldowns, spacing, or daily budget. No message is ever sent
    from this value alone. Real S2/S3 delivery eligibility is Phase 2 work.
    """
    if final_score >= thresholds.alert_candidate:
        return Severity.S2
    if final_score >= thresholds.daily_quick_brief:
        return Severity.S1
    return Severity.S0


def persist_score(
    conn: sqlite3.Connection,
    event_id: str,
    scores: ScoreComponents,
    weights: dict[str, float],
    thresholds,
) -> float:
    final = scores.final_score(weights)
    severity = classify_severity_candidate(final, thresholds)
    conn.execute(
        """
        UPDATE events SET
            significance = ?, user_relevance = ?, market_impact = ?, confirmation_quality = ?,
            corroboration = ?, novelty = ?, popularity = ?, freshness = ?,
            noise_penalty = ?, repetition_penalty = ?, final_score = ?, severity = ?
        WHERE id = ?
        """,
        (
            scores.significance,
            scores.user_relevance,
            scores.market_impact,
            scores.confirmation_quality,
            scores.corroboration,
            scores.novelty,
            scores.popularity,
            scores.freshness,
            scores.noise_penalty,
            scores.repetition_penalty,
            final,
            severity.value,
            event_id,
        ),
    )
    return final
