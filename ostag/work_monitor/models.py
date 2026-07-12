"""Strict, serializable contracts for remote work monitoring."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar, Mapping


TASK_STATUSES = frozenset(
    {"queued", "running", "blocked", "awaiting_approval", "completed", "failed"}
)
EVENT_KINDS = frozenset(
    {
        "task.created",
        "task.started",
        "task.progress",
        "task.blocked",
        "task.awaiting_approval",
        "task.completed",
        "task.failed",
        "task.cancelled",
        "instance.heartbeat",
    }
)
COMMAND_ACTIONS = frozenset({"pause", "resume", "retry", "cancel"})


def _validate_keys(
    payload: Mapping[str, Any], *, required: frozenset[str], model_name: str
) -> None:
    keys = set(payload)
    unknown = keys - required
    missing = required - keys
    if unknown:
        raise ValueError(f"{model_name} unknown keys: {', '.join(sorted(unknown))}")
    if missing:
        raise ValueError(f"{model_name} missing keys: {', '.join(sorted(missing))}")


def _require_string(value: Any, field: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        raise ValueError(f"{field} must be a non-empty string")
    return value


@dataclass(frozen=True)
class TaskSnapshot:
    """The deliberately small task view allowed to leave an OStag instance."""

    task_id: str
    title: str
    project: str
    status: str
    agent: str
    progress: int
    duration_seconds: int
    summary: str
    error_class: str | None
    evidence_ref: str | None

    _KEYS: ClassVar[frozenset[str]] = frozenset(
        {
            "task_id",
            "title",
            "project",
            "status",
            "agent",
            "progress",
            "duration_seconds",
            "summary",
            "error_class",
            "evidence_ref",
        }
    )

    def __post_init__(self) -> None:
        for field in ("task_id", "title", "project", "agent"):
            _require_string(getattr(self, field), field)
        if self.status not in TASK_STATUSES:
            raise ValueError(f"status must be one of {sorted(TASK_STATUSES)}")
        if isinstance(self.progress, bool) or not isinstance(self.progress, int):
            raise ValueError("progress must be an integer")
        if not 0 <= self.progress <= 100:
            raise ValueError("progress must be between 0 and 100")
        if isinstance(self.duration_seconds, bool) or not isinstance(self.duration_seconds, int):
            raise ValueError("duration_seconds must be an integer")
        if self.duration_seconds < 0:
            raise ValueError("duration_seconds must not be negative")
        _require_string(self.summary, "summary", allow_empty=True)
        if len(self.summary) > 500:
            raise ValueError("summary must be at most 500 characters")
        for field in ("error_class", "evidence_ref"):
            value = getattr(self, field)
            if value is not None:
                _require_string(value, field)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> TaskSnapshot:
        _validate_keys(payload, required=cls._KEYS, model_name=cls.__name__)
        return cls(**{key: payload[key] for key in cls._KEYS})

    def to_dict(self) -> dict[str, Any]:
        return {key: getattr(self, key) for key in self._KEYS}


@dataclass(frozen=True)
class MonitorEvent:
    event_id: str
    instance_id: str
    sequence: int
    kind: str
    occurred_at: str
    task: TaskSnapshot | None

    _KEYS: ClassVar[frozenset[str]] = frozenset(
        {"event_id", "instance_id", "sequence", "kind", "occurred_at", "task"}
    )

    def __post_init__(self) -> None:
        for field in ("event_id", "instance_id", "occurred_at"):
            _require_string(getattr(self, field), field)
        if isinstance(self.sequence, bool) or not isinstance(self.sequence, int) or self.sequence <= 0:
            raise ValueError("sequence must be a positive integer")
        if self.kind not in EVENT_KINDS:
            raise ValueError(f"kind must be one of {sorted(EVENT_KINDS)}")
        if self.kind == "instance.heartbeat":
            if self.task is not None:
                raise ValueError("instance.heartbeat must not contain a task")
        elif not isinstance(self.task, TaskSnapshot):
            raise ValueError("task events require a TaskSnapshot")

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> MonitorEvent:
        _validate_keys(payload, required=cls._KEYS, model_name=cls.__name__)
        raw_task = payload["task"]
        task = None if raw_task is None else TaskSnapshot.from_dict(raw_task)
        return cls(
            event_id=payload["event_id"],
            instance_id=payload["instance_id"],
            sequence=payload["sequence"],
            kind=payload["kind"],
            occurred_at=payload["occurred_at"],
            task=task,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "instance_id": self.instance_id,
            "sequence": self.sequence,
            "kind": self.kind,
            "occurred_at": self.occurred_at,
            "task": None if self.task is None else self.task.to_dict(),
        }


@dataclass(frozen=True)
class RemoteCommand:
    command_id: str
    task_id: str
    instance_id: str
    action: str
    expected_status: str
    requested_by: str
    issued_at: str
    expires_at: str

    _KEYS: ClassVar[frozenset[str]] = frozenset(
        {
            "command_id",
            "task_id",
            "instance_id",
            "action",
            "expected_status",
            "requested_by",
            "issued_at",
            "expires_at",
        }
    )

    def __post_init__(self) -> None:
        for field in self._KEYS:
            _require_string(getattr(self, field), field)
        if self.action not in COMMAND_ACTIONS:
            raise ValueError(f"action must be one of {sorted(COMMAND_ACTIONS)}")
        if self.expected_status not in TASK_STATUSES:
            raise ValueError(f"expected_status must be one of {sorted(TASK_STATUSES)}")

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> RemoteCommand:
        _validate_keys(payload, required=cls._KEYS, model_name=cls.__name__)
        return cls(**{key: payload[key] for key in cls._KEYS})

    def to_dict(self) -> dict[str, str]:
        return {key: getattr(self, key) for key in self._KEYS}
