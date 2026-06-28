from __future__ import annotations

import json
import os
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


DEFAULT_MEMORY_ROOT = Path.home() / ".mfs" / "ostag-memory"


@dataclass(frozen=True)
class MemoryEntry:
    platform: str
    chat_id: str
    text: str
    timestamp: datetime


def memory_root() -> Path:
    return Path(os.getenv("OSTAG_MEMORY_ROOT", str(DEFAULT_MEMORY_ROOT))).expanduser()


def memory_file(platform: str, chat_id: str, root: Path | None = None) -> Path:
    safe_platform = _safe_segment(platform)
    safe_chat_id = _safe_segment(chat_id)
    return (root or memory_root()) / safe_platform / safe_chat_id / "memory.md"


def ensure_memory_file(platform: str, chat_id: str, root: Path | None = None) -> Path:
    path = memory_file(platform, chat_id, root)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "\n".join(
                [
                    f"# OS Tag Memory: {platform}/{chat_id}",
                    "",
                    "## Preferences",
                    "- Answers should be concise and grounded in available context.",
                    "",
                    "## Decisions",
                    "",
                    "## Facts",
                    "",
                ]
            ),
            encoding="utf-8",
        )
    return path


def remember(
    platform: str,
    chat_id: str,
    text: str,
    root: Path | None = None,
    now: datetime | None = None,
) -> Path:
    path = ensure_memory_file(platform, chat_id, root)
    timestamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    entry = MemoryEntry(platform=platform, chat_id=chat_id, text=text.strip(), timestamp=timestamp)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"\n- {entry.timestamp.isoformat(timespec='seconds')}: {entry.text}\n")
    return path


def read_memory(platform: str, chat_id: str, root: Path | None = None) -> str:
    return ensure_memory_file(platform, chat_id, root).read_text(encoding="utf-8")


def token_from_env() -> str | None:
    if os.getenv("MFS_TOKEN"):
        return os.environ["MFS_TOKEN"]
    token_file = Path.home() / ".mfs" / "server.token"
    if token_file.exists():
        return token_file.read_text(encoding="utf-8").strip()
    return None


def sync_memory_root(root: Path | None = None, server_url: str | None = None) -> str:
    base = (server_url or os.getenv("MFS_SERVER_URL", "http://127.0.0.1:8000")).rstrip("/")
    target = str(root or memory_root())
    body = json.dumps({"target": target}).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    token = token_from_env()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(f"{base}/v1/add", data=body, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=60) as response:
        data = json.loads(response.read().decode("utf-8"))
    return str(data.get("job_id", ""))


def _safe_segment(value: str) -> str:
    cleaned = value.strip().replace("/", "_").replace("\\", "_")
    if not cleaned or cleaned in {".", ".."}:
        raise ValueError("memory path segment cannot be empty or relative")
    return cleaned
