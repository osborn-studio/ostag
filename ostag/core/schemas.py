"""Core governance schemas for the OS Tag open protocol.

These schemas are platform-independent and dependency-free. They define the
stable contract shared by adapters, connectors, executors, workflows, the run
ledger, and the CLI. Serialized forms prune null/empty fields so agent-facing
JSON stays compact.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

PLATFORMS = ("slack", "feishu", "lark", "dingtalk", "github", "gitlab")

RUN_STATUSES = (
    "queued",
    "admitted",
    "running",
    "waiting_approval",
    "blocked",
    "succeeded",
    "failed",
    "cancelled",
)
TERMINAL_RUN_STATUSES = ("succeeded", "failed", "cancelled")

RECEIPT_STATES = (
    "proposed",
    "needs_approval",
    "ready_to_apply",
    "applying",
    "applied",
    "approved_only",
    "rejected",
    "blocked",
    "stale",
    "failed",
)
TERMINAL_RECEIPT_STATES = ("applied", "approved_only", "rejected", "stale", "failed")

MUTATION_TYPES = ("create", "update", "delete", "send", "schedule", "merge", "assign")
RISK_LEVELS = ("low", "medium", "high")
PERMISSION_LEVELS = ("read", "propose", "write", "admin")

LEDGER_EVENT_TYPES = (
    "run_created",
    "run_status_changed",
    "context_packet_built",
    "executor_invoked",
    "action_proposed",
    "action_state_changed",
    "artifact_recorded",
    "approval_recorded",
    "apply_started",
    "apply_finished",
    "run_finished",
    "continuation_scheduled",
    "continuation_fired",
    "continuation_cancelled",
    "ambient_decision",
    "cost_recorded",
    "security_event",
)


class SchemaError(ValueError):
    """Raised when a schema payload fails validation.

    Always carries a machine-readable ``code`` and a ``recovery_hint`` so both
    humans and agents can act on the failure.
    """

    def __init__(self, code: str, message: str, recovery_hint: str) -> None:
        super().__init__(message)
        self.code = code
        self.recovery_hint = recovery_hint

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": str(self), "recovery_hint": self.recovery_hint}


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _require(value: object, name: str, schema: str) -> None:
    if value in (None, ""):
        raise SchemaError(
            code="missing_field",
            message=f"{schema}.{name} is required",
            recovery_hint=f"Provide a non-empty value for '{name}'.",
        )


def _require_choice(value: str, choices: tuple[str, ...], name: str, schema: str) -> None:
    if value not in choices:
        raise SchemaError(
            code="invalid_choice",
            message=f"{schema}.{name}={value!r} is not one of {list(choices)}",
            recovery_hint=f"Use one of: {', '.join(choices)}.",
        )


def prune(value: Any) -> Any:
    """Recursively drop None values and empty dicts/lists/strings from payloads."""
    if isinstance(value, dict):
        cleaned = {k: prune(v) for k, v in value.items()}
        return {k: v for k, v in cleaned.items() if v not in (None, {}, [], "")}
    if isinstance(value, (list, tuple)):
        return [prune(item) for item in value if item is not None]
    return value


@dataclass(frozen=True)
class RunSource:
    platform: str
    channel_id: str
    thread_id: str = ""
    message_id: str = ""

    def __post_init__(self) -> None:
        _require_choice(self.platform, PLATFORMS, "platform", "RunSource")
        _require(self.channel_id, "channel_id", "RunSource")

    def to_dict(self) -> dict[str, Any]:
        return prune(
            {
                "platform": self.platform,
                "channel_id": self.channel_id,
                "thread_id": self.thread_id,
                "message_id": self.message_id,
            }
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RunSource:
        return cls(
            platform=data.get("platform", ""),
            channel_id=data.get("channel_id", ""),
            thread_id=data.get("thread_id", ""),
            message_id=data.get("message_id", ""),
        )


@dataclass(frozen=True)
class RunActor:
    actor_id: str
    display_name: str = ""
    permission_level: str = "propose"

    def __post_init__(self) -> None:
        _require(self.actor_id, "actor_id", "RunActor")
        _require_choice(self.permission_level, PERMISSION_LEVELS, "permission_level", "RunActor")

    def to_dict(self) -> dict[str, Any]:
        return prune(
            {
                "actor_id": self.actor_id,
                "display_name": self.display_name,
                "permission_level": self.permission_level,
            }
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RunActor:
        return cls(
            actor_id=data.get("actor_id", ""),
            display_name=data.get("display_name", ""),
            permission_level=data.get("permission_level", "propose"),
        )


@dataclass(frozen=True)
class RunIntent:
    raw_text: str
    normalized_type: str = "chat"
    confidence: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        return prune(
            {
                "raw_text": self.raw_text,
                "normalized_type": self.normalized_type,
                "confidence": self.confidence,
            }
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RunIntent:
        return cls(
            raw_text=data.get("raw_text", ""),
            normalized_type=data.get("normalized_type", "chat"),
            confidence=float(data.get("confidence", 1.0)),
        )


@dataclass(frozen=True)
class Run:
    """One unit of OS Tag work anchored to a source thread or source object."""

    run_id: str
    source: RunSource
    actor: RunActor
    intent: RunIntent
    status: str = "queued"
    context_packet_id: str = ""
    executor_id: str = ""
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)

    def __post_init__(self) -> None:
        _require(self.run_id, "run_id", "Run")
        _require_choice(self.status, RUN_STATUSES, "status", "Run")

    def with_status(self, status: str) -> Run:
        _require_choice(status, RUN_STATUSES, "status", "Run")
        return Run(
            run_id=self.run_id,
            source=self.source,
            actor=self.actor,
            intent=self.intent,
            status=status,
            context_packet_id=self.context_packet_id,
            executor_id=self.executor_id,
            created_at=self.created_at,
            updated_at=utc_now_iso(),
        )

    def to_dict(self) -> dict[str, Any]:
        return prune(
            {
                "run_id": self.run_id,
                "source": self.source.to_dict(),
                "actor": self.actor.to_dict(),
                "intent": self.intent.to_dict(),
                "status": self.status,
                "context_packet_id": self.context_packet_id,
                "executor_id": self.executor_id,
                "created_at": self.created_at,
                "updated_at": self.updated_at,
            }
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Run:
        return cls(
            run_id=data.get("run_id", ""),
            source=RunSource.from_dict(data.get("source", {})),
            actor=RunActor.from_dict(data.get("actor", {})),
            intent=RunIntent.from_dict(data.get("intent", {})),
            status=data.get("status", "queued"),
            context_packet_id=data.get("context_packet_id", ""),
            executor_id=data.get("executor_id", ""),
            created_at=data.get("created_at", utc_now_iso()),
            updated_at=data.get("updated_at", utc_now_iso()),
        )


@dataclass(frozen=True)
class ContextPacket:
    """Record of what OS Tag gave to an agent or workflow, hashed for audit."""

    context_packet_id: str
    run_id: str
    source_thread_summary: str = ""
    linked_resources: tuple[str, ...] = ()
    channel_brain_facts: tuple[str, ...] = ()
    previous_run_ids: tuple[str, ...] = ()
    connector_platforms: tuple[str, ...] = ()
    policy_constraints: dict[str, Any] = field(default_factory=dict)
    citations: tuple[str, ...] = ()
    created_at: str = field(default_factory=utc_now_iso)

    def __post_init__(self) -> None:
        _require(self.context_packet_id, "context_packet_id", "ContextPacket")
        _require(self.run_id, "run_id", "ContextPacket")

    def content_hash(self) -> str:
        payload = self.to_dict()
        payload.pop("hash", None)
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]

    def to_dict(self) -> dict[str, Any]:
        return prune(
            {
                "context_packet_id": self.context_packet_id,
                "run_id": self.run_id,
                "source_thread_summary": self.source_thread_summary,
                "linked_resources": list(self.linked_resources),
                "channel_brain_facts": list(self.channel_brain_facts),
                "previous_run_ids": list(self.previous_run_ids),
                "connector_platforms": list(self.connector_platforms),
                "policy_constraints": dict(self.policy_constraints),
                "citations": list(self.citations),
                "created_at": self.created_at,
            }
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ContextPacket:
        return cls(
            context_packet_id=data.get("context_packet_id", ""),
            run_id=data.get("run_id", ""),
            source_thread_summary=data.get("source_thread_summary", ""),
            linked_resources=tuple(data.get("linked_resources", ())),
            channel_brain_facts=tuple(data.get("channel_brain_facts", ())),
            previous_run_ids=tuple(data.get("previous_run_ids", ())),
            connector_platforms=tuple(data.get("connector_platforms", ())),
            policy_constraints=dict(data.get("policy_constraints", {})),
            citations=tuple(data.get("citations", ())),
            created_at=data.get("created_at", utc_now_iso()),
        )


@dataclass(frozen=True)
class VerificationCheck:
    name: str
    status: str = "skipped"
    evidence: str = ""

    def __post_init__(self) -> None:
        _require_choice(self.status, ("pass", "fail", "skipped"), "status", "VerificationCheck")

    def to_dict(self) -> dict[str, Any]:
        return prune({"name": self.name, "status": self.status, "evidence": self.evidence})

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> VerificationCheck:
        return cls(
            name=data.get("name", ""),
            status=data.get("status", "skipped"),
            evidence=data.get("evidence", ""),
        )


@dataclass(frozen=True)
class ActionReceipt:
    """A proposed or applied mutation against a target system."""

    action_id: str
    run_id: str
    title: str
    target_system: str
    target_object: str
    mutation_type: str
    summary: str = ""
    diff: str = ""
    risk: str = "medium"
    required_permissions: tuple[str, ...] = ("propose",)
    state: str = "proposed"
    verification: tuple[VerificationCheck, ...] = ()
    apply_payload: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)

    def __post_init__(self) -> None:
        _require(self.action_id, "action_id", "ActionReceipt")
        _require(self.run_id, "run_id", "ActionReceipt")
        _require(self.title, "title", "ActionReceipt")
        _require(self.target_system, "target_system", "ActionReceipt")
        _require(self.target_object, "target_object", "ActionReceipt")
        _require_choice(self.mutation_type, MUTATION_TYPES, "mutation_type", "ActionReceipt")
        _require_choice(self.risk, RISK_LEVELS, "risk", "ActionReceipt")
        _require_choice(self.state, RECEIPT_STATES, "state", "ActionReceipt")
        for permission in self.required_permissions:
            _require_choice(
                permission, PERMISSION_LEVELS, "required_permissions", "ActionReceipt"
            )

    @property
    def commands(self) -> dict[str, str]:
        return {
            "approve": f"approve {self.action_id}",
            "reject": f"reject {self.action_id}",
            "apply": f"apply {self.action_id}",
            "continue": f"continue {self.run_id}",
            "status": f"status {self.run_id}",
        }

    def with_state(self, state: str) -> ActionReceipt:
        _require_choice(state, RECEIPT_STATES, "state", "ActionReceipt")
        return ActionReceipt(
            action_id=self.action_id,
            run_id=self.run_id,
            title=self.title,
            target_system=self.target_system,
            target_object=self.target_object,
            mutation_type=self.mutation_type,
            summary=self.summary,
            diff=self.diff,
            risk=self.risk,
            required_permissions=self.required_permissions,
            state=state,
            verification=self.verification,
            apply_payload=self.apply_payload,
            created_at=self.created_at,
            updated_at=utc_now_iso(),
        )

    def to_dict(self) -> dict[str, Any]:
        return prune(
            {
                "action_id": self.action_id,
                "run_id": self.run_id,
                "title": self.title,
                "target_system": self.target_system,
                "target_object": self.target_object,
                "mutation_type": self.mutation_type,
                "summary": self.summary,
                "diff": self.diff,
                "risk": self.risk,
                "required_permissions": list(self.required_permissions),
                "state": self.state,
                "verification": [check.to_dict() for check in self.verification],
                "apply_payload": self.apply_payload,
                "commands": self.commands,
                "created_at": self.created_at,
                "updated_at": self.updated_at,
            }
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ActionReceipt:
        return cls(
            action_id=data.get("action_id", ""),
            run_id=data.get("run_id", ""),
            title=data.get("title", ""),
            target_system=data.get("target_system", ""),
            target_object=data.get("target_object", ""),
            mutation_type=data.get("mutation_type", "update"),
            summary=data.get("summary", ""),
            diff=data.get("diff", ""),
            risk=data.get("risk", "medium"),
            required_permissions=tuple(data.get("required_permissions", ("propose",))),
            state=data.get("state", "proposed"),
            verification=tuple(
                VerificationCheck.from_dict(check) for check in data.get("verification", ())
            ),
            apply_payload=dict(data.get("apply_payload", {})),
            created_at=data.get("created_at", utc_now_iso()),
            updated_at=data.get("updated_at", utc_now_iso()),
        )


@dataclass(frozen=True)
class Artifact:
    artifact_id: str
    run_id: str
    name: str
    kind: str = "file"
    path: str = ""
    url: str = ""
    sha256_16: str = ""
    size_bytes: int = 0
    created_at: str = field(default_factory=utc_now_iso)

    def __post_init__(self) -> None:
        _require(self.artifact_id, "artifact_id", "Artifact")
        _require(self.run_id, "run_id", "Artifact")
        _require(self.name, "name", "Artifact")
        if not self.path and not self.url:
            raise SchemaError(
                code="missing_location",
                message="Artifact requires an absolute path or a URL",
                recovery_hint="Set 'path' to an absolute file path or 'url' to a link.",
            )

    def to_dict(self) -> dict[str, Any]:
        return prune(
            {
                "artifact_id": self.artifact_id,
                "run_id": self.run_id,
                "name": self.name,
                "kind": self.kind,
                "path": self.path,
                "url": self.url,
                "sha256_16": self.sha256_16,
                "size_bytes": self.size_bytes or None,
                "created_at": self.created_at,
            }
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Artifact:
        return cls(
            artifact_id=data.get("artifact_id", ""),
            run_id=data.get("run_id", ""),
            name=data.get("name", ""),
            kind=data.get("kind", "file"),
            path=data.get("path", ""),
            url=data.get("url", ""),
            sha256_16=data.get("sha256_16", ""),
            size_bytes=int(data.get("size_bytes", 0)),
            created_at=data.get("created_at", utc_now_iso()),
        )


@dataclass(frozen=True)
class PermissionGrant:
    grant_id: str
    actor_id: str
    platform: str
    channel_id: str = "*"
    permission_level: str = "propose"
    granted_by: str = ""
    expires_at: str = ""
    created_at: str = field(default_factory=utc_now_iso)

    def __post_init__(self) -> None:
        _require(self.grant_id, "grant_id", "PermissionGrant")
        _require(self.actor_id, "actor_id", "PermissionGrant")
        _require_choice(self.platform, PLATFORMS, "platform", "PermissionGrant")
        _require_choice(
            self.permission_level, PERMISSION_LEVELS, "permission_level", "PermissionGrant"
        )

    def allows(self, permission: str) -> bool:
        order = {level: index for index, level in enumerate(PERMISSION_LEVELS)}
        return order.get(self.permission_level, 0) >= order.get(permission, 0)

    def to_dict(self) -> dict[str, Any]:
        return prune(
            {
                "grant_id": self.grant_id,
                "actor_id": self.actor_id,
                "platform": self.platform,
                "channel_id": self.channel_id,
                "permission_level": self.permission_level,
                "granted_by": self.granted_by,
                "expires_at": self.expires_at,
                "created_at": self.created_at,
            }
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PermissionGrant:
        return cls(
            grant_id=data.get("grant_id", ""),
            actor_id=data.get("actor_id", ""),
            platform=data.get("platform", ""),
            channel_id=data.get("channel_id", "*"),
            permission_level=data.get("permission_level", "propose"),
            granted_by=data.get("granted_by", ""),
            expires_at=data.get("expires_at", ""),
            created_at=data.get("created_at", utc_now_iso()),
        )


@dataclass(frozen=True)
class ApprovalDecision:
    decision_id: str
    action_id: str
    run_id: str
    actor_id: str
    decision: str
    reason: str = ""
    created_at: str = field(default_factory=utc_now_iso)

    def __post_init__(self) -> None:
        _require(self.decision_id, "decision_id", "ApprovalDecision")
        _require(self.action_id, "action_id", "ApprovalDecision")
        _require(self.actor_id, "actor_id", "ApprovalDecision")
        _require_choice(self.decision, ("approve", "reject"), "decision", "ApprovalDecision")

    def to_dict(self) -> dict[str, Any]:
        return prune(
            {
                "decision_id": self.decision_id,
                "action_id": self.action_id,
                "run_id": self.run_id,
                "actor_id": self.actor_id,
                "decision": self.decision,
                "reason": self.reason,
                "created_at": self.created_at,
            }
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ApprovalDecision:
        return cls(
            decision_id=data.get("decision_id", ""),
            action_id=data.get("action_id", ""),
            run_id=data.get("run_id", ""),
            actor_id=data.get("actor_id", ""),
            decision=data.get("decision", "reject"),
            reason=data.get("reason", ""),
            created_at=data.get("created_at", utc_now_iso()),
        )


@dataclass(frozen=True)
class ApplyPlan:
    plan_id: str
    action_id: str
    run_id: str
    steps: tuple[str, ...] = ()
    rollback_hint: str = ""
    requires_approval: bool = True
    created_at: str = field(default_factory=utc_now_iso)

    def __post_init__(self) -> None:
        _require(self.plan_id, "plan_id", "ApplyPlan")
        _require(self.action_id, "action_id", "ApplyPlan")
        if not self.steps:
            raise SchemaError(
                code="empty_plan",
                message="ApplyPlan.steps must contain at least one step",
                recovery_hint="Describe the mutation as one or more ordered steps.",
            )

    def to_dict(self) -> dict[str, Any]:
        return prune(
            {
                "plan_id": self.plan_id,
                "action_id": self.action_id,
                "run_id": self.run_id,
                "steps": list(self.steps),
                "rollback_hint": self.rollback_hint,
                "requires_approval": self.requires_approval,
                "created_at": self.created_at,
            }
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ApplyPlan:
        return cls(
            plan_id=data.get("plan_id", ""),
            action_id=data.get("action_id", ""),
            run_id=data.get("run_id", ""),
            steps=tuple(data.get("steps", ())),
            rollback_hint=data.get("rollback_hint", ""),
            requires_approval=bool(data.get("requires_approval", True)),
            created_at=data.get("created_at", utc_now_iso()),
        )


@dataclass(frozen=True)
class LedgerEvent:
    """A single immutable audit record appended to the run ledger."""

    event_id: str
    run_id: str
    event_type: str
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now_iso)

    def __post_init__(self) -> None:
        _require(self.event_id, "event_id", "LedgerEvent")
        _require(self.run_id, "run_id", "LedgerEvent")
        _require_choice(self.event_type, LEDGER_EVENT_TYPES, "event_type", "LedgerEvent")

    def to_dict(self) -> dict[str, Any]:
        return prune(
            {
                "event_id": self.event_id,
                "run_id": self.run_id,
                "event_type": self.event_type,
                "payload": self.payload,
                "created_at": self.created_at,
            }
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LedgerEvent:
        return cls(
            event_id=data.get("event_id", ""),
            run_id=data.get("run_id", ""),
            event_type=data.get("event_type", ""),
            payload=dict(data.get("payload", {})),
            created_at=data.get("created_at", utc_now_iso()),
        )


@dataclass(frozen=True)
class ConnectorCapability:
    """Declaration of what a platform adapter/connector can and cannot do."""

    platform: str
    connector: str
    capabilities: dict[str, bool | str] = field(default_factory=dict)
    limits: dict[str, Any] = field(default_factory=dict)
    default_mode: str = "propose"
    destructive_requires_approval: bool = True
    status: str = "available"

    def __post_init__(self) -> None:
        _require_choice(self.platform, PLATFORMS, "platform", "ConnectorCapability")
        _require(self.connector, "connector", "ConnectorCapability")
        _require_choice(
            self.default_mode, ("read", "propose", "write"), "default_mode", "ConnectorCapability"
        )
        _require_choice(
            self.status, ("available", "planned"), "status", "ConnectorCapability"
        )

    def can(self, capability: str) -> bool:
        return self.capabilities.get(capability) is True

    def to_dict(self) -> dict[str, Any]:
        return prune(
            {
                "platform": self.platform,
                "connector": self.connector,
                "capabilities": self.capabilities,
                "limits": self.limits,
                "policy": {
                    "default_mode": self.default_mode,
                    "destructive_requires_approval": self.destructive_requires_approval,
                },
                "status": self.status,
            }
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ConnectorCapability:
        policy = data.get("policy", {})
        return cls(
            platform=data.get("platform", ""),
            connector=data.get("connector", ""),
            capabilities=dict(data.get("capabilities", {})),
            limits=dict(data.get("limits", {})),
            default_mode=policy.get("default_mode", "propose"),
            destructive_requires_approval=bool(
                policy.get("destructive_requires_approval", True)
            ),
            status=data.get("status", "available"),
        )
