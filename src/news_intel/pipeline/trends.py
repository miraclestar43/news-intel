"""Phase 3: seven-day topic-cluster construction and trend-strength scoring.

A "trend" groups related *events* (already deduped/clustered articles) that
share a category and at least one known entity within the report window --
not raw articles. Strength is deliberately built only from importance,
confirmation quality, source-family diversity, persistence, and material
developments; raw article/raw_item counts never enter the formula, so ten
syndicated copies of one story cannot outscore a handful of independently
corroborated, persistent, escalating events.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime

from news_intel.pipeline.entities import extract_known_entities


@dataclass
class TrendCluster:
    id: str
    category: str
    key_entities: list[str]
    event_ids: list[str] = field(default_factory=list)
    avg_final_score: float = 0.0
    avg_confirmation_quality: float = 0.0
    distinct_origin_families: int = 0
    persistence_days: float = 0.0
    material_update_count: int = 0
    strength: float = 0.0


def _cluster_id(event_ids: list[str]) -> str:
    return "TR-" + hashlib.sha256(",".join(sorted(event_ids)).encode()).hexdigest()[:12]


def _origin_family_count(conn: sqlite3.Connection, event_ids: list[str]) -> int:
    if not event_ids:
        return 0
    placeholders = ",".join("?" for _ in event_ids)
    rows = conn.execute(
        f"""
        SELECT DISTINCT r.origin_family AS origin_family
        FROM event_sources es JOIN raw_items r ON r.id = es.raw_item_id
        WHERE es.event_id IN ({placeholders})
        """,
        event_ids,
    ).fetchall()
    return len({r["origin_family"] for r in rows})


def _material_update_count(conn: sqlite3.Connection, event_ids: list[str]) -> int:
    if not event_ids:
        return 0
    placeholders = ",".join("?" for _ in event_ids)
    row = conn.execute(
        f"SELECT COUNT(*) AS n FROM event_updates WHERE event_id IN ({placeholders})", event_ids
    ).fetchone()
    return row["n"]


def build_trend_clusters(
    conn: sqlite3.Connection, events: list[sqlite3.Row], known_entities: set[str]
) -> list[TrendCluster]:
    """`events` should already be the report-window's qualified events,
    ordered by final_score DESC (highest-importance event anchors each
    cluster first, matching pipeline/cluster.py's greedy style)."""
    groups: list[dict] = []  # {category, entities: set, rows: [row, ...]}

    for row in events:
        text = f"{row['primary_title']} "
        entities = set(extract_known_entities(text, known_entities))
        if not entities and row["entities_json"]:
            try:
                entities |= set(json.loads(row["entities_json"]))
            except (json.JSONDecodeError, TypeError):
                pass
        category = row["category"] or "uncategorized"

        target = None
        for group in groups:
            if group["category"] == category and (group["entities"] & entities):
                target = group
                break
        if target is None:
            groups.append({"category": category, "entities": set(entities), "rows": [row]})
        else:
            target["entities"] |= entities
            target["rows"].append(row)

    clusters: list[TrendCluster] = []
    for group in groups:
        rows = group["rows"]
        event_ids = [r["id"] for r in rows]
        avg_score = sum(r["final_score"] for r in rows) / len(rows)
        avg_confq = sum(r["confirmation_quality"] for r in rows) / len(rows)
        distinct_families = _origin_family_count(conn, event_ids)
        first_seen = min(datetime.fromisoformat(r["first_seen_at"]) for r in rows)
        last_updated = max(datetime.fromisoformat(r["last_updated_at"]) for r in rows)
        persistence_days = max(0.0, (last_updated - first_seen).total_seconds() / 86400.0)
        material_updates = _material_update_count(conn, event_ids)

        strength = (
            0.30 * avg_score
            + 0.25 * avg_confq
            + min(20.0, distinct_families * 7.0)
            + min(15.0, persistence_days * 5.0)
            + min(10.0, material_updates * 5.0)
        )

        clusters.append(
            TrendCluster(
                id=_cluster_id(event_ids),
                category=group["category"],
                key_entities=sorted(group["entities"]),
                event_ids=event_ids,
                avg_final_score=avg_score,
                avg_confirmation_quality=avg_confq,
                distinct_origin_families=distinct_families,
                persistence_days=persistence_days,
                material_update_count=material_updates,
                strength=max(0.0, min(100.0, strength)),
            )
        )

    clusters.sort(key=lambda c: c.strength, reverse=True)
    return clusters
