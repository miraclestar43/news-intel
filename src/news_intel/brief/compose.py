"""Write and check each chosen item (2026-11-01).

Per item, in the editor's order (reserves fill in for items that fail, never beyond the editor's own count):
  1. text: pipeline/editorial_enrichment.enrich_story on the zh_cn contract, with every source of the merged
     duplicates as evidence and the cutoff as reference time -- the same validator the dailies use;
  2. today: the "what" field must rest on at least one source published inside the window (old news is refused);
  3. verify: an independent fact-check call (verify_brief.md) against the item's own sources -- relation,
     subject, period, sign, attribution, certainty, headline/body agreement, supported why line;
  4. one rewrite with the verifier's problems as feedback, then a second verification; still failing -> dropped.
A verifier call that fails leaves the item unverified, and an unverified item is dropped (fail closed). Items on
the deterministic floor never enter the brief: a short brief is better than an unchecked one.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

from news_intel.brief.pool import BUCKET_LABEL_ZH
from news_intel.brief.select import Pick, Selection
from news_intel.llm.errors import LLMUnavailableError
from news_intel.llm.prompts import load_template, render, render_sources_block
from news_intel.pipeline.editorial_enrichment import EditorialResult, enrich_story

MAX_WHAT_CHARS = 220


@dataclass
class ComposeLog:
    accepted: list[dict] = field(default_factory=list)
    rejected: list[dict] = field(default_factory=list)
    verifier_calls: list[dict] = field(default_factory=list)


def _parse_ts(ts) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _fields(ed: EditorialResult) -> dict:
    return {"headline": ed.chinese_headline or "", "what": ed.chinese_what_happened or "",
            "why": ed.chinese_takeaway or "", "why_kind": ed.why_kind or "", "brief": ed.chinese_brief_line or ""}


def _cited_sources(ed: EditorialResult) -> list[dict]:
    ids = {q["source_id"] for quotes in (ed.support or {}).values() for q in quotes}
    return [s for s in ed.sources or [] if s["id"] in ids]


def today_problem(ed: EditorialResult, edition) -> str | None:
    """None when the what field rests on a source published inside the window."""
    by_id = {s["id"]: s for s in ed.sources or []}
    start, cutoff = edition.window_start.astimezone(UTC), edition.cutoff_utc
    for q in (ed.support or {}).get("what") or []:
        pub = _parse_ts(by_id.get(q["source_id"], {}).get("published_at"))
        if pub and start <= pub < cutoff:
            return None
    return "what rests on no source published in the 24 hours before the cutoff (no new development)"


def build_verify_prompt(item_id: str, ed: EditorialResult, edition, continues: str | None = None) -> str:
    f = _fields(ed)
    quotes = "\n".join(f"  {group}: " + " | ".join(f'[{q["source_id"]}] "{q["quote"]}"' for q in qs)
                       for group, qs in (ed.support or {}).items())
    cont = (f"this item continues an earlier brief item: 「{continues}」 -- 'what' must lead with a newer "
            "development\n") if continues else ""
    names = "; ".join(f'{r["zh"]} = "{r["source_name"]}" [{r["source_id"]}]' for r in ed.name_renderings or [])
    cont += f"name renderings the writer used: {names}\n" if names else ""
    block = (f'<item id="{item_id}">\n{cont}headline: {f["headline"]}\nwhat: {f["what"]}\n'
             f'why ({f["why_kind"] or "none"}): {f["why"] or "(none)"}\nbrief: {f["brief"]}\n'
             f"quotes the writer cited:\n{quotes}\n</item>\n"
             f"Sources for {item_id}:\n{render_sources_block(_cited_sources(ed) or ed.sources or [])}")
    return render(load_template("verify_brief.md"), cutoff=edition.cutoff.isoformat(), items_block=block)


def verify(provider, item_id: str, ed: EditorialResult, edition, log: ComposeLog,
           continues: str | None = None) -> tuple[str, list[dict]]:
    """("pass" | "fail" | "unverified", problems)."""
    t0 = time.monotonic()
    try:
        data = provider._complete(build_verify_prompt(item_id, ed, edition, continues))
        entry = next((i for i in data.get("items") or [] if i.get("id") == item_id), None)
        if entry is None or entry.get("verdict") not in ("pass", "fail"):
            raise LLMUnavailableError(f"verifier gave no verdict for {item_id}")
        problems = [p for p in entry.get("problems") or [] if isinstance(p, dict)]
        verdict = entry["verdict"] if entry["verdict"] == "fail" or not problems else "fail"
        log.verifier_calls.append({"item": item_id, "verdict": verdict,
                                   "latency_ms": round((time.monotonic() - t0) * 1000)})
        return verdict, problems
    except LLMUnavailableError as exc:
        log.verifier_calls.append({"item": item_id, "verdict": "unverified", "error": str(exc)[:200],
                                   "latency_ms": round((time.monotonic() - t0) * 1000)})
        return "unverified", [{"field": "-", "problem": f"verifier unavailable: {exc}"[:200]}]


def _feedback(problems: list[dict]) -> list[str]:
    return [f"fact-check ({p.get('field', '?')}): {p.get('problem', '')} -- {p.get('fix', '')}"[:400]
            for p in problems][:8]


def _write(provider, pick: Pick, edition, deadline, feedback=None) -> EditorialResult:
    primary = pick.candidates[0]
    note = None
    earlier = pick.continues or (primary.previously or {}).get("headline")
    if earlier:
        note = (f"This continues an earlier brief item: 「{earlier}」. Lead with what is new since then; "
                "do not present the earlier facts as today's news.")
    return enrich_story(
        primary.event, None, BUCKET_LABEL_ZH.get(primary.bucket, "综合"), primary.event.get("entities"),
        edition_id="zh_cn", update_note=note, provider=provider, deadline=deadline,
        related=[c.event for c in pick.candidates[1:]], reference_time=edition.cutoff, feedback=feedback,
    )


def compose(provider, selection: Selection, edition, *, deadline: float) -> tuple[list[dict], ComposeLog]:
    log = ComposeLog()
    target = len(selection.picks)
    items: list[dict] = []
    topics: set[str] = set()
    for pick in [*selection.picks, *selection.reserves]:
        if len(items) >= target:
            break
        if pick.reserve and pick.topic and pick.topic.strip().lower() in topics:
            log.rejected.append({"title": pick.candidates[0].title, "event_ids": [c.event_id for c in pick.candidates],
                                 "reserve": True, "stage": "same topic", "reasons": [f"topic {pick.topic!r} is in"]})
            continue
        head = pick.candidates[0]
        record = {"title": head.title, "event_ids": [c.event_id for c in pick.candidates], "reserve": pick.reserve}
        item_id = f"i{len(items) + 1}"
        ed = _write(provider, pick, edition, deadline)
        stage, reasons, first_problems, rewritten = None, [], [], False
        if ed.source != "llm_validated":
            stage, reasons = "validator", [ed.fallback_reason or "?"]
        elif (p := today_problem(ed, edition)) or len(ed.chinese_what_happened or "") > MAX_WHAT_CHARS:
            stage, reasons = "today/length", [p or f"what is longer than {MAX_WHAT_CHARS} characters"]
        else:
            earlier = pick.continues or (head.previously or {}).get("headline")
            verdict, problems = verify(provider, item_id, ed, edition, log, earlier)
            if verdict == "fail":
                first_problems, rewritten = problems, True
                ed2 = _write(provider, pick, edition, deadline, feedback=_feedback(problems))
                if ed2.source != "llm_validated":
                    stage, reasons = "validator after fact-check", [ed2.fallback_reason or "?"]
                elif p2 := today_problem(ed2, edition):
                    stage, reasons = "today/length", [p2]
                else:
                    ed = ed2
                    verdict, problems = verify(provider, item_id, ed, edition, log, earlier)
            if stage is None and verdict != "pass":
                stage, reasons = f"verifier ({verdict})", [x.get("problem", "") for x in problems][:6]
        record.update(stage=stage, reasons=reasons, attempts=ed.attempts, latency_ms=ed.latency_ms)
        if stage is not None:
            log.rejected.append(record)
            continue
        f = _fields(ed)
        cited = _cited_sources(ed)
        items.append({
            "id": item_id,
            "headline": f["headline"], "what": f["what"], "why": f["why"] or "", "why_kind": f["why_kind"] or "",
            "brief_line": f["brief"] or f["headline"],
            "sources": [{"name": s["publisher"], "title": s["title"], "url": s["url"],
                         "published_at": s["published_at"], "language": s["language"]} for s in cited][:3],
            "support": ed.support,
            "name_renderings": ed.name_renderings or [],
            "evidence_sources": [{k: s[k] for k in ("id", "publisher", "title", "url", "published_at", "language")}
                                 for s in ed.sources or []],
            "event_ids": record["event_ids"],
            "editions": sorted({c.edition_id for c in pick.candidates}),
            "bucket": head.bucket,
            "selection_reason": pick.reason,
            "topic": pick.topic,
            "continues": pick.continues,
            "reserve": pick.reserve,
            "previously": head.previously,
            "verification": {
                "validator": "pass", "verifier": "pass", "rewritten_after_fact_check": rewritten,
                "first_fact_check_problems": first_problems, "model": ed.model_used, "attempts": ed.attempts,
                "latency_ms": round(ed.latency_ms or 0),
            },
        })
        if pick.topic:
            topics.add(pick.topic.strip().lower())
        log.accepted.append(record)
    return items, log
