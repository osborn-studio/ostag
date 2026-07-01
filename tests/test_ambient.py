from __future__ import annotations

from datetime import date
from pathlib import Path

from ostag.config import load_settings
from ostag.core.ambient import (
    AmbientMonitor,
    AmbientScheduler,
    RequirementSnapshot,
    snapshots_from_records,
)
from ostag.core.channel_brain import ChannelBrain, ChannelBrainStore, ChannelPolicy
from ostag.core.event import NormalizedEvent
from ostag.core.runner import OSTagRunner


class FakeRequirementConnector:
    def __init__(self, response, fields_response=None):
        self.response = response
        self.fields_response = fields_response or {
            "data": {
                "fields": [
                    {"field_name": "需求编号"},
                    {"field_name": "需求名称"},
                    {"field_name": "需求描述"},
                    {"field_name": "负责人"},
                    {"field_name": "owner"},
                    {"field_name": "需求状态"},
                    {"field_name": "状态"},
                    {"field_name": "停滞天数"},
                ]
            }
        }
        self.calls = []

    async def list_fields(self, base_token, table_id):
        self.calls.append(("fields", base_token, table_id))
        return self.fields_response

    async def list_records(self, base_token, table_id, projection_fields, limit=50):
        self.calls.append(("records", base_token, table_id, tuple(projection_fields), limit))
        return self.response


class CapturingAmbientAdapter:
    platform = "feishu"

    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id: str, text: str) -> None:
        self.sent.append((chat_id, text))


def test_ambient_monitor_is_disabled_by_default(tmp_path: Path) -> None:
    config = load_settings(None, audit_log=tmp_path / "audit.log")
    store = ChannelBrainStore(config, root=tmp_path)
    monitor = AmbientMonitor(store)

    decisions = monitor.evaluate_requirements(
        "feishu",
        "oc_demo",
        [RequirementSnapshot(record_id="rec1", title="交易盯盘")],
        today=date(2026, 6, 30),
    )

    assert decisions == []


def test_ambient_monitor_proposes_without_write_when_enabled(tmp_path: Path) -> None:
    config = load_settings(None, audit_log=tmp_path / "audit.log")
    store = ChannelBrainStore(config, root=tmp_path)
    store.save(
        ChannelBrain(
            platform="feishu",
            chat_id="oc_demo",
            policy=ChannelPolicy(ambient_enabled=True),
        )
    )
    monitor = AmbientMonitor(store)

    decisions = monitor.evaluate_requirements(
        "feishu",
        "oc_demo",
        [RequirementSnapshot(record_id="rec1", title="交易盯盘")],
        today=date(2026, 6, 30),
    )

    assert len(decisions) == 1
    assert decisions[0].kind == "unowned_requirement"
    assert decisions[0].proposal_only is True


def test_ambient_monitor_rate_limits_daily_reminders(tmp_path: Path) -> None:
    config = load_settings(None, audit_log=tmp_path / "audit.log")
    store = ChannelBrainStore(config, root=tmp_path)
    store.save(
        ChannelBrain(
            platform="feishu",
            chat_id="oc_demo",
            policy=ChannelPolicy(ambient_enabled=True),
        )
    )
    monitor = AmbientMonitor(store, daily_limit=1)
    requirement = RequirementSnapshot(record_id="rec1", title="交易盯盘")

    assert monitor.evaluate_requirements(
        "feishu", "oc_demo", [requirement], today=date(2026, 6, 30)
    )
    assert (
        monitor.evaluate_requirements(
            "feishu", "oc_demo", [requirement], today=date(2026, 6, 30)
        )
        == []
    )


async def test_runner_reports_ambient_status(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("OSTAG_MEMORY_ROOT", str(tmp_path / "memory"))
    config = load_settings(None, audit_log=tmp_path / "audit.log")
    runner = OSTagRunner(config)
    event = NormalizedEvent(
        platform="feishu",
        event_id="evt",
        chat_id="oc_demo",
        message_id="om_demo",
        sender_id="ou_demo",
        text="主动巡检状态",
        mention_bot=True,
    )

    reply = await runner.handle_event(event)

    assert "Ambient 主动模式未开启" in reply
    assert "不会自动写表或创建任务" in reply


async def test_runner_reports_ambient_scheduler_status(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("OSTAG_MEMORY_ROOT", str(tmp_path / "memory"))
    config = load_settings(
        None,
        audit_log=tmp_path / "audit.log",
        ambient_enabled=True,
        ambient_chat_ids=("oc_demo",),
        ambient_interval_seconds=900,
    )
    store = ChannelBrainStore(config, root=tmp_path / "memory")
    store.save(
        ChannelBrain(
            platform="feishu",
            chat_id="oc_demo",
            policy=ChannelPolicy(ambient_enabled=True),
        )
    )
    runner = OSTagRunner(config)
    event = NormalizedEvent(
        platform="feishu",
        event_id="evt",
        chat_id="oc_demo",
        message_id="om_demo",
        sender_id="ou_demo",
        text="主动巡检状态",
        mention_bot=True,
    )

    reply = await runner.handle_event(event)

    assert "Ambient 主动模式已开启" in reply
    assert "后台调度已开启" in reply
    assert "巡检间隔：900 秒" in reply


def test_snapshots_from_records_extracts_owner_status_and_staleness() -> None:
    data = {
        "data": {
            "records": [
                {
                    "record_id": "rec1",
                    "fields": {
                        "需求名称": "交易盯盘",
                        "负责人": "",
                        "需求状态": "开发交付中",
                        "停滞天数": 9,
                    },
                },
                {
                    "record_id": "rec2",
                    "fields": {
                        "需求名称": "投研报告",
                        "负责人": "张三",
                        "需求状态": "已完成",
                        "停滞天数": 20,
                    },
                },
            ]
        }
    }

    snapshots = snapshots_from_records(data)

    assert snapshots == [
        RequirementSnapshot(
            record_id="rec1",
            title="交易盯盘",
            owner="",
            status="开发交付中",
            days_stale=9,
        ),
        RequirementSnapshot(
            record_id="rec2",
            title="投研报告",
            owner="张三",
            status="已完成",
            days_stale=20,
        ),
    ]


async def test_ambient_scheduler_sends_proposal_for_enabled_channel(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("OSTAG_AMBIENT_ENABLED", "true")
    monkeypatch.setenv("OSTAG_AMBIENT_CHAT_IDS", "oc_demo")
    config = load_settings(
        None,
        audit_log=tmp_path / "audit.log",
        auto_base_default_base_token="base_demo",
        auto_base_default_table_id="tblDemo",
    )
    store = ChannelBrainStore(config, root=tmp_path)
    store.save(
        ChannelBrain(
            platform="feishu",
            chat_id="oc_demo",
            policy=ChannelPolicy(
                default_base_token="base_channel",
                default_table_id="tblChannel",
                ambient_enabled=True,
            ),
        )
    )
    connector = FakeRequirementConnector(
        {
            "data": {
                "records": [
                    {
                        "record_id": "rec1",
                        "fields": {"需求名称": "交易盯盘", "负责人": "", "需求状态": "待处理"},
                    }
                ]
            }
        }
    )
    adapter = CapturingAmbientAdapter()
    scheduler = AmbientScheduler(
        config,
        adapter=adapter,
        channel_brain_store=store,
        requirement_connector=connector,
        today=lambda: date(2026, 7, 1),
    )

    result = await scheduler.run_once()

    assert result.sent == 1
    assert connector.calls == [
        ("fields", "base_channel", "tblChannel"),
        (
            "records",
            "base_channel",
            "tblChannel",
            ("需求编号", "需求名称", "需求描述", "负责人", "owner", "需求状态", "状态", "停滞天数"),
            50,
        )
    ]
    assert adapter.sent == [
        ("oc_demo", "主动巡检建议：需求「交易盯盘」还没有负责人，建议指定 owner。")
    ]


async def test_ambient_scheduler_projects_only_existing_fields(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("OSTAG_AMBIENT_ENABLED", "true")
    monkeypatch.setenv("OSTAG_AMBIENT_CHAT_IDS", "oc_demo")
    config = load_settings(
        None,
        audit_log=tmp_path / "audit.log",
        auto_base_default_base_token="base_demo",
        auto_base_default_table_id="tblDemo",
    )
    store = ChannelBrainStore(config, root=tmp_path)
    store.save(
        ChannelBrain(
            platform="feishu",
            chat_id="oc_demo",
            policy=ChannelPolicy(ambient_enabled=True),
        )
    )
    connector = FakeRequirementConnector(
        {"data": {"records": []}},
        fields_response={
            "data": {
                "fields": [
                    {"field_name": "需求名称"},
                    {"field_name": "负责人"},
                    {"field_name": "需求状态"},
                ]
            }
        },
    )
    scheduler = AmbientScheduler(
        config,
        adapter=CapturingAmbientAdapter(),
        channel_brain_store=store,
        requirement_connector=connector,
    )

    await scheduler.run_once()

    assert connector.calls[-1] == (
        "records",
        "base_demo",
        "tblDemo",
        ("需求名称", "负责人", "需求状态"),
        50,
    )


async def test_ambient_scheduler_is_disabled_by_config(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("OSTAG_AMBIENT_ENABLED", raising=False)
    monkeypatch.setenv("OSTAG_AMBIENT_CHAT_IDS", "oc_demo")
    config = load_settings(None, audit_log=tmp_path / "audit.log")
    scheduler = AmbientScheduler(
        config,
        adapter=CapturingAmbientAdapter(),
        channel_brain_store=ChannelBrainStore(config, root=tmp_path),
        requirement_connector=FakeRequirementConnector({"data": {"records": []}}),
    )

    result = await scheduler.run_once()

    assert result.sent == 0
    assert result.skipped == 1
