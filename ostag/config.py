from __future__ import annotations

import os
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None


Platform = Literal["slack", "feishu", "lark", "dingtalk", "telegram"]
BackendName = Literal["mock", "codex_cli", "claude_cli", "openai_api"]


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
    actor_permissions: tuple[str, ...] = ()
    default_permission_level: str = "propose"
    apply_enabled: bool = False
    receipt_ttl_seconds: int = 86400
    approval_quorum_high_risk: int = 1
    four_eyes_enabled: bool = False
    gh_bin: str = "gh"
    github_default_repo: str = ""
    glab_bin: str = "glab"
    gitlab_default_repo: str = ""
    run_cost_credits: int = 0
    daily_cost_budget: int = 0
    governed_writes: bool = False
    followup_enabled: bool = True
    followup_after_seconds: int = 14400
    env_scrub_extra: tuple[str, ...] = ()
    mfs_enabled: bool = False
    mfs_bin: str = "mfs"
    mfs_server_url: str = "http://127.0.0.1:8000"
    mfs_allowed_scopes: tuple[str, ...] = ("file://./context",)
    mfs_max_results: int = 8
    codex_bin: str = "codex"
    claude_bin: str = "claude"
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
    copilot_relay_enabled: bool = False
    copilot_relay_instance_id: str = "local"
    copilot_relay_secret: str = ""
    copilot_relay_central_url: str = ""
    copilot_relay_spool_db: Path = Path("./logs/copilot_relay_spool.sqlite3")
    copilot_relay_osstack_bin: str = "osstack"
    copilot_relay_poll_interval_seconds: int = 5
    copilot_relay_max_text_chars: int = 4_000
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

    def validate(self) -> Settings:
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
        if self.copilot_relay_enabled:
            required = {
                "copilot_relay_instance_id": self.copilot_relay_instance_id,
                "copilot_relay_secret": self.copilot_relay_secret,
                "copilot_relay_central_url": self.copilot_relay_central_url,
                "copilot_relay_osstack_bin": self.copilot_relay_osstack_bin,
            }
            missing = next((name for name, value in required.items() if not value), "")
            if missing:
                raise ValueError(f"copilot relay requires {missing}")
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
        actor_permissions=_csv("OSTAG_ACTOR_PERMISSIONS"),
        default_permission_level=os.getenv("OSTAG_DEFAULT_PERMISSION_LEVEL", "propose"),
        apply_enabled=_bool("OSTAG_APPLY_ENABLED", False),
        receipt_ttl_seconds=_int("OSTAG_RECEIPT_TTL_SECONDS", 86400),
        approval_quorum_high_risk=_int("OSTAG_APPROVAL_QUORUM_HIGH_RISK", 1),
        four_eyes_enabled=_bool("OSTAG_FOUR_EYES_ENABLED", False),
        gh_bin=os.getenv("OSTAG_GH_BIN", "gh"),
        github_default_repo=os.getenv("OSTAG_GITHUB_DEFAULT_REPO", ""),
        glab_bin=os.getenv("OSTAG_GLAB_BIN", "glab"),
        gitlab_default_repo=os.getenv("OSTAG_GITLAB_DEFAULT_REPO", ""),
        run_cost_credits=_int("OSTAG_RUN_COST_CREDITS", 0),
        daily_cost_budget=_int("OSTAG_DAILY_COST_BUDGET", 0),
        governed_writes=_bool("OSTAG_GOVERNED_WRITES", False),
        followup_enabled=_bool("OSTAG_FOLLOWUP_ENABLED", True),
        followup_after_seconds=_int("OSTAG_FOLLOWUP_AFTER_SECONDS", 14400),
        env_scrub_extra=_csv("OSTAG_ENV_SCRUB_EXTRA"),
        mfs_enabled=_bool("MFS_ENABLED", False),
        mfs_bin=os.getenv("MFS_BIN", "mfs"),
        mfs_server_url=os.getenv("MFS_SERVER_URL", "http://127.0.0.1:8000"),
        mfs_allowed_scopes=scopes,
        mfs_max_results=_int("MFS_MAX_RESULTS", 8),
        codex_bin=os.getenv("CODEX_BIN", "codex"),
        claude_bin=os.getenv("CLAUDE_BIN", "claude"),
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
        copilot_relay_enabled=_bool("OSTAG_COPILOT_RELAY_ENABLED", False),
        copilot_relay_instance_id=os.getenv("OSTAG_COPILOT_RELAY_INSTANCE_ID", "local"),
        copilot_relay_secret=os.getenv("OSTAG_COPILOT_RELAY_SECRET", ""),
        copilot_relay_central_url=os.getenv("OSTAG_COPILOT_RELAY_CENTRAL_URL", ""),
        copilot_relay_spool_db=Path(
            os.getenv("OSTAG_COPILOT_RELAY_SPOOL_DB", "./logs/copilot_relay_spool.sqlite3")
        ),
        copilot_relay_osstack_bin=os.getenv("OSTAG_COPILOT_RELAY_OSSTACK_BIN", "osstack"),
        copilot_relay_poll_interval_seconds=_int("OSTAG_COPILOT_RELAY_POLL_INTERVAL_SECONDS", 5),
        copilot_relay_max_text_chars=_int("OSTAG_COPILOT_RELAY_MAX_TEXT_CHARS", 4_000),
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
