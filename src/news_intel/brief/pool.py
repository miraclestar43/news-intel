"""Candidate pool for the brief: both editions, as known at the cutoff, with a deterministic pre-ranking.

Point in time: only raw items fetched at or before the cutoff are used, and an event qualifies only if one of those
items was published (or, if undated, first fetched) inside the window. Scores stored on events are NOT used -- they
were computed at the last ingestion, possibly after the cutoff, by the dailies' tech/markets ranking. A replay of a
past window therefore sees what a run at that cutoff would have seen (one known leak: a CONTENT_CHANGED item is
updated in place, so its stored text may be newer than the cutoff).

The pre-ranking only decides which ~40 candidates the editor (select.py) sees, and the order used when the editor
call is unavailable. It fixes what kept general news out of the dailies (2026-11-01 funnel, three windows):
  - "Unclassified" is not worthless here: the dailies multiply it by 0.0, so a 5,000-death epidemic or a deadly
    train crash could never qualify. Here it is a low but non-zero base, and the public-safety bucket is on;
  - consumer product launches, prices and reviews, and sport/entertainment, are demoted unless several independent
    families carry them -- the zh pool used to be dozens of single-source IT之家 items tied at 69-71 points;
  - government/regulator feeds and established newsrooms add weight (a company's own blog does not count as
    official here); independent families add weight; discovery feeds lose some.
No title-specific rule: every term list below is generic and reviewable.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from news_intel.event_repo import event_as_digest_dict
from news_intel.pipeline.materiality import compute_materiality
from news_intel.pipeline.relevance import classify_relevance

BUCKET_BASE = {
    "public_safety": 30.0,
    "world_politics_geopolitics": 25.0,
    "us_china_policy_geopolitics": 25.0,
    "markets_macro_corporate": 18.0,
    "ai_semiconductor_export_controls": 15.0,
    "tech_platforms_datacenter": 12.0,
    "routine_politics_general": 10.0,
    "unclassified": 8.0,
}
BUCKET_LABEL_ZH = {
    "public_safety": "公共安全", "world_politics_geopolitics": "国际", "us_china_policy_geopolitics": "中国与世界",
    "markets_macro_corporate": "经济", "ai_semiconductor_export_controls": "科技", "tech_platforms_datacenter": "科技",
    "routine_politics_general": "政治", "unclassified": "综合",
}
FAMILY_BONUS = {1: 0.0, 2: 8.0, 3: 14.0}
FAMILY_BONUS_MAX = 20.0
TIER_BONUS = {0: 8.0, 1: 8.0, 2: 0.0, 3: -6.0}
_OFFICIAL_HINTS = {"policy", "macro"}  # tier 0 is "official" only for government/regulator feeds
DEMOTION = 20.0

_ZH_PRODUCT_ACTION = re.compile(
    r"(发布|上架|开售|首销|预售|开启预约|预约|曝光|评测|上手|官宣|亮相|新品|促销|降价|优惠|到手价|售价|起售|入网|跑分|"
    r"开箱|配置|续航)"
)
_ZH_PRODUCT_NOUN = re.compile(
    r"(手机|耳机|充电器|充电宝|移动电源|笔记本|平板|手表|手环|显卡|主板|显示器|电视|路由器|键盘|鼠标|音箱|相机|游戏本|机箱|电源|"
    r"固态硬盘|SSD|内存条|U\s?盘|扫地机|洗衣机|冰箱|空调|投影|眼镜|iPhone|iPad|Galaxy|Pixel|Redmi|OPPO|vivo|一加|荣耀|真我|"
    r"小米\s?\d|Mate\s?\d|Watch|Buds|Pro\b|Ultra\b|Max\b)"
)
_EN_PRODUCT = re.compile(
    r"\b(launch(es|ed)?|unveil(s|ed)?|review|hands-on|deal|discount|price cut|on sale|pre-?order)\b.*"
    r"\b(phone|iphone|ipad|laptop|earbuds|headphones|charger|tablet|smartwatch|tv|monitor|keyboard|mouse|console)\b",
    re.IGNORECASE,
)
_ZH_SOFT = re.compile(
    r"(比赛|夺冠|决赛|联赛|球队|球星|电影|票房|综艺|明星|演唱会|主演|定档|预告片|手游|网游|首曝|周年)"
)
_EN_SOFT = re.compile(
    r"\b(match|tournament|championship|league|title fight|boxing|wnba|nba|nfl|film|box office|celebrity|actor|actress|"
    r"singer|concert|album|trailer|game review)\b",
    re.IGNORECASE,
)


@dataclass
class Candidate:
    key: str  # c1, c2, ... in pre-rank order
    event: dict  # event_as_digest_dict, sources restricted to the point in time
    edition_id: str
    bucket: str
    score: float
    families: int
    best_tier: int
    first_published: datetime
    demoted: list[str] = field(default_factory=list)
    previously: dict | None = None  # {"edition_date", "headline"} when an earlier brief carried this event

    @property
    def event_id(self) -> str:
        return self.event["event_id"]

    @property
    def title(self) -> str:
        return self.event["title"]


def _parse(ts) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def point_in_time_event(conn: sqlite3.Connection, row: sqlite3.Row, cutoff: datetime) -> dict | None:
    """The event as known at `cutoff`: only sources fetched by then; the title from its first such source."""
    ev = event_as_digest_dict(conn, row)
    srcs = [s for s in ev["sources"] if (_parse(s.get("fetched_at")) or cutoff) <= cutoff]
    if not srcs:
        return None
    ev["sources"] = srcs
    ev["title"] = srcs[0].get("raw_title") or srcs[0].get("origin_title") or ev["title"]
    ev["excerpt"] = srcs[0].get("excerpt") or ""
    return ev


def _published(src: dict) -> datetime | None:
    return _parse(src.get("published_at")) or _parse(src.get("fetched_at"))


def demotions(title: str, text: str) -> list[str]:
    out = []
    if (_ZH_PRODUCT_ACTION.search(title) and _ZH_PRODUCT_NOUN.search(title)) or _EN_PRODUCT.search(title):
        out.append("consumer product")
    if _ZH_SOFT.search(title) or _EN_SOFT.search(title):
        out.append("sport/entertainment")
    return out


def score_candidate(config, ev: dict, edition_id: str) -> tuple[str, float, int, int, list[str]]:
    text = " ".join(f"{s.get('raw_title') or ''} {s.get('excerpt') or ''}" for s in ev["sources"])
    rel = classify_relevance(
        text, config.topics_for(edition_id), config.policy.relevance_multipliers, title=ev["title"], public_safety=True
    )
    if rel.bucket == "explicit_noise":
        return rel.bucket, -100.0, 0, 3, ["noise"]
    families = len({s.get("origin_family") or s.get("publisher") for s in ev["sources"]})
    official = {s.id for s in config.sources.sources if s.tier == 0 and _OFFICIAL_HINTS & set(s.category_hints)}

    def tier(s: dict) -> int:
        t = s.get("tier") if s.get("tier") is not None else 3
        return 2 if t == 0 and s.get("source_id") not in official else t  # a company's own blog: specialist

    best_tier = min(tier(s) for s in ev["sources"])
    score = BUCKET_BASE.get(rel.bucket, 8.0) + 0.4 * compute_materiality(text, rel.bucket)
    score += FAMILY_BONUS.get(families, FAMILY_BONUS_MAX) + TIER_BONUS.get(best_tier, 0.0)
    dem = demotions(ev["title"], text)
    if dem and families < 3:
        score -= DEMOTION * len(dem)
    return rel.bucket, round(score, 1), families, best_tier, dem


def prior_briefs(export_dir: Path | None, edition_date, days: int = 3) -> dict[str, dict]:
    """event_id -> {"edition_date", "headline"} for events carried by the frozen briefs of the previous `days`
    editions (cross-day repeat check)."""
    out: dict[str, dict] = {}
    if export_dir is None or not export_dir.exists():
        return out
    for d in sorted(export_dir.iterdir(), reverse=True):
        if not d.is_dir() or d.name >= edition_date.isoformat():
            continue
        current = d / "current.json"
        if not current.exists():
            continue
        try:
            snap = json.loads((d / json.loads(current.read_text())["file"]).read_text(encoding="utf-8"))
        except (OSError, ValueError, KeyError):
            continue
        for item in snap.get("items") or []:
            for eid in item.get("event_ids") or []:
                out.setdefault(eid, {"edition_date": snap.get("edition_date"), "headline": item.get("headline")})
        days -= 1
        if days == 0:
            break
    return out


def build_pool(conn: sqlite3.Connection, config, edition, *, export_dir: Path | None = None,
               limit: int = 40) -> tuple[list[Candidate], dict]:
    """All qualifying candidates in pre-rank order (keys c1..), the first `limit` to be shown to the editor, plus a
    funnel record of how many events each stage kept and why the rest were dropped."""
    cutoff, start = edition.cutoff_utc, edition.window_start.astimezone(UTC)
    rows = conn.execute(
        """
        SELECT * FROM events
        WHERE first_seen_at <= ? AND last_updated_at >= ? AND orphaned_at IS NULL AND COALESCE(archive_only, 0) = 0
        """,
        (cutoff.isoformat(), start.isoformat()),
    ).fetchall()
    funnel = {"events_touched_in_window": len(rows), "by_edition": {}, "dropped": {}}

    def drop(reason: str) -> None:
        funnel["dropped"][reason] = funnel["dropped"].get(reason, 0) + 1

    prior = prior_briefs(export_dir, edition.edition_date)
    cands: list[Candidate] = []
    for row in rows:
        ev = point_in_time_event(conn, row, cutoff)
        if ev is None:
            drop("no source fetched by the cutoff")
            continue
        pubs = [p for p in (_published(s) for s in ev["sources"]) if p]
        in_window = [p for p in pubs if start <= p < cutoff]
        if not in_window:
            drop("no source published in the window")
            continue
        if not any((s.get("display_url") or s.get("url") or "").startswith("http") for s in ev["sources"]):
            drop("no article link")
            continue
        edition_id = row["edition_id"]
        bucket, score, families, tier, dem = score_candidate(config, ev, edition_id)
        if bucket == "explicit_noise":
            drop("noise")
            continue
        funnel["by_edition"][edition_id] = funnel["by_edition"].get(edition_id, 0) + 1
        cands.append(Candidate("", ev, edition_id, bucket, score, families, tier, min(in_window), dem,
                               prior.get(ev["event_id"])))
    cands.sort(key=lambda c: (-c.score, c.first_published))
    for i, c in enumerate(cands, start=1):
        c.key = f"c{i}"
    funnel["candidates"] = len(cands)
    funnel["shown_to_editor"] = min(limit, len(cands))
    funnel["by_bucket"] = {}
    for c in cands:
        funnel["by_bucket"][c.bucket] = funnel["by_bucket"].get(c.bucket, 0) + 1
    funnel["demoted"] = sum(bool(c.demoted) for c in cands)
    funnel["by_source"] = {}
    for c in cands:
        for s in {s.get("source_id") for s in c.event["sources"]}:
            funnel["by_source"][s] = funnel["by_source"].get(s, 0) + 1
    return cands, funnel
