from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import sqlite3
import sys

from ostag.config import load_settings
from ostag.core.doctor import doctor_exit_code, format_checks, run_doctor
from ostag.main import main_async
from ostag.work_monitor.store import MonitorStore


LIVE_CREDENTIAL_ENV = (
    "FEISHU_APP_ID",
    "FEISHU_APP_SECRET",
    "LARK_APP_ID",
    "LARK_APP_SECRET",
    "SLACK_BOT_TOKEN",
    "SLACK_APP_TOKEN",
    "DINGTALK_CLIENT_ID",
    "DINGTALK_CLIENT_SECRET",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_WEBHOOK_SECRET",
    "TELEGRAM_WEBHOOK_URL",
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


def test_doctor_reports_github_vercel_release_readiness_without_token(
    tmp_path: Path,
) -> None:
    token = "vercel-secret-never-render"
    config = load_settings(
        None,
        audit_log=tmp_path / "audit.log",
        github_vercel_release_enabled=True,
        github_vercel_release_repository="owner/repo",
        github_vercel_release_channels=("feishu:oc_release",),
        vercel_access_token=token,
        vercel_team_id="team_test",
        vercel_project_id="prj_test",
        vercel_project_name="release-app",
        gh_bin=sys.executable,
    )

    output = format_checks(run_doctor(config))

    assert "github_vercel_release" in output
    assert "owner/repo -> release-app" in output
    assert token not in output


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


def test_monitor_reporter_doctor_requires_credential_without_leaking_url(tmp_path: Path) -> None:
    token = "fake-central-token-must-not-leak"
    config = load_settings(
        None,
        audit_log=tmp_path / "audit.log",
        monitor_enabled=True,
        monitor_central_url=f"https://{token}@monitor.example",
        monitor_instance_secret="",
    )

    output = format_checks(run_doctor(config))

    assert "monitor_instance_credential" in output
    assert token not in output
    assert doctor_exit_code(run_doctor(config)) == 1


def test_monitor_reporter_doctor_reports_unreachable_origin_redacted(
    monkeypatch, tmp_path: Path
) -> None:
    def unavailable(*args, **kwargs):
        raise OSError("request contained fake-origin-secret")

    monkeypatch.setattr("ostag.core.doctor.urlopen", unavailable)
    config = load_settings(
        None,
        audit_log=tmp_path / "audit.log",
        monitor_enabled=True,
        monitor_central_url="https://monitor.example",
        monitor_instance_secret="fake-instance-secret",
    )

    output = format_checks(run_doctor(config))

    assert "monitor_origin" in output and "unreachable" in output
    assert "fake-origin-secret" not in output


def test_production_monitor_doctor_verifies_volume_backup_restore_and_scheduler_owner(
    monkeypatch, tmp_path: Path
) -> None:
    database = tmp_path / "volume" / "monitor.sqlite3"
    database.parent.mkdir()
    MonitorStore(database)
    monkeypatch.setenv("OSTAG_MONITOR_ROLE", "central")
    future = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
    with sqlite3.connect(database) as connection:
        connection.executemany(
            "INSERT INTO monitor_scheduler_owners VALUES (?,?,?)",
            (("owner-a", future, future), ("owner-b", future, future)),
        )
    config = load_settings(
        None,
        env="production",
        audit_log=tmp_path / "audit.log",
        monitor_server_db=database,
        monitor_admin_token="fake-admin-token",
        monitor_instance_secrets=(("mac", "fake-instance-token"),),
    )

    output = format_checks(run_doctor(config))

    assert "persistent_volume" in output and "backup and restore verified" in output
    assert "scheduler_owner" in output and "exactly one" in output
    assert "fake-admin-token" not in output and "fake-instance-token" not in output


def test_production_monitor_doctor_reports_unwritable_volume(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OSTAG_MONITOR_ROLE", "central")
    config = load_settings(
        None,
        env="production",
        audit_log=tmp_path / "audit.log",
        monitor_server_db=Path("/proc/ostag/monitor.sqlite3"),
        monitor_admin_token="configured",
        monitor_instance_secrets=(("mac", "configured"),),
    )

    output = format_checks(run_doctor(config))

    assert "persistent_volume" in output and "not writable" in output


def test_production_monitor_platform_and_stale_instance_checks(monkeypatch, tmp_path: Path) -> None:
    database = tmp_path / "monitor.sqlite3"
    MonitorStore(database)
    stale = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    with sqlite3.connect(database) as connection:
        connection.execute("INSERT INTO instances VALUES (?,?,?)", ("mac", 1, stale))
    monkeypatch.setenv("OSTAG_MONITOR_ROLE", "central")
    future = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO monitor_scheduler_owners VALUES (?,?,?)", ("owner", future, future)
        )
    config = load_settings(
        None,
        env="production",
        platform="feishu",
        audit_log=tmp_path / "audit.log",
        monitor_server_db=database,
        monitor_admin_token="configured",
        monitor_instance_secrets=(("mac", "configured"),),
        feishu_app_id="app",
        feishu_app_secret="fake-feishu-secret",
        feishu_verification_token="verify",
        feishu_encrypt_key="encrypt",
        feishu_use_websocket=True,
        feishu_callback_url="https://monitor.example/webhooks/feishu/card-action",
        monitor_offline_seconds=600,
    )

    output = format_checks(
        run_doctor(config, http_probe=lambda *args: (401, b""))
    )

    assert "feishu_callback" in output and "verification passed" in output
    assert "stale_instances" in output and "mac" in output
    assert "fake-feishu-secret" not in output


def test_production_feishu_doctor_rejects_callback_only_without_websocket(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OSTAG_MONITOR_ROLE", "central")
    config = load_settings(
        None, env="production", platform="feishu", audit_log=tmp_path / "audit.log",
        feishu_app_id="app", feishu_app_secret="secret",
        feishu_verification_token="verify", feishu_encrypt_key="encrypt",
        feishu_use_websocket=False,
        feishu_callback_url="https://monitor.example/webhooks/feishu/card-action",
    )
    check = next(item for item in run_doctor(
        config, http_probe=lambda *args: (401, b"")
    ) if item.name == "feishu_callback")
    assert check.ok is False


def test_production_telegram_doctor_requires_verified_webhook_status(
    monkeypatch, tmp_path: Path
) -> None:
    database = tmp_path / "monitor.sqlite3"
    MonitorStore(database)
    monkeypatch.setenv("OSTAG_MONITOR_ROLE", "central")
    future = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO monitor_scheduler_owners VALUES (?,?,?)", ("owner", future, future)
        )
    config = load_settings(
        None,
        env="production",
        platform="telegram",
        audit_log=tmp_path / "audit.log",
        monitor_server_db=database,
        monitor_admin_token="configured",
        monitor_instance_secrets=(("mac", "configured"),),
        telegram_bot_token="fake-telegram-token",
        telegram_webhook_secret="hook-secret",
        telegram_webhook_url="https://monitor.example/webhooks/telegram",
    )

    output = format_checks(
        run_doctor(
            config,
            http_probe=lambda *args: (
                200,
                b'{"ok":true,"result":{"url":"https://wrong.example/hook"}}',
            ),
        )
    )

    assert "telegram_webhook" in output and "not verified" in output
    assert "fake-telegram-token" not in output


def test_scheduler_owner_uses_runtime_probe_not_claimed_environment(
    monkeypatch, tmp_path: Path
) -> None:
    database = tmp_path / "monitor.sqlite3"
    MonitorStore(database)
    monkeypatch.setenv("OSTAG_MONITOR_ROLE", "central")
    monkeypatch.setenv("OSTAG_MONITOR_SCHEDULER_OWNER_COUNT", "1")
    config = load_settings(
        None,
        env="production",
        audit_log=tmp_path / "audit.log",
        monitor_server_db=database,
    )

    output = format_checks(run_doctor(config, scheduler_owner_probe=lambda _: 2))

    assert "scheduler_owner" in output and "exactly one" in output


def test_feishu_callback_runtime_probe_failure_and_exception_are_redacted(
    monkeypatch, tmp_path: Path
) -> None:
    database = tmp_path / "monitor.sqlite3"
    MonitorStore(database)
    monkeypatch.setenv("OSTAG_MONITOR_ROLE", "central")
    config = load_settings(
        None,
        env="production",
        platform="feishu",
        audit_log=tmp_path / "audit.log",
        monitor_server_db=database,
        feishu_app_id="app",
        feishu_app_secret="fake-callback-secret",
        feishu_verification_token="verify",
        feishu_encrypt_key="encrypt",
        feishu_use_websocket=True,
        feishu_callback_url="https://monitor.example/webhooks/feishu/card-action",
    )

    for probe in (
        lambda *args: (503, b""),
        lambda *args: (_ for _ in ()).throw(OSError("fake-callback-secret unreachable")),
    ):
        output = format_checks(run_doctor(config, http_probe=probe))
        assert "feishu_callback" in output and "failed or is unreachable" in output
        assert "fake-callback-secret" not in output


def test_telegram_webhook_probe_uses_bounded_transport_and_expected_url(
    monkeypatch, tmp_path: Path
) -> None:
    database = tmp_path / "monitor.sqlite3"
    MonitorStore(database)
    monkeypatch.setenv("OSTAG_MONITOR_ROLE", "central")
    expected = "https://monitor.example/webhooks/telegram"
    observed = []

    def probe(method, url, body, headers, timeout):
        observed.append((method, url.startswith("https://api.telegram.org/bot"), body, timeout))
        return 200, json.dumps({"ok": True, "result": {"url": expected}}).encode()

    config = load_settings(
        None,
        env="production",
        platform="telegram",
        audit_log=tmp_path / "audit.log",
        monitor_server_db=database,
        telegram_bot_token="synthetic-telegram-credential",
        telegram_webhook_secret="synthetic-webhook-credential",
        telegram_webhook_url=expected,
    )
    output = format_checks(run_doctor(config, http_probe=probe))

    assert "Telegram webhook is verified" in output
    assert observed == [("GET", True, None, 5.0)]
    assert "synthetic-telegram-credential" not in output
