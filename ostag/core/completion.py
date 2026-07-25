"""Completion-governance contracts ported from OpenTag v0.7.0.

Pinned upstream source: `amplifthq/opentag` @ ``758fb5a6`` (``v0.7.0^{}``),
``packages/core/src/schema.ts`` and ``packages/governance/src/types.ts``.

The wire contract (``to_dict``/``from_dict``) preserves the upstream canonical
camelCase field names, enum values, and invariants; Python attributes use
repository-standard ``snake_case``. Deliberate divergences from upstream are
listed in ``docs/upgrade/v0.3-p1-completion-mapping.md`` and covered by
compatibility tests — the headline ones: ``assessedBy`` emits ``"ostag"``
instead of ``"opentag"``; run results are simplified to
``{runId, conclusion, recordedAt}``; material-action receipts carry
``actionFamily`` as a first-class field; the waiver/assessment actor is a
compact ``{actorId, handle}`` reference.

Everything here is a frozen dataclass with hand validation (repo idiom — no
pydantic) raising :class:`ostag.core.schemas.SchemaError` with recovery hints.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ostag.core.schemas import SchemaError

COMPLETION_STATES = ("pending", "satisfied", "unsatisfied", "blocked", "waived")
COMPLETION_GATE_KINDS = (
    "artifact",
    "verification",
    "external_state",
    "material_action",
    "human_acceptance",
)
GATE_RESULT_STATES = ("passed", "failed", "missing", "unknown", "waived")
EVIDENCE_ASSURANCES = ("verified", "reported", "unverifiable")
MINIMUM_ASSURANCES = ("verified", "reported")
CONTRACT_MODES = ("execution_compat", "governed")
ON_SATISFIED = ("report_only", "propose_work_item_transition", "apply_transition_by_policy")
RUN_CONCLUSIONS = (
    "success",
    "failure",
    "cancelled",
    "timed_out",
    "interrupted",
    "needs_human",
)
MATERIAL_ACTION_OUTCOMES = ("succeeded", "failed", "unknown")
ASSESSED_BY = ("ostag", "human")  # native divergence: upstream uses "opentag"

POLICY_SCOPES = (
    "organization_default",
    "adapter_surface_default",
    "work_context_owner_container",
    "work_item_override",
    "primary_anchor_override",
)
ARTIFACT_KINDS = (
    "root_cause_note",
    "suggested_changes_snapshot",
    "verification_summary",
    "patch",
    "report",
    "screenshot",
    "log_summary",
    "pull_request",
    "risk_note",
    "follow_up_task",
    "audit_trail",
    "decision_record",
)

COMPLETION_REASON_CODES = (
    "artifact_requirement_satisfied",
    "artifact_missing",
    "artifact_ambiguous",
    "verification_passed",
    "verification_failed",
    "verification_missing",
    "verification_assurance_insufficient",
    "verification_subject_mismatch",
    "verification_stale",
    "external_state_satisfied",
    "external_state_mismatch",
    "external_state_missing",
    "external_state_assurance_insufficient",
    "external_state_subject_mismatch",
    "external_state_stale",
    "material_action_succeeded",
    "material_action_failed",
    "material_action_unknown",
    "material_action_missing",
    "human_acceptance_recorded",
    "human_acceptance_missing",
    "gate_waived",
    "waiver_invalid",
    "execution_succeeded",
    "execution_incomplete",
    "execution_not_succeeded",
)

_ESCALATION_ACTIVE_STATES = ("open", "acknowledged")


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


def _require_positive(value: int, name: str, schema: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise SchemaError(
            code="invalid_number",
            message=f"{schema}.{name}={value!r} must be a positive integer",
            recovery_hint=f"Provide an integer >= 1 for '{name}'.",
        )


def _require_non_negative(value: int, name: str, schema: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise SchemaError(
            code="invalid_number",
            message=f"{schema}.{name}={value!r} must be a non-negative integer",
            recovery_hint=f"Provide an integer >= 0 for '{name}'.",
        )


def _prune(value: dict[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if item not in (None, "", (), [])}


def _require_datetime(value: str, name: str, schema: str, *, optional: bool = False) -> None:
    """ISO-8601 timestamp with an explicit timezone (native-lenient offset form)."""
    if optional and value == "":
        return
    _require(value, name, schema)
    from datetime import datetime

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        parsed = None
    if (
        parsed is None
        or parsed.tzinfo is None
        or parsed.utcoffset() is None
        or parsed.utcoffset().total_seconds() != 0
    ):
        # UTC-only: the protocol compares timestamps lexically (expiry windows,
        # authoritative-evidence ordering), which is only correct in one zone.
        raise SchemaError(
            code="invalid_datetime",
            message=f"{schema}.{name}={value!r} is not a UTC ISO-8601 timestamp",
            recovery_hint="Use UTC, e.g. 2026-07-26T10:00:00+00:00 (or ...Z).",
        )


def _validate_keys(data: dict[str, Any], allowed: frozenset[str], schema: str) -> None:
    """Reject unknown wire fields (upstream schemas are .strict())."""
    unknown = set(data) - allowed
    if unknown:
        raise SchemaError(
            code="unknown_field",
            message=f"{schema} does not accept field(s): {', '.join(sorted(unknown))}",
            recovery_hint=f"Allowed fields: {', '.join(sorted(allowed))}.",
        )


@dataclass(frozen=True)
class ActorRef:
    """Compact actor attribution (native simplification of upstream ActorIdentity)."""

    actor_id: str
    handle: str = ""

    def __post_init__(self) -> None:
        _require(self.actor_id, "actor_id", "ActorRef")

    def to_dict(self) -> dict[str, Any]:
        return _prune({"actorId": self.actor_id, "handle": self.handle})

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ActorRef":
        _validate_keys(data, frozenset({"actorId", "handle"}), "ActorRef")
        return cls(actor_id=data.get("actorId", ""), handle=data.get("handle", ""))

    @property
    def display(self) -> str:
        return self.handle or self.actor_id


@dataclass(frozen=True)
class CompletionTargetSelector:
    key: str
    kind: str = "change_request"
    lineage: str = "current_cycle"
    cardinality: str = "exactly_one"

    def __post_init__(self) -> None:
        _require(self.key, "key", "CompletionTargetSelector")
        _require_choice(self.kind, ("change_request",), "kind", "CompletionTargetSelector")
        _require_choice(self.lineage, ("current_cycle",), "lineage", "CompletionTargetSelector")
        _require_choice(
            self.cardinality, ("exactly_one",), "cardinality", "CompletionTargetSelector"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "kind": self.kind,
            "lineage": self.lineage,
            "cardinality": self.cardinality,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CompletionTargetSelector":
        _validate_keys(
            data, frozenset({"key", "kind", "lineage", "cardinality"}), "CompletionTargetSelector"
        )
        return cls(
            key=data.get("key", ""),
            kind=data.get("kind", "change_request"),
            lineage=data.get("lineage", "current_cycle"),
            cardinality=data.get("cardinality", "exactly_one"),
        )


@dataclass(frozen=True)
class ResolvedCompletionTarget:
    key: str
    provider: str
    resource_ref: str
    resource_version: str
    artifact_id: str

    def __post_init__(self) -> None:
        for name in ("key", "provider", "resource_ref", "resource_version", "artifact_id"):
            _require(getattr(self, name), name, "ResolvedCompletionTarget")

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "provider": self.provider,
            "resourceRef": self.resource_ref,
            "resourceVersion": self.resource_version,
            "artifactId": self.artifact_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ResolvedCompletionTarget":
        _validate_keys(
            data,
            frozenset({"key", "provider", "resourceRef", "resourceVersion", "artifactId"}),
            "ResolvedCompletionTarget",
        )
        return cls(
            key=data.get("key", ""),
            provider=data.get("provider", ""),
            resource_ref=data.get("resourceRef", ""),
            resource_version=data.get("resourceVersion", ""),
            artifact_id=data.get("artifactId", ""),
        )


@dataclass(frozen=True)
class ArtifactTarget:
    key: str
    provider: str
    resource_ref: str
    resource_version: str

    def __post_init__(self) -> None:
        for name in ("key", "provider", "resource_ref", "resource_version"):
            _require(getattr(self, name), name, "ArtifactTarget")

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "provider": self.provider,
            "resourceRef": self.resource_ref,
            "resourceVersion": self.resource_version,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ArtifactTarget":
        _validate_keys(
            data,
            frozenset({"key", "provider", "resourceRef", "resourceVersion"}),
            "ArtifactTarget",
        )
        return cls(
            key=data.get("key", ""),
            provider=data.get("provider", ""),
            resource_ref=data.get("resourceRef", ""),
            resource_version=data.get("resourceVersion", ""),
        )


@dataclass(frozen=True)
class CompletionArtifact:
    id: str
    kind: str
    recorded_at: str
    source_run_id: str = ""
    uri: str = ""
    target: ArtifactTarget | None = None

    def __post_init__(self) -> None:
        _require(self.id, "id", "CompletionArtifact")
        _require_choice(self.kind, ARTIFACT_KINDS, "kind", "CompletionArtifact")
        _require_datetime(self.recorded_at, "recorded_at", "CompletionArtifact")

    def to_dict(self) -> dict[str, Any]:
        payload = _prune(
            {
                "id": self.id,
                "kind": self.kind,
                "sourceRunId": self.source_run_id,
                "uri": self.uri,
                "recordedAt": self.recorded_at,
            }
        )
        if self.target is not None:
            payload["target"] = self.target.to_dict()
        return payload

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CompletionArtifact":
        _validate_keys(
            data,
            frozenset({"id", "kind", "sourceRunId", "uri", "target", "recordedAt"}),
            "CompletionArtifact",
        )
        target = data.get("target")
        return cls(
            id=data.get("id", ""),
            kind=data.get("kind", ""),
            recorded_at=data.get("recordedAt", ""),
            source_run_id=data.get("sourceRunId", ""),
            uri=data.get("uri", ""),
            target=ArtifactTarget.from_dict(target) if isinstance(target, dict) else None,
        )


# --- Gates -------------------------------------------------------------------


@dataclass(frozen=True)
class ArtifactGate:
    id: str
    target_key: str
    artifact_kind: str
    minimum: int
    kind: str = "artifact"

    def __post_init__(self) -> None:
        _require(self.id, "id", "ArtifactGate")
        _require(self.target_key, "target_key", "ArtifactGate")
        _require_choice(self.artifact_kind, ARTIFACT_KINDS, "artifact_kind", "ArtifactGate")
        _require_positive(self.minimum, "minimum", "ArtifactGate")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": "artifact",
            "targetKey": self.target_key,
            "artifactKind": self.artifact_kind,
            "minimum": self.minimum,
        }


@dataclass(frozen=True)
class VerificationGate:
    id: str
    target_key: str
    evidence_kind: str
    minimum_assurance: str
    required_observations: tuple[str, ...] = ()
    required_outcome: str = "passed"
    kind: str = "verification"

    def __post_init__(self) -> None:
        _require(self.id, "id", "VerificationGate")
        _require(self.target_key, "target_key", "VerificationGate")
        _require(self.evidence_kind, "evidence_kind", "VerificationGate")
        _require_choice(
            self.minimum_assurance, MINIMUM_ASSURANCES, "minimum_assurance", "VerificationGate"
        )
        _require_choice(self.required_outcome, ("passed",), "required_outcome", "VerificationGate")

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "kind": "verification",
            "targetKey": self.target_key,
            "evidenceKind": self.evidence_kind,
            "requiredOutcome": self.required_outcome,
            "minimumAssurance": self.minimum_assurance,
        }
        if self.required_observations:
            payload["requiredObservations"] = list(self.required_observations)
        return payload


@dataclass(frozen=True)
class ExternalStateGate:
    id: str
    target_key: str
    provider: str
    required_state: str
    minimum_assurance: str
    kind: str = "external_state"

    def __post_init__(self) -> None:
        _require(self.id, "id", "ExternalStateGate")
        _require(self.target_key, "target_key", "ExternalStateGate")
        _require(self.provider, "provider", "ExternalStateGate")
        _require(self.required_state, "required_state", "ExternalStateGate")
        _require_choice(
            self.minimum_assurance, MINIMUM_ASSURANCES, "minimum_assurance", "ExternalStateGate"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": "external_state",
            "targetKey": self.target_key,
            "provider": self.provider,
            "requiredState": self.required_state,
            "minimumAssurance": self.minimum_assurance,
        }


@dataclass(frozen=True)
class MaterialActionGate:
    id: str
    action_family: str
    target_key: str = ""
    required_outcome: str = "succeeded"
    kind: str = "material_action"

    def __post_init__(self) -> None:
        _require(self.id, "id", "MaterialActionGate")
        _require(self.action_family, "action_family", "MaterialActionGate")
        _require_choice(
            self.required_outcome, ("succeeded",), "required_outcome", "MaterialActionGate"
        )

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "kind": "material_action",
            "actionFamily": self.action_family,
            "requiredOutcome": self.required_outcome,
        }
        if self.target_key:
            payload["targetKey"] = self.target_key
        return payload


@dataclass(frozen=True)
class HumanAcceptanceGate:
    id: str
    required_role: str
    target_key: str = ""
    kind: str = "human_acceptance"

    def __post_init__(self) -> None:
        _require(self.id, "id", "HumanAcceptanceGate")
        _require(self.required_role, "required_role", "HumanAcceptanceGate")

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "kind": "human_acceptance",
            "requiredRole": self.required_role,
        }
        if self.target_key:
            payload["targetKey"] = self.target_key
        return payload


CompletionGate = (
    ArtifactGate | VerificationGate | ExternalStateGate | MaterialActionGate | HumanAcceptanceGate
)


_GATE_ALLOWED_KEYS = {
    "artifact": frozenset({"id", "kind", "targetKey", "artifactKind", "minimum"}),
    "verification": frozenset(
        {
            "id",
            "kind",
            "targetKey",
            "evidenceKind",
            "requiredObservations",
            "requiredOutcome",
            "minimumAssurance",
        }
    ),
    "external_state": frozenset(
        {"id", "kind", "targetKey", "provider", "requiredState", "minimumAssurance"}
    ),
    "material_action": frozenset(
        {"id", "kind", "targetKey", "actionFamily", "requiredOutcome"}
    ),
    "human_acceptance": frozenset({"id", "kind", "targetKey", "requiredRole"}),
}


def gate_from_dict(data: dict[str, Any]) -> CompletionGate:
    kind = data.get("kind", "")
    _require_choice(kind, COMPLETION_GATE_KINDS, "kind", "CompletionGate")
    _validate_keys(data, _GATE_ALLOWED_KEYS[kind], f"CompletionGate[{kind}]")
    if kind == "artifact":
        return ArtifactGate(
            id=data.get("id", ""),
            target_key=data.get("targetKey", ""),
            artifact_kind=data.get("artifactKind", ""),
            minimum=data.get("minimum", 0),
        )
    if kind == "verification":
        return VerificationGate(
            id=data.get("id", ""),
            target_key=data.get("targetKey", ""),
            evidence_kind=data.get("evidenceKind", ""),
            minimum_assurance=data.get("minimumAssurance", ""),
            required_observations=tuple(data.get("requiredObservations", ())),
            required_outcome=data.get("requiredOutcome", "passed"),
        )
    if kind == "external_state":
        return ExternalStateGate(
            id=data.get("id", ""),
            target_key=data.get("targetKey", ""),
            provider=data.get("provider", ""),
            required_state=data.get("requiredState", ""),
            minimum_assurance=data.get("minimumAssurance", ""),
        )
    if kind == "material_action":
        return MaterialActionGate(
            id=data.get("id", ""),
            action_family=data.get("actionFamily", ""),
            target_key=data.get("targetKey", ""),
            required_outcome=data.get("requiredOutcome", "succeeded"),
        )
    return HumanAcceptanceGate(
        id=data.get("id", ""),
        required_role=data.get("requiredRole", ""),
        target_key=data.get("targetKey", ""),
    )


@dataclass(frozen=True)
class PolicyRef:
    scope: str
    ref: str
    version: str = ""

    def __post_init__(self) -> None:
        _require_choice(self.scope, POLICY_SCOPES, "scope", "PolicyRef")
        _require(self.ref, "ref", "PolicyRef")

    def to_dict(self) -> dict[str, Any]:
        return _prune({"scope": self.scope, "ref": self.ref, "version": self.version})

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PolicyRef":
        _validate_keys(data, frozenset({"scope", "ref", "version"}), "PolicyRef")
        return cls(
            scope=data.get("scope", ""),
            ref=data.get("ref", ""),
            version=data.get("version", ""),
        )


@dataclass(frozen=True)
class CompletionContract:
    id: str
    version: int
    work_thread_id: str
    cycle: int
    mode: str
    gates: tuple[CompletionGate, ...]
    max_automatic_retries: int
    on_satisfied: str
    created_at: str
    target_selectors: tuple[CompletionTargetSelector, ...] = ()
    resolved_from: tuple[PolicyRef, ...] = ()

    def __post_init__(self) -> None:
        _require(self.id, "id", "CompletionContract")
        _require_positive(self.version, "version", "CompletionContract")
        _require(self.work_thread_id, "work_thread_id", "CompletionContract")
        _require_positive(self.cycle, "cycle", "CompletionContract")
        _require_choice(self.mode, CONTRACT_MODES, "mode", "CompletionContract")
        _require_non_negative(
            self.max_automatic_retries, "max_automatic_retries", "CompletionContract"
        )
        _require_choice(self.on_satisfied, ON_SATISFIED, "on_satisfied", "CompletionContract")
        _require_datetime(self.created_at, "created_at", "CompletionContract")
        if not self.gates:
            raise SchemaError(
                code="missing_field",
                message="CompletionContract.gates must contain at least one gate",
                recovery_hint="Declare at least one completion gate.",
            )
        selector_keys: set[str] = set()
        for selector in self.target_selectors:
            if selector.key in selector_keys:
                raise SchemaError(
                    code="duplicate_key",
                    message=f"Completion target selector key must be unique: {selector.key}",
                    recovery_hint="Give each target selector a distinct key.",
                )
            selector_keys.add(selector.key)
        gate_ids: set[str] = set()
        for gate in self.gates:
            if gate.id in gate_ids:
                raise SchemaError(
                    code="duplicate_key",
                    message=f"Completion gate id must be unique: {gate.id}",
                    recovery_hint="Give each gate a distinct id.",
                )
            gate_ids.add(gate.id)
            target_key = getattr(gate, "target_key", "")
            if target_key and target_key not in selector_keys:
                raise SchemaError(
                    code="invalid_reference",
                    message=(
                        f"Completion gate targetKey must reference a target selector: {target_key}"
                    ),
                    recovery_hint="Declare the selector or fix the gate's targetKey.",
                )
        if self.mode == "execution_compat" and self.target_selectors:
            raise SchemaError(
                code="invalid_contract",
                message="execution_compat contracts must not declare delivery targets",
                recovery_hint="Remove targetSelectors or switch mode to 'governed'.",
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "version": self.version,
            "workThreadId": self.work_thread_id,
            "cycle": self.cycle,
            "mode": self.mode,
            "targetSelectors": [selector.to_dict() for selector in self.target_selectors],
            "resolvedFrom": [ref.to_dict() for ref in self.resolved_from],
            "gates": [gate.to_dict() for gate in self.gates],
            "maxAutomaticRetries": self.max_automatic_retries,
            "onSatisfied": self.on_satisfied,
            "createdAt": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CompletionContract":
        _validate_keys(
            data,
            frozenset(
                {
                    "id",
                    "version",
                    "workThreadId",
                    "cycle",
                    "mode",
                    "targetSelectors",
                    "resolvedFrom",
                    "gates",
                    "maxAutomaticRetries",
                    "onSatisfied",
                    "createdAt",
                }
            ),
            "CompletionContract",
        )
        return cls(
            id=data.get("id", ""),
            version=data.get("version", 0),
            work_thread_id=data.get("workThreadId", ""),
            cycle=data.get("cycle", 0),
            mode=data.get("mode", ""),
            gates=tuple(gate_from_dict(gate) for gate in data.get("gates", ())),
            max_automatic_retries=data.get("maxAutomaticRetries", -1),
            on_satisfied=data.get("onSatisfied", ""),
            created_at=data.get("createdAt", ""),
            target_selectors=tuple(
                CompletionTargetSelector.from_dict(selector)
                for selector in data.get("targetSelectors", ())
            ),
            resolved_from=tuple(
                PolicyRef.from_dict(ref) for ref in data.get("resolvedFrom", ())
            ),
        )


# --- Evidence ----------------------------------------------------------------


@dataclass(frozen=True)
class EvidenceSubject:
    provider: str
    resource_ref: str
    resource_version: str

    def __post_init__(self) -> None:
        for name in ("provider", "resource_ref", "resource_version"):
            _require(getattr(self, name), name, "EvidenceSubject")

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "resourceRef": self.resource_ref,
            "resourceVersion": self.resource_version,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EvidenceSubject":
        _validate_keys(
            data, frozenset({"provider", "resourceRef", "resourceVersion"}), "EvidenceSubject"
        )
        return cls(
            provider=data.get("provider", ""),
            resource_ref=data.get("resourceRef", ""),
            resource_version=data.get("resourceVersion", ""),
        )


@dataclass(frozen=True)
class EvidenceClaim:
    predicate: str
    outcome: str
    observations: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require(self.predicate, "predicate", "EvidenceClaim")
        _require(self.outcome, "outcome", "EvidenceClaim")

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"predicate": self.predicate, "outcome": self.outcome}
        if self.observations:
            payload["observations"] = dict(self.observations)
        return payload

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EvidenceClaim":
        _validate_keys(
            data, frozenset({"predicate", "outcome", "observations"}), "EvidenceClaim"
        )
        return cls(
            predicate=data.get("predicate", ""),
            outcome=data.get("outcome", ""),
            observations=dict(data.get("observations", {})),
        )


@dataclass(frozen=True)
class EvidenceProvenance:
    adapter: str
    adapter_version: str
    payload_digest: str
    source_event_id: str = ""
    provider_delivery_id: str = ""

    def __post_init__(self) -> None:
        for name in ("adapter", "adapter_version", "payload_digest"):
            _require(getattr(self, name), name, "EvidenceProvenance")

    def to_dict(self) -> dict[str, Any]:
        return _prune(
            {
                "adapter": self.adapter,
                "adapterVersion": self.adapter_version,
                "payloadDigest": self.payload_digest,
                "sourceEventId": self.source_event_id,
                "providerDeliveryId": self.provider_delivery_id,
            }
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EvidenceProvenance":
        _validate_keys(
            data,
            frozenset(
                {
                    "adapter",
                    "adapterVersion",
                    "payloadDigest",
                    "sourceEventId",
                    "providerDeliveryId",
                }
            ),
            "EvidenceProvenance",
        )
        return cls(
            adapter=data.get("adapter", ""),
            adapter_version=data.get("adapterVersion", ""),
            payload_digest=data.get("payloadDigest", ""),
            source_event_id=data.get("sourceEventId", ""),
            provider_delivery_id=data.get("providerDeliveryId", ""),
        )


@dataclass(frozen=True)
class CompletionEvidenceFact:
    id: str
    work_thread_id: str
    cycle: int
    kind: str
    assurance: str
    subject: EvidenceSubject
    claim: EvidenceClaim
    provenance: EvidenceProvenance
    observed_at: str
    received_at: str

    def __post_init__(self) -> None:
        _require(self.id, "id", "CompletionEvidenceFact")
        _require(self.work_thread_id, "work_thread_id", "CompletionEvidenceFact")
        _require_positive(self.cycle, "cycle", "CompletionEvidenceFact")
        _require(self.kind, "kind", "CompletionEvidenceFact")
        _require_choice(
            self.assurance, EVIDENCE_ASSURANCES, "assurance", "CompletionEvidenceFact"
        )
        _require_datetime(self.observed_at, "observed_at", "CompletionEvidenceFact")
        _require_datetime(self.received_at, "received_at", "CompletionEvidenceFact")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "workThreadId": self.work_thread_id,
            "cycle": self.cycle,
            "kind": self.kind,
            "assurance": self.assurance,
            "subject": self.subject.to_dict(),
            "claim": self.claim.to_dict(),
            "provenance": self.provenance.to_dict(),
            "observedAt": self.observed_at,
            "receivedAt": self.received_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CompletionEvidenceFact":
        _validate_keys(
            data,
            frozenset(
                {
                    "id",
                    "workThreadId",
                    "cycle",
                    "kind",
                    "assurance",
                    "subject",
                    "claim",
                    "provenance",
                    "observedAt",
                    "receivedAt",
                }
            ),
            "CompletionEvidenceFact",
        )
        return cls(
            id=data.get("id", ""),
            work_thread_id=data.get("workThreadId", ""),
            cycle=data.get("cycle", 0),
            kind=data.get("kind", ""),
            assurance=data.get("assurance", ""),
            subject=EvidenceSubject.from_dict(data.get("subject", {})),
            claim=EvidenceClaim.from_dict(data.get("claim", {})),
            provenance=EvidenceProvenance.from_dict(data.get("provenance", {})),
            observed_at=data.get("observedAt", ""),
            received_at=data.get("receivedAt", ""),
        )


@dataclass(frozen=True)
class CompletionRunResult:
    """Native simplification of the upstream run-result wrapper (mapping note)."""

    run_id: str
    conclusion: str
    recorded_at: str

    def __post_init__(self) -> None:
        _require(self.run_id, "run_id", "CompletionRunResult")
        _require_choice(self.conclusion, RUN_CONCLUSIONS, "conclusion", "CompletionRunResult")
        _require_datetime(self.recorded_at, "recorded_at", "CompletionRunResult")

    def to_dict(self) -> dict[str, Any]:
        return {
            "runId": self.run_id,
            "conclusion": self.conclusion,
            "recordedAt": self.recorded_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CompletionRunResult":
        _validate_keys(
            data, frozenset({"runId", "conclusion", "recordedAt"}), "CompletionRunResult"
        )
        return cls(
            run_id=data.get("runId", ""),
            conclusion=data.get("conclusion", ""),
            recorded_at=data.get("recordedAt", ""),
        )


@dataclass(frozen=True)
class MaterialActionReceiptFact:
    """Material-action receipt input; actionFamily is first-class (mapping note)."""

    id: str
    action_family: str
    outcome: str
    observed_at: str

    def __post_init__(self) -> None:
        _require(self.id, "id", "MaterialActionReceiptFact")
        _require(self.action_family, "action_family", "MaterialActionReceiptFact")
        _require_choice(
            self.outcome, MATERIAL_ACTION_OUTCOMES, "outcome", "MaterialActionReceiptFact"
        )
        _require_datetime(self.observed_at, "observed_at", "MaterialActionReceiptFact")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "actionFamily": self.action_family,
            "outcome": self.outcome,
            "observedAt": self.observed_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MaterialActionReceiptFact":
        _validate_keys(
            data,
            frozenset({"id", "actionFamily", "outcome", "observedAt"}),
            "MaterialActionReceiptFact",
        )
        return cls(
            id=data.get("id", ""),
            action_family=data.get("actionFamily", ""),
            outcome=data.get("outcome", ""),
            observed_at=data.get("observedAt", ""),
        )


@dataclass(frozen=True)
class CompletionGateResult:
    gate_id: str
    state: str
    evidence_ids: tuple[str, ...]
    reason_code: str
    reason: str
    evaluated_at: str
    target_key: str = ""

    def __post_init__(self) -> None:
        _require(self.gate_id, "gate_id", "CompletionGateResult")
        _require_choice(self.state, GATE_RESULT_STATES, "state", "CompletionGateResult")
        _require_choice(
            self.reason_code, COMPLETION_REASON_CODES, "reason_code", "CompletionGateResult"
        )
        _require(self.reason, "reason", "CompletionGateResult")
        _require_datetime(self.evaluated_at, "evaluated_at", "CompletionGateResult")

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "gateId": self.gate_id,
            "state": self.state,
            "evidenceIds": list(self.evidence_ids),
            "reasonCode": self.reason_code,
            "reason": self.reason,
            "evaluatedAt": self.evaluated_at,
        }
        if self.target_key:
            payload["targetKey"] = self.target_key
        return payload

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CompletionGateResult":
        _validate_keys(
            data,
            frozenset(
                {
                    "gateId",
                    "targetKey",
                    "state",
                    "evidenceIds",
                    "reasonCode",
                    "reason",
                    "evaluatedAt",
                }
            ),
            "CompletionGateResult",
        )
        return cls(
            gate_id=data.get("gateId", ""),
            state=data.get("state", ""),
            evidence_ids=tuple(data.get("evidenceIds", ())),
            reason_code=data.get("reasonCode", ""),
            reason=data.get("reason", ""),
            evaluated_at=data.get("evaluatedAt", ""),
            target_key=data.get("targetKey", ""),
        )


@dataclass(frozen=True)
class CompletionWaiver:
    id: str
    contract_id: str
    contract_version: int
    cycle: int
    actor: ActorRef
    reason: str
    policy_scope: str
    gate_ids: tuple[str, ...]
    waived_at: str
    run_id: str = ""
    scope: str = "selected_gates"
    expires_at: str = ""

    def __post_init__(self) -> None:
        _require(self.id, "id", "CompletionWaiver")
        _require(self.contract_id, "contract_id", "CompletionWaiver")
        _require_positive(self.contract_version, "contract_version", "CompletionWaiver")
        _require_positive(self.cycle, "cycle", "CompletionWaiver")
        _require(self.reason, "reason", "CompletionWaiver")
        _require_choice(self.policy_scope, POLICY_SCOPES, "policy_scope", "CompletionWaiver")
        _require_choice(self.scope, ("selected_gates",), "scope", "CompletionWaiver")
        _require_datetime(self.waived_at, "waived_at", "CompletionWaiver")
        _require_datetime(self.expires_at, "expires_at", "CompletionWaiver", optional=True)
        if not self.gate_ids:
            raise SchemaError(
                code="missing_field",
                message="CompletionWaiver.gate_ids must contain at least one gate id",
                recovery_hint="List the gate ids this waiver covers.",
            )
        if len(set(self.gate_ids)) != len(self.gate_ids):
            raise SchemaError(
                code="duplicate_key",
                message="Waived gate id must be unique",
                recovery_hint="Remove duplicate gate ids from the waiver.",
            )

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "contractId": self.contract_id,
            "contractVersion": self.contract_version,
            "cycle": self.cycle,
            "actor": self.actor.to_dict(),
            "reason": self.reason,
            "scope": self.scope,
            "policyScope": self.policy_scope,
            "gateIds": list(self.gate_ids),
            "waivedAt": self.waived_at,
        }
        if self.run_id:
            payload["runId"] = self.run_id
        if self.expires_at:
            payload["expiresAt"] = self.expires_at
        return payload

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CompletionWaiver":
        _validate_keys(
            data,
            frozenset(
                {
                    "id",
                    "runId",
                    "contractId",
                    "contractVersion",
                    "cycle",
                    "actor",
                    "reason",
                    "scope",
                    "policyScope",
                    "gateIds",
                    "waivedAt",
                    "expiresAt",
                }
            ),
            "CompletionWaiver",
        )
        return cls(
            id=data.get("id", ""),
            contract_id=data.get("contractId", ""),
            contract_version=data.get("contractVersion", 0),
            cycle=data.get("cycle", 0),
            actor=ActorRef.from_dict(data.get("actor", {})),
            reason=data.get("reason", ""),
            policy_scope=data.get("policyScope", ""),
            gate_ids=tuple(data.get("gateIds", ())),
            waived_at=data.get("waivedAt", ""),
            run_id=data.get("runId", ""),
            scope=data.get("scope", "selected_gates"),
            expires_at=data.get("expiresAt", ""),
        )


@dataclass(frozen=True)
class EscalationInput:
    """Minimal escalation shape the evaluator consumes (full lifecycle is Phase 4)."""

    id: str
    blocking: bool
    state: str
    reason: str
    opened_at: str
    resolved_at: str = ""

    def __post_init__(self) -> None:
        _require(self.id, "id", "EscalationInput")
        _require(self.state, "state", "EscalationInput")
        _require(self.reason, "reason", "EscalationInput")
        _require_datetime(self.opened_at, "opened_at", "EscalationInput")
        _require_datetime(self.resolved_at, "resolved_at", "EscalationInput", optional=True)

    @property
    def active(self) -> bool:
        return self.blocking and self.state in _ESCALATION_ACTIVE_STATES

    def to_dict(self) -> dict[str, Any]:
        return _prune(
            {
                "id": self.id,
                "blocking": self.blocking,
                "state": self.state,
                "reason": self.reason,
                "openedAt": self.opened_at,
                "resolvedAt": self.resolved_at,
            }
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EscalationInput":
        _validate_keys(
            data,
            frozenset({"id", "blocking", "state", "reason", "openedAt", "resolvedAt"}),
            "EscalationInput",
        )
        return cls(
            id=data.get("id", ""),
            blocking=bool(data.get("blocking", False)),
            state=data.get("state", ""),
            reason=data.get("reason", ""),
            opened_at=data.get("openedAt", ""),
            resolved_at=data.get("resolvedAt", ""),
        )


@dataclass(frozen=True)
class CompletionAssessment:
    id: str
    work_thread_id: str
    contract_id: str
    contract_version: int
    cycle: int
    sequence: int
    input_digest: str
    target_bindings: tuple[ResolvedCompletionTarget, ...]
    state: str
    evidence_backed: bool
    gate_results: tuple[CompletionGateResult, ...]
    assessed_at: str
    assessed_by: str
    triggered_by_run_id: str = ""
    supersedes_assessment_id: str = ""
    accepted_at: str = ""
    waiver: CompletionWaiver | None = None

    def __post_init__(self) -> None:
        _require(self.id, "id", "CompletionAssessment")
        _require(self.work_thread_id, "work_thread_id", "CompletionAssessment")
        _require(self.contract_id, "contract_id", "CompletionAssessment")
        _require_positive(self.contract_version, "contract_version", "CompletionAssessment")
        _require_positive(self.cycle, "cycle", "CompletionAssessment")
        _require_positive(self.sequence, "sequence", "CompletionAssessment")
        _require_choice(self.state, COMPLETION_STATES, "state", "CompletionAssessment")
        _require_choice(self.assessed_by, ASSESSED_BY, "assessed_by", "CompletionAssessment")
        _require_datetime(self.assessed_at, "assessed_at", "CompletionAssessment")
        _require_datetime(self.accepted_at, "accepted_at", "CompletionAssessment", optional=True)
        if not (
            self.input_digest.startswith("sha256:")
            and len(self.input_digest) == len("sha256:") + 64
            and all(char in "0123456789abcdef" for char in self.input_digest[7:])
        ):
            raise SchemaError(
                code="invalid_digest",
                message="CompletionAssessment.input_digest must match sha256:<64 hex>",
                recovery_hint="Use completion_input_digest() to compute the digest.",
            )
        if not self.gate_results:
            raise SchemaError(
                code="missing_field",
                message="CompletionAssessment.gate_results must contain at least one result",
                recovery_hint="Include the evaluated gate results.",
            )
        # Upstream superRefine invariants (schema.ts 762-810):
        binding_keys = [binding.key for binding in self.target_bindings]
        if len(set(binding_keys)) != len(binding_keys):
            raise SchemaError(
                code="duplicate_key",
                message="Resolved completion target key must be unique",
                recovery_hint="Each target binding key may appear once.",
            )
        result_ids = [result.gate_id for result in self.gate_results]
        if len(set(result_ids)) != len(result_ids):
            raise SchemaError(
                code="duplicate_key",
                message="Completion gate result must be unique",
                recovery_hint="Each gateId may appear once in gateResults.",
            )
        if self.state == "waived" and self.waiver is None:
            raise SchemaError(
                code="invalid_assessment",
                message="A waived completion assessment requires waiver attribution",
                recovery_hint="Attach the active waiver to the assessment.",
            )
        if self.waiver is not None and self.assessed_by != "human":
            raise SchemaError(
                code="invalid_assessment",
                message="A completion waiver must be assessed by a human",
                recovery_hint="Set assessedBy to 'human' when a waiver applies.",
            )
        if self.waiver is not None and (
            self.waiver.contract_id != self.contract_id
            or self.waiver.contract_version != self.contract_version
            or self.waiver.cycle != self.cycle
        ):
            raise SchemaError(
                code="invalid_assessment",
                message="A completion waiver must target the assessed contract version and cycle",
                recovery_hint="Use a waiver issued for this contract id/version/cycle.",
            )

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "workThreadId": self.work_thread_id,
            "contractId": self.contract_id,
            "contractVersion": self.contract_version,
            "cycle": self.cycle,
            "sequence": self.sequence,
            "inputDigest": self.input_digest,
            "targetBindings": [binding.to_dict() for binding in self.target_bindings],
            "state": self.state,
            "evidenceBacked": self.evidence_backed,
            "gateResults": [result.to_dict() for result in self.gate_results],
            "assessedAt": self.assessed_at,
            "assessedBy": self.assessed_by,
        }
        if self.triggered_by_run_id:
            payload["triggeredByRunId"] = self.triggered_by_run_id
        if self.supersedes_assessment_id:
            payload["supersedesAssessmentId"] = self.supersedes_assessment_id
        if self.accepted_at:
            payload["acceptedAt"] = self.accepted_at
        if self.waiver is not None:
            payload["waiver"] = self.waiver.to_dict()
        return payload

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CompletionAssessment":
        _validate_keys(
            data,
            frozenset(
                {
                    "id",
                    "workThreadId",
                    "triggeredByRunId",
                    "contractId",
                    "contractVersion",
                    "cycle",
                    "sequence",
                    "inputDigest",
                    "targetBindings",
                    "state",
                    "evidenceBacked",
                    "gateResults",
                    "assessedAt",
                    "assessedBy",
                    "supersedesAssessmentId",
                    "acceptedAt",
                    "waiver",
                }
            ),
            "CompletionAssessment",
        )
        waiver = data.get("waiver")
        return cls(
            id=data.get("id", ""),
            work_thread_id=data.get("workThreadId", ""),
            contract_id=data.get("contractId", ""),
            contract_version=data.get("contractVersion", 0),
            cycle=data.get("cycle", 0),
            sequence=data.get("sequence", 0),
            input_digest=data.get("inputDigest", ""),
            target_bindings=tuple(
                ResolvedCompletionTarget.from_dict(binding)
                for binding in data.get("targetBindings", ())
            ),
            state=data.get("state", ""),
            evidence_backed=bool(data.get("evidenceBacked", False)),
            gate_results=tuple(
                CompletionGateResult.from_dict(result) for result in data.get("gateResults", ())
            ),
            assessed_at=data.get("assessedAt", ""),
            assessed_by=data.get("assessedBy", ""),
            triggered_by_run_id=data.get("triggeredByRunId", ""),
            supersedes_assessment_id=data.get("supersedesAssessmentId", ""),
            accepted_at=data.get("acceptedAt", ""),
            waiver=CompletionWaiver.from_dict(waiver) if isinstance(waiver, dict) else None,
        )


@dataclass(frozen=True)
class EvaluationLineage:
    sequence: int
    supersedes_assessment_id: str = ""

    def __post_init__(self) -> None:
        _require_positive(self.sequence, "sequence", "EvaluationLineage")


@dataclass(frozen=True)
class CompletionEvaluationInput:
    contract: CompletionContract
    run_results: tuple[CompletionRunResult, ...] = ()
    artifacts: tuple[CompletionArtifact, ...] = ()
    evidence: tuple[CompletionEvidenceFact, ...] = ()
    material_action_receipts: tuple[MaterialActionReceiptFact, ...] = ()
    waivers: tuple[CompletionWaiver, ...] = ()
    blocking_escalations: tuple[EscalationInput, ...] = ()
    evaluated_at: str = ""
    lineage: EvaluationLineage | None = None


@dataclass(frozen=True)
class WorkLoopView:
    work_thread_id: str
    execution: str
    completion: str
    evidence_backed: bool
    contract_id: str
    contract_version: int
    contract_cycle: int
    contract_mode: str
    current_assessment: CompletionAssessment
    target_bindings: tuple[ResolvedCompletionTarget, ...]
    missing_gate_ids: tuple[str, ...]
    failed_gate_ids: tuple[str, ...]
    blocked_gate_ids: tuple[str, ...]
    next_action: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "workThreadId": self.work_thread_id,
            "execution": self.execution,
            "completion": self.completion,
            "evidenceBacked": self.evidence_backed,
            "contract": {
                "id": self.contract_id,
                "version": self.contract_version,
                "cycle": self.contract_cycle,
                "mode": self.contract_mode,
            },
            "currentAssessment": self.current_assessment.to_dict(),
            "targetBindings": [binding.to_dict() for binding in self.target_bindings],
            "missingGateIds": list(self.missing_gate_ids),
            "failedGateIds": list(self.failed_gate_ids),
            "blockedGateIds": list(self.blocked_gate_ids),
            "nextAction": self.next_action,
        }
