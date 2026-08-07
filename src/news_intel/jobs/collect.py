"""Stage 1 orchestration: one short-lived job triggered every 5 minutes by
systemd. The 5-minute timer is only a scheduler tick -- each source is only
actually fetched when its own configured poll_interval_seconds (plus any
backoff) says it is due (Gate A remediation Phase 1.2). One source's
failure never aborts collection from the others — each adapter call is
individually wrapped so an unhandled adapter exception degrades that source
instead of killing the run.

Only genuinely NEW or CONTENT_CHANGED raw items are fed into
normalization/clustering/confirmation/scoring (Gate A remediation Phase
1.1) -- an UNCHANGED item (same identity, same content_hash, e.g. a feed
that just refreshed its own timestamp) is counted but never reprocessed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from news_intel import db
from news_intel.adapters.base import FetchOutcome, SourceHealthState, get_adapter
from news_intel.adapters.context import build_context
from news_intel.jobs import job_runs
from news_intel.pipeline.cluster import find_or_create_event
from news_intel.pipeline.confirmation import update_confirmation_state
from news_intel.pipeline.dedupe import UpsertResult, upsert_raw_item
from news_intel.pipeline.entities import known_entities
from news_intel.pipeline.normalize import normalize
from news_intel.pipeline.scoring import persist_score, score_event
from news_intel.scheduled_repo import record_scheduled_events
from news_intel.settings import AppConfig
from news_intel.sources_repo import get_cursor, mark_disabled, record_outcome, source_due, sync_sources


@dataclass
class SourceRunResult:
    source_id: str
    action: str  # disabled | skipped_not_due | attempted
    health_state: str  # healthy | degraded | unavailable | disabled | n/a (skipped_not_due)
    item_count: int
    detail: str | None = None


@dataclass
class CollectSummary:
    skipped_lock: bool = False
    sources: list[SourceRunResult] = field(default_factory=list)
    fetched_items: int = 0
    new_raw_items: int = 0
    changed_raw_items: int = 0
    unchanged_raw_items: int = 0
    new_events: int = 0
    updated_events: int = 0

    @property
    def processed_items(self) -> int:
        """Items that actually went through normalization/clustering/
        confirmation/scoring -- must equal new_raw_items + changed_raw_items,
        never fetched_items."""
        return self.new_raw_items + self.changed_raw_items


def run_collect(config: AppConfig, conn, *, now: datetime | None = None) -> CollectSummary:
    now = now or datetime.now(UTC)

    if not db.acquire_job_lock(conn, "collect"):
        return CollectSummary(skipped_lock=True)

    run_id = job_runs.start(conn, "collect")
    summary = CollectSummary()
    known = known_entities(config.topics.groups)
    weights = config.policy.scoring.weights.model_dump()
    thresholds = config.policy.scoring.thresholds

    try:
        sync_sources(conn, config.sources)

        for entry in config.sources.sources:
            if not entry.enabled:
                mark_disabled(conn, entry.id)
                summary.sources.append(SourceRunResult(entry.id, "disabled", "disabled", 0))
                continue

            if not source_due(conn, entry.id, now=now):
                summary.sources.append(SourceRunResult(entry.id, "skipped_not_due", "n/a", 0))
                continue

            ctx = build_context(entry, config)
            cursor = get_cursor(conn, entry.id)
            try:
                adapter = get_adapter(entry.adapter)
                outcome = adapter.fetch(ctx, cursor)
            except Exception as exc:  # noqa: BLE001 - one bad source must not abort the run
                outcome = FetchOutcome([], cursor, SourceHealthState.DEGRADED, f"unhandled error: {exc}")

            record_outcome(conn, entry.id, outcome, now=now)
            summary.sources.append(
                SourceRunResult(
                    entry.id, "attempted", outcome.health_state.value, len(outcome.items), outcome.health_detail
                )
            )

            if "calendar" in entry.category_hints:
                record_scheduled_events(conn, entry.id, outcome.items, now=now)
                continue

            for item in outcome.items:
                summary.fetched_items += 1
                normalized = normalize(
                    item, excerpt_max_chars=config.policy.storage.raw_excerpt_max_chars, now=now
                )
                upsert = upsert_raw_item(conn, normalized)

                prior_event_ids: list[str] = []
                if upsert.result == UpsertResult.NEW:
                    summary.new_raw_items += 1
                elif upsert.result == UpsertResult.CONTENT_CHANGED:
                    summary.changed_raw_items += 1
                    prior_event_ids = [
                        r["event_id"] for r in conn.execute(
                            "SELECT event_id FROM event_sources WHERE raw_item_id = ?", (upsert.raw_item_id,)
                        ).fetchall()
                    ]
                else:
                    summary.unchanged_raw_items += 1
                    continue  # never reprocessed

                with db.transaction(conn):
                    if prior_event_ids:
                        # At most one event_sources link per raw_item: drop any
                        # stale link before reclustering against the new content,
                        # so a content change can never leave a duplicate link.
                        conn.execute("DELETE FROM event_sources WHERE raw_item_id = ?", (upsert.raw_item_id,))

                    event_id, is_new = find_or_create_event(
                        conn, normalized, upsert.raw_item_id, config.policy.dedupe, known,
                        now=now, time_policy=config.policy.time_semantics,
                        legal_notice_policy=config.topics.legal_notice_boilerplate,
                    )
                    if is_new:
                        summary.new_events += 1
                    else:
                        summary.updated_events += 1

                    update_confirmation_state(conn, event_id, now=now)
                    scores = score_event(conn, event_id, config, is_new_event=is_new, now=now)
                    persist_score(conn, event_id, scores, weights, thresholds)

                    # 2026-08-06 incident (migration 008): reclustering a
                    # CONTENT_CHANGED item can legitimately land it on a
                    # *different* event than the one it was previously
                    # linked to. If that leaves the previous event with zero
                    # remaining sources, it must never again be treated as
                    # current/eligible/scored -- detected and marked
                    # atomically with the delete+recluster above, in the
                    # same transaction, not as a separate later pass.
                    for prior_event_id in prior_event_ids:
                        if prior_event_id == event_id:
                            continue
                        remaining = conn.execute(
                            "SELECT COUNT(*) AS n FROM event_sources WHERE event_id = ?", (prior_event_id,)
                        ).fetchone()["n"]
                        if remaining == 0:
                            conn.execute(
                                "UPDATE events SET orphaned_at = ? WHERE id = ? AND orphaned_at IS NULL",
                                (now.isoformat(), prior_event_id),
                            )

        job_runs.finish(
            conn,
            run_id,
            "success",
            {
                "sources_run": len(summary.sources),
                "fetched_items": summary.fetched_items,
                "new_raw_items": summary.new_raw_items,
                "changed_raw_items": summary.changed_raw_items,
                "unchanged_raw_items": summary.unchanged_raw_items,
                "new_events": summary.new_events,
                "updated_events": summary.updated_events,
            },
        )
    except Exception as exc:  # noqa: BLE001 - record failure, then re-raise for the caller/CLI
        job_runs.finish(conn, run_id, "failed", {"error": str(exc)})
        raise
    finally:
        db.release_job_lock(conn, "collect")

    return summary
