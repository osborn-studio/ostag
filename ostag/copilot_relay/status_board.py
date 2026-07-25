"""Identity-scoped, read-only osstack status board aggregation."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from zoneinfo import ZoneInfo

from ostag.core.identity import Principal
from ostag.work_monitor.identity import IdentityStore

from .store import RelayStore


BEIJING = ZoneInfo("Asia/Shanghai")
BoardScope = Literal["personal", "team"]


@dataclass(frozen=True)
class StatusSessionItem:
    instance_id: str
    session_id: str
    device_name: str
    agent_id: str
    title: str
    state: str
    latest_summary: str
    updated_at: str
    online: bool
    waiting_decision: bool
    runtime_id: str = "legacy"


@dataclass(frozen=True)
class RuntimeStatusItem:
    runtime_id: str
    agent_type: str
    state: str
    session_count: int
    updated_at: str


@dataclass(frozen=True)
class DeviceStatusItem:
    device_id: str
    display_name: str
    connection_state: str
    last_seen_at: str
    runtimes: tuple[RuntimeStatusItem, ...]
    sessions: tuple[StatusSessionItem, ...]
    attention_count: int


@dataclass(frozen=True)
class OsstackStatusBoardView:
    scope: BoardScope
    generated_at: str
    device_count: int
    online_count: int
    running_count: int
    waiting_decision_count: int
    blocked_count: int
    completed_today_count: int
    current_session: StatusSessionItem | None
    sessions: tuple[StatusSessionItem, ...]
    attention: tuple[StatusSessionItem, ...]
    stale: bool
    devices: tuple[DeviceStatusItem, ...] = ()


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.removesuffix("Z") + ("+00:00" if value.endswith("Z") else ""))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _beijing_time(value: str) -> str:
    return _parse_timestamp(value).astimezone(BEIJING).strftime("%Y-%m-%d %H:%M 北京时间")


def _rank(item: StatusSessionItem) -> tuple[int, float]:
    priority = (
        0
        if item.waiting_decision
        else {"blocked": 1, "running": 2, "idle": 3, "completed": 4}.get(
            item.state, 5
        )
    )
    return priority, -_parse_timestamp(item.updated_at).timestamp()


def _connection_state(last_seen_at: str, now: datetime) -> str:
    if not last_seen_at:
        return "offline"
    age = max(0.0, (now - _parse_timestamp(last_seen_at)).total_seconds())
    if age < 30:
        return "online"
    if age < 120:
        return "degraded"
    return "offline"


def _device_rank(item: DeviceStatusItem) -> tuple[int, float, str]:
    if any(session.waiting_decision for session in item.sessions):
        priority = 0
    elif any(session.state == "blocked" for session in item.sessions):
        priority = 1
    elif item.connection_state == "degraded":
        priority = 2
    elif any(session.state == "running" for session in item.sessions):
        priority = 3
    elif item.connection_state == "online":
        priority = 4
    else:
        priority = 5
    seen = _parse_timestamp(item.last_seen_at).timestamp() if item.last_seen_at else 0.0
    return priority, -seen, item.device_id


class OsstackStatusBoardService:
    """Build dashboard views from central projections without enqueueing work."""

    def __init__(
        self,
        store: RelayStore,
        identities: IdentityStore,
        *,
        owner: Principal,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        offline_seconds: int = 600,
    ) -> None:
        if offline_seconds <= 0:
            raise ValueError("offline_seconds must be positive")
        self.store = store
        self.identities = identities
        self.owner = owner
        self.clock = clock
        self.offline_seconds = offline_seconds

    def _allowed(self, principal: Principal, scope: BoardScope) -> frozenset[str]:
        if scope == "team":
            if principal != self.owner or not self.identities.is_global_owner(principal):
                raise PermissionError("team status requires global owner")
            legacy = self.identities.team_instances()
            query = "SELECT device_id FROM monitor_devices WHERE revoked_at=''"
            parameters: tuple[str, ...] = ()
        elif scope == "personal":
            legacy = self.identities.personal_instances(principal)
            query = (
                "SELECT device_id FROM monitor_devices WHERE principal_platform=? "
                "AND actor_id=? AND revoked_at=''"
            )
            parameters = (principal.platform, principal.actor_id)
        else:
            raise ValueError("status board scope must be personal or team")
        with self.store.monitor_store.connect() as connection:
            registered = connection.execute(query, parameters).fetchall()
        return frozenset(legacy | {str(row["device_id"]) for row in registered})

    def build(
        self, principal: Principal, *, scope: BoardScope = "personal"
    ) -> OsstackStatusBoardView:
        allowed = self._allowed(principal, scope)
        now = self.clock()
        if not allowed:
            return OsstackStatusBoardView(
                scope=scope,
                generated_at=now.isoformat(),
                device_count=0,
                online_count=0,
                running_count=0,
                waiting_decision_count=0,
                blocked_count=0,
                completed_today_count=0,
                current_session=None,
                sessions=(),
                attention=(),
                stale=False,
                devices=(),
            )

        ids = tuple(sorted(allowed))
        placeholders = ",".join("?" for _ in ids)
        with self.store.monitor_store.connect() as connection:
            session_rows = connection.execute(
                f"SELECT * FROM relay_sessions "  # noqa: S608
                f"WHERE instance_id IN ({placeholders})",
                ids,
            ).fetchall()
            decision_rows = connection.execute(
                "SELECT instance_id,session_id FROM relay_decisions WHERE state='pending' "
                f"AND instance_id IN ({placeholders})",  # noqa: S608
                ids,
            ).fetchall()
            instance_rows = connection.execute(
                f"SELECT instance_id,last_seen_at FROM instances "  # noqa: S608
                f"WHERE instance_id IN ({placeholders})",
                ids,
            ).fetchall()
            device_rows = connection.execute(
                f"SELECT device_id,display_name,last_seen_at FROM monitor_devices "  # noqa: S608
                f"WHERE revoked_at='' AND device_id IN ({placeholders})",
                ids,
            ).fetchall()
            runtime_rows = connection.execute(
                f"SELECT device_id,runtime_id,agent_type,state,session_count,updated_at "  # noqa: S608
                f"FROM monitor_device_runtimes WHERE device_id IN ({placeholders})",
                ids,
            ).fetchall()
            task_rows = connection.execute(
                f"SELECT status,occurred_at FROM tasks "  # noqa: S608
                f"WHERE instance_id IN ({placeholders})",
                ids,
            ).fetchall()

        pending = {
            (str(row["instance_id"]), str(row["session_id"]))
            for row in decision_rows
        }
        legacy_last_seen = {
            str(row["instance_id"]): str(row["last_seen_at"] or "")
            for row in instance_rows
        }
        registered = {str(row["device_id"]): row for row in device_rows}
        last_seen = {
            device_id: str(registered[device_id]["last_seen_at"] or "")
            if device_id in registered
            else legacy_last_seen.get(device_id, "")
            for device_id in ids
        }
        names = self.identities.instance_display_names(frozenset(ids))
        names.update(
            {
                str(row["device_id"]): str(row["display_name"] or row["device_id"])
                for row in device_rows
            }
        )
        states = {
            device_id: _connection_state(last_seen[device_id], now)
            for device_id in ids
        }

        projected = tuple(
            StatusSessionItem(
                instance_id=str(row["instance_id"]),
                session_id=str(row["session_id"]),
                device_name=names.get(
                    str(row["instance_id"]), str(row["instance_id"])
                ),
                agent_id=str(row["agent_id"]),
                title=str(row["title"]),
                state=str(row["state"]),
                latest_summary=str(row["latest_summary"]),
                updated_at=str(row["updated_at"]),
                online=states[str(row["instance_id"])] == "online",
                waiting_decision=(
                    str(row["instance_id"]), str(row["session_id"])
                )
                in pending,
                runtime_id=str(row["runtime_id"]),
            )
            for row in session_rows
        )
        ordered = tuple(sorted(projected, key=_rank))
        selected = self.store.current_session(principal.platform, principal.actor_id)
        current = next(
            (
                item
                for item in ordered
                if selected is not None
                and item.instance_id == selected.instance_id
                and item.runtime_id == selected.runtime_id
                and item.session_id == selected.session_id
            ),
            ordered[0] if ordered else None,
        )
        today = now.astimezone(BEIJING).date()
        completed_today = sum(
            str(row["status"]).casefold() in {"done", "completed"}
            and _parse_timestamp(str(row["occurred_at"])).astimezone(BEIJING).date()
            == today
            for row in task_rows
        )
        attention = tuple(
            item
            for item in ordered
            if item.waiting_decision or item.state == "blocked"
        )[:3]
        runtimes_by_device: dict[str, list[RuntimeStatusItem]] = {
            device_id: [] for device_id in ids
        }
        for row in runtime_rows:
            runtimes_by_device[str(row["device_id"])].append(
                RuntimeStatusItem(
                    runtime_id=str(row["runtime_id"]),
                    agent_type=str(row["agent_type"]),
                    state=str(row["state"]),
                    session_count=int(row["session_count"]),
                    updated_at=str(row["updated_at"]),
                )
            )
        sessions_by_device = {
            device_id: tuple(
                item for item in ordered if item.instance_id == device_id
            )[:10]
            for device_id in ids
        }
        devices = tuple(
            sorted(
                (
                    DeviceStatusItem(
                        device_id=device_id,
                        display_name=names.get(device_id, device_id),
                        connection_state=states[device_id],
                        last_seen_at=last_seen[device_id],
                        runtimes=tuple(
                            sorted(
                                runtimes_by_device[device_id],
                                key=lambda runtime: runtime.runtime_id,
                            )
                        )[:10],
                        sessions=sessions_by_device[device_id],
                        attention_count=sum(
                            session.waiting_decision or session.state == "blocked"
                            for session in sessions_by_device[device_id]
                        ),
                    )
                    for device_id in ids
                ),
                key=_device_rank,
            )
        )[:20]
        return OsstackStatusBoardView(
            scope=scope,
            generated_at=now.isoformat(),
            device_count=len(ids),
            online_count=sum(state == "online" for state in states.values()),
            running_count=sum(item.state == "running" for item in ordered),
            waiting_decision_count=sum(item.waiting_decision for item in ordered),
            blocked_count=sum(item.state == "blocked" for item in ordered),
            completed_today_count=completed_today,
            current_session=current,
            sessions=ordered[:4],
            attention=attention,
            stale=not all(state == "online" for state in states.values()),
            devices=devices,
        )


def format_osstack_status_text(view: OsstackStatusBoardView) -> str:
    """Stable fallback when Feishu cannot accept the structured card."""
    scope = "团队总览" if view.scope == "team" else "我的设备"
    lines = [
        f"OStag · osstack 实时进度看板（{scope}）",
        f"在线 {view.online_count}/{view.device_count}｜运行中 {view.running_count}｜"
        f"等你决策 {view.waiting_decision_count}｜阻塞 {view.blocked_count}｜"
        f"今日完成 {view.completed_today_count}",
    ]
    if view.device_count == 0:
        lines.append("尚未绑定 osstack，请联系管理员绑定设备。")
    state_labels = {"online": "在线", "degraded": "连接不稳", "offline": "离线"}
    for device in view.devices:
        device_state = state_labels.get(device.connection_state, "未知")
        if not device.sessions:
            lines.append(f"- {device.display_name}｜{device_state}｜暂无活跃会话")
            continue
        lines.append(f"- {device.display_name}｜{device_state}")
        for item in device.sessions[:4]:
            state = "等待决策" if item.waiting_decision else item.state
            lines.append(
                f"  {item.title}｜Agent {item.agent_id}｜{state}｜"
                f"{_beijing_time(item.updated_at)}"
            )
            if item.latest_summary:
                lines.append(f"  最近进展：{item.latest_summary[:600]}")
    lines.append(f"只读状态查询｜生成于 {_beijing_time(view.generated_at)}")
    if view.stale:
        lines.append("数据可能已过期。")
    return "\n".join(lines)
