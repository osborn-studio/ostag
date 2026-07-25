"""Agent access-profile snapshots (Phase 3, shadow mode).

Ported from OpenTag Phase 2 (`amplifthq/opentag` @ ``cc1f4fb``, read at the
containing head ``de70cca``): ``AgentAccessProfileSnapshotSchema``,
``AgentPrincipalSchema``, ``PolicySnapshotProvenance`` reference, and the
``ConnectionRef``/constraint vocabularies in ``packages/core/src/schema.ts``.

Shadow-only this cycle: snapshots are captured at intake, persisted
immutably with a content digest, and exposed read-only — they are **never**
consulted to reject intake, filter candidates, select a route, or alter
fallback (see the Phase 3 mapping note).

Declared native mappings (mapping note): ``requestedBy`` is OStag's compact
``{platform, actorId, handle?}`` principal reference (upstream
``ActorIdentity``); ``permissions[].scope`` is a free string (upstream binds
it to ``PermissionScope``); the ``ostagBinding`` block is a native extension
carrying the existing Device/Runtime/Session/Turn/Version identity, with
``runner ↔ device/runtime host`` and ``executor ↔ agent adapter``.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ostag.core.completion import _require_choice, _validate_keys
from ostag.core.schemas import SchemaError

AGENT_PRINCIPAL_KINDS = ("opentag_agent", "provider_agent", "custom")
CONNECTION_CUSTODY = ("agent", "opentag", "operator")
LOCALITY = ("local_required", "private_required", "hosted_allowed")
ACTION_RISK_TIERS = ("low", "medium", "high", "critical")


def _prune(value: dict[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if item not in (None, "", (), [], {})}


def _instant(value: str) -> datetime:
    """Parse an already-validated UTC timestamp for comparison."""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _require_str(value: object, name: str, schema: str) -> None:
    """Upstream fields are ``z.string().min(1)`` — reject non-strings too,
    not just empties (an integer id must not pass as truthy)."""
    if not isinstance(value, str) or not value:
        raise SchemaError(
            code="invalid_type",
            message=f"{schema}.{name} must be a non-empty string",
            recovery_hint=f"Provide '{name}' as a non-empty string.",
        )


def _optional_str(value: object, name: str, schema: str) -> None:
    if not isinstance(value, str):
        raise SchemaError(
            code="invalid_type",
            message=f"{schema}.{name} must be a string",
            recovery_hint=f"Provide '{name}' as a string (or omit it).",
        )


# Strict UTC grammar: T-separated, optional dot fraction, Z or +00:00 only.
# Deliberately narrower than datetime.fromisoformat (which accepts any
# single-character separator, week dates, and comma fractions).
_UTC_DATETIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?(Z|\+00:00)$")


def _require_utc_datetime(value: object, name: str, schema: str, *, optional: bool = False) -> None:
    if optional and value == "":
        return
    _require_str(value, name, schema)
    assert isinstance(value, str)
    if not _UTC_DATETIME.match(value):
        raise SchemaError(
            code="invalid_datetime",
            message=f"{schema}.{name}={value!r} is not a UTC timestamp "
            "(YYYY-MM-DDTHH:MM:SS[.ffffff] with Z or +00:00)",
            recovery_hint="Use e.g. 2026-07-26T10:00:00+00:00.",
        )
    try:
        _instant(value)
    except ValueError:
        raise SchemaError(
            code="invalid_datetime",
            message=f"{schema}.{name}={value!r} is not a valid calendar timestamp",
            recovery_hint="Use a real date/time, e.g. 2026-07-26T10:00:00+00:00.",
        ) from None


def _require_instance(value: object, expected: type, name: str, schema: str) -> None:
    """Nested fields must be the model type — a raw dict must not slip through
    the constructor and later escape as an AttributeError."""
    if not isinstance(value, expected):
        raise SchemaError(
            code="invalid_type",
            message=f"{schema}.{name} must be a {expected.__name__}",
            recovery_hint=f"Build '{name}' via {expected.__name__} / from_dict.",
        )


def _require_str_tuple(value: object, name: str, schema: str) -> tuple[str, ...]:
    """Upstream arrays are lists of non-empty strings; a bare string or other
    iterable must not silently decompose into elements."""
    if not isinstance(value, (list, tuple)):
        raise SchemaError(
            code="invalid_type",
            message=f"{schema}.{name} must be a list of strings",
            recovery_hint=f"Provide '{name}' as a JSON array of non-empty strings.",
        )
    for item in value:
        if not isinstance(item, str) or not item:
            raise SchemaError(
                code="invalid_type",
                message=f"{schema}.{name} elements must be non-empty strings",
                recovery_hint=f"Remove empty or non-string entries from '{name}'.",
            )
    return tuple(value)


def _require_wire_keys(data: dict[str, Any], required: frozenset[str], schema: str) -> None:
    """Upstream schemas require these keys to be present on the wire."""
    if not isinstance(data, dict):
        raise SchemaError(
            code="invalid_type",
            message=f"{schema} payload must be an object",
            recovery_hint=f"Provide {schema} as a JSON object.",
        )
    missing = required - set(data)
    if missing:
        raise SchemaError(
            code="missing_field",
            message=f"{schema} is missing required field(s): {', '.join(sorted(missing))}",
            recovery_hint=f"Provide: {', '.join(sorted(required))}.",
        )


def _require_object_list(value: object, name: str, schema: str) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, (list, tuple)) or any(
        not isinstance(item, dict) for item in value
    ):
        raise SchemaError(
            code="invalid_type",
            message=f"{schema}.{name} must be a list of objects",
            recovery_hint=f"Provide '{name}' as a JSON array of objects.",
        )
    return tuple(value)


@dataclass(frozen=True)
class AgentPrincipal:
    id: str
    kind: str

    def __post_init__(self) -> None:
        _require_str(self.id, "id", "AgentPrincipal")
        _require_choice(self.kind, AGENT_PRINCIPAL_KINDS, "kind", "AgentPrincipal")

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "kind": self.kind}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AgentPrincipal":
        _validate_keys(data, frozenset({"id", "kind"}), "AgentPrincipal")
        return cls(id=data.get("id", ""), kind=data.get("kind", ""))


@dataclass(frozen=True)
class PrincipalRef:
    """Native compact form of upstream ActorIdentity (mapping note)."""

    platform: str
    actor_id: str
    handle: str = ""

    def __post_init__(self) -> None:
        _require_str(self.platform, "platform", "PrincipalRef")
        _require_str(self.actor_id, "actor_id", "PrincipalRef")
        _optional_str(self.handle, "handle", "PrincipalRef")

    def to_dict(self) -> dict[str, Any]:
        return _prune({"platform": self.platform, "actorId": self.actor_id, "handle": self.handle})

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PrincipalRef":
        _validate_keys(data, frozenset({"platform", "actorId", "handle"}), "PrincipalRef")
        return cls(
            platform=data.get("platform", ""),
            actor_id=data.get("actorId", ""),
            handle=data.get("handle", ""),
        )


@dataclass(frozen=True)
class PermissionGrantRef:
    scope: str
    reason: str
    expires_at: str = ""

    def __post_init__(self) -> None:
        _require_str(self.scope, "scope", "PermissionGrantRef")
        _require_str(self.reason, "reason", "PermissionGrantRef")
        _require_utc_datetime(self.expires_at, "expires_at", "PermissionGrantRef", optional=True)

    def to_dict(self) -> dict[str, Any]:
        return _prune({"scope": self.scope, "reason": self.reason, "expiresAt": self.expires_at})

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PermissionGrantRef":
        _validate_keys(data, frozenset({"scope", "reason", "expiresAt"}), "PermissionGrantRef")
        return cls(
            scope=data.get("scope", ""),
            reason=data.get("reason", ""),
            expires_at=data.get("expiresAt", ""),
        )


@dataclass(frozen=True)
class ConnectionRef:
    id: str
    provider: str
    custody: str
    broker_ref: str
    declared_capabilities: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_str(self.id, "id", "ConnectionRef")
        _require_str(self.provider, "provider", "ConnectionRef")
        _require_choice(self.custody, CONNECTION_CUSTODY, "custody", "ConnectionRef")
        _require_str(self.broker_ref, "broker_ref", "ConnectionRef")
        _require_str_tuple(self.declared_capabilities, "declared_capabilities", "ConnectionRef")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "provider": self.provider,
            "custody": self.custody,
            "brokerRef": self.broker_ref,
            "declaredCapabilities": list(self.declared_capabilities),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ConnectionRef":
        _validate_keys(
            data,
            frozenset({"id", "provider", "custody", "brokerRef", "declaredCapabilities"}),
            "ConnectionRef",
        )
        _require_wire_keys(
            data,
            frozenset({"id", "provider", "custody", "brokerRef", "declaredCapabilities"}),
            "ConnectionRef",
        )
        return cls(
            id=data.get("id", ""),
            provider=data.get("provider", ""),
            custody=data.get("custody", ""),
            broker_ref=data.get("brokerRef", ""),
            declared_capabilities=_require_str_tuple(
                data.get("declaredCapabilities"), "declaredCapabilities", "ConnectionRef"
            ),
        )


@dataclass(frozen=True)
class AccessConstraints:
    locality: str = ""
    maximum_risk_tier: str = ""
    allowed_executor_ids: tuple[str, ...] = ()
    allowed_runner_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.locality:
            _require_choice(self.locality, LOCALITY, "locality", "AccessConstraints")
        if self.maximum_risk_tier:
            _require_choice(
                self.maximum_risk_tier, ACTION_RISK_TIERS, "maximum_risk_tier", "AccessConstraints"
            )
        _require_str_tuple(self.allowed_executor_ids, "allowed_executor_ids", "AccessConstraints")
        _require_str_tuple(self.allowed_runner_ids, "allowed_runner_ids", "AccessConstraints")

    def to_dict(self) -> dict[str, Any]:
        return _prune(
            {
                "locality": self.locality,
                "maximumRiskTier": self.maximum_risk_tier,
                "allowedExecutorIds": list(self.allowed_executor_ids),
                "allowedRunnerIds": list(self.allowed_runner_ids),
            }
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AccessConstraints":
        _validate_keys(
            data,
            frozenset(
                {"locality", "maximumRiskTier", "allowedExecutorIds", "allowedRunnerIds"}
            ),
            "AccessConstraints",
        )
        return cls(
            locality=data.get("locality", ""),
            maximum_risk_tier=data.get("maximumRiskTier", ""),
            allowed_executor_ids=_require_str_tuple(
                data.get("allowedExecutorIds", ()), "allowedExecutorIds", "AccessConstraints"
            ),
            allowed_runner_ids=_require_str_tuple(
                data.get("allowedRunnerIds", ()), "allowedRunnerIds", "AccessConstraints"
            ),
        )


@dataclass(frozen=True)
class OstagBinding:
    """Native extension: the existing precise routing identity, frozen at intake."""

    instance_id: str
    runtime_id: str = ""
    session_id: str = ""
    turn_id: str = ""
    session_version: int = 0

    def __post_init__(self) -> None:
        _require_str(self.instance_id, "instance_id", "OstagBinding")
        for name in ("runtime_id", "session_id", "turn_id"):
            _optional_str(getattr(self, name), name, "OstagBinding")
        if (
            isinstance(self.session_version, bool)
            or not isinstance(self.session_version, int)
            or self.session_version < 0
        ):
            raise SchemaError(
                code="invalid_type",
                message="OstagBinding.session_version must be a non-negative integer",
                recovery_hint="Provide sessionVersion as an integer >= 0 (0 = unset).",
            )

    def to_dict(self) -> dict[str, Any]:
        payload = _prune(
            {
                "instanceId": self.instance_id,
                "runtimeId": self.runtime_id,
                "sessionId": self.session_id,
                "turnId": self.turn_id,
            }
        )
        if self.session_version:
            payload["sessionVersion"] = self.session_version
        return payload

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "OstagBinding":
        _validate_keys(
            data,
            frozenset({"instanceId", "runtimeId", "sessionId", "turnId", "sessionVersion"}),
            "OstagBinding",
        )
        return cls(
            instance_id=data.get("instanceId", ""),
            runtime_id=data.get("runtimeId", ""),
            session_id=data.get("sessionId", ""),
            turn_id=data.get("turnId", ""),
            session_version=data.get("sessionVersion", 0),
        )


@dataclass(frozen=True)
class AgentAccessProfileSnapshot:
    id: str
    agent_principal: AgentPrincipal
    requested_by: PrincipalRef
    policy_snapshot_id: str
    captured_at: str
    ostag_binding: OstagBinding
    project_targets: tuple[str, ...] = ()
    connection_refs: tuple[ConnectionRef, ...] = ()
    permissions: tuple[PermissionGrantRef, ...] = ()
    constraints: AccessConstraints = field(default_factory=AccessConstraints)
    expires_at: str = ""
    revoked_at: str = ""

    def __post_init__(self) -> None:
        _require_str(self.id, "id", "AgentAccessProfileSnapshot")
        _require_str(self.policy_snapshot_id, "policy_snapshot_id", "AgentAccessProfileSnapshot")
        _require_instance(
            self.agent_principal, AgentPrincipal, "agent_principal", "AgentAccessProfileSnapshot"
        )
        _require_instance(
            self.requested_by, PrincipalRef, "requested_by", "AgentAccessProfileSnapshot"
        )
        _require_instance(
            self.constraints, AccessConstraints, "constraints", "AgentAccessProfileSnapshot"
        )
        _require_instance(
            self.ostag_binding, OstagBinding, "ostag_binding", "AgentAccessProfileSnapshot"
        )
        _require_str_tuple(self.project_targets, "project_targets", "AgentAccessProfileSnapshot")
        for name, expected, items in (
            ("connection_refs", ConnectionRef, self.connection_refs),
            ("permissions", PermissionGrantRef, self.permissions),
        ):
            if not isinstance(items, tuple) or any(
                not isinstance(item, expected) for item in items
            ):
                raise SchemaError(
                    code="invalid_type",
                    message=f"AgentAccessProfileSnapshot.{name} must be a tuple of "
                    f"{expected.__name__}",
                    recovery_hint=f"Build '{name}' from {expected.__name__} values.",
                )
        _require_utc_datetime(self.captured_at, "captured_at", "AgentAccessProfileSnapshot")
        _require_utc_datetime(
            self.expires_at, "expires_at", "AgentAccessProfileSnapshot", optional=True
        )
        _require_utc_datetime(
            self.revoked_at, "revoked_at", "AgentAccessProfileSnapshot", optional=True
        )
        # Upstream superRefine invariants. Compare parsed instants, never the
        # raw strings: valid UTC forms (Z / +00:00 / space-separated) are not
        # lexically equivalent (Phase 2 review lesson).
        captured = _instant(self.captured_at)
        if self.expires_at and _instant(self.expires_at) <= captured:
            raise SchemaError(
                code="invalid_snapshot",
                message="Agent access profile expiresAt must be later than capturedAt",
                recovery_hint="Use an expiry strictly after the capture time.",
            )
        if self.revoked_at and _instant(self.revoked_at) < captured:
            raise SchemaError(
                code="invalid_snapshot",
                message="Agent access profile revokedAt cannot precede capturedAt",
                recovery_hint="Revocation time must not be earlier than capture time.",
            )

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "agentPrincipal": self.agent_principal.to_dict(),
            "requestedBy": self.requested_by.to_dict(),
            "projectTargets": list(self.project_targets),
            "connectionRefs": [ref.to_dict() for ref in self.connection_refs],
            "permissions": [grant.to_dict() for grant in self.permissions],
            "constraints": self.constraints.to_dict(),
            "policySnapshotId": self.policy_snapshot_id,
            "capturedAt": self.captured_at,
            "ostagBinding": self.ostag_binding.to_dict(),
        }
        if self.expires_at:
            payload["expiresAt"] = self.expires_at
        if self.revoked_at:
            payload["revokedAt"] = self.revoked_at
        return payload

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AgentAccessProfileSnapshot":
        _validate_keys(
            data,
            frozenset(
                {
                    "id",
                    "agentPrincipal",
                    "requestedBy",
                    "projectTargets",
                    "connectionRefs",
                    "permissions",
                    "constraints",
                    "policySnapshotId",
                    "capturedAt",
                    "expiresAt",
                    "revokedAt",
                    "ostagBinding",
                }
            ),
            "AgentAccessProfileSnapshot",
        )
        # Upstream requires these on the wire (schema.ts fields without
        # .optional()); to_dict always emits them, so round-trips stay safe.
        _require_wire_keys(
            data,
            frozenset(
                {
                    "id",
                    "agentPrincipal",
                    "requestedBy",
                    "projectTargets",
                    "connectionRefs",
                    "permissions",
                    "constraints",
                    "policySnapshotId",
                    "capturedAt",
                    "ostagBinding",
                }
            ),
            "AgentAccessProfileSnapshot",
        )
        return cls(
            id=data.get("id", ""),
            agent_principal=AgentPrincipal.from_dict(data.get("agentPrincipal", {})),
            requested_by=PrincipalRef.from_dict(data.get("requestedBy", {})),
            policy_snapshot_id=data.get("policySnapshotId", ""),
            captured_at=data.get("capturedAt", ""),
            ostag_binding=OstagBinding.from_dict(data.get("ostagBinding", {})),
            project_targets=_require_str_tuple(
                data.get("projectTargets"), "projectTargets", "AgentAccessProfileSnapshot"
            ),
            connection_refs=tuple(
                ConnectionRef.from_dict(ref)
                for ref in _require_object_list(
                    data.get("connectionRefs"), "connectionRefs", "AgentAccessProfileSnapshot"
                )
            ),
            permissions=tuple(
                PermissionGrantRef.from_dict(grant)
                for grant in _require_object_list(
                    data.get("permissions"), "permissions", "AgentAccessProfileSnapshot"
                )
            ),
            constraints=AccessConstraints.from_dict(data.get("constraints", {})),
            expires_at=data.get("expiresAt", ""),
            revoked_at=data.get("revokedAt", ""),
        )

    def content_digest(self) -> str:
        canonical = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
        return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"

    def redacted_view(self) -> dict[str, Any]:
        """Board/audit-safe projection: no broker refs, no grant reasons."""
        return _prune(
            {
                "id": self.id,
                "agentPrincipal": self.agent_principal.to_dict(),
                "requestedBy": _prune(
                    {
                        "platform": self.requested_by.platform,
                        "actorId": self.requested_by.actor_id,
                    }
                ),
                "projectTargets": list(self.project_targets),
                "permissionScopes": [grant.scope for grant in self.permissions],
                "constraints": self.constraints.to_dict(),
                "capturedAt": self.captured_at,
                "expiresAt": self.expires_at,
                "revokedAt": self.revoked_at,
                "ostagBinding": self.ostag_binding.to_dict(),
                "contentDigest": self.content_digest(),
            }
        )
