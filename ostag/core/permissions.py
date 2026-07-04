"""Actor permission resolution and receipt-verb gating.

Permission levels are ordered: ``read < propose < write < admin``. Actors are
mapped to levels via ``OSTAG_ACTOR_PERMISSIONS`` (``actor_id:level`` pairs);
everyone else gets ``OSTAG_DEFAULT_PERMISSION_LEVEL`` (default ``propose``).
The reserved actor ``local-admin`` — used by the local CLI — defaults to
``admin`` unless explicitly configured otherwise, because the CLI runs on the
operator's own machine.
"""

from __future__ import annotations

from ostag.config import Settings
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


def resolve_permission(config: Settings, actor_id: str) -> str:
    mapping = parse_actor_permissions(config.actor_permissions)
    if actor_id in mapping:
        return mapping[actor_id]
    if actor_id == LOCAL_ADMIN_ACTOR:
        return "admin"
    return config.default_permission_level


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
                f"Actor {actor_id!r} has permission level {level!r}; "
                f"'{verb}' requires {required!r}"
            ),
            recovery_hint=(
                "Ask an admin to raise your level via OSTAG_ACTOR_PERMISSIONS "
                f"(e.g. {actor_id}:{required})."
            ),
        )
