import plistlib
import sqlite3
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
    assert environment["OSTAG_COPILOT_RELAY_CANARY_SESSION_IDS"] == "${OSTAG_COPILOT_RELAY_CANARY_SESSION_IDS:-}"
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
    assert plist["EnvironmentVariables"]["OSTAG_COPILOT_RELAY_SPOOL_DB"].endswith(
        "/device-relay.sqlite3"
    )
    assert plist["EnvironmentVariables"]["OSSTACK_HOME"].startswith(
        "/Users/Shared/OStag/"
    )
    assert plist["EnvironmentVariables"]["OSSTACK_SOCKET"].endswith("/daemon.sock")
    assert plist["EnvironmentVariables"]["PATH"] == "__OSTAG_RUNTIME_PATH__"
    encoded = path.read_text()
    assert "SECRET" not in encoded and "TOKEN" not in encoded


def test_install_script_validates_absolute_repo_and_renders_launch_agent(tmp_path) -> None:
    secret_code = "PAIRING-CODE-MUST-NOT-PRINT"
    result = subprocess.run(
        [
            "bash", "deploy/copilot-relay/install-launchd.sh", "--dry-run",
            "--pairing-code", secret_code, "--display-name", "MacBook Pro",
            "--central-url", "https://example.invalid", str(tmp_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "ai.ostag.copilot-relay.plist" in result.stdout
    assert "copilot-relay.env" in result.stdout
    assert secret_code not in result.stdout + result.stderr

    relative = subprocess.run(
        ["bash", "deploy/copilot-relay/install-launchd.sh", "--dry-run", "."],
        capture_output=True,
        text=True,
    )
    assert relative.returncode != 0
    assert "absolute" in relative.stderr


def test_install_refuses_weak_existing_config_permissions(tmp_path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    config = data / "copilot-relay.env"
    config.write_text("OSTAG_COPILOT_RELAY_ENABLED=true\n")
    config.chmod(0o644)
    result = subprocess.run(
        ["bash", "deploy/copilot-relay/install-launchd.sh", str(Path.cwd())],
        env={"HOME": str(tmp_path), "OSTAG_COPILOT_DATA_DIR": str(data)},
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "0600" in result.stderr


def test_installer_preserves_only_non_secret_osstack_runtime_settings() -> None:
    installer = Path("deploy/copilot-relay/install-launchd.sh").read_text()
    assert "PRESERVED_OSSTACK_BIN" in installer
    assert 'OSTAG_MONITOR_SERVER_DB="$DATA_DIR/device-admin.sqlite3"' in installer
    assert 'RUNNER="$DATA_DIR/run-copilot-relay.sh"' in installer
    assert "printf 'OSTAG_COPILOT_RELAY_SPOOL_DB=%q" in installer
    assert "PRESERVED_OSSTACK_BIN//" in installer
    assert 'NODE_BIN="$(command -v node || true)"' in installer
    assert 'for attempt in $(seq 1 5)' in installer
    assert 'launchctl bootstrap "gui/$(id -u)" "$PLIST"' in installer
    assert 'if [[ "$attempt" == "5" ]]' in installer
    assert "RELAY_SECRET|TOKEN|PRIVATE_KEY" not in installer


def test_worker_launcher_quotes_standard_macos_paths_with_spaces() -> None:
    launcher = Path("deploy/copilot-relay/run-worker.sh").read_text(encoding="utf-8")

    assert 'exec "/opt/ostag/.venv/bin/python"' in launcher


def test_ci_explicitly_skips_only_the_external_osstack_smoke() -> None:
    workflow = yaml.safe_load(Path(".github/workflows/ostag-ci.yml").read_text())
    release_step = next(
        step
        for step in workflow["jobs"]["release-check"]["steps"]
        if step.get("name") == "Run release check"
    )
    script = Path("scripts/copilot_relay_smoke.sh").read_text(encoding="utf-8")

    assert release_step["env"]["OSTAG_SKIP_OSSTACK_CROSS_REPO_SMOKE"] == "1"
    assert '${OSTAG_SKIP_OSSTACK_CROSS_REPO_SMOKE:-0}' in script
    assert "osstack Copilot worktree not found" in script


def test_legacy_migration_is_once_only_and_preserves_audit_rows(tmp_path) -> None:
    database = tmp_path / "monitor.sqlite3"
    with sqlite3.connect(database) as db:
        db.executescript(
            "CREATE TABLE relay_sessions(instance_id TEXT,runtime_id TEXT,session_id TEXT);"
            "CREATE TABLE relay_callbacks(callback_id TEXT,reference_id TEXT,consumed_at INTEGER);"
            "CREATE TABLE monitor_instance_owners(instance_id TEXT,revoked INTEGER,revoked_at TEXT);"
            "INSERT INTO relay_sessions VALUES ('device_new','codex','session-1');"
            "INSERT INTO relay_sessions VALUES ('ostag-local','legacy','legacy-session');"
            "INSERT INTO relay_callbacks VALUES ('callback-1','ostag-local',NULL);"
            "INSERT INTO monitor_instance_owners VALUES ('ostag-local',0,'');"
        )
    command = [
        "bash", "deploy/copilot-relay/migrate-legacy-instance.sh",
        "--database", str(database), "--replacement", "device_new",
    ]
    subprocess.run(command, check=True, capture_output=True, text=True)
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT COUNT(*) FROM relay_sessions").fetchone()[0] == 2
        assert db.execute(
            "SELECT revoked FROM monitor_instance_owners WHERE instance_id='ostag-local'"
        ).fetchone()[0] == 1
        assert db.execute(
            "SELECT COUNT(*) FROM relay_legacy_migrations WHERE legacy_instance_id='ostag-local'"
        ).fetchone()[0] == 1

    refused = subprocess.run(
        [*command[:-1], "device_other"], capture_output=True, text=True
    )
    assert refused.returncode != 0
    assert "already migrated" in refused.stderr


def test_runbook_covers_rotation_backup_rollout_rollback_and_evidence() -> None:
    text = Path("docs/runbooks/copilot-relay.md").read_text(encoding="utf-8").casefold()
    for topic in (
        "key rotation", "instance revocation", "codex binary", "health check",
        "database backup", "rollout", "rollback", "evidence",
    ):
        assert topic in text
