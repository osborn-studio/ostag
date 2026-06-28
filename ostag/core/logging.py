from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ostag.core.event import NormalizedEvent
from ostag.core.safety import redact_secrets


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def write_audit_log(
    path: Path,
    *,
    event: NormalizedEvent,
    backend: str,
    mfs_enabled: bool,
    input_text: str,
    output_text: str,
    status: str,
    env: str = "dev",
    error: str | None = None,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "timestamp": datetime.now(UTC).isoformat(),
        "platform": event.platform,
        "chat_id": event.chat_id,
        "message_id": event.message_id,
        "sender_id": event.sender_id,
        "backend": backend,
        "mfs_enabled": mfs_enabled,
        "input_chars": len(input_text),
        "output_chars": len(output_text),
        "status": status,
    }
    if env == "dev":
        record["input_preview"] = redact_secrets(input_text)[:500]
    else:
        record["input_sha256_16"] = _digest(input_text)
    if error:
        record["error"] = redact_secrets(error)[:1000]

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record
