from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None


Platform = Literal["slack", "feishu", "lark", "dingtalk"]
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
    governed_writes: bool = False
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
        governed_writes=_bool("OSTAG_GOVERNED_WRITES", False),
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
        auto_base_enabled=_bool("OSTAG_AUTO_BASE_ENABLED", True),
        auto_base_identity=os.getenv("OSTAG_AUTO_BASE_IDENTITY", "user"),
        auto_base_history_page_size=_int("OSTAG_AUTO_BASE_HISTORY_PAGE_SIZE", 30),
        auto_base_cli_timeout_seconds=_int("OSTAG_AUTO_BASE_CLI_TIMEOUT_SECONDS", 120),
        auto_base_create_table_if_missing=_bool("OSTAG_AUTO_BASE_CREATE_TABLE_IF_MISSING", True),
        auto_base_default_table_name=os.getenv("OSTAG_AUTO_BASE_DEFAULT_TABLE_NAME", "需求池"),
        auto_base_default_base_token=os.getenv("OSTAG_AUTO_BASE_DEFAULT_BASE_TOKEN", ""),
        auto_base_default_table_id=os.getenv("OSTAG_AUTO_BASE_DEFAULT_TABLE_ID", ""),
        auto_base_download_dir=os.getenv(
            "OSTAG_AUTO_BASE_DOWNLOAD_DIR", "tmp/ostag-auto-base"
        ),
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
    )
    if overrides:
        data = settings.__dict__ | overrides
        settings = Settings(**data)
    if "run_ledger_path" not in overrides and os.getenv("OSTAG_RUN_LEDGER_PATH") is None:
        derived = settings.audit_log.parent / "run_ledger.jsonl"
        settings = Settings(**(settings.__dict__ | {"run_ledger_path": derived}))
    return settings
