from __future__ import annotations

from pathlib import Path

from ostag.config import load_settings
from ostag.core.doctor import doctor_exit_code, format_checks, run_doctor
from ostag.main import main_async


LIVE_CREDENTIAL_ENV = (
    "FEISHU_APP_ID",
    "FEISHU_APP_SECRET",
    "LARK_APP_ID",
    "LARK_APP_SECRET",
    "SLACK_BOT_TOKEN",
    "SLACK_APP_TOKEN",
    "DINGTALK_CLIENT_ID",
    "DINGTALK_CLIENT_SECRET",
)


def clear_live_credentials(monkeypatch) -> None:
    for name in LIVE_CREDENTIAL_ENV:
        monkeypatch.delenv(name, raising=False)


def test_doctor_passes_for_default_dry_run(tmp_path: Path) -> None:
    config = load_settings(None, audit_log=tmp_path / "audit.log", dry_run=True, backend="mock")

    checks = run_doctor(config, Path("examples/feishu_event.json"))

    assert doctor_exit_code(checks) == 0
    assert "[ok] backend: mock backend is ready" in format_checks(checks)
    assert "[ok] event_file:" in format_checks(checks)


def test_doctor_errors_for_missing_live_credentials(monkeypatch, tmp_path: Path) -> None:
    clear_live_credentials(monkeypatch)
    monkeypatch.chdir(tmp_path)
    config = load_settings(None, audit_log=tmp_path / "audit.log", dry_run=False, platform="feishu")

    checks = run_doctor(config)

    assert doctor_exit_code(checks) == 1
    assert "missing live credentials" in format_checks(checks)


def test_doctor_warns_but_does_not_fail_for_explicit_unsafe_switch(tmp_path: Path) -> None:
    config = load_settings(None, audit_log=tmp_path / "audit.log", allow_shell=True)

    checks = run_doctor(config)

    assert doctor_exit_code(checks) == 0
    assert "[warn] safety:" in format_checks(checks)


def test_live_cli_doctor_requires_credentials(monkeypatch, tmp_path: Path) -> None:
    clear_live_credentials(monkeypatch)
    monkeypatch.chdir(tmp_path)

    exit_code = __import__("asyncio").run(main_async(["--platform", "feishu", "--live", "--doctor"]))

    assert exit_code == 1


def test_slack_live_cli_doctor_requires_credentials(monkeypatch, tmp_path: Path) -> None:
    clear_live_credentials(monkeypatch)
    monkeypatch.chdir(tmp_path)

    exit_code = __import__("asyncio").run(main_async(["--platform", "slack", "--live", "--doctor"]))

    assert exit_code == 1


def test_dingtalk_live_cli_doctor_requires_credentials(monkeypatch, tmp_path: Path) -> None:
    clear_live_credentials(monkeypatch)
    monkeypatch.chdir(tmp_path)

    exit_code = __import__("asyncio").run(
        main_async(["--platform", "dingtalk", "--live", "--doctor"])
    )

    assert exit_code == 1
