from __future__ import annotations

from datetime import date
from pathlib import Path

from ostag.config import load_settings
from ostag.core.ambient import AmbientMonitor, RequirementSnapshot
from ostag.core.channel_brain import ChannelBrain, ChannelBrainStore, ChannelPolicy
from ostag.core.event import NormalizedEvent
from ostag.core.runner import OSTagRunner


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
