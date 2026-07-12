from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable
from urllib.request import Request, urlopen

from ostag.config import Settings


HttpProbe = Callable[[str, str, bytes | None, dict[str, str], float], tuple[int, bytes]]


@dataclass(frozen=True)
class DoctorCheck:
    name: str
    ok: bool
    severity: str
    message: str


def run_doctor(
    config: Settings,
    event_path: Path | None = None,
    *,
    scheduler_owner_probe: Callable[[Settings], int] | None = None,
    http_probe: HttpProbe | None = None,
) -> list[DoctorCheck]:
    checks = [
        _check_python(),
        _check_safe_defaults(config),
        _check_audit_log(config),
        _check_backend(config),
        _check_mfs(config),
        _check_live_adapter(config),
    ]
    if event_path is not None:
        checks.append(_check_event_file(event_path))
    if config.monitor_enabled:
        checks.extend((_check_monitor_instance_credential(config), _check_monitor_origin(config)))
    if os.getenv("OSTAG_MONITOR_ROLE", "").casefold() == "central":
        checks.extend(
            (
                _check_monitor_central_credentials(config),
                _check_persistent_volume(config),
                _check_scheduler_owner(config, scheduler_owner_probe),
                _check_monitor_platform(config, http_probe or _bounded_http_probe),
                _check_stale_instances(config),
            )
        )
    return checks


def doctor_exit_code(checks: list[DoctorCheck]) -> int:
    return 1 if any((not check.ok and check.severity == "error") for check in checks) else 0


def format_checks(checks: list[DoctorCheck]) -> str:
    lines = []
    for check in checks:
        state = "ok" if check.ok else check.severity
        lines.append(f"[{state}] {check.name}: {check.message}")
    return "\n".join(lines)


def _check_python() -> DoctorCheck:
    ok = sys.version_info >= (3, 11)
    return DoctorCheck(
        "python",
        ok,
        "error",
        f"Python {sys.version_info.major}.{sys.version_info.minor}; requires >=3.11",
    )


def _check_safe_defaults(config: Settings) -> DoctorCheck:
    unsafe = []
    if config.allow_shell:
        unsafe.append("OSTAG_ALLOW_SHELL=true")
    if config.codex_unsafe_mode:
        unsafe.append("OSTAG_CODEX_UNSAFE_MODE=true")
    if config.claude_unsafe_mode:
        unsafe.append("OSTAG_CLAUDE_UNSAFE_MODE=true")
    if unsafe:
        return DoctorCheck("safety", False, "warn", "unsafe switches enabled: " + ", ".join(unsafe))
    return DoctorCheck("safety", True, "error", "dangerous shell and unsafe CLI flags are disabled")


def _check_audit_log(config: Settings) -> DoctorCheck:
    try:
        config.audit_log.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return DoctorCheck("audit_log", False, "error", f"cannot create audit directory: {exc}")
    return DoctorCheck("audit_log", True, "error", f"audit log path is writable: {config.audit_log}")


def _check_backend(config: Settings) -> DoctorCheck:
    if config.backend == "mock":
        return DoctorCheck("backend", True, "error", "mock backend is ready")
    if config.backend == "codex_cli":
        return _check_executable("backend", config.codex_bin, "Codex CLI")
    if config.backend == "claude_cli":
        return _check_executable("backend", config.claude_bin, "Claude CLI")
    if config.backend == "openai_api":
        if importlib.util.find_spec("openai") is None:
            return DoctorCheck("backend", False, "error", "OpenAI SDK missing; run `uv sync --extra openai`")
        return DoctorCheck("backend", True, "error", f"OpenAI SDK installed; model={config.openai_model}")
    return DoctorCheck("backend", False, "error", f"unknown backend: {config.backend}")


def _check_mfs(config: Settings) -> DoctorCheck:
    if not config.mfs_enabled:
        return DoctorCheck("mfs", True, "error", "MFS disabled; context lookup will be skipped")
    if shutil.which(config.mfs_bin) is None:
        return DoctorCheck("mfs", False, "warn", f"MFS binary not found: {config.mfs_bin}")
    scopes = ", ".join(config.mfs_allowed_scopes)
    return DoctorCheck("mfs", True, "error", f"MFS enabled with scopes: {scopes}")


def _check_live_adapter(config: Settings) -> DoctorCheck:
    if config.dry_run:
        return DoctorCheck("adapter", True, "error", "dry-run mode; live credentials not required")
    if config.platform == "feishu":
        missing = [
            name
            for name, value in {
                "FEISHU_APP_ID": config.feishu_app_id,
                "FEISHU_APP_SECRET": config.feishu_app_secret,
            }.items()
            if not value
        ]
    elif config.platform == "lark":
        missing = [
            name
            for name, value in {
                "LARK_APP_ID": config.lark_app_id,
                "LARK_APP_SECRET": config.lark_app_secret,
            }.items()
            if not value
        ]
    elif config.platform == "slack":
        missing = [
            name
            for name, value in {
                "SLACK_BOT_TOKEN": config.slack_bot_token,
                "SLACK_APP_TOKEN": config.slack_app_token,
            }.items()
            if not value
        ]
    elif config.platform == "dingtalk":
        missing = [
            name
            for name, value in {
                "DINGTALK_CLIENT_ID": config.dingtalk_client_id,
                "DINGTALK_CLIENT_SECRET": config.dingtalk_client_secret,
            }.items()
            if not value
        ]
    else:
        return DoctorCheck("adapter", False, "error", f"unsupported live platform: {config.platform}")
    if missing:
        return DoctorCheck("adapter", False, "error", "missing live credentials: " + ", ".join(missing))
    if config.platform == "slack":
        if importlib.util.find_spec("slack_bolt") is None:
            return DoctorCheck("adapter", False, "error", "slack-bolt missing; run `uv sync --extra slack`")
        return DoctorCheck("adapter", True, "error", "slack live adapter dependencies found")
    if config.platform == "dingtalk":
        if importlib.util.find_spec("dingtalk_stream") is None:
            return DoctorCheck(
                "adapter",
                False,
                "error",
                "dingtalk-stream missing; run `uv sync --extra dingtalk`",
            )
        return DoctorCheck("adapter", True, "error", "dingtalk live adapter dependencies found")
    if importlib.util.find_spec("lark_oapi") is None:
        return DoctorCheck("adapter", False, "error", "lark-oapi missing; run `uv sync --extra feishu`")
    return DoctorCheck("adapter", True, "error", f"{config.platform} live adapter dependencies found")


def _check_event_file(event_path: Path) -> DoctorCheck:
    if not event_path.exists():
        return DoctorCheck("event_file", False, "error", f"event file not found: {event_path}")
    if event_path.stat().st_size == 0:
        return DoctorCheck("event_file", False, "error", f"event file is empty: {event_path}")
    return DoctorCheck("event_file", True, "error", f"event file exists: {event_path}")


def _check_executable(name: str, executable: str, label: str) -> DoctorCheck:
    path = shutil.which(executable)
    if path is None:
        return DoctorCheck(name, False, "error", f"{label} not found on PATH: {executable}")
    return DoctorCheck(name, True, "error", f"{label} found: {path}")


def _check_monitor_instance_credential(config: Settings) -> DoctorCheck:
    if not config.monitor_instance_secret:
        return DoctorCheck(
            "monitor_instance_credential", False, "error", "instance credential is missing"
        )
    return DoctorCheck(
        "monitor_instance_credential", True, "error", "instance credential is configured"
    )


def _check_monitor_origin(config: Settings) -> DoctorCheck:
    if not config.monitor_central_url.startswith(("http://", "https://")):
        return DoctorCheck("monitor_origin", False, "error", "central origin URL is missing")
    try:
        request = Request(config.monitor_central_url.rstrip("/") + "/healthz", method="GET")
        with urlopen(request, timeout=5) as response:  # nosec B310 - URL is operator configuration
            ok = response.status == 200
    except Exception:
        return DoctorCheck("monitor_origin", False, "error", "central origin is unreachable")
    if not ok:
        return DoctorCheck("monitor_origin", False, "error", "central origin health check failed")
    return DoctorCheck("monitor_origin", True, "error", "central origin health check passed")


def _check_monitor_central_credentials(config: Settings) -> DoctorCheck:
    missing = []
    if not config.monitor_admin_token:
        missing.append("OSTAG_MONITOR_ADMIN_TOKEN")
    if not config.monitor_instance_secrets:
        missing.append("OSTAG_MONITOR_INSTANCE_SECRETS")
    if missing:
        return DoctorCheck(
            "monitor_central_credentials",
            False,
            "error",
            "missing central credentials: " + ", ".join(missing),
        )
    return DoctorCheck(
        "monitor_central_credentials", True, "error", "central credentials are configured"
    )


def _check_persistent_volume(config: Settings) -> DoctorCheck:
    database = config.monitor_server_db
    parent = database.parent
    probe_path: Path | None = None
    backup_path: Path | None = None
    try:
        parent.mkdir(parents=True, exist_ok=True)
        fd, probe = tempfile.mkstemp(prefix=".ostag-write-", dir=parent)
        os.close(fd)
        probe_path = Path(probe)
        if not database.is_file():
            raise OSError("database missing")
        fd, backup = tempfile.mkstemp(prefix=".ostag-backup-", suffix=".sqlite3", dir=parent)
        os.close(fd)
        backup_path = Path(backup)
        with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as source:
            with sqlite3.connect(backup_path) as target:
                source.backup(target)
        with sqlite3.connect(f"file:{backup_path}?mode=ro", uri=True) as restored:
            result = restored.execute("PRAGMA integrity_check").fetchone()
        if result is None or result[0] != "ok":
            raise OSError("restore verification failed")
    except (OSError, sqlite3.Error):
        return DoctorCheck(
            "persistent_volume", False, "error", "persistent volume is not writable or recoverable"
        )
    finally:
        for path in (probe_path, backup_path):
            if path is not None:
                path.unlink(missing_ok=True)
    return DoctorCheck(
        "persistent_volume", True, "error", "persistent volume write, backup and restore verified"
    )


def _check_scheduler_owner(
    config: Settings, probe: Callable[[Settings], int] | None = None
) -> DoctorCheck:
    if not config.monitor_scheduler_enabled:
        return DoctorCheck("scheduler_owner", True, "error", "scheduler is disabled")
    try:
        if probe is not None:
            count = probe(config)
        else:
            with sqlite3.connect(
                f"file:{config.monitor_server_db}?mode=ro", uri=True
            ) as connection:
                count = int(
                    connection.execute(
                        "SELECT count(*) FROM monitor_scheduler_owners WHERE lease_until>?",
                        (datetime.now(UTC).isoformat(),),
                    ).fetchone()[0]
                )
    except (OSError, sqlite3.Error, TypeError, ValueError):
        return DoctorCheck(
            "scheduler_owner", False, "error", "scheduler runtime ownership cannot be verified"
        )
    if count != 1:
        return DoctorCheck(
            "scheduler_owner", False, "error", "exactly one scheduler owner is required"
        )
    return DoctorCheck("scheduler_owner", True, "error", "exactly one scheduler owner verified")


def _bounded_http_probe(
    method: str,
    url: str,
    body: bytes | None,
    headers: dict[str, str],
    timeout: float,
) -> tuple[int, bytes]:
    request = Request(url, data=body, headers=headers, method=method)
    try:
        with urlopen(request, timeout=timeout) as response:  # nosec B310 - operator URL
            return response.status, response.read(65_537)[:65_536]
    except Exception as exc:
        status = getattr(exc, "code", None)
        if isinstance(status, int):
            return status, b""
        raise


def _check_monitor_platform(config: Settings, http_probe: HttpProbe) -> DoctorCheck:
    if config.platform == "feishu":
        configured = all(
            (
                config.feishu_app_id,
                config.feishu_app_secret,
                config.feishu_verification_token,
                config.feishu_encrypt_key,
                config.feishu_callback_url.startswith("https://"),
            )
        ) and not config.feishu_use_websocket
        if not configured:
            return DoctorCheck(
                "feishu_callback",
                False,
                "error",
                "Feishu credentials or encrypted card callback are missing",
            )
        try:
            status, _ = http_probe(
                "POST",
                config.feishu_callback_url,
                b"{}",
                {"content-type": "application/json", "x-ostag-doctor": "invalid-signature"},
                5.0,
            )
            verified = status == 401
        except Exception:
            verified = False
        message = (
            "Feishu callback runtime verification passed"
            if verified
            else "Feishu callback runtime verification failed or is unreachable"
        )
        return DoctorCheck("feishu_callback", verified, "error", message)
    if config.platform == "telegram":
        configured = all(
            (
                config.telegram_bot_token,
                config.telegram_webhook_secret,
                config.telegram_webhook_url,
            )
        )
        verified = False
        if configured:
            try:
                status, body = http_probe(
                    "GET",
                    f"https://api.telegram.org/bot{config.telegram_bot_token}/getWebhookInfo",
                    None,
                    {},
                    5.0,
                )
                payload = json.loads(body)
                verified = (
                    status == 200
                    and payload.get("ok") is True
                    and payload.get("result", {}).get("url") == config.telegram_webhook_url
                )
            except Exception:
                verified = False
        ok = configured and verified
        message = "Telegram webhook is verified" if ok else "Telegram webhook is not verified"
        return DoctorCheck("telegram_webhook", ok, "error", message)
    return DoctorCheck(
        "monitor_platform", False, "error", "central monitor platform must be feishu or telegram"
    )


def _check_stale_instances(config: Settings) -> DoctorCheck:
    try:
        with sqlite3.connect(f"file:{config.monitor_server_db}?mode=ro", uri=True) as connection:
            rows = connection.execute(
                "SELECT instance_id,last_seen_at FROM instances ORDER BY instance_id"
            ).fetchall()
    except sqlite3.Error:
        return DoctorCheck("stale_instances", False, "error", "instance health cannot be read")
    now = datetime.now(UTC)
    stale = []
    for instance_id, last_seen_at in rows:
        try:
            seen = datetime.fromisoformat(last_seen_at) if last_seen_at else None
            if seen is None or (now - seen.astimezone(UTC)).total_seconds() > config.monitor_offline_seconds:
                stale.append(str(instance_id))
        except (TypeError, ValueError):
            stale.append(str(instance_id))
    if stale:
        return DoctorCheck(
            "stale_instances", False, "error", "stale instances: " + ", ".join(stale)
        )
    return DoctorCheck("stale_instances", True, "error", "all registered instances are fresh")
