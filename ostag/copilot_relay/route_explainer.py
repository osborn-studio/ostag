"""Read-only routing explanations for the Copilot relay (Phase 5).

The existing router (:class:`CopilotRelayService`) keeps making every
decision **unchanged** — this module wraps it, lets the real call happen,
and derives a :class:`RoutingDecision` strictly **afterwards** from the
actual outcome. Three properties hold by construction:

1. **No behavioral influence.** The explainer never calls the relay policy
   (a stateful ``can_access`` provider would observe extra lookups), never
   writes relay state, and derives everything from the result the router
   already produced plus plain SQLite reads. Explanation recording is
   best-effort: a persistence failure can never change the routing result
   or mask a router exception (mirrors the Phase 3 shadow-hook contract).
2. **``selected`` equals the actual route.** It is taken from the routed
   envelope's binding — including duplicate events, where the router
   returns the ORIGINAL envelope (possibly bound to a different session
   than the principal's current one).
3. **The route is never adjusted to fit the schema.** If an outcome cannot
   be represented, a ``schema_mismatch`` decision is recorded instead
   (handoff §9).
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

from .models import SessionBinding
from .service import CopilotRelayService, RelayResult, RelayRoutingError
from .store import RelaySession

_ERROR_REASON_CODES = (
    ("message text is required", "empty_text"),
    ("quoted relay message was not found", "quoted_message_not_found"),
    ("no current session", "no_current_session"),
    ("session is terminal", "terminal_session"),
    ("stale decision session version", "stale_decision_version"),
    ("decision expired or was already answered", "decision_expired"),
    ("expected a task proposal", "proposal_invalid"),
    ("proposal does not match", "proposal_mismatch"),
    ("proposal expired or was already confirmed", "proposal_expired"),
)

_RESULT_REASONS = {
    "routed": "The message was routed to the selected session.",
    "duplicate_event": (
        "The event was already routed; the original envelope and its route stand."
    ),
    "policy_requires_approval": (
        "The relay policy classified this instruction as high-risk; "
        "no route occurs without receipt approval."
    ),
    "policy_requires_confirmation": (
        "The relay policy requires an explicit confirmation before this "
        "proposal is executed on the selected session."
    ),
    "snapshot_requested": "A read-only snapshot was requested from the selected session.",
    "proposal_confirmed": "The confirmed proposal was routed to its original session.",
}


def _clean(text: str, limit: int = 200) -> str:
    return " ".join(sanitize_user_text(text).split())[:limit]


class RouteExplainer:
    """Wraps relay routing entry points; every decision gains a
    deterministic, read-only explanation recorded best-effort."""

    def __init__(
        self,
        service: CopilotRelayService,
        decisions: "RoutingDecisionStore | None" = None,
    ) -> None:
        self.service = service
        self.decisions = decisions

    # -- wrapped entry points -------------------------------------------------

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
        directly; on router errors the explanation is recorded (best-effort)
        and the original exception re-raised unchanged.
        """
        try:
            result = self.service.handle_text(
                principal, text, quote_message_id=quote_message_id, event_id=event_id
            )
        except (RelayRoutingError, PermissionError) as exc:
            error = exc  # except-name is unbound after the block; lambdas need a local
            self._derive_and_record_safe(
                principal,
                lambda: self._explain_error(
                    principal, error, quote_message_id,
                    decision_id=decision_id, decided_at=decided_at,
                ),
                decision_id, decided_at,
            )
            raise
        decision = self._derive_and_record_safe(
            principal,
            lambda: self._explain_result(
                principal, result, quote_message_id,
                decision_id=decision_id, decided_at=decided_at,
            ),
            decision_id, decided_at,
        )
        return result, decision

    def request_snapshot(
        self,
        principal: Principal,
        *,
        event_id: str | None = None,
        decision_id: str,
        decided_at: str,
    ) -> tuple[RelayResult, RoutingDecision]:
        try:
            result = self.service.request_snapshot(principal, event_id=event_id)
        except (RelayRoutingError, PermissionError) as exc:
            error = exc
            self._derive_and_record_safe(
                principal,
                lambda: self._explain_error(
                    principal, error, None, decision_id=decision_id, decided_at=decided_at
                ),
                decision_id, decided_at,
            )
            raise
        decision = self._derive_and_record_safe(
            principal,
            lambda: self._explain_result(
                principal, result, None,
                decision_id=decision_id, decided_at=decided_at,
                success_code="snapshot_requested",
            ),
            decision_id, decided_at,
        )
        return result, decision

    def confirm_task_proposal(
        self,
        principal: Principal,
        proposal,
        *,
        confirmed_at: str,
        decision_id: str,
        decided_at: str,
    ) -> tuple[RelayResult, RoutingDecision]:
        try:
            result = self.service.confirm_task_proposal(
                principal, proposal, confirmed_at=confirmed_at
            )
        except (RelayRoutingError, PermissionError) as exc:
            error = exc
            self._derive_and_record_safe(
                principal,
                # The considered session is the PROPOSAL's binding — known
                # routing context that must not be discarded on failure.
                lambda: self._explain_error(
                    principal, error, None,
                    decision_id=decision_id, decided_at=decided_at,
                    binding=getattr(proposal, "binding", None),
                ),
                decision_id, decided_at,
            )
            raise
        decision = self._derive_and_record_safe(
            principal,
            lambda: self._explain_result(
                principal, result, None,
                decision_id=decision_id, decided_at=decided_at,
                success_code="proposal_confirmed",
            ),
            decision_id, decided_at,
        )
        return result, decision

    # -- derivation (all post-hoc, all read-only) ------------------------------

    def _explain_result(
        self,
        principal: Principal,
        result: RelayResult,
        quote_message_id: str | None,
        *,
        decision_id: str,
        decided_at: str,
        success_code: str = "routed",
    ) -> RoutingDecision:
        try:
            if result.outbound is not None:
                binding = result.outbound.binding
                if result.state == "duplicate":
                    reason_code = "duplicate_event"
                    candidate_code = "duplicate_original_route"
                    candidate_message = (
                        "The original route of the already-processed event."
                    )
                elif result.state == "requires_confirmation":
                    reason_code = "policy_requires_confirmation"
                    candidate_code = "proposal_target"
                    candidate_message = (
                        "The session this proposal will execute on once confirmed."
                    )
                else:
                    reason_code = success_code
                    candidate_code = (
                        "quoted_session" if quote_message_id else "current_session"
                    )
                    candidate_message = (
                        "Session referenced by the quoted relay message."
                        if quote_message_id
                        else "The principal's currently selected session."
                    )
                candidate = self._candidate_from_binding(
                    binding, code=candidate_code, message=candidate_message
                )
                return RoutingDecision(
                    id=decision_id,
                    run_id=binding.run_id or decision_id,
                    reason_code=reason_code,
                    reason=_RESULT_REASONS[reason_code],
                    decided_at=decided_at,
                    candidates=(candidate,),
                    selected=SelectedRoute(
                        runner_id=candidate.runner_id, executor_id=candidate.executor_id
                    ),
                    policy_snapshot_id=f"ostag:{binding.instance_id}:unversioned",
                )
            if result.state == "requires_approval":
                # The router validated the session (existence, access,
                # non-terminal) before classifying — eligibility is proven by
                # the actual control flow, not re-derived from policy calls.
                session = self._session_considered(principal, quote_message_id)
                candidates = (
                    (
                        self._candidate_from_session(
                            session,
                            code=(
                                "quoted_session" if quote_message_id else "current_session"
                            ),
                            message="The session that would receive this instruction.",
                        ),
                    )
                    if session is not None
                    else ()
                )
                return RoutingDecision(
                    id=decision_id,
                    run_id=(session.run_id if session is not None else decision_id),
                    reason_code="policy_requires_approval",
                    reason=_RESULT_REASONS["policy_requires_approval"],
                    decided_at=decided_at,
                    candidates=candidates,
                    selected=None,
                    policy_snapshot_id=(
                        f"ostag:{session.instance_id}:unversioned"
                        if session is not None
                        else ""
                    ),
                )
            return self._schema_mismatch(
                decision_id, decided_at,
                f"The router produced state {result.state!r}, which this explanation "
                "schema cannot represent; the actual route is unchanged.",
            )
        except SchemaError:
            return self._schema_mismatch(
                decision_id, decided_at,
                "The actual routing outcome could not be represented without "
                "changing eligibility semantics; the actual route is unchanged.",
            )

    def _explain_error(
        self,
        principal: Principal,
        error: Exception,
        quote_message_id: str | None,
        *,
        decision_id: str,
        decided_at: str,
        binding: SessionBinding | None = None,
    ) -> RoutingDecision:
        message = str(error)
        if isinstance(error, PermissionError):
            reason_code = "access_denied"
        else:
            reason_code = next(
                (code for marker, code in _ERROR_REASON_CODES if marker in message),
                "routing_error",
            )
        try:
            if binding is not None:
                # Known routing context (e.g. a proposal's binding): keep it —
                # the failure is about the attempt, not the target's identity.
                candidate = self._candidate_from_binding(
                    binding,
                    code="proposal_target",
                    message="The session the failed attempt targeted.",
                )
                return RoutingDecision(
                    id=decision_id,
                    run_id=binding.run_id or decision_id,
                    reason_code=reason_code,
                    reason=f"The router rejected this message: {_clean(message)}",
                    decided_at=decided_at,
                    candidates=(candidate,),
                    selected=None,
                    policy_snapshot_id=f"ostag:{binding.instance_id}:unversioned",
                )
            session = self._session_considered(principal, quote_message_id)
            candidates: tuple[RoutingCandidate, ...] = ()
            if session is not None and reason_code in (
                "access_denied",
                "terminal_session",
                "stale_decision_version",
                "decision_expired",
            ):
                # Eligibility derived from the router's actual rejection —
                # never from re-invoking the policy.
                ineligible = reason_code in ("access_denied", "terminal_session")
                reasons = [
                    RoutingCandidateReason(
                        code="current_session" if not quote_message_id else "quoted_session",
                        message="The session the router considered for this message.",
                    )
                ]
                if reason_code == "access_denied":
                    reasons.append(
                        RoutingCandidateReason(
                            code="access_denied",
                            message="The router denied the principal access to this instance.",
                        )
                    )
                if reason_code == "terminal_session":
                    reasons.append(
                        RoutingCandidateReason(
                            code="session_terminal",
                            message=f"The session is terminal (state={session.state}).",
                        )
                    )
                candidates = (
                    RoutingCandidate(
                        runner_id=f"{session.instance_id}/{session.runtime_id}",
                        executor_id=session.session_id,
                        eligible=not ineligible,
                        reasons=tuple(reasons),
                    ),
                )
            return RoutingDecision(
                id=decision_id,
                run_id=(session.run_id if session is not None else decision_id),
                reason_code=reason_code,
                reason=f"The router rejected this message: {_clean(message)}",
                decided_at=decided_at,
                candidates=candidates,
                selected=None,
                policy_snapshot_id=(
                    f"ostag:{session.instance_id}:unversioned" if session is not None else ""
                ),
            )
        except SchemaError:
            return self._schema_mismatch(
                decision_id, decided_at,
                "The routing failure could not be represented without changing "
                "eligibility semantics; the actual (rejected) outcome is unchanged.",
            )

    def _session_considered(
        self, principal: Principal, quote_message_id: str | None
    ) -> RelaySession | None:
        """Read-only reconstruction of the session the router looked at.
        Pure SQLite reads — never a policy call."""
        store = self.service.store
        source = store.envelope_for_message(quote_message_id) if quote_message_id else None
        if quote_message_id and source is None:
            return None
        if source is not None:
            return store.get_session(
                source.binding.instance_id,
                source.binding.session_id,
                source.binding.runtime_id,
            )
        return store.current_session(principal.platform, principal.actor_id)

    @staticmethod
    def _candidate_from_binding(
        binding: SessionBinding, *, code: str, message: str
    ) -> RoutingCandidate:
        return RoutingCandidate(
            runner_id=f"{binding.instance_id}/{binding.runtime_id}",
            executor_id=binding.session_id,
            eligible=True,  # proven: the router actually routed to it
            reasons=(RoutingCandidateReason(code=code, message=message),),
        )

    @staticmethod
    def _candidate_from_session(
        session: RelaySession, *, code: str, message: str
    ) -> RoutingCandidate:
        return RoutingCandidate(
            runner_id=f"{session.instance_id}/{session.runtime_id}",
            executor_id=session.session_id,
            eligible=True,
            reasons=(RoutingCandidateReason(code=code, message=message),),
        )

    @staticmethod
    def _schema_mismatch(decision_id: str, decided_at: str, reason: str) -> RoutingDecision:
        return RoutingDecision(
            id=decision_id,
            run_id=decision_id,
            reason_code="schema_mismatch",
            reason=reason,
            decided_at=decided_at,
            candidates=(),
            selected=None,
        )

    def _derive_and_record_safe(
        self,
        principal: Principal,
        derive,
        decision_id: str,
        decided_at: str,
    ) -> RoutingDecision | None:
        """The COMPLETE explanation pipeline — derivation and recording — is
        best-effort: no exception here (schema, sqlite, anything) may replace
        the routing result or mask a router exception (Phase 3 shadow-hook
        contract). A failed derivation degrades to an ``explanation_error``
        decision; if even the caller-supplied id/timestamp are invalid, the
        fallback degrades further to pure constants, and as a last resort the
        explanation is dropped (None) — routing is untouched in every case."""
        try:
            decision = derive()
        except Exception:  # noqa: BLE001 - derivation must not affect routing
            decision = self._explanation_error(decision_id, decided_at)
        if decision is not None:
            self._record_safe(principal, decision)
        return decision

    @staticmethod
    def _explanation_error(decision_id: str, decided_at: str) -> RoutingDecision | None:
        """Guaranteed non-throwing fallback: tries the caller-supplied stamp,
        then a valid epoch stamp, then pure constants; never raises."""
        reason = (
            "The explanation could not be derived; the actual routing "
            "outcome is unchanged."
        )
        for candidate_id, candidate_at in (
            (decision_id, decided_at),
            (decision_id, "1970-01-01T00:00:00+00:00"),
            ("rd_invalid", "1970-01-01T00:00:00+00:00"),
        ):
            try:
                return RoutingDecision(
                    id=candidate_id,
                    run_id=candidate_id,
                    reason_code="explanation_error",
                    reason=reason,
                    decided_at=candidate_at,
                    candidates=(),
                    selected=None,
                )
            except Exception:  # noqa: BLE001 - keep degrading, never raise
                continue
        return None

    def _record_safe(self, principal: Principal, decision: RoutingDecision) -> None:
        """Best-effort: a recording failure must never change the routing
        result or mask a router exception (Phase 3 shadow-hook contract)."""
        if self.decisions is None:
            return
        try:
            self.decisions.record(principal, decision)
        except Exception:  # noqa: BLE001 - explanation loss must not affect routing
            pass


class RoutingDecisionStore:
    """Principal-scoped persistence + authorized views for routing decisions.
    Read-only with respect to routing: nothing here is ever consulted by the
    router."""

    def __init__(self, store: MonitorStore, identities: IdentityStore | None = None) -> None:
        self.store = store
        self.identities = identities

    def record(self, principal: Principal, decision: RoutingDecision) -> bool:
        """Insert-or-verify (immutable). An identical replay by the SAME
        principal returns False; content OR attribution mismatch raises —
        a cross-principal id collision must never be silently absorbed."""
        instance_id = ""
        if decision.selected is not None:
            instance_id = decision.selected.runner_id.split("/", 1)[0]
        elif decision.candidates:
            instance_id = decision.candidates[0].runner_id.split("/", 1)[0]
        payload = json.dumps(decision.to_dict(), sort_keys=True, separators=(",", ":"))
        with self.store.transaction() as connection:
            row = connection.execute(
                "SELECT decision_json, principal_platform, principal_actor_id "
                "FROM routing_decisions WHERE decision_id=?",
                (decision.id,),
            ).fetchone()
            if row is not None:
                if (
                    row["principal_platform"] != principal.platform
                    or row["principal_actor_id"] != principal.actor_id
                ):
                    raise SchemaError(
                        code="conflict",
                        message=f"RoutingDecision {decision.id} is attributed to a "
                        "different principal",
                        recovery_hint="Use a distinct decision id per routing attempt.",
                    )
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
        return tuple(self._render(json.loads(row["decision_json"])) for row in rows)

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
        return tuple(self._render(json.loads(row["decision_json"])) for row in rows)

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
        """Sanitized projection: EVERY emitted string passes _clean, including
        reason codes (the model only requires them non-empty)."""
        decision = RoutingDecision.from_dict(data)
        rendered: dict = {
            "id": _clean(decision.id, 80),
            "runId": _clean(decision.run_id, 80),
            "reasonCode": _clean(decision.reason_code, 60),
            "reason": _clean(decision.reason),
            "decidedAt": decision.decided_at,
            "candidates": [
                {
                    "runnerId": _clean(candidate.runner_id, 120),
                    "executorId": _clean(candidate.executor_id, 120),
                    "eligible": candidate.eligible,
                    "reasons": [
                        {"code": _clean(reason.code, 60), "message": _clean(reason.message)}
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
