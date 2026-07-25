"""Read-only routing explanations (Phase 5 of the v0.3→v0.4 upgrade).

Ported types from OpenTag Phase 3 (`amplifthq/opentag` @ ``de70cca``,
``packages/core/src/routing.ts``): ``RoutingCandidateReasonSchema``,
``RoutingCandidateSchema``, ``RoutingDecisionSchema`` — including the
superRefine rule that a decision may select only an eligible candidate.

**What this phase deliberately does NOT port**: upstream Phase 3's
authoritative placement selector. OStag's existing router keeps making every
decision unchanged; these types only *explain* the decision after the fact
(see :mod:`ostag.copilot_relay.route_explainer`), and nothing ever feeds an
explanation back into selection.

Native mappings (declared in the Phase 5 mapping note): ``runnerId`` carries
``"{instance_id}/{runtime_id}"`` (runner ↔ device/runtime host, as declared
in Phase 3); ``executorId`` carries the relay session id (the executable
unit OStag routes to); ``runId`` carries the routed session's run id, or the
routing attempt id when no session was found.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ostag.core.access_snapshot import (
    _optional_str,
    _require_object_list,
    _require_str,
    _require_utc_datetime,
    _require_wire_keys,
)
from ostag.core.completion import _validate_keys
from ostag.core.schemas import SchemaError

RUNNER_LOCALITIES = ("local", "private", "hosted")
RUNNER_READINESS_STATES = ("ready", "stale", "draining", "at_capacity")


def _invalid(message: str, hint: str) -> SchemaError:
    return SchemaError(code="invalid_routing_decision", message=message, recovery_hint=hint)


@dataclass(frozen=True)
class RoutingCandidateReason:
    code: str
    message: str

    def __post_init__(self) -> None:
        _require_str(self.code, "code", "RoutingCandidateReason")
        _require_str(self.message, "message", "RoutingCandidateReason")

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RoutingCandidateReason":
        keys = frozenset({"code", "message"})
        _validate_keys(data, keys, "RoutingCandidateReason")
        _require_wire_keys(data, keys, "RoutingCandidateReason")
        return cls(code=data.get("code", ""), message=data.get("message", ""))


@dataclass(frozen=True)
class RoutingCapacity:
    active: int
    limit: int

    def __post_init__(self) -> None:
        if isinstance(self.active, bool) or not isinstance(self.active, int) or self.active < 0:
            raise _invalid(
                "RoutingCapacity.active must be a non-negative integer",
                "Provide active as an integer >= 0.",
            )
        if isinstance(self.limit, bool) or not isinstance(self.limit, int) or self.limit < 1:
            raise _invalid(
                "RoutingCapacity.limit must be a positive integer",
                "Provide limit as an integer >= 1.",
            )

    def to_dict(self) -> dict[str, Any]:
        return {"active": self.active, "limit": self.limit}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RoutingCapacity":
        keys = frozenset({"active", "limit"})
        _validate_keys(data, keys, "RoutingCapacity")
        _require_wire_keys(data, keys, "RoutingCapacity")
        return cls(active=data.get("active", -1), limit=data.get("limit", 0))


@dataclass(frozen=True)
class RoutingCandidate:
    runner_id: str
    executor_id: str
    eligible: bool
    reasons: tuple[RoutingCandidateReason, ...]
    locality: str = ""
    readiness: str = ""
    capacity: RoutingCapacity | None = None

    def __post_init__(self) -> None:
        _require_str(self.runner_id, "runner_id", "RoutingCandidate")
        _require_str(self.executor_id, "executor_id", "RoutingCandidate")
        if not isinstance(self.eligible, bool):
            raise _invalid(
                "RoutingCandidate.eligible must be a boolean",
                "Provide eligible as true or false.",
            )
        if (
            not isinstance(self.reasons, tuple)
            or not self.reasons
            or any(not isinstance(reason, RoutingCandidateReason) for reason in self.reasons)
        ):
            raise _invalid(
                "RoutingCandidate.reasons must be a non-empty tuple of RoutingCandidateReason",
                "Every candidate needs at least one reason (upstream .min(1)).",
            )
        _optional_str(self.locality, "locality", "RoutingCandidate")
        _optional_str(self.readiness, "readiness", "RoutingCandidate")
        if self.locality and self.locality not in RUNNER_LOCALITIES:
            raise _invalid(
                f"RoutingCandidate.locality={self.locality!r} is not one of "
                f"{list(RUNNER_LOCALITIES)}",
                "Use local, private, or hosted (or omit).",
            )
        if self.readiness and self.readiness not in RUNNER_READINESS_STATES:
            raise _invalid(
                f"RoutingCandidate.readiness={self.readiness!r} is not one of "
                f"{list(RUNNER_READINESS_STATES)}",
                "Use ready, stale, draining, or at_capacity (or omit).",
            )
        if self.capacity is not None and not isinstance(self.capacity, RoutingCapacity):
            raise _invalid(
                "RoutingCandidate.capacity must be a RoutingCapacity",
                "Build capacity via RoutingCapacity / from_dict.",
            )

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "runnerId": self.runner_id,
            "executorId": self.executor_id,
            "eligible": self.eligible,
            "reasons": [reason.to_dict() for reason in self.reasons],
        }
        if self.locality:
            payload["locality"] = self.locality
        if self.readiness:
            payload["readiness"] = self.readiness
        if self.capacity is not None:
            payload["capacity"] = self.capacity.to_dict()
        return payload

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RoutingCandidate":
        _validate_keys(
            data,
            frozenset(
                {"runnerId", "executorId", "eligible", "reasons", "locality", "readiness", "capacity"}
            ),
            "RoutingCandidate",
        )
        _require_wire_keys(
            data,
            frozenset({"runnerId", "executorId", "eligible", "reasons"}),
            "RoutingCandidate",
        )
        capacity = data.get("capacity")
        return cls(
            runner_id=data.get("runnerId", ""),
            executor_id=data.get("executorId", ""),
            eligible=data.get("eligible", False),
            reasons=tuple(
                RoutingCandidateReason.from_dict(reason)
                for reason in _require_object_list(
                    data.get("reasons"), "reasons", "RoutingCandidate"
                )
            ),
            locality=data.get("locality", ""),
            readiness=data.get("readiness", ""),
            capacity=RoutingCapacity.from_dict(capacity) if capacity is not None else None,
        )


@dataclass(frozen=True)
class SelectedRoute:
    runner_id: str
    executor_id: str

    def __post_init__(self) -> None:
        _require_str(self.runner_id, "runner_id", "SelectedRoute")
        _require_str(self.executor_id, "executor_id", "SelectedRoute")

    def to_dict(self) -> dict[str, Any]:
        return {"runnerId": self.runner_id, "executorId": self.executor_id}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SelectedRoute":
        keys = frozenset({"runnerId", "executorId"})
        _validate_keys(data, keys, "SelectedRoute")
        _require_wire_keys(data, keys, "SelectedRoute")
        return cls(runner_id=data.get("runnerId", ""), executor_id=data.get("executorId", ""))


@dataclass(frozen=True)
class RoutingDecision:
    id: str
    run_id: str
    reason_code: str
    reason: str
    decided_at: str
    candidates: tuple[RoutingCandidate, ...] = ()
    selected: SelectedRoute | None = None
    policy_snapshot_id: str = ""
    access_profile_snapshot_id: str = ""

    def __post_init__(self) -> None:
        _require_str(self.id, "id", "RoutingDecision")
        _require_str(self.run_id, "run_id", "RoutingDecision")
        _require_str(self.reason_code, "reason_code", "RoutingDecision")
        _require_str(self.reason, "reason", "RoutingDecision")
        _require_utc_datetime(self.decided_at, "decided_at", "RoutingDecision")
        _optional_str(self.policy_snapshot_id, "policy_snapshot_id", "RoutingDecision")
        _optional_str(
            self.access_profile_snapshot_id, "access_profile_snapshot_id", "RoutingDecision"
        )
        if not isinstance(self.candidates, tuple) or any(
            not isinstance(candidate, RoutingCandidate) for candidate in self.candidates
        ):
            raise _invalid(
                "RoutingDecision.candidates must be a tuple of RoutingCandidate",
                "Build candidates from RoutingCandidate values.",
            )
        if self.selected is not None:
            if not isinstance(self.selected, SelectedRoute):
                raise _invalid(
                    "RoutingDecision.selected must be a SelectedRoute",
                    "Build selected via SelectedRoute / from_dict.",
                )
            # Upstream superRefine: only an eligible candidate may be selected.
            match = next(
                (
                    candidate
                    for candidate in self.candidates
                    if candidate.runner_id == self.selected.runner_id
                    and candidate.executor_id == self.selected.executor_id
                ),
                None,
            )
            if match is None or not match.eligible:
                raise _invalid(
                    "A routing decision may select only an eligible candidate.",
                    "The selected route must appear among candidates with eligible=true.",
                )

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "runId": self.run_id,
            "candidates": [candidate.to_dict() for candidate in self.candidates],
            "reasonCode": self.reason_code,
            "reason": self.reason,
            "decidedAt": self.decided_at,
        }
        if self.policy_snapshot_id:
            payload["policySnapshotId"] = self.policy_snapshot_id
        if self.access_profile_snapshot_id:
            payload["accessProfileSnapshotId"] = self.access_profile_snapshot_id
        if self.selected is not None:
            payload["selected"] = self.selected.to_dict()
        return payload

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RoutingDecision":
        _validate_keys(
            data,
            frozenset(
                {
                    "id",
                    "runId",
                    "policySnapshotId",
                    "accessProfileSnapshotId",
                    "candidates",
                    "selected",
                    "reasonCode",
                    "reason",
                    "decidedAt",
                }
            ),
            "RoutingDecision",
        )
        _require_wire_keys(
            data,
            frozenset({"id", "runId", "candidates", "reasonCode", "reason", "decidedAt"}),
            "RoutingDecision",
        )
        selected = data.get("selected")
        return cls(
            id=data.get("id", ""),
            run_id=data.get("runId", ""),
            reason_code=data.get("reasonCode", ""),
            reason=data.get("reason", ""),
            decided_at=data.get("decidedAt", ""),
            candidates=tuple(
                RoutingCandidate.from_dict(candidate)
                for candidate in _require_object_list(
                    data.get("candidates"), "candidates", "RoutingDecision"
                )
            ),
            selected=SelectedRoute.from_dict(selected) if selected is not None else None,
            policy_snapshot_id=data.get("policySnapshotId", ""),
            access_profile_snapshot_id=data.get("accessProfileSnapshotId", ""),
        )
