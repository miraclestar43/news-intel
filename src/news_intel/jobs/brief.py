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
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from news_intel import db
from news_intel.brief import render as brief_render
from news_intel.brief import snapshot as snap_mod
from news_intel.brief.compose import compose
from news_intel.brief.pool import build_pool, prior_headlines
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
            "editions": [c.edition_id for c in p.candidates], "topic": p.topic, "continues": p.continues,
            "reason": p.reason}


def _short_model(model: str | None) -> str:
    return (model or "none").split(" (")[0]


FINAL_AFTER = timedelta(minutes=40)  # the 20:50 Beijing run (cutoff 20:00) is the edition's last attempt
TECHNICAL_MARKERS = ("provider unavailable", "editorial run budget exhausted", "verifier unavailable",
                     "no provider configured")


def renderings(snap: dict, archive_base: str) -> dict[str, str]:
    """Every file derived from a frozen snapshot, by name. Deterministic: the same snapshot gives the same bytes."""
    stem = snap["snapshot_id"]
    dig = brief_render.digest(snap, archive_base)
    return {
        f"{stem}.telegram.html": brief_render.telegram_html(snap, archive_base),
        f"{stem}.digest.json": json.dumps(dig, ensure_ascii=False, indent=1),
        f"{stem}.archive.html": brief_render.archive_html(snap, archive_base),
    }


def ensure_renderings(export_dir: Path, snap: dict, archive_base: str) -> list[str]:
    """Make every rendering of a frozen snapshot present and byte-exact, reading each back. Idempotent: a missing or
    damaged file is rewritten from the snapshot; the news text and its hash never change. Returns the repaired
    names. A manifest records each file's sha256 against the snapshot's text_sha256."""
    d = snap_mod.edition_dir(export_dir, snap["edition_date"])
    repaired = []
    manifest = {"snapshot_id": snap["snapshot_id"], "text_sha256": snap["text_sha256"], "files": {}}
    for name, text in renderings(snap, archive_base).items():
        path = d / name
        if not path.exists() or path.read_text(encoding="utf-8") != text:
            snap_mod._atomic_write(path, text)
            repaired.append(name)
        if path.read_text(encoding="utf-8") != text:
            raise snap_mod.SnapshotError(f"{path} does not read back as rendered")
        manifest["files"][name] = hashlib.sha256(text.encode()).hexdigest()
    snap_mod._atomic_write(d / f"{snap['snapshot_id']}.renderings.json", json.dumps(manifest, indent=1))
    return repaired


def write_renderings(export_dir: Path, snap: dict, archive_base: str) -> dict:
    ensure_renderings(export_dir, snap, archive_base)
    dig = brief_render.digest(snap, archive_base)
    return {"digest_chars": dig["chars"], "digest_zh_chars": dig["zh_chars"], "archive_url": dig["url"]}


def technical_failures(provider, selection, log) -> list[str]:
    """Failures of the machinery, not of the news: no provider, an editor call that failed (deterministic fallback
    used), items lost to an unavailable provider, the run budget or an unavailable verifier."""
    out = []
    if provider is None:
        out.append("no provider configured")
    if selection.error and selection.method == "deterministic_fallback":
        out.append(f"editor: {selection.error}"[:300])
    for r in (log.rejected if log else []):
        why = " ".join(r.get("reasons") or [])
        if r.get("stage") == "verifier (unverified)" or any(m in why for m in TECHNICAL_MARKERS):
            out.append(f"{r.get('stage')}: {r.get('title', '')[:60]}: {why[:200]}")
    return out


def _attempt_record(export_dir: Path, snap: dict, outcome: str) -> Path:
    """Failure evidence for a run that froze nothing: everything it found and produced, never a consumer file."""
    d = snap_mod.edition_dir(export_dir, snap["edition_date"]) / "attempts"
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{outcome}.json"
    snap_mod._atomic_write(path, json.dumps({**snap, "attempt_outcome": outcome}, ensure_ascii=False, indent=1))
    return path


def _complete_commit(export_dir: Path, snap: dict, archive_base: str) -> list[str]:
    repaired = ensure_renderings(export_dir, snap, archive_base)
    snap_mod.publish(export_dir, snap)
    return repaired


def build(conn, config, edition, *, provider=None, export_dir: Path, revision: int | None = None,
          archive_base: str = brief_render.DEFAULT_ARCHIVE_BASE, budget: float = BUDGET_SECONDS,
          now: datetime | None = None, final: bool | None = None) -> dict:
    """Returns {"result", "snapshot", "path", ...}. result:
      reused          the edition was already published; its renderings were checked (and repaired if needed)
      completed       a frozen but unpublished revision was found: renderings made, then published (no rewrite)
      built           published; snapshot["run_outcome"] is complete (incl. a real short day) or no_qualifying_news
      built_degraded  last attempt: published with the verified items it has, but technical failures cost items
      retry_later     technical failures before the last attempt: nothing frozen, evidence in attempts/
      failed          last attempt, technical failures and no verified item: nothing frozen
      skipped_lock    another run holds the lock
    `final`: whether this is the edition's last attempt (default: from `now` -- cutoff + 40 min or later)."""
    now = now or datetime.now(UTC)
    final = (now >= edition.cutoff + FINAL_AFTER) if final is None else final
    if revision is None:
        existing = snap_mod.current(export_dir, edition.edition_date)
        if existing is not None:
            repaired = ensure_renderings(export_dir, existing, archive_base)
            return {"result": "reused", "snapshot": existing, "path": None, "repaired": repaired}
        pending = snap_mod.unpublished(export_dir, edition.edition_date)
        if pending is not None:
            repaired = _complete_commit(export_dir, pending, archive_base)
            return {"result": "completed", "snapshot": pending, "path": None, "repaired": repaired}
        revision = snap_mod.next_revision(export_dir, edition.edition_date)
    if not db.acquire_job_lock(conn, LOCK, ttl_seconds=int(budget) + 600):
        return {"result": "skipped_lock", "snapshot": None, "path": None}
    run_id = job_runs.start(conn, "brief", edition_id="zh_cn")
    t0 = time.monotonic()
    try:
        cands, funnel = build_pool(conn, config, edition, export_dir=export_dir, limit=EDITOR_POOL)
        prior = prior_headlines(export_dir, edition.edition_date)
        selection = select(provider, cands[:EDITOR_POOL], edition, MAX_ITEMS, prior)
        items, log = compose(provider, selection, edition, deadline=t0 + budget) if provider else ([], None)
        writer = next((i["verification"]["model"] for i in items), getattr(provider, "model", None))
        technical = technical_failures(provider, selection, log)
        if not technical:
            run_outcome = "complete" if items else "no_qualifying_news"
        else:
            run_outcome = "degraded_technical"
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
            "run_outcome": run_outcome,
            "technical_failures": technical,
            "final_attempt": final,
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
            "prior_headlines": prior,
            "models": {"writer": writer, "editor": selection.model, "verifier": writer,
                       "writer_short": _short_model(writer)},
            "provenance": provenance(),
            "run": {"provider_calls": list(getattr(provider, "calls", []) or []),
                    "verifier_calls": log.verifier_calls if log else [],
                    "wall_seconds": round(time.monotonic() - t0, 1), "budget_seconds": budget},
        }
        text = "".join(f"{i['headline']}{i['what']}{i['why']}" for i in items)
        snap["stats"] = {"items": len(items), "chars": snap_mod.chars(text), "zh_chars": snap_mod.zh_chars(text),
                         "reading_minutes": max(1, round(snap_mod.chars(text) / 320)) if items else 0}
        snap["text_sha256"] = snap_mod.text_sha256(snap)
        rendered = renderings(snap, archive_base)  # renderable, or raise before anything is frozen
        snap["stats"]["telegram_chars"] = len(rendered[f"{snap['snapshot_id']}.telegram.html"])
        dig = brief_render.digest(snap, archive_base)
        snap["stats"].update(digest_chars=dig["chars"], digest_zh_chars=dig["zh_chars"])
        if technical and (not final or not items):
            outcome = "retry_later" if not final else "failed"
            evidence = _attempt_record(export_dir, snap, outcome)
            job_runs.finish(conn, run_id, "failed", {
                "outcome": outcome, "technical_failures": technical, "items": len(items), "evidence": str(evidence),
                "wall_seconds": snap["run"]["wall_seconds"]})
            return {"result": outcome, "snapshot": None, "path": None, "evidence": str(evidence),
                    "technical_failures": technical}
        path = snap_mod.write_frozen(export_dir, snap)
        _complete_commit(export_dir, snap, archive_base)
        result = "built_degraded" if technical else "built"
        job_runs.finish(conn, run_id, "degraded" if technical else "success", {
            "snapshot_id": snap["snapshot_id"], "text_sha256": snap["text_sha256"], "status": snap["status"],
            "run_outcome": run_outcome, "technical_failures": technical, "items": len(items),
            "selection": selection.method, "rejected": len(snap["rejected"]),
            "wall_seconds": snap["run"]["wall_seconds"], "path": str(path)})
        return {"result": result, "snapshot": snap, "path": str(path)}
    except Exception as exc:  # noqa: BLE001 - record, then re-raise; a frozen-but-unpublished revision is completed
        job_runs.finish(conn, run_id, "failed", {"error": str(exc)[:500]})  # by the next run, never rewritten
        raise
    finally:
        db.release_job_lock(conn, LOCK)


def revise_item(conn, config, edition, *, provider, export_dir: Path, item_id: str, feedback: list[str],
                archive_base: str = brief_render.DEFAULT_ARCHIVE_BASE, budget: float = 600.0) -> dict:
    """Have the model revise ONE item of the published edition and re-check it fully (validator, today, verifier),
    with `feedback` as its instruction; nothing else is re-selected or rewritten. On success the edition is frozen as
    the next revision: every other item copied unchanged, the revised one replaced, with revision_of and the old and
    new text recorded; the earlier revision stays as it was. On failure nothing is frozen and the evidence is kept.
    A consumer that already bound the earlier revision keeps it (history-in-practice binds once)."""
    from news_intel.brief.compose import ComposeLog, item_record, write_and_check
    from news_intel.brief.select import Pick

    cur = snap_mod.current(export_dir, edition.edition_date)
    if cur is None:
        raise SystemExit(f"no published brief for {edition.edition_date}")
    old = next((i for i in cur["items"] if i["id"] == item_id), None)
    if old is None:
        raise SystemExit(f"{cur['snapshot_id']} has no item {item_id}")
    cands, _ = build_pool(conn, config, edition, export_dir=export_dir, limit=EDITOR_POOL)
    by_event = {c.event_id: c for c in cands}
    group = [by_event[e] for e in old["event_ids"] if e in by_event]
    if len(group) != len(old["event_ids"]):
        raise SystemExit(f"events of {item_id} are not in the pool rebuilt at the cutoff: {old['event_ids']}")
    pick = Pick(group, old.get("selection_reason", ""), reserve=old.get("reserve", False), topic=old.get("topic", ""),
                continues=old.get("continues"))
    log = ComposeLog()
    t0 = time.monotonic()
    ed, stage, reasons, first_problems, rewritten = write_and_check(provider, pick, item_id, edition,
                                                                    t0 + budget, log, feedback=feedback)
    revision = snap_mod.next_revision(export_dir, edition.edition_date)
    snap = json.loads(json.dumps(cur))
    for k in ("text_sha256", "frozen_at"):
        snap.pop(k, None)
    snap["snapshot_id"] = snap_mod.snapshot_id(edition.edition_date, revision)
    snap["revision"] = revision
    snap["generated_at"] = datetime.now(UTC).astimezone(BEIJING).isoformat(timespec="seconds")
    record = {"item": item_id, "feedback": feedback, "stage": stage, "reasons": reasons,
              "attempts": ed.attempts, "verifier_calls": log.verifier_calls,
              "provider_calls": list(getattr(provider, "calls", []) or []),
              "code_commit": provenance()["code_commit"]}
    if stage is not None:
        snap["revision_attempt"] = record
        evidence = _attempt_record(export_dir, snap, "revision_refused")
        return {"result": "revision_refused", "snapshot": None, "evidence": str(evidence), "reasons": reasons}
    new = item_record(ed, pick, item_id, rewritten, first_problems)
    snap["items"] = [new if i["id"] == item_id else i for i in cur["items"]]
    snap["revision_of"] = cur["snapshot_id"]
    snap["revised_items"] = [{**record, "old": {k: old[k] for k in ("headline", "what", "why", "brief_line")},
                              "new": {k: new[k] for k in ("headline", "what", "why", "brief_line")}}]
    snap["provenance"] = {**snap["provenance"], "revision_code_commit": record["code_commit"]}
    text = "".join(f"{i['headline']}{i['what']}{i['why']}" for i in snap["items"])
    snap["stats"].update(chars=snap_mod.chars(text), zh_chars=snap_mod.zh_chars(text))
    snap["text_sha256"] = snap_mod.text_sha256(snap)
    renderings(snap, archive_base)
    path = snap_mod.write_frozen(export_dir, snap)
    _complete_commit(export_dir, snap, archive_base)
    return {"result": "revised", "snapshot": snap, "path": str(path)}


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
    ap.add_argument("command", choices=["build", "render", "show", "send-telegram", "revise-item"])
    ap.add_argument("--date", help="Beijing edition date (cutoff 20:00 Asia/Shanghai)")
    ap.add_argument("--cutoff", help="explicit cutoff instant, ISO with offset (replays)")
    ap.add_argument("--db", help="database path (default: this checkout's data/news-intel.db)")
    ap.add_argument("--export-dir", help="snapshot directory (default: <data>/exports/brief)")
    ap.add_argument("--revision", type=int, help="make this revision explicitly (keeps earlier ones)")
    ap.add_argument("--archive-base", default=brief_render.DEFAULT_ARCHIVE_BASE)
    ap.add_argument("--no-model", action="store_true", help="build without a provider (deterministic selection only)")
    ap.add_argument("--final", action="store_true", help="treat this run as the edition's last attempt")
    ap.add_argument("--item", help="revise-item: the item id (i1..i5)")
    ap.add_argument("--feedback-file", help="revise-item: the revision instruction, one point per line")
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
                        archive_base=a.archive_base, final=True if a.final else None)
            s = out["snapshot"]
            print(json.dumps({"result": out["result"], "path": out["path"],
                              **{k: out[k] for k in ("repaired", "evidence", "technical_failures") if k in out},
                              **({k: s[k] for k in ("snapshot_id", "status", "text_sha256", "stats")} if s else {})},
                             ensure_ascii=False))
            return {"built": 0, "reused": 0, "completed": 0, "built_degraded": 3, "retry_later": 75,
                    "failed": 1, "skipped_lock": 3}[out["result"]]
        if a.command == "revise-item":
            lines = Path(a.feedback_file).read_text(encoding="utf-8").splitlines()
            feedback = [ln.strip() for ln in lines if ln.strip()]
            out = revise_item(conn, config, edition, provider=brief_provider(config), export_dir=export_dir,
                              item_id=a.item, feedback=feedback, archive_base=a.archive_base)
            s = out.get("snapshot")
            print(json.dumps({"result": out["result"], **({k: s[k] for k in ("snapshot_id", "revision_of",
                                                                              "text_sha256")} if s else {}),
                              **{k: out[k] for k in ("evidence", "reasons") if k in out}}, ensure_ascii=False))
            return 0 if out["result"] == "revised" else 1
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
