"""Structured, time-bounded human escalation (Phase 4 of the v0.3→v0.4 upgrade).

Ported from OpenTag (`amplifthq/opentag` @ ``cc1f4fb``, read at the containing
head ``de70cca``): ``HumanEscalationSchema``, ``HumanEscalationOptionSchema``,
and ``ActionHintSchema`` in ``packages/core/src/schema.ts``, including every
superRefine invariant. Lifecycle semantics (dedupe, races, deadline coercion)
live in :mod:`ostag.work_monitor.escalation_store`.

The core rule this phase exists to enforce: **an expired escalation never
authorizes anything** — a late acknowledgement or resolution is coerced to
``expired``, and expired escalations are inert for the Phase 1 evaluator.

Native mappings (declared in the Phase 4 mapping note): actors use OStag's
compact :class:`~ostag.core.completion.ActorRef` (upstream ``ActorIdentity``);
timestamps use the strict Phase 3 UTC grammar; comparisons use parsed
instants, never raw strings.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ostag.core.access_snapshot import (
    _instant,
    _optional_str,
    _require_instance,
    _require_object_list,
    _require_str,
    _require_str_tuple,
    _require_utc_datetime,
    _require_wire_keys,
)
from ostag.core.completion import ActorRef, EscalationInput, _require_choice, _validate_keys
from ostag.core.schemas import SchemaError

ESCALATION_CLASSES = (
    "approval",
    "missing_input",
    "configuration",
    "verification",
    "reconciliation",
    "security",
)
ESCALATION_AUDIENCES = (
    "requester",
    "work_item_owner",
    "repo_owner",
    "operator",
    "security",
)
ESCALATION_STATES = ("open", "acknowledged", "resolved", "expired", "superseded")
ACTION_HINT_KINDS = (
    "apply_suggested_changes",
    "generate_patch",
    "request_human_decision",
    "link_to_work_item",
    "request_review",
    "create_pull_request",
    "none",
)

# Handoff §8 audience mapping onto the existing authorization boundary:
# requester-facing audiences surface on the personal view; operator-facing
# audiences require the admin (bound global owner) view.
AUDIENCE_VIEWS = {
    "requester": "personal",
    "work_item_owner": "personal",
    "repo_owner": "admin",
    "operator": "admin",
    "security": "admin",
}


def _prune(value: dict[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if item not in (None, "", (), [], {})}


def _invalid(message: str, hint: str) -> SchemaError:
    return SchemaError(code="invalid_escalation", message=message, recovery_hint=hint)


def _validate_actor(actor: ActorRef, name: str, schema: str) -> None:
    """Phase 4 boundary strengthening of the Phase 1 ActorRef: attributed
    actors must carry typed, non-empty string ids (ActorRef itself only
    checks truthiness, which lets non-string values through)."""
    _require_instance(actor, ActorRef, name, schema)
    _require_str(actor.actor_id, f"{name}.actor_id", schema)
    _optional_str(actor.handle, f"{name}.handle", schema)


def _actor_from_wire(value: object, name: str, schema: str) -> ActorRef:
    if not isinstance(value, dict):
        raise SchemaError(
            code="invalid_type",
            message=f"{schema}.{name} must be an object",
            recovery_hint=f"Provide '{name}' as an actor object.",
        )
    return ActorRef.from_dict(value)


@dataclass(frozen=True)
class HumanEscalationOption:
    id: str
    label: str
    consequence: str

    def __post_init__(self) -> None:
        _require_str(self.id, "id", "HumanEscalationOption")
        _require_str(self.label, "label", "HumanEscalationOption")
        _require_str(self.consequence, "consequence", "HumanEscalationOption")

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "label": self.label, "consequence": self.consequence}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "HumanEscalationOption":
        keys = frozenset({"id", "label", "consequence"})
        _validate_keys(data, keys, "HumanEscalationOption")
        _require_wire_keys(data, keys, "HumanEscalationOption")
        return cls(
            id=data.get("id", ""),
            label=data.get("label", ""),
            consequence=data.get("consequence", ""),
        )


@dataclass(frozen=True)
class ActionHint:
    kind: str
    target_id: str = ""
    selected_intent_ids: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_choice(self.kind, ACTION_HINT_KINDS, "kind", "ActionHint")
        _optional_str(self.target_id, "target_id", "ActionHint")
        _require_str_tuple(self.selected_intent_ids, "selected_intent_ids", "ActionHint")
        if not isinstance(self.metadata, dict):
            raise _invalid(
                "ActionHint.metadata must be an object",
                "Provide metadata as a JSON object (values are unvalidated, as upstream).",
            )

    def to_dict(self) -> dict[str, Any]:
        return _prune(
            {
                "kind": self.kind,
                "targetId": self.target_id,
                "selectedIntentIds": list(self.selected_intent_ids),
                "metadata": dict(self.metadata),
            }
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ActionHint":
        _validate_keys(
            data,
            frozenset({"kind", "targetId", "selectedIntentIds", "metadata"}),
            "ActionHint",
        )
        _require_wire_keys(data, frozenset({"kind"}), "ActionHint")
        selected = data.get("selectedIntentIds", ())
        metadata = data.get("metadata")
        return cls(
            kind=data.get("kind", ""),
            target_id=data.get("targetId", ""),
            selected_intent_ids=_require_str_tuple(
                selected, "selectedIntentIds", "ActionHint"
            ),
            metadata={} if metadata is None else metadata,
        )


@dataclass(frozen=True)
class EscalationAcknowledgement:
    actor: ActorRef
    acknowledged_at: str

    def __post_init__(self) -> None:
        _validate_actor(self.actor, "actor", "EscalationAcknowledgement")
        _require_utc_datetime(
            self.acknowledged_at, "acknowledged_at", "EscalationAcknowledgement"
        )

    def to_dict(self) -> dict[str, Any]:
        return {"actor": self.actor.to_dict(), "acknowledgedAt": self.acknowledged_at}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EscalationAcknowledgement":
        keys = frozenset({"actor", "acknowledgedAt"})
        _validate_keys(data, keys, "EscalationAcknowledgement")
        _require_wire_keys(data, keys, "EscalationAcknowledgement")
        return cls(
            actor=_actor_from_wire(data.get("actor"), "actor", "EscalationAcknowledgement"),
            acknowledged_at=data.get("acknowledgedAt", ""),
        )


@dataclass(frozen=True)
class EscalationResolution:
    actor: ActorRef
    resolved_at: str
    option_id: str = ""
    reason: str = ""

    def __post_init__(self) -> None:
        _validate_actor(self.actor, "actor", "EscalationResolution")
        _require_utc_datetime(self.resolved_at, "resolved_at", "EscalationResolution")
        _optional_str(self.option_id, "option_id", "EscalationResolution")
        _optional_str(self.reason, "reason", "EscalationResolution")

    def to_dict(self) -> dict[str, Any]:
        return _prune(
            {
                "optionId": self.option_id,
                "actor": self.actor.to_dict(),
                "reason": self.reason,
                "resolvedAt": self.resolved_at,
            }
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EscalationResolution":
        _validate_keys(
            data, frozenset({"optionId", "actor", "reason", "resolvedAt"}), "EscalationResolution"
        )
        _require_wire_keys(data, frozenset({"actor", "resolvedAt"}), "EscalationResolution")
        return cls(
            actor=_actor_from_wire(data.get("actor"), "actor", "EscalationResolution"),
            resolved_at=data.get("resolvedAt", ""),
            option_id=data.get("optionId", ""),
            reason=data.get("reason", ""),
        )


@dataclass(frozen=True)
class HumanEscalation:
    id: str
    work_thread_key: str
    escalation_class: str
    audience: str
    subject_ref: str
    state: str
    blocking: bool
    summary: str
    reason: str
    opened_at: str
    run_id: str = ""
    attempt_id: str = ""
    options: tuple[HumanEscalationOption, ...] = ()
    next_action: ActionHint | None = None
    dedupe_key: str = ""
    expires_at: str = ""
    acknowledgement: EscalationAcknowledgement | None = None
    resolution: EscalationResolution | None = None
    terminal_reason: str = ""
    superseded_by_id: str = ""

    def __post_init__(self) -> None:
        _require_str(self.id, "id", "HumanEscalation")
        _require_str(self.work_thread_key, "work_thread_key", "HumanEscalation")
        _require_choice(
            self.escalation_class, ESCALATION_CLASSES, "escalation_class", "HumanEscalation"
        )
        _require_choice(self.audience, ESCALATION_AUDIENCES, "audience", "HumanEscalation")
        _require_str(self.subject_ref, "subject_ref", "HumanEscalation")
        _require_choice(self.state, ESCALATION_STATES, "state", "HumanEscalation")
        if not isinstance(self.blocking, bool):
            raise _invalid(
                "HumanEscalation.blocking must be a boolean",
                "Provide blocking as true or false.",
            )
        _require_str(self.summary, "summary", "HumanEscalation")
        _require_str(self.reason, "reason", "HumanEscalation")
        _require_utc_datetime(self.opened_at, "opened_at", "HumanEscalation")
        for name in ("run_id", "attempt_id", "dedupe_key", "terminal_reason", "superseded_by_id"):
            _optional_str(getattr(self, name), name, "HumanEscalation")
        _require_utc_datetime(self.expires_at, "expires_at", "HumanEscalation", optional=True)
        if not isinstance(self.options, tuple) or any(
            not isinstance(option, HumanEscalationOption) for option in self.options
        ):
            raise _invalid(
                "HumanEscalation.options must be a tuple of HumanEscalationOption",
                "Build options from HumanEscalationOption values.",
            )
        if self.next_action is not None:
            _require_instance(self.next_action, ActionHint, "next_action", "HumanEscalation")
        if self.acknowledgement is not None:
            _require_instance(
                self.acknowledgement,
                EscalationAcknowledgement,
                "acknowledgement",
                "HumanEscalation",
            )
        if self.resolution is not None:
            _require_instance(
                self.resolution, EscalationResolution, "resolution", "HumanEscalation"
            )

        # Upstream superRefine invariants (all instants compared parsed):
        option_ids = [option.id for option in self.options]
        if len(option_ids) != len(set(option_ids)):
            raise _invalid(
                "Human escalation option ids must be unique.",
                "Give each option a distinct id.",
            )
        if self.state == "resolved" and self.resolution is None:
            raise _invalid(
                "A resolved human escalation requires resolution attribution.",
                "Attach a resolution with an attributed actor.",
            )
        if self.resolution is not None and self.state != "resolved":
            raise _invalid(
                "Human escalation resolution is only valid for the resolved state.",
                "Remove the resolution or set state to resolved.",
            )
        if self.state == "acknowledged" and self.acknowledgement is None:
            raise _invalid(
                "An acknowledged human escalation requires actor attribution.",
                "Attach an acknowledgement with an attributed actor.",
            )
        if self.acknowledgement is not None and self.state == "open":
            raise _invalid(
                "An open human escalation cannot already be acknowledged.",
                "Set state to acknowledged or drop the acknowledgement.",
            )
        opened = _instant(self.opened_at)
        if self.expires_at and _instant(self.expires_at) <= opened:
            raise _invalid(
                "Human escalation expiresAt must be later than openedAt.",
                "Use an expiry strictly after the open time.",
            )
        if self.state == "expired" and not self.expires_at:
            raise _invalid(
                "An expired human escalation requires expiresAt.",
                "Record the deadline that expired.",
            )
        if self.state == "superseded" and not self.superseded_by_id:
            raise _invalid(
                "A superseded human escalation requires supersededById.",
                "Record the successor escalation id.",
            )
        if self.resolution is not None:
            if self.resolution.option_id and self.resolution.option_id not in option_ids:
                raise _invalid(
                    "Human escalation resolution optionId must identify one of the offered options.",
                    "Resolve with one of the offered option ids.",
                )
            if _instant(self.resolution.resolved_at) < opened:
                raise _invalid(
                    "Human escalation resolvedAt cannot precede openedAt.",
                    "Use a resolution time at or after the open time.",
                )
        if self.acknowledgement is not None and _instant(
            self.acknowledgement.acknowledged_at
        ) < opened:
            raise _invalid(
                "Human escalation acknowledgedAt cannot precede openedAt.",
                "Use an acknowledgement time at or after the open time.",
            )
        if (
            self.acknowledgement is not None
            and self.resolution is not None
            and _instant(self.resolution.resolved_at)
            < _instant(self.acknowledgement.acknowledged_at)
        ):
            raise _invalid(
                "Human escalation resolvedAt cannot precede acknowledgedAt.",
                "Resolve at or after the acknowledgement time.",
            )
        if self.superseded_by_id and self.state != "superseded":
            raise _invalid(
                "Human escalation supersededById is only valid for the superseded state.",
                "Remove supersededById or set state to superseded.",
            )
        if self.terminal_reason and self.state not in ("expired", "superseded"):
            raise _invalid(
                "Human escalation terminalReason is only valid for expired or superseded states.",
                "Remove terminalReason or use a terminal state.",
            )
        # OStag strengthening (declared): the deadline rule must hold at the
        # model layer too, or open() with a pre-attributed escalation could
        # bypass transition()'s coercion. Attribution at or after the
        # deadline is never valid.
        if self.expires_at:
            deadline = _instant(self.expires_at)
            if (
                self.acknowledgement is not None
                and _instant(self.acknowledgement.acknowledged_at) >= deadline
            ):
                raise _invalid(
                    "Human escalation cannot be acknowledged at or after expiresAt.",
                    "A late acknowledgement must be recorded as expired instead.",
                )
            if self.resolution is not None and _instant(self.resolution.resolved_at) >= deadline:
                raise _invalid(
                    "Human escalation cannot be resolved at or after expiresAt.",
                    "A late resolution must be recorded as expired instead.",
                )

    @property
    def terminal(self) -> bool:
        return self.state in ("resolved", "expired", "superseded")

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "workThreadId": self.work_thread_key,
            "class": self.escalation_class,
            "audience": self.audience,
            "subjectRef": self.subject_ref,
            "state": self.state,
            "blocking": self.blocking,
            "summary": self.summary,
            "reason": self.reason,
            "openedAt": self.opened_at,
        }
        if self.run_id:
            payload["runId"] = self.run_id
        if self.attempt_id:
            payload["attemptId"] = self.attempt_id
        if self.options:
            payload["options"] = [option.to_dict() for option in self.options]
        if self.next_action is not None:
            payload["nextAction"] = self.next_action.to_dict()
        if self.dedupe_key:
            payload["dedupeKey"] = self.dedupe_key
        if self.expires_at:
            payload["expiresAt"] = self.expires_at
        if self.acknowledgement is not None:
            payload["acknowledgement"] = self.acknowledgement.to_dict()
        if self.resolution is not None:
            payload["resolution"] = self.resolution.to_dict()
        if self.terminal_reason:
            payload["terminalReason"] = self.terminal_reason
        if self.superseded_by_id:
            payload["supersededById"] = self.superseded_by_id
        return payload

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "HumanEscalation":
        _validate_keys(
            data,
            frozenset(
                {
                    "id",
                    "workThreadId",
                    "runId",
                    "attemptId",
                    "class",
                    "audience",
                    "subjectRef",
                    "state",
                    "blocking",
                    "summary",
                    "reason",
                    "options",
                    "nextAction",
                    "dedupeKey",
                    "openedAt",
                    "expiresAt",
                    "acknowledgement",
                    "resolution",
                    "terminalReason",
                    "supersededById",
                }
            ),
            "HumanEscalation",
        )
        _require_wire_keys(
            data,
            frozenset(
                {
                    "id",
                    "workThreadId",
                    "class",
                    "audience",
                    "subjectRef",
                    "state",
                    "blocking",
                    "summary",
                    "reason",
                    "openedAt",
                }
            ),
            "HumanEscalation",
        )
        options = data.get("options", ())
        if "options" in data and not data["options"]:
            raise _invalid(
                "Human escalation options, when present, must offer at least one option.",
                "Omit the options field or provide at least one option (upstream .min(1)).",
            )
        acknowledgement = data.get("acknowledgement")
        resolution = data.get("resolution")
        next_action = data.get("nextAction")
        return cls(
            id=data.get("id", ""),
            work_thread_key=data.get("workThreadId", ""),
            escalation_class=data.get("class", ""),
            audience=data.get("audience", ""),
            subject_ref=data.get("subjectRef", ""),
            state=data.get("state", ""),
            blocking=data.get("blocking", True),
            summary=data.get("summary", ""),
            reason=data.get("reason", ""),
            opened_at=data.get("openedAt", ""),
            run_id=data.get("runId", ""),
            attempt_id=data.get("attemptId", ""),
            options=tuple(
                HumanEscalationOption.from_dict(option)
                for option in _require_object_list(options, "options", "HumanEscalation")
            ),
            next_action=ActionHint.from_dict(next_action) if next_action is not None else None,
            dedupe_key=data.get("dedupeKey", ""),
            expires_at=data.get("expiresAt", ""),
            acknowledgement=(
                EscalationAcknowledgement.from_dict(acknowledgement)
                if acknowledgement is not None
                else None
            ),
            resolution=(
                EscalationResolution.from_dict(resolution) if resolution is not None else None
            ),
            terminal_reason=data.get("terminalReason", ""),
            superseded_by_id=data.get("supersededById", ""),
        )

    def to_escalation_input(self) -> EscalationInput:
        """Feed the Phase 1 evaluator. Expired/superseded escalations map to
        inactive states, so an expired escalation can never authorize or
        block anything through the completion pipeline."""
        return EscalationInput(
            id=self.id,
            blocking=self.blocking,
            state=self.state,
            reason=self.reason,
            opened_at=self.opened_at,
            resolved_at=self.resolution.resolved_at if self.resolution is not None else "",
        )
