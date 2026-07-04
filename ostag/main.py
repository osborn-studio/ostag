from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from ostag.adapters.dingtalk import DingTalkAdapter
from ostag.adapters.base import Adapter
from ostag.adapters.feishu import FeishuAdapter
from ostag.adapters.lark import LarkAdapter
from ostag.adapters.slack import SlackAdapter
from ostag.config import load_settings
from ostag.core.ambient import AmbientScheduler
from ostag.core.approvals import PENDING_STATES, ApprovalService
from ostag.core.capabilities import list_capabilities
from ostag.core.doctor import doctor_exit_code, format_checks, run_doctor
from ostag.core.event import NormalizedEvent
from ostag.core.followups import FollowUpService
from ostag.core.permissions import LOCAL_ADMIN_ACTOR
from ostag.core.receipts import ReceiptCommand
from ostag.core.run_ledger import RunLedger
from ostag.core.runner import OSTagRunner
from ostag.core.schemas import ActionReceipt, SchemaError, new_id
from ostag.mfs.memory import ensure_memory_file, read_memory, remember, sync_memory_root


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run OS Tag.")
    subparsers = parser.add_subparsers(dest="command")

    memory = subparsers.add_parser("memory", help="Manage optional OS Tag channel memory.")
    memory_sub = memory.add_subparsers(dest="memory_command", required=True)
    memory_show = memory_sub.add_parser("show")
    memory_show.add_argument("--platform", choices=["slack", "feishu", "lark", "dingtalk"], required=True)
    memory_show.add_argument("--chat-id", required=True)
    memory_path = memory_sub.add_parser("path")
    memory_path.add_argument("--platform", choices=["slack", "feishu", "lark", "dingtalk"], required=True)
    memory_path.add_argument("--chat-id", required=True)
    memory_remember = memory_sub.add_parser("remember")
    memory_remember.add_argument("--platform", choices=["slack", "feishu", "lark", "dingtalk"], required=True)
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
    run_submit.add_argument(
        "--backend", choices=["mock", "codex_cli", "claude_cli", "openai_api"]
    )
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

    parser.add_argument("--platform", choices=["slack", "feishu", "lark", "dingtalk"])
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--live", action="store_true", help="Disable dry-run and require live credentials.")
    parser.add_argument("--event", type=Path)
    parser.add_argument("--backend", choices=["mock", "codex_cli", "claude_cli", "openai_api"])
    parser.add_argument("--config")
    parser.add_argument("--doctor", action="store_true", help="Run deployment preflight checks and exit.")
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
    raise NotImplementedError(f"Unsupported platform: {platform}")


async def main_async(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "memory":
        return run_memory_command(args)
    if args.command == "run":
        return await run_submit_command(args)
    if args.command in ("status", "ledger", "artifacts", "connector", "runs", "context"):
        return run_inspection_command(args)
    if args.command in ("approve", "reject", "apply", "continue", "receipts"):
        return await run_receipt_command(args)
    if args.command == "followups":
        return await run_followups_command(args)
    if args.command == "channel":
        return run_channel_command(args)
    if args.command == "ambient":
        return await run_ambient_command(args)

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

    runner = OSTagRunner(config)
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


def run_inspection_command(args: argparse.Namespace) -> int:
    """Read-only agent-facing commands: status / ledger export / artifacts / connector."""
    as_json = getattr(args, "json", False)
    try:
        if args.command == "connector":
            capabilities = list_capabilities(getattr(args, "platform", None))
            _emit([item.to_dict() for item in capabilities], as_json=as_json)
            return 0

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

    async def send_message(self, chat_id: str, text: str) -> None:
        self.messages.append({"chat_id": chat_id, "text": text})


async def run_ambient_command(args: argparse.Namespace) -> int:
    as_json = getattr(args, "json", False)
    config = load_settings(getattr(args, "config", None))
    if args.send:
        adapter = create_adapter(config.platform, config)
    else:
        adapter = _CollectingAdapter()
    scheduler = AmbientScheduler(config, adapter=adapter)
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
    _emit(payload, as_json=as_json)
    return 0 if result.errors == 0 else 1


def _policy_payload(config, policy) -> dict:
    from ostag.core.permissions import effective_flag

    channel = {
        "governed_writes": policy.governed_writes,
        "apply_enabled": policy.apply_enabled,
        "approval_quorum_high_risk": policy.approval_quorum_high_risk,
        "four_eyes": policy.four_eyes,
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
    }
    return {"channel": channel, "effective": effective}


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
                pair.split(":", 1)[0]: pair
                for pair in policy.actor_permissions
                if ":" in pair
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
    service = ApprovalService(config, ledger)
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
