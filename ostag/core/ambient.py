from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date
from typing import Any, Callable

from ostag.adapters.base import Adapter
from ostag.config import Settings
from ostag.connectors.requirements import (
    LarkCLIRequirementTableConnector,
    RequirementTableConnector,
)
from ostag.core.channel_brain import ChannelBrainStore
from ostag.core.event import NormalizedEvent
from ostag.core.logging import write_audit_log
from ostag.workflows.auto_base import LarkCLI, cell_to_text, extract_matching_records


AMBIENT_PROJECTION_FIELDS = [
    "需求编号",
    "需求名称",
    "需求描述",
    "负责人",
    "owner",
    "需求状态",
    "状态",
    "停滞天数",
]


@dataclass(frozen=True)
class RequirementSnapshot:
    record_id: str
    title: str
    owner: str = ""
    status: str = ""
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
    ):
        self.config = config
        self.adapter = adapter
        self.store = channel_brain_store or ChannelBrainStore(config)
        self.requirements = requirement_connector or LarkCLIRequirementTableConnector(
            LarkCLI(config), config.auto_base_identity
        )
        self.monitor = AmbientMonitor(self.store)
        self.today = today or date.today

    async def run_forever(self) -> None:
        while True:
            await self.run_once()
            await asyncio.sleep(max(60, self.config.ambient_interval_seconds))

    async def run_once(self) -> AmbientRunResult:
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
                decisions = self.monitor.evaluate_requirements(
                    self.config.platform,
                    chat_id,
                    snapshots_from_records(data),
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
                owner=_first_text(record.fields, ("负责人", "owner", "Owner", "责任人")),
                status=_first_text(record.fields, ("需求状态", "状态", "Status")),
                days_stale=_first_int(record.fields, ("停滞天数", "days_stale", "Days Stale")),
            )
        )
    return snapshots


def _field_names(data: dict[str, Any]) -> set[str]:
    blob = data.get("data", {})
    raw_fields = blob.get("fields") or blob.get("items") or []
    names: set[str] = set()
    if not isinstance(raw_fields, list):
        return names
    for item in raw_fields:
        if isinstance(item, dict):
            value = item.get("field_name") or item.get("name")
            if value:
                names.add(str(value))
    return names


def _first_text(fields: dict[str, Any], names: tuple[str, ...]) -> str:
    for name in names:
        value = cell_to_text(fields.get(name))
        if value:
            return value
    return ""


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
