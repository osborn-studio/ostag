"""A1 trusted grant configuration intersected with current core permissions.

No self-grant endpoint, persistent grant storage or revocation service exists
here. Lifecycle integration is a later task: callers must invoke authorize
again before each read, model egress, commit or delivery side effect. A returned
grant is only the immutable context of this check, never durable approval.
Only read and generate are supported in A01; writes and delivery are not.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import ClassVar

from ostag.briefs.models import (
    _audience,
    _audience_from_dict,
    _identifier,
    _ids,
    _invalid,
    _keys,
    _positive_int,
    _principal,
    _principal_from_dict,
    _sequence,
    _text,
    _timestamp,
    _utc,
)
from ostag.config import Settings
from ostag.core.identity import Principal
from ostag.core.permissions import level_allows, resolve_principal_permission
from ostag.core.schemas import PERMISSION_LEVELS, SchemaError


@dataclass(frozen=True)
class PilotGrant:
    """Trusted configuration-issued, exact resource allowlist for a private pilot.

    Parsing validates shape, not issuance: only the injected trusted lookup can
    supply an authoritative grant. A1 audience must be exactly the requester.
    revoked_at is omitted on the wire when unset; explicit wire null is invalid.
    """

    grant_id: str
    namespace: str
    principal: Principal
    resource_ids: tuple[str, ...]
    purpose: str
    allowed_audience: tuple[Principal, ...]
    model_egress_allowed: bool
    valid_from: str
    expires_at: str
    revision: int
    revoked_at: str | None = None

    _KEYS: ClassVar = (
        "grant_id", "namespace", "principal", "resource_ids", "purpose", "allowed_audience",
        "model_egress_allowed", "valid_from", "expires_at", "revision",
    )

    def __post_init__(self) -> None:
        for field in ("grant_id", "namespace", "purpose"):
            _identifier(getattr(self, field), field)
        principal, audience = _principal(self.principal), _audience(self.allowed_audience)
        if audience != (principal,):
            raise _invalid("A1 grant audience must be exactly the requester")
        if type(self.model_egress_allowed) is not bool:
            raise _invalid("model_egress_allowed must be an explicit boolean")
        _positive_int(self.revision, "revision")
        if _utc(self.valid_from, "valid_from") >= _utc(self.expires_at, "expires_at"):
            raise _invalid("grant valid_from must precede expiry")
        if self.revoked_at is not None:
            object.__setattr__(self, "revoked_at", _timestamp(self.revoked_at, "revoked_at"))
        object.__setattr__(self, "valid_from", _timestamp(self.valid_from, "valid_from"))
        object.__setattr__(self, "expires_at", _timestamp(self.expires_at, "expires_at"))
        object.__setattr__(self, "principal", principal)
        object.__setattr__(self, "allowed_audience", audience)
        object.__setattr__(self, "resource_ids", _ids(self.resource_ids, "resource_ids"))

    @classmethod
    def from_dict(cls, payload: object) -> PilotGrant:
        values = _keys(payload, cls._KEYS, ("revoked_at",))
        if "revoked_at" in values:
            _utc(values["revoked_at"], "revoked_at")
        values["principal"] = _principal_from_dict(values["principal"])
        values["allowed_audience"] = _audience_from_dict(values["allowed_audience"])
        return cls(**values)

    def to_dict(self) -> dict:
        payload = {key: getattr(self, key) for key in self._KEYS} | {
            "principal": self.principal.to_dict(),
            "resource_ids": list(self.resource_ids),
            "allowed_audience": [actor.to_dict() for actor in self.allowed_audience],
        }
        if self.revoked_at is not None:
            payload["revoked_at"] = self.revoked_at
        return payload


# Supplied by trusted application configuration, never by the requesting user/model.
# The lookup must read current state on each invocation and return None if missing.
GrantLookup = Callable[[str], PilotGrant | None]


def _denied(message: str) -> SchemaError:
    return SchemaError("brief_authorization_denied", message,
                       "Re-check current trusted grant and core permissions before retrying.")


def _permission_levels(config: Settings, channel_grants: tuple[str, ...]) -> None:
    """Do not let the core parser silently discard an unknown permission level."""
    if type(config.default_permission_level) is not str or (
        config.default_permission_level not in PERMISSION_LEVELS
    ):
        raise _denied("unknown default permission level")
    for pairs in (config.actor_permissions, channel_grants):
        for pair in _sequence(pairs, "permission grants", maximum=10000, empty=True):
            _text(pair, "permission grant", 1024)
            parts = tuple(part.strip() for part in pair.split(":"))
            if len(parts) not in (2, 3) or not all(parts):
                raise _denied("malformed configured permission grant")
            if parts[-1] not in PERMISSION_LEVELS:
                raise _denied("unknown configured permission level")
            if len(parts) == 3:
                _principal_from_dict({"platform": parts[0], "actor_id": parts[1]})


def authorize(
    *,
    lookup: GrantLookup,
    config: Settings,
    grant_id: str,
    grant_revision: int,
    namespace: str,
    principal: Principal,
    resource_ids: tuple[str, ...],
    purpose: str,
    allowed_audience: tuple[Principal, ...],
    operation: str,
    now: str,
    channel_grants: tuple[str, ...] = (),
) -> PilotGrant:
    """Re-resolve the pinned grant and core permissions for one imminent action.

    lookup, config, channel_grants and the UTC clock value must come from trusted
    application code. No caller-provided grant object or prior success is used
    as authority. Resource IDs are exact within the grant's namespace.
    """
    _identifier(grant_id, "grant_id")
    if not callable(lookup):
        raise _invalid("lookup must be a trusted current-grant callable")
    current = lookup(grant_id)
    if type(current) is not PilotGrant:
        raise _denied("current grant is missing or invalid")
    current = replace(current)
    _positive_int(grant_revision, "grant_revision")
    if current.grant_id != grant_id or current.revision != grant_revision:
        raise _denied("pinned grant ID/revision is stale or mismatched")
    instant = _utc(now, "now")
    if not _utc(current.valid_from, "valid_from") <= instant < _utc(current.expires_at, "expiry"):
        raise _denied("grant is future or expired")
    if current.revoked_at is not None and instant >= _utc(current.revoked_at, "revoked_at"):
        raise _denied("grant is revoked")
    _identifier(namespace, "namespace")
    _identifier(purpose, "purpose")
    principal = _principal(principal)
    resources, audience = _ids(resource_ids, "resource_ids"), _audience(allowed_audience)
    if (current.namespace != namespace or current.principal != principal
            or current.purpose != purpose or current.allowed_audience != audience
            or not set(resources) <= set(current.resource_ids)):
        raise _denied("requested scope is outside the current grant")
    if type(operation) is not str or operation not in ("read", "generate"):
        raise _denied("unsupported A01 operation")
    if operation == "generate" and not current.model_egress_allowed:
        raise _denied("A1 model egress was not explicitly granted")
    if not isinstance(config, Settings):
        raise _invalid("config must be current Settings")
    _permission_levels(config, channel_grants)
    level = resolve_principal_permission(config, principal, channel_grants=channel_grants)
    if type(level) is not str or level not in PERMISSION_LEVELS:
        raise _denied("unknown resolved permission level")
    required = "read" if operation == "read" else "propose"
    if not level_allows(level, required):
        raise _denied(f"core permission does not allow {operation}")
    return current
