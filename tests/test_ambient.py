from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

from ostag.config import load_settings
from ostag.core.ambient import (
    AmbientMonitor,
    AmbientScheduler,
    RequirementSnapshot,
    format_daily_digest,
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
                    {"field_name": "需求负责人"},
                    {"field_name": "owner"},
                    {"field_name": "需求状态"},
                    {"field_name": "状态"},
                    {"field_name": "优先级"},
                    {"field_name": "预计/实际完成时间"},
                    {"field_name": "停滞天数"},
                    {"field_name": "需求持续时长"},
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
        ambient_daily_digest_enabled=True,
        ambient_morning_digest_hour=9,
        ambient_evening_digest_hour=18,
        ambient_manager_open_ids=("ou_manager",),
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
    assert "每日提醒：09:00 / 18:00" in reply
    assert "管理者：1 人" in reply


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


def test_snapshots_from_records_extracts_feishu_user_ids_and_due_date() -> None:
    data = {
        "data": {
            "data": [
                [
                    "读取和展示QD资产分配值",
                    [{"id": "ou_owner", "name": "葛德彬"}],
                    ["方案设计中"],
                    ["高 - P0"],
                    "2026-07-01 00:00:00",
                    "5",
                ]
            ],
            "fields": [
                "需求描述",
                "需求负责人",
                "需求状态",
                "优先级",
                "预计/实际完成时间",
                "需求持续时长",
            ],
            "record_id_list": ["rec_qd"],
        }
    }

    snapshots = snapshots_from_records(data)

    assert snapshots == [
        RequirementSnapshot(
            record_id="rec_qd",
            title="读取和展示QD资产分配值",
            owner="葛德彬",
            owner_ids=("ou_owner",),
            status="方案设计中",
            priority="高 - P0",
            due_date=date(2026, 7, 1),
            days_stale=5,
        )
    ]


def test_format_morning_daily_digest_groups_due_tasks_and_mentions_manager() -> None:
    tasks = [
        RequirementSnapshot(
            record_id="rec1",
            title="读取和展示QD资产分配值",
            owner="葛德彬",
            owner_ids=("ou_owner",),
            status="方案设计中",
            priority="高 - P0",
            due_date=date(2026, 7, 1),
            days_stale=5,
        ),
        RequirementSnapshot(
            record_id="rec2",
            title="证券字段中可自定义新增字段和内容",
            owner="左士文",
            owner_ids=("ou_left",),
            status="开发交付中",
            priority="中 - P1",
            due_date=date(2026, 7, 2),
        ),
    ]

    text = format_daily_digest(
        "morning",
        tasks,
        today=date(2026, 7, 1),
        manager_ids=("ou_manager",),
    )

    assert "<at user_id=\"ou_manager\">管理者</at>" in text
    assert "<at user_id=\"ou_owner\">葛德彬</at>" in text
    assert "读取和展示QD资产分配值" in text
    assert "证券字段中可自定义新增字段和内容" not in text
    assert "今日重点任务" in text


def test_format_evening_daily_digest_summarizes_done_and_open_tasks() -> None:
    tasks = [
        RequirementSnapshot(
            record_id="rec1",
            title="读取和展示QD资产分配值",
            owner="葛德彬",
            owner_ids=("ou_owner",),
            status="方案设计中",
            priority="高 - P0",
            due_date=date(2026, 7, 1),
        ),
        RequirementSnapshot(
            record_id="rec2",
            title="证券字段中可自定义新增字段和内容",
            owner="左士文",
            owner_ids=("ou_left",),
            status="已完成",
            priority="中 - P1",
            due_date=date(2026, 7, 1),
        ),
    ]

    text = format_daily_digest(
        "evening",
        tasks,
        today=date(2026, 7, 1),
        manager_ids=("ou_manager",),
    )

    assert "今日完成" in text
    assert "仍未完成" in text
    assert "读取和展示QD资产分配值" in text
    assert "证券字段中可自定义新增字段和内容" in text
    assert "<at user_id=\"ou_owner\">葛德彬</at>" in text
    assert "<at user_id=\"ou_left\">左士文</at>" in text


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
        ambient_daily_digest_enabled=False,
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
            (
                "需求编号",
                "需求名称",
                "需求描述",
                "负责人",
                "需求负责人",
                "owner",
                "需求状态",
                "状态",
                "优先级",
                "预计/实际完成时间",
                "停滞天数",
                "需求持续时长",
            ),
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
        ambient_daily_digest_enabled=False,
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


async def test_ambient_scheduler_sends_morning_digest_once_per_day(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("OSTAG_AMBIENT_ENABLED", "true")
    monkeypatch.setenv("OSTAG_AMBIENT_CHAT_IDS", "oc_demo")
    config = load_settings(
        None,
        audit_log=tmp_path / "audit.log",
        auto_base_default_base_token="base_demo",
        auto_base_default_table_id="tblDemo",
        ambient_daily_digest_enabled=True,
        ambient_morning_digest_hour=9,
        ambient_manager_open_ids=("ou_manager",),
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
        {
            "data": {
                "data": [
                    [
                        "读取和展示QD资产分配值",
                        [{"id": "ou_owner", "name": "葛德彬"}],
                        ["方案设计中"],
                        ["高 - P0"],
                        "2026-07-01 00:00:00",
                        "5",
                    ]
                ],
                "fields": [
                    "需求描述",
                    "需求负责人",
                    "需求状态",
                    "优先级",
                    "预计/实际完成时间",
                    "需求持续时长",
                ],
                "record_id_list": ["rec_qd"],
            }
        }
    )
    adapter = CapturingAmbientAdapter()
    scheduler = AmbientScheduler(
        config,
        adapter=adapter,
        channel_brain_store=store,
        requirement_connector=connector,
        now=lambda: datetime(2026, 7, 1, 9, 5),
    )

    first = await scheduler.run_once()
    second = await scheduler.run_once()

    assert first.sent == 1
    assert second.sent == 0
    assert len(adapter.sent) == 1
    assert "今日重点任务" in adapter.sent[0][1]
    assert "<at user_id=\"ou_manager\">管理者</at>" in adapter.sent[0][1]


async def test_daily_digest_mode_suppresses_continuous_proposals(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("OSTAG_AMBIENT_ENABLED", "true")
    monkeypatch.setenv("OSTAG_AMBIENT_CHAT_IDS", "oc_demo")
    config = load_settings(
        None,
        audit_log=tmp_path / "audit.log",
        auto_base_default_base_token="base_demo",
        auto_base_default_table_id="tblDemo",
        ambient_daily_digest_enabled=True,
    )
    store = ChannelBrainStore(config, root=tmp_path)
    store.save(
        ChannelBrain(
            platform="feishu",
            chat_id="oc_demo",
            policy=ChannelPolicy(ambient_enabled=True),
        )
    )
    adapter = CapturingAmbientAdapter()
    scheduler = AmbientScheduler(
        config,
        adapter=adapter,
        channel_brain_store=store,
        requirement_connector=FakeRequirementConnector(
            {
                "data": {
                    "records": [
                        {
                            "record_id": "rec1",
                            "fields": {"需求名称": "交易盯盘", "负责人": ""},
                        }
                    ]
                }
            }
        ),
        now=lambda: datetime(2026, 7, 1, 15, 0),
    )

    result = await scheduler.run_once()

    assert result.sent == 0
    assert adapter.sent == []


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
