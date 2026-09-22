"""Rendering contract, end to end (2026-09-22 production incident).

Sep 21 [GLOBAL DAILY] and [中文日报] showed literal `<a href="...">title</a>`,
`AMD&#x27;s`, `AI &amp; Semiconductors`. Root cause: run_daily sent the
(correct, once-escaped) Telegram HTML with parse_mode=HTML only when
`fallback_used` was true -- i.e. only when NO story had a validated LLM
editorial result. Every [GLOBAL DAILY] since Sep 1 and [中文日报] since
Sep 21 was sent with parse_mode=None, so Telegram displayed the markup.

Contract pinned here:
- stored/canonical text is plain Unicode (entities decoded once at
  normalization, no presentation HTML);
- the Telegram message is HTML, escaped exactly once, and is ALWAYS sent
  with parse_mode="HTML" -- on every path (LLM-validated, deterministic
  fallback, whole-digest plain text) and for every split part;
- CLI/plain output has no tags and no entities.

The oracle below mimics how Telegram DISPLAYS a message and is independent
of the implementation under test.
"""

from __future__ import annotations

import sys
import types
from datetime import UTC, datetime
from html.parser import HTMLParser

import pytest

from news_intel.adapters.base import CollectedItem
from news_intel.delivery.daily_production_formatter import render_production_daily
from news_intel.delivery.daily_production_formatter_zh import render_production_daily_zh
from news_intel.jobs import daily as daily_module
from news_intel.jobs.daily import run_daily
from news_intel.pipeline.confirmation import update_confirmation_state
from news_intel.pipeline.daily_selection import SelectionResult, Story
from news_intel.pipeline.editorial_enrichment import EditorialResult
from news_intel.pipeline.normalize import normalize
from news_intel.pipeline.scoring import persist_score, score_event
from tests.conftest import insert_source_row
from tests.integration.test_daily_job import _FakeStoryEnrichProvider

TITLE = "AMD's market cap & AI demand"
URL = "https://news.example.test/a?id=7&ref=feed"
ZH_TITLE = "英伟达 & AMD 的“市值”创新高：<AI> 需求强劲"
CHAT = 424242

# --- oracle: what a Telegram user actually sees --------------------------------

_TELEGRAM_TAGS = {"b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "a", "code", "pre", "tg-spoiler",
                  "span", "blockquote"}


class _TelegramHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.visible, self.links, self._href, self._link_text, self.errors = [], [], None, [], []

    def handle_starttag(self, tag, attrs):
        if tag not in _TELEGRAM_TAGS:
            self.errors.append(f"unsupported tag <{tag}>")
        if tag == "a":
            self._href, self._link_text = dict(attrs).get("href"), []

    def handle_endtag(self, tag):
        if tag == "a":
            self.links.append(("".join(self._link_text), self._href))
            self._href = None

    def handle_data(self, data):
        self.visible.append(data)
        if self._href is not None:
            self._link_text.append(data)


def telegram_display(text: str, parse_mode: str | None) -> tuple[str, list[tuple[str, str]]]:
    """(visible text, [(link text, href)]). Without a parse mode Telegram
    shows the raw string. With HTML, markup must be well formed: a bare '<'
    or an '&' that does not start an entity is rejected by Telegram."""
    if parse_mode is None:
        return text, []
    assert parse_mode == "HTML"
    import re

    assert not re.search(r"&(?!(?:#\d+|#x[0-9a-fA-F]+|amp|lt|gt|quot);)", text), "bare & in HTML message"
    p = _TelegramHTML()
    p.feed(text)
    p.close()
    assert not p.errors, p.errors
    return "".join(p.visible), p.links


def _assert_clean(visible: str) -> None:
    for bad in ("<a", "</a>", "&#x27;", "&amp;", "&quot;", "&lt;", "&gt;"):
        assert bad not in visible, f"reader would see literal {bad!r}"


# --- fixtures -----------------------------------------------------------------------

class _FakeBot:
    sent: list[tuple[str, str | None]] = []

    def __init__(self, token):
        pass

    async def send_message(self, chat_id, text, parse_mode=None):
        _FakeBot.sent.append((text, parse_mode))
        return types.SimpleNamespace(message_id=len(_FakeBot.sent))


@pytest.fixture
def telegram(monkeypatch, config):
    _FakeBot.sent = []
    fake_module = types.ModuleType("telegram")
    fake_module.Bot = _FakeBot
    monkeypatch.setitem(sys.modules, "telegram", fake_module)
    monkeypatch.setattr(config.secrets, "telegram_bot_token", "test-token-not-real")
    monkeypatch.setattr(config.secrets, "telegram_allowed_chat_ids", str(CHAT))
    return _FakeBot


def _seed(config, conn, now, rows, edition_id="en_global"):
    for i, (title, excerpt, url) in enumerate(rows):
        sid = f"seed_{edition_id}_{i}"
        insert_source_row(conn, sid, 0)
        eid = f"E-20260921-{edition_id[:2]}{i:02d}"
        conn.execute(
            "INSERT INTO events (id, primary_title, first_seen_at, last_updated_at, published_anchor_at, "
            "confirmation_state, edition_id) VALUES (?, ?, ?, ?, ?, 'unverified', ?)",
            (eid, title, now.isoformat(), now.isoformat(), now.isoformat(), edition_id),
        )
        raw = conn.execute(
            "INSERT INTO raw_items (source_id, external_id, canonical_url, url_hash, title, normalized_title, "
            "summary_excerpt, language, fetched_at, content_hash, origin_family, edition_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (sid, f"x{i}", url, f"h{edition_id}{i}", title, title.lower(), excerpt,
             "zh" if edition_id == "zh_cn" else "en", now.isoformat(), f"c{edition_id}{i}", f"pub{i}.test",
             edition_id),
        ).lastrowid
        conn.execute("INSERT INTO event_sources (event_id, raw_item_id, is_primary, supports_claims_json) "
                     "VALUES (?, ?, 1, '[]')", (eid, raw))
        update_confirmation_state(conn, eid)
        persist_score(conn, eid, score_event(conn, eid, config, is_new_event=True, now=now),
                      config.policy.scoring.weights.model_dump(), config.policy.scoring.thresholds)


_EN = [
    (TITLE, "AMD's market cap topped $1 trillion as AI demand lifted chip stocks & export orders.", URL),
    ("NVIDIA export control chip rule", "New export control rule affects AI chip shipments.",
     "https://bis.test/rule?x=1&y=2"),
    ("Federal Reserve holds rates steady", "The Fed held rates steady, surprising markets.",
     "https://fed.test/p?a=1&b=2"),
]


def _validated(primary, secondary, category, entities, **kw):
    """A validated LLM result for every story -- the Sep 21 condition for
    BOTH editions (the zh brief had its first validated story that day)."""
    t = primary.get("title", "")
    return EditorialResult(what_happened=f"{t} -- per the source.", why_it_matters="It matters for chips & AI.",
                           chinese_what_happened=f"{t}，据来源报道。", chinese_takeaway="对芯片与 AI 需求有影响。",
                           category=None, source="llm_validated", model_used="fake-llm", validated=True)


def _run(config, conn, now, monkeypatch, *, llm: bool, edition_id="en_global"):
    if llm:
        monkeypatch.setattr(daily_module, "build_bounded_provider", lambda cfg: _FakeStoryEnrichProvider())
        monkeypatch.setattr(daily_module, "enrich_story", _validated)
    summary = run_daily(config, conn, chat_id=CHAT, dry_run=False, now=now, edition_id=edition_id)
    assert summary.fallback_used is (not llm)   # the path under test really was taken
    return summary


# --- the Sep 21 failure -------------------------------------------------------------

@pytest.mark.parametrize("llm", [True, False], ids=["llm_validated_path", "deterministic_fallback_path"])
def test_global_daily_is_delivered_as_telegram_html_on_every_path(config, conn, telegram, monkeypatch, llm):
    now = datetime(2026, 9, 21, 5, 0, tzinfo=UTC)
    _seed(config, conn, now, _EN)
    summary = _run(config, conn, now, monkeypatch, llm=llm)
    assert summary.sent and telegram.sent
    for text, parse_mode in telegram.sent:
        assert parse_mode == "HTML"                          # Sep 21: was None on the LLM path
        visible, links = telegram_display(text, parse_mode)
        _assert_clean(visible)
    visible = "".join(telegram_display(t, p)[0] for t, p in telegram.sent)
    links = [lk for t, p in telegram.sent for lk in telegram_display(t, p)[1]]
    assert (TITLE, URL) in links                            # clickable, exact text, exact URL
    assert "[GLOBAL DAILY]" in visible and "AI & Semiconductors" in visible


def test_chinese_daily_is_delivered_as_telegram_html(config, conn, telegram, monkeypatch):
    now = datetime(2026, 9, 21, 14, 0, tzinfo=UTC)
    rows = [(ZH_TITLE, "英伟达和 AMD 的市值创新高，AI 需求强劲带动芯片股上涨。", URL),
            ("工信部发布新规：车企须规范供应商账款支付", "工业和信息化部发布通知，要求车企规范供应商账款支付。",
             "https://gov.test/a?p=1&q=2")]
    _seed(config, conn, now, rows, edition_id="zh_cn")
    summary = _run(config, conn, now, monkeypatch, llm=True, edition_id="zh_cn")
    assert summary.sent and telegram.sent
    for text, parse_mode in telegram.sent:
        assert parse_mode == "HTML"
        _assert_clean(telegram_display(text, parse_mode)[0])
    visible = "".join(telegram_display(t, p)[0] for t, p in telegram.sent)
    assert "[中文日报]" in visible and ZH_TITLE in visible


# --- renderer: escaped exactly once --------------------------------------------------

def _story(title=TITLE, url=URL, category="AI & Semiconductors", excerpt="AMD's rally & more."):
    return Story(primary={"event_id": "E1", "title": title, "category": category, "final_score": 80,
                          "excerpt": excerpt, "confirmation_state": "official", "entities": [],
                          "sources": [{"publisher": "Barron's & Co", "url": url, "excerpt": excerpt}]})


def test_renderer_escapes_each_field_exactly_once():
    out = render_production_daily(SelectionResult(top_stories=[_story()]), "Sep 21", [])
    assert '<a href="https://news.example.test/a?id=7&amp;ref=feed">AMD&#x27;s market cap &amp; AI demand</a>' in out
    assert "AI &amp; Semiconductors" in out and "&amp;amp;" not in out and "&amp;#x27;" not in out
    visible, links = telegram_display(out, "HTML")
    assert (TITLE, URL) in links and "Barron's & Co" in visible
    _assert_clean(visible)


def test_chinese_renderer_escapes_each_field_exactly_once():
    out = render_production_daily_zh(SelectionResult(top_stories=[_story(title=ZH_TITLE, excerpt="市值创新高。")]),
                                     "Sep 21", [])
    visible, links = telegram_display(out, "HTML")
    assert (ZH_TITLE, URL) in links
    _assert_clean(visible)


# --- split path: every part is valid HTML, nothing re-escaped ----------------------

def test_every_split_part_is_valid_telegram_html(config, conn, telegram, monkeypatch):
    now = datetime(2026, 9, 21, 5, 0, tzinfo=UTC)
    _seed(config, conn, now, _EN)
    # size the limit from the same selection, rendered without side effects
    _, selection = daily_module._select_events(conn, config, now)
    for st in selection.top_stories:
        st.editorial = _validated(st.primary, st.secondary, st.category, [])
    full = "[GLOBAL DAILY]\n" + render_production_daily(selection, "Sep 20", [])
    limit = len(full) * 2 // 3 + 40            # forces exactly two parts (daily allows at most two)
    monkeypatch.setattr(config.policy.digests, "telegram_hard_limit_chars", limit)
    _run(config, conn, now, monkeypatch, llm=True)
    assert len(telegram.sent) == 2
    for text, parse_mode in telegram.sent:
        assert parse_mode == "HTML" and len(text) <= limit
        _assert_clean(telegram_display(text, parse_mode)[0])
    links = [lk for t, p in telegram.sent for lk in telegram_display(t, p)[1]]
    assert (TITLE, URL) in links


def test_oversized_line_is_never_cut_inside_a_tag_or_entity():
    from news_intel.delivery.splitter import split_and_label

    line = " ".join(f'<a href="https://x.test/{i}?a=1&amp;b=2">AMD&#x27;s item {i} &amp; more</a>'
                    for i in range(40))
    for part in split_and_label(line, 300):
        assert len(part) <= 300
        _assert_clean(telegram_display(part, "HTML")[0])


# --- canonical text: decoded once at normalization and on read ----------------------

def test_source_entities_and_markup_are_decoded_once_at_normalization():
    item = CollectedItem(source_id="s", external_id="x", canonical_url=URL,
                         title="Q&amp;A: AMD&#x27;s <b>plan</b> &quot;now&quot;",
                         summary_excerpt="Chips &amp; AI&nbsp;demand", language="en",
                         published_at=datetime(2026, 9, 21, tzinfo=UTC), origin_family="s", metadata={})
    n = normalize(item, excerpt_max_chars=500)
    assert n.title == 'Q&A: AMD\'s plan "now"' and n.summary_excerpt == "Chips & AI demand"


def test_legacy_stored_entities_are_canonical_when_read(config, conn):
    from news_intel.event_repo import event_sources

    now = datetime(2026, 9, 21, tzinfo=UTC)
    _seed(config, conn, now, [("Q&amp;A: How Capcom Brought Path Tracing", "Q&amp;A excerpt &#x27;x&#x27;", URL)])
    src = event_sources(conn, "E-20260921-en00")[0]
    assert src["raw_title"] == "Q&A: How Capcom Brought Path Tracing" and src["excerpt"] == "Q&A excerpt 'x'"


def test_plain_cli_rendering_has_no_markup():
    from news_intel.delivery.telegram_html import html_to_plain

    plain = html_to_plain(render_production_daily(SelectionResult(top_stories=[_story()]), "Sep 21", []))
    assert TITLE in plain and "AI & Semiconductors" in plain
    for bad in ("<", ">", "&#x27;", "&amp;"):
        assert bad not in plain
