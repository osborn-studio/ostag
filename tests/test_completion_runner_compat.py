from __future__ import annotations

import json
from pathlib import Path

import pytest

from ostag.config import load_settings
from ostag.core.event import NormalizedEvent
from ostag.core.run_ledger import RunLedger
from ostag.core.runner import OSTagRunner

FIXED_NOW = "2026-07-26T09:00:00+00:00"


@pytest.fixture(autouse=True)
def _frozen_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Inject the clock for every runner-compat test (handoff 0.4: no wall clocks)."""
    monkeypatch.setattr("ostag.core.runner.utc_now_iso", lambda: FIXED_NOW)


def _event(text: str = "请总结项目") -> NormalizedEvent:
    return NormalizedEvent(
        platform="feishu",
        event_id="evt",
        chat_id="chat1",
        message_id="m1",
        sender_id="u1",
        text=text,
        mention_bot=True,
        chat_type="group",
        destination_id="chat",
    )


def _ledger_records(tmp_path: Path) -> list[dict]:
    ledger_path = tmp_path / "run_ledger.jsonl"
    return [
        json.loads(line)
        for line in ledger_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


@pytest.mark.asyncio
async def test_successful_run_mirrors_satisfied_compat_assessment(tmp_path: Path) -> None:
    config = load_settings(None, audit_log=tmp_path / "audit.log", backend="mock")
    runner = OSTagRunner(config)

    reply = await runner.handle_event(_event())

    # legacy behavior unchanged: reply + run_finished status write still happen
    assert reply.startswith("OS Tag mock reply:")
    records = _ledger_records(tmp_path)
    statuses = [r for r in records if r["event_type"] == "run_finished"]
    assert statuses and statuses[-1]["payload"]["status"] == "succeeded"

    # and the same terminal outcome is mirrored as an execution_compat assessment
    mirrored = [r for r in records if r["event_type"] == "completion_assessed"]
    assert len(mirrored) == 1
    assessment = mirrored[0]["payload"]["assessment"]
    assert assessment["state"] == "satisfied"
    assert assessment["assessedAt"] == FIXED_NOW
    assert assessment["evidenceBacked"] is False
    assert assessment["assessedBy"] == "ostag"
    assert assessment["inputDigest"].startswith("sha256:")
    assert assessment["gateResults"][0]["reasonCode"] == "execution_succeeded"
    assert assessment["workThreadId"] == mirrored[0]["run_id"]


@pytest.mark.asyncio
async def test_failed_run_mirrors_unsatisfied_compat_assessment(tmp_path: Path) -> None:
    config = load_settings(None, audit_log=tmp_path / "audit.log", max_input_chars=4)
    runner = OSTagRunner(config)

    await runner.handle_event(_event("这条消息太长一定会被拒绝"))

    records = _ledger_records(tmp_path)
    assert [r for r in records if r["event_type"] == "run_finished"][-1]["payload"][
        "status"
    ] == "failed"
    mirrored = [r for r in records if r["event_type"] == "completion_assessed"]
    assert mirrored[-1]["payload"]["assessment"]["state"] == "unsatisfied"


@pytest.mark.asyncio
async def test_mirror_failure_never_breaks_reply_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = load_settings(None, audit_log=tmp_path / "audit.log", backend="mock")
    runner = OSTagRunner(config)

    def boom(*args, **kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError("mirror exploded")

    monkeypatch.setattr(
        "ostag.core.completion_evaluate.evaluate_completion", boom, raising=True
    )

    reply = await runner.handle_event(_event())

    assert reply.startswith("OS Tag mock reply:")  # legacy path intact
    records = _ledger_records(tmp_path)
    assert [r for r in records if r["event_type"] == "run_finished"]
    assert not [r for r in records if r["event_type"] == "completion_assessed"]


@pytest.mark.asyncio
async def test_compat_mirror_survives_ledger_round_trip(tmp_path: Path) -> None:
    from ostag.core.completion import CompletionAssessment

    config = load_settings(None, audit_log=tmp_path / "audit.log", backend="mock")
    runner = OSTagRunner(config)
    await runner.handle_event(_event())

    ledger = RunLedger(config.run_ledger_path)
    mirrored = [
        record
        for record in ledger.events()
        if record.get("event_type") == "completion_assessed"
    ]
    restored = CompletionAssessment.from_dict(mirrored[0]["payload"]["assessment"])
    assert restored.state == "satisfied"
    # the ledger prunes empty collections (targetBindings: [] <=> absent), so
    # equality is asserted semantically: a second round trip is lossless
    assert CompletionAssessment.from_dict(restored.to_dict()) == restored
    assert restored.evidence_backed is False
    assert restored.gate_results[0].reason_code == "execution_succeeded"
