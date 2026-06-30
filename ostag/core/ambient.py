from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from ostag.core.channel_brain import ChannelBrainStore


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
