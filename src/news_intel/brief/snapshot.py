"""The frozen brief snapshot (schema news-intel/brief-v1, 2026-11-01).

One JSON file per edition and revision under <data>/exports/brief/<edition_date>/, written atomically (temp file,
fsync, rename, then read-only) and never rewritten. <edition_date>/current.json names the revision consumers use.
A later run for the same edition reuses the frozen snapshot instead of selecting and writing again; a new revision
is only ever made on purpose (`revision=` explicitly) and keeps the earlier one.

text_sha256 covers exactly the reader-facing content (edition, cutoff, every item's text and sources), so every
output rendered from the snapshot -- Telegram, archive page, newsletter digest -- can name the text it carries.

status:
  ready  3-5 verified items
  short  1-2 verified items (a quiet or poorly sourced day: never padded)
  empty  no item survived (the newsletter then goes out without news)
A failed run freezes nothing.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path

SCHEMA = "news-intel/brief-v1"
READER_KEYS = ("headline", "what", "why", "why_kind", "brief_line", "sources")


class SnapshotError(RuntimeError):
    pass


def canonical(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def reader_content(snap: dict) -> dict:
    return {
        "edition_date": snap["edition_date"], "cutoff": snap["cutoff"],
        "items": [{k: it.get(k) for k in READER_KEYS} for it in snap["items"]],
    }


def text_sha256(snap: dict) -> str:
    return hashlib.sha256(canonical(reader_content(snap)).encode()).hexdigest()


def zh_chars(text: str) -> int:
    return len(re.findall(r"[一-鿿]", text or ""))


def status_for(items: list[dict]) -> str:
    return "ready" if len(items) >= 3 else "short" if items else "empty"


def edition_dir(export_dir: Path, edition_date) -> Path:
    return export_dir / str(edition_date)


def current(export_dir: Path, edition_date) -> dict | None:
    """The frozen snapshot consumers use for this edition, verified against its own hash, or None."""
    d = edition_dir(export_dir, edition_date)
    pointer = d / "current.json"
    if not pointer.exists():
        return None
    ref = json.loads(pointer.read_text(encoding="utf-8"))
    snap = json.loads((d / ref["file"]).read_text(encoding="utf-8"))
    if snap.get("schema") != SCHEMA:
        raise SnapshotError(f"{d / ref['file']} has schema {snap.get('schema')!r}, not {SCHEMA}")
    if text_sha256(snap) != snap.get("text_sha256"):
        raise SnapshotError(f"{d / ref['file']} does not match its own text_sha256")
    if snap["snapshot_id"] != ref["snapshot_id"]:
        raise SnapshotError(f"current.json names {ref['snapshot_id']}, file holds {snap['snapshot_id']}")
    return snap


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    dfd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def freeze(export_dir: Path, snap: dict) -> Path:
    """Write the snapshot once and point current.json at it. Refuses to overwrite any existing revision."""
    d = edition_dir(export_dir, snap["edition_date"])
    d.mkdir(parents=True, exist_ok=True)
    snap["text_sha256"] = text_sha256(snap)
    snap["frozen_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    path = d / f"{snap['snapshot_id']}.json"
    if path.exists():
        raise SnapshotError(f"{path} already exists; frozen snapshots are never rewritten")
    _atomic_write(path, json.dumps(snap, ensure_ascii=False, indent=1))
    os.chmod(path, 0o444)
    _atomic_write(d / "current.json", json.dumps(
        {"snapshot_id": snap["snapshot_id"], "file": path.name, "text_sha256": snap["text_sha256"],
         "status": snap["status"]}, ensure_ascii=False))
    return path


def snapshot_id(edition_date, revision: int) -> str:
    return f"brief-zh-{edition_date}-r{revision}"
