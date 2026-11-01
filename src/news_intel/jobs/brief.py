"""Build, freeze, render and (separately) deliver the automatic Chinese daily brief (news-intel v2, 2026-11-01).

  python -m news_intel.jobs.brief build  [--date YYYY-MM-DD | --cutoff ISO] [--db PATH] [--export-dir DIR]
  python -m news_intel.jobs.brief render [--date ...] [--export-dir DIR]
  python -m news_intel.jobs.brief send-telegram --date ... [--real]    (dry run unless --real)

build: one run per Beijing edition date. If that edition is already frozen it is reused, never re-selected or
rewritten (a retry after a crash, a second timer firing, or a consumer asking twice all get the same text); a new
revision only with an explicit --revision. Writes to the database only its own job_runs row and job lock.
A past cutoff (a replay) is refused on the production database: replays run on a copy.

send-telegram: sends the frozen snapshot's Telegram rendering -- nothing is composed at send time -- with the
deliveries table's idempotency key brief:zh_cn:<edition_date>:<chat hash>, pre-marked before a real send so an
attempted date is never retried. Not scheduled by this branch.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import subprocess
import time
from datetime import UTC, date, datetime
from pathlib import Path

from news_intel import db
from news_intel.brief import render as brief_render
from news_intel.brief import snapshot as snap_mod
from news_intel.brief.compose import compose
from news_intel.brief.pool import build_pool
from news_intel.brief.schedule import BEIJING, WINDOW_HOURS, current_edition, edition_at, edition_for
from news_intel.brief.select import MAX_ITEMS, select
from news_intel.jobs import job_runs

LOCK = "brief_zh_cn"
BUDGET_SECONDS = 1200.0  # the unit's TimeoutStartSec (deploy/systemd/pi-news-brief.service) leaves 600 s on top
EDITOR_POOL = 40
CALL_TIMEOUT = 150.0
PROD_DB = Path.home() / "pi-news-intel" / "data" / "news-intel.db"
ROOT = Path(__file__).resolve().parents[3]


def brief_provider(config):
    """The same ClaudeCliProvider the dailies use (LLM_PROVIDER=claude_cli), with a per-call cap sized for the
    brief's longer prompts. Any other provider setting -> None: the brief is never written by a different model."""
    if config.secrets.llm_provider != "claude_cli":
        return None
    from news_intel.llm.provider import _claude

    return _claude(config.secrets, CALL_TIMEOUT)


def export_dir_for(config) -> Path:
    return config.data_dir / "exports" / "brief"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def provenance() -> dict:
    try:
        head = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True,
                              timeout=10).stdout.strip()
        dirty = bool(subprocess.run(["git", "-C", str(ROOT), "status", "--porcelain", "--untracked-files=no"],
                                    capture_output=True, text=True, timeout=10).stdout.strip())
    except (OSError, subprocess.SubprocessError):
        head, dirty = "unknown", None
    prompts = ROOT / "config" / "prompts"
    return {
        "code_commit": head, "code_dirty": dirty,
        "prompts_sha256": {p: _sha(prompts / p)[:16]
                           for p in ("select_brief.md", "enrich_event.md", "verify_brief.md")},
        "sources_sha256": _sha(ROOT / "config" / "sources.yaml")[:16],
        "entity_aliases_sha256": _sha(ROOT / "config" / "entity_aliases.yaml")[:16],
    }


def _pick(p) -> dict:
    return {"event_ids": [c.event_id for c in p.candidates], "titles": [c.title for c in p.candidates],
            "editions": [c.edition_id for c in p.candidates], "reason": p.reason}


def _short_model(model: str | None) -> str:
    return (model or "none").split(" (")[0]


def write_renderings(export_dir: Path, snap: dict, archive_base: str) -> dict:
    d = snap_mod.edition_dir(export_dir, snap["edition_date"])
    tg = brief_render.telegram_html(snap, archive_base)
    dig = brief_render.digest(snap, archive_base)
    page = brief_render.archive_html(snap, archive_base)
    stem = snap["snapshot_id"]
    snap_mod._atomic_write(d / f"{stem}.telegram.html", tg)
    snap_mod._atomic_write(d / f"{stem}.digest.json", json.dumps(dig, ensure_ascii=False, indent=1))
    snap_mod._atomic_write(d / f"{stem}.archive.html", page)
    return {"telegram_chars": len(tg), "digest_zh_chars": dig["zh_chars"], "archive_url": dig["url"]}


def build(conn, config, edition, *, provider=None, export_dir: Path, revision: int | None = None,
          archive_base: str = brief_render.DEFAULT_ARCHIVE_BASE, budget: float = BUDGET_SECONDS) -> dict:
    """Returns {"result": "built" | "reused" | "skipped_lock", "snapshot": dict | None, "path": str | None}."""
    if revision is None:
        existing = snap_mod.current(export_dir, edition.edition_date)
        if existing is not None:
            return {"result": "reused", "snapshot": existing, "path": None}
        revision = 1
    if not db.acquire_job_lock(conn, LOCK, ttl_seconds=int(budget) + 600):
        return {"result": "skipped_lock", "snapshot": None, "path": None}
    run_id = job_runs.start(conn, "brief", edition_id="zh_cn")
    t0 = time.monotonic()
    try:
        cands, funnel = build_pool(conn, config, edition, export_dir=export_dir, limit=EDITOR_POOL)
        selection = select(provider, cands[:EDITOR_POOL], edition, MAX_ITEMS)
        items, log = compose(provider, selection, edition, deadline=t0 + budget) if provider else ([], None)
        writer = next((i["verification"]["model"] for i in items), getattr(provider, "model", None))
        snap = {
            "schema": snap_mod.SCHEMA,
            "snapshot_id": snap_mod.snapshot_id(edition.edition_date, revision),
            "revision": revision,
            "language": "zh",
            "edition_date": edition.edition_date.isoformat(),
            "timezone": "Asia/Shanghai",
            "cutoff": edition.cutoff.isoformat(),
            "window": {"start": edition.window_start.isoformat(), "end": edition.cutoff.isoformat(),
                       "hours": WINDOW_HOURS},
            "generated_at": datetime.now(UTC).astimezone(BEIJING).isoformat(timespec="seconds"),
            "status": snap_mod.status_for(items),
            "label": {"zh": edition.label_zh()},
            "selection_mode": "automatic",
            "items": items,
            "stats": {},
            "selection": {
                "method": selection.method, "model": selection.model, "error": selection.error,
                "latency_ms": selection.latency_ms,
                "picks": [_pick(p) for p in selection.picks],
                "reserves": [_pick(p) for p in selection.reserves],
                "not_chosen": selection.not_chosen,
            },
            "rejected": log.rejected if log else [],
            "funnel": funnel,
            "models": {"writer": writer, "editor": selection.model, "verifier": writer,
                       "writer_short": _short_model(writer)},
            "provenance": provenance(),
            "run": {"provider_calls": list(getattr(provider, "calls", []) or []),
                    "verifier_calls": log.verifier_calls if log else [],
                    "wall_seconds": round(time.monotonic() - t0, 1), "budget_seconds": budget},
        }
        text = "".join(f"{i['headline']}{i['what']}{i['why']}" for i in items)
        snap["stats"] = {"items": len(items), "zh_chars": snap_mod.zh_chars(text),
                         "reading_minutes": max(1, round(snap_mod.zh_chars(text) / 320)) if items else 0}
        snap["stats"]["telegram_chars"] = len(brief_render.telegram_html(snap, archive_base))  # renderable, or raise
        snap["stats"]["digest_zh_chars"] = brief_render.digest({**snap, "text_sha256": ""}, archive_base)["zh_chars"]
        path = snap_mod.freeze(export_dir, snap)
        write_renderings(export_dir, snap, archive_base)
        job_runs.finish(conn, run_id, "success", {
            "snapshot_id": snap["snapshot_id"], "text_sha256": snap["text_sha256"], "status": snap["status"],
            "items": len(items), "selection": selection.method, "rejected": len(snap["rejected"]),
            "wall_seconds": snap["run"]["wall_seconds"], "path": str(path)})
        return {"result": "built", "snapshot": snap, "path": str(path)}
    except Exception as exc:  # noqa: BLE001 - record, then re-raise; nothing is frozen on failure
        job_runs.finish(conn, run_id, "failed", {"error": str(exc)[:500]})
        raise
    finally:
        db.release_job_lock(conn, LOCK)


def send_telegram(conn, config, snap: dict, *, chat_id: int, dry_run: bool = True,
                  archive_base: str = brief_render.DEFAULT_ARCHIVE_BASE) -> dict:
    from news_intel.delivery.telegram import TelegramSender, chat_id_hash, mark_attempting

    if snap.get("status") not in ("ready", "short"):
        return {"sent": False, "reason": f"snapshot status {snap.get('status')!r}: nothing to send"}
    key = f"brief:zh_cn:{snap['edition_date']}:{chat_id_hash(chat_id)}"
    text = brief_render.telegram_html(snap, archive_base)
    if not dry_run and not mark_attempting(conn, key, chat_id, "brief"):
        return {"sent": False, "reason": f"{key} was already attempted -- not retrying", "idempotency_key": key}
    sender = TelegramSender(config.secrets.telegram_bot_token, config.secrets.allowed_chat_id_list)
    res = asyncio.run(sender.send(conn, chat_id=chat_id, text=text, kind="brief", idempotency_key=key,
                                  max_chars=brief_render.TELEGRAM_LIMIT, dry_run=dry_run, parse_mode="HTML"))
    return {"sent": not res.dry_run, "dry_run": res.dry_run, "idempotency_key": key,
            "snapshot_id": snap["snapshot_id"], "text_sha256": snap["text_sha256"]}


def _edition(a):
    if a.cutoff:
        return edition_at(datetime.fromisoformat(a.cutoff))
    if a.date:
        return edition_for(date.fromisoformat(a.date))
    return current_edition(datetime.now(UTC))


def main(argv=None) -> int:
    from news_intel.settings import load_config

    ap = argparse.ArgumentParser(prog="python -m news_intel.jobs.brief")
    ap.add_argument("command", choices=["build", "render", "show", "send-telegram"])
    ap.add_argument("--date", help="Beijing edition date (cutoff 20:00 Asia/Shanghai)")
    ap.add_argument("--cutoff", help="explicit cutoff instant, ISO with offset (replays)")
    ap.add_argument("--db", help="database path (default: this checkout's data/news-intel.db)")
    ap.add_argument("--export-dir", help="snapshot directory (default: <data>/exports/brief)")
    ap.add_argument("--revision", type=int, help="make this revision explicitly (keeps earlier ones)")
    ap.add_argument("--archive-base", default=brief_render.DEFAULT_ARCHIVE_BASE)
    ap.add_argument("--no-model", action="store_true", help="build without a provider (deterministic selection only)")
    ap.add_argument("--chat-id", type=int)
    ap.add_argument("--real", action="store_true", help="send-telegram: a real send (default: dry run)")
    a = ap.parse_args(argv)
    config = load_config()
    db_path = Path(a.db) if a.db else config.data_dir / "news-intel.db"
    export_dir = Path(a.export_dir) if a.export_dir else export_dir_for(config)
    edition = _edition(a)
    is_prod = db_path.resolve() == PROD_DB.resolve()
    if is_prod and (a.cutoff or edition.cutoff < current_edition(datetime.now(UTC)).cutoff):
        raise SystemExit("refusing a past-cutoff replay on the production database; pass --db <copy>")
    conn = db.connect(db_path)
    db.run_migrations(conn)
    try:
        if a.command == "build":
            provider = None if a.no_model else brief_provider(config)
            out = build(conn, config, edition, provider=provider, export_dir=export_dir, revision=a.revision,
                        archive_base=a.archive_base)
            s = out["snapshot"]
            print(json.dumps({"result": out["result"], "path": out["path"],
                              **({k: s[k] for k in ("snapshot_id", "status", "text_sha256", "stats")} if s else {})},
                             ensure_ascii=False))
            return 0 if out["result"] in ("built", "reused") else 3
        snap = snap_mod.current(export_dir, edition.edition_date)
        if snap is None:
            raise SystemExit(f"no frozen brief for {edition.edition_date}")
        if a.command == "render":
            print(json.dumps(write_renderings(export_dir, snap, a.archive_base), ensure_ascii=False))
        elif a.command == "show":
            from news_intel.delivery.telegram_html import html_to_plain

            print(html_to_plain(brief_render.telegram_html(snap, a.archive_base)))
        else:
            if a.chat_id is None:
                raise SystemExit("--chat-id is required")
            print(json.dumps(send_telegram(conn, config, snap, chat_id=a.chat_id, dry_run=not a.real,
                                           archive_base=a.archive_base), ensure_ascii=False))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
