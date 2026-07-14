import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ostag.work_monitor.daily_digest_scheduler import (
    DailyDigestProfile,
    DailyRequirementBoardProfile,
    DailyTaskBoardScheduler,
)
from ostag.work_monitor.store import MonitorStore


class Connector:
    def __init__(self, *, fails: bool = False):
        self.fails = fails
        self.calls = 0

    async def list_records(self, base_token, table_id, fields, limit=50):
        self.calls += 1
        if self.fails:
            raise RuntimeError("source unavailable")
        return {
            "data": {
                "records": [
                    {
                        "record_id": f"{base_token}-task",
                        "fields": {
                            "需求名称": f"{base_token} task",
                            "负责人": [{"id": "owner", "name": "Owner"}],
                            "需求状态": "待处理",
                            "优先级": "高 - P0",
                            "预计/实际完成时间": "2026-07-13",
                        },
                    }
                ]
            }
        }


class Transport:
    def __init__(self, *, fail_times: int = 0):
        self.fail_times = fail_times
        self.sent = []

    async def send_card(self, *args, **kwargs):
        if self.fail_times:
            self.fail_times -= 1
            raise RuntimeError("delivery unavailable")
        self.sent.append((args, kwargs))


def _profile(platform: str, connector, transport) -> DailyDigestProfile:
    return DailyDigestProfile(
        platform=platform,
        destination_id=f"{platform}-group",
        base_token=f"{platform}-base",
        table_id=f"{platform}-table",
        table_url=f"https://example.test/{platform}",
        manager_ids=("manager",),
        connector=connector,
        transport=transport,
    )


def _requirement_profile(connector, transport) -> DailyRequirementBoardProfile:
    return DailyRequirementBoardProfile(
        destination_id="oc_fde",
        base_token="base_fde",
        table_id="tbl_fde",
        table_url="https://example.test/fde",
        group_name="Dadao 投研交易 FDE",
        connector=connector,
        transport=transport,
        record_limit=200,
    )


@pytest.mark.asyncio
async def test_noon_requirement_board_uses_durable_window_and_read_only_connector(
    tmp_path: Path,
) -> None:
    store = MonitorStore(tmp_path / "monitor.sqlite3")
    now = datetime(2026, 7, 18, 4, 5, tzinfo=UTC)
    connector, transport = Connector(), Transport()
    profile = _requirement_profile(connector, transport)

    await DailyTaskBoardScheduler(
        store, (), requirement_profiles=(profile,), clock=lambda: now
    ).tick()
    await DailyTaskBoardScheduler(
        store, (), requirement_profiles=(profile,), clock=lambda: now
    ).tick()

    assert connector.calls == 1
    assert len(transport.sent) == 1
    blob = json.dumps(transport.sent[0], ensure_ascii=False)
    assert "OStag 每日需求看板" in blob
    assert "12:00（北京时间）" in blob
    with store.connect() as connection:
        row = connection.execute(
            "SELECT window_key,state FROM daily_digest_outbox WHERE window_key LIKE '%requirement_noon%'"
        ).fetchone()
    assert row["state"] == "delivered"


@pytest.mark.asyncio
async def test_morning_profiles_send_once_and_restart_deduplicates(tmp_path: Path) -> None:
    store = MonitorStore(tmp_path / "monitor.sqlite3")
    now = datetime(2026, 7, 13, 1, 5, tzinfo=UTC)
    feishu, dingtalk = Transport(), Transport()
    profiles = (
        _profile("feishu", Connector(), feishu),
        _profile("dingtalk", Connector(), dingtalk),
    )

    await DailyTaskBoardScheduler(store, profiles, clock=lambda: now).tick()
    await DailyTaskBoardScheduler(store, profiles, clock=lambda: now).tick()

    assert len(feishu.sent) == len(dingtalk.sent) == 1
    assert "今日作战看板" in json.dumps(feishu.sent[0], ensure_ascii=False)
    assert "今日作战看板" in json.dumps(dingtalk.sent[0], ensure_ascii=False)
    with store.connect() as connection:
        windows = connection.execute(
            "SELECT window_key,delivered_at FROM daily_digest_windows ORDER BY window_key"
        ).fetchall()
    assert len(windows) == 2
    assert all(row["delivered_at"] for row in windows)


@pytest.mark.asyncio
async def test_one_connector_failure_does_not_block_other_platform(tmp_path: Path) -> None:
    store = MonitorStore(tmp_path / "monitor.sqlite3")
    now = datetime(2026, 7, 13, 1, 5, tzinfo=UTC)
    feishu, dingtalk = Transport(), Transport()
    profiles = (
        _profile("feishu", Connector(fails=True), feishu),
        _profile("dingtalk", Connector(), dingtalk),
    )

    await DailyTaskBoardScheduler(store, profiles, clock=lambda: now).tick()

    assert feishu.sent == []
    assert len(dingtalk.sent) == 1


@pytest.mark.asyncio
async def test_evening_catchup_and_retry_use_stable_delivery_id(tmp_path: Path) -> None:
    store = MonitorStore(tmp_path / "monitor.sqlite3")
    current = [datetime(2026, 7, 13, 10, 5, tzinfo=UTC)]
    transport = Transport(fail_times=1)
    profile = _profile("feishu", Connector(), transport)
    scheduler = DailyTaskBoardScheduler(store, (profile,), clock=lambda: current[0])

    await scheduler.tick()
    with store.connect() as connection:
        rows = connection.execute(
            "SELECT delivery_id,state,attempts,next_attempt_at FROM daily_digest_outbox "
            "ORDER BY window_key"
        ).fetchall()
    assert len(rows) == 2
    pending = next(row for row in rows if row["state"] == "pending")
    assert pending["attempts"] == 1
    first_delivery_id = pending["delivery_id"]

    current[0] += timedelta(minutes=1, seconds=1)
    await scheduler.tick()

    with store.connect() as connection:
        retried = connection.execute(
            "SELECT delivery_id,state,attempts FROM daily_digest_outbox WHERE delivery_id=?",
            (first_delivery_id,),
        ).fetchone()
    assert retried["delivery_id"] == first_delivery_id
    assert retried["state"] == "delivered"
    assert any("今日复盘看板" in json.dumps(item, ensure_ascii=False) for item in transport.sent)


@pytest.mark.asyncio
async def test_disabled_profile_suppresses_queued_delivery(tmp_path: Path) -> None:
    store = MonitorStore(tmp_path / "monitor.sqlite3")
    now = datetime(2026, 7, 13, 1, 5, tzinfo=UTC)
    transport = Transport(fail_times=1)
    profile = _profile("feishu", Connector(), transport)
    scheduler = DailyTaskBoardScheduler(store, (profile,), clock=lambda: now)
    await scheduler.tick()

    scheduler.profiles = ()
    await scheduler.drain_once()

    with store.connect() as connection:
        state = connection.execute(
            "SELECT state FROM daily_digest_outbox"
        ).fetchone()["state"]
    assert state == "suppressed"
