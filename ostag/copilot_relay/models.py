"""Strict, immutable wire contracts for the Copilot Relay."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Any, ClassVar

from ostag.core.identity import Principal


RELAY_KINDS = frozenset(
    {
        "session.upsert",
        "assistant.snapshot",
        "decision.request",
        "decision.answer",
        "instruction.send",
        "task.create.proposal",
        "task.create.confirm",
        "delivery.receipt",
    }
)

SESSION_STATES = frozenset(
    {"starting", "running", "awaiting_decision", "idle", "completed", "failed", "cancelled"}
)
DELIVERY_STATES = frozenset(
    {"queued", "delivered", "accepted", "applied", "rejected", "expired", "indeterminate"}
)

_UTC_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$")


def _validate_keys(payload: object, *, required: tuple[str, ...], model_name: str) -> None:
    if not isinstance(payload, Mapping):
        raise ValueError(f"{model_name} payload must be a mapping")
    if not all(isinstance(key, str) for key in payload):
        raise ValueError(f"{model_name} payload must contain only string keys")
    actual = set(payload)
    expected = set(required)
    unknown = actual - expected
    missing = expected - actual
    if unknown:
        raise ValueError(f"{model_name} unknown keys: {', '.join(sorted(unknown))}")
    if missing:
        raise ValueError(f"{model_name} missing keys: {', '.join(sorted(missing))}")


def _require_string(
    value: Any,
    field: str,
    *,
    max_length: int = 256,
    allow_empty: bool = False,
) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise ValueError(f"{field} must be a non-empty string")
    if len(value) > max_length:
        raise ValueError(f"{field} must be at most {max_length} characters")
    return value


def _require_positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _parse_utc(value: Any, field: str) -> datetime:
    _require_string(value, field, max_length=32)
    if not _UTC_TIMESTAMP.fullmatch(value):
        raise ValueError(f"{field} must be a canonical UTC timestamp ending in Z")
    try:
        return datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError as exc:
        raise ValueError(f"{field} must be a valid UTC timestamp") from exc


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, list | tuple):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _require_bool(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{field} must be a boolean")
    return value


def _require_string_list(
    value: Any,
    field: str,
    *,
    maximum_items: int,
    item_max_length: int,
) -> None:
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise ValueError(f"{field} must be a list")
    if len(value) > maximum_items:
        raise ValueError(f"{field} must contain at most {maximum_items} items")
    for item in value:
        _require_string(item, f"{field} item", max_length=item_max_length)


@dataclass(frozen=True)
class SessionBinding:
    instance_id: str
    task_id: str
    run_id: str
    session_id: str
    turn_id: str
    session_version: int

    _KEYS: ClassVar[tuple[str, ...]] = (
        "instance_id",
        "task_id",
        "run_id",
        "session_id",
        "turn_id",
        "session_version",
    )

    def __post_init__(self) -> None:
        for field in ("instance_id", "task_id", "run_id", "session_id", "turn_id"):
            _require_string(getattr(self, field), field)
        _require_positive_int(self.session_version, "session_version")

    @classmethod
    def from_dict(cls, payload: object) -> SessionBinding:
        _validate_keys(payload, required=cls._KEYS, model_name=cls.__name__)
        assert isinstance(payload, Mapping)
        return cls(**{key: payload[key] for key in cls._KEYS})

    def to_dict(self) -> dict[str, str | int]:
        return {key: getattr(self, key) for key in self._KEYS}


def _validate_choice(choice: object) -> tuple[str, str]:
    _validate_keys(choice, required=("choice_id", "label"), model_name="decision choice")
    assert isinstance(choice, Mapping)
    return (
        _require_string(choice["choice_id"], "choice_id"),
        _require_string(choice["label"], "label", max_length=160),
    )


def _validate_payload(kind: str, payload: object) -> Mapping[str, Any]:
    schemas: dict[str, tuple[str, ...]] = {
        "session.upsert": (
            "agent_id", "title", "state", "resume_supported", "latest_summary"
        ),
        "assistant.snapshot": ("assistant_text", "redaction_count", "truncated"),
        "decision.request": (
            "decision_id", "question", "choices", "recommended_choice_id", "blocking", "safe_work"
        ),
        "decision.answer": ("decision_id", "choice_id", "answer"),
        "instruction.send": ("text",),
        "task.create.proposal": (
            "proposal_id", "objective", "repository", "workspace", "suggested_agent",
            "acceptance_criteria", "sensitive_operations",
        ),
        "task.create.confirm": ("proposal_id", "confirmed"),
        "delivery.receipt": ("related_message_id", "state", "summary", "error_code"),
    }
    _validate_keys(payload, required=schemas[kind], model_name=f"{kind} payload")
    assert isinstance(payload, Mapping)

    if kind == "session.upsert":
        _require_string(payload["agent_id"], "agent_id")
        _require_string(payload["title"], "title", max_length=300)
        if payload["state"] not in SESSION_STATES:
            raise ValueError(f"state must be one of {sorted(SESSION_STATES)}")
        _require_bool(payload["resume_supported"], "resume_supported")
        _require_string(payload["latest_summary"], "latest_summary", max_length=1_000, allow_empty=True)
    elif kind == "assistant.snapshot":
        _require_string(payload["assistant_text"], "assistant_text", max_length=4_000, allow_empty=True)
        count = payload["redaction_count"]
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValueError("redaction_count must be a non-negative integer")
        _require_bool(payload["truncated"], "truncated")
    elif kind == "decision.request":
        _require_string(payload["decision_id"], "decision_id")
        _require_string(payload["question"], "question", max_length=1_000)
        choices = payload["choices"]
        if not isinstance(choices, list) or not 1 <= len(choices) <= 8:
            raise ValueError("choices must contain between 1 and 8 items")
        parsed_choices = [_validate_choice(choice) for choice in choices]
        choice_ids = [choice_id for choice_id, _ in parsed_choices]
        if len(choice_ids) != len(set(choice_ids)):
            raise ValueError("choice_id values must be unique")
        recommended = payload["recommended_choice_id"]
        if recommended is not None:
            _require_string(recommended, "recommended_choice_id")
            if recommended not in choice_ids:
                raise ValueError("recommended_choice_id must identify one of choices")
        _require_bool(payload["blocking"], "blocking")
        _require_string_list(payload["safe_work"], "safe_work", maximum_items=16, item_max_length=300)
    elif kind == "decision.answer":
        _require_string(payload["decision_id"], "decision_id")
        if payload["choice_id"] is not None:
            _require_string(payload["choice_id"], "choice_id")
        _require_string(payload["answer"], "answer", max_length=4_000)
    elif kind == "instruction.send":
        _require_string(payload["text"], "text", max_length=16_000)
    elif kind == "task.create.proposal":
        _require_string(payload["proposal_id"], "proposal_id")
        _require_string(payload["objective"], "objective", max_length=1_000)
        for field in ("repository", "workspace", "suggested_agent"):
            _require_string(payload[field], field)
        _require_string_list(
            payload["acceptance_criteria"], "acceptance_criteria", maximum_items=16,
            item_max_length=300,
        )
        _require_string_list(
            payload["sensitive_operations"], "sensitive_operations", maximum_items=16,
            item_max_length=300,
        )
    elif kind == "task.create.confirm":
        _require_string(payload["proposal_id"], "proposal_id")
        _require_bool(payload["confirmed"], "confirmed")
    else:
        _require_string(payload["related_message_id"], "related_message_id")
        if payload["state"] not in DELIVERY_STATES:
            raise ValueError(f"state must be one of {sorted(DELIVERY_STATES)}")
        _require_string(payload["summary"], "summary", max_length=1_000, allow_empty=True)
        if payload["error_code"] is not None:
            _require_string(payload["error_code"], "error_code")

    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if len(encoded) > 32_000:
        raise ValueError("payload must be at most 32000 characters when encoded")
    return _freeze(payload)


@dataclass(frozen=True)
class RelayEnvelope:
    schema_version: int
    message_id: str
    binding: SessionBinding
    sequence: int
    created_at: str
    expires_at: str
    sender: Principal
    kind: str
    payload: Mapping[str, Any]
    correlation_id: str | None
    causation_id: str | None

    _KEYS: ClassVar[tuple[str, ...]] = (
        "schema_version",
        "message_id",
        "binding",
        "sequence",
        "created_at",
        "expires_at",
        "sender",
        "kind",
        "payload",
        "correlation_id",
        "causation_id",
    )

    def __post_init__(self) -> None:
        _require_positive_int(self.schema_version, "schema_version")
        _require_string(self.message_id, "message_id")
        if not isinstance(self.binding, SessionBinding):
            raise ValueError("binding must be a SessionBinding")
        _require_positive_int(self.sequence, "sequence")
        created = _parse_utc(self.created_at, "created_at")
        expires = _parse_utc(self.expires_at, "expires_at")
        if expires <= created:
            raise ValueError("expires_at must be later than created_at")
        if not isinstance(self.sender, Principal):
            raise ValueError("sender must be a Principal")
        if self.kind not in RELAY_KINDS:
            raise ValueError(f"kind must be one of {sorted(RELAY_KINDS)}")
        for field in ("correlation_id", "causation_id"):
            value = getattr(self, field)
            if value is not None:
                _require_string(value, field)

    @classmethod
    def from_dict(cls, payload: object) -> RelayEnvelope:
        _validate_keys(payload, required=cls._KEYS, model_name=cls.__name__)
        assert isinstance(payload, Mapping)
        kind = payload["kind"]
        if kind not in RELAY_KINDS:
            raise ValueError(f"kind must be one of {sorted(RELAY_KINDS)}")
        return cls(
            schema_version=payload["schema_version"],
            message_id=payload["message_id"],
            binding=SessionBinding.from_dict(payload["binding"]),
            sequence=payload["sequence"],
            created_at=payload["created_at"],
            expires_at=payload["expires_at"],
            sender=Principal.from_dict(payload["sender"]),
            kind=kind,
            payload=_validate_payload(kind, payload["payload"]),
            correlation_id=payload["correlation_id"],
            causation_id=payload["causation_id"],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "message_id": self.message_id,
            "binding": self.binding.to_dict(),
            "sequence": self.sequence,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "sender": self.sender.to_dict(),
            "kind": self.kind,
            "payload": _thaw(self.payload),
            "correlation_id": self.correlation_id,
            "causation_id": self.causation_id,
        }
