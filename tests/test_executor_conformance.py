from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from ostag.backends.executor_runtime import (
    ExecutorManifest,
    ExecutorProfile,
    ExecutorRuntime,
    executor_profiles,
    run_conformance_suite,
)
from ostag.backends.kimi_cli import KimiCLIBackend, build_kimi_command
from ostag.config import load_settings
from ostag.core.doctor import format_checks, run_doctor
from ostag.main import main_async


def test_executor_manifest_round_trip_is_strict() -> None:
    wire = {
        "schemaVersion": 1,
        "executorId": "kimi_cli",
        "backend": "kimi_cli",
        "command": "kimi",
        "promptTransport": "argv",
        "resumeMode": "session_id",
        "artifactMode": "stdout",
        "timeoutSeconds": 120,
        "riskMode": "approval_required",
        "enabled": False,
        "osstackRegistered": False,
    }

    assert ExecutorManifest.from_dict(wire).to_dict() == wire
    with pytest.raises(ValueError, match="unknown fields"):
        ExecutorManifest.from_dict({**wire, "unexpected": True})
    with pytest.raises(ValueError, match="resumeMode"):
        ExecutorManifest.from_dict({**wire, "resumeMode": "maybe"})


def test_kimi_command_keeps_untrusted_prompt_in_one_argument(tmp_path: Path) -> None:
    config = load_settings(
        None,
        audit_log=tmp_path / "audit.log",
        kimi_bin="/opt/kimi code/kimi",
    )
    prompt = "review; touch /tmp/should-not-run\n$(whoami)"

    command = build_kimi_command(config, prompt, resume_session_id="session_123")

    assert command == [
        "/opt/kimi code/kimi",
        "--session",
        "session_123",
        "--prompt",
        prompt,
        "--output-format",
        "text",
    ]
    assert "--yolo" not in command
    assert "--auto" not in command


def test_kimi_resume_session_id_is_bounded(tmp_path: Path) -> None:
    config = load_settings(None, audit_log=tmp_path / "audit.log")

    with pytest.raises(ValueError, match="session id"):
        build_kimi_command(config, "hello", resume_session_id="bad session")


@pytest.mark.asyncio
async def test_executor_runtime_scrubs_environment_and_uses_stdin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FEISHU_APP_SECRET", "must-not-leak")
    script = (
        "import json, os, sys; "
        "print(json.dumps({'prompt': sys.stdin.read(), "
        "'secret': os.getenv('FEISHU_APP_SECRET')}))"
    )
    manifest = ExecutorManifest(
        executor_id="fake_cli",
        backend="fake_cli",
        command=sys.executable,
        prompt_transport="stdin",
        resume_mode="none",
        artifact_mode="stdout",
        timeout_seconds=2,
        risk_mode="sandboxed",
        enabled=True,
        osstack_registered=False,
    )
    profile = ExecutorProfile(manifest, lambda prompt, resume: [sys.executable, "-c", script])

    result = await ExecutorRuntime(profile, max_output_bytes=4096).run("hello")

    assert json.loads(result) == {"prompt": "hello", "secret": None}


@pytest.mark.asyncio
async def test_executor_runtime_kills_timeout() -> None:
    manifest = ExecutorManifest(
        executor_id="slow_cli",
        backend="slow_cli",
        command=sys.executable,
        prompt_transport="stdin",
        resume_mode="none",
        artifact_mode="stdout",
        timeout_seconds=1,
        risk_mode="sandboxed",
        enabled=True,
        osstack_registered=False,
    )
    profile = ExecutorProfile(
        manifest,
        lambda prompt, resume: [sys.executable, "-c", "import time; time.sleep(5)"],
    )

    with pytest.raises(RuntimeError, match="timed out"):
        await ExecutorRuntime(profile, max_output_bytes=4096).run("hello")


@pytest.mark.asyncio
async def test_executor_runtime_bounds_output_while_reading() -> None:
    manifest = ExecutorManifest(
        executor_id="loud_cli",
        backend="loud_cli",
        command=sys.executable,
        prompt_transport="stdin",
        resume_mode="none",
        artifact_mode="stdout",
        timeout_seconds=2,
        risk_mode="sandboxed",
        enabled=True,
        osstack_registered=False,
    )
    profile = ExecutorProfile(
        manifest,
        lambda prompt, resume: [sys.executable, "-c", "print('x' * 8192)"],
    )

    with pytest.raises(RuntimeError, match="output exceeded"):
        await ExecutorRuntime(profile, max_output_bytes=4096).run("hello")


@pytest.mark.asyncio
async def test_fake_executor_conformance_is_deterministic(tmp_path: Path) -> None:
    config = load_settings(
        None,
        audit_log=tmp_path / "audit.log",
        kimi_bin=sys.executable,
    )
    profile = executor_profiles(config)["kimi_cli"]

    first = await run_conformance_suite(profile)
    second = await run_conformance_suite(profile)

    assert first.to_dict() == second.to_dict()
    assert first.conformant is True
    assert {check.name for check in first.checks} == {
        "manifest",
        "argv",
        "environment",
        "timeout",
        "resume",
        "executable",
    }


@pytest.mark.asyncio
async def test_kimi_backend_requires_explicit_activation(tmp_path: Path) -> None:
    config = load_settings(
        None,
        audit_log=tmp_path / "audit.log",
        backend="kimi_cli",
        executor_runtime_enabled=False,
        kimi_executor_enabled=False,
    )

    with pytest.raises(RuntimeError, match="disabled"):
        await KimiCLIBackend(config).run("hello")


def test_doctor_requires_both_kimi_activation_flags(tmp_path: Path) -> None:
    config = load_settings(
        None,
        audit_log=tmp_path / "audit.log",
        backend="kimi_cli",
        kimi_bin=sys.executable,
        executor_runtime_enabled=True,
        kimi_executor_enabled=False,
    )

    checks = run_doctor(config)
    output = format_checks(checks)

    assert "Kimi executor is disabled" in output
    backend_check = next(check for check in checks if check.name == "backend")
    assert sys.executable not in backend_check.message


@pytest.mark.asyncio
async def test_executor_cli_reports_kimi_unavailable_without_crashing(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KIMI_BIN", str(tmp_path / "missing-kimi"))

    exit_code = await main_async(["executor", "test", "kimi_cli", "--json"])

    assert exit_code == 1
    report = json.loads(capsys.readouterr().out)
    assert report["executorId"] == "kimi_cli"
    assert report["conformant"] is False
    assert report["activation"] == "disabled"
    assert next(check for check in report["checks"] if check["name"] == "executable") == {
        "name": "executable",
        "ok": False,
        "detail": "command unavailable",
    }
    assert report["manifest"]["osstackRegistered"] is False
    assert str(tmp_path) not in json.dumps(report)
