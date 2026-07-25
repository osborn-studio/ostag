"""Read-only routing explanations for the Copilot relay (Phase 5).

The existing router (:class:`CopilotRelayService`) keeps making every
decision **unchanged** — this module wraps it, lets the real call happen,
and derives a :class:`RoutingDecision` from the actual outcome plus
read-only store lookups. Nothing here writes to the relay store, alters
eligibility, or feeds back into selection; the corpus equivalence test
proves the wrapped and bare routers produce identical results and identical
relay-store state.

``selected`` is taken **from the actual routed envelope's binding**, so it
equals the actual route by construction; candidate eligibility is derived
from the same rules the router applied (access policy, terminal state) and
cross-checked in tests. If a real decision ever could not be represented
without changing eligibility semantics, the explainer records a
``schema_mismatch`` decision — it never adjusts the route to fit the schema.
"""

from __future__ import annotations

import json

from ostag.core.identity import Principal
from ostag.core.routing_explanation import (
    RoutingCandidate,
    RoutingCandidateReason,
    RoutingDecision,
    SelectedRoute,
)
from ostag.core.safety import sanitize_user_text
from ostag.core.schemas import SchemaError
from ostag.work_monitor.identity import IdentityStore
from ostag.work_monitor.store import MonitorStore

from .service import CopilotRelayService, RelayResult, RelayRoutingError
from .store import RelaySession

_STATE_REASON_CODES = {
    "queued": "routed",
    "duplicate": "duplicate_event",
    "requires_approval": "policy_requires_approval",
    "requires_confirmation": "policy_requires_confirmation",
}

_ERROR_REASON_CODES = (
    ("message text is required", "empty_text"),
    ("quoted relay message was not found", "quoted_message_not_found"),
    ("no current session", "no_current_session"),
    ("session is terminal", "terminal_session"),
    ("stale decision session version", "stale_decision_version"),
    ("decision expired or was already answered", "decision_expired"),
)


def _runner_id(session: RelaySession) -> str:
    return f"{session.instance_id}/{session.runtime_id}"


def _clean(text: str, limit: int = 200) -> str:
    return " ".join(sanitize_user_text(text).split())[:limit]


class RouteExplainer:
    """Wraps the relay router; every text routing gains a deterministic,
    read-only explanation persisted to the monitor store."""

    def __init__(
        self,
        service: CopilotRelayService,
        decisions: "RoutingDecisionStore | None" = None,
    ) -> None:
        self.service = service
        self.decisions = decisions

    def handle_text(
        self,
        principal: Principal,
        text: str,
        *,
        quote_message_id: str | None = None,
        event_id: str | None = None,
        decision_id: str,
        decided_at: str,
    ) -> tuple[RelayResult, RoutingDecision]:
        """Route via the REAL router, then explain the actual outcome.

        The router call is byte-for-byte the same as calling the service
        directly; on router errors the explanation is recorded and the
        original exception re-raised unchanged.
        """
        candidates, candidate_session = self._derive_candidates(principal, quote_message_id)
        try:
            result = self.service.handle_text(
                principal, text, quote_message_id=quote_message_id, event_id=event_id
            )
        except (RelayRoutingError, PermissionError) as exc:
            decision = self._decision_for_error(
                exc, principal, candidates, candidate_session,
                decision_id=decision_id, decided_at=decided_at,
            )
            self._record(principal, decision)
            raise
        decision = self._decision_for_result(
            result, candidates, candidate_session,
            decision_id=decision_id, decided_at=decided_at,
        )
        self._record(principal, decision)
        return result, decision

    # -- derivation -----------------------------------------------------------

    def _derive_candidates(
        self, principal: Principal, quote_message_id: str | None
    ) -> tuple[tuple[RoutingCandidate, ...], RelaySession | None]:
        """Read-only reconstruction of the session the router will consider.

        OStag's relay router is deterministic with a single candidate: the
        quoted envelope's session, or the principal's current session. The
        eligibility flags reuse the router's own rules (access policy,
        terminal state) — never a reimplementation of selection.
        """
        store = self.service.store
        source = store.envelope_for_message(quote_message_id) if quote_message_id else None
        if quote_message_id and source is None:
            return (), None
        session = (
            store.get_session(
                source.binding.instance_id,
                source.binding.session_id,
                source.binding.runtime_id,
            )
            if source is not None
            else store.current_session(principal.platform, principal.actor_id)
        )
        if session is None:
            return (), None
        reasons: list[RoutingCandidateReason] = [
            RoutingCandidateReason(
                code="quoted_session" if source is not None else "current_session",
                message=(
                    "Session referenced by the quoted relay message."
                    if source is not None
                    else "The principal's currently selected session."
                ),
            )
        ]
        eligible = True
        if not self.service.policy.can_access(principal, session.instance_id):
            eligible = False
            reasons.append(
                RoutingCandidateReason(
                    code="access_denied",
                    message="The principal cannot access this instance.",
                )
            )
        if session.state in {"completed", "failed", "cancelled"}:
            eligible = False
            reasons.append(
                RoutingCandidateReason(
                    code="session_terminal",
                    message=f"The session is terminal (state={session.state}).",
                )
            )
        candidate = RoutingCandidate(
            runner_id=_runner_id(session),
            executor_id=session.session_id,
            eligible=eligible,
            reasons=tuple(reasons),
            locality="local",  # relay sessions execute on the bound device
        )
        return (candidate,), session

    def _decision_for_result(
        self,
        result: RelayResult,
        candidates: tuple[RoutingCandidate, ...],
        session: RelaySession | None,
        *,
        decision_id: str,
        decided_at: str,
    ) -> RoutingDecision:
        selected = None
        if result.outbound is not None:
            binding = result.outbound.binding
            selected = SelectedRoute(
                runner_id=f"{binding.instance_id}/{binding.runtime_id}",
                executor_id=binding.session_id,
            )
        reason_code = _STATE_REASON_CODES.get(result.state, "schema_mismatch")
        reason = {
            "routed": "The message was routed to the selected session.",
            "duplicate_event": "The event was already routed; the original envelope stands.",
            "policy_requires_approval": (
                "The relay policy classified this instruction as high-risk; "
                "no route occurs without receipt approval."
            ),
            "policy_requires_confirmation": (
                "The relay policy requires an explicit confirmation before this "
                "proposal is executed on the selected session."
            ),
            "schema_mismatch": (
                f"The router produced state {result.state!r}, which this explanation "
                "schema cannot represent; the actual route is unchanged."
            ),
        }[reason_code]
        if reason_code == "policy_requires_approval":
            # No envelope was produced; nothing was routed.
            selected = None
        return RoutingDecision(
            id=decision_id,
            run_id=session.run_id if session is not None else decision_id,
            reason_code=reason_code,
            reason=reason,
            decided_at=decided_at,
            candidates=candidates,
            selected=selected,
            policy_snapshot_id=(
                f"ostag:{session.instance_id}:unversioned" if session is not None else ""
            ),
        )

    def _decision_for_error(
        self,
        error: Exception,
        principal: Principal,
        candidates: tuple[RoutingCandidate, ...],
        session: RelaySession | None,
        *,
        decision_id: str,
        decided_at: str,
    ) -> RoutingDecision:
        message = str(error)
        if isinstance(error, PermissionError):
            reason_code = "access_denied"
        else:
            reason_code = next(
                (code for marker, code in _ERROR_REASON_CODES if marker in message),
                "routing_error",
            )
        return RoutingDecision(
            id=decision_id,
            run_id=session.run_id if session is not None else decision_id,
            reason_code=reason_code,
            reason=f"The router rejected this message: {_clean(message)}",
            decided_at=decided_at,
            candidates=candidates,
            selected=None,
            policy_snapshot_id=(
                f"ostag:{session.instance_id}:unversioned" if session is not None else ""
            ),
        )

    def _record(self, principal: Principal, decision: RoutingDecision) -> None:
        if self.decisions is not None:
            self.decisions.record(principal, decision)


class RoutingDecisionStore:
    """Instance/principal-scoped persistence + authorized views for
    routing decisions. Read-only with respect to routing: nothing here is
    ever consulted by the router."""

    def __init__(self, store: MonitorStore, identities: IdentityStore | None = None) -> None:
        self.store = store
        self.identities = identities

    def record(self, principal: Principal, decision: RoutingDecision) -> bool:
        """Insert-or-verify (immutable, like snapshots). Returns True when created."""
        instance_id = ""
        if decision.selected is not None:
            instance_id = decision.selected.runner_id.split("/", 1)[0]
        elif decision.candidates:
            instance_id = decision.candidates[0].runner_id.split("/", 1)[0]
        payload = json.dumps(decision.to_dict(), sort_keys=True, separators=(",", ":"))
        with self.store.transaction() as connection:
            row = connection.execute(
                "SELECT decision_json FROM routing_decisions WHERE decision_id=?",
                (decision.id,),
            ).fetchone()
            if row is not None:
                if row["decision_json"] != payload:
                    raise SchemaError(
                        code="immutable_record",
                        message=f"RoutingDecision {decision.id} is immutable",
                        recovery_hint="Record a new decision id instead of editing.",
                    )
                return False
            connection.execute(
                "INSERT INTO routing_decisions(decision_id,instance_id,principal_platform,"
                "principal_actor_id,run_id,decision_json,decided_at) VALUES(?,?,?,?,?,?,?)",
                (
                    decision.id,
                    instance_id,
                    principal.platform,
                    principal.actor_id,
                    decision.run_id,
                    payload,
                    decision.decided_at,
                ),
            )
            return True

    def get(self, decision_id: str) -> RoutingDecision:
        """Internal, unredacted read; external consumers use the views."""
        with self.store.connect() as connection:
            row = connection.execute(
                "SELECT decision_json FROM routing_decisions WHERE decision_id=?",
                (decision_id,),
            ).fetchone()
        if row is None:
            raise SchemaError(
                code="not_found",
                message=f"RoutingDecision {decision_id} not found",
                recovery_hint="Check the decision id.",
            )
        return RoutingDecision.from_dict(json.loads(row["decision_json"]))

    # -- authorized views -----------------------------------------------------

    def personal_views(self, principal: Principal) -> tuple[dict, ...]:
        """The principal's own routing decisions only."""
        self._require_identities()
        with self.store.connect() as connection:
            rows = connection.execute(
                "SELECT decision_json FROM routing_decisions WHERE principal_platform=? "
                "AND principal_actor_id=? ORDER BY decided_at, decision_id",
                (principal.platform, principal.actor_id),
            ).fetchall()
        return tuple(
            self._render(json.loads(row["decision_json"])) for row in rows
        )

    def admin_views(self, principal: Principal) -> tuple[dict, ...]:
        """All routing decisions; requires a bound global owner."""
        identities = self._require_identities()
        if not identities.is_global_owner(principal):
            raise SchemaError(
                code="permission_denied",
                message="Team routing-decision views require a bound global owner",
                recovery_hint="Use the personal view, or bind an owner/admin principal.",
            )
        with self.store.connect() as connection:
            rows = connection.execute(
                "SELECT decision_json FROM routing_decisions ORDER BY decided_at, decision_id"
            ).fetchall()
        return tuple(
            self._render(json.loads(row["decision_json"])) for row in rows
        )

    def _require_identities(self) -> IdentityStore:
        if self.identities is None:
            raise SchemaError(
                code="permission_denied",
                message="Routing-decision views require an identity store",
                recovery_hint="Construct RoutingDecisionStore with an IdentityStore.",
            )
        return self.identities

    @staticmethod
    def _render(data: dict) -> dict:
        decision = RoutingDecision.from_dict(data)
        rendered: dict = {
            "id": _clean(decision.id, 80),
            "runId": _clean(decision.run_id, 80),
            "reasonCode": decision.reason_code,
            "reason": _clean(decision.reason),
            "decidedAt": decision.decided_at,
            "candidates": [
                {
                    "runnerId": _clean(candidate.runner_id, 120),
                    "executorId": _clean(candidate.executor_id, 120),
                    "eligible": candidate.eligible,
                    "reasons": [
                        {"code": reason.code, "message": _clean(reason.message)}
                        for reason in candidate.reasons
                    ],
                }
                for candidate in decision.candidates
            ],
        }
        if decision.selected is not None:
            rendered["selected"] = {
                "runnerId": _clean(decision.selected.runner_id, 120),
                "executorId": _clean(decision.selected.executor_id, 120),
            }
        return rendered
