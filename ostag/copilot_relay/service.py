"""Deterministic human-to-session routing for Copilot Relay."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from ostag.core.identity import Principal

from .models import RelayEnvelope, SessionBinding
from .policy import PolicyDecision, RelayPolicy
from .store import RelaySession, RelayStore


class RelayRoutingError(ValueError):
    pass


@dataclass(frozen=True)
class RelayResult:
    state: str
    outbound: RelayEnvelope | None = None
    proposal_id: str | None = None
    policy: PolicyDecision | None = None


class CopilotRelayService:
    def __init__(self, store: RelayStore, policy: RelayPolicy) -> None:
        self.store = store
        self.policy = policy

    def select_session(
        self, principal: Principal, instance_id: str, session_id: str, *, selected_at: str
    ) -> RelaySession:
        if not self.policy.can_access(principal, instance_id):
            raise PermissionError("principal cannot access this instance")
        session = self.store.get_session(instance_id, session_id)
        if session is None:
            raise RelayRoutingError("unknown session")
        self.store.select_current_task(
            principal.platform, principal.actor_id, instance_id, session_id,
            selected_at=selected_at,
        )
        return session

    def handle_text(
        self,
        principal: Principal,
        text: str,
        *,
        quote_message_id: str | None = None,
        event_id: str | None = None,
    ) -> RelayResult:
        if not text.strip():
            raise RelayRoutingError("message text is required")
        if event_id is not None:
            existing = self.store.envelope_for_message(f"relay_{event_id}")
            if existing is not None:
                if existing.sender != principal:
                    raise PermissionError("event_id is already bound to another principal")
                return RelayResult("duplicate", outbound=existing, policy=self.policy.classify(text))
        source = self.store.envelope_for_message(quote_message_id) if quote_message_id else None
        if quote_message_id and source is None:
            raise RelayRoutingError("quoted relay message was not found")
        current = (
            self.store.get_session(source.binding.instance_id, source.binding.session_id)
            if source is not None
            else self.store.current_session(principal.platform, principal.actor_id)
        )
        if current is None:
            raise RelayRoutingError("no current session; select a task first")
        if not self.policy.can_access(principal, current.instance_id):
            raise PermissionError("principal cannot access this instance")
        policy = self.policy.classify(text)
        if policy.outcome == "requires_approval":
            return RelayResult("requires_approval", policy=policy)
        binding = self._binding(current, source.binding.turn_id if source else current.last_turn_id)
        if source is not None and source.kind == "decision.request":
            if source.binding.session_version != current.session_version:
                raise RelayRoutingError("stale decision session version")
            outbound = self._envelope(
                principal, binding, "decision.answer",
                {
                    "decision_id": source.payload["decision_id"],
                    "choice_id": None,
                    "answer": text,
                },
                event_id,
            )
            inserted = self.store.enqueue(outbound, direction="outbound")
            return RelayResult("queued" if inserted else "duplicate", outbound=outbound, policy=policy)
        if policy.outcome == "requires_confirmation":
            proposal_id = f"proposal_{event_id or uuid4().hex}"
            objective = text.split("：", 1)[-1].split(":", 1)[-1].strip()
            proposal = self._envelope(
                principal, binding, "task.create.proposal",
                {
                    "proposal_id": proposal_id,
                    "objective": objective,
                    "repository": "current",
                    "workspace": "managed",
                    "suggested_agent": current.agent_id,
                    "acceptance_criteria": [],
                    "sensitive_operations": [],
                },
                event_id,
            )
            self.store.record_proposal(proposal)
            return RelayResult("requires_confirmation", outbound=proposal, proposal_id=proposal_id, policy=policy)
        outbound = self._envelope(
            principal, binding, "instruction.send", {"text": text}, event_id
        )
        inserted = self.store.enqueue(outbound, direction="outbound")
        return RelayResult("queued" if inserted else "duplicate", outbound=outbound, policy=policy)

    def confirm_task_proposal(
        self, principal: Principal, proposal: RelayEnvelope, *, confirmed_at: str
    ) -> RelayResult:
        if proposal.kind != "task.create.proposal":
            raise RelayRoutingError("expected a task proposal")
        if proposal.sender != principal:
            raise PermissionError("only the proposing principal may confirm")
        proposal_id = str(proposal.payload["proposal_id"])
        stored = self.store.proposal_envelope(proposal_id)
        if stored is None or stored.to_dict() != proposal.to_dict():
            raise RelayRoutingError("proposal does not match the stored confirmation target")
        confirmation = self._envelope(
            principal,
            proposal.binding,
            "task.create.confirm",
            {"proposal_id": proposal_id, "confirmed": True},
            None,
        )
        if not self.store.confirm_proposal(confirmation, confirmed_at=confirmed_at):
            raise RelayRoutingError("proposal expired or was already confirmed")
        self.store.enqueue(confirmation, direction="outbound")
        return RelayResult("queued", outbound=confirmation)

    def answer_decision(
        self,
        principal: Principal,
        decision_message_id: str,
        answer: str,
        *,
        event_id: str | None = None,
    ) -> RelayResult:
        return self.handle_text(
            principal, answer, quote_message_id=decision_message_id, event_id=event_id
        )

    def request_snapshot(
        self, principal: Principal, *, event_id: str | None = None
    ) -> RelayResult:
        current = self.store.current_session(principal.platform, principal.actor_id)
        if current is None:
            raise RelayRoutingError("no current session; select a task first")
        if not self.policy.can_access(principal, current.instance_id):
            raise PermissionError("principal cannot access this instance")
        outbound = self._envelope(
            principal,
            self._binding(current, current.last_turn_id),
            "assistant.snapshot",
            {"assistant_text": "", "redaction_count": 0, "truncated": False},
            event_id,
        )
        inserted = self.store.enqueue(outbound, direction="outbound")
        return RelayResult("queued" if inserted else "duplicate", outbound=outbound)

    @staticmethod
    def _binding(session: RelaySession, turn_id: str) -> SessionBinding:
        return SessionBinding(
            session.instance_id, session.task_id, session.run_id, session.session_id,
            turn_id, session.session_version,
        )

    @staticmethod
    def _envelope(
        principal: Principal,
        binding: SessionBinding,
        kind: str,
        payload: dict[str, object],
        event_id: str | None,
    ) -> RelayEnvelope:
        now = datetime.now(timezone.utc).replace(microsecond=0)
        expires = now + timedelta(minutes=10)
        return RelayEnvelope.from_dict(
            {
                "schema_version": 1,
                "message_id": f"relay_{event_id or uuid4().hex}",
                "binding": binding.to_dict(),
                "sequence": 1,
                "created_at": now.isoformat().replace("+00:00", "Z"),
                "expires_at": expires.isoformat().replace("+00:00", "Z"),
                "sender": principal.to_dict(),
                "kind": kind,
                "payload": payload,
                "correlation_id": event_id,
                "causation_id": None,
            }
        )
