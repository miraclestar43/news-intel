"""Phase 3/Gate A remediation Phase 4: seven-day topic-cluster construction
and trend-strength scoring.

Three distinct things, never conflated:
  - same-event clustering (pipeline/cluster.py): many sources, one real
    event -- already resolved before anything here runs.
  - trend grouping (this module, len(event_ids) >= 2): multiple *distinct*
    material events that form one continuing development.
  - standalone material developments (this module, len(event_ids) == 1): a
    single qualifying event that does not continue/repeat anything else in
    the window.

Gate A produced 915 nearly-meaningless "trends" for one week, some merging
unrelated headlines, because the old grouping rule let any two events
sharing *any* known entity link up, and let group membership grow by
transitive union (A~B, B~C => A,B,C one group even if A and C share
nothing) -- so one generic entity like "AI" or "United States" could chain
arbitrarily many unrelated stories into one blob. The fix:

  1. Only events that already cleared a minimum score, have a real
     category (not noise/unclassified), and are not archive-only are even
     candidates for trend/standalone consideration.
  2. A shared anchor must be a *specific* entity -- config's
     trend_identity.generic_anchor_terms can never be the sole basis for
     linking two events.
  3. Complete-link validation: a candidate only joins a group if it shares
     a specific anchor *and* clears a topical-similarity floor against
     EVERY existing member, not just the group's founding event -- this is
     what stops transitive bridging, since incompatible members can never
     end up in the same group no matter what chain of intermediate events
     might otherwise have connected them.

Strength is deliberately built only from importance, confirmation quality,
source-family diversity, persistence, and material developments; raw
article/raw_item counts never enter the formula, so ten syndicated copies
of one story cannot outscore a handful of independently corroborated,
persistent, escalating events.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime

from news_intel.pipeline.entities import extract_known_entities
from news_intel.pipeline.normalize import normalize_title
from news_intel.pipeline.product_identity import (
    ProductIdentity,
    extract_product_identity,
    has_conflicting_product_version,
)
from news_intel.pipeline.similarity import trigram_jaccard
from news_intel.settings import TrendIdentityPolicy

_NOISE_CATEGORIES = {"noise", "Unclassified", None}


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
    is_trend: bool = False
    shared_anchor: list[str] = field(default_factory=list)
    comparison_anchor: list[str] = field(default_factory=list)


@dataclass
class _Candidate:
    row: sqlite3.Row
    entities: set[str]
    specific_entities: set[str]
    normalized_title: str
    identity: ProductIdentity


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


def _event_entities(row: sqlite3.Row, known_entities: set[str]) -> set[str]:
    entities = set(extract_known_entities(row["primary_title"], known_entities))
    if row["entities_json"]:
        try:
            entities |= set(json.loads(row["entities_json"]))
        except (json.JSONDecodeError, TypeError):
            pass
    return entities


def _is_eligible(row: sqlite3.Row, min_score: float) -> bool:
    category = row["category"]
    if category in _NOISE_CATEGORIES:
        return False
    if bool(row["archive_only"]) if "archive_only" in row.keys() else False:
        return False
    return row["final_score"] >= min_score


def _compatible(candidate: _Candidate, member: _Candidate, policy: TrendIdentityPolicy) -> bool:
    """Complete-link check: a candidate is compatible with an existing group
    member only if they share a specific (non-generic) anchor entity *and*
    clear a topical-similarity floor -- sharing a company name alone is not
    enough to call two events "the same continuing development" (e.g. one
    company's export-control story and its unrelated earnings release).
    Retry 1 Blocker 2: an explicit, conflicting product/version identifier
    (GPT-5.5 vs GPT-5.3) always wins regardless of shared entities/trigram
    -- an organization name alone must never group unrelated product
    launches into one "trend"."""
    if has_conflicting_product_version(candidate.identity, member.identity):
        return False
    shared = candidate.specific_entities & member.specific_entities
    if not shared:
        return False
    return trigram_jaccard(candidate.normalized_title, member.normalized_title) >= policy.topic_trigram_floor


def build_trend_clusters(
    conn: sqlite3.Connection,
    events: list[sqlite3.Row],
    known_entities: set[str],
    *,
    policy: TrendIdentityPolicy | None = None,
    min_event_score: float = 0.0,
) -> list[TrendCluster]:
    """`events` should already be the report-window's qualified events,
    ordered by final_score DESC (highest-importance event anchors each
    group first). Returns both multi-event trends (is_trend=True) and
    standalone material developments (is_trend=False, a single event that
    didn't group with anything) -- callers that only want trends should
    filter on is_trend."""
    policy = policy or TrendIdentityPolicy()
    generic = {t.lower() for t in policy.generic_anchor_terms}

    groups: list[dict] = []  # {category, members: [_Candidate, ...]}

    for row in events:
        if not _is_eligible(row, min_event_score):
            continue

        entities = _event_entities(row, known_entities)
        specific = {e for e in entities if e.lower() not in generic}
        candidate = _Candidate(
            row=row, entities=entities, specific_entities=specific,
            normalized_title=normalize_title(row["primary_title"]),
            identity=extract_product_identity(row["primary_title"]),
        )
        category = row["category"] or "uncategorized"

        target = None
        for group in groups:
            if group["category"] != category:
                continue
            # Complete-link: this candidate must be individually compatible
            # with every existing member, not merely the founding one --
            # this is what blocks transitive bridging (A~B, B~C is not
            # enough if A and C themselves share nothing).
            if group["members"] and all(_compatible(candidate, member, policy) for member in group["members"]):
                target = group
                break
        if target is None:
            groups.append({"category": category, "members": [candidate]})
        else:
            target["members"].append(candidate)

    clusters: list[TrendCluster] = []
    for group in groups:
        members = group["members"]
        rows = [m.row for m in members]
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

        all_entities: set[str] = set()
        shared_anchor: set[str] | None = None
        for m in members:
            all_entities |= m.entities
            shared_anchor = m.specific_entities if shared_anchor is None else (shared_anchor & m.specific_entities)

        clusters.append(
            TrendCluster(
                id=_cluster_id(event_ids),
                category=group["category"],
                key_entities=sorted(all_entities),
                event_ids=event_ids,
                avg_final_score=avg_score,
                avg_confirmation_quality=avg_confq,
                distinct_origin_families=distinct_families,
                persistence_days=persistence_days,
                material_update_count=material_updates,
                strength=max(0.0, min(100.0, strength)),
                is_trend=len(event_ids) >= 2,
                shared_anchor=sorted(shared_anchor) if len(event_ids) >= 2 and shared_anchor else [],
                comparison_anchor=sorted(shared_anchor) if shared_anchor else [],
            )
        )

    clusters.sort(key=lambda c: c.strength, reverse=True)
    return clusters


def select_top_clusters(clusters: list[TrendCluster], *, max_items: int) -> list[TrendCluster]:
    """Gate A remediation Phase 4: the weekly composer must receive a
    bounded, config-driven set of the highest-quality eligible trends/
    standalone developments (already strength-sorted) -- not hundreds of
    low-value clusters for the LLM to try to repair into a 650-800 word
    brief."""
    return clusters[:max_items]
