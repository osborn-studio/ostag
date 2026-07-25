#!/usr/bin/env python3
"""Sanitized, read-only acceptance for two OStag Device Gateways."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


ALLOWED_FIELDS = {
    "display_name",
    "agent_type",
    "session_state",
    "latency_seconds",
    "callback_result",
    "crossed_routes",
}


def _files(args: argparse.Namespace) -> tuple[Path, ...]:
    if args.fixtures is not None:
        return tuple(sorted(args.fixtures.glob("*.json")))
    return tuple(args.status)


def main() -> int:
    parser = argparse.ArgumentParser()
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--fixtures", type=Path)
    source.add_argument("--status", type=Path, action="append")
    parser.add_argument("--allow-write-canary", action="store_true")
    args = parser.parse_args()
    files = _files(args)
    if len(files) != 2:
        parser.error("exactly two sanitized device status files are required")

    devices: list[dict[str, object]] = []
    for path in files:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or set(payload) != ALLOWED_FIELDS:
            raise SystemExit(f"status=failed reason=unsafe_status_shape file={path.name}")
        if not isinstance(payload["latency_seconds"], int | float):
            raise SystemExit("status=failed reason=invalid_latency")
        devices.append(payload)

    names = {str(item["display_name"]) for item in devices}
    agents = {str(item["agent_type"]) for item in devices}
    crossed_routes = sum(int(item["crossed_routes"]) for item in devices)
    passed = (
        names == {"MacBook Pro", "Mac mini"}
        and agents == {"codex", "claude-code"}
        and all(float(item["latency_seconds"]) < 10 for item in devices)
        and all(item["callback_result"] == "applied_once" for item in devices)
        and crossed_routes == 0
    )
    for item in devices:
        print(
            "device={display_name} agent={agent_type} state={session_state} "
            "latency_seconds={latency_seconds} callback={callback_result}".format(**item)
        )
    print(
        f"status={'passed' if passed else 'failed'} devices={len(devices)} "
        f"crossed_routes={crossed_routes} write_canary={'enabled' if args.allow_write_canary else 'disabled'}"
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
