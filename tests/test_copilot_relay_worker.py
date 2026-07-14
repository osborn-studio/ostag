from __future__ import annotations

import pytest

from ostag.copilot_relay.worker import run_cycle


class _Reporter:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def __getattr__(self, name: str):
        async def call() -> int:
            self.calls.append(name)
            return 0

        return call


@pytest.mark.asyncio
async def test_read_only_worker_never_polls_or_applies_inbound_messages() -> None:
    reporter = _Reporter()

    await run_cycle(reporter, write_enabled=False)  # type: ignore[arg-type]

    assert "poll_inbound_once" not in reporter.calls
    assert "apply_inbound_once" not in reporter.calls
    assert reporter.calls == [
        "discover_sessions_once",
        "capture_changed_snapshots_once",
        "flush_outbound_once",
        "flush_receipts_once",
        "flush_outbound_once",
    ]


@pytest.mark.asyncio
async def test_write_enabled_worker_polls_and_applies_inbound_messages() -> None:
    reporter = _Reporter()

    await run_cycle(reporter, write_enabled=True)  # type: ignore[arg-type]

    assert "poll_inbound_once" in reporter.calls
    assert "apply_inbound_once" in reporter.calls
