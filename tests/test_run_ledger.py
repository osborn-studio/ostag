from __future__ import annotations

import json
from pathlib import Path

import pytest

from ostag.config import load_settings
from ostag.core.event import NormalizedEvent
from ostag.core.run_ledger import RunLedger
from ostag.core.runner import OSTagRunner
from ostag.core.schemas import Artifact, LedgerEvent, SchemaError, new_id


def _event(text: str = "帮我总结一下") -> NormalizedEvent:
    return NormalizedEvent(
        platform="feishu",
        event_id="evt",
        chat_id="chat1",
        message_id="msg1",
        sender_id="sender1",
        text=text,
        mention_bot=True,
        chat_type="group",
        destination_id="chat",
        thread_id="thread1",
    )


def test_open_run_appends_run_created(tmp_path: Path) -> None:
    ledger = RunLedger(tmp_path / "run_ledger.jsonl")

    run = ledger.open_run(_event(), executor_id="mock")

    assert run.run_id.startswith("run_")
    assert run.status == "admitted"
    events = ledger.export_run(run.run_id)
    assert len(events) == 1
    assert events[0]["event_type"] == "run_created"
    assert events[0]["payload"]["run"]["source"]["platform"] == "feishu"
    assert events[0]["payload"]["run"]["source"]["thread_id"] == "thread1"
    assert events[0]["payload"]["run"]["actor"]["actor_id"] == "sender1"
    assert events[0]["payload"]["run"]["executor_id"] == "mock"


def test_ledger_redacts_secrets_before_disk(tmp_path: Path) -> None:
    path = tmp_path / "run_ledger.jsonl"
    ledger = RunLedger(path)

    run = ledger.open_run(_event("请用 OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwxyz 部署"))
    ledger.record_status(run.run_id, "failed", detail="xoxb-123-abc leaked in output")

    raw = path.read_text(encoding="utf-8")
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in raw
    assert "xoxb-123-abc" not in raw
    assert "[REDACTED]" in raw


def test_run_status_reconstructs_latest_snapshot(tmp_path: Path) -> None:
    ledger = RunLedger(tmp_path / "run_ledger.jsonl")
    run = ledger.open_run(_event())

    ledger.record_status(run.run_id, "running")
    ledger.record_status(run.run_id, "succeeded")

    snapshot = ledger.run_status(run.run_id)
    assert snapshot["status"] == "succeeded"
    assert snapshot["run_id"] == run.run_id
    assert snapshot["event_count"] == 3
    assert snapshot["intent"]["raw_text"] == "帮我总结一下"


def test_run_status_unknown_run_raises_with_recovery_hint(tmp_path: Path) -> None:
    ledger = RunLedger(tmp_path / "run_ledger.jsonl")

    with pytest.raises(SchemaError) as excinfo:
        ledger.run_status("run_missing")
    assert excinfo.value.code == "run_not_found"
    assert "ledger" in excinfo.value.recovery_hint


def test_artifacts_are_listed_per_run(tmp_path: Path) -> None:
    ledger = RunLedger(tmp_path / "run_ledger.jsonl")
    run = ledger.open_run(_event())
    other = ledger.open_run(_event("另一个请求"))

    ledger.record_artifact(
        Artifact(
            artifact_id=new_id("art"),
            run_id=run.run_id,
            name="report.md",
            path="/tmp/report.md",
        )
    )

    assert len(ledger.list_artifacts(run.run_id)) == 1
    assert ledger.list_artifacts(run.run_id)[0]["name"] == "report.md"
    assert ledger.list_artifacts(other.run_id) == []


def test_list_runs_returns_newest_first(tmp_path: Path) -> None:
    ledger = RunLedger(tmp_path / "run_ledger.jsonl")
    first = ledger.open_run(_event("第一"))
    second = ledger.open_run(_event("第二"))

    runs = ledger.list_runs()

    assert [item["run_id"] for item in runs] == [second.run_id, first.run_id]


def test_ledger_lines_are_stable_json(tmp_path: Path) -> None:
    path = tmp_path / "run_ledger.jsonl"
    ledger = RunLedger(path)
    ledger.append(
        LedgerEvent(
            event_id="evt_fixed",
            run_id="run_fixed",
            event_type="security_event",
            payload={"reason": "test"},
            created_at="2026-07-04T00:00:00+00:00",
        )
    )

    line = path.read_text(encoding="utf-8").strip()
    record = json.loads(line)
    assert record["event_id"] == "evt_fixed"
    assert record["event_type"] == "security_event"
    # keys are sorted so agents can rely on stable serialization
    assert list(record) == sorted(record)


@pytest.mark.asyncio
async def test_runner_creates_run_for_every_handled_event(tmp_path: Path) -> None:
    config = load_settings(None, audit_log=tmp_path / "audit.log", backend="mock")
    runner = OSTagRunner(config)

    reply = await runner.handle_event(_event("请总结项目"))

    assert reply
    ledger_path = tmp_path / "run_ledger.jsonl"
    assert ledger_path.exists()
    records = [json.loads(line) for line in ledger_path.read_text(encoding="utf-8").splitlines()]
    event_types = [record["event_type"] for record in records]
    assert event_types[0] == "run_created"
    assert "run_finished" in event_types
    run_id = records[0]["run_id"]
    assert runner.run_ledger.run_status(run_id)["status"] == "succeeded"


@pytest.mark.asyncio
async def test_runner_records_failed_run_for_rejected_input(tmp_path: Path) -> None:
    config = load_settings(None, audit_log=tmp_path / "audit.log", max_input_chars=4)
    runner = OSTagRunner(config)

    await runner.handle_event(_event("这条消息太长了一定会被拒绝"))

    runs = runner.run_ledger.list_runs()
    assert len(runs) == 1
    assert runs[0]["status"] == "failed"


@pytest.mark.asyncio
async def test_runner_ignores_non_mention_without_creating_run(tmp_path: Path) -> None:
    config = load_settings(None, audit_log=tmp_path / "audit.log")
    runner = OSTagRunner(config)
    event = NormalizedEvent(
        platform="feishu",
        event_id="evt",
        chat_id="chat1",
        message_id="msg1",
        sender_id="sender1",
        text="普通闲聊",
        mention_bot=False,
        chat_type="group",
        destination_id="chat",
    )

    reply = await runner.handle_event(event)

    assert reply == ""
    assert not (tmp_path / "run_ledger.jsonl").exists()
