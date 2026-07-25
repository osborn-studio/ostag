"""Outbound allowlisting and best-effort secret redaction."""

from __future__ import annotations

import re
from typing import Any, Mapping

from ostag.core.safety import redact_secrets

from .models import TaskSnapshot


PUBLIC_TASK_FIELDS = (
    "task_id",
    "title",
    "project",
    "status",
    "agent",
    "progress",
    "duration_seconds",
    "summary",
    "error_class",
    "evidence_ref",
    "version",
)
MAX_TEXT_LENGTH = 500
_SECRET_PATTERNS = (
    re.compile(
        r'''(?ix)\bAuthorization\s*:\s*(?:Basic|Bearer)\s+
        (?:"[^"]*"|'[^']*'|[^\s,;"']+)'''
    ),
    re.compile(r'''(?ix)\bBearer\s+(?:"[^"]*"|'[^']*'|[^\s,;"']+)'''),
    re.compile(
        r'''(?ix)\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|token|password|secret)
        \s*[:=]\s*(?:"[^"]*"|'[^']*'|[^\s,;"']+)'''
    ),
)


def _sanitize_text(value: str) -> str:
    for pattern in _SECRET_PATTERNS:
        value = pattern.sub("[REDACTED]", value)
    return redact_secrets(value)[:MAX_TEXT_LENGTH]


def sanitize_task_input(payload: Mapping[str, Any]) -> TaskSnapshot:
    """Return only approved task metadata, with bounded and redacted strings."""

    sanitized: dict[str, Any] = {}
    for field in PUBLIC_TASK_FIELDS:
        if field not in payload:
            continue
        value = payload[field]
        sanitized[field] = _sanitize_text(value) if isinstance(value, str) else value
    return TaskSnapshot.from_dict(sanitized)
