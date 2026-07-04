from __future__ import annotations

import json
from pathlib import Path

import pytest

from ostag.core.event import NormalizedEvent
from ostag.core.run_ledger import RunLedger
from ostag.core.schemas import Artifact, new_id
from ostag.main import main_async


def _seed_run(tmp_path: Path) -> tuple[str, Path]:
    ledger_path = tmp_path / "run_ledger.jsonl"
    ledger = RunLedger(ledger_path)
    run = ledger.open_run(
        NormalizedEvent(
            platform="slack",
            event_id="evt",
            chat_id="C123",
            message_id="m1",
            sender_id="U1",
            text="ship the report",
            mention_bot=True,
        ),
        executor_id="mock",
    )
    ledger.record_status(run.run_id, "running")
    ledger.record_artifact(
        Artifact(
            artifact_id=new_id("art"),
            run_id=run.run_id,
            name="report.md",
            path="/tmp/report.md",
        )
    )
    ledger.record_status(run.run_id, "succeeded")
    return run.run_id, ledger_path


@pytest.fixture()
def ledger_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    run_id, ledger_path = _seed_run(tmp_path)
    monkeypatch.setenv("OSTAG_RUN_LEDGER_PATH", str(ledger_path))
    return run_id


async def test_status_json_outputs_compact_snapshot(
    ledger_env: str, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = await main_async(["status", "--run", ledger_env, "--json"])

    assert exit_code == 0
    out = capsys.readouterr().out.strip()
    assert "\n" not in out
    snapshot = json.loads(out)
    assert snapshot["run_id"] == ledger_env
    assert snapshot["status"] == "succeeded"
    assert snapshot["source"]["platform"] == "slack"


async def test_status_unknown_run_returns_error_with_recovery_hint(
    ledger_env: str, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = await main_async(["status", "--run", "run_nope", "--json"])

    assert exit_code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"]["code"] == "run_not_found"
    assert payload["error"]["recovery_hint"]


async def test_ledger_export_json_returns_all_events(
    ledger_env: str, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = await main_async(["ledger", "export", "--run", ledger_env, "--json"])

    assert exit_code == 0
    events = json.loads(capsys.readouterr().out)
    assert [event["event_type"] for event in events] == [
        "run_created",
        "run_status_changed",
        "artifact_recorded",
        "run_finished",
    ]


async def test_artifacts_list_json(ledger_env: str, capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = await main_async(["artifacts", "list", "--run", ledger_env, "--json"])

    assert exit_code == 0
    artifacts = json.loads(capsys.readouterr().out)
    assert len(artifacts) == 1
    assert artifacts[0]["name"] == "report.md"
    assert artifacts[0]["path"] == "/tmp/report.md"


async def test_connector_list_json_covers_platforms(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = await main_async(["connector", "list", "--json"])

    assert exit_code == 0
    connectors = json.loads(capsys.readouterr().out)
    platforms = {item["platform"] for item in connectors}
    assert {"slack", "feishu", "lark", "dingtalk", "github", "gitlab"} <= platforms


async def test_connector_list_platform_filter(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = await main_async(["connector", "list", "--platform", "dingtalk", "--json"])

    assert exit_code == 0
    connectors = json.loads(capsys.readouterr().out)
    assert connectors
    assert all(item["platform"] == "dingtalk" for item in connectors)
