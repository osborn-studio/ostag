from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from ostag.config import load_settings
from ostag.core.ambient import (
    AmbientMonitor,
    AmbientRunResult,
    AmbientScheduler,
    RequirementSnapshot,
)
from ostag.core.channel_brain import ChannelBrain, ChannelBrainStore, ChannelPolicy
from ostag.core.run_ledger import RunLedger
from ostag.main import main_async

TODAY = date(2026, 7, 4)


def _store(tmp_path: Path, config) -> ChannelBrainStore:
    store = ChannelBrainStore(config, root=tmp_path / "brains")
    store.save(
        ChannelBrain(
            platform="feishu",
            chat_id="oc_demo",
            policy=ChannelPolicy(
                default_base_token="base_x",
                default_table_id="tbl_x",
                ambient_enabled=True,
            ),
        )
    )
    return store


def test_overdue_requirement_decision_cites_rule(tmp_path: Path) -> None:
    config = load_settings(None, audit_log=tmp_path / "audit.log")
    monitor = AmbientMonitor(_store(tmp_path, config))

    decisions = monitor.evaluate_requirements(
        "feishu",
        "oc_demo",
        [
            RequirementSnapshot(
                record_id="rec_due",
                title="上线报表",
                owner="张三",
                status="开发交付中",
                due_date=date(2026, 6, 30),
            )
        ],
        today=TODAY,
    )

    assert len(decisions) == 1
    assert decisions[0].kind == "overdue_requirement"
    assert decisions[0].record_id == "rec_due"
    assert "2026-06-30" in decisions[0].message
    assert "rec_due" in decisions[0].message
    assert "due_date" in decisions[0].rule


def test_missing_status_decision(tmp_path: Path) -> None:
    config = load_settings(None, audit_log=tmp_path / "audit.log")
    monitor = AmbientMonitor(_store(tmp_path, config))

    decisions = monitor.evaluate_requirements(
        "feishu",
        "oc_demo",
        [RequirementSnapshot(record_id="rec_ns", title="补状态", owner="李四", status="")],
        today=TODAY,
    )

    assert decisions[0].kind == "missing_status"
    assert "rec_ns" in decisions[0].message


def test_done_requirements_are_quiet(tmp_path: Path) -> None:
    config = load_settings(None, audit_log=tmp_path / "audit.log")
    monitor = AmbientMonitor(_store(tmp_path, config))

    decisions = monitor.evaluate_requirements(
        "feishu",
        "oc_demo",
        [
            RequirementSnapshot(
                record_id="rec_done",
                title="已完成需求",
                owner="张三",
                status="已完成",
                due_date=date(2026, 6, 1),
                days_stale=30,
            )
        ],
        today=TODAY,
    )

    assert decisions == []


def test_multi_decision_sweep_respects_cap(tmp_path: Path) -> None:
    config = load_settings(None, audit_log=tmp_path / "audit.log")
    monitor = AmbientMonitor(_store(tmp_path, config))
    requirements = [
        RequirementSnapshot(record_id=f"rec_{n}", title=f"需求{n}") for n in range(5)
    ]

    decisions = monitor.evaluate_requirements(
        "feishu", "oc_demo", requirements, today=TODAY, max_decisions=2
    )

    assert len(decisions) == 2
    assert {decision.kind for decision in decisions} == {"unowned_requirement"}


def test_suppression_skips_recently_flagged_records(tmp_path: Path) -> None:
    config = load_settings(None, audit_log=tmp_path / "audit.log")
    store = _store(tmp_path, config)
    monitor = AmbientMonitor(store, daily_limit=10)
    requirement = RequirementSnapshot(record_id="rec_1", title="需求一")

    first = monitor.evaluate_requirements(
        "feishu", "oc_demo", [requirement], today=TODAY, suppress_days=3
    )
    assert len(first) == 1

    # same record+kind next day: suppressed
    second = monitor.evaluate_requirements(
        "feishu",
        "oc_demo",
        [requirement],
        today=date(2026, 7, 5),
        suppress_days=3,
    )
    assert second == []

    # after the suppression window it fires again
    third = monitor.evaluate_requirements(
        "feishu",
        "oc_demo",
        [requirement],
        today=date(2026, 7, 8),
        suppress_days=3,
    )
    assert len(third) == 1


class FakeRequirementConnector:
    def __init__(self, records: list[dict]) -> None:
        self.records = records

    async def list_fields(self, base_token: str, table_id: str):
        return {"data": {"fields": [{"name": "需求名称"}, {"name": "负责人"}, {"name": "需求状态"}]}}

    async def list_records(self, base_token: str, table_id: str, fields, limit: int):
        return {"data": {"records": self.records}}


class CapturingAdapter:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send_message(self, chat_id: str, text: str, *, delivery_id: str) -> None:
        self.sent.append((chat_id, text))


@pytest.mark.asyncio
async def test_scheduler_audits_decisions_to_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OSTAG_AMBIENT_ENABLED", "true")
    monkeypatch.setenv("OSTAG_AMBIENT_CHAT_IDS", "oc_demo")
    config = load_settings(
        None,
        audit_log=tmp_path / "audit.log",
        ambient_daily_digest_enabled=False,
    )
    store = _store(tmp_path, config)
    connector = FakeRequirementConnector(
        [
            {"record_id": "rec1", "fields": {"需求名称": "无主需求", "负责人": "", "需求状态": "待处理"}},
            {"record_id": "rec2", "fields": {"需求名称": "无状态", "负责人": "李四", "需求状态": ""}},
        ]
    )
    adapter = CapturingAdapter()
    scheduler = AmbientScheduler(
        config,
        adapter=adapter,
        channel_brain_store=store,
        requirement_connector=connector,
        today=lambda: TODAY,
    )

    result = await scheduler.run_once()

    assert result.sent == 1
    assert len(adapter.sent) == 1
    message = adapter.sent[0][1]
    assert message.startswith("主动巡检建议：\n")
    assert "无主需求" in message and "无状态" in message

    ledger = RunLedger(config.run_ledger_path)
    runs = ledger.list_runs()
    ambient_runs = [run for run in runs if run.get("executor_id") == "ambient"]
    assert len(ambient_runs) == 1
    assert ambient_runs[0]["status"] == "succeeded"
    events = ledger.export_run(ambient_runs[0]["run_id"])
    kinds = [
        event["payload"]["kind"]
        for event in events
        if event["event_type"] == "ambient_decision"
    ]
    assert kinds == ["unowned_requirement", "missing_status"]
    assert all(
        event["payload"]["proposal_only"] is True
        for event in events
        if event["event_type"] == "ambient_decision"
    )


async def test_cli_ambient_run_once_dry_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("OSTAG_RUN_LEDGER_PATH", str(tmp_path / "ledger.jsonl"))
    monkeypatch.setenv("OSTAG_AUDIT_LOG", str(tmp_path / "audit.log"))
    monkeypatch.setenv("OSTAG_MEMORY_ROOT", str(tmp_path / "brains"))
    # ambient disabled: sweep skips, exit 0, dry delivery reported
    exit_code = await main_async(["ambient", "run-once", "--json"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["delivery"] == "dry_run"
    assert payload["errors"] == 0
    assert payload["messages"] == []


async def test_cli_daily_board_once_collects_card(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class FakeScheduler:
        def __init__(self, config, *, adapter):
            self.adapter = adapter

        async def run_daily_board_once(self, *, force=True):
            assert force is True
            await self.adapter.send_card("oc_demo", {"schema": "2.0"})
            return AmbientRunResult(checked=1, sent=1)

    monkeypatch.setattr("ostag.main.AmbientScheduler", FakeScheduler)
    exit_code = await main_async(["ambient", "board-once", "--json"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["sent"] == 1
    assert payload["cards"] == [{"chat_id": "oc_demo", "card": {"schema": "2.0"}}]
