"""news-intel v2 brief (src/news_intel/brief, jobs/brief.py): schedule/DST, point-in-time pool, editor selection and
its fallback, compose (validator, today check, verifier, rewrite, fail-closed), frozen snapshots, renderings and the
Telegram send's idempotency. A fake provider only -- never the real CLI, never Telegram."""

from __future__ import annotations

import json
import os
from datetime import UTC, date, datetime, timedelta

import pytest

from news_intel.brief import render as R
from news_intel.brief import snapshot as S
from news_intel.brief.compose import compose
from news_intel.brief.pool import build_pool, demotions
from news_intel.brief.schedule import BEIJING, current_edition, edition_for
from news_intel.brief.select import select
from news_intel.jobs import brief as job
from news_intel.llm.errors import LLMUnavailableError
from news_intel.models import EventEnrichment
from tests.conftest import insert_source_row

ED = edition_for(date(2026, 11, 1))  # cutoff 2026-11-01 20:00 +08:00 = 12:00 UTC


# --- schedule -----------------------------------------------------------------------------------------------------

def test_cutoff_is_20_beijing_and_the_same_utc_instant_across_the_us_dst_change():
    for d in (date(2026, 10, 31), date(2026, 11, 1), date(2026, 11, 2)):  # LA leaves DST on 11-01
        e = edition_for(d)
        assert e.cutoff.tzinfo == BEIJING and e.cutoff.hour == 20
        assert e.cutoff_utc.hour == 12 and e.cutoff - e.window_start == timedelta(hours=24)
    # the newsletter sends 22:08 Beijing: a run at 20:05 builds today's edition, one at 19:59 yesterday's
    assert current_edition(datetime(2026, 11, 1, 12, 5, tzinfo=UTC)).edition_date == date(2026, 11, 1)
    assert current_edition(datetime(2026, 11, 1, 11, 59, tzinfo=UTC)).edition_date == date(2026, 10, 31)
    assert ED.label_zh() == "截至北京时间11月1日20:00 · 过去24小时要闻"


# --- pool ---------------------------------------------------------------------------------------------------------

def _event(conn, eid, title, *, edition="en_global", published, fetched, tier=1, source=None, excerpt="",
           url=None, family=None):
    source = source or f"src_{eid}"
    insert_source_row(conn, source, tier)
    conn.execute(
        "INSERT INTO events (id, primary_title, first_seen_at, last_updated_at, published_anchor_at, "
        "confirmation_state, edition_id) VALUES (?, ?, ?, ?, ?, 'reported', ?)",
        (eid, title, fetched.isoformat(), fetched.isoformat(), published.isoformat(), edition))
    cur = conn.execute(
        "INSERT INTO raw_items (source_id, external_id, canonical_url, url_hash, title, normalized_title, "
        "summary_excerpt, language, published_at, fetched_at, content_hash, origin_family, edition_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'en', ?, ?, ?, ?, ?)",
        (source, eid, url or f"https://news.test/{eid}", f"h-{eid}", title, title.lower(), excerpt,
         published.isoformat(), fetched.isoformat(), f"c-{eid}", family or f"{source}.test", edition))
    conn.execute("INSERT INTO event_sources (event_id, raw_item_id, is_primary, supports_claims_json) "
                 "VALUES (?, ?, 1, '[]')", (eid, cur.lastrowid))


T_IN = ED.cutoff_utc - timedelta(hours=3)


def test_point_in_time_pool_ignores_later_sources_and_out_of_window_events(config, conn):
    _event(conn, "E-in", "Earthquake kills at least 120 people in eastern Turkey", published=T_IN, fetched=T_IN,
           excerpt="At least 120 people were killed.")
    # a second report of E-in, fetched after the cutoff: a replay must not see it
    late = ED.cutoff_utc + timedelta(minutes=5)
    insert_source_row(conn, "src_late", 1)
    cur = conn.execute(
        "INSERT INTO raw_items (source_id, external_id, canonical_url, url_hash, title, normalized_title, "
        "summary_excerpt, language, published_at, fetched_at, content_hash, origin_family, edition_id) "
        "VALUES ('src_late', 'late', 'https://late.test/x', 'h-late', 'Death toll rises to 300', 'x', '', 'en', ?, ?, "
        "'c-late', 'late.test', 'en_global')", (late.isoformat(), late.isoformat()))
    conn.execute("INSERT INTO event_sources (event_id, raw_item_id, is_primary, supports_claims_json) "
                 "VALUES ('E-in', ?, 0, '[]')", (cur.lastrowid,))
    _event(conn, "E-late", "Volcano erupts", published=T_IN, fetched=late)  # first seen after the cutoff
    _event(conn, "E-old", "Parliament votes on budget", published=ED.window_start.astimezone(UTC) - timedelta(hours=2),
           fetched=T_IN)
    cands, funnel = build_pool(conn, config, ED)
    assert [c.event_id for c in cands] == ["E-in"]
    assert [s["raw_title"] for s in cands[0].event["sources"]] == [
        "Earthquake kills at least 120 people in eastern Turkey"]
    assert cands[0].bucket == "public_safety"  # on in the brief, whatever the dailies' flag says
    assert funnel["dropped"]["no source published in the window"] == 1


def test_consumer_products_and_soft_news_are_demoted_not_excluded():
    assert demotions("小米 17 Ultra 手机开启预售，售价 5999 元", "") == ["consumer product"]
    assert demotions("Apple unveils new iPhone with a faster chip", "") == ["consumer product"]
    assert demotions("Golden State Valkyries defeat Atlanta Dream to win WNBA title", "") == ["sport/entertainment"]
    assert demotions("国务院常务会议部署稳就业举措", "") == []
    assert demotions("More than 5,000 die from Ebola in DR Congo", "") == []


# --- fake provider ------------------------------------------------------------------------------------------------

class Fake:
    """enrich_events -> a grounded zh enrichment from the first source title (or a scripted one); _complete -> the
    editor's or verifier's JSON from queues."""

    model = "claude-opus-5-5 (Claude Code CLI test)"
    timeout = 120.0

    def __init__(self, editor=None, verdicts=None, bad_events=(), fail_editor=False):
        self.editor, self.verdicts, self.bad = editor, list(verdicts or []), set(bad_events)
        self.fail_editor, self.calls, self.enrich_feedback = fail_editor, [], []

    def enrich_events(self, events):
        ev = events[0]
        self.enrich_feedback.append(list(ev.retry_feedback))
        self.notes = getattr(self, "notes", []) + [[f.text for f in ev.facts if f.kind == "prior_coverage"]]
        title = ev.sources[0]["title"]
        q = [{"source_id": ev.sources[0]["id"], "quote": title}]
        support = {} if ev.event_id in self.bad else {"headline": q, "what": q, "brief": q}
        return [EventEnrichment(event_id=ev.event_id, category=ev.category_candidates[0], chinese_headline=title,
                                chinese_what_happened=f"{title}。", chinese_brief_line=title,
                                supporting_source_ids=[ev.sources[0]["id"]], support=support)]

    def _complete(self, prompt):
        if "news editor of a short daily Chinese news brief" in prompt:
            if self.fail_editor:
                raise LLMUnavailableError("editor down")
            return self.editor
        verdict = self.verdicts.pop(0) if self.verdicts else "pass"
        if isinstance(verdict, Exception):
            raise verdict
        item_id = prompt.split('<item id="')[1].split('"')[0]
        problems = [] if verdict == "pass" else [{"field": "what", "problem": "wrong subject", "fix": "fix it"}]
        return {"items": [{"id": item_id, "verdict": verdict, "problems": problems}]}


def _seed_three(conn):
    _event(conn, "E-a", "地震造成至少120人死亡", edition="zh_cn", published=T_IN, fetched=T_IN)
    _event(conn, "E-b", "Earthquake kills at least 120 people", published=T_IN, fetched=T_IN)
    _event(conn, "E-c", "央行宣布下调存款准备金率", edition="zh_cn", published=T_IN, fetched=T_IN)
    _event(conn, "E-d", "联合国安理会通过决议", edition="zh_cn", published=T_IN, fetched=T_IN)


def _keys(cands):
    return {c.event_id: c.key for c in cands}


def test_editor_merges_duplicates_across_languages_and_reserves_fill_failed_items(config, conn):
    _seed_three(conn)
    cands, _ = build_pool(conn, config, ED)
    k = _keys(cands)
    p = Fake(editor={"selected": [{"ids": [k["E-a"], k["E-b"]], "reason": "deadly quake"},
                                  {"ids": [k["E-c"]], "reason": "policy"}],
                     "reserves": [{"ids": [k["E-d"]], "reason": "un"}], "not_chosen": []},
             bad_events={"E-c"})
    sel = select(p, cands, ED)
    assert sel.method == "editor_model" and [len(x.candidates) for x in sel.picks] == [2, 1]
    items, log = compose(p, sel, ED, deadline=__import__("time").monotonic() + 600)
    assert [i["event_ids"] for i in items][0] in (["E-a", "E-b"], ["E-b", "E-a"])  # one item, both reports
    assert items[1]["event_ids"] == ["E-d"] and items[1]["reserve"] is True  # never beyond the editor's 2
    assert len(items) == 2 and log.rejected[0]["stage"] == "validator"
    assert set(items[0]["editions"]) == {"en_global", "zh_cn"}


def test_unknown_ids_from_the_editor_fall_back_to_the_deterministic_order(config, conn):
    _seed_three(conn)
    cands, _ = build_pool(conn, config, ED)
    sel = select(Fake(editor={"selected": [{"ids": ["c99"], "reason": "x"}]}), cands, ED)
    assert sel.method == "deterministic_fallback" and "unknown candidate ids" in sel.error
    assert select(Fake(fail_editor=True), cands, ED).method == "deterministic_fallback"


def test_verifier_fail_rewrites_once_with_its_problems_and_a_second_fail_or_no_verdict_drops(config, conn):
    _seed_three(conn)
    cands, _ = build_pool(conn, config, ED)
    k = _keys(cands)
    editor = {"selected": [{"ids": [k["E-a"]], "reason": "r"}, {"ids": [k["E-c"]], "reason": "r"},
                           {"ids": [k["E-d"]], "reason": "r"}]}
    p = Fake(editor=editor, verdicts=["fail", "pass", "fail", "fail", LLMUnavailableError("verifier down")])
    items, log = compose(p, select(p, cands, ED), ED, deadline=__import__("time").monotonic() + 600)
    assert [i["event_ids"] for i in items] == [["E-a"]]
    v = items[0]["verification"]
    assert v["rewritten_after_fact_check"] and v["first_fact_check_problems"][0]["problem"] == "wrong subject"
    assert any("fact-check (what): wrong subject" in r for r in p.enrich_feedback[1])  # the rewrite saw it
    assert [r["stage"] for r in log.rejected] == ["verifier (fail)", "verifier (unverified)"]


def test_old_news_is_refused_even_when_well_written(config, conn):
    old = ED.window_start.astimezone(UTC) - timedelta(hours=30)
    _event(conn, "E-x", "央行宣布下调存款准备金率", edition="zh_cn", published=T_IN, fetched=T_IN)
    cands, _ = build_pool(conn, config, ED)
    # an older source for the same event, the only one the writer quotes
    cands[0].event["sources"] = [{**cands[0].event["sources"][0], "published_at": old.isoformat(),
                                  "url": "https://news.test/old"}]
    p = Fake(editor={"selected": [{"ids": [cands[0].key], "reason": "r"}]})
    items, log = compose(p, select(p, cands, ED), ED, deadline=__import__("time").monotonic() + 600)
    assert items == [] and log.rejected[0]["stage"] == "today/length"


# --- snapshot, renderings, job ------------------------------------------------------------------------------------

def _build(config, conn, tmp_path, provider, **kw):
    return job.build(conn, config, ED, provider=provider, export_dir=tmp_path / "brief", **kw)


def test_build_freezes_once_and_a_retry_reuses_the_frozen_text(config, conn, tmp_path):
    _seed_three(conn)
    cands, _ = build_pool(conn, config, ED)
    k = _keys(cands)
    editor = {"selected": [{"ids": [k["E-a"], k["E-b"]], "reason": "r"}, {"ids": [k["E-c"]], "reason": "r"},
                           {"ids": [k["E-d"]], "reason": "r"}]}
    first = _build(config, conn, tmp_path, Fake(editor=editor))
    snap = first["snapshot"]
    assert first["result"] == "built" and snap["status"] == "ready" and len(snap["items"]) == 3
    assert snap["edition_date"] == "2026-11-01" and snap["cutoff"] == "2026-11-01T20:00:00+08:00"
    assert snap["timezone"] == "Asia/Shanghai" and snap["selection_mode"] == "automatic"
    assert snap["snapshot_id"] == "brief-zh-2026-11-01-r1" and len(snap["text_sha256"]) == 64
    path = tmp_path / "brief" / "2026-11-01" / "brief-zh-2026-11-01-r1.json"
    assert oct(os.stat(path).st_mode)[-3:] == "444"
    again = _build(config, conn, tmp_path, Fake(editor={"selected": []}))
    assert again["result"] == "reused" and again["snapshot"]["text_sha256"] == snap["text_sha256"]
    with pytest.raises(S.SnapshotError):
        S.freeze(tmp_path / "brief", {**snap, "status": "ready"})  # same id: never rewritten
    second = _build(config, conn, tmp_path, Fake(editor={"selected": []}), revision=2)
    assert second["snapshot"]["status"] == "empty" and path.exists()  # r1 kept, current -> r2
    assert S.current(tmp_path / "brief", "2026-11-01")["snapshot_id"] == "brief-zh-2026-11-01-r2"


def test_a_tampered_snapshot_is_detected(config, conn, tmp_path):
    _seed_three(conn)
    cands, _ = build_pool(conn, config, ED)
    out = _build(config, conn, tmp_path, Fake(editor={"selected": [{"ids": [cands[0].key], "reason": "r"}]}))
    path = tmp_path / "brief" / "2026-11-01" / f"{out['snapshot']['snapshot_id']}.json"
    os.chmod(path, 0o644)
    data = json.loads(path.read_text())
    data["items"][0]["what"] = "被改动的文字。"
    path.write_text(json.dumps(data, ensure_ascii=False))
    with pytest.raises(S.SnapshotError, match="text_sha256"):
        S.current(tmp_path / "brief", "2026-11-01")


def test_renderings_come_from_the_snapshot_and_name_it(config, conn, tmp_path):
    _seed_three(conn)
    cands, _ = build_pool(conn, config, ED)
    out = _build(config, conn, tmp_path, Fake(editor={"selected": [{"ids": [cands[0].key], "reason": "r"}]}))
    snap = out["snapshot"]
    assert snap["status"] == "short"
    tg = R.telegram_html(snap)
    assert "每日要闻｜2026年11月1日" in tg and "截至北京时间2026年11月1日 20:00" in tg
    assert "https://starhub.vip/history/news/2026-11-01/" in tg
    dig = R.digest(snap)
    assert dig["snapshot_id"] == snap["snapshot_id"] and dig["text_sha256"] == snap["text_sha256"]
    assert dig["text"].startswith("今日新闻速览（截至北京时间2026年11月1日 20:00）")
    page = R.archive_html(snap)
    assert "Asia/Shanghai" in page and snap["snapshot_id"] in page and '<link rel="canonical"' in page
    d = tmp_path / "brief" / "2026-11-01"
    assert (d / f"{snap['snapshot_id']}.telegram.html").read_text() == tg


def test_telegram_send_is_from_the_frozen_snapshot_and_never_retries_an_attempted_date(config, conn, tmp_path,
                                                                                         monkeypatch):
    from news_intel.delivery import telegram as tgmod

    _seed_three(conn)
    cands, _ = build_pool(conn, config, ED)
    snap = _build(config, conn, tmp_path, Fake(editor={"selected": [{"ids": [cands[0].key], "reason": "r"}]}))[
        "snapshot"]
    config.secrets.telegram_allowed_chat_ids = "555"
    sent = []

    async def fake_send(self, conn, **kw):
        sent.append(kw)
        return tgmod.SendResult(kw["idempotency_key"], False, ["1"], kw["dry_run"])

    monkeypatch.setattr(tgmod.TelegramSender, "send", fake_send)
    dry = job.send_telegram(conn, config, snap, chat_id=555, dry_run=True)
    assert dry["dry_run"] and dry["idempotency_key"].startswith("brief:zh_cn:2026-11-01:")
    assert sent[0]["text"] == R.telegram_html(snap)
    real = job.send_telegram(conn, config, snap, chat_id=555, dry_run=False)
    assert real["sent"] is True
    again = job.send_telegram(conn, config, snap, chat_id=555, dry_run=False)
    assert again["sent"] is False and "already attempted" in again["reason"]
    assert job.send_telegram(conn, config, {**snap, "status": "empty"}, chat_id=555)["sent"] is False


def test_a_reserve_on_a_topic_already_in_the_brief_is_skipped(config, conn):
    _seed_three(conn)
    cands, _ = build_pool(conn, config, ED)
    k = _keys(cands)
    p = Fake(editor={"selected": [{"ids": [k["E-a"]], "topic": "地震", "reason": "r"},
                                  {"ids": [k["E-c"]], "topic": "央行", "reason": "r"}],
                     "reserves": [{"ids": [k["E-b"]], "topic": "地震", "reason": "r"},
                                  {"ids": [k["E-d"]], "topic": "联合国", "reason": "r"}]},
             bad_events={"E-c"})
    items, log = compose(p, select(p, cands, ED), ED, deadline=__import__("time").monotonic() + 600)
    assert [i["event_ids"] for i in items] == [["E-a"], ["E-d"]]
    assert [r["stage"] for r in log.rejected] == ["validator", "same topic"]


def test_earlier_brief_headlines_reach_the_editor_and_a_continuation_reaches_writer_and_verifier(config, conn):
    from news_intel.brief.select import build_select_prompt

    _seed_three(conn)
    cands, _ = build_pool(conn, config, ED)
    prompt = build_select_prompt(cands, ED, prior=[{"edition_date": "2026-10-31", "headline": "央行宣布降准"}])
    assert "- 2026-10-31: 央行宣布降准" in prompt
    k = _keys(cands)
    p = Fake(editor={"selected": [{"ids": [k["E-c"]], "topic": "央行", "continues": "央行宣布降准", "reason": "r"}]})
    seen = []
    orig = p._complete

    def spy(prompt):
        seen.append(prompt)
        return orig(prompt)

    p._complete = spy
    items, _ = compose(p, select(p, cands, ED), ED, deadline=__import__("time").monotonic() + 600)
    assert items and items[0]["continues"] == "央行宣布降准"
    assert any("continues an earlier brief item: 「央行宣布降准」" in s for s in seen)  # the verifier saw it
    assert "Lead with what is new since then" in p.notes[0][0] and "央行宣布降准" in p.notes[0][0]  # so did the writer


def test_the_verifier_sees_the_writers_name_renderings():
    from news_intel.brief.compose import build_verify_prompt
    from news_intel.pipeline.editorial_enrichment import EditorialResult

    ed = EditorialResult(what_happened=None, why_it_matters=None, chinese_what_happened="鲁比奥称。",
                         chinese_takeaway=None,
                         category=None, source="llm_validated", chinese_headline="鲁比奥", support={},
                         sources=[{"id": "s0", "publisher": "SCMP", "title": "Rubio says", "excerpt": "", "url": "u",
                                   "published_at": "", "language": "en"}],
                         name_renderings=[{"zh": "鲁比奥", "source_name": "Rubio", "source_id": "s0"}])
    prompt = build_verify_prompt("i1", ed, ED)
    assert 'name renderings the writer used: 鲁比奥 = "Rubio" [s0]' in prompt
    assert "standard Chinese name" in prompt
