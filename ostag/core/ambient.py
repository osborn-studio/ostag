from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Callable

from ostag.adapters.base import Adapter
from ostag.config import Settings
from ostag.connectors.requirements import (
    DwsCLI,
    DwsRequirementTableConnector,
    LarkCLIRequirementTableConnector,
    RequirementTableConnector,
)
from ostag.core.channel_brain import ChannelBrainStore
from ostag.core.event import NormalizedEvent
from ostag.core.followups import FollowUpService
from ostag.core.run_ledger import RunLedger
from ostag.core.logging import write_audit_log
from ostag.workflows.auto_base import LarkCLI, cell_to_text, extract_matching_records


AMBIENT_PROJECTION_FIELDS = [
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
]

DONE_STATUSES = {"已完成", "完成", "done", "completed"}


@dataclass(frozen=True)
class RequirementSnapshot:
    record_id: str
    title: str
    owner: str = ""
    owner_ids: tuple[str, ...] = ()
    status: str = ""
    priority: str = ""
    due_date: date | None = None
    days_stale: int = 0


@dataclass(frozen=True)
class AmbientDecision:
    kind: str
    message: str
    proposal_only: bool = True


@dataclass(frozen=True)
class AmbientRunResult:
    checked: int = 0
    sent: int = 0
    skipped: int = 0
    errors: int = 0


class AmbientMonitor:
    def __init__(self, store: ChannelBrainStore, *, daily_limit: int = 3):
        self.store = store
        self.daily_limit = daily_limit

    def evaluate_requirements(
        self,
        platform: str,
        chat_id: str,
        requirements: list[RequirementSnapshot],
        *,
        today: date,
    ) -> list[AmbientDecision]:
        brain = self.store.load(platform, chat_id)
        if not brain.policy.ambient_enabled:
            return []
        if self._ambient_count(platform, chat_id, today) >= self.daily_limit:
            return []

        decisions: list[AmbientDecision] = []
        for requirement in requirements:
            if not requirement.owner:
                decisions.append(
                    AmbientDecision(
                        kind="unowned_requirement",
                        message=(
                            f"需求「{requirement.title or requirement.record_id}」还没有负责人，"
                            "建议指定 owner。"
                        ),
                    )
                )
            elif requirement.days_stale >= 7 and requirement.status not in {"已完成", "完成"}:
                decisions.append(
                    AmbientDecision(
                        kind="stale_requirement",
                        message=(
                            f"需求「{requirement.title or requirement.record_id}」已 {requirement.days_stale} "
                            "天未更新，建议跟进状态。"
                        ),
                    )
                )
            if decisions:
                break
        if decisions:
            self._record_ambient(platform, chat_id, today)
        return decisions

    def _ambient_count(self, platform: str, chat_id: str, today: date) -> int:
        brain = self.store.load(platform, chat_id)
        return int(brain.action_counts.get(f"ambient:{today.isoformat()}", 0))

    def _record_ambient(self, platform: str, chat_id: str, today: date) -> None:
        brain = self.store.load(platform, chat_id)
        counts = dict(brain.action_counts)
        key = f"ambient:{today.isoformat()}"
        counts[key] = int(counts.get(key, 0)) + 1
        self.store.save(
            type(brain)(
                platform=brain.platform,
                chat_id=brain.chat_id,
                facts=brain.facts,
                preferences=brain.preferences,
                policy=brain.policy,
                action_counts=counts,
                updated_at=brain.updated_at,
            )
        )


def is_ambient_status_request(text: str) -> bool:
    value = text.strip().lower()
    return any(key in value for key in ("主动巡检", "ambient", "主动模式", "主动提醒"))


class AmbientScheduler:
    def __init__(
        self,
        config: Settings,
        *,
        adapter: Adapter,
        channel_brain_store: ChannelBrainStore | None = None,
        requirement_connector: RequirementTableConnector | None = None,
        today: Callable[[], date] | None = None,
        now: Callable[[], datetime] | None = None,
    ):
        self.config = config
        self.adapter = adapter
        self.store = channel_brain_store or ChannelBrainStore(config)
        self.requirements = requirement_connector or _requirement_connector_for_platform(config)
        self.monitor = AmbientMonitor(self.store)
        self.now = now or datetime.now
        self.today = today or (lambda: self.now().date())
        self.followups = FollowUpService(config, RunLedger(config.run_ledger_path))

    async def run_forever(self) -> None:
        while True:
            await self.run_once()
            await asyncio.sleep(max(60, self.config.ambient_interval_seconds))

    async def run_once(self) -> AmbientRunResult:
        if self.config.followup_enabled:
            try:
                await self.followups.run_once(self.adapter.send_message)
            except Exception:  # noqa: BLE001 - follow-up sweep must never kill the scheduler
                pass
        if not self.config.ambient_enabled:
            return AmbientRunResult(skipped=max(1, len(self.config.ambient_chat_ids)))
        if not self.config.ambient_chat_ids:
            return AmbientRunResult(skipped=1)

        checked = sent = skipped = errors = 0
        for chat_id in self.config.ambient_chat_ids:
            brain = self.store.load(self.config.platform, chat_id)
            if not brain.policy.ambient_enabled:
                skipped += 1
                continue
            if not brain.policy.default_base_token or not brain.policy.default_table_id:
                skipped += 1
                continue
            checked += 1
            try:
                projection_fields = await self._projection_fields(
                    brain.policy.default_base_token,
                    brain.policy.default_table_id,
                )
                data = await self.requirements.list_records(
                    brain.policy.default_base_token,
                    brain.policy.default_table_id,
                    projection_fields,
                    limit=self.config.ambient_record_limit,
                )
                snapshots = snapshots_from_records(data)
                digest = self._due_daily_digest(chat_id, snapshots)
                if digest:
                    await self.adapter.send_message(chat_id, digest)
                    sent += 1
                    self._record_digest(chat_id)
                    self._audit(chat_id, digest, status="ok")
                    continue
                if self.config.ambient_daily_digest_enabled:
                    continue
                decisions = self.monitor.evaluate_requirements(
                    self.config.platform,
                    chat_id,
                    snapshots,
                    today=self.today(),
                )
                if not decisions:
                    continue
                message = f"主动巡检建议：{decisions[0].message}"
                await self.adapter.send_message(chat_id, message)
                sent += 1
                self._audit(chat_id, message, status="ok")
            except Exception as exc:  # noqa: BLE001
                errors += 1
                self._audit(chat_id, "", status="error", error=f"{type(exc).__name__}: {exc}")
        return AmbientRunResult(checked=checked, sent=sent, skipped=skipped, errors=errors)

    def _due_daily_digest(
        self, chat_id: str, snapshots: list[RequirementSnapshot]
    ) -> str | None:
        kind = self._digest_kind_due()
        if not kind:
            return None
        today = self.today()
        if self._digest_sent(chat_id, kind, today):
            return None
        return format_daily_digest(
            kind,
            snapshots,
            today=today,
            manager_ids=self.config.ambient_manager_open_ids,
        )

    def _digest_kind_due(self) -> str | None:
        if not self.config.ambient_daily_digest_enabled:
            return None
        now = self.now()
        minute_of_day = now.hour * 60 + now.minute
        window = max(1, self.config.ambient_digest_window_minutes)
        morning = self.config.ambient_morning_digest_hour * 60
        evening = self.config.ambient_evening_digest_hour * 60
        if morning <= minute_of_day < morning + window:
            return "morning"
        if evening <= minute_of_day < evening + window:
            return "evening"
        return None

    def _digest_sent(self, chat_id: str, kind: str, today: date) -> bool:
        brain = self.store.load(self.config.platform, chat_id)
        return int(brain.action_counts.get(_digest_key(kind, today), 0)) > 0

    def _record_digest(self, chat_id: str) -> None:
        today = self.today()
        kind = self._digest_kind_due()
        if not kind:
            return
        brain = self.store.load(self.config.platform, chat_id)
        counts = dict(brain.action_counts)
        counts[_digest_key(kind, today)] = int(counts.get(_digest_key(kind, today), 0)) + 1
        self.store.save(
            type(brain)(
                platform=brain.platform,
                chat_id=brain.chat_id,
                facts=brain.facts,
                preferences=brain.preferences,
                policy=brain.policy,
                action_counts=counts,
                updated_at=brain.updated_at,
            )
        )

    async def _projection_fields(self, base_token: str, table_id: str) -> list[str]:
        try:
            data = await self.requirements.list_fields(base_token, table_id)
        except Exception:  # noqa: BLE001
            return []
        names = _field_names(data)
        return [name for name in AMBIENT_PROJECTION_FIELDS if name in names]

    def _audit(
        self,
        chat_id: str,
        output_text: str,
        *,
        status: str,
        error: str | None = None,
    ) -> None:
        event = NormalizedEvent(
            platform=self.config.platform,
            event_id=f"ambient:{chat_id}",
            chat_id=chat_id,
            message_id=f"ambient:{chat_id}",
            sender_id="ambient_scheduler",
            text="ambient scheduled scan",
            mention_bot=False,
        )
        write_audit_log(
            self.config.audit_log,
            event=event,
            backend="ambient_scheduler",
            mfs_enabled=self.config.mfs_enabled,
            input_text="ambient scheduled scan",
            output_text=output_text,
            status=status,
            env=self.config.env,
            error=error,
        )


def snapshots_from_records(data: dict[str, Any]) -> list[RequirementSnapshot]:
    snapshots: list[RequirementSnapshot] = []
    for record in extract_matching_records(data):
        snapshots.append(
            RequirementSnapshot(
                record_id=record.record_id,
                title=_first_text(record.fields, ("需求名称", "需求描述", "标题", "Name")),
                owner=_first_text(record.fields, ("需求负责人", "负责人", "owner", "Owner", "责任人")),
                owner_ids=_user_ids(record.fields, ("需求负责人", "负责人", "owner", "Owner", "责任人")),
                status=_first_text(record.fields, ("需求状态", "状态", "Status")),
                priority=_first_text(record.fields, ("优先级", "Priority", "priority")),
                due_date=_first_date(
                    record.fields,
                    ("预计/实际完成时间", "预计完成时间", "实际完成时间", "due_date", "Due Date"),
                ),
                days_stale=_first_int(
                    record.fields, ("停滞天数", "需求持续时长", "days_stale", "Days Stale")
                ),
            )
        )
    return snapshots


def format_daily_digest(
    kind: str,
    requirements: list[RequirementSnapshot],
    *,
    today: date,
    manager_ids: tuple[str, ...] = (),
) -> str:
    open_tasks = [item for item in requirements if not _is_done(item)]
    due_tasks = [
        item
        for item in open_tasks
        if item.due_date is None or item.due_date <= today
    ]
    completed_today = [
        item for item in requirements if _is_done(item) and item.due_date == today
    ]
    people_to_mention = _ordered_people(due_tasks + completed_today, manager_ids)
    mentions = " ".join(_mention(user_id, name) for user_id, name in people_to_mention)

    if kind == "morning":
        lines = [
            f"{mentions}\n早上好，OS Tag 今日 9 点需求任务提醒：".strip(),
            f"今日重点任务：{len(due_tasks)} 项；未完成总数：{len(open_tasks)} 项。",
        ]
        lines.extend(_grouped_task_lines(due_tasks, today=today))
        if not due_tasks:
            lines.append("今天没有识别到到期或无截止日期的未完成需求。")
        lines.append("请相关负责人今天推进并在需求表更新状态。")
        return "\n".join(lines)

    lines = [
        f"{mentions}\nOS Tag 18 点需求复盘：".strip(),
        f"今日完成：{len(completed_today)} 项；仍未完成：{len(open_tasks)} 项。",
    ]
    if completed_today:
        lines.append("今日完成：")
        lines.extend(_task_lines(completed_today, today=today))
    if open_tasks:
        lines.append("仍未完成：")
        lines.extend(_grouped_task_lines(open_tasks, today=today))
    if not completed_today and not open_tasks:
        lines.append("当前没有识别到未完成需求，也没有今日完成记录。")
    lines.append("请负责人补齐进展；管理者可据此做明日优先级调整。")
    return "\n".join(lines)


def _field_names(data: dict[str, Any]) -> set[str]:
    blob = data.get("data", {})
    raw_fields = blob.get("fields") or blob.get("items") or []
    if not raw_fields and isinstance(blob.get("tables"), list):
        for table in blob["tables"]:
            if isinstance(table, dict) and isinstance(table.get("fields"), list):
                raw_fields.extend(table["fields"])
    names: set[str] = set()
    if not isinstance(raw_fields, list):
        return names
    for item in raw_fields:
        if isinstance(item, dict):
            value = item.get("field_name") or item.get("fieldName") or item.get("name")
            if value:
                names.add(str(value))
    return names


def _requirement_connector_for_platform(config: Settings) -> RequirementTableConnector:
    if config.platform == "dingtalk":
        return DwsRequirementTableConnector(DwsCLI(config))
    return LarkCLIRequirementTableConnector(LarkCLI(config), config.auto_base_identity)


def _first_text(fields: dict[str, Any], names: tuple[str, ...]) -> str:
    for name in names:
        value = cell_to_text(fields.get(name))
        if value:
            return value
    return ""


def _user_ids(fields: dict[str, Any], names: tuple[str, ...]) -> tuple[str, ...]:
    for name in names:
        ids = _extract_user_ids(fields.get(name))
        if ids:
            return ids
    return ()


def _extract_user_ids(value: Any) -> tuple[str, ...]:
    if isinstance(value, list):
        result: list[str] = []
        for item in value:
            if isinstance(item, dict):
                user_id = item.get("id") or item.get("open_id") or item.get("user_id")
                if user_id:
                    result.append(str(user_id))
        return tuple(result)
    if isinstance(value, dict):
        user_id = value.get("id") or value.get("open_id") or value.get("user_id")
        return (str(user_id),) if user_id else ()
    return ()


def _first_int(fields: dict[str, Any], names: tuple[str, ...]) -> int:
    for name in names:
        value = cell_to_text(fields.get(name))
        if not value:
            continue
        try:
            return int(float(value))
        except ValueError:
            continue
    return 0


def _first_date(fields: dict[str, Any], names: tuple[str, ...]) -> date | None:
    for name in names:
        value = fields.get(name)
        parsed = _parse_date(cell_to_text(value))
        if parsed:
            return parsed
    return None


def _parse_date(value: str) -> date | None:
    text = value.strip()
    if not text:
        return None
    candidates = (text, text[:19], text[:10])
    for candidate in candidates:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d"):
            try:
                return datetime.strptime(candidate, fmt).date()
            except ValueError:
                continue
    return None


def _is_done(requirement: RequirementSnapshot) -> bool:
    return requirement.status.strip().lower() in DONE_STATUSES


def _ordered_people(
    tasks: list[RequirementSnapshot], manager_ids: tuple[str, ...]
) -> list[tuple[str, str]]:
    people: list[tuple[str, str]] = [(user_id, "管理者") for user_id in manager_ids]
    for task in tasks:
        names = [part.strip() for part in task.owner.split(",") if part.strip()]
        for idx, user_id in enumerate(task.owner_ids):
            name = names[idx] if idx < len(names) else task.owner or "负责人"
            people.append((user_id, name))
    seen: set[str] = set()
    result: list[tuple[str, str]] = []
    for user_id, name in people:
        if user_id and user_id not in seen:
            seen.add(user_id)
            result.append((user_id, name))
    return result


def _mention(user_id: str, name: str) -> str:
    label = name or "相关人"
    return f'<at user_id="{user_id}">{label}</at>'


def _grouped_task_lines(tasks: list[RequirementSnapshot], *, today: date) -> list[str]:
    if not tasks:
        return []
    by_owner: dict[str, list[RequirementSnapshot]] = {}
    for task in tasks:
        key = task.owner or "未指定负责人"
        by_owner.setdefault(key, []).append(task)
    lines: list[str] = []
    for owner, owned_tasks in by_owner.items():
        lines.append(f"- {owner}:")
        lines.extend(f"  {line}" for line in _task_lines(owned_tasks, today=today))
    return lines


def _task_lines(tasks: list[RequirementSnapshot], *, today: date) -> list[str]:
    lines: list[str] = []
    for task in tasks[:20]:
        parts = [f"「{task.title or task.record_id}」"]
        if task.priority:
            parts.append(task.priority)
        if task.status:
            parts.append(task.status)
        if task.due_date:
            due_label = task.due_date.isoformat()
            if task.due_date < today and not _is_done(task):
                due_label += " 已逾期"
            elif task.due_date == today:
                due_label += " 今日"
            parts.append(due_label)
        if task.days_stale:
            parts.append(f"持续 {task.days_stale} 天")
        lines.append("；".join(parts))
    if len(tasks) > 20:
        lines.append(f"... 还有 {len(tasks) - 20} 项未展示")
    return lines


def _digest_key(kind: str, today: date) -> str:
    return f"ambient_digest:{kind}:{today.isoformat()}"
