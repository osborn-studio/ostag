from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ostag.copilot_relay.status_board import (
    OsstackStatusBoardService,
    format_osstack_status_text,
)
from ostag.copilot_relay.store import RelayStore
from ostag.core.identity import Principal
from ostag.work_monitor.identity import Destination, IdentityStore
from ostag.work_monitor.store import MonitorStore


OWNER = Principal("feishu", "owner")
EMPLOYEE = Principal("feishu", "employee")
NOW = datetime(2026, 7, 15, 1, 0, tzinfo=UTC)


def _service(tmp_path: Path):
    monitor = MonitorStore(tmp_path / "monitor.sqlite3")
    relay = RelayStore(monitor)
    identities = IdentityStore(monitor.path)
    identities.bind_private(
        owner=OWNER,
        destination=Destination("feishu", "owner-private", "private"),
        local_admin=True,
    )
    identities.bind_private(
        owner=EMPLOYEE,
        destination=Destination("feishu", "employee-private", "private"),
        local_admin=True,
        role="member",
    )
    identities.bind_instance(
        OWNER,
        "owner-mac",
        display_name="我的 Mac mini",
        bound_at=NOW.isoformat(),
    )
    identities.bind_instance(
        EMPLOYEE,
        "employee-mac",
        display_name="员工 Mac",
        bound_at=NOW.isoformat(),
    )
    with monitor.transaction() as connection:
        connection.executemany(
            "INSERT INTO instances(instance_id,last_sequence,last_seen_at) VALUES (?,?,?)",
            (
                ("owner-mac", 2, (NOW - timedelta(seconds=30)).isoformat()),
                ("employee-mac", 3, (NOW - timedelta(seconds=45)).isoformat()),
            ),
        )
        connection.executemany(
            "INSERT INTO relay_sessions(instance_id,session_id,task_id,run_id,agent_id,"
            "title,state,session_version,last_turn_id,latest_summary,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                (
                    "owner-mac",
                    "owner-session",
                    "owner-task",
                    "owner-run",
                    "codex-cli",
                    "OStag 状态看板",
                    "running",
                    2,
                    "turn-owner",
                    "正在实现状态聚合",
                    (NOW - timedelta(minutes=2)).isoformat(),
                ),
                (
                    "employee-mac",
                    "decision-session",
                    "decision-task",
                    "decision-run",
                    "claude-code",
                    "员工等待决策",
                    "idle",
                    3,
                    "turn-decision",
                    "等待员工选择方案",
                    (NOW - timedelta(minutes=1)).isoformat(),
                ),
                (
                    "employee-mac",
                    "blocked-session",
                    "blocked-task",
                    "blocked-run",
                    "codex-cli",
                    "员工阻塞任务",
                    "blocked",
                    1,
                    "turn-blocked",
                    "缺少测试账号",
                    (NOW - timedelta(minutes=3)).isoformat(),
                ),
            ),
        )
        connection.execute(
            "INSERT INTO relay_decisions(decision_id,instance_id,session_id,turn_id,"
            "session_version,question,choices_json,state,expires_at) "
            "VALUES ('decision-1','employee-mac','decision-session','turn-decision',3,"
            "'选择方案','[]','pending',?)",
            ((NOW + timedelta(hours=1)).isoformat(),),
        )
        connection.executemany(
            "INSERT INTO tasks(instance_id,task_id,title,project,status,agent,progress,"
            "duration_seconds,summary,error_class,evidence_ref,occurred_at,version) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                (
                    "owner-mac",
                    "done-today",
                    "已完成",
                    "ostag",
                    "done",
                    "codex-cli",
                    100,
                    60,
                    "完成",
                    None,
                    None,
                    (NOW - timedelta(minutes=10)).isoformat(),
                    1,
                ),
                (
                    "owner-mac",
                    "running-now",
                    "运行中",
                    "ostag",
                    "running",
                    "codex-cli",
                    50,
                    60,
                    "进行中",
                    None,
                    None,
                    NOW.isoformat(),
                    1,
                ),
            ),
        )
    relay.select_current_task(
        "feishu",
        "owner",
        "owner-mac",
        "owner-session",
        selected_at=NOW.isoformat(),
    )
    current = [NOW]
    service = OsstackStatusBoardService(
        relay,
        identities,
        owner=OWNER,
        clock=lambda: current[0],
        offline_seconds=600,
    )
    return service, relay, current


def test_personal_board_shows_exact_device_agent_and_current_session(
    tmp_path: Path,
) -> None:
    service, relay, _ = _service(tmp_path)

    view = service.build(OWNER, scope="personal")

    assert view.scope == "personal"
    assert (view.device_count, view.online_count) == (1, 1)
    assert (view.running_count, view.waiting_decision_count, view.blocked_count) == (
        1,
        0,
        0,
    )
    assert view.completed_today_count == 1
    assert view.current_session is not None
    assert view.current_session.device_name == "我的 Mac mini"
    assert view.current_session.agent_id == "codex-cli"
    assert view.current_session.session_id == "owner-session"
    assert all(item.instance_id == "owner-mac" for item in view.sessions)
    with relay.monitor_store.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM relay_messages").fetchone()[0] == 0


def test_owner_team_board_prioritizes_decisions_blockers_and_running(
    tmp_path: Path,
) -> None:
    service, _, _ = _service(tmp_path)

    team = service.build(OWNER, scope="team")

    assert (team.device_count, team.online_count) == (2, 2)
    assert [item.session_id for item in team.sessions] == [
        "decision-session",
        "blocked-session",
        "owner-session",
    ]
    assert team.waiting_decision_count == 1
    assert team.blocked_count == 1
    assert [item.session_id for item in team.attention] == [
        "decision-session",
        "blocked-session",
    ]
    assert "北京时间" in format_osstack_status_text(team)
    assert "只读状态查询" in format_osstack_status_text(team)


def test_member_cannot_request_team_scope_and_offline_is_explicit(
    tmp_path: Path,
) -> None:
    service, _, current = _service(tmp_path)

    employee = service.build(EMPLOYEE, scope="personal")
    assert {item.instance_id for item in employee.sessions} == {"employee-mac"}
    with pytest.raises(PermissionError, match="team"):
        service.build(EMPLOYEE, scope="team")

    current[0] += timedelta(seconds=601)
    offline = service.build(OWNER, scope="personal")
    assert offline.online_count == 0
    assert offline.stale is True
    assert "离线" in format_osstack_status_text(offline)


def test_unbound_member_gets_empty_non_leaking_view(tmp_path: Path) -> None:
    service, _, _ = _service(tmp_path)

    view = service.build(Principal("feishu", "stranger"), scope="personal")

    assert view.device_count == 0
    assert view.sessions == ()
    assert "尚未绑定 osstack" in format_osstack_status_text(view)
    assert "owner-mac" not in format_osstack_status_text(view)
