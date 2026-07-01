from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from ostag.adapters.base import Adapter
from ostag.adapters.feishu import FeishuAdapter
from ostag.adapters.lark import LarkAdapter
from ostag.adapters.slack import SlackAdapter
from ostag.config import load_settings
from ostag.core.ambient import AmbientScheduler
from ostag.core.doctor import doctor_exit_code, format_checks, run_doctor
from ostag.core.runner import OSTagRunner
from ostag.mfs.memory import ensure_memory_file, read_memory, remember, sync_memory_root


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run OS Tag.")
    subparsers = parser.add_subparsers(dest="command")

    memory = subparsers.add_parser("memory", help="Manage optional OS Tag channel memory.")
    memory_sub = memory.add_subparsers(dest="memory_command", required=True)
    memory_show = memory_sub.add_parser("show")
    memory_show.add_argument("--platform", choices=["slack", "feishu", "lark"], required=True)
    memory_show.add_argument("--chat-id", required=True)
    memory_path = memory_sub.add_parser("path")
    memory_path.add_argument("--platform", choices=["slack", "feishu", "lark"], required=True)
    memory_path.add_argument("--chat-id", required=True)
    memory_remember = memory_sub.add_parser("remember")
    memory_remember.add_argument("--platform", choices=["slack", "feishu", "lark"], required=True)
    memory_remember.add_argument("--chat-id", required=True)
    memory_remember.add_argument("--text", required=True)
    memory_remember.add_argument("--sync", action="store_true")
    memory_sub.add_parser("sync")

    parser.add_argument("--platform", choices=["slack", "feishu", "lark"])
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
    raise NotImplementedError(f"Unsupported platform: {platform}")


async def main_async(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "memory":
        return run_memory_command(args)

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
