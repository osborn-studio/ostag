import plistlib
import subprocess
from pathlib import Path

import yaml


def test_central_compose_reuses_service_port_and_database_with_safe_relay_flags() -> None:
    compose = yaml.safe_load(Path("deploy/work-monitor/docker-compose.yml").read_text())
    service = compose["services"]["work-monitor"]
    environment = service["environment"]

    assert service["ports"] == ["127.0.0.1:${OSTAG_MONITOR_PORT:-8000}:8000"]
    assert environment["OSTAG_MONITOR_SERVER_DB"] == "/var/lib/ostag/work_monitor.sqlite3"
    assert environment["OSTAG_COPILOT_RELAY_SERVER_ENABLED"] == "${OSTAG_COPILOT_RELAY_SERVER_ENABLED:-true}"
    assert environment["OSTAG_COPILOT_RELAY_WRITE_ENABLED"] == "${OSTAG_COPILOT_RELAY_WRITE_ENABLED:-false}"
    assert not any("SECRET" in key or "TOKEN" in key for key in environment)
    assert len(service["volumes"]) == 1


def test_launchd_template_is_bounded_absolute_and_keeps_secrets_outside_plist() -> None:
    path = Path("deploy/copilot-relay/ai.ostag.copilot-relay.plist")
    plist = plistlib.loads(path.read_bytes())
    arguments = plist["ProgramArguments"]

    assert all(item.startswith("/") for item in arguments[:2])
    assert plist["KeepAlive"] is True and plist["RunAtLoad"] is True
    assert 10 <= plist["ThrottleInterval"] <= 300
    assert plist["StandardOutPath"] != plist["StandardErrorPath"]
    assert plist["StandardOutPath"].startswith("/Users/Shared/OStag/")
    assert plist["EnvironmentVariables"]["OSTAG_COPILOT_RELAY_SPOOL_DB"].startswith(
        "/Users/Shared/OStag/"
    )
    encoded = path.read_text()
    assert "SECRET" not in encoded and "TOKEN" not in encoded


def test_install_script_validates_absolute_repo_and_renders_launch_agent(tmp_path) -> None:
    result = subprocess.run(
        ["bash", "deploy/copilot-relay/install-launchd.sh", "--dry-run", str(tmp_path)],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "ai.ostag.copilot-relay.plist" in result.stdout
    assert "copilot-relay.env" in result.stdout


def test_worker_launcher_quotes_standard_macos_paths_with_spaces() -> None:
    launcher = Path("deploy/copilot-relay/run-worker.sh").read_text(encoding="utf-8")

    assert 'exec "/opt/ostag/.venv/bin/python"' in launcher


def test_runbook_covers_rotation_backup_rollout_rollback_and_evidence() -> None:
    text = Path("docs/runbooks/copilot-relay.md").read_text(encoding="utf-8").casefold()
    for topic in (
        "key rotation", "instance revocation", "codex binary", "health check",
        "database backup", "rollout", "rollback", "evidence",
    ):
        assert topic in text
