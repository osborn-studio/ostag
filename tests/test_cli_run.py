from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import ANY

import pytest

from ostag.main import main_async


@pytest.fixture()
def ledger_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    ledger_path = tmp_path / "run_ledger.jsonl"
    monkeypatch.setenv("OSTAG_RUN_LEDGER_PATH", str(ledger_path))
    monkeypatch.setenv("OSTAG_AUDIT_LOG", str(tmp_path / "audit.log"))
    monkeypatch.setenv("OSTAG_BACKEND", "mock")
    return ledger_path


async def test_run_submits_through_pipeline(
    ledger_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = await main_async(
        [
            "run",
            "--platform",
            "feishu",
            "--chat",
            "oc_cli_demo",
            "--text",
            "请总结项目下一步",
            "--json",
        ]
    )

    assert exit_code == 0
    result = json.loads(capsys.readouterr().out)
    assert result["reply"].startswith("OS Tag mock reply:")
    assert result["run"]["run_id"].startswith("run_")
    assert result["run"]["status"] == "succeeded"
    assert result["run"]["source"]["platform"] == "feishu"
    assert result["run"]["source"]["channel_id"] == "oc_cli_demo"
    assert result["run"]["actor"]["actor_id"] == "local-admin"
    assert result["run"]["context_packet_id"].startswith("ctx_")


async def test_run_failure_exits_nonzero(
    ledger_env: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OSTAG_MAX_INPUT_CHARS", "4")

    exit_code = await main_async(
        ["run", "--chat", "oc_cli_demo", "--text", "这条消息太长会被拒绝", "--json"]
    )

    assert exit_code == 1
    result = json.loads(capsys.readouterr().out)
    assert result["run"]["status"] == "failed"


async def test_runs_list_newest_first(
    ledger_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    await main_async(["run", "--chat", "oc_a", "--text", "第一个任务", "--json"])
    first = json.loads(capsys.readouterr().out)["run"]["run_id"]
    await main_async(["run", "--chat", "oc_b", "--text", "第二个任务", "--json"])
    second = json.loads(capsys.readouterr().out)["run"]["run_id"]

    exit_code = await main_async(["runs", "list", "--json"])

    assert exit_code == 0
    runs = json.loads(capsys.readouterr().out)
    assert [run["run_id"] for run in runs[:2]] == [second, first]


async def test_context_export_returns_packet_with_hash(
    ledger_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    await main_async(["run", "--chat", "oc_ctx", "--text", "分析一下现状", "--json"])
    run_id = json.loads(capsys.readouterr().out)["run"]["run_id"]

    exit_code = await main_async(["context", "export", "--run", run_id, "--json"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    packet = payload["context_packet"]
    assert packet["run_id"] == run_id
    assert packet["source_thread_summary"] == "分析一下现状"
    assert packet["citations"] == [f"feishu://oc_ctx/{packet['citations'][0].split('/')[-1]}"]
    assert packet["policy_constraints"]["backend"] == "mock"
    assert len(payload["hash"]) == 16


async def test_context_export_unknown_run_errors(
    ledger_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = await main_async(["context", "export", "--run", "run_nope", "--json"])

    assert exit_code == 1
    error = json.loads(capsys.readouterr().out)["error"]
    assert error["code"] == "run_not_found"


@pytest.fixture()
def monitor_access_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    database = tmp_path / "monitor.sqlite3"
    monkeypatch.setenv("OSTAG_MONITOR_SERVER_DB", str(database))
    return database


async def test_monitor_access_binds_lists_and_revokes_instance(
    monitor_access_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bind = await main_async(
        [
            "monitor",
            "access",
            "bind-instance",
            "--platform",
            "feishu",
            "--actor",
            "ou_employee",
            "--private-chat",
            "oc_employee",
            "--instance",
            "employee-mac",
            "--display-name",
            "员工 Mac mini",
            "--role",
            "member",
            "--json",
        ]
    )
    assert bind == 0
    assert json.loads(capsys.readouterr().out) == {
        "bound": True,
        "platform": "feishu",
        "actor_id": "ou_employee",
        "instance_id": "employee-mac",
        "display_name": "员工 Mac mini",
        "role": "member",
    }

    listed = await main_async(
        ["monitor", "access", "list", "--actor", "ou_employee", "--json"]
    )
    assert listed == 0
    assert json.loads(capsys.readouterr().out) == {
        "bindings": [
            {
                "platform": "feishu",
                "actor_id": "ou_employee",
                "instance_id": "employee-mac",
                "display_name": "员工 Mac mini",
                "bound_at": ANY,
            }
        ]
    }

    revoked = await main_async(
        ["monitor", "access", "revoke-instance", "--instance", "employee-mac", "--json"]
    )
    assert revoked == 0
    assert json.loads(capsys.readouterr().out) == {
        "revoked": True,
        "instance_id": "employee-mac",
    }
    assert await main_async(["monitor", "access", "list", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"bindings": []}


async def test_monitor_access_rejects_duplicate_active_instance_owner(
    monitor_access_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    common = [
        "monitor",
        "access",
        "bind-instance",
        "--platform",
        "feishu",
        "--private-chat",
        "oc_employee",
        "--instance",
        "employee-mac",
        "--display-name",
        "Employee Mac",
        "--role",
        "member",
        "--json",
    ]
    assert await main_async(common + ["--actor", "ou_employee"]) == 0
    capsys.readouterr()

    assert await main_async(common + ["--actor", "ou_other"]) == 1
    error = json.loads(capsys.readouterr().out)["error"]
    assert error["code"] == "identity_binding_failed"


async def test_copilot_device_cli_creates_lists_and_revokes_for_private_owner(
    monitor_access_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert await main_async(
        [
            "monitor", "access", "bind-instance",
            "--platform", "feishu",
            "--actor", "ou_owner",
            "--private-chat", "oc_owner",
            "--instance", "legacy-instance",
            "--display-name", "Legacy",
            "--role", "owner",
            "--json",
        ]
    ) == 0
    capsys.readouterr()

    assert await main_async(["copilot", "device", "pairing-code", "--json"]) == 0
    pairing = json.loads(capsys.readouterr().out)
    assert pairing["expires_in"] == 600
    assert pairing["pairing_code"].count("-") == 2

    assert await main_async(["copilot", "device", "list", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"devices": []}

    assert await main_async(
        ["copilot", "device", "revoke", "--device", "device_missing", "--json"]
    ) == 1
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "device_operation_failed"
