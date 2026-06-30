from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ostag.config import Settings
from ostag.core.event import NormalizedEvent
from ostag.mfs.memory import memory_root


@dataclass(frozen=True)
class ChannelPolicy:
    default_base_token: str = ""
    default_table_id: str = ""
    ambient_enabled: bool = False
    task_create_allowed: bool = True
    task_create_requires_confirmation: bool = False
    daily_action_budget: int = 0
    writable_fields: tuple[str, ...] = ()
    readable_fields: tuple[str, ...] = ()


@dataclass(frozen=True)
class ChannelBrain:
    platform: str
    chat_id: str
    facts: tuple[str, ...] = ()
    preferences: tuple[str, ...] = ()
    policy: ChannelPolicy = field(default_factory=ChannelPolicy)
    action_counts: dict[str, int] = field(default_factory=dict)
    updated_at: str = ""


class ChannelBrainStore:
    def __init__(self, config: Settings, root: Path | None = None):
        self.config = config
        self.root = root or memory_root()

    def path_for(self, platform: str, chat_id: str) -> Path:
        safe_platform = _safe_segment(platform)
        safe_chat_id = _safe_segment(chat_id)
        return self.root / safe_platform / safe_chat_id / "brain.json"

    def load(self, platform: str, chat_id: str) -> ChannelBrain:
        path = self.path_for(platform, chat_id)
        if not path.exists():
            return ChannelBrain(
                platform=platform,
                chat_id=chat_id,
                policy=ChannelPolicy(
                    default_base_token=self.config.auto_base_default_base_token,
                    default_table_id=self.config.auto_base_default_table_id,
                ),
            )
        data = json.loads(path.read_text(encoding="utf-8"))
        policy_data = data.get("policy") if isinstance(data.get("policy"), dict) else {}
        return ChannelBrain(
            platform=str(data.get("platform") or platform),
            chat_id=str(data.get("chat_id") or chat_id),
            facts=tuple(str(item) for item in data.get("facts", []) if str(item).strip()),
            preferences=tuple(
                str(item) for item in data.get("preferences", []) if str(item).strip()
            ),
            policy=ChannelPolicy(
                default_base_token=str(
                    policy_data.get("default_base_token")
                    or self.config.auto_base_default_base_token
                ),
                default_table_id=str(
                    policy_data.get("default_table_id") or self.config.auto_base_default_table_id
                ),
                ambient_enabled=bool(policy_data.get("ambient_enabled", False)),
                task_create_allowed=bool(policy_data.get("task_create_allowed", True)),
                task_create_requires_confirmation=bool(
                    policy_data.get("task_create_requires_confirmation", False)
                ),
                daily_action_budget=int(policy_data.get("daily_action_budget") or 0),
                writable_fields=tuple(policy_data.get("writable_fields", []) or ()),
                readable_fields=tuple(policy_data.get("readable_fields", []) or ()),
            ),
            action_counts={
                str(key): int(value)
                for key, value in (data.get("action_counts") or {}).items()
            },
            updated_at=str(data.get("updated_at") or ""),
        )

    def remember_fact(
        self, platform: str, chat_id: str, fact: str, *, now: datetime | None = None
    ) -> ChannelBrain:
        text = fact.strip()
        if not text:
            return self.load(platform, chat_id)
        brain = self.load(platform, chat_id)
        facts = list(brain.facts)
        if text not in facts:
            facts.append(text)
        updated = ChannelBrain(
            platform=platform,
            chat_id=chat_id,
            facts=tuple(facts),
            preferences=brain.preferences,
            policy=brain.policy,
            action_counts=brain.action_counts,
            updated_at=(now or datetime.now(timezone.utc))
            .astimezone(timezone.utc)
            .isoformat(timespec="seconds"),
        )
        self.save(updated)
        return updated

    def save(self, brain: ChannelBrain) -> Path:
        path = self.path_for(brain.platform, brain.chat_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "platform": brain.platform,
            "chat_id": brain.chat_id,
            "facts": list(brain.facts),
            "preferences": list(brain.preferences),
            "policy": {
                "default_base_token": brain.policy.default_base_token,
                "default_table_id": brain.policy.default_table_id,
                "ambient_enabled": brain.policy.ambient_enabled,
                "task_create_allowed": brain.policy.task_create_allowed,
                "task_create_requires_confirmation": brain.policy.task_create_requires_confirmation,
                "daily_action_budget": brain.policy.daily_action_budget,
                "writable_fields": list(brain.policy.writable_fields),
                "readable_fields": list(brain.policy.readable_fields),
            },
            "action_counts": brain.action_counts,
            "updated_at": brain.updated_at,
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def actions_today(self, platform: str, chat_id: str, today: str | None = None) -> int:
        brain = self.load(platform, chat_id)
        key = today or datetime.now(timezone.utc).date().isoformat()
        return int(brain.action_counts.get(key, 0))

    def record_action(
        self, platform: str, chat_id: str, today: str | None = None
    ) -> ChannelBrain:
        key = today or datetime.now(timezone.utc).date().isoformat()
        brain = self.load(platform, chat_id)
        counts = dict(brain.action_counts)
        counts[key] = int(counts.get(key, 0)) + 1
        updated = ChannelBrain(
            platform=brain.platform,
            chat_id=brain.chat_id,
            facts=brain.facts,
            preferences=brain.preferences,
            policy=brain.policy,
            action_counts=counts,
            updated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        self.save(updated)
        return updated


def channel_policy_for_event(event: NormalizedEvent, config: Settings) -> ChannelPolicy:
    return ChannelBrainStore(config).load(event.platform, event.chat_id).policy


def is_brain_status_request(text: str) -> bool:
    normalized = text.strip().lower()
    return any(key in normalized for key in ("记住了什么", "记忆状态", "channel brain", "群记忆"))


def extract_remember_fact(text: str) -> str | None:
    stripped = text.strip()
    for marker in ("记住：", "记住:", "请记住：", "请记住:"):
        if marker in stripped:
            return stripped.split(marker, 1)[1].strip()
    return None


def format_channel_brain(brain: ChannelBrain) -> str:
    facts = "\n".join(f"- {fact}" for fact in brain.facts[-10:]) or "- 暂无事实记忆"
    prefs = "\n".join(f"- {pref}" for pref in brain.preferences[-5:]) or "- 暂无偏好记忆"
    base = brain.policy.default_base_token or "未配置"
    table = brain.policy.default_table_id or "未配置"
    return (
        "这个群的 Channel Brain：\n"
        f"事实：\n{facts}\n"
        f"偏好：\n{prefs}\n"
        f"默认需求表：base={base}, table={table}"
    )


def _safe_segment(value: str) -> str:
    cleaned = value.strip().replace("/", "_").replace("\\", "_")
    if not cleaned or cleaned in {".", ".."}:
        raise ValueError("channel brain path segment cannot be empty or relative")
    return cleaned
