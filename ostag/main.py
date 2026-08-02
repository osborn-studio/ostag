from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

from ostag.adapters.dingtalk import DingTalkAdapter
from ostag.adapters.base import Adapter
from ostag.adapters.feishu import FeishuAdapter
from ostag.adapters.telegram import TelegramAdapter
from ostag.adapters.lark import LarkAdapter
from ostag.adapters.slack import SlackAdapter
from ostag.config import load_settings
from ostag.connectors.apply import build_apply_executors
from ostag.core.ambient import AmbientScheduler
from ostag.core.approvals import PENDING_STATES, ApprovalService
from ostag.core.capabilities import list_capabilities
from ostag.core.connector_diagnostics import connector_test
from ostag.core.doctor import doctor_exit_code, format_checks, run_doctor
from ostag.core.event import NormalizedEvent
from ostag.core.identity import Principal
from ostag.core.followups import FollowUpService
from ostag.core.permissions import LOCAL_ADMIN_ACTOR
from ostag.core.receipts import ReceiptCommand
from ostag.core.run_ledger import RunLedger
from ostag.core.runner import OSTagRunner
from ostag.core.schemas import ActionReceipt, SchemaError, new_id
from ostag.mfs.memory import ensure_memory_file, read_memory, remember, sync_memory_root
from ostag.workflows.auto_base import AutoBaseWorkflow
from ostag.work_monitor.reporter import Reporter
from ostag.work_monitor.identity import Destination, IdentityError, IdentityStore
from ostag.work_monitor.service import WorkMonitorService
from ostag.work_monitor.store import MonitorStore
from ostag.work_monitor.executor import (
    CommandExecutor,
    OsstackCliControlPort,
    OsstackTaskReference,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run OS Tag.")
    subparsers = parser.add_subparsers(dest="command")

    memory = subparsers.add_parser("memory", help="Manage optional OS Tag channel memory.")
    memory_sub = memory.add_subparsers(dest="memory_command", required=True)
    memory_show = memory_sub.add_parser("show")
    memory_show.add_argument(
        "--platform", choices=["slack", "feishu", "lark", "dingtalk"], required=True
    )
    memory_show.add_argument("--chat-id", required=True)
    memory_path = memory_sub.add_parser("path")
    memory_path.add_argument(
        "--platform", choices=["slack", "feishu", "lark", "dingtalk"], required=True
    )
    memory_path.add_argument("--chat-id", required=True)
    memory_remember = memory_sub.add_parser("remember")
    memory_remember.add_argument(
        "--platform", choices=["slack", "feishu", "lark", "dingtalk"], required=True
    )
    memory_remember.add_argument("--chat-id", required=True)
    memory_remember.add_argument("--text", required=True)
    memory_remember.add_argument("--sync", action="store_true")
    memory_sub.add_parser("sync")

    run_submit = subparsers.add_parser(
        "run", help="Submit a task through the OS Tag pipeline from the CLI."
    )
    run_submit.add_argument(
        "--platform", choices=["slack", "feishu", "lark", "dingtalk"], help="Defaults to config."
    )
    run_submit.add_argument("--chat", required=True, help="Chat/channel id the run is anchored to.")
    run_submit.add_argument("--text", required=True, help="Task text, as if the bot was mentioned.")
    run_submit.add_argument(
        "--actor",
        default=LOCAL_ADMIN_ACTOR,
        help="Actor id the run is attributed to (default: local-admin).",
    )
    run_submit.add_argument("--thread", help="Optional thread id.")
    run_submit.add_argument("--backend", choices=["mock", "codex_cli", "claude_cli", "openai_api"])
    run_submit.add_argument("--json", action="store_true")
    run_submit.add_argument("--config")

    runs = subparsers.add_parser("runs", help="Inspect recent runs.")
    runs_sub = runs.add_subparsers(dest="runs_command", required=True)
    runs_list = runs_sub.add_parser("list", help="List recent runs, newest first.")
    runs_list.add_argument("--limit", type=int, default=20)
    runs_list.add_argument("--json", action="store_true")
    runs_list.add_argument("--config")

    context = subparsers.add_parser("context", help="Inspect run context packets.")
    context_sub = context.add_subparsers(dest="context_command", required=True)
    context_export = context_sub.add_parser(
        "export", help="Export the context packet recorded for a run."
    )
    context_export.add_argument("--run", required=True)
    context_export.add_argument("--json", action="store_true")
    context_export.add_argument("--config")

    status = subparsers.add_parser("status", help="Show the latest state of a run.")
    status.add_argument("--run", required=True, help="Run id, e.g. run_ab12cd34ef56.")
    status.add_argument("--json", action="store_true")
    status.add_argument("--config")

    ledger = subparsers.add_parser("ledger", help="Inspect the durable run ledger.")
    ledger_sub = ledger.add_subparsers(dest="ledger_command", required=True)
    ledger_export = ledger_sub.add_parser("export", help="Export all ledger events for a run.")
    ledger_export.add_argument("--run", required=True)
    ledger_export.add_argument("--json", action="store_true")
    ledger_export.add_argument("--config")

    artifacts = subparsers.add_parser("artifacts", help="Inspect run artifacts.")
    artifacts_sub = artifacts.add_subparsers(dest="artifacts_command", required=True)
    artifacts_list = artifacts_sub.add_parser("list", help="List artifacts recorded for a run.")
    artifacts_list.add_argument("--run", required=True)
    artifacts_list.add_argument("--json", action="store_true")
    artifacts_list.add_argument("--config")

    connector = subparsers.add_parser("connector", help="Inspect connector capabilities.")
    connector_sub = connector.add_subparsers(dest="connector_command", required=True)
    connector_list = connector_sub.add_parser("list", help="List declared connector capabilities.")
    connector_list.add_argument(
        "--platform", choices=["slack", "feishu", "lark", "dingtalk", "github", "gitlab"]
    )
    connector_list.add_argument("--json", action="store_true")
    connector_test_parser = connector_sub.add_parser(
        "test", help="Run non-mutating connector readiness diagnostics."
    )
    connector_test_parser.add_argument(
        "platform", choices=["feishu", "lark", "dingtalk", "github", "gitlab"]
    )
    connector_test_parser.add_argument(
        "--live", action="store_true", help="Add bounded read-only provider probes."
    )
    connector_test_parser.add_argument(
        "--card-platform",
        choices=["feishu", "dingtalk"],
        help="Render a native diagnostic card instead of the raw report.",
    )
    connector_test_parser.add_argument("--json", action="store_true")
    connector_test_parser.add_argument("--config")

    query = subparsers.add_parser("query", help="Run isolated read-only queries.")
    query_sub = query.add_subparsers(dest="query_command", required=True)
    query_status = query_sub.add_parser("status", help="Read a run status through the query lane.")
    query_status.add_argument("--run", required=True)
    query_status.add_argument(
        "--platform", choices=["slack", "feishu", "lark", "dingtalk"], required=True
    )
    query_status.add_argument("--chat", required=True)
    query_status.add_argument("--actor", default=LOCAL_ADMIN_ACTOR)
    query_status.add_argument("--json", action="store_true")
    query_status.add_argument("--config")
    query_audit = query_sub.add_parser("audit", help="Read an audit bundle through the query lane.")
    query_audit.add_argument("--since")
    query_audit.add_argument("--until")
    query_audit.add_argument(
        "--platform", choices=["slack", "feishu", "lark", "dingtalk"], required=True
    )
    query_audit.add_argument("--chat", required=True)
    query_audit.add_argument("--actor", default=LOCAL_ADMIN_ACTOR)
    query_audit.add_argument("--json", action="store_true")
    query_audit.add_argument("--config")

    workstream = subparsers.add_parser("workstream", help="Manage governed multi-run workstreams.")
    workstream_sub = workstream.add_subparsers(dest="workstream_command", required=True)
    recipe_register = workstream_sub.add_parser("recipe-register")
    recipe_register.add_argument("--file", type=Path, required=True)
    recipe_register.add_argument("--json", action="store_true")
    recipe_register.add_argument("--config")
    workstream_admit = workstream_sub.add_parser("admit")
    workstream_admit.add_argument("--recipe", required=True)
    workstream_admit.add_argument("--version", type=int, required=True)
    workstream_admit.add_argument("--key", required=True)
    workstream_admit.add_argument("--goal", required=True)
    workstream_admit.add_argument("--json", action="store_true")
    workstream_admit.add_argument("--config")
    workstream_attach = workstream_sub.add_parser("attach-run")
    workstream_attach.add_argument("--id", required=True)
    workstream_attach.add_argument("--run", required=True)
    workstream_attach.add_argument("--cost", type=int, default=0)
    workstream_attach.add_argument("--json", action="store_true")
    workstream_attach.add_argument("--config")
    workstream_show = workstream_sub.add_parser("show")
    workstream_show.add_argument("--id", required=True)
    workstream_show.add_argument("--json", action="store_true")
    workstream_show.add_argument("--config")
    workstream_list = workstream_sub.add_parser("list")
    workstream_list.add_argument("--json", action="store_true")
    workstream_list.add_argument("--config")

    for verb in ("approve", "reject", "apply", "continue"):
        verb_parser = subparsers.add_parser(
            verb, help=f"{verb.capitalize()} an action receipt (or run, for continue)."
        )
        verb_parser.add_argument(
            "reference", help="Pending index, action_... id, or run_... id (continue/status)."
        )
        verb_parser.add_argument(
            "--actor",
            default=LOCAL_ADMIN_ACTOR,
            help="Actor id to execute as (default: local-admin).",
        )
        verb_parser.add_argument("--chat", help="Chat id scope for numeric indexes.")
        verb_parser.add_argument("--json", action="store_true")
        verb_parser.add_argument("--config")

    receipts = subparsers.add_parser("receipts", help="Manage action receipts.")
    receipts_sub = receipts.add_subparsers(dest="receipts_command", required=True)
    receipts_list = receipts_sub.add_parser("list", help="List receipts.")
    receipts_list.add_argument("--chat", help="Filter by chat id.")
    receipts_list.add_argument("--pending", action="store_true", help="Pending states only.")
    receipts_list.add_argument("--json", action="store_true")
    receipts_list.add_argument("--config")
    receipts_propose = receipts_sub.add_parser(
        "propose", help="Propose a new receipt from a JSON file."
    )
    receipts_propose.add_argument("--file", required=True, type=Path)
    receipts_propose.add_argument("--chat", default="", help="Chat id the receipt belongs to.")
    receipts_propose.add_argument(
        "--source-platform",
        default="",
        choices=["", "slack", "feishu", "lark", "dingtalk", "github", "gitlab"],
        help="Platform the receipt belongs to.",
    )
    receipts_propose.add_argument(
        "--actor",
        default=LOCAL_ADMIN_ACTOR,
        help="Proposer actor id recorded on the receipt (default: local-admin).",
    )
    receipts_propose.add_argument("--json", action="store_true")
    receipts_propose.add_argument("--config")

    followups = subparsers.add_parser("followups", help="Manage scheduled continuations.")
    followups_sub = followups.add_subparsers(dest="followups_command", required=True)
    followups_list = followups_sub.add_parser("list", help="List pending continuations.")
    followups_list.add_argument("--all", action="store_true", help="Include fired/cancelled.")
    followups_list.add_argument("--json", action="store_true")
    followups_list.add_argument("--config")
    followups_run = followups_sub.add_parser(
        "run-once", help="Fire due continuations now (renders reminders to stdout)."
    )
    followups_run.add_argument("--json", action="store_true")
    followups_run.add_argument("--config")

    ambient = subparsers.add_parser("ambient", help="Ambient monitor operations.")
    ambient_sub = ambient.add_subparsers(dest="ambient_command", required=True)
    ambient_run = ambient_sub.add_parser(
        "run-once", help="Run one ambient sweep now. Dry by default; --send delivers to chats."
    )
    ambient_run.add_argument(
        "--send", action="store_true", help="Actually send messages via the platform adapter."
    )
    ambient_run.add_argument("--json", action="store_true")
    ambient_run.add_argument("--config")
    ambient_board = ambient_sub.add_parser(
        "board-once", help="Build the daily requirement board now. Dry by default."
    )
    ambient_board.add_argument(
        "--send", action="store_true", help="Actually send the card via the platform adapter."
    )
    ambient_board.add_argument("--json", action="store_true")
    ambient_board.add_argument("--config")

    audit = subparsers.add_parser("audit", help="Consolidated audit export.")
    audit_sub = audit.add_subparsers(dest="audit_command", required=True)
    audit_export = audit_sub.add_parser(
        "export", help="Export a filtered audit bundle (runs, receipts, costs, security)."
    )
    audit_export.add_argument("--since", help="Earliest UTC date, YYYY-MM-DD (inclusive).")
    audit_export.add_argument("--until", help="Latest UTC date, YYYY-MM-DD (inclusive).")
    audit_export.add_argument(
        "--platform", choices=["slack", "feishu", "lark", "dingtalk", "github", "gitlab"]
    )
    audit_export.add_argument("--chat", help="Filter by channel/chat id.")
    audit_export.add_argument("--output", type=Path, help="Write the bundle to a file.")
    audit_export.add_argument("--json", action="store_true")
    audit_export.add_argument("--config")

    budget = subparsers.add_parser("budget", help="Cost budget inspection.")
    budget_sub = budget.add_subparsers(dest="budget_command", required=True)
    budget_status = budget_sub.add_parser(
        "status", help="Show a channel's daily spend against its budget."
    )
    budget_status.add_argument(
        "--platform", choices=["slack", "feishu", "lark", "dingtalk"], required=True
    )
    budget_status.add_argument("--chat", required=True)
    budget_status.add_argument("--json", action="store_true")
    budget_status.add_argument("--config")

    monitor = subparsers.add_parser("monitor", help="Report local OStag/osstack work safely.")
    monitor_sub = monitor.add_subparsers(dest="monitor_command", required=True)
    ingest = monitor_sub.add_parser(
        "ingest", help="Queue one strict monitor event (osstack bridge)."
    )
    source = ingest.add_mutually_exclusive_group(required=True)
    source.add_argument("--file", type=Path)
    source.add_argument("--stdin", action="store_true")
    ingest.add_argument("--json", action="store_true")
    ingest.add_argument("--config")
    ingest.add_argument("--osstack-run-id")
    ingest.add_argument("--workspace")
    ingest.add_argument("--state-path")
    reporter_parser = monitor_sub.add_parser(
        "reporter", help="Run or inspect the durable reporter."
    )
    reporter_sub = reporter_parser.add_subparsers(dest="reporter_command", required=True)
    for name in ("run-once", "start", "status"):
        command = reporter_sub.add_parser(name)
        command.add_argument("--json", action="store_true")
        command.add_argument("--config")
    access_parser = monitor_sub.add_parser(
        "access", help="Manage Feishu member and osstack instance ownership."
    )
    access_sub = access_parser.add_subparsers(dest="access_command", required=True)
    access_bind = access_sub.add_parser("bind-instance")
    access_bind.add_argument("--platform", choices=["feishu"], required=True)
    access_bind.add_argument("--actor", required=True)
    access_bind.add_argument("--private-chat", required=True)
    access_bind.add_argument("--instance", required=True)
    access_bind.add_argument("--display-name", required=True)
    access_bind.add_argument("--role", choices=["member", "owner", "admin"], required=True)
    access_bind.add_argument("--json", action="store_true")
    access_bind.add_argument("--config")
    access_revoke = access_sub.add_parser("revoke-instance")
    access_revoke.add_argument("--instance", required=True)
    access_revoke.add_argument("--json", action="store_true")
    access_revoke.add_argument("--config")
    access_list = access_sub.add_parser("list")
    access_list.add_argument("--actor")
    access_list.add_argument("--json", action="store_true")
    access_list.add_argument("--config")

    copilot = subparsers.add_parser("copilot", help="Manage the OStag Copilot device gateway.")
    copilot_sub = copilot.add_subparsers(dest="copilot_command", required=True)
    device = copilot_sub.add_parser("device", help="Pair and manage signed devices.")
    device_sub = device.add_subparsers(dest="device_command", required=True)
    pairing = device_sub.add_parser("pairing-code")
    pairing.add_argument("--platform")
    pairing.add_argument("--actor")
    pairing.add_argument("--json", action="store_true")
    pairing.add_argument("--config")
    pair = device_sub.add_parser("pair")
    pair.add_argument("--code", required=True)
    pair.add_argument("--display-name", required=True)
    pair.add_argument("--central-url", required=True)
    pair.add_argument("--json", action="store_true")
    pair.add_argument("--config")
    device_list = device_sub.add_parser("list")
    device_list.add_argument("--platform")
    device_list.add_argument("--actor")
    device_list.add_argument("--json", action="store_true")
    device_list.add_argument("--config")
    rotate = device_sub.add_parser("rotate-key")
    rotate.add_argument("--device", required=True)
    rotate.add_argument("--central-url")
    rotate.add_argument("--json", action="store_true")
    rotate.add_argument("--config")
    revoke = device_sub.add_parser("revoke")
    revoke.add_argument("--device", required=True)
    revoke.add_argument("--platform")
    revoke.add_argument("--actor")
    revoke.add_argument("--json", action="store_true")
    revoke.add_argument("--config")

    channel = subparsers.add_parser("channel", help="Per-channel policy management.")
    channel_sub = channel.add_subparsers(dest="channel_command", required=True)
    channel_policy = channel_sub.add_parser("policy", help="Show or set channel policy.")
    channel_policy_sub = channel_policy.add_subparsers(dest="policy_command", required=True)
    policy_show = channel_policy_sub.add_parser("show")
    policy_show.add_argument(
        "--platform", choices=["slack", "feishu", "lark", "dingtalk"], required=True
    )
    policy_show.add_argument("--chat", required=True)
    policy_show.add_argument("--json", action="store_true")
    policy_show.add_argument("--config")
    policy_set = channel_policy_sub.add_parser("set")
    policy_set.add_argument(
        "--platform", choices=["slack", "feishu", "lark", "dingtalk"], required=True
    )
    policy_set.add_argument("--chat", required=True)
    policy_set.add_argument(
        "--governed-writes", choices=["true", "false", "inherit"], dest="governed_writes"
    )
    policy_set.add_argument(
        "--apply-enabled", choices=["true", "false", "inherit"], dest="apply_enabled"
    )
    policy_set.add_argument("--ambient", choices=["true", "false"])
    policy_set.add_argument(
        "--quorum-high-risk",
        dest="quorum_high_risk",
        help="Distinct approvers required for high-risk receipts: a number or 'inherit'.",
    )
    policy_set.add_argument(
        "--four-eyes",
        dest="four_eyes",
        choices=["true", "false", "inherit"],
        help="Whether the proposer may approve their own receipts.",
    )
    policy_set.add_argument(
        "--daily-cost-budget",
        dest="daily_cost_budget",
        help="Daily spend cap in credits: a number (0 = unlimited) or 'inherit'.",
    )
    policy_set.add_argument(
        "--grant",
        action="append",
        default=[],
        help="Channel-scoped actor grant, e.g. ou_alice:write (repeatable).",
    )
    policy_set.add_argument(
        "--revoke", action="append", default=[], help="Remove a channel grant by actor id."
    )
    policy_set.add_argument("--json", action="store_true")
    policy_set.add_argument("--config")

    parser.add_argument("--platform", choices=["slack", "feishu", "lark", "dingtalk", "telegram"])
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--live", action="store_true", help="Disable dry-run and require live credentials."
    )
    parser.add_argument("--event", type=Path)
    parser.add_argument("--backend", choices=["mock", "codex_cli", "claude_cli", "openai_api"])
    parser.add_argument("--config")
    parser.add_argument(
        "--doctor", action="store_true", help="Run deployment preflight checks and exit."
    )
    return parser


def create_adapter(platform: str, config) -> Adapter:
    if platform == "feishu":
        return FeishuAdapter(config)
    if platform == "lark":
        return LarkAdapter(config)
    if platform == "slack":
        return SlackAdapter(config)
    if platform == "dingtalk":
        return DingTalkAdapter(config)
    if platform == "telegram":
        return TelegramAdapter(config)
    raise NotImplementedError(f"Unsupported platform: {platform}")


async def main_async(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "memory":
        return run_memory_command(args)
    if args.command == "run":
        return await run_submit_command(args)
    if args.command in ("status", "ledger", "artifacts", "connector", "runs", "context"):
        return run_inspection_command(args)
    if args.command == "query":
        return await run_query_command(args)
    if args.command == "workstream":
        return run_workstream_command(args)
    if args.command in ("approve", "reject", "apply", "continue", "receipts"):
        return await run_receipt_command(args)
    if args.command == "followups":
        return await run_followups_command(args)
    if args.command == "channel":
        return run_channel_command(args)
    if args.command == "ambient":
        return await run_ambient_command(args)
    if args.command == "budget":
        return run_budget_command(args)
    if args.command == "monitor":
        return run_monitor_command(args)
    if args.command == "copilot":
        return run_copilot_device_command(args)
    if args.command == "audit":
        return run_audit_command(args)

    overrides = {}
    if args.platform:
        overrides["platform"] = args.platform
    if args.backend:
        overrides["backend"] = args.backend
    if args.dry_run:
        overrides["dry_run"] = True
    if args.live:
        overrides["dry_run"] = False
    config = load_settings(args.config, **overrides)

    if args.doctor:
        checks = run_doctor(config, args.event)
        print(format_checks(checks))
        return doctor_exit_code(checks)

    work_monitor = None
    if config.monitor_enabled:
        monitor_store = MonitorStore(config.monitor_spool_db)
        identities = IdentityStore(
            config.monitor_spool_db,
            enrollment_secret=config.monitor_enrollment_secret,
        )
        work_monitor = WorkMonitorService(monitor_store, identities)
    runner = OSTagRunner(config, work_monitor=work_monitor)
    adapter = create_adapter(config.platform, config)

    if config.dry_run:
        if not args.event:
            raise SystemExit("--event is required in dry-run mode")
        await adapter.dry_run(args.event, runner)
        return 0

    ambient_task = None
    if config.ambient_enabled:
        ambient_task = asyncio.create_task(
            AmbientScheduler(config, adapter=adapter).run_forever(),
            name="ostag-ambient-scheduler",
        )
    try:
        await adapter.start(runner)
    finally:
        if ambient_task is not None:
            ambient_task.cancel()
    return 0


def _emit(payload: object, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))


def _reporter_from_config(config) -> tuple[Reporter, CommandExecutor]:
    reporter = Reporter(
        instance_id=config.monitor_instance_id,
        secret=config.monitor_instance_secret,
        central_url=config.monitor_central_url,
        ledger_path=config.run_ledger_path,
        spool_path=config.monitor_spool_db,
        interval_seconds=config.monitor_report_interval_seconds,
        command_poll_seconds=config.monitor_command_poll_seconds,
    )
    seeds: dict[str, OsstackTaskReference] = {}
    if config.monitor_osstack_references.exists():
        try:
            raw_references = json.loads(
                config.monitor_osstack_references.read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError):
            raw_references = {}
        if isinstance(raw_references, dict):
            for task_id, raw in raw_references.items():
                if isinstance(raw, dict):
                    try:
                        seeds[str(task_id)] = OsstackTaskReference(
                            run_id=str(raw.get("run_id", "")),
                            workspace=str(raw.get("workspace", "")),
                            state_path=str(raw.get("state_path", "")),
                            version=int(raw.get("version", 1)),
                            lifecycle=str(raw.get("lifecycle", "running")),
                            attempt_id=str(raw.get("attempt_id", "")),
                            attempt_order=int(raw.get("attempt_order", 0)),
                        )
                    except (TypeError, ValueError):
                        continue
    reporter.seed_osstack_references(seeds)
    port = OsstackCliControlPort(
        instance_id=config.monitor_instance_id,
        binary=config.monitor_osstack_bin,
        reference_provider=reporter.osstack_references,
    )
    executor = CommandExecutor(config.monitor_instance_id, osstack=port)
    reporter.capabilities = port.binary_capabilities
    reporter.task_capabilities = lambda: {
        task_id: port.capabilities_for(task_id) for task_id in reporter.osstack_references()
    }
    return reporter, executor


def run_monitor_command(args: argparse.Namespace) -> int:
    import sys

    config = load_settings(getattr(args, "config", None))
    as_json = getattr(args, "json", False)
    if args.monitor_command == "access":
        identities = IdentityStore(config.monitor_server_db)
        now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        try:
            if args.access_command == "bind-instance":
                principal = Principal(args.platform, args.actor)
                identities.bind_private(
                    owner=principal,
                    destination=Destination(args.platform, args.private_chat, "private"),
                    local_admin=True,
                    role=args.role,
                )
                identities.bind_instance(
                    principal,
                    args.instance,
                    display_name=args.display_name,
                    bound_at=now,
                )
                _emit(
                    {
                        "bound": True,
                        "platform": args.platform,
                        "actor_id": args.actor,
                        "instance_id": args.instance,
                        "display_name": args.display_name,
                        "role": args.role,
                    },
                    as_json=as_json,
                )
                return 0
            if args.access_command == "revoke-instance":
                identities.revoke_instance(args.instance, revoked_at=now)
                _emit(
                    {"revoked": True, "instance_id": args.instance}, as_json=as_json
                )
                return 0
            if args.access_command == "list":
                _emit(
                    {"bindings": identities.instance_bindings(actor_id=args.actor)},
                    as_json=as_json,
                )
                return 0
            raise AssertionError(args.access_command)
        except (IdentityError, ValueError) as exc:
            _emit(
                {"error": {"code": "identity_binding_failed", "message": str(exc)}},
                as_json=as_json,
            )
            return 1
    reporter, executor = _reporter_from_config(config)
    if args.monitor_command == "ingest":
        raw = args.file.read_text(encoding="utf-8") if args.file else sys.stdin.read()
        try:
            payload = json.loads(raw)
            reference_args = (args.osstack_run_id, args.workspace, args.state_path)
            if any(reference_args) and not all(reference_args):
                raise ValueError("osstack reference requires run id, workspace, and state path")
            reference = None
            if all(reference_args):
                task = payload.get("task")
                if not isinstance(task, dict):
                    raise ValueError("osstack reference requires a task event")
                lifecycle = {
                    "task.failed": "failed",
                    "task.completed": "completed",
                    "task.cancelled": "cancelled",
                    "task.started": "running",
                    "task.progress": "running",
                    "task.blocked": "blocked",
                    "task.awaiting_approval": "blocked",
                    "task.created": "queued",
                }[str(payload.get("kind", ""))]
                reference = OsstackTaskReference(
                    run_id=args.osstack_run_id,
                    workspace=args.workspace,
                    state_path=args.state_path,
                    version=int(task.get("version", 0)),
                    lifecycle=lifecycle,
                )
            event = reporter.enqueue(payload, osstack_reference=reference)
        except (json.JSONDecodeError, ValueError, TypeError, KeyError) as exc:
            _emit(
                {"error": {"code": "invalid_monitor_event", "message": str(exc)}}, as_json=as_json
            )
            return 1
        _emit(
            {"queued": True, "event_id": event.event_id, "sequence": event.sequence},
            as_json=as_json,
        )
        return 0
    if args.reporter_command == "status":
        _emit(reporter.status(), as_json=as_json)
        return 0
    if args.reporter_command == "run-once":
        _emit(reporter.run_once(executor.execute), as_json=as_json)
        return 0
    if args.reporter_command == "start":
        if not config.monitor_enabled:
            _emit(
                {
                    "error": {
                        "code": "monitor_disabled",
                        "message": "Set OSTAG_MONITOR_ENABLED=true",
                    }
                },
                as_json=as_json,
            )
            return 1
        if not config.monitor_central_url or not config.monitor_instance_secret:
            _emit(
                {
                    "error": {
                        "code": "monitor_not_configured",
                        "message": "Set central URL and instance secret",
                    }
                },
                as_json=as_json,
            )
            return 1
        reporter.start(executor.execute)
        return 0
    raise AssertionError(args.reporter_command)


def run_copilot_device_command(args: argparse.Namespace) -> int:
    import hashlib
    import secrets
    import time
    import urllib.error
    import urllib.request

    from ostag.copilot_relay.device_identity import DeviceIdentityError, DeviceIdentityStore
    from ostag.work_monitor.device_registry import DeviceRegistry, DeviceRegistryError

    config = load_settings(getattr(args, "config", None))
    as_json = getattr(args, "json", False)
    monitor_store = MonitorStore(config.monitor_server_db)
    registry = DeviceRegistry(monitor_store)

    def resolve_owner() -> Principal:
        platform = getattr(args, "platform", None)
        actor = getattr(args, "actor", None)
        if bool(platform) != bool(actor):
            raise DeviceRegistryError("--platform and --actor must be provided together")
        if platform and actor:
            principal = Principal(platform, actor)
            identities = IdentityStore(config.monitor_server_db)
            if not identities.is_global_owner(principal):
                raise DeviceRegistryError("principal is not a private owner")
            return principal
        with monitor_store.connect() as connection:
            rows = connection.execute(
                "SELECT DISTINCT owner_platform,owner_actor_id FROM monitor_bindings "
                "WHERE chat_type='private' AND role IN ('owner','admin') AND revoked=0"
            ).fetchall()
        if len(rows) != 1:
            raise DeviceRegistryError("exactly one private owner is required; specify --platform and --actor")
        return Principal(str(rows[0]["owner_platform"]), str(rows[0]["owner_actor_id"]))

    def central_url(explicit: str | None = None) -> str:
        value = (explicit or config.copilot_relay_central_url).rstrip("/")
        local = value.startswith(("http://127.0.0.1", "http://localhost", "http://[::1]"))
        if not value or (not value.startswith("https://") and not local):
            raise DeviceRegistryError("central URL must use HTTPS outside localhost")
        return value

    def post(url: str, body: bytes, headers: dict[str, str] | None = None) -> dict[str, object]:
        request = urllib.request.Request(
            url,
            data=body,
            headers={"content-type": "application/json", **(headers or {})},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310
                payload = json.loads(response.read())
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise DeviceRegistryError("central device request failed") from exc
        if not isinstance(payload, dict):
            raise DeviceRegistryError("central device response is invalid")
        return payload

    try:
        if args.device_command == "pairing-code":
            code = registry.create_pairing_code(resolve_owner())
            _emit({"pairing_code": code, "expires_in": 600}, as_json=as_json)
            return 0
        if args.device_command == "list":
            devices = [item.public_dict() for item in registry.list_devices(resolve_owner())]
            _emit({"devices": devices}, as_json=as_json)
            return 0
        if args.device_command == "revoke":
            registry.revoke_device(resolve_owner(), args.device)
            _emit({"revoked": True, "device_id": args.device}, as_json=as_json)
            return 0
        identity_store = DeviceIdentityStore(config.device_identity_file)
        if args.device_command == "pair":
            identity = identity_store.load_or_create(display_name=args.display_name)
            body = json.dumps(
                {
                    "pairing_code": args.code,
                    "device_id": identity.device_id,
                    "display_name": identity.display_name,
                    "public_key": identity.public_key,
                },
                separators=(",", ":"),
            ).encode()
            result = post(central_url(args.central_url) + "/v1/copilot/devices/pair", body)
            _emit(result, as_json=as_json)
            return 0 if result.get("paired") is True else 1
        if args.device_command == "rotate-key":
            identity = identity_store.load_or_create(display_name=config.device_display_name)
            if identity.device_id != args.device:
                raise DeviceRegistryError("--device does not match local device identity")
            pending = identity_store.prepare_rotation(identity)
            path = f"/v1/copilot/devices/{identity.device_id}/rotate-key"
            body = json.dumps({"public_key": pending.public_key}, separators=(",", ":")).encode()
            timestamp = str(int(time.time()))
            nonce = secrets.token_urlsafe(18)
            canonical = "\n".join(
                (
                    "v2",
                    "POST",
                    path,
                    identity.device_id,
                    timestamp,
                    nonce,
                    hashlib.sha256(body).hexdigest(),
                )
            ).encode()
            result = post(
                central_url(args.central_url) + path,
                body,
                {
                    "X-OStag-Device": identity.device_id,
                    "X-OStag-Timestamp": timestamp,
                    "X-OStag-Nonce": nonce,
                    "X-OStag-Signature": identity.sign(canonical),
                },
            )
            if result.get("rotated") is not True:
                raise DeviceRegistryError("central key rotation was not confirmed")
            pending.commit()
            _emit(result, as_json=as_json)
            return 0
        raise AssertionError(args.device_command)
    except (DeviceRegistryError, DeviceIdentityError, ValueError) as exc:
        _emit(
            {"error": {"code": "device_operation_failed", "message": str(exc)}},
            as_json=as_json,
        )
        return 1


def run_inspection_command(args: argparse.Namespace) -> int:
    """Read-only agent-facing commands: status / ledger export / artifacts / connector."""
    as_json = getattr(args, "json", False)
    try:
        if args.command == "connector":
            if args.connector_command == "list":
                capabilities = list_capabilities(getattr(args, "platform", None))
                _emit([item.to_dict() for item in capabilities], as_json=as_json)
                return 0
            config = load_settings(getattr(args, "config", None))
            report = connector_test(config, args.platform, live=args.live)
            if args.card_platform:
                if not config.native_diagnostic_cards_enabled:
                    raise SchemaError(
                        code="feature_disabled",
                        message="Native diagnostic cards are disabled",
                        recovery_hint=(
                            "Set OSTAG_NATIVE_DIAGNOSTIC_CARDS_ENABLED=true after validation."
                        ),
                    )
                from ostag.core.diagnostic_cards import (
                    build_diagnostic_view,
                    render_diagnostic_dingtalk_card,
                    render_diagnostic_feishu_card,
                    render_diagnostic_text,
                )

                view = build_diagnostic_view(report)
                card = (
                    render_diagnostic_feishu_card(view)
                    if args.card_platform == "feishu"
                    else render_diagnostic_dingtalk_card(view)
                )
                _emit(
                    {
                        "card_platform": args.card_platform,
                        "card": card,
                        "fallback": render_diagnostic_text(view),
                    },
                    as_json=as_json,
                )
                return 1 if report.status == "blocked" else 0
            _emit(report.to_dict(), as_json=as_json)
            return 1 if report.status == "blocked" else 0

        config = load_settings(getattr(args, "config", None))
        ledger = RunLedger(config.run_ledger_path)
        if args.command == "status":
            _emit(ledger.run_status(args.run), as_json=as_json)
            return 0
        if args.command == "runs":
            _emit(ledger.list_runs(limit=args.limit), as_json=as_json)
            return 0
        if args.command == "context":
            ledger.run_status(args.run)  # raises run_not_found for unknown runs
            _emit(ledger.get_context_packet(args.run), as_json=as_json)
            return 0
        if args.command == "ledger":
            events = ledger.export_run(args.run)
            if not events:
                raise SchemaError(
                    code="run_not_found",
                    message=f"No ledger events found for run_id={args.run!r}",
                    recovery_hint=(
                        f"Confirm the run id and the ledger path ({config.run_ledger_path})."
                    ),
                )
            _emit(events, as_json=as_json)
            return 0
        if args.command == "artifacts":
            ledger.run_status(args.run)  # raises run_not_found for unknown runs
            _emit(ledger.list_artifacts(args.run), as_json=as_json)
            return 0
        raise AssertionError(args.command)
    except SchemaError as exc:
        _emit({"error": exc.to_dict()}, as_json=as_json)
        return 1


async def run_submit_command(args: argparse.Namespace) -> int:
    """Submit a task through the full OS Tag pipeline without a chat event.

    The reply is returned to the caller; nothing is posted to any platform.
    Table writes still go through governed-writes/policy exactly as chat
    events do.
    """
    as_json = getattr(args, "json", False)
    overrides: dict[str, object] = {}
    if args.platform:
        overrides["platform"] = args.platform
    if args.backend:
        overrides["backend"] = args.backend
    config = load_settings(getattr(args, "config", None), **overrides)
    runner = OSTagRunner(config)
    event = NormalizedEvent(
        platform=config.platform,
        event_id=new_id("evt"),
        chat_id=args.chat,
        message_id=new_id("msg"),
        sender_id=args.actor,
        text=args.text,
        mention_bot=True,
        chat_type="private",
        destination_id=args.chat,
        thread_id=args.thread,
    )
    try:
        reply = await runner.handle_event(event)
        snapshot = runner.run_ledger.run_status(runner.last_run_id)
    except SchemaError as exc:
        _emit({"error": exc.to_dict()}, as_json=as_json)
        return 1
    _emit({"run": snapshot, "reply": reply}, as_json=as_json)
    return 0 if snapshot.get("status") == "succeeded" else 1


class _CollectingAdapter:
    """Dry-run message sink for `ostag ambient run-once` without --send."""

    def __init__(self) -> None:
        self.messages: list[dict[str, str]] = []
        self.cards: list[dict[str, object]] = []

    async def send_message(self, chat_id: str, text: str, *, delivery_id: str) -> None:
        self.messages.append({"chat_id": chat_id, "text": text})

    async def send_card(
        self, chat_id: str, card: dict, fallback: str, *, delivery_id: str
    ) -> None:
        assert delivery_id
        self.cards.append({"chat_id": chat_id, "card": card})


async def run_ambient_command(args: argparse.Namespace) -> int:
    as_json = getattr(args, "json", False)
    config = load_settings(getattr(args, "config", None))
    if args.send:
        adapter = create_adapter(config.platform, config)
    else:
        adapter = _CollectingAdapter()
    scheduler = AmbientScheduler(config, adapter=adapter)
    if args.ambient_command == "board-once":
        result = await scheduler.run_daily_board_once(force=True)
    else:
        result = await scheduler.run_once()
    payload: dict[str, object] = {
        "checked": result.checked,
        "sent": result.sent,
        "skipped": result.skipped,
        "errors": result.errors,
        "delivery": "sent" if args.send else "dry_run",
    }
    if isinstance(adapter, _CollectingAdapter):
        payload["messages"] = adapter.messages
        payload["cards"] = adapter.cards
    _emit(payload, as_json=as_json)
    return 0 if result.errors == 0 else 1


def _policy_payload(config, policy) -> dict:
    from ostag.core.permissions import effective_flag

    channel = {
        "governed_writes": policy.governed_writes,
        "apply_enabled": policy.apply_enabled,
        "approval_quorum_high_risk": policy.approval_quorum_high_risk,
        "four_eyes": policy.four_eyes,
        "daily_cost_budget": policy.daily_cost_budget,
        "ambient_enabled": policy.ambient_enabled,
        "actor_permissions": list(policy.actor_permissions),
        "task_create_allowed": policy.task_create_allowed,
        "task_create_requires_confirmation": policy.task_create_requires_confirmation,
        "daily_action_budget": policy.daily_action_budget,
        "writable_fields": list(policy.writable_fields),
        "default_base_token": policy.default_base_token,
        "default_table_id": policy.default_table_id,
    }
    effective = {
        "governed_writes": effective_flag(policy.governed_writes, config.governed_writes),
        "apply_enabled": effective_flag(policy.apply_enabled, config.apply_enabled),
        "ambient_enabled": policy.ambient_enabled,
        "default_permission_level": config.default_permission_level,
        "approval_quorum_high_risk": (
            policy.approval_quorum_high_risk
            if policy.approval_quorum_high_risk is not None
            else config.approval_quorum_high_risk
        ),
        "four_eyes": effective_flag(policy.four_eyes, config.four_eyes_enabled),
        "daily_cost_budget": (
            policy.daily_cost_budget
            if policy.daily_cost_budget is not None
            else config.daily_cost_budget
        ),
    }
    return {"channel": channel, "effective": effective}


def run_audit_command(args: argparse.Namespace) -> int:
    from ostag.core.audit import AuditExporter

    as_json = getattr(args, "json", False)
    config = load_settings(getattr(args, "config", None))
    ledger = RunLedger(config.run_ledger_path)
    bundle = AuditExporter(config, ledger).export(
        since=args.since,
        until=args.until,
        platform=args.platform,
        chat_id=args.chat,
    )
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(bundle, ensure_ascii=False, sort_keys=True, indent=2),
            encoding="utf-8",
        )
        _emit(
            {"output": str(args.output), "runs": bundle["summary"]["runs"]},
            as_json=as_json,
        )
        return 0
    _emit(bundle, as_json=as_json)
    return 0


async def run_query_command(args: argparse.Namespace) -> int:
    from ostag.core.audit import AuditExporter
    from ostag.core.query_lane import (
        QueryLaneError,
        QueryPolicy,
        QueryRequest,
        ReadOnlyQueryLane,
    )

    as_json = getattr(args, "json", False)
    try:
        config = load_settings(getattr(args, "config", None))
        ledger = RunLedger(config.run_ledger_path)

        async def status_handler(request: QueryRequest) -> dict[str, object]:
            try:
                result = ledger.run_status(str(request.payload["run_id"]))
            except SchemaError as exc:
                raise QueryLaneError(exc.code, exc.message, exc.recovery_hint) from exc
            source = result.get("source")
            source_chat = source.get("channel_id") if isinstance(source, dict) else None
            if request.actor_id != LOCAL_ADMIN_ACTOR and source_chat != request.chat_id:
                raise QueryLaneError(
                    "query_scope_denied", "The requested run is outside this channel"
                )
            return result

        async def audit_handler(request: QueryRequest) -> dict[str, object]:
            return AuditExporter(config, ledger).export(
                since=request.payload.get("since"),
                until=request.payload.get("until"),
                platform=request.platform,
                chat_id=request.chat_id,
            )

        handlers = {"status": status_handler, "audit": audit_handler}
        lane = ReadOnlyQueryLane(
            QueryPolicy(
                enabled=config.query_lane_enabled,
                authorized_channels=frozenset(config.query_lane_channels),
                max_concurrency=config.query_lane_max_concurrency,
                timeout_seconds=config.query_lane_timeout_seconds,
                retry_count=config.query_lane_retry_count,
                max_result_bytes=config.query_lane_max_result_bytes,
            ),
            handlers,
        )
        payload: dict[str, object]
        if args.query_command == "status":
            payload = {"run_id": args.run}
        else:
            payload = {
                key: value
                for key, value in {"since": args.since, "until": args.until}.items()
                if value is not None
            }
        response = await lane.execute(
            QueryRequest(
                query_id=new_id("query"),
                kind=args.query_command,
                platform=args.platform,
                chat_id=args.chat,
                actor_id=args.actor,
                payload=payload,
            )
        )
    except QueryLaneError as exc:
        _emit({"error": exc.to_dict()}, as_json=as_json)
        return 1
    _emit(response.to_dict(), as_json=as_json)
    return 0


def run_workstream_command(args: argparse.Namespace) -> int:
    from ostag.core.workstreams import (
        RecipeDefinition,
        WorkstreamAdmission,
        WorkstreamStore,
    )

    as_json = getattr(args, "json", False)
    try:
        config = load_settings(getattr(args, "config", None))
        if not config.workstreams_enabled:
            raise SchemaError(
                code="feature_disabled",
                message="Workstreams are disabled",
                recovery_hint="Set OSTAG_WORKSTREAMS_ENABLED=true after migration checks.",
            )
        store = WorkstreamStore(
            config.workstream_store_path, max_batch=config.workstream_max_batch
        )
        if args.workstream_command == "recipe-register":
            raw = json.loads(args.file.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise SchemaError(
                    code="invalid_recipe",
                    message="Recipe file must contain an object",
                    recovery_hint="Provide a JSON object matching RecipeDefinition.",
                )
            recipe = store.register_recipe(RecipeDefinition.from_dict(raw))
            _emit({**recipe.to_dict(), "digest": recipe.digest}, as_json=as_json)
            return 0
        if args.workstream_command == "admit":
            admitted = store.admit(
                WorkstreamAdmission(args.key, args.recipe, args.version, args.goal)
            )
            _emit(admitted.to_dict(), as_json=as_json)
            return 0
        if args.workstream_command == "attach-run":
            attached = store.attach_run(args.id, args.run, cost_credits=args.cost)
            _emit(attached.to_dict(), as_json=as_json)
            return 0
        if args.workstream_command == "show":
            workstream = store.get_workstream(args.id)
            _emit(
                {
                    "workstream": workstream.to_dict(),
                    "childRuns": [item.to_dict() for item in store.child_runs(args.id)],
                },
                as_json=as_json,
            )
            return 0
        _emit(
            [item.to_dict() for item in store.list_workstreams()],
            as_json=as_json,
        )
        return 0
    except (SchemaError, OSError, json.JSONDecodeError) as exc:
        if isinstance(exc, SchemaError):
            error = exc.to_dict()
        else:
            error = {
                "code": "workstream_input_error",
                "message": "Workstream input could not be read",
            }
        _emit({"error": error}, as_json=as_json)
        return 1


def run_budget_command(args: argparse.Namespace) -> int:
    from ostag.core.budget import BudgetService
    from ostag.core.channel_brain import ChannelBrainStore

    as_json = getattr(args, "json", False)
    config = load_settings(getattr(args, "config", None))
    ledger = RunLedger(config.run_ledger_path)
    policy = ChannelBrainStore(config).load(args.platform, args.chat).policy
    status = BudgetService(config, ledger).status(args.chat, policy)
    _emit({"platform": args.platform, "chat_id": args.chat, **status}, as_json=as_json)
    return 0


def run_channel_command(args: argparse.Namespace) -> int:
    from ostag.core.channel_brain import ChannelBrainStore

    as_json = getattr(args, "json", False)
    config = load_settings(getattr(args, "config", None))
    store = ChannelBrainStore(config)
    if args.policy_command == "show":
        policy = store.load(args.platform, args.chat).policy
        _emit(_policy_payload(config, policy), as_json=as_json)
        return 0
    if args.policy_command == "set":
        changes: dict[str, object] = {}
        tristate = {"true": True, "false": False, "inherit": None}
        if args.governed_writes is not None:
            changes["governed_writes"] = tristate[args.governed_writes]
        if args.apply_enabled is not None:
            changes["apply_enabled"] = tristate[args.apply_enabled]
        if args.four_eyes is not None:
            changes["four_eyes"] = tristate[args.four_eyes]
        if args.daily_cost_budget is not None:
            if args.daily_cost_budget == "inherit":
                changes["daily_cost_budget"] = None
            elif args.daily_cost_budget.isdigit():
                changes["daily_cost_budget"] = int(args.daily_cost_budget)
            else:
                _emit(
                    {
                        "error": {
                            "code": "invalid_budget",
                            "message": f"Budget {args.daily_cost_budget!r} is not valid",
                            "recovery_hint": "Use a non-negative integer or 'inherit'.",
                        }
                    },
                    as_json=as_json,
                )
                return 1
        if args.ambient is not None:
            changes["ambient_enabled"] = args.ambient == "true"
        if args.quorum_high_risk is not None:
            if args.quorum_high_risk == "inherit":
                changes["approval_quorum_high_risk"] = None
            elif args.quorum_high_risk.isdigit() and int(args.quorum_high_risk) >= 1:
                changes["approval_quorum_high_risk"] = int(args.quorum_high_risk)
            else:
                _emit(
                    {
                        "error": {
                            "code": "invalid_quorum",
                            "message": f"Quorum {args.quorum_high_risk!r} is not valid",
                            "recovery_hint": "Use a positive integer or 'inherit'.",
                        }
                    },
                    as_json=as_json,
                )
                return 1
        if args.grant or args.revoke:
            policy = store.load(args.platform, args.chat).policy
            grants = {
                pair.split(":", 1)[0]: pair for pair in policy.actor_permissions if ":" in pair
            }
            for pair in args.grant:
                actor, _, level = pair.partition(":")
                if not actor or level not in ("read", "propose", "write", "admin"):
                    _emit(
                        {
                            "error": {
                                "code": "invalid_grant",
                                "message": f"Grant {pair!r} is not actor:level",
                                "recovery_hint": "Use e.g. --grant ou_alice:write "
                                "(levels: read/propose/write/admin).",
                            }
                        },
                        as_json=as_json,
                    )
                    return 1
                grants[actor] = pair
            for actor in args.revoke:
                grants.pop(actor, None)
            changes["actor_permissions"] = tuple(grants.values())
        if not changes:
            _emit(
                {
                    "error": {
                        "code": "no_changes",
                        "message": "No policy changes given",
                        "recovery_hint": "Pass at least one of --governed-writes, "
                        "--apply-enabled, --ambient, --grant, --revoke.",
                    }
                },
                as_json=as_json,
            )
            return 1
        brain = store.update_policy(args.platform, args.chat, **changes)
        _emit(_policy_payload(config, brain.policy), as_json=as_json)
        return 0
    raise AssertionError(args.policy_command)


async def run_followups_command(args: argparse.Namespace) -> int:
    as_json = getattr(args, "json", False)
    config = load_settings(getattr(args, "config", None))
    ledger = RunLedger(config.run_ledger_path)
    if args.followups_command == "list":
        _emit(ledger.list_continuations(pending_only=not args.all), as_json=as_json)
        return 0
    if args.followups_command == "run-once":
        outcomes = await FollowUpService(config, ledger).run_once()
        _emit(outcomes, as_json=as_json)
        return 0
    raise AssertionError(args.followups_command)


async def run_receipt_command(args: argparse.Namespace) -> int:
    """Receipt verbs and receipt management for humans and agents."""
    as_json = getattr(args, "json", False)
    config = load_settings(getattr(args, "config", None))
    ledger = RunLedger(config.run_ledger_path)
    service = ApprovalService(config, ledger, build_apply_executors(AutoBaseWorkflow(config)))
    try:
        if args.command == "receipts":
            if args.receipts_command == "list":
                states = PENDING_STATES if args.pending else None
                receipts = ledger.list_receipts(chat_id=args.chat, states=states)
                _emit([receipt.to_dict() for receipt in receipts], as_json=as_json)
                return 0
            if args.receipts_command == "propose":
                payload = json.loads(args.file.read_text(encoding="utf-8"))
                receipt = ActionReceipt.from_dict(payload)
                service.propose(
                    receipt,
                    chat_id=args.chat,
                    platform=args.source_platform,
                    proposer_id=args.actor,
                )
                _emit(receipt.to_dict(), as_json=as_json)
                return 0
            raise AssertionError(args.receipts_command)

        command = ReceiptCommand(verb=args.command, reference=args.reference)
        result = await service.execute(command, actor_id=args.actor, chat_id=args.chat)
        _emit(result, as_json=as_json)
        return 0
    except json.JSONDecodeError as exc:
        _emit(
            {
                "error": {
                    "code": "invalid_json",
                    "message": f"Could not parse {args.file}: {exc}",
                    "recovery_hint": "Provide a JSON object matching the ActionReceipt schema.",
                }
            },
            as_json=as_json,
        )
        return 1
    except SchemaError as exc:
        _emit({"error": exc.to_dict()}, as_json=as_json)
        return 1


def run_memory_command(args: argparse.Namespace) -> int:
    if args.memory_command == "show":
        print(read_memory(args.platform, args.chat_id))
        return 0
    if args.memory_command == "path":
        print(ensure_memory_file(args.platform, args.chat_id))
        return 0
    if args.memory_command == "remember":
        path = remember(args.platform, args.chat_id, args.text)
        print(f"remembered: {path}")
        if args.sync:
            print(f"mfs_job_id: {sync_memory_root()}")
        return 0
    if args.memory_command == "sync":
        print(f"mfs_job_id: {sync_memory_root()}")
        return 0
    raise AssertionError(args.memory_command)


def cli() -> None:
    raise SystemExit(asyncio.run(main_async()))


if __name__ == "__main__":
    cli()
