from __future__ import annotations

import os
import re
from dataclasses import dataclass


SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"xoxb-[A-Za-z0-9\-]+"),
    re.compile(r"xapp-[A-Za-z0-9\-]+"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(
        r"(?i)(ANTHROPIC_API_KEY|OPENAI_API_KEY|FEISHU_APP_SECRET|LARK_APP_SECRET|"
        r"OSTAG_VERCEL_ACCESS_TOKEN)\s*=\s*[^\s]+"
    ),
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


# Chat-platform credentials OS Tag reads from the environment. Agent backends
# (Codex/Claude CLI) and the MFS client never need these — they get a scrubbed
# environment so a misbehaving or compromised agent process cannot read them.
# Connector CLIs (lark-cli / dws-cli) are deliberately NOT scrubbed: they are
# the system-of-record transport and may authenticate via these variables.
PLATFORM_SECRET_ENV_VARS = (
    "FEISHU_APP_SECRET",
    "FEISHU_VERIFICATION_TOKEN",
    "FEISHU_ENCRYPT_KEY",
    "LARK_APP_SECRET",
    "LARK_VERIFICATION_TOKEN",
    "LARK_ENCRYPT_KEY",
    "SLACK_BOT_TOKEN",
    "SLACK_APP_TOKEN",
    "DINGTALK_CLIENT_SECRET",
    "DINGTALK_ROBOT_SECRET",
    "OSTAG_VERCEL_ACCESS_TOKEN",
)


def scrub_environment(
    env: dict[str, str] | None = None, extra: tuple[str, ...] = ()
) -> dict[str, str]:
    """Copy of the environment with platform secrets (and ``extra`` vars) removed."""
    scrubbed = dict(os.environ if env is None else env)
    for name in PLATFORM_SECRET_ENV_VARS + tuple(extra):
        scrubbed.pop(name, None)
    return scrubbed


def is_shell_allowed(allow_shell: bool) -> bool:
    return bool(allow_shell)


def validate_scope(scope: str, allowed_scopes: tuple[str, ...]) -> bool:
    normalized = scope.rstrip("/")
    return any(
        normalized == allowed.rstrip("/") or normalized.startswith(allowed.rstrip("/") + "/")
        for allowed in allowed_scopes
    )
