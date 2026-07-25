from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ostag.config import load_settings
from ostag.core.audit import AuditExporter
from ostag.core.event import NormalizedEvent
from ostag.core.run_ledger import RunLedger
from ostag.core.schemas import ActionReceipt, ApprovalDecision, new_id
from ostag.main import main_async

FIXED_NOW = datetime(2026, 7, 5, 12, 0, 0, tzinfo=UTC)


def _config(tmp_path: Path):
    return load_settings(None, audit_log=tmp_path / "audit.log")


def _event(platform: str, chat_id: str, text: str = "任务") -> NormalizedEvent:
    return NormalizedEvent(
        platform=platform,
        event_id="evt",
        chat_id=chat_id,
        message_id="m1",
        sender_id="u1",
        text=text,
        mention_bot=True,
        chat_type="group",
        destination_id=chat_id,
    )


def _seed(ledger: RunLedger) -> dict:
    # a succeeded feishu run in chat A with cost and an applied receipt
    run_a = ledger.open_run(_event("feishu", "A"), executor_id="mock")
    ledger.record_cost(
        run_a.run_id, chat_id="A", platform="feishu", credits=5, backend="mock", date="2026-07-04"
    )
    receipt = ActionReceipt(
        action_id=new_id("action"),
        run_id=run_a.run_id,
        title="更新",
        target_system="feishu_base",
        target_object="rec1",
        mutation_type="update",
        state="needs_approval",
    )
    ledger.record_receipt(receipt, chat_id="A", platform="feishu")
    approved = receipt.with_state("ready_to_apply")
    ledger.record_receipt_state(approved, previous_state="needs_approval", actor_id="alice")
    ledger.record_approval(
        ApprovalDecision(
            decision_id=new_id("dec"),
            action_id=receipt.action_id,
            run_id=run_a.run_id,
            actor_id="alice",
            decision="approve",
        )
    )
    applied = approved.with_state("applied")
    ledger.record_receipt_state(applied, previous_state="ready_to_apply", actor_id="alice")
    ledger.record_status(run_a.run_id, "succeeded")

    # a failed dingtalk run in chat B with a security event
    run_b = ledger.open_run(_event("dingtalk", "B"), executor_id="mock")
    ledger.record_security_event(run_b.run_id, "permission_denied: viewer tried approve")
    ledger.record_status(run_b.run_id, "failed")
    return {"run_a": run_a.run_id, "run_b": run_b.run_id, "action": receipt.action_id}


def test_export_summary_aggregates(tmp_path: Path) -> None:
    ledger = RunLedger(tmp_path / "ledger.jsonl")
    _seed(ledger)
    bundle = AuditExporter(_config(tmp_path), ledger).export(now=FIXED_NOW)

    assert bundle["generated_at"] == "2026-07-05T12:00:00+00:00"
    summary = bundle["summary"]
    assert summary["runs"] == 2
    assert summary["runs_by_status"] == {"succeeded": 1, "failed": 1}
    assert summary["cost_credits"] == 5
    assert summary["receipts"] == 1
    assert summary["receipts_by_state"] == {"applied": 1}
    assert summary["approvals"] == {"approve": 1, "reject": 0}
    assert summary["security_events"] == 1
    assert len(bundle["runs"]) == 2
    assert len(bundle["security_events"]) == 1
    assert bundle["security_events"][0]["reason"].startswith("permission_denied")


def test_export_run_carries_cost_and_source(tmp_path: Path) -> None:
    ledger = RunLedger(tmp_path / "ledger.jsonl")
    ids = _seed(ledger)
    bundle = AuditExporter(_config(tmp_path), ledger).export(now=FIXED_NOW)

    run_a = next(r for r in bundle["runs"] if r["run_id"] == ids["run_a"])
    assert run_a["cost_credits"] == 5
    assert run_a["source"]["platform"] == "feishu"
    assert run_a["status"] == "succeeded"


def test_export_filters_by_platform_and_chat(tmp_path: Path) -> None:
    ledger = RunLedger(tmp_path / "ledger.jsonl")
    _seed(ledger)
    exporter = AuditExporter(_config(tmp_path), ledger)

    feishu = exporter.export(platform="feishu", now=FIXED_NOW)
    assert feishu["summary"]["runs"] == 1
    assert feishu["summary"]["security_events"] == 0
    assert feishu["filters"] == {"platform": "feishu"}

    chat_b = exporter.export(chat_id="B", now=FIXED_NOW)
    assert chat_b["summary"]["runs"] == 1
    assert chat_b["summary"]["runs_by_status"] == {"failed": 1}
    assert chat_b["summary"]["security_events"] == 1


def test_export_filters_by_date_range(tmp_path: Path) -> None:
    ledger = RunLedger(tmp_path / "ledger.jsonl")
    # hand-craft two runs on different dates via raw records
    from ostag.core.schemas import LedgerEvent

    for run_id, day in (("run_old", "2026-07-01"), ("run_new", "2026-07-05")):
        ledger.append(
            LedgerEvent(
                event_id=new_id("evt"),
                run_id=run_id,
                event_type="run_created",
                payload={
                    "run": {
                        "run_id": run_id,
                        "source": {"platform": "feishu", "channel_id": "A"},
                        "status": "admitted",
                        "created_at": f"{day}T08:00:00+00:00",
                    }
                },
                created_at=f"{day}T08:00:00+00:00",
            )
        )
    exporter = AuditExporter(_config(tmp_path), ledger)

    assert exporter.export(since="2026-07-03", now=FIXED_NOW)["summary"]["runs"] == 1
    assert exporter.export(until="2026-07-03", now=FIXED_NOW)["summary"]["runs"] == 1
    assert (
        exporter.export(since="2026-07-01", until="2026-07-05", now=FIXED_NOW)["summary"]["runs"]
        == 2
    )
    assert exporter.export(since="2026-07-06", now=FIXED_NOW)["summary"]["runs"] == 0


def test_empty_ledger_exports_zeroed_bundle(tmp_path: Path) -> None:
    ledger = RunLedger(tmp_path / "ledger.jsonl")
    bundle = AuditExporter(_config(tmp_path), ledger).export(now=FIXED_NOW)

    assert bundle["summary"]["runs"] == 0
    assert bundle["runs"] == []
    assert bundle["security_events"] == []


async def test_cli_audit_export_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    ledger_path = tmp_path / "ledger.jsonl"
    monkeypatch.setenv("OSTAG_RUN_LEDGER_PATH", str(ledger_path))
    _seed(RunLedger(ledger_path))

    exit_code = await main_async(["audit", "export", "--json"])
    assert exit_code == 0
    bundle = json.loads(capsys.readouterr().out)
    assert bundle["summary"]["runs"] == 2
    assert bundle["summary"]["cost_credits"] == 5


async def test_cli_audit_export_to_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    ledger_path = tmp_path / "ledger.jsonl"
    monkeypatch.setenv("OSTAG_RUN_LEDGER_PATH", str(ledger_path))
    _seed(RunLedger(ledger_path))
    out_path = tmp_path / "bundle" / "audit.json"

    exit_code = await main_async(
        ["audit", "export", "--platform", "feishu", "--output", str(out_path), "--json"]
    )
    assert exit_code == 0
    result = json.loads(capsys.readouterr().out)
    assert result["output"] == str(out_path)
    assert result["runs"] == 1

    written = json.loads(out_path.read_text(encoding="utf-8"))
    assert written["summary"]["runs"] == 1
    assert written["filters"] == {"platform": "feishu"}


def test_export_redacts_via_ledger(tmp_path: Path) -> None:
    # secrets never reach the ledger, so they never reach the export
    ledger = RunLedger(tmp_path / "ledger.jsonl")
    run = ledger.open_run(_event("feishu", "A", "OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwxyz"))
    ledger.record_status(run.run_id, "succeeded")
    bundle = AuditExporter(_config(tmp_path), ledger).export(now=FIXED_NOW)

    dumped = json.dumps(bundle, ensure_ascii=False)
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in dumped
    assert "[REDACTED]" in dumped
