from __future__ import annotations

import os
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None


Platform = Literal["slack", "feishu", "lark", "dingtalk", "telegram"]
BackendName = Literal["mock", "codex_cli", "claude_cli", "kimi_cli", "openai_api"]

# Canonical registry: these flags have stricter parsing than legacy settings.
BRIEF_FLAGS = (
    ("brief_enabled", "OSTAG_BRIEF_ENABLED"),
    ("brief_meetings_enabled", "OSTAG_BRIEF_MEETINGS_ENABLED"),
    ("brief_history_enabled", "OSTAG_BRIEF_HISTORY_ENABLED"),
    ("brief_actions_enabled", "OSTAG_BRIEF_ACTIONS_ENABLED"),
    ("brief_routines_enabled", "OSTAG_BRIEF_ROUTINES_ENABLED"),
    ("brief_sales_enabled", "OSTAG_BRIEF_SALES_ENABLED"),
)


def _managed_ledger_paths():
    from ostag.core.ledger_factory import parse_managed_paths
    return parse_managed_paths(os.getenv('OSTAG_MANAGED_LEDGER_PATHS', '[]'))


def _brief_bool(name: str) -> bool:
    value = os.getenv(name, "false").strip().lower()
    if value not in {"true", "1", "yes", "on", "false", "0", "no", "off"}:
        raise ValueError(f"{name} must be a boolean")
    return value in {"true", "1", "yes", "on"}


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return int(value)


def _csv(name: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in os.getenv(name, "").split(",") if item.strip())


def _json_object(name: str) -> dict[str, object]:
    value = json.loads(os.getenv(name, "{}"))
    if not isinstance(value, dict) or not all(isinstance(key, str) and key for key in value):
        raise ValueError(f"{name} must be a JSON object with non-empty string keys")
    return value


def _instance_secrets() -> tuple[tuple[str, str], ...]:
    value = _json_object("OSTAG_MONITOR_INSTANCE_SECRETS")
    if not all(isinstance(secret, str) and secret for secret in value.values()):
        raise ValueError("OSTAG_MONITOR_INSTANCE_SECRETS values must be non-empty strings")
    return tuple((key, secret) for key, secret in value.items())  # type: ignore[misc]


def _human_credentials() -> tuple[tuple[str, str, str, str, str], ...]:
    value = _json_object("OSTAG_MONITOR_HUMAN_CREDENTIALS")
    required = {"platform", "actor_id", "destination_id", "chat_type"}
    records = []
    for token, credential in value.items():
        if not isinstance(credential, dict) or set(credential) != required:
            raise ValueError(
                "human credentials require exactly platform, actor_id, destination_id, chat_type"
            )
        fields = tuple(
            credential[key] for key in ("platform", "actor_id", "destination_id", "chat_type")
        )
        if not all(isinstance(field, str) and field for field in fields) or fields[-1] not in {
            "private",
            "group",
        }:
            raise ValueError("human credential fields are invalid")
        records.append((token, *fields))
    return tuple(records)  # type: ignore[return-value]


@dataclass(frozen=True)
class Settings:
    env: str = "dev"
    log_level: str = "INFO"
    dry_run: bool = True
    bot_name: str = "OS Tag"
    platform: Platform = "feishu"
    backend: BackendName = "mock"
    agent_timeout_seconds: int = 120
    max_input_chars: int = 12000
    max_context_chars: int = 30000
    allow_shell: bool = False
    audit_log: Path = Path("./logs/audit.log")
    run_ledger_path: Path = Path("./logs/run_ledger.jsonl")
    managed_ledger_paths: tuple[Path, ...] = ()
    actor_permissions: tuple[str, ...] = ()
    default_permission_level: str = "propose"
    apply_enabled: bool = False
    receipt_ttl_seconds: int = 86400
    approval_quorum_high_risk: int = 1
    four_eyes_enabled: bool = False
    gh_bin: str = "gh"
    github_default_repo: str = ""
    github_vercel_release_enabled: bool = False
    github_vercel_release_repository: str = ""
    github_vercel_release_channels: tuple[str, ...] = ()
    github_vercel_release_base_branch: str = "main"
    github_vercel_required_checks: tuple[str, ...] = ()
    vercel_access_token: str = ""
    vercel_team_id: str = ""
    vercel_project_id: str = ""
    vercel_project_name: str = ""
    vercel_deploy_timeout_seconds: int = 600
    vercel_poll_interval_seconds: int = 2
    glab_bin: str = "glab"
    gitlab_default_repo: str = ""
    run_cost_credits: int = 0
    daily_cost_budget: int = 0
    governed_writes: bool = False
    followup_enabled: bool = True
    followup_after_seconds: int = 14400
    query_lane_enabled: bool = False
    query_lane_channels: tuple[str, ...] = ()
    query_lane_max_concurrency: int = 4
    query_lane_timeout_seconds: int = 10
    query_lane_retry_count: int = 1
    query_lane_max_result_bytes: int = 65_536
    native_diagnostic_cards_enabled: bool = False
    workstreams_enabled: bool = False
    workstream_store_path: Path = Path("./logs/workstreams.sqlite3")
    workstream_max_batch: int = 25
    provider_evidence_enabled: bool = False
    provider_evidence_max_checks: int = 100
    executor_runtime_enabled: bool = False
    kimi_executor_enabled: bool = False
    executor_max_output_bytes: int = 1_048_576
    executor_osstack_registered: tuple[str, ...] = ()
    env_scrub_extra: tuple[str, ...] = ()
    mfs_enabled: bool = False
    mfs_bin: str = "mfs"
    mfs_server_url: str = "http://127.0.0.1:8000"
    mfs_allowed_scopes: tuple[str, ...] = ("file://./context",)
    mfs_max_results: int = 8
    codex_bin: str = "codex"
    claude_bin: str = "claude"
    kimi_bin: str = "kimi"
    codex_unsafe_mode: bool = False
    claude_unsafe_mode: bool = False
    openai_model: str = "gpt-5.5"
    feishu_app_id: str = ""
    feishu_app_secret: str = ""
    feishu_verification_token: str = ""
    feishu_encrypt_key: str = ""
    feishu_use_websocket: bool = True
    feishu_api_base_url: str = "https://open.feishu.cn"
    feishu_callback_url: str = ""
    lark_app_id: str = ""
    lark_app_secret: str = ""
    lark_verification_token: str = ""
    lark_encrypt_key: str = ""
    lark_use_websocket: bool = True
    lark_api_base_url: str = "https://open.larksuite.com"
    slack_bot_token: str = ""
    slack_app_token: str = ""
    dingtalk_client_id: str = ""
    dingtalk_client_secret: str = ""
    dingtalk_api_base_url: str = "https://api.dingtalk.com"
    dingtalk_robot_webhook: str = ""
    dingtalk_robot_secret: str = ""
    telegram_bot_token: str = ""
    telegram_webhook_secret: str = ""
    telegram_webhook_url: str = ""
    telegram_group_allowlist: tuple[str, ...] = ()
    telegram_admin_ids: tuple[str, ...] = ()
    auto_base_enabled: bool = True
    auto_base_identity: str = "user"
    auto_base_history_page_size: int = 30
    auto_base_cli_timeout_seconds: int = 120
    auto_base_create_table_if_missing: bool = True
    auto_base_default_table_name: str = "需求池"
    auto_base_default_base_token: str = ""
    auto_base_default_table_id: str = ""
    auto_base_download_dir: str = "tmp/ostag-auto-base"
    auto_base_workdir: str = "."
    lark_cli_bin: str = "lark-cli"
    dws_cli_bin: str = "npx -y dingtalk-workspace-cli"
    ambient_enabled: bool = False
    ambient_interval_seconds: int = 1800
    ambient_chat_ids: tuple[str, ...] = ()
    ambient_record_limit: int = 50
    ambient_daily_digest_enabled: bool = False
    ambient_morning_digest_hour: int = 9
    ambient_evening_digest_hour: int = 18
    ambient_digest_window_minutes: int = 90
    ambient_manager_open_ids: tuple[str, ...] = ()
    ambient_stale_days: int = 7
    ambient_suppress_days: int = 3
    ambient_max_decisions: int = 3
    ambient_daily_board_enabled: bool = False
    ambient_daily_board_hour: int = 12
    ambient_daily_board_window_minutes: int = 60
    ambient_timezone: str = "Asia/Shanghai"
    ambient_daily_board_group_name: str = "Dadao 投研交易 FDE"
    ambient_daily_board_base_url: str = ""
    monitor_enabled: bool = False
    monitor_instance_id: str = "local"
    monitor_instance_secret: str = ""
    monitor_enrollment_secret: str = ""
    monitor_central_url: str = ""
    monitor_spool_db: Path = Path("./logs/work_monitor_spool.sqlite3")
    monitor_report_interval_seconds: int = 30
    monitor_command_poll_seconds: int = 10
    monitor_osstack_bin: str = "osstack"
    monitor_osstack_references: Path = Path("./logs/osstack_task_references.json")
    monitor_server_db: Path = Path("/var/lib/ostag/work_monitor.sqlite3")
    monitor_admin_token: str = ""
    monitor_instance_secrets: tuple[tuple[str, str], ...] = ()
    monitor_human_credentials: tuple[tuple[str, str, str, str, str], ...] = ()
    monitor_server_max_body_bytes: int = 1_048_576
    monitor_scheduler_enabled: bool = True
    monitor_morning_digest_hour: int = 9
    monitor_evening_digest_hour: int = 19
    monitor_degraded_seconds: int = 120
    monitor_offline_seconds: int = 600
    monitor_notification_retry_cap: int = 5
    operational_report_enabled: bool = False
    operational_report_connector_evidence_dir: Path = Path("./release")
    operational_report_max_items: int = 100
    copilot_relay_enabled: bool = False
    copilot_relay_server_enabled: bool = True
    copilot_relay_write_enabled: bool = True
    copilot_status_board_enabled: bool = False
    copilot_relay_canary_session_ids: tuple[str, ...] = ()
    copilot_relay_instance_id: str = "local"
    copilot_relay_secret: str = ""
    copilot_relay_central_url: str = ""
    copilot_relay_spool_db: Path = Path("./logs/copilot_relay_spool.sqlite3")
    copilot_relay_osstack_bin: str = "osstack"
    copilot_relay_default_repo: str = ""
    copilot_relay_poll_interval_seconds: int = 5
    copilot_relay_max_text_chars: int = 4_000
    device_identity_file: Path = (
        Path.home() / "Library/Application Support/OStag/device-identity.json"
    )
    device_display_name: str = "OStag Device"
    daily_task_boards_enabled: bool = False
    daily_task_board_poll_seconds: int = 30
    daily_task_board_morning_hour: int = 9
    daily_task_board_evening_hour: int = 18
    daily_task_board_record_limit: int = 50
    daily_task_board_feishu_chat_id: str = ""
    daily_task_board_feishu_base_token: str = ""
    daily_task_board_feishu_table_id: str = ""
    daily_task_board_feishu_table_url: str = ""
    daily_task_board_feishu_manager_ids: tuple[str, ...] = ()
    daily_task_board_dingtalk_chat_id: str = ""
    daily_task_board_dingtalk_base_token: str = ""
    daily_task_board_dingtalk_table_id: str = ""
    daily_task_board_dingtalk_table_url: str = ""
    daily_task_board_dingtalk_manager_ids: tuple[str, ...] = ()

    brief_enabled: bool = False
    brief_meetings_enabled: bool = False
    brief_history_enabled: bool = False
    brief_actions_enabled: bool = False
    brief_routines_enabled: bool = False
    brief_sales_enabled: bool = False
    brief_status_dir: Path = Path("/run/ostag-brief-status")
    brief_control_path: Path = Path("/run/ostag-brief-control/control.json")

    def validate(self) -> Settings:
        from ostag.core.ledger_factory import validate_managed_paths
        validate_managed_paths(self.managed_ledger_paths)
        for field, env in BRIEF_FLAGS:
            value = getattr(self, field)
            if type(value) is not bool:
                raise ValueError(f"{env} must be a boolean")
            if value and field not in ('brief_enabled', 'brief_meetings_enabled'):
                raise ValueError(f"{env} is not ready: trusted Brief runtime is unsupported")
        if self.brief_meetings_enabled and not self.brief_enabled:
            raise ValueError("OSTAG_BRIEF_MEETINGS_ENABLED is not ready without BRIEF_ENABLED")
        if not isinstance(self.brief_status_dir, Path) or not isinstance(
            self.brief_control_path, Path
        ):
            raise ValueError("Brief status and control paths must be Path values")
        if not 1 <= self.agent_timeout_seconds <= 3600:
            raise ValueError("OSTAG_AGENT_TIMEOUT_SECONDS must be in 1..3600")
        if self.query_lane_max_concurrency < 1:
            raise ValueError("OSTAG_QUERY_LANE_MAX_CONCURRENCY must be positive")
        if self.query_lane_timeout_seconds < 1:
            raise ValueError("OSTAG_QUERY_LANE_TIMEOUT_SECONDS must be positive")
        if self.query_lane_retry_count < 0:
            raise ValueError("OSTAG_QUERY_LANE_RETRY_COUNT cannot be negative")
        if self.query_lane_max_result_bytes < 1:
            raise ValueError("OSTAG_QUERY_LANE_MAX_RESULT_BYTES must be positive")
        if not 1 <= self.workstream_max_batch <= 100:
            raise ValueError("OSTAG_WORKSTREAM_MAX_BATCH must be in 1..100")
        if not 1 <= self.provider_evidence_max_checks <= 500:
            raise ValueError("OSTAG_PROVIDER_EVIDENCE_MAX_CHECKS must be in 1..500")
        if not 1_024 <= self.executor_max_output_bytes <= 16_777_216:
            raise ValueError("OSTAG_EXECUTOR_MAX_OUTPUT_BYTES must be in 1024..16777216")
        if any(
            item not in {"codex_cli", "claude_cli", "kimi_cli"}
            for item in self.executor_osstack_registered
        ):
            raise ValueError("OSTAG_EXECUTOR_OSSTACK_REGISTERED contains an unknown executor")
        if any(not item or ":" not in item for item in self.query_lane_channels):
            raise ValueError("OSTAG_QUERY_LANE_CHANNELS entries must use platform:chat_id")
        if self.github_vercel_release_enabled:
            required = {
                "github_vercel_release_repository": self.github_vercel_release_repository,
                "github_vercel_release_channels": self.github_vercel_release_channels,
                "github_vercel_required_checks": self.github_vercel_required_checks,
                "vercel_access_token": self.vercel_access_token,
                "vercel_team_id": self.vercel_team_id,
                "vercel_project_id": self.vercel_project_id,
                "vercel_project_name": self.vercel_project_name,
            }
            missing = next((name for name, value in required.items() if not value), "")
            if missing:
                raise ValueError(f"GitHub/Vercel release requires {missing}")
            repository_pattern = r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*"
            if not re.fullmatch(repository_pattern, self.github_vercel_release_repository):
                raise ValueError(
                    "github_vercel_release_repository must use GitHub owner/name syntax"
                )
            base_branch = self.github_vercel_release_base_branch
            if (
                not isinstance(base_branch, str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,254}", base_branch)
                or ".." in base_branch
                or "//" in base_branch
                or base_branch.endswith((".", "/"))
            ):
                raise ValueError("github_vercel_release_base_branch is invalid")
            if not isinstance(self.github_vercel_required_checks, tuple) or not all(
                isinstance(check, str) for check in self.github_vercel_required_checks
            ):
                raise ValueError("github_vercel_required_checks must be a tuple of names")
            if len(set(self.github_vercel_required_checks)) != len(
                self.github_vercel_required_checks
            ):
                raise ValueError("github_vercel_required_checks must be unique")
            if any(
                not check or len(check) > 200 or any(ord(char) < 32 for char in check)
                for check in self.github_vercel_required_checks
            ):
                raise ValueError("github_vercel_required_checks contains an invalid name")
            allowed_platforms = {"slack", "feishu", "lark", "dingtalk", "telegram"}
            for channel in self.github_vercel_release_channels:
                platform, separator, chat_id = channel.partition(":")
                if not separator or not chat_id:
                    raise ValueError(
                        "github_vercel_release_channels entries must use platform:chat_id"
                    )
                if platform not in allowed_platforms:
                    raise ValueError(
                        "github_vercel_release_channels contains an unknown platform"
                    )
            project_pattern = r"[a-z0-9](?:[a-z0-9._-]{0,98}[a-z0-9])?"
            if not re.fullmatch(project_pattern, self.vercel_project_name):
                raise ValueError("vercel_project_name must be a lowercase Vercel project name")
            if not 30 <= self.vercel_deploy_timeout_seconds <= 3600:
                raise ValueError("vercel_deploy_timeout_seconds must be in 30..3600")
            if not 1 <= self.vercel_poll_interval_seconds <= 60:
                raise ValueError("vercel_poll_interval_seconds must be in 1..60")
        if not 0 <= self.ambient_daily_board_hour <= 23:
            raise ValueError("OSTAG_AMBIENT_DAILY_BOARD_HOUR must be between 0 and 23")
        if self.ambient_daily_board_window_minutes <= 0:
            raise ValueError("OSTAG_AMBIENT_DAILY_BOARD_WINDOW_MINUTES must be positive")
        try:
            ZoneInfo(self.ambient_timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"Invalid OSTAG_AMBIENT_TIMEZONE: {self.ambient_timezone}") from exc
        if self.ambient_daily_board_enabled:
            if len(self.ambient_chat_ids) != 1:
                raise ValueError("daily requirement board requires exactly one ambient chat")
            required = {
                "auto_base_default_base_token": self.auto_base_default_base_token,
                "auto_base_default_table_id": self.auto_base_default_table_id,
                "ambient_daily_board_base_url": self.ambient_daily_board_base_url,
                "feishu_app_id": self.feishu_app_id,
                "feishu_app_secret": self.feishu_app_secret,
            }
            missing = next((name for name, value in required.items() if not value), "")
            if missing:
                raise ValueError(f"daily requirement board requires {missing}")
            if not self.ambient_daily_board_base_url.startswith("https://"):
                raise ValueError("daily requirement board URL must use HTTPS")
            if not 1 <= self.ambient_record_limit <= 500:
                raise ValueError("daily requirement board record limit must be in 1..500")
        if (
            not 0 <= self.monitor_morning_digest_hour <= 23
            or not 0 <= self.monitor_evening_digest_hour <= 23
        ):
            raise ValueError("monitor digest hours must be in 0..23")
        if self.monitor_morning_digest_hour == self.monitor_evening_digest_hour:
            raise ValueError("monitor digest hours must be different")
        if not 0 < self.monitor_degraded_seconds < self.monitor_offline_seconds:
            raise ValueError("monitor thresholds require 0 < degraded < offline")
        if self.monitor_notification_retry_cap <= 0:
            raise ValueError("monitor notification retry cap must be positive")
        if not 1 <= self.operational_report_max_items <= 500:
            raise ValueError("OSTAG_OPERATIONAL_REPORT_MAX_ITEMS must be in 1..500")
        if self.copilot_relay_enabled:
            required = {
                "copilot_relay_instance_id": self.copilot_relay_instance_id,
                "copilot_relay_central_url": self.copilot_relay_central_url,
                "copilot_relay_osstack_bin": self.copilot_relay_osstack_bin,
            }
            missing = next((name for name, value in required.items() if not value), "")
            if missing:
                raise ValueError(f"copilot relay requires {missing}")
            if not self.copilot_relay_secret and not self.device_identity_file.is_file():
                raise ValueError(
                    "copilot relay requires copilot_relay_secret or device identity"
                )
            if self.copilot_relay_default_repo and not Path(self.copilot_relay_default_repo).is_absolute():
                raise ValueError("copilot relay default repository must be absolute")
            url = self.copilot_relay_central_url
            local_http = url.startswith(("http://127.0.0.1", "http://localhost", "http://[::1]"))
            if not url.startswith("https://") and not local_http:
                raise ValueError("copilot relay central URL must use HTTPS outside localhost")
            if self.env.casefold() in {"prod", "production"} and not url.startswith("https://"):
                raise ValueError("copilot relay production central URL must use HTTPS")
            if self.copilot_relay_poll_interval_seconds < 1:
                raise ValueError("copilot relay poll interval must be positive")
            if not 1 <= self.copilot_relay_max_text_chars <= 16_000:
                raise ValueError("copilot relay max text must be in 1..16000")
        if self.daily_task_boards_enabled:
            if (
                not 0 <= self.daily_task_board_morning_hour <= 23
                or not 0 <= self.daily_task_board_evening_hour <= 23
            ):
                raise ValueError("daily task board hours must be in 0..23")
            if self.daily_task_board_morning_hour == self.daily_task_board_evening_hour:
                raise ValueError("daily task board hours must be different")
            if self.daily_task_board_poll_seconds < 5:
                raise ValueError("daily task board poll seconds must be at least 5")
            if not 1 <= self.daily_task_board_record_limit <= 500:
                raise ValueError("daily task board record limit must be in 1..500")
            required = {
                "daily_task_board_feishu_chat_id": self.daily_task_board_feishu_chat_id,
                "daily_task_board_feishu_base_token": self.daily_task_board_feishu_base_token,
                "daily_task_board_feishu_table_id": self.daily_task_board_feishu_table_id,
                "daily_task_board_feishu_table_url": self.daily_task_board_feishu_table_url,
                "daily_task_board_dingtalk_chat_id": self.daily_task_board_dingtalk_chat_id,
                "daily_task_board_dingtalk_base_token": self.daily_task_board_dingtalk_base_token,
                "daily_task_board_dingtalk_table_id": self.daily_task_board_dingtalk_table_id,
                "daily_task_board_dingtalk_table_url": self.daily_task_board_dingtalk_table_url,
                "feishu_app_id": self.feishu_app_id,
                "feishu_app_secret": self.feishu_app_secret,
                "dingtalk_client_id": self.dingtalk_client_id,
                "dingtalk_client_secret": self.dingtalk_client_secret,
            }
            missing = next((name for name, value in required.items() if not value), "")
            if missing:
                raise ValueError(f"daily task boards require {missing}")
        telegram_configured = any(
            (self.telegram_bot_token, self.telegram_webhook_secret, self.telegram_webhook_url)
        )
        if self.env.casefold() in {"prod", "production"} and (
            self.platform == "telegram" or telegram_configured
        ):
            if not all(
                (self.telegram_bot_token, self.telegram_webhook_secret, self.telegram_webhook_url)
            ):
                raise ValueError("Telegram production requires token, secret, and webhook URL")
            if not self.telegram_webhook_url.startswith("https://"):
                raise ValueError("Telegram production webhook URL must use HTTPS")
        return self

    def monitor_human_credentials_dict(self):
        from ostag.core.identity import Principal
        from ostag.work_monitor.identity import Destination
        from ostag.work_monitor.server import HumanCredential

        return {
            token: HumanCredential(
                Principal(platform, actor), Destination(platform, destination, chat_type)
            )
            for token, platform, actor, destination, chat_type in self.monitor_human_credentials
        }
def load_settings(config_path: str | None = None, **overrides: object) -> Settings:
    if load_dotenv:
        load_dotenv(config_path or ".env", override=False)

    scopes = tuple(
        scope.strip()
        for scope in os.getenv("MFS_ALLOWED_SCOPES", "file://./context").split(",")
        if scope.strip()
    )
    settings = Settings(
        **{field: _brief_bool(env) for field, env in BRIEF_FLAGS},
        brief_status_dir=Path(os.getenv("OSTAG_BRIEF_STATUS_DIR", "/run/ostag-brief-status")),
        brief_control_path=Path(
            os.getenv("OSTAG_BRIEF_CONTROL_PATH", "/run/ostag-brief-control/control.json")
        ),
        env=os.getenv("OSTAG_ENV", "dev"),
        log_level=os.getenv("OSTAG_LOG_LEVEL", "INFO"),
        dry_run=_bool("OSTAG_DRY_RUN", True),
        bot_name=os.getenv("OSTAG_BOT_NAME", "OS Tag").strip('"'),
        platform=os.getenv("OSTAG_PLATFORM", "feishu"),  # type: ignore[arg-type]
        backend=os.getenv("OSTAG_BACKEND", "mock"),  # type: ignore[arg-type]
        agent_timeout_seconds=_int("OSTAG_AGENT_TIMEOUT_SECONDS", 120),
        max_input_chars=_int("OSTAG_MAX_INPUT_CHARS", 12000),
        max_context_chars=_int("OSTAG_MAX_CONTEXT_CHARS", 30000),
        allow_shell=_bool("OSTAG_ALLOW_SHELL", False),
        audit_log=Path(os.getenv("OSTAG_AUDIT_LOG", "./logs/audit.log")),
        run_ledger_path=Path(os.getenv("OSTAG_RUN_LEDGER_PATH", "./logs/run_ledger.jsonl")),
        managed_ledger_paths=_managed_ledger_paths(),
        actor_permissions=_csv("OSTAG_ACTOR_PERMISSIONS"),
        default_permission_level=os.getenv("OSTAG_DEFAULT_PERMISSION_LEVEL", "propose"),
        apply_enabled=_bool("OSTAG_APPLY_ENABLED", False),
        receipt_ttl_seconds=_int("OSTAG_RECEIPT_TTL_SECONDS", 86400),
        approval_quorum_high_risk=_int("OSTAG_APPROVAL_QUORUM_HIGH_RISK", 1),
        four_eyes_enabled=_bool("OSTAG_FOUR_EYES_ENABLED", False),
        gh_bin=os.getenv("OSTAG_GH_BIN", "gh"),
        github_default_repo=os.getenv("OSTAG_GITHUB_DEFAULT_REPO", ""),
        github_vercel_release_enabled=_bool(
            "OSTAG_GITHUB_VERCEL_RELEASE_ENABLED", False
        ),
        github_vercel_release_repository=os.getenv(
            "OSTAG_GITHUB_VERCEL_RELEASE_REPOSITORY", ""
        ),
        github_vercel_release_channels=_csv("OSTAG_GITHUB_VERCEL_RELEASE_CHANNELS"),
        github_vercel_release_base_branch=os.getenv(
            "OSTAG_GITHUB_VERCEL_RELEASE_BASE_BRANCH", "main"
        ),
        github_vercel_required_checks=_csv("OSTAG_GITHUB_VERCEL_REQUIRED_CHECKS"),
        vercel_access_token=os.getenv("OSTAG_VERCEL_ACCESS_TOKEN", ""),
        vercel_team_id=os.getenv("OSTAG_VERCEL_TEAM_ID", ""),
        vercel_project_id=os.getenv("OSTAG_VERCEL_PROJECT_ID", ""),
        vercel_project_name=os.getenv("OSTAG_VERCEL_PROJECT_NAME", ""),
        vercel_deploy_timeout_seconds=_int("OSTAG_VERCEL_DEPLOY_TIMEOUT_SECONDS", 600),
        vercel_poll_interval_seconds=_int("OSTAG_VERCEL_POLL_INTERVAL_SECONDS", 2),
        glab_bin=os.getenv("OSTAG_GLAB_BIN", "glab"),
        gitlab_default_repo=os.getenv("OSTAG_GITLAB_DEFAULT_REPO", ""),
        run_cost_credits=_int("OSTAG_RUN_COST_CREDITS", 0),
        daily_cost_budget=_int("OSTAG_DAILY_COST_BUDGET", 0),
        governed_writes=_bool("OSTAG_GOVERNED_WRITES", False),
        followup_enabled=_bool("OSTAG_FOLLOWUP_ENABLED", True),
        followup_after_seconds=_int("OSTAG_FOLLOWUP_AFTER_SECONDS", 14400),
        query_lane_enabled=_bool("OSTAG_QUERY_LANE_ENABLED", False),
        query_lane_channels=_csv("OSTAG_QUERY_LANE_CHANNELS"),
        query_lane_max_concurrency=_int("OSTAG_QUERY_LANE_MAX_CONCURRENCY", 4),
        query_lane_timeout_seconds=_int("OSTAG_QUERY_LANE_TIMEOUT_SECONDS", 10),
        query_lane_retry_count=_int("OSTAG_QUERY_LANE_RETRY_COUNT", 1),
        query_lane_max_result_bytes=_int("OSTAG_QUERY_LANE_MAX_RESULT_BYTES", 65_536),
        native_diagnostic_cards_enabled=_bool(
            "OSTAG_NATIVE_DIAGNOSTIC_CARDS_ENABLED", False
        ),
        workstreams_enabled=_bool("OSTAG_WORKSTREAMS_ENABLED", False),
        workstream_store_path=Path(
            os.getenv("OSTAG_WORKSTREAM_STORE_PATH", "./logs/workstreams.sqlite3")
        ),
        workstream_max_batch=_int("OSTAG_WORKSTREAM_MAX_BATCH", 25),
        provider_evidence_enabled=_bool("OSTAG_PROVIDER_EVIDENCE_ENABLED", False),
        provider_evidence_max_checks=_int("OSTAG_PROVIDER_EVIDENCE_MAX_CHECKS", 100),
        executor_runtime_enabled=_bool("OSTAG_EXECUTOR_RUNTIME_ENABLED", False),
        kimi_executor_enabled=_bool("OSTAG_KIMI_EXECUTOR_ENABLED", False),
        executor_max_output_bytes=_int("OSTAG_EXECUTOR_MAX_OUTPUT_BYTES", 1_048_576),
        executor_osstack_registered=_csv("OSTAG_EXECUTOR_OSSTACK_REGISTERED"),
        env_scrub_extra=_csv("OSTAG_ENV_SCRUB_EXTRA"),
        mfs_enabled=_bool("MFS_ENABLED", False),
        mfs_bin=os.getenv("MFS_BIN", "mfs"),
        mfs_server_url=os.getenv("MFS_SERVER_URL", "http://127.0.0.1:8000"),
        mfs_allowed_scopes=scopes,
        mfs_max_results=_int("MFS_MAX_RESULTS", 8),
        codex_bin=os.getenv("CODEX_BIN", "codex"),
        claude_bin=os.getenv("CLAUDE_BIN", "claude"),
        kimi_bin=os.getenv("KIMI_BIN", "kimi"),
        codex_unsafe_mode=_bool("OSTAG_CODEX_UNSAFE_MODE", False),
        claude_unsafe_mode=_bool("OSTAG_CLAUDE_UNSAFE_MODE", False),
        openai_model=os.getenv("OSTAG_OPENAI_MODEL", "gpt-5.5"),
        feishu_app_id=os.getenv("FEISHU_APP_ID", ""),
        feishu_app_secret=os.getenv("FEISHU_APP_SECRET", ""),
        feishu_verification_token=os.getenv("FEISHU_VERIFICATION_TOKEN", ""),
        feishu_encrypt_key=os.getenv("FEISHU_ENCRYPT_KEY", ""),
        feishu_use_websocket=_bool("FEISHU_USE_WEBSOCKET", True),
        feishu_api_base_url=os.getenv("FEISHU_API_BASE_URL", "https://open.feishu.cn"),
        feishu_callback_url=os.getenv("FEISHU_CALLBACK_URL", ""),
        lark_app_id=os.getenv("LARK_APP_ID", ""),
        lark_app_secret=os.getenv("LARK_APP_SECRET", ""),
        lark_verification_token=os.getenv("LARK_VERIFICATION_TOKEN", ""),
        lark_encrypt_key=os.getenv("LARK_ENCRYPT_KEY", ""),
        lark_use_websocket=_bool("LARK_USE_WEBSOCKET", True),
        lark_api_base_url=os.getenv("LARK_API_BASE_URL", "https://open.larksuite.com"),
        slack_bot_token=os.getenv("SLACK_BOT_TOKEN", ""),
        slack_app_token=os.getenv("SLACK_APP_TOKEN", ""),
        dingtalk_client_id=os.getenv("DINGTALK_CLIENT_ID", ""),
        dingtalk_client_secret=os.getenv("DINGTALK_CLIENT_SECRET", ""),
        dingtalk_api_base_url=os.getenv("DINGTALK_API_BASE_URL", "https://api.dingtalk.com"),
        dingtalk_robot_webhook=os.getenv("DINGTALK_ROBOT_WEBHOOK", ""),
        dingtalk_robot_secret=os.getenv("DINGTALK_ROBOT_SECRET", ""),
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
        telegram_webhook_secret=os.getenv("TELEGRAM_WEBHOOK_SECRET", ""),
        telegram_webhook_url=os.getenv("TELEGRAM_WEBHOOK_URL", ""),
        telegram_group_allowlist=_csv("TELEGRAM_GROUP_ALLOWLIST"),
        telegram_admin_ids=_csv("TELEGRAM_ADMIN_IDS"),
        auto_base_enabled=_bool("OSTAG_AUTO_BASE_ENABLED", True),
        auto_base_identity=os.getenv("OSTAG_AUTO_BASE_IDENTITY", "user"),
        auto_base_history_page_size=_int("OSTAG_AUTO_BASE_HISTORY_PAGE_SIZE", 30),
        auto_base_cli_timeout_seconds=_int("OSTAG_AUTO_BASE_CLI_TIMEOUT_SECONDS", 120),
        auto_base_create_table_if_missing=_bool("OSTAG_AUTO_BASE_CREATE_TABLE_IF_MISSING", True),
        auto_base_default_table_name=os.getenv("OSTAG_AUTO_BASE_DEFAULT_TABLE_NAME", "需求池"),
        auto_base_default_base_token=os.getenv("OSTAG_AUTO_BASE_DEFAULT_BASE_TOKEN", ""),
        auto_base_default_table_id=os.getenv("OSTAG_AUTO_BASE_DEFAULT_TABLE_ID", ""),
        auto_base_download_dir=os.getenv("OSTAG_AUTO_BASE_DOWNLOAD_DIR", "tmp/ostag-auto-base"),
        auto_base_workdir=os.getenv("OSTAG_AUTO_BASE_WORKDIR", "."),
        lark_cli_bin=os.getenv("LARK_CLI_BIN", "lark-cli"),
        dws_cli_bin=os.getenv("DWS_CLI_BIN", "npx -y dingtalk-workspace-cli"),
        ambient_enabled=_bool("OSTAG_AMBIENT_ENABLED", False),
        ambient_interval_seconds=_int("OSTAG_AMBIENT_INTERVAL_SECONDS", 1800),
        ambient_chat_ids=tuple(
            item.strip()
            for item in os.getenv("OSTAG_AMBIENT_CHAT_IDS", "").split(",")
            if item.strip()
        ),
        ambient_record_limit=_int("OSTAG_AMBIENT_RECORD_LIMIT", 50),
        ambient_daily_digest_enabled=_bool("OSTAG_AMBIENT_DAILY_DIGEST_ENABLED", False),
        ambient_morning_digest_hour=_int("OSTAG_AMBIENT_MORNING_DIGEST_HOUR", 9),
        ambient_evening_digest_hour=_int("OSTAG_AMBIENT_EVENING_DIGEST_HOUR", 18),
        ambient_digest_window_minutes=_int("OSTAG_AMBIENT_DIGEST_WINDOW_MINUTES", 90),
        ambient_manager_open_ids=_csv("OSTAG_AMBIENT_MANAGER_OPEN_IDS"),
        ambient_stale_days=_int("OSTAG_AMBIENT_STALE_DAYS", 7),
        ambient_suppress_days=_int("OSTAG_AMBIENT_SUPPRESS_DAYS", 3),
        ambient_max_decisions=_int("OSTAG_AMBIENT_MAX_DECISIONS", 3),
        ambient_daily_board_enabled=_bool("OSTAG_AMBIENT_DAILY_BOARD_ENABLED", False),
        ambient_daily_board_hour=_int("OSTAG_AMBIENT_DAILY_BOARD_HOUR", 12),
        ambient_daily_board_window_minutes=_int("OSTAG_AMBIENT_DAILY_BOARD_WINDOW_MINUTES", 60),
        ambient_timezone=os.getenv("OSTAG_AMBIENT_TIMEZONE", "Asia/Shanghai"),
        ambient_daily_board_group_name=os.getenv(
            "OSTAG_AMBIENT_DAILY_BOARD_GROUP_NAME", "Dadao 投研交易 FDE"
        ).strip('"'),
        ambient_daily_board_base_url=os.getenv("OSTAG_AMBIENT_DAILY_BOARD_BASE_URL", ""),
        monitor_enabled=_bool("OSTAG_MONITOR_ENABLED", False),
        monitor_instance_id=os.getenv("OSTAG_MONITOR_INSTANCE_ID", "local"),
        monitor_instance_secret=os.getenv("OSTAG_MONITOR_INSTANCE_SECRET", ""),
        monitor_enrollment_secret=os.getenv("OSTAG_MONITOR_ENROLLMENT_SECRET", ""),
        monitor_central_url=os.getenv("OSTAG_MONITOR_CENTRAL_URL", ""),
        monitor_spool_db=Path(
            os.getenv("OSTAG_MONITOR_SPOOL_DB", "./logs/work_monitor_spool.sqlite3")
        ),
        monitor_report_interval_seconds=_int("OSTAG_MONITOR_REPORT_INTERVAL_SECONDS", 30),
        monitor_command_poll_seconds=_int("OSTAG_MONITOR_COMMAND_POLL_SECONDS", 10),
        monitor_osstack_bin=os.getenv("OSTAG_MONITOR_OSSTACK_BIN", "osstack"),
        monitor_osstack_references=Path(
            os.getenv("OSTAG_MONITOR_OSSTACK_REFERENCES", "./logs/osstack_task_references.json")
        ),
        monitor_server_db=Path(
            os.getenv("OSTAG_MONITOR_SERVER_DB", "/var/lib/ostag/work_monitor.sqlite3")
        ),
        monitor_admin_token=os.getenv("OSTAG_MONITOR_ADMIN_TOKEN", ""),
        monitor_instance_secrets=_instance_secrets(),
        monitor_human_credentials=_human_credentials(),
        monitor_server_max_body_bytes=_int("OSTAG_MONITOR_SERVER_MAX_BODY_BYTES", 1_048_576),
        monitor_scheduler_enabled=_bool("OSTAG_MONITOR_SCHEDULER_ENABLED", True),
        monitor_morning_digest_hour=_int("OSTAG_MONITOR_MORNING_DIGEST_HOUR", 9),
        monitor_evening_digest_hour=_int("OSTAG_MONITOR_EVENING_DIGEST_HOUR", 19),
        monitor_degraded_seconds=_int("OSTAG_MONITOR_DEGRADED_SECONDS", 120),
        monitor_offline_seconds=_int("OSTAG_MONITOR_OFFLINE_SECONDS", 600),
        monitor_notification_retry_cap=_int("OSTAG_MONITOR_NOTIFICATION_RETRY_CAP", 5),
        operational_report_enabled=_bool("OSTAG_OPERATIONAL_REPORT_ENABLED", False),
        operational_report_connector_evidence_dir=Path(
            os.getenv("OSTAG_OPERATIONAL_REPORT_CONNECTOR_EVIDENCE_DIR", "./release")
        ),
        operational_report_max_items=_int("OSTAG_OPERATIONAL_REPORT_MAX_ITEMS", 100),
        copilot_relay_enabled=_bool("OSTAG_COPILOT_RELAY_ENABLED", False),
        copilot_relay_server_enabled=_bool("OSTAG_COPILOT_RELAY_SERVER_ENABLED", True),
        copilot_relay_write_enabled=_bool("OSTAG_COPILOT_RELAY_WRITE_ENABLED", True),
        copilot_status_board_enabled=_bool("OSTAG_COPILOT_STATUS_BOARD_ENABLED", False),
        copilot_relay_canary_session_ids=_csv("OSTAG_COPILOT_RELAY_CANARY_SESSION_IDS"),
        copilot_relay_instance_id=os.getenv("OSTAG_COPILOT_RELAY_INSTANCE_ID", "local"),
        copilot_relay_secret=os.getenv("OSTAG_COPILOT_RELAY_SECRET", ""),
        copilot_relay_central_url=os.getenv("OSTAG_COPILOT_RELAY_CENTRAL_URL", ""),
        copilot_relay_spool_db=Path(
            os.getenv("OSTAG_COPILOT_RELAY_SPOOL_DB", "./logs/copilot_relay_spool.sqlite3")
        ),
        copilot_relay_osstack_bin=os.getenv("OSTAG_COPILOT_RELAY_OSSTACK_BIN", "osstack"),
        copilot_relay_default_repo=os.getenv("OSTAG_COPILOT_RELAY_DEFAULT_REPO", ""),
        copilot_relay_poll_interval_seconds=_int("OSTAG_COPILOT_RELAY_POLL_INTERVAL_SECONDS", 5),
        copilot_relay_max_text_chars=_int("OSTAG_COPILOT_RELAY_MAX_TEXT_CHARS", 4_000),
        device_identity_file=Path(
            os.getenv(
                "OSTAG_DEVICE_IDENTITY_FILE",
                "~/Library/Application Support/OStag/device-identity.json",
            )
        ).expanduser(),
        device_display_name=os.getenv("OSTAG_DEVICE_DISPLAY_NAME", "OStag Device").strip(),
        daily_task_boards_enabled=_bool("OSTAG_DAILY_TASK_BOARDS_ENABLED", False),
        daily_task_board_poll_seconds=_int("OSTAG_DAILY_TASK_BOARD_POLL_SECONDS", 30),
        daily_task_board_morning_hour=_int("OSTAG_DAILY_TASK_BOARD_MORNING_HOUR", 9),
        daily_task_board_evening_hour=_int("OSTAG_DAILY_TASK_BOARD_EVENING_HOUR", 18),
        daily_task_board_record_limit=_int("OSTAG_DAILY_TASK_BOARD_RECORD_LIMIT", 50),
        daily_task_board_feishu_chat_id=os.getenv(
            "OSTAG_DAILY_TASK_BOARD_FEISHU_CHAT_ID", ""
        ),
        daily_task_board_feishu_base_token=os.getenv(
            "OSTAG_DAILY_TASK_BOARD_FEISHU_BASE_TOKEN", ""
        ),
        daily_task_board_feishu_table_id=os.getenv(
            "OSTAG_DAILY_TASK_BOARD_FEISHU_TABLE_ID", ""
        ),
        daily_task_board_feishu_table_url=os.getenv(
            "OSTAG_DAILY_TASK_BOARD_FEISHU_TABLE_URL", ""
        ),
        daily_task_board_feishu_manager_ids=_csv(
            "OSTAG_DAILY_TASK_BOARD_FEISHU_MANAGER_IDS"
        ),
        daily_task_board_dingtalk_chat_id=os.getenv(
            "OSTAG_DAILY_TASK_BOARD_DINGTALK_CHAT_ID", ""
        ),
        daily_task_board_dingtalk_base_token=os.getenv(
            "OSTAG_DAILY_TASK_BOARD_DINGTALK_BASE_TOKEN", ""
        ),
        daily_task_board_dingtalk_table_id=os.getenv(
            "OSTAG_DAILY_TASK_BOARD_DINGTALK_TABLE_ID", ""
        ),
        daily_task_board_dingtalk_table_url=os.getenv(
            "OSTAG_DAILY_TASK_BOARD_DINGTALK_TABLE_URL", ""
        ),
        daily_task_board_dingtalk_manager_ids=_csv(
            "OSTAG_DAILY_TASK_BOARD_DINGTALK_MANAGER_IDS"
        ),
    )
    if overrides:
        data = settings.__dict__ | overrides
        settings = Settings(**data)
    if "run_ledger_path" not in overrides and os.getenv("OSTAG_RUN_LEDGER_PATH") is None:
        derived = settings.audit_log.parent / "run_ledger.jsonl"
        settings = Settings(**(settings.__dict__ | {"run_ledger_path": derived}))
    return settings.validate()
