"""3-minute news snapshot for the history-in-practice newsletter (its prompts/08), and the ranking replay behind it.

Offline/manual only: nothing here is scheduled, sends anything, or writes to the production database. Both entry
points work on a COPY of the database (`--db`), because rescoring writes category/score columns.

  replay    rescore the events of one window twice -- policy.features.public_safety_bucket off and on, each as at
            the event's own first sighting (is_new_event, freshness as at ingest) -- and report how the ranking
            moves. Validates the flag on real stored events before anyone enables it.
  snapshot  pick 3-5 items automatically from both editions' qualified events (no human choice), with a per-
            category cap for variety, and write the newsletter's frozen JSON (schema news3min/v0-prototype). The
            text of each item comes from the bounded per-story editorial layer when a provider is configured
            (validated against the item's own sources), else from the source-grounded floor -- every item says
            which, and the snapshot says whether selection was automatic and whether any text is a fallback.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from news_intel.event_repo import event_as_digest_dict
from news_intel.pipeline.scoring import persist_score, score_event

SCHEMA = "news3min/v0-prototype"
EDITIONS = ("en_global", "zh_cn")
EXCLUDED = {"noise", "Unclassified", "uncategorized", None}
PER_CATEGORY = 1  # a 3-minute general brief: variety over depth (replay 2026-10-31: cap 2 left one non-business slot)
LA = ZoneInfo("America/Los_Angeles")


def window_events(conn: sqlite3.Connection, start: datetime, end: datetime) -> list[sqlite3.Row]:
    """Source-backed events whose eligibility time falls in [start, end), both editions."""
    return conn.execute(
        """
        SELECT e.* FROM events e
        WHERE COALESCE(e.content_changed_at, e.published_anchor_at) >= ?
          AND COALESCE(e.content_changed_at, e.published_anchor_at) < ?
          AND EXISTS (SELECT 1 FROM event_sources es WHERE es.event_id = e.id)
        """,
        (start.astimezone(UTC).isoformat(), end.astimezone(UTC).isoformat()),
    ).fetchall()


def rescore(conn: sqlite3.Connection, config, rows, *, flag: bool) -> dict[str, tuple[str, float]]:
    config.policy.features.public_safety_bucket = flag
    weights = config.policy.scoring.weights.model_dump()
    out = {}
    for r in rows:
        seen = datetime.fromisoformat(r["first_seen_at"])
        seen = seen if seen.tzinfo else seen.replace(tzinfo=UTC)
        persist_score(
            conn,
            r["id"],
            score_event(conn, r["id"], config, is_new_event=True, now=seen),
            weights,
            config.policy.scoring.thresholds,
        )
    for r in conn.execute(
        f"SELECT id, category, final_score FROM events WHERE id IN ({','.join('?' * len(rows))})",
        [r["id"] for r in rows],
    ):
        out[r["id"]] = (r["category"], round(r["final_score"], 1))
    return out


def replay(conn, config, start, end, *, top=15) -> dict:
    rows = window_events(conn, start, end)
    titles = {r["id"]: (r["primary_title"], r["edition_id"]) for r in rows}
    off = rescore(conn, config, rows, flag=False)
    on = rescore(conn, config, rows, flag=True)

    def ranked(scores):
        return sorted(((eid, *v) for eid, v in scores.items() if v[0] not in EXCLUDED), key=lambda x: -x[2])

    bar = config.policy.scoring.thresholds.daily_quick_brief
    moved = sorted((eid for eid in on if on[eid][0] != off[eid][0]), key=lambda e: -on[e][1])
    return {
        "window": [start.isoformat(), end.isoformat()],
        "events": len(rows),
        "quick_brief_bar": bar,
        "unclassified": {
            "off": sum(v[0] == "Unclassified" for v in off.values()),
            "on": sum(v[0] == "Unclassified" for v in on.values()),
        },
        "reclassified": [
            {
                "event_id": e,
                "edition": titles[e][1],
                "title": titles[e][0][:110],
                "off": off[e],
                "on": on[e],
                "qualifies_on": on[e][1] >= bar,
            }
            for e in moved
        ],
        "top_off": [
            {"event_id": e, "category": c, "score": s, "title": titles[e][0][:90]} for e, c, s in ranked(off)[:top]
        ],
        "top_on": [
            {"event_id": e, "category": c, "score": s, "title": titles[e][0][:90]} for e, c, s in ranked(on)[:top]
        ],
    }


def pick(conn, config, start, end, n: int = 5) -> list[dict]:
    """Automatic selection among qualified, source-linked events of both editions, highest score first: one item
    per category first (variety), then the remaining slots by score with at most two per category; one item per
    primary URL."""
    bar = config.policy.scoring.thresholds.daily_quick_brief
    rows = [r for r in window_events(conn, start, end) if r["final_score"] >= bar]
    cands = [event_as_digest_dict(conn, r) for r in sorted(rows, key=lambda r: -r["final_score"])]
    picked, per_cat, urls = [], {}, set()
    for cap in (PER_CATEGORY, PER_CATEGORY + 1):
        for c in cands:
            if len(picked) == n:
                return sorted(picked, key=lambda c: -c["final_score"])
            url = next((s.get("url") for s in c["sources"] if s.get("url")), None)
            if c["category"] in EXCLUDED or not url or url in urls or per_cat.get(c["category"], 0) >= cap:
                continue
            picked.append(c)
            urls.add(url)
            per_cat[c["category"]] = per_cat.get(c["category"], 0) + 1
    return sorted(picked, key=lambda c: -c["final_score"])


def _item(c: dict, provider, deadline) -> dict:
    from news_intel.pipeline.editorial_enrichment import enrich_story

    ed = enrich_story(
        c, None, c["category"], c.get("entities"), edition_id="zh_cn", provider=provider, deadline=deadline
    )
    srcs = [
        {"name": s.get("publisher"), "url": s.get("url"), "published_at": s.get("published_at")}
        for s in c["sources"]
        if s.get("url")
    ][:3]
    model = ed.source == "llm_validated"
    return {
        "headline": c["title"],
        "what": (ed.chinese_what_happened if model else None) or ed.chinese_what_happened or c.get("excerpt") or "",
        "why": (ed.chinese_takeaway or "") if model else "",
        "why_kind": "model_takeaway_validated" if model and ed.chinese_takeaway else "",
        "qualifiers": "",
        "sources": srcs,
        "news_intel_event_ids": [c["event_id"]],
        "category": c["category"],
        "score": round(c["final_score"], 1),
        "text_source": ed.source,
        "fallback_reason": ed.fallback_reason,
        "model": ed.model_used,
    }


def snapshot(conn, config, as_of: datetime, *, provider=None, n: int = 5, hours: int = 24) -> dict:
    start = as_of - timedelta(hours=hours)
    deadline = time.monotonic() + 600
    items = [_item(c, provider, deadline) for c in pick(conn, config, start, as_of, n)]
    model_items = sum(i["text_source"] == "llm_validated" for i in items)
    local = as_of.astimezone(LA)
    text_mode = (
        "model (all items validated)"
        if items and model_items == len(items)
        else "deterministic fallback (no provider configured)"
        if provider is None
        else f"mixed: {model_items}/{len(items)} items model-validated, the rest source-grounded fallback"
    )
    return {
        "schema": SCHEMA,
        "snapshot_id": f"news3min-auto-{local:%Y%m%d-%H%M}PT",
        "revision": 1,
        "language": "zh",
        "intended_edition_date": local.date().isoformat(),
        "timezone": "America/Los_Angeles",
        "as_of": local.isoformat(),
        "window": {"start": start.astimezone(LA).isoformat(), "end": local.isoformat()},
        "label": {
            "zh": f"截至 {local:%Y年%-m月%-d日 %H:%M}（洛杉矶时间）· 过去{hours}小时要闻（自动选题）",
            "en": f"As of {local:%H:%M} Los Angeles time, {local:%-d %B %Y} · the past {hours} hours "
            "(automatic selection)",
        },
        # a reader-ready snapshot needs Chinese text for every item; anything else is for comparison only
        "status": "ready" if items and model_items == len(items) and len(items) >= 3 else "comparison_only",
        "status_note": "Automatic news-intel snapshot for comparison with the hand-picked prototype. "
        "Not sent anywhere.",
        "provenance": {
            "selection": "automatic: highest final_score among qualified source-linked events of en_global and zh_cn, "
            f"at most {PER_CATEGORY} per category, one per primary URL (no human choice)",
            "public_safety_bucket": config.policy.features.public_safety_bucket,
            "text_mode": text_mode,
            "generated_at": datetime.now(UTC).isoformat(),
        },
        "items": items,
    }


def main(argv=None) -> int:
    from news_intel import db
    from news_intel.settings import load_config

    ap = argparse.ArgumentParser(prog="python -m news_intel.jobs.news3min")
    ap.add_argument("command", choices=["replay", "snapshot"])
    ap.add_argument("--db", required=True, help="a COPY of the database (rescoring writes to it)")
    ap.add_argument("--as-of", required=True, help="ISO time, e.g. 2026-10-31T16:00:00-07:00")
    ap.add_argument("--hours", type=int, default=24)
    ap.add_argument(
        "--public-safety", choices=["on", "off"], default="off", help="snapshot: flag value to rescore with"
    )
    ap.add_argument("--with-provider", action="store_true", help="snapshot: use the configured bounded story provider")
    ap.add_argument("--out")
    a = ap.parse_args(argv)
    if Path(a.db).resolve() == (Path.home() / "pi-news-intel/data/news-intel.db").resolve():
        raise SystemExit("refusing to rescore the production database; pass a copy")
    config = load_config()
    conn = db.connect(Path(a.db))
    as_of = datetime.fromisoformat(a.as_of)
    start = as_of - timedelta(hours=a.hours)
    if a.command == "replay":
        res = replay(conn, config, start, as_of)
    else:
        rows = window_events(conn, start, as_of)
        rescore(conn, config, rows, flag=a.public_safety == "on")
        provider = None
        if a.with_provider:
            from news_intel.pipeline.editorial_enrichment import build_bounded_provider

            provider = build_bounded_provider(config)
        res = snapshot(conn, config, as_of, provider=provider, hours=a.hours)
    conn.commit()
    text = json.dumps(res, ensure_ascii=False, indent=1)
    if a.out:
        Path(a.out).write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
