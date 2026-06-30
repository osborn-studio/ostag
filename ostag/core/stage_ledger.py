from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(frozen=True)
class StageEvent:
    key: str
    label: str
    status: str
    timestamp: str


class StageLedger:
    def __init__(self) -> None:
        self._events: list[StageEvent] = []

    def complete(self, key: str, label: str) -> None:
        self._events.append(
            StageEvent(
                key=key,
                label=label,
                status="completed",
                timestamp=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            )
        )

    @property
    def events(self) -> tuple[StageEvent, ...]:
        return tuple(self._events)

    def to_dicts(self) -> list[dict[str, str]]:
        return [
            {
                "key": event.key,
                "label": event.label,
                "status": event.status,
                "timestamp": event.timestamp,
            }
            for event in self._events
        ]

    def format_user(self) -> str:
        if not self._events:
            return "阶段：尚未开始"
        return "阶段：" + " → ".join(event.label for event in self._events)
