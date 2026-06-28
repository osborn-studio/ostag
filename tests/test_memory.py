from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from ostag.mfs.memory import ensure_memory_file, memory_file, read_memory, remember
from ostag.main import main_async


def test_memory_file_is_platform_and_chat_scoped(tmp_path: Path) -> None:
    path = memory_file("feishu", "oc_demo/chat", tmp_path)

    assert path == tmp_path / "feishu" / "oc_demo_chat" / "memory.md"


def test_remember_creates_markdown_memory(tmp_path: Path) -> None:
    timestamp = datetime(2026, 6, 28, 8, 0, tzinfo=timezone.utc)

    path = remember("lark", "oc_demo", "Prefer short launch updates.", tmp_path, timestamp)

    text = path.read_text(encoding="utf-8")
    assert "# OS Tag Memory: lark/oc_demo" in text
    assert "## Preferences" in text
    assert "2026-06-28T08:00:00+00:00: Prefer short launch updates." in text


def test_read_memory_initializes_file(tmp_path: Path) -> None:
    text = read_memory("feishu", "chat", tmp_path)

    assert "OS Tag Memory: feishu/chat" in text
    assert ensure_memory_file("feishu", "chat", tmp_path).exists()


def test_memory_rejects_relative_segments(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        memory_file("feishu", "..", tmp_path)


def test_memory_cli_remember_and_show(monkeypatch, tmp_path: Path, capsys) -> None:
    monkeypatch.setenv("OSTAG_MEMORY_ROOT", str(tmp_path))

    assert __import__("asyncio").run(
        main_async(
            [
                "memory",
                "remember",
                "--platform",
                "feishu",
                "--chat-id",
                "oc_demo",
                "--text",
                "Use mock backend for pilot.",
            ]
        )
    ) == 0
    assert "remembered:" in capsys.readouterr().out

    assert __import__("asyncio").run(
        main_async(["memory", "show", "--platform", "feishu", "--chat-id", "oc_demo"])
    ) == 0
    assert "Use mock backend for pilot." in capsys.readouterr().out
