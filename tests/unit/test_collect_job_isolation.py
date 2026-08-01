"""Acceptance test 7: multiple simulated source failures must not abort the
collection job — every other source still runs and the job completes."""

from __future__ import annotations

from news_intel.adapters.base import CollectedItem, FetchOutcome, SourceHealthState
from news_intel.jobs import collect as collect_module
from news_intel.jobs.collect import run_collect
from news_intel.settings import SourceEntry


class _FakeAdapter:
    def __init__(self, source_id: str):
        self.source_id = source_id

    def fetch(self, ctx, cursor):
        if self.source_id == "broken_source":
            raise RuntimeError("simulated adapter crash")
        if self.source_id == "http_error_source":
            return FetchOutcome([], cursor, SourceHealthState.DEGRADED, "http 503")
        return FetchOutcome(
            [
                CollectedItem(
                    source_id=self.source_id,
                    external_id="ok-1",
                    canonical_url="https://good.test/story",
                    title="A working source produced a story",
                    summary_excerpt="This source works fine.",
                    language="en",
                    published_at=None,
                    origin_family="good.test",
                )
            ],
            "cursor-1",
            SourceHealthState.HEALTHY,
        )


def test_multiple_source_failures_do_not_abort_job(config, conn, monkeypatch):
    config.sources.sources.clear()
    config.sources.sources.extend(
        [
            SourceEntry(id="broken_source", name="Broken", adapter="rss", tier=1, enabled=True, url="https://x/1"),
            SourceEntry(id="http_error_source", name="HTTP Error", adapter="rss", tier=1, enabled=True, url="https://x/2"),
            SourceEntry(id="good_source", name="Good", adapter="rss", tier=0, enabled=True, url="https://x/3"),
        ]
    )

    # get_adapter only receives the adapter *type* ("rss" for all three fake
    # entries here), so dispatch by source id instead, read off the context
    # passed into fetch().
    class _Dispatcher:
        def fetch(self, ctx, cursor):
            return _FakeAdapter(ctx.id).fetch(ctx, cursor)

    monkeypatch.setattr(collect_module, "get_adapter", lambda adapter_type: _Dispatcher())

    summary = run_collect(config, conn)

    assert len(summary.sources) == 3
    states = {s.source_id: s.health_state for s in summary.sources}
    assert states["broken_source"] == SourceHealthState.DEGRADED.value
    assert states["http_error_source"] == SourceHealthState.DEGRADED.value
    assert states["good_source"] == SourceHealthState.HEALTHY.value
    assert summary.new_raw_items == 1
    assert summary.new_events == 1
