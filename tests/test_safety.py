from __future__ import annotations

from pathlib import Path

import pytest

from ostag.config import load_settings
from ostag.core.event import NormalizedEvent
from ostag.core.logging import write_audit_log
from ostag.core.runner import OSTagRunner
from ostag.core.safety import check_message_length, redact_secrets, validate_scope


def test_redact_secrets() -> None:
    text = "OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwxyz xoxb-123-abc AKIA1234567890ABCDEF"

    assert "[REDACTED]" in redact_secrets(text)
    assert "sk-" not in redact_secrets(text)
    assert "xoxb-" not in redact_secrets(text)
    assert "AKIA" not in redact_secrets(text)


def test_message_length_rejects_oversized_input() -> None:
    result = check_message_length("abcdef", 3)

    assert result.ok is False
    assert result.text == "abc"
    assert result.reason


def test_validate_scope_stays_inside_allowlist() -> None:
    allowed = ("file://./context", "github://org/repo")

    assert validate_scope("file://./context/project_alpha.md", allowed)
    assert not validate_scope("file://./other", allowed)


@pytest.mark.asyncio
async def test_runner_rejects_too_long_message(tmp_path: Path) -> None:
    config = load_settings(None, audit_log=tmp_path / "audit.log", max_input_chars=4)
    event = NormalizedEvent(
        platform="feishu",
        event_id="evt",
        chat_id="chat",
        message_id="msg",
        sender_id="sender",
        text="hello world",
        mention_bot=True,
    )

    reply = await OSTagRunner(config).handle_event(event)

    assert "已拒绝处理" in reply
    assert '"status": "rejected"' in (tmp_path / "audit.log").read_text(encoding="utf-8")


def test_audit_log_writes_jsonl(tmp_path: Path) -> None:
    event = NormalizedEvent("feishu", "evt", "chat", "msg", "sender", "hello", True)

    record = write_audit_log(
        tmp_path / "audit.log",
        event=event,
        backend="mock",
        mfs_enabled=False,
        input_text="hello",
        output_text="world",
        status="ok",
    )

    assert record["status"] == "ok"
    assert (tmp_path / "audit.log").read_text(encoding="utf-8").count("\n") == 1


def test_prod_audit_log_hashes_input_without_preview_or_secret(tmp_path: Path) -> None:
    event = NormalizedEvent("feishu", "evt", "chat", "msg", "sender", "hello", True)

    record = write_audit_log(
        tmp_path / "audit.log",
        event=event,
        backend="mock",
        mfs_enabled=False,
        input_text="OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwxyz",
        output_text="world",
        status="ok",
        env="prod",
    )
    log_text = (tmp_path / "audit.log").read_text(encoding="utf-8")

    assert "input_sha256_16" in record
    assert "input_preview" not in record
    assert "sk-" not in log_text
    assert "OPENAI_API_KEY" not in log_text
