from __future__ import annotations

import re
from dataclasses import dataclass


SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"xoxb-[A-Za-z0-9\-]+"),
    re.compile(r"xapp-[A-Za-z0-9\-]+"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"(?i)(ANTHROPIC_API_KEY|OPENAI_API_KEY|FEISHU_APP_SECRET|LARK_APP_SECRET)\s*=\s*[^\s]+"),
]


@dataclass(frozen=True)
class SafetyResult:
    ok: bool
    text: str
    reason: str | None = None


def redact_secrets(text: str) -> str:
    redacted = text
    for pattern in SECRET_PATTERNS:
        redacted = pattern.sub("[REDACTED]", redacted)
    return redacted


def sanitize_user_text(text: str) -> str:
    cleaned = text.replace("\x00", "")
    cleaned = re.sub(r"[\u202a-\u202e\u2066-\u2069]", "", cleaned)
    return redact_secrets(cleaned).strip()


def check_message_length(text: str, max_chars: int) -> SafetyResult:
    if len(text) <= max_chars:
        return SafetyResult(ok=True, text=text)
    return SafetyResult(
        ok=False,
        text=text[:max_chars],
        reason=f"message exceeds OSTAG_MAX_INPUT_CHARS={max_chars}",
    )


def is_shell_allowed(allow_shell: bool) -> bool:
    return bool(allow_shell)


def validate_scope(scope: str, allowed_scopes: tuple[str, ...]) -> bool:
    normalized = scope.rstrip("/")
    return any(
        normalized == allowed.rstrip("/") or normalized.startswith(allowed.rstrip("/") + "/")
        for allowed in allowed_scopes
    )
