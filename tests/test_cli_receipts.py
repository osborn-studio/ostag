from __future__ import annotations

import json
from pathlib import Path

import pytest

from ostag.core.run_ledger import RunLedger
from ostag.core.schemas import ActionReceipt, new_id
from ostag.main import main_async


def _receipt_payload(run_id: str = "run_cli1") -> dict:
    return {
        "action_id": new_id("action"),
        "run_id": run_id,
        "title": "更新需求 REQ-7 owner",
        "target_system": "feishu_base",
        "target_object": "REQ-7",
        "mutation_type": "update",
        "risk": "low",
        "state": "needs_approval",
    }


@pytest.fixture()
def ledger_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    ledger_path = tmp_path / "run_ledger.jsonl"
    monkeypatch.setenv("OSTAG_RUN_LEDGER_PATH", str(ledger_path))
    return ledger_path


async def test_receipts_propose_and_list(
    ledger_env: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    payload = _receipt_payload()
    receipt_file = tmp_path / "receipt.json"
    receipt_file.write_text(json.dumps(payload), encoding="utf-8")

    exit_code = await main_async(
        ["receipts", "propose", "--file", str(receipt_file), "--chat", "chat1", "--json"]
    )
    assert exit_code == 0
    proposed = json.loads(capsys.readouterr().out)
    assert proposed["action_id"] == payload["action_id"]

    exit_code = await main_async(["receipts", "list", "--pending", "--json"])
    assert exit_code == 0
    receipts = json.loads(capsys.readouterr().out)
    assert [item["action_id"] for item in receipts] == [payload["action_id"]]


async def test_cli_approve_as_local_admin(
    ledger_env: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    payload = _receipt_payload()
    ledger = RunLedger(ledger_env)
    ledger.record_receipt(ActionReceipt.from_dict(payload), chat_id="chat1")

    exit_code = await main_async(["approve", payload["action_id"], "--json"])

    assert exit_code == 0
    result = json.loads(capsys.readouterr().out)
    assert result["action"]["state"] == "ready_to_apply"


async def test_cli_apply_disabled_returns_structured_error(
    ledger_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    payload = _receipt_payload()
    ledger = RunLedger(ledger_env)
    ledger.record_receipt(ActionReceipt.from_dict(payload), chat_id="chat1")
    await main_async(["approve", payload["action_id"], "--json"])
    capsys.readouterr()

    exit_code = await main_async(["apply", payload["action_id"], "--json"])

    assert exit_code == 1
    error = json.loads(capsys.readouterr().out)["error"]
    assert error["code"] == "apply_disabled"
    assert "OSTAG_APPLY_ENABLED" in error["recovery_hint"]


async def test_cli_reject_with_restricted_actor_denied(
    ledger_env: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OSTAG_ACTOR_PERMISSIONS", "viewer:read")
    payload = _receipt_payload()
    ledger = RunLedger(ledger_env)
    ledger.record_receipt(ActionReceipt.from_dict(payload), chat_id="chat1")

    exit_code = await main_async(["reject", payload["action_id"], "--actor", "viewer", "--json"])

    assert exit_code == 1
    error = json.loads(capsys.readouterr().out)["error"]
    assert error["code"] == "permission_denied"


async def test_cli_continue_returns_run_snapshot(
    ledger_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from ostag.core.event import NormalizedEvent

    ledger = RunLedger(ledger_env)
    run = ledger.open_run(
        NormalizedEvent(
            platform="slack",
            event_id="evt",
            chat_id="C1",
            message_id="m1",
            sender_id="U1",
            text="do things",
            mention_bot=True,
            chat_type="group",
            destination_id="chat",
        )
    )

    exit_code = await main_async(["continue", run.run_id, "--json"])

    assert exit_code == 0
    result = json.loads(capsys.readouterr().out)
    assert result["run"]["run_id"] == run.run_id
    assert "note" in result


async def test_cli_propose_invalid_json_error(
    ledger_env: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bad_file = tmp_path / "bad.json"
    bad_file.write_text("{not json", encoding="utf-8")

    exit_code = await main_async(["receipts", "propose", "--file", str(bad_file), "--json"])

    assert exit_code == 1
    error = json.loads(capsys.readouterr().out)["error"]
    assert error["code"] == "invalid_json"
