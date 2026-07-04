from __future__ import annotations

from pathlib import Path

import pytest

import ostag.backends.claude_cli as claude_cli_module
import ostag.backends.codex_cli as codex_cli_module
import ostag.mfs.client as mfs_client_module
from ostag.backends.claude_cli import ClaudeCLIBackend
from ostag.backends.codex_cli import CodexCLIBackend
from ostag.config import load_settings
from ostag.core.safety import PLATFORM_SECRET_ENV_VARS, scrub_environment
from ostag.mfs.client import MFSClient


def test_scrub_environment_removes_platform_secrets() -> None:
    env = {
        "PATH": "/usr/bin",
        "ANTHROPIC_API_KEY": "sk-ant-keep",
        "OPENAI_API_KEY": "sk-keep",
        "FEISHU_APP_SECRET": "leak1",
        "SLACK_BOT_TOKEN": "xoxb-leak",
        "DINGTALK_CLIENT_SECRET": "leak2",
        "LARK_ENCRYPT_KEY": "leak3",
    }

    scrubbed = scrub_environment(env)

    assert scrubbed["PATH"] == "/usr/bin"
    # model-provider keys survive: agent backends need their own auth
    assert scrubbed["ANTHROPIC_API_KEY"] == "sk-ant-keep"
    assert scrubbed["OPENAI_API_KEY"] == "sk-keep"
    for name in PLATFORM_SECRET_ENV_VARS:
        assert name not in scrubbed


def test_scrub_environment_extra_vars_and_default_environ(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FEISHU_APP_SECRET", "leak")
    monkeypatch.setenv("MY_INTERNAL_TOKEN", "leak2")
    monkeypatch.setenv("SAFE_VAR", "ok")

    scrubbed = scrub_environment(extra=("MY_INTERNAL_TOKEN",))

    assert "FEISHU_APP_SECRET" not in scrubbed
    assert "MY_INTERNAL_TOKEN" not in scrubbed
    assert scrubbed["SAFE_VAR"] == "ok"


class _FakeProc:
    returncode = 0

    async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
        return b"ok", b""

    def kill(self) -> None:  # pragma: no cover
        pass


def _capture_exec(captured: dict):
    async def fake_exec(*cmd, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = kwargs.get("env")
        return _FakeProc()

    return fake_exec


@pytest.mark.asyncio
async def test_codex_backend_spawns_with_scrubbed_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-secret")
    captured: dict = {}
    monkeypatch.setattr(
        codex_cli_module.asyncio, "create_subprocess_exec", _capture_exec(captured)
    )
    config = load_settings(None, audit_log=tmp_path / "audit.log")

    result = await CodexCLIBackend(config).run("hello")

    assert result == "ok"
    assert captured["env"] is not None
    assert "SLACK_BOT_TOKEN" not in captured["env"]


@pytest.mark.asyncio
async def test_claude_backend_spawns_with_scrubbed_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FEISHU_APP_SECRET", "secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-keep")
    captured: dict = {}
    monkeypatch.setattr(
        claude_cli_module.asyncio, "create_subprocess_exec", _capture_exec(captured)
    )
    config = load_settings(None, audit_log=tmp_path / "audit.log")

    await ClaudeCLIBackend(config).run("hello")

    assert "FEISHU_APP_SECRET" not in captured["env"]
    assert captured["env"]["ANTHROPIC_API_KEY"] == "sk-ant-keep"


@pytest.mark.asyncio
async def test_mfs_client_spawns_with_scrubbed_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DINGTALK_CLIENT_SECRET", "secret")
    captured: dict = {}
    monkeypatch.setattr(
        mfs_client_module.asyncio, "create_subprocess_exec", _capture_exec(captured)
    )
    config = load_settings(
        None,
        audit_log=tmp_path / "audit.log",
        mfs_enabled=True,
        mfs_allowed_scopes=("file://./context",),
    )

    await MFSClient(config).search("query")

    assert captured["env"] is not None
    assert "DINGTALK_CLIENT_SECRET" not in captured["env"]


@pytest.mark.asyncio
async def test_env_scrub_extra_reaches_backend_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CUSTOM_DEPLOY_TOKEN", "secret")
    captured: dict = {}
    monkeypatch.setattr(
        codex_cli_module.asyncio, "create_subprocess_exec", _capture_exec(captured)
    )
    config = load_settings(
        None, audit_log=tmp_path / "audit.log", env_scrub_extra=("CUSTOM_DEPLOY_TOKEN",)
    )

    await CodexCLIBackend(config).run("hello")

    assert "CUSTOM_DEPLOY_TOKEN" not in captured["env"]
