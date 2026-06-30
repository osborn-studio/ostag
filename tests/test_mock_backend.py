from __future__ import annotations

from pathlib import Path

import pytest

from ostag.config import load_settings
from ostag.backends.base import BackendError
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


class FailingCLIBackend:
    async def run(self, prompt: str) -> str:
        raise BackendError("token_invalidated: secret stderr should stay private")


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

    assert "当前处理失败" in reply
    assert '"status": "error"' in (tmp_path / "audit.log").read_text(encoding="utf-8")


async def test_runner_remembers_channel_fact_without_backend(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("OSTAG_MEMORY_ROOT", str(tmp_path / "memory"))

    class FailingBackend:
        async def run(self, prompt):
            raise AssertionError("backend should not run for memory command")

    config = load_settings(None, audit_log=tmp_path / "audit.log", backend="mock")
    runner = OSTagRunner(config, backend=FailingBackend())
    event = NormalizedEvent(
        platform="feishu",
        event_id="evt",
        chat_id="oc_demo",
        message_id="om_demo",
        sender_id="ou_demo",
        text="记住：张三负责交易相关需求",
        mention_bot=True,
    )

    reply = await runner.handle_event(event)

    assert "已记住" in reply
    assert "张三负责交易相关需求" in reply


async def test_runner_reports_channel_brain_without_backend(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("OSTAG_MEMORY_ROOT", str(tmp_path / "memory"))
    config = load_settings(
        None,
        audit_log=tmp_path / "audit.log",
        auto_base_default_base_token="base_env",
        auto_base_default_table_id="tblEnv",
    )
    runner = OSTagRunner(config)
    remember_event = NormalizedEvent(
        platform="feishu",
        event_id="evt1",
        chat_id="oc_demo",
        message_id="om_1",
        sender_id="ou_demo",
        text="记住：张三负责交易相关需求",
        mention_bot=True,
    )
    status_event = NormalizedEvent(
        platform="feishu",
        event_id="evt2",
        chat_id="oc_demo",
        message_id="om_2",
        sender_id="ou_demo",
        text="这个群你记住了什么",
        mention_bot=True,
    )

    await runner.handle_event(remember_event)
    reply = await runner.handle_event(status_event)

    assert "Channel Brain" in reply
    assert "张三负责交易相关需求" in reply
    assert "base=base_env" in reply


async def test_runner_supports_slack_channel_brain(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("OSTAG_MEMORY_ROOT", str(tmp_path / "memory"))
    config = load_settings(None, audit_log=tmp_path / "audit.log")
    runner = OSTagRunner(config)

    reply = await runner.handle_event(
        NormalizedEvent(
            platform="slack",
            event_id="evt",
            chat_id="C123",
            message_id="1710000000.000100",
            sender_id="U123",
            text="记住：这个 Slack 频道用于产品需求评审",
            mention_bot=True,
        )
    )
    status = await runner.handle_event(
        NormalizedEvent(
            platform="slack",
            event_id="evt2",
            chat_id="C123",
            message_id="1710000000.000200",
            sender_id="U123",
            text="这个群你记住了什么",
            mention_bot=True,
        )
    )

    assert "已记住" in reply
    assert "这个 Slack 频道用于产品需求评审" in status


@pytest.mark.asyncio
async def test_backend_error_reply_does_not_expose_private_cli_output(tmp_path: Path) -> None:
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

    reply = await OSTagRunner(config, backend=FailingCLIBackend()).handle_event(event)
    audit = (tmp_path / "audit.log").read_text(encoding="utf-8")

    assert "通用问答后端暂时不可用" in reply
    assert "token_invalidated" not in reply
    assert "token_invalidated" in audit
