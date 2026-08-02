"""Non-mutating connector readiness diagnostics."""

from __future__ import annotations

from dataclasses import dataclass
import shlex
import shutil
import subprocess
from collections.abc import Callable

from ostag.config import Settings
from ostag.core.capabilities import list_capabilities
from ostag.core.safety import scrub_environment


CheckStatus = str
Which = Callable[[str], str | None]


@dataclass(frozen=True)
class ProbeResult:
    ok: bool
    reason: str = ""


Probe = Callable[[tuple[str, ...], float], ProbeResult]


@dataclass(frozen=True)
class ConnectorCheck:
    id: str
    status: CheckStatus
    required: bool
    summary: str
    recovery_hint: str = ""

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "id": self.id,
            "status": self.status,
            "required": self.required,
            "summary": self.summary,
        }
        if self.recovery_hint:
            payload["recovery_hint"] = self.recovery_hint
        return payload


@dataclass(frozen=True)
class ConnectorDiagnosticReport:
    platform: str
    mode: str
    status: str
    read_ready: bool
    write_ready: bool
    target: dict[str, object]
    checks: tuple[ConnectorCheck, ...]
    capabilities: tuple[dict[str, object], ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "platform": self.platform,
            "mode": self.mode,
            "status": self.status,
            "read_ready": self.read_ready,
            "write_ready": self.write_ready,
            "target": self.target,
            "checks": [item.to_dict() for item in self.checks],
            "capabilities": list(self.capabilities),
        }


def _default_probe(argv: tuple[str, ...], timeout: float) -> ProbeResult:
    try:
        completed = subprocess.run(  # noqa: S603 - argv is assembled from trusted operator config
            argv,
            check=False,
            capture_output=True,
            env=scrub_environment(),
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ProbeResult(ok=False)
    return ProbeResult(ok=completed.returncode == 0)


def _configured_check(
    check_id: str,
    configured: bool,
    *,
    summary: str,
    recovery_hint: str,
) -> ConnectorCheck:
    return ConnectorCheck(
        id=check_id,
        status="pass" if configured else "fail",
        required=True,
        summary=summary if configured else f"{summary} missing",
        recovery_hint="" if configured else recovery_hint,
    )


def _command_name(command: str) -> str:
    try:
        parts = shlex.split(command)
    except ValueError:
        return ""
    return parts[0] if parts else ""


def _cli_check(command: str, *, which: Which, label: str) -> ConnectorCheck:
    executable = _command_name(command)
    available = bool(executable and which(executable))
    return ConnectorCheck(
        id="cli",
        status="pass" if available else "fail",
        required=True,
        summary=f"{label} executable available" if available else f"{label} executable missing",
        recovery_hint="" if available else f"Install {label} or configure its OStag binary setting.",
    )


def _target_check(target: dict[str, object], hint: str) -> ConnectorCheck:
    configured = target["configured"] is True
    return ConnectorCheck(
        id="target",
        status="pass" if configured else "fail",
        required=True,
        summary="default connector target configured" if configured else "default target missing",
        recovery_hint="" if configured else hint,
    )


def _live_command_check(
    check_id: str,
    argv: tuple[str, ...],
    *,
    probe: Probe,
    failure_summary: str,
) -> ConnectorCheck:
    result = probe(argv, 10.0)
    return ConnectorCheck(
        id=check_id,
        status="pass" if result.ok else "fail",
        required=True,
        summary=f"{check_id.replace('_', ' ')} passed" if result.ok else failure_summary,
        recovery_hint="" if result.ok else "Check provider authentication and target access, then retry.",
    )


def _supports_governed_write(capabilities: tuple[dict[str, object], ...]) -> bool:
    for item in capabilities:
        values = item.get("capabilities", {})
        if not isinstance(values, dict):
            continue
        if values.get("can_apply_action") is True or values.get("can_write_records") is True:
            return True
    return False


def connector_test(
    config: Settings,
    platform: str,
    *,
    live: bool = False,
    which: Which = shutil.which,
    probe: Probe = _default_probe,
) -> ConnectorDiagnosticReport:
    declared = tuple(item.to_dict() for item in list_capabilities(platform))
    if not declared:
        raise ValueError(f"unsupported connector platform: {platform}")

    checks: list[ConnectorCheck] = [
        ConnectorCheck("declaration", "pass", True, "connector capabilities declared")
    ]
    target: dict[str, object]

    if platform in {"feishu", "lark"}:
        app_id = config.feishu_app_id if platform == "feishu" else config.lark_app_id
        app_secret = (
            config.feishu_app_secret if platform == "feishu" else config.lark_app_secret
        )
        checks.append(
            _configured_check(
                "credentials",
                bool(app_id and app_secret),
                summary="application credentials configured",
                recovery_hint=(
                    f"Configure {platform.upper()}_APP_ID and {platform.upper()}_APP_SECRET."
                ),
            )
        )
        checks.append(_cli_check(config.lark_cli_bin, which=which, label="lark-cli"))
        target = {
            "kind": "base_table",
            "configured": bool(
                config.auto_base_default_base_token and config.auto_base_default_table_id
            ),
        }
        checks.append(
            _target_check(
                target,
                "Configure OSTAG_AUTO_BASE_DEFAULT_BASE_TOKEN and "
                "OSTAG_AUTO_BASE_DEFAULT_TABLE_ID.",
            )
        )
    elif platform == "dingtalk":
        checks.append(
            _configured_check(
                "credentials",
                bool(config.dingtalk_client_id and config.dingtalk_client_secret),
                summary="application credentials configured",
                recovery_hint="Configure DINGTALK_CLIENT_ID and DINGTALK_CLIENT_SECRET.",
            )
        )
        checks.append(_cli_check(config.dws_cli_bin, which=which, label="DingTalk workspace CLI"))
        target = {
            "kind": "ai_table",
            "configured": bool(config.daily_task_board_dingtalk_table_id),
        }
        checks.append(
            _target_check(
                target,
                "Configure OSTAG_DAILY_TASK_BOARD_DINGTALK_TABLE_ID.",
            )
        )
    elif platform == "github":
        checks.append(_cli_check(config.gh_bin, which=which, label="GitHub CLI"))
        target = {"kind": "repository", "configured": bool(config.github_default_repo)}
        checks.append(_target_check(target, "Configure OSTAG_GITHUB_DEFAULT_REPO."))
        if live and checks[-2].status == "pass" and checks[-1].status == "pass":
            checks.extend(
                (
                    _live_command_check(
                        "live_auth",
                        (config.gh_bin, "auth", "status", "--hostname", "github.com"),
                        probe=probe,
                        failure_summary="live authentication probe failed",
                    ),
                    _live_command_check(
                        "live_target",
                        (
                            config.gh_bin,
                            "repo",
                            "view",
                            config.github_default_repo,
                            "--json",
                            "nameWithOwner",
                        ),
                        probe=probe,
                        failure_summary="live target probe failed",
                    ),
                )
            )
    elif platform == "gitlab":
        checks.append(_cli_check(config.glab_bin, which=which, label="GitLab CLI"))
        target = {"kind": "repository", "configured": bool(config.gitlab_default_repo)}
        checks.append(_target_check(target, "Configure OSTAG_GITLAB_DEFAULT_REPO."))
        if live and checks[-2].status == "pass" and checks[-1].status == "pass":
            checks.extend(
                (
                    _live_command_check(
                        "live_auth",
                        (config.glab_bin, "auth", "status"),
                        probe=probe,
                        failure_summary="live authentication probe failed",
                    ),
                    _live_command_check(
                        "live_target",
                        (config.glab_bin, "repo", "view", config.gitlab_default_repo),
                        probe=probe,
                        failure_summary="live target probe failed",
                    ),
                )
            )
    else:
        raise ValueError(f"unsupported connector platform: {platform}")

    if live and platform in {"feishu", "lark", "dingtalk"}:
        checks.append(
            ConnectorCheck(
                "live_provider",
                "warn",
                False,
                "provider-live read probe is delegated to the credential-gated smoke harness",
                "Run scripts/connector_live_smoke.sh for a bounded provider API check.",
            )
        )

    read_ready = not any(item.required and item.status == "fail" for item in checks)
    has_warning = any(item.status == "warn" for item in checks)
    status = "blocked" if not read_ready else ("degraded" if has_warning else "ready")
    write_ready = bool(
        read_ready
        and _supports_governed_write(declared)
        and config.governed_writes
        and config.apply_enabled
    )
    checks.append(
        ConnectorCheck(
            "governed_write",
            "pass" if write_ready else "warn",
            False,
            "governed connector writes enabled" if write_ready else "connector writes disabled",
            "Enable both OSTAG_GOVERNED_WRITES and OSTAG_APPLY_ENABLED after approval."
            if not write_ready
            else "",
        )
    )
    return ConnectorDiagnosticReport(
        platform=platform,
        mode="live" if live else "local",
        status=status,
        read_ready=read_ready,
        write_ready=write_ready,
        target=target,
        checks=tuple(checks),
        capabilities=declared,
    )
