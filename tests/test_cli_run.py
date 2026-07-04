from __future__ import annotations

import json
from pathlib import Path

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
