from __future__ import annotations

import importlib.util
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from ostag.config import Settings


@dataclass(frozen=True)
class DoctorCheck:
    name: str
    ok: bool
    severity: str
    message: str


def run_doctor(config: Settings, event_path: Path | None = None) -> list[DoctorCheck]:
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
    else:
        return DoctorCheck("adapter", False, "error", f"unsupported live platform: {config.platform}")
    if missing:
        return DoctorCheck("adapter", False, "error", "missing live credentials: " + ", ".join(missing))
    if config.platform == "slack":
        if importlib.util.find_spec("slack_bolt") is None:
            return DoctorCheck("adapter", False, "error", "slack-bolt missing; run `uv sync --extra slack`")
        return DoctorCheck("adapter", True, "error", "slack live adapter dependencies found")
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
