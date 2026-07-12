"""Actor permission resolution and receipt-verb gating.

Permission levels are ordered: ``read < propose < write < admin``. Actors are
mapped to levels via ``OSTAG_ACTOR_PERMISSIONS`` (``actor_id:level`` pairs);
everyone else gets ``OSTAG_DEFAULT_PERMISSION_LEVEL`` (default ``propose``).
The reserved actor ``local-admin`` — used by the local CLI — defaults to
``admin`` unless explicitly configured otherwise, because the CLI runs on the
operator's own machine.
"""

from __future__ import annotations

import warnings

from ostag.config import Settings
from ostag.core.identity import Principal
from ostag.core.schemas import PERMISSION_LEVELS, SchemaError

LOCAL_ADMIN_ACTOR = "local-admin"

# Minimum level required to execute each receipt verb. Rejecting a pending
# action is deliberately cheap (it only prevents writes); approving or
# applying a mutation authorizes a write and therefore requires write level.
VERB_REQUIRED_LEVEL = {
    "status": "read",
    "continue": "propose",
    "reject": "propose",
    "approve": "write",
    "apply": "write",
    "pause": "write",
    "resume": "write",
    "retry": "write",
    "cancel": "write",
}

_ORDER = {level: index for index, level in enumerate(PERMISSION_LEVELS)}


def parse_actor_permissions(pairs: tuple[str, ...]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for pair in pairs:
        actor, _, level = pair.partition(":")
        actor = actor.strip()
        level = level.strip()
        if actor and level in PERMISSION_LEVELS:
            mapping[actor] = level
    return mapping


def parse_principal_permissions(pairs: tuple[str, ...]) -> dict[Principal, str]:
    """Parse strict ``platform:actor_id:level`` grants without bare-actor fallback."""
    mapping: dict[Principal, str] = {}
    for pair in pairs:
        parts = tuple(part.strip() for part in pair.split(":"))
        if len(parts) != 3:
            if len(parts) == 2:
                warnings.warn(
                    f"Ignoring legacy actor grant {pair!r} on a remote Principal path; "
                    "migrate it to platform:actor_id:level",
                    UserWarning,
                    stacklevel=2,
                )
            continue
        platform, actor_id, level = parts
        if platform and actor_id and level in PERMISSION_LEVELS:
            mapping[Principal(platform, actor_id)] = level
    return mapping


def resolve_principal_permission(
    config: Settings, principal: Principal, channel_grants: tuple[str, ...] = ()
) -> str:
    """Resolve only platform-scoped grants; identical actor IDs never alias."""
    channel_mapping = parse_principal_permissions(channel_grants)
    if principal in channel_mapping:
        return channel_mapping[principal]
    mapping = parse_principal_permissions(config.actor_permissions)
    if principal in mapping:
        return mapping[principal]
    return config.default_permission_level


def resolve_permission(
    config: Settings, actor_id: str, channel_grants: tuple[str, ...] = ()
) -> str:
    """Resolve an actor's level: channel grants > global mapping > default.

    ``channel_grants`` are per-channel "actor:level" pairs from the channel
    policy; inside their channel they take precedence over the global
    OSTAG_ACTOR_PERMISSIONS mapping.
    """
    channel_mapping = parse_actor_permissions(channel_grants)
    if actor_id in channel_mapping:
        return channel_mapping[actor_id]
    mapping = parse_actor_permissions(config.actor_permissions)
    if actor_id in mapping:
        return mapping[actor_id]
    if actor_id == LOCAL_ADMIN_ACTOR:
        return "admin"
    return config.default_permission_level


def effective_flag(channel_value: bool | None, global_value: bool) -> bool:
    """Channel override when set; otherwise inherit the global config value."""
    return global_value if channel_value is None else channel_value


def level_allows(level: str, required: str) -> bool:
    return _ORDER.get(level, 0) >= _ORDER.get(required, 0)


def ensure_verb_allowed(actor_id: str, level: str, verb: str) -> None:
    required = VERB_REQUIRED_LEVEL.get(verb)
    if required is None:
        raise SchemaError(
            code="unsupported_verb",
            message=f"Unknown receipt verb {verb!r}",
            recovery_hint=f"Use one of: {', '.join(VERB_REQUIRED_LEVEL)}.",
        )
    if not level_allows(level, required):
        raise SchemaError(
            code="permission_denied",
            message=(
                f"Actor {actor_id!r} has permission level {level!r}; '{verb}' requires {required!r}"
            ),
            recovery_hint=(
                "Ask an admin to raise your level via OSTAG_ACTOR_PERMISSIONS "
                f"(e.g. {actor_id}:{required})."
            ),
        )
