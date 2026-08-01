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


def render_sources_block(sources: list[dict]) -> str:
    """Wrap each untrusted excerpt in an explicit delimiter so the model
    treats it as data, never as instructions (design doc section 8.3)."""
    parts = []
    for s in sources:
        parts.append(
            f'<source id="{s["id"]}" publisher="{s.get("publisher", "")}" '
            f'published_at="{s.get("published_at", "")}">\n'
            f'{s.get("excerpt", "")}\n</source>'
        )
    return "\n\n".join(parts)
