"""Canonical platform-scoped actor identity."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

PLATFORMS = frozenset({"slack", "feishu", "lark", "dingtalk", "telegram"})


@dataclass(frozen=True)
class Principal:
    platform: str
    actor_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.platform, str) or not self.platform:
            raise ValueError("principal platform is required")
        if self.platform not in PLATFORMS:
            raise ValueError(f"unsupported principal platform: {self.platform}")
        if not isinstance(self.actor_id, str) or not self.actor_id:
            raise ValueError("principal actor_id is required")

    def to_dict(self) -> dict[str, str]:
        return {"platform": self.platform, "actor_id": self.actor_id}

    @classmethod
    def from_dict(cls, payload: object) -> Principal:
        if not isinstance(payload, Mapping):
            raise ValueError("Principal payload must be a mapping")
        if set(payload) != {"platform", "actor_id"}:
            raise ValueError("Principal payload requires exactly platform and actor_id")
        platform: Any = payload["platform"]
        actor_id: Any = payload["actor_id"]
        return cls(platform=platform, actor_id=actor_id)
