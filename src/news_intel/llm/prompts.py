from __future__ import annotations

from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parent.parent.parent.parent / "config" / "prompts"


def load_template(name: str) -> str:
    path = PROMPTS_DIR / name
    return path.read_text(encoding="utf-8")


def render(template: str, **placeholders: str) -> str:
    """Plain substring replacement, not str.format — untrusted article text
    inserted into {sources_block} etc. may itself contain literal braces,
    which would otherwise raise or silently corrupt a .format() call."""
    out = template
    for key, value in placeholders.items():
        out = out.replace("{" + key + "}", value)
    return out


def _attr(value) -> str:
    return str(value or "").replace('"', "'").replace("\n", " ")


def _data(value) -> str:
    """Source text cannot close its own delimiter."""
    return str(value or "").replace("</source", "<\\/source").replace("<source", "<\\source")


def render_sources_block(sources: list[dict]) -> str:
    """Wrap each untrusted source in an explicit delimiter so the model
    treats it as data, never as instructions (design doc section 8.3).

    2026-11-01: the source's own TITLE is rendered (it was never sent: a death toll that only the BBC headline gave was
    invisible to the model, which then wrote that the source gave no toll), with its publisher, publication time, URL
    and language as attributes. Quotes the model cites must come verbatim from the TITLE or TEXT lines."""
    parts = []
    for s in sources:
        lines = [
            f'<source id="{_attr(s["id"])}" publisher="{_attr(s.get("publisher"))}" '
            f'published_at="{_attr(s.get("published_at"))}" language="{_attr(s.get("language"))}" '
            f'url="{_attr(s.get("url"))}">'
        ]
        if s.get("title"):
            lines.append(f"TITLE: {_data(s['title'])}")
        lines.append(f"TEXT: {_data(s.get('excerpt'))}" if s.get("excerpt") else "TEXT: (none beyond the title)")
        lines.append("</source>")
        parts.append("\n".join(lines))
    return "\n\n".join(parts)
