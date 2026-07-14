from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, Callable
from zoneinfo import ZoneInfo

from ostag.adapters.base import Adapter, StructuredCardAdapter
from ostag.adapters.feishu_cards import render_requirement_board_card
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
from ostag.core.schemas import LedgerEvent, new_id
from ostag.core.logging import write_audit_log
from ostag.core.requirement_board import (
    RequirementSnapshot,
    analyze_daily_requirement_board,
    format_daily_requirement_board_text,
)
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
    "需求提出日期",
    "停滞天数",
    "需求持续时长",
]

DONE_STATUSES = {"已完成", "完成", "done", "completed"}


@dataclass(frozen=True)
class AmbientDecision:
    kind: str
    message: str
    proposal_only: bool = True
    record_id: str = ""
    rule: str = ""


def _delivery_id(chat_id: str, message: str) -> str:
    return "ambient-" + hashlib.sha256(f"{chat_id}\0{message}".encode()).hexdigest()[:32]


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
        max_decisions: int = 3,
        stale_days: int = 7,
        suppress_days: int = 3,
    ) -> list[AmbientDecision]:
        brain = self.store.load(platform, chat_id)
        if not brain.policy.ambient_enabled:
            return []
        if self._ambient_count(platform, chat_id, today) >= self.daily_limit:
            return []

        decisions: list[AmbientDecision] = []
        for requirement in requirements:
            if len(decisions) >= max(1, max_decisions):
                break
            decision = self._evaluate_one(requirement, today=today, stale_days=stale_days)
            if decision is None:
                continue
            if self._suppressed(brain, decision, today, suppress_days):
                continue
            decisions.append(decision)
        if decisions:
            self._record_ambient(platform, chat_id, today)
            self._record_flags(platform, chat_id, decisions, today)
        return decisions

    def _evaluate_one(
        self, requirement: RequirementSnapshot, *, today: date, stale_days: int
    ) -> AmbientDecision | None:
        """One decision per requirement, highest-priority issue first."""
        label = requirement.title or requirement.record_id
        done = requirement.status in DONE_STATUSES
        if not requirement.owner:
            return AmbientDecision(
                kind="unowned_requirement",
                message=f"需求「{label}」还没有负责人，建议指定 owner。",
                record_id=requirement.record_id,
                rule="owner is empty",
            )
        if requirement.due_date and requirement.due_date < today and not done:
            return AmbientDecision(
                kind="overdue_requirement",
                message=(
                    f"需求「{label}」（{requirement.record_id}）已于 "
                    f"{requirement.due_date.isoformat()} 到期仍未完成"
                    f"（当前状态：{requirement.status or '未设置'}），建议跟进。"
                ),
                record_id=requirement.record_id,
                rule=f"due_date {requirement.due_date.isoformat()} < today, status not done",
            )
        if requirement.days_stale >= stale_days and not done:
            return AmbientDecision(
                kind="stale_requirement",
                message=(
                    f"需求「{label}」已 {requirement.days_stale} 天未更新"
                    f"（阈值 {stale_days} 天），建议跟进状态。"
                ),
                record_id=requirement.record_id,
                rule=f"days_stale {requirement.days_stale} >= {stale_days}",
            )
        if not requirement.status:
            return AmbientDecision(
                kind="missing_status",
                message=(
                    f"需求「{label}」（{requirement.record_id}）未设置状态，建议补全需求状态。"
                ),
                record_id=requirement.record_id,
                rule="status is empty",
            )
        return None

    def _suppressed(
        self, brain: Any, decision: AmbientDecision, today: date, suppress_days: int
    ) -> bool:
        if not decision.record_id or suppress_days <= 0:
            return False
        key = f"ambient_flag:{decision.kind}:{decision.record_id}"
        flagged_ordinal = int(brain.action_counts.get(key, 0))
        return flagged_ordinal > 0 and today.toordinal() - flagged_ordinal < suppress_days

    def _record_flags(
        self, platform: str, chat_id: str, decisions: list[AmbientDecision], today: date
    ) -> None:
        brain = self.store.load(platform, chat_id)
        counts = dict(brain.action_counts)
        for decision in decisions:
            if decision.record_id:
                counts[f"ambient_flag:{decision.kind}:{decision.record_id}"] = today.toordinal()
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
        self.now = now or (lambda: datetime.now(UTC))
        self.today = today or (lambda: self._local_now().date())
        self.followups = FollowUpService(config, RunLedger(config.run_ledger_path))

    async def run_forever(self) -> None:
        while True:
            await self.run_once()
            await asyncio.sleep(max(60, self.config.ambient_interval_seconds))

    async def run_once(self) -> AmbientRunResult:
        return await self._run_once()

    async def run_daily_board_once(self, *, force: bool = True) -> AmbientRunResult:
        return await self._run_once(force_daily_board=force, board_only=True)

    async def _run_once(
        self, *, force_daily_board: bool = False, board_only: bool = False
    ) -> AmbientRunResult:
        if self.config.followup_enabled:
            try:
                async def send_followup(chat_id: str, message: str) -> None:
                    await self.adapter.send_message(
                        chat_id, message, delivery_id=_delivery_id(chat_id, message)
                    )

                await self.followups.run_once(send_followup)
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
                board_due = self.config.ambient_daily_board_enabled and (
                    force_daily_board or self._daily_board_window_open()
                )
                if board_due:
                    local_today = self._local_now().date()
                    if not self._daily_board_sent(chat_id, local_today):
                        board = analyze_daily_requirement_board(snapshots, today=local_today)
                        card = render_requirement_board_card(
                            board,
                            group_name=self.config.ambient_daily_board_group_name,
                            base_url=self.config.ambient_daily_board_base_url,
                        )
                        text = format_daily_requirement_board_text(board)
                        if isinstance(self.adapter, StructuredCardAdapter):
                            await self.adapter.send_card(
                                chat_id,
                                card,
                                text,
                                delivery_id=_delivery_id(chat_id, text),
                            )
                        else:
                            await self.adapter.send_message(
                                chat_id, text, delivery_id=_delivery_id(chat_id, text)
                            )
                        self._record_daily_board(chat_id, local_today)
                        sent += 1
                        self._audit(chat_id, text, status="ok")
                    continue
                if board_only:
                    skipped += 1
                    continue
                digest = self._due_daily_digest(chat_id, snapshots)
                if digest:
                    await self.adapter.send_message(
                        chat_id, digest, delivery_id=_delivery_id(chat_id, digest)
                    )
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
                    max_decisions=self.config.ambient_max_decisions,
                    stale_days=self.config.ambient_stale_days,
                    suppress_days=self.config.ambient_suppress_days,
                )
                if not decisions:
                    continue
                if len(decisions) == 1:
                    message = f"主动巡检建议：{decisions[0].message}"
                else:
                    message = "主动巡检建议：\n" + "\n".join(
                        f"- {decision.message}" for decision in decisions
                    )
                await self.adapter.send_message(
                    chat_id, message, delivery_id=_delivery_id(chat_id, message)
                )
                sent += 1
                self._record_decisions(chat_id, decisions)
                self._audit(chat_id, message, status="ok")
            except Exception as exc:  # noqa: BLE001
                errors += 1
                self._audit(chat_id, "", status="error", error=f"{type(exc).__name__}: {exc}")
        return AmbientRunResult(checked=checked, sent=sent, skipped=skipped, errors=errors)

    def _local_now(self) -> datetime:
        moment = self.now()
        zone = ZoneInfo(self.config.ambient_timezone)
        if moment.tzinfo is None:
            return moment.replace(tzinfo=zone)
        return moment.astimezone(zone)

    def _daily_board_window_open(self) -> bool:
        local = self._local_now()
        minute = local.hour * 60 + local.minute
        start = self.config.ambient_daily_board_hour * 60
        return start <= minute < start + self.config.ambient_daily_board_window_minutes

    def _daily_board_sent(self, chat_id: str, today: date) -> bool:
        brain = self.store.load(self.config.platform, chat_id)
        return int(brain.action_counts.get(f"daily_board:{today.isoformat()}", 0)) > 0

    def _record_daily_board(self, chat_id: str, today: date) -> None:
        brain = self.store.load(self.config.platform, chat_id)
        counts = dict(brain.action_counts)
        counts[f"daily_board:{today.isoformat()}"] = 1
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

    def _record_decisions(self, chat_id: str, decisions: list[AmbientDecision]) -> None:
        """Audit ambient decisions as a run in the ledger, citing rule and record."""
        try:
            ledger = self.followups.ledger
            run = ledger.open_run(
                NormalizedEvent(
                    platform=self.config.platform,
                    event_id=f"ambient:{chat_id}",
                    chat_id=chat_id,
                    message_id=f"ambient:{chat_id}",
                    sender_id="ambient_scheduler",
                    text="ambient scheduled scan",
                    mention_bot=True,
                    chat_type="group",
                    destination_id=chat_id,
                ),
                executor_id="ambient",
            )
            for decision in decisions:
                ledger.append(
                    LedgerEvent(
                        event_id=new_id("evt"),
                        run_id=run.run_id,
                        event_type="ambient_decision",
                        payload={
                            "kind": decision.kind,
                            "record_id": decision.record_id,
                            "rule": decision.rule,
                            "message": decision.message,
                            "proposal_only": decision.proposal_only,
                        },
                    )
                )
            ledger.record_status(run.run_id, "succeeded")
        except Exception:  # noqa: BLE001 - auditing must never break the scan
            pass

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
            chat_type="group",
            destination_id=chat_id,
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
                owner=_first_text(
                    record.fields, ("需求负责人", "负责人", "owner", "Owner", "责任人")
                ),
                owner_ids=_user_ids(
                    record.fields, ("需求负责人", "负责人", "owner", "Owner", "责任人")
                ),
                status=_first_text(record.fields, ("需求状态", "状态", "Status")),
                priority=_first_text(record.fields, ("优先级", "Priority", "priority")),
                due_date=_first_date(
                    record.fields,
                    ("预计/实际完成时间", "预计完成时间", "实际完成时间", "due_date", "Due Date"),
                ),
                proposed_date=_first_date(
                    record.fields, ("需求提出日期", "提出日期", "created_at")
                ),
                days_stale=_first_int(
                    record.fields, ("停滞天数", "需求持续时长", "days_stale", "Days Stale")
                ),
            )
        )
    return snapshots


def format_weekly_requirement_review(
    requirements: list[RequirementSnapshot],
    *,
    today: date,
    stale_days: int = 7,
) -> str:
    week_start = today.fromordinal(today.toordinal() - today.weekday())
    open_tasks = [item for item in requirements if not _is_done(item)]
    completed_this_week = [
        item
        for item in requirements
        if _is_done(item) and item.due_date is not None and week_start <= item.due_date <= today
    ]
    overdue = [item for item in open_tasks if item.due_date is not None and item.due_date < today]
    unowned = [item for item in open_tasks if not item.owner]
    missing_status = [item for item in requirements if not item.status]
    stale = [item for item in open_tasks if item.days_stale >= stale_days]

    lines = [
        f"OS Tag 本周需求回顾（{week_start.isoformat()} 至 {today.isoformat()}）：",
        (
            f"总计：{len(requirements)} 项；未完成：{len(open_tasks)} 项；"
            f"本周完成：{len(completed_this_week)} 项。"
        ),
        (
            f"风险：逾期：{len(overdue)} 项；无人负责：{len(unowned)} 项；"
            f"缺状态：{len(missing_status)} 项；停滞：{len(stale)} 项。"
        ),
    ]
    sections = (
        ("逾期需求", overdue),
        ("无人负责", unowned),
        ("缺状态", missing_status),
        (f"停滞超过 {stale_days} 天", stale),
        ("本周完成", completed_this_week),
    )
    for title, tasks in sections:
        if tasks:
            lines.append(f"{title}：")
            lines.extend(_review_task_lines(tasks, today=today))
    if not any((overdue, unowned, missing_status, stale)):
        lines.append("本周没有识别到逾期、无人负责、缺状态或停滞需求。")
    lines.append("建议：优先处理逾期和无人负责项，并在需求表补齐负责人、状态和下一步。")
    return "\n".join(lines)


def format_daily_digest(
    kind: str,
    requirements: list[RequirementSnapshot],
    *,
    today: date,
    manager_ids: tuple[str, ...] = (),
) -> str:
    open_tasks = [item for item in requirements if not _is_done(item)]
    due_tasks = [item for item in open_tasks if item.due_date is None or item.due_date <= today]
    completed_today = [item for item in requirements if _is_done(item) and item.due_date == today]
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


def _review_task_lines(tasks: list[RequirementSnapshot], *, today: date) -> list[str]:
    lines: list[str] = []
    for task in tasks[:10]:
        parts = [f"「{task.title or task.record_id}」", task.record_id]
        if task.owner:
            parts.append(f"负责人：{task.owner}")
        else:
            parts.append("负责人：未指定")
        if task.status:
            parts.append(f"状态：{task.status}")
        else:
            parts.append("状态：未设置")
        if task.priority:
            parts.append(f"优先级：{task.priority}")
        if task.due_date:
            due = task.due_date.isoformat()
            if task.due_date < today and not _is_done(task):
                due += " 已逾期"
            elif task.due_date == today:
                due += " 今日"
            parts.append(f"截止：{due}")
        if task.days_stale:
            parts.append(f"停滞：{task.days_stale} 天")
        lines.append("- " + "；".join(parts))
    if len(tasks) > 10:
        lines.append(f"- ... 还有 {len(tasks) - 10} 项未展示")
    return lines


def _digest_key(kind: str, today: date) -> str:
    return f"ambient_digest:{kind}:{today.isoformat()}"
