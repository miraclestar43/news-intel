"""zh_cn production daily digest renderer (2026-08-17).

Takes the SAME pipeline.daily_selection.SelectionResult the en_global
formatter takes (selection/ranking/render-readiness are fully shared core
-- only rendering differs) and produces Chinese-language Telegram HTML-mode
text with the structure requested for the Chinese edition: 今日最重要 /
科技与产业 / 中国经济与政策 / 国际与中美 / 明日关注, sections appearing only
when qualified content exists. Never a generic "看点" template alone when a
story-specific entity/action was extractable (pipeline/zh_render_readiness.py).

All dynamic text is HTML-escaped before insertion; every link points at the
specific mapped article/filing/release URL already stored in the database,
never a URL this module invents."""

from __future__ import annotations

from news_intel.delivery.telegram_html import escape_text, link
from news_intel.pipeline.daily_selection import UNDERFILLED_POOL_THRESHOLD, SelectionResult, Story
from news_intel.pipeline.zh_render_readiness import what_happened_zh, why_it_matters_zh


def _escape(text: str) -> str:
    return escape_text(text)  # the one escaping step: delivery/telegram_html.py


def _link(text: str, url: str | None) -> str:
    return link(text, url)


_CATEGORY_LABELS_ZH = {
    "AI & Semiconductors": "AI与半导体",
    "Markets & Economy": "市场与经济",
    "Politics": "政策",
    "World": "国际时政",
    "US / China / World": "中美与国际",
    "Technology": "科技",
}


def _category_zh(category: str) -> str:
    return _CATEGORY_LABELS_ZH.get(category, category)


def _primary_source(item: dict) -> dict | None:
    sources = item.get("sources") or []
    return sources[0] if sources and sources[0].get("url") else None


def _display_url(source: dict) -> str:
    return source.get("display_url") or source["url"]


def _source_labels(story: Story) -> list[tuple[str, str]]:
    """2026-08-23 repair: real production regression (Aug 22 zh_cn digest,
    not yet delivered) -- "来源：IT之家，IT之家" for two genuinely
    different, legitimately corroborating articles (different URLs, same
    publisher covering an evolving story). Both links are real and worth
    keeping; disambiguates the second label instead of dropping it, since
    identical display text gives a reader no way to tell them apart."""
    labels: list[tuple[str, str]] = []
    primary_src = _primary_source(story.primary)
    if primary_src:
        labels.append((primary_src.get("publisher") or "来源", _display_url(primary_src)))
    if story.secondary:
        secondary_src = _primary_source(story.secondary)
        if secondary_src and secondary_src["url"] != (primary_src or {}).get("url"):
            name = secondary_src.get("publisher") or "来源"
            if name == (primary_src or {}).get("publisher"):
                name = f"{name}（相关报道）"
            labels.append((name, _display_url(secondary_src)))
    return labels[:2]


def _source_line(story: Story) -> str | None:
    labels = _source_labels(story)
    if not labels:
        return None
    links = [_link(name, url) for name, url in labels]
    return "来源：" + "，".join(links)


def _render_top_story(index: int, story: Story) -> list[str]:
    """pipeline/daily_selection.py's _apply_render_readiness_gate already
    guarantees (via Story.render_ready's zh_cn dispatch) every story here
    has a complete "what happened" sentence from at least one source --
    never a placeholder."""
    primary_src = _primary_source(story.primary)
    title_url = _display_url(primary_src) if primary_src else None
    lines = [
        "",
        f"{index}. {_link(story.primary.get('title', '（无标题）'), title_url)}",
        f"分类：{_escape(_category_zh(story.category))}",
    ]
    entities = story.primary.get("entities")
    editorial = story.editorial
    summary = editorial.chinese_what_happened if editorial else None
    if summary is None:
        summary = what_happened_zh(story.primary, story.secondary)
    if summary:
        lines.append(_escape(summary))
    why = editorial.chinese_takeaway if editorial else why_it_matters_zh(story.primary, story.secondary,
                                                                          story.category, entities)
    if why:
        lines.append(_escape(why))
    if story.update_note:
        lines.append(_escape(story.update_note))
    source_line = _source_line(story)
    if source_line:
        lines.append(source_line)
    return lines


def _render_brief_item(story: Story) -> str:
    primary_src = _primary_source(story.primary)
    title_url = _display_url(primary_src) if primary_src else None
    title_link = _link(story.primary.get("title", "（无标题）"), title_url)
    parts = [f"• {title_link}"]
    if story.update_note:
        parts.append(_escape(story.update_note))
    source_line = _source_line(story)
    if source_line:
        parts.append(source_line)
    return " — ".join(parts)


# Section bucketing for MORE-TO-KNOW-equivalent stories: the same English
# category labels every event carries (scoring.py's _CATEGORY_LABELS is
# shared across editions) route into the Chinese section headers.
_TECH_CATEGORIES = {"AI & Semiconductors", "Technology"}
_CHINA_ECON_CATEGORIES = {"Markets & Economy", "Politics"}
# "World" (general international conflict/diplomacy, none of it
# China-specific -- see scoring.py's 2026-08-21 category split) routes
# into the same "国际与中美" section as "US / China / World": both are
# international in nature, and the documented 5-section Chinese daily
# structure (今日最重要/科技与产业/中国经济与政策/国际与中美/明日关注) has
# no separate section for it.
_INTL_CATEGORIES = {"US / China / World", "World"}


def _section_for(story: Story) -> str:
    if story.category in _TECH_CATEGORIES:
        return "科技与产业"
    if story.category in _CHINA_ECON_CATEGORIES:
        return "中国经济与政策"
    if story.category in _INTL_CATEGORIES:
        return "国际与中美"
    return "其他"


def render_production_daily_zh(
    result: SelectionResult, date_label: str, calendar_items: list[dict]
) -> str:
    lines = [f"每日情报简报 — {date_label}", "", "本简报基于规则排序的信息来源摘要。"]

    if result.top_stories:
        lines.append("")
        lines.append("今日最重要")
        for i, story in enumerate(result.top_stories, start=1):
            lines.extend(_render_top_story(i, story))

    if result.more_to_know:
        by_section: dict[str, list[Story]] = {}
        for story in result.more_to_know:
            by_section.setdefault(_section_for(story), []).append(story)
        for section in ("科技与产业", "中国经济与政策", "国际与中美", "其他"):
            stories = by_section.get(section)
            if not stories:
                continue
            lines.append("")
            lines.append(section)
            for story in stories:
                lines.append(_render_brief_item(story))

    if not result.top_stories and not result.more_to_know:
        lines.append("")
        lines.append("过去24小时内未发现符合条件的重大动态。")
    elif len(result.all_stories) < UNDERFILLED_POOL_THRESHOLD:
        lines.append("")
        # 2026-08-29 production incident: this note previously always
        # blamed "质量与来源多样性标准" (the quality bar), even when the
        # real cause was that every qualified candidate came from one or
        # two publishers -- telling the reader their feed was low-quality
        # when it was actually narrow. Now reports the cause selection
        # actually observed.
        shown = len(result.all_stories)
        cause = result.underfill_cause
        if cause == "source_concentration":
            lines.append(
                f"说明：今日共有 {result.qualified_family_count} 条动态达到质量标准，"
                f"但全部来自 {result.distinct_publisher_families} 家媒体；"
                f"受单一来源上限限制，实际展示 {shown} 条——本期来源集中，并不具备来源多样性。"
            )
        elif cause == "publisher_cap":
            lines.append(
                f"说明：今日共有 {result.qualified_family_count} 条动态达到质量标准，"
                f"受单一来源数量上限限制，实际展示 {shown} 条。"
            )
        else:
            lines.append(
                f"说明：今日仅有 {shown} 条动态达到质量标准，未添加低质量或宣传性内容凑数。"
            )

    if calendar_items:
        lines.append("")
        lines.append("明日关注")
        for cal in calendar_items:
            title = cal.get("title", "")
            url = cal.get("url")
            entry = _link(title, url) if url else _escape(title)
            time_label = cal.get("pacific_time_label")
            if time_label:
                entry += f" — {_escape(time_label)}"
            lines.append(f"• {entry}")

    return "\n".join(lines)
