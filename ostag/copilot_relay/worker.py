"""Long-running local Copilot Relay reporter entry point."""

from __future__ import annotations

import asyncio
import signal

from ostag.config import load_settings

from .osstack_port import OsstackSessionPort
from .reporter import CopilotReporter


async def run_worker(stop: asyncio.Event | None = None) -> None:
    settings = load_settings().validate()
    if not settings.copilot_relay_enabled:
        raise RuntimeError("OSTAG_COPILOT_RELAY_ENABLED must be true")
    reporter = CopilotReporter(
        instance_id=settings.copilot_relay_instance_id,
        secret=settings.copilot_relay_secret,
        central_url=settings.copilot_relay_central_url,
        spool_path=settings.copilot_relay_spool_db,
        port=OsstackSessionPort(binary=settings.copilot_relay_osstack_bin),
    )
    stop = stop or asyncio.Event()
    while not stop.is_set():
        try:
            await reporter.discover_sessions_once()
            await reporter.capture_changed_snapshots_once()
            await reporter.flush_outbound_once()
            await reporter.poll_inbound_once()
            await reporter.apply_inbound_once()
            await reporter.flush_receipts_once()
            await reporter.flush_outbound_once()
        except Exception as exc:  # noqa: BLE001 - launch worker retries without logging payloads
            print(f"copilot relay cycle failed: {type(exc).__name__}", flush=True)
        try:
            await asyncio.wait_for(
                stop.wait(), timeout=settings.copilot_relay_poll_interval_seconds
            )
        except TimeoutError:
            pass


def main() -> None:
    stop = asyncio.Event()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    for name in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(name, stop.set)
    try:
        loop.run_until_complete(run_worker(stop))
    finally:
        loop.close()


if __name__ == "__main__":
    main()
