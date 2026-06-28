from __future__ import annotations

from ostag.config import Settings


def trim_context(items: list[str], config: Settings) -> str:
    text = "\n\n".join(item.strip() for item in items if item.strip())
    if len(text) <= config.max_context_chars:
        return text
    return text[: config.max_context_chars] + "\n[context truncated]"
