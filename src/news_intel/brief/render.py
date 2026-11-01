"""Every reader-facing form of the brief, rendered from the frozen snapshot only (2026-11-01).

  telegram_html  the full brief for Telegram (HTML parse mode, one message, validated)
  archive_html   the dated web archive page (edition date, cutoff, time zone, sources, stable link)
  digest         the newsletter's short digest: one line per item and the archive link, about 200-300 characters
Nothing here selects, writes or checks news: a renderer can only show what the snapshot holds, and each output names
the snapshot_id and text_sha256 it came from.
"""

from __future__ import annotations

import html
from datetime import datetime

from news_intel.brief.schedule import BEIJING
from news_intel.brief.snapshot import chars, zh_chars
from news_intel.delivery.telegram_html import escape_text, link, validate

WHY_LABEL = {"reported": "为什么值得知道：", "source_judgment": "来源观点："}
DEFAULT_ARCHIVE_BASE = "https://starhub.vip/history/news/"
TELEGRAM_LIMIT = 4096


def archive_url(snap: dict, base: str = DEFAULT_ARCHIVE_BASE) -> str:
    return f"{base.rstrip('/')}/{snap['edition_date']}/"


def _when(iso: str) -> str:
    dt = datetime.fromisoformat(iso).astimezone(BEIJING)
    return f"{dt.year}年{dt.month}月{dt.day}日 {dt:%H:%M}"


def _date_zh(d: str) -> str:
    y, m, day = d.split("-")
    return f"{int(y)}年{int(m)}月{int(day)}日"


def header_zh(snap: dict) -> str:
    return f"截至北京时间{_when(snap['cutoff'])} · 过去24小时要闻"


def provenance_zh(snap: dict) -> str:
    n = len(snap["items"])
    return f"自动选题，由 {snap['models']['writer_short']} 撰写，逐条对照来源核验（{n} 条）"


def telegram_html(snap: dict, base: str = DEFAULT_ARCHIVE_BASE) -> str:
    lines = [f"<b>{escape_text('每日要闻｜' + _date_zh(snap['edition_date']))}</b>",
             escape_text(header_zh(snap)), ""]
    if not snap["items"]:
        lines.append(escape_text("今天没有达到发布标准的新闻（可核对的来源不足）。"))
    for i, it in enumerate(snap["items"], start=1):
        lines.append(f"<b>{i}. {escape_text(it['headline'])}</b>")
        lines.append(escape_text(it["what"]))
        if it.get("why"):
            lines.append(escape_text(WHY_LABEL.get(it.get("why_kind"), "为什么值得知道：") + it["why"]))
        lines.append(escape_text("来源：") + " · ".join(link(s["name"], s["url"]) for s in it["sources"]))
        lines.append("")
    lines.append(escape_text(provenance_zh(snap)) if snap["items"] else "")
    lines.append(f"{escape_text('完整归档：')}{link(archive_url(snap, base), archive_url(snap, base))}")
    text = "\n".join(lines).strip()
    validate(text)
    if len(text) > TELEGRAM_LIMIT:
        raise ValueError(f"brief is {len(text)} characters, over one Telegram message")
    return text


def digest(snap: dict, base: str = DEFAULT_ARCHIVE_BASE) -> dict:
    """The newsletter's closing news digest: the brief lines, nothing new. `chars` (字数, punctuation and digits
    included) and `zh_chars` (汉字) count the digest without its link line; the 200-300 target is reported, not
    forced."""
    url = archive_url(snap, base)
    head = f"今日新闻速览（{header_zh(snap).split(' · ')[0]}）"
    lines = [f"· {it['brief_line']}" for it in snap["items"]]
    text = "\n".join([head, *lines, f"完整新闻与来源：{url}"])
    html_lines = "".join(f'<li style="margin:0 0 6px;">{html.escape(it["brief_line"])}</li>' for it in snap["items"])
    frag = (f'<p style="font-size:14px;color:#8a857d;margin:0 0 6px;">{html.escape(head)}</p>'
            f'<ul style="font-size:15px;line-height:1.75;margin:0 0 8px;padding-left:18px;">{html_lines}</ul>'
            f'<p style="font-size:14px;margin:0;"><a href="{html.escape(url)}">完整新闻与来源</a></p>')
    body = "\n".join([head, *lines])
    return {"text": text, "html": frag, "zh_chars": zh_chars(body), "chars": chars(body), "url": url,
            "snapshot_id": snap["snapshot_id"], "text_sha256": snap["text_sha256"]}


def archive_html(snap: dict, base: str = DEFAULT_ARCHIVE_BASE) -> str:
    e = html.escape
    items = []
    for i, it in enumerate(snap["items"], start=1):
        srcs = " · ".join(
            f'<a href="{e(s["url"])}">{e(s["name"])}</a> <span class="t">{e(_when(s["published_at"]))}</span>'
            if s.get("published_at") else f'<a href="{e(s["url"])}">{e(s["name"])}</a>'
            for s in it["sources"]
        )
        why = (f'<p class="why">{e(WHY_LABEL.get(it.get("why_kind"), "为什么值得知道：") + it["why"])}</p>'
               if it.get("why") else "")
        items.append(f'<article id="n{i}"><h2>{i}. {e(it["headline"])}</h2><p>{e(it["what"])}</p>{why}'
                     f'<p class="src">来源：{srcs}</p></article>')
    body = "\n".join(items) or "<p>今天没有达到发布标准的新闻（可核对的来源不足）。</p>"
    title = f"每日要闻｜{_date_zh(snap['edition_date'])}"
    prov = provenance_zh(snap) if snap["items"] else ""
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{e(title)}</title><link rel="canonical" href="{e(archive_url(snap, base))}">
<style>body{{max-width:680px;margin:0 auto;padding:20px 18px 40px;font:16px/1.85 -apple-system,"PingFang SC",
"Noto Sans CJK SC",sans-serif;color:#1d1d1f;background:#fbfaf7}}h1{{font-size:24px;margin:8px 0 4px}}
h2{{font-size:18px;margin:26px 0 6px}}.meta,.src,.t,footer{{color:#8a857d;font-size:13px}}.why{{color:#3a3732}}
a{{color:#8b4513}}footer{{margin-top:32px;border-top:1px solid #d8d2c8;padding-top:12px}}</style></head>
<body><h1>{e(title)}</h1>
<p class="meta">{e(header_zh(snap))}（时区：Asia/Shanghai，UTC+8）<br>{e(prov)}</p>
{body}
<footer>版期 {e(snap["edition_date"])} · 截稿 {e(snap["cutoff"])} · 生成 {e(snap["generated_at"])}<br>
快照 {e(snap["snapshot_id"])} · 文本 sha256 {e(snap["text_sha256"][:16])}…<br>
新闻由 news-intel 自动采集、选题和撰写，每条都对照所列来源核验；如有出入，以原始报道为准。</footer>
</body></html>
"""
