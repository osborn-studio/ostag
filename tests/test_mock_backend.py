from __future__ import annotations

from pathlib import Path

import pytest

from ostag.config import load_settings
from ostag.backends.claude_cli import build_claude_command
from ostag.backends.codex_cli import build_codex_command
from ostag.core.event import NormalizedEvent
from ostag.core.runner import OSTagRunner
from ostag.mfs.client import MFSClient
from ostag.mfs.helpers import build_mfs_cat_command, build_mfs_search_command


@pytest.mark.asyncio
async def test_mock_backend_returns_stable_reply(tmp_path: Path) -> None:
    config = load_settings(None, audit_log=tmp_path / "audit.log", backend="mock")
    event = NormalizedEvent(
        platform="feishu",
        event_id="evt",
        chat_id="chat",
        message_id="msg",
        sender_id="sender",
        text="请总结项目下一步",
        mention_bot=True,
    )

    reply = await OSTagRunner(config).handle_event(event)

    assert reply.startswith("OS Tag mock reply:")
    assert "请总结项目下一步" in reply


@pytest.mark.asyncio
async def test_mfs_disabled_returns_empty_results(tmp_path: Path) -> None:
    config = load_settings(None, audit_log=tmp_path / "audit.log", mfs_enabled=False)

    assert await MFSClient(config).search("query") == []


def test_cli_backend_commands_do_not_use_unsafe_flags_by_default(tmp_path: Path) -> None:
    config = load_settings(None, audit_log=tmp_path / "audit.log")

    assert "--dangerously-bypass-approvals-and-sandbox" not in build_codex_command(config, "hi")
    assert "--dangerously-skip-permissions" not in build_claude_command(config)


def test_cli_backend_unsafe_flags_are_explicit(tmp_path: Path) -> None:
    config = load_settings(
        None,
        audit_log=tmp_path / "audit.log",
        codex_unsafe_mode=True,
        claude_unsafe_mode=True,
    )

    assert "--dangerously-bypass-approvals-and-sandbox" in build_codex_command(config, "hi")
    assert "--dangerously-skip-permissions" in build_claude_command(config)


def test_mfs_helpers_build_allowlisted_commands(tmp_path: Path) -> None:
    config = load_settings(
        None,
        audit_log=tmp_path / "audit.log",
        mfs_allowed_scopes=("file://./context",),
        mfs_max_results=3,
    )

    assert build_mfs_search_command(config, "roadmap", "file://./context/project.md") == [
        "mfs",
        "search",
        "roadmap",
        "file://./context/project.md",
        "--limit",
        "3",
    ]
    assert build_mfs_cat_command(config, "file://./context/project.md") == [
        "mfs",
        "cat",
        "file://./context/project.md",
    ]


def test_mfs_helpers_reject_disallowed_scope(tmp_path: Path) -> None:
    config = load_settings(
        None,
        audit_log=tmp_path / "audit.log",
        mfs_allowed_scopes=("file://./context",),
    )

    with pytest.raises(ValueError):
        build_mfs_search_command(config, "secret", "file://./other")


class SlowBackend:
    async def run(self, prompt: str) -> str:
        raise TimeoutError("backend timeout")


@pytest.mark.asyncio
async def test_backend_exception_is_handled_and_audited(tmp_path: Path) -> None:
    config = load_settings(None, audit_log=tmp_path / "audit.log")
    event = NormalizedEvent(
        platform="feishu",
        event_id="evt",
        chat_id="chat",
        message_id="msg",
        sender_id="sender",
        text="hello",
        mention_bot=True,
    )

    reply = await OSTagRunner(config, backend=SlowBackend()).handle_event(event)

    assert "backend failed" in reply
    assert '"status": "error"' in (tmp_path / "audit.log").read_text(encoding="utf-8")
