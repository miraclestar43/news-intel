"""Stage 1 orchestration: one short-lived job triggered every 5 minutes by
systemd. One source's failure never aborts collection from the others —
each adapter call is individually wrapped so an unhandled adapter exception
degrades that source instead of killing the run."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from news_intel import db
from news_intel.adapters.base import FetchOutcome, SourceHealthState, get_adapter
from news_intel.adapters.context import build_context
from news_intel.jobs import job_runs
from news_intel.pipeline.cluster import find_or_create_event
from news_intel.pipeline.confirmation import update_confirmation_state
from news_intel.pipeline.dedupe import insert_raw_item
from news_intel.pipeline.entities import known_entities
from news_intel.pipeline.normalize import normalize
from news_intel.pipeline.scoring import persist_score, score_event
from news_intel.scheduled_repo import record_scheduled_events
from news_intel.settings import AppConfig
from news_intel.sources_repo import get_cursor, mark_disabled, record_outcome, sync_sources


@dataclass
class SourceRunResult:
    source_id: str
    health_state: str
    item_count: int
    detail: str | None = None


@dataclass
class CollectSummary:
    skipped_lock: bool = False
    sources: list[SourceRunResult] = field(default_factory=list)
    new_raw_items: int = 0
    new_events: int = 0
    updated_events: int = 0


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
                summary.sources.append(SourceRunResult(entry.id, SourceHealthState.DISABLED.value, 0))
                continue

            ctx = build_context(entry, config)
            cursor = get_cursor(conn, entry.id)
            try:
                adapter = get_adapter(entry.adapter)
                outcome = adapter.fetch(ctx, cursor)
            except Exception as exc:  # noqa: BLE001 - one bad source must not abort the run
                outcome = FetchOutcome([], cursor, SourceHealthState.DEGRADED, f"unhandled error: {exc}")

            record_outcome(conn, entry.id, outcome)
            summary.sources.append(
                SourceRunResult(entry.id, outcome.health_state.value, len(outcome.items), outcome.health_detail)
            )

            if "calendar" in entry.category_hints:
                record_scheduled_events(conn, entry.id, outcome.items)
                continue

            for item in outcome.items:
                normalized = normalize(
                    item, excerpt_max_chars=config.policy.storage.raw_excerpt_max_chars, now=now
                )
                raw_id = insert_raw_item(conn, normalized)
                summary.new_raw_items += 1

                event_id, is_new = find_or_create_event(
                    conn, normalized, raw_id, config.policy.dedupe, known, now=now
                )
                if is_new:
                    summary.new_events += 1
                else:
                    summary.updated_events += 1

                update_confirmation_state(conn, event_id, now=now)
                scores = score_event(conn, event_id, config, is_new_event=is_new, now=now)
                persist_score(conn, event_id, scores, weights, thresholds)

        job_runs.finish(
            conn,
            run_id,
            "success",
            {
                "sources_run": len(summary.sources),
                "new_raw_items": summary.new_raw_items,
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
