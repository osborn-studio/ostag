"""Lifecycle store for structured human escalations (Phase 4).

Port of the upstream repository semantics (`amplifthq/opentag` @ ``cc1f4fb``,
``packages/store/src/repository.ts``): dedupe on open, attributed
transitions, idempotent replays, terminal immutability, optimistic
concurrency, deadline coercion (**a late acknowledgement or resolution
becomes ``expired`` — an expired escalation never authorizes**), and a
best-effort expiry sweep. Governance events land in the existing
``completion_governance_events`` table.
"""

from __future__ import annotations

import json

from ostag.core.completion import ActorRef
from ostag.core.access_snapshot import _require_utc_datetime
from ostag.core.escalation import (
    AUDIENCE_VIEWS,
    EscalationAcknowledgement,
    EscalationInput,
    EscalationResolution,
    HumanEscalation,
    _instant,
)
from ostag.core.identity import Principal
from ostag.core.safety import sanitize_user_text
from ostag.core.schemas import SchemaError
from ostag.work_monitor.identity import IdentityStore
from ostag.work_monitor.store import MonitorStore


def _clean(text: str, limit: int = 200) -> str:
    return " ".join(sanitize_user_text(text).split())[:limit]


def _replace(escalation: HumanEscalation, *, wire: dict) -> HumanEscalation:
    """Rebuild an escalation with wire-level field changes, revalidating all
    model invariants."""
    data = escalation.to_dict()
    data.update(wire)
    return HumanEscalation.from_dict(data)


class EscalationStore:
    def __init__(self, store: MonitorStore, identities: IdentityStore | None = None) -> None:
        self.store = store
        self.identities = identities

    # -- open -----------------------------------------------------------------

    def open(self, instance_id: str, escalation: HumanEscalation) -> tuple[HumanEscalation, bool]:
        """Insert a new escalation; dedupe on the active key. Returns
        (escalation, created). A live escalation holding the same dedupe slot
        is returned instead of creating a duplicate."""
        if escalation.state not in ("open", "acknowledged"):
            raise SchemaError(
                code="invalid_escalation",
                message="A new HumanEscalation must be open or acknowledged",
                recovery_hint="Open first; transition later.",
            )
        active_dedupe_key = (
            f"{escalation.run_id or 'thread'}:{escalation.dedupe_key}"
            if escalation.dedupe_key
            else None
        )
        payload = json.dumps(escalation.to_dict(), sort_keys=True, separators=(",", ":"))
        with self.store.transaction() as connection:
            thread = connection.execute(
                "SELECT 1 FROM completion_threads WHERE instance_id=? AND work_thread_key=?",
                (instance_id, escalation.work_thread_key),
            ).fetchone()
            if thread is None:
                raise SchemaError(
                    code="not_found",
                    message=f"Work thread {escalation.work_thread_key} does not exist "
                    f"for {instance_id}",
                    recovery_hint="Record completion governance for the thread first.",
                )
            if active_dedupe_key is not None:
                live = connection.execute(
                    "SELECT escalation_json FROM human_escalations WHERE instance_id=? "
                    "AND work_thread_key=? AND active_dedupe_key=?",
                    (instance_id, escalation.work_thread_key, active_dedupe_key),
                ).fetchone()
                if live is not None:
                    return HumanEscalation.from_dict(json.loads(live["escalation_json"])), False
            existing = connection.execute(
                "SELECT escalation_json FROM human_escalations "
                "WHERE instance_id=? AND escalation_id=?",
                (instance_id, escalation.id),
            ).fetchone()
            if existing is not None:
                correlated = HumanEscalation.from_dict(json.loads(existing["escalation_json"]))
                if (
                    correlated.work_thread_key != escalation.work_thread_key
                    or correlated.run_id != escalation.run_id
                    or correlated.dedupe_key != escalation.dedupe_key
                ):
                    raise SchemaError(
                        code="conflict",
                        message=f"HumanEscalation {escalation.id} exists with a "
                        "different identity",
                        recovery_hint="Use a new escalation id.",
                    )
                return correlated, False
            connection.execute(
                "INSERT INTO human_escalations(instance_id,escalation_id,work_thread_key,"
                "class,state,dedupe_key,active_dedupe_key,escalation_json,created_at,"
                "updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    instance_id,
                    escalation.id,
                    escalation.work_thread_key,
                    escalation.escalation_class,
                    escalation.state,
                    escalation.dedupe_key,
                    active_dedupe_key,
                    payload,
                    escalation.opened_at,
                    escalation.opened_at,
                ),
            )
            self._event(
                connection,
                instance_id,
                escalation.work_thread_key,
                "human_escalation.opened",
                escalation.id,
                {
                    "class": escalation.escalation_class,
                    "blocking": escalation.blocking,
                    "dedupeKey": escalation.dedupe_key or None,
                },
                escalation.opened_at,
            )
            return escalation, True

    # -- transitions ----------------------------------------------------------

    def transition(
        self,
        instance_id: str,
        escalation_id: str,
        to_state: str,
        *,
        at: str,
        actor: ActorRef | None = None,
        option_id: str = "",
        reason: str = "",
        superseded_by_id: str = "",
    ) -> tuple[HumanEscalation, bool]:
        """Attributed state transition with upstream race semantics.

        A late acknowledgement/resolution (at >= expiresAt) is coerced to
        ``expired`` — the deadline wins, and the rejected transition is
        recorded in the governance event. Identical replays return
        changed=False; conflicting replays and post-terminal transitions
        raise. The UPDATE is guarded on the observed state (optimistic
        concurrency).
        """
        if to_state not in ("acknowledged", "resolved", "expired", "superseded"):
            raise SchemaError(
                code="invalid_choice",
                message=f"Cannot transition a HumanEscalation to {to_state!r}",
                recovery_hint="Use acknowledged, resolved, expired, or superseded.",
            )
        _require_utc_datetime(at, "at", "EscalationTransition")
        with self.store.transaction() as connection:
            row = connection.execute(
                "SELECT escalation_json, active_dedupe_key FROM human_escalations "
                "WHERE instance_id=? AND escalation_id=?",
                (instance_id, escalation_id),
            ).fetchone()
            if row is None:
                raise SchemaError(
                    code="not_found",
                    message=f"HumanEscalation {escalation_id} does not exist for {instance_id}",
                    recovery_hint="Open the escalation before transitioning it.",
                )
            current = HumanEscalation.from_dict(json.loads(row["escalation_json"]))
            expired_due_to_deadline = bool(
                to_state in ("acknowledged", "resolved")
                and current.expires_at
                and _instant(at) >= _instant(current.expires_at)
            )
            effective_state = "expired" if expired_due_to_deadline else to_state

            if current.state == effective_state:
                self._verify_idempotent_replay(
                    current, effective_state, actor, option_id, reason, superseded_by_id
                )
                return current, False
            if current.terminal:
                raise SchemaError(
                    code="conflict",
                    message=f"HumanEscalation {escalation_id} is already terminal "
                    f"in state {current.state}",
                    recovery_hint="Open a new escalation instead.",
                )

            escalation = self._build_transition(
                connection,
                instance_id,
                current,
                effective_state,
                at=at,
                actor=actor,
                option_id=option_id,
                reason=reason,
                superseded_by_id=superseded_by_id,
                expired_due_to_deadline=expired_due_to_deadline,
            )
            swapped = connection.execute(
                "UPDATE human_escalations SET state=?, active_dedupe_key=?, "
                "escalation_json=?, updated_at=? WHERE instance_id=? AND escalation_id=? "
                "AND state=?",
                (
                    escalation.state,
                    row["active_dedupe_key"] if escalation.state == "acknowledged" else None,
                    json.dumps(escalation.to_dict(), sort_keys=True, separators=(",", ":")),
                    at,
                    instance_id,
                    escalation_id,
                    current.state,
                ),
            ).rowcount
            if swapped != 1:
                raise SchemaError(
                    code="conflict",
                    message=f"HumanEscalation {escalation_id} changed state concurrently",
                    recovery_hint="Re-read the escalation and retry.",
                )
            event_payload: dict = {"class": escalation.escalation_class}
            if actor is not None:
                event_payload["actor"] = actor.to_dict()
            if option_id:
                event_payload["optionId"] = option_id
            if reason:
                event_payload["reason"] = reason
            if expired_due_to_deadline:
                event_payload["rejectedLateTransition"] = to_state
            if superseded_by_id:
                event_payload["supersededById"] = superseded_by_id
            self._event(
                connection,
                instance_id,
                escalation.work_thread_key,
                f"human_escalation.{escalation.state}",
                escalation.id,
                event_payload,
                at,
            )
            return escalation, True

    def _build_transition(
        self,
        connection,
        instance_id: str,
        current: HumanEscalation,
        to_state: str,
        *,
        at: str,
        actor: ActorRef | None,
        option_id: str,
        reason: str,
        superseded_by_id: str,
        expired_due_to_deadline: bool,
    ) -> HumanEscalation:
        if to_state == "acknowledged":
            if actor is None:
                raise SchemaError(
                    code="missing_field",
                    message="Acknowledging a HumanEscalation requires an attributed actor",
                    recovery_hint="Provide the acknowledging actor.",
                )
            return _replace(
                current,
                wire={
                    "state": "acknowledged",
                    "acknowledgement": EscalationAcknowledgement(
                        actor=actor, acknowledged_at=at
                    ).to_dict(),
                },
            )
        if to_state == "resolved":
            if actor is None:
                raise SchemaError(
                    code="missing_field",
                    message="Resolving a HumanEscalation requires an attributed actor",
                    recovery_hint="Provide the resolving actor.",
                )
            if current.options and not option_id:
                raise SchemaError(
                    code="missing_field",
                    message="Resolving this HumanEscalation requires one offered optionId",
                    recovery_hint="Pick one of the offered options.",
                )
            return _replace(
                current,
                wire={
                    "state": "resolved",
                    "resolution": EscalationResolution(
                        actor=actor, resolved_at=at, option_id=option_id, reason=reason
                    ).to_dict(),
                },
            )
        if to_state == "expired":
            if not current.expires_at or _instant(at) < _instant(current.expires_at):
                raise SchemaError(
                    code="conflict",
                    message=f"HumanEscalation {current.id} has not reached expiresAt",
                    recovery_hint="Expiry is only valid at or after the deadline.",
                )
            terminal_reason = (
                "Escalation expired without implicit approval."
                if expired_due_to_deadline
                else (reason or "Escalation expired without implicit approval.")
            )
            return _replace(
                current, wire={"state": "expired", "terminalReason": terminal_reason}
            )
        # superseded
        if not superseded_by_id or superseded_by_id == current.id:
            raise SchemaError(
                code="missing_field",
                message="Superseding a HumanEscalation requires a different supersededById",
                recovery_hint="Open the successor first, then supersede.",
            )
        successor_row = connection.execute(
            "SELECT escalation_json FROM human_escalations "
            "WHERE instance_id=? AND escalation_id=?",
            (instance_id, superseded_by_id),
        ).fetchone()
        successor = (
            HumanEscalation.from_dict(json.loads(successor_row["escalation_json"]))
            if successor_row is not None
            else None
        )
        if successor is None or successor.work_thread_key != current.work_thread_key:
            raise SchemaError(
                code="not_found",
                message="A HumanEscalation successor must exist in the same work thread",
                recovery_hint="Open the successor in the same thread first.",
            )
        return _replace(
            current,
            wire={
                "state": "superseded",
                "supersededById": successor.id,
                "terminalReason": reason or f"Superseded by {successor.id}.",
            },
        )

    @staticmethod
    def _verify_idempotent_replay(
        current: HumanEscalation,
        to_state: str,
        actor: ActorRef | None,
        option_id: str,
        reason: str,
        superseded_by_id: str,
    ) -> None:
        """Same-state replays are no-ops only when the attribution matches."""
        mismatch = SchemaError(
            code="conflict",
            message=f"HumanEscalation {current.id} already has different "
            f"{to_state} attribution",
            recovery_hint="Read the recorded escalation instead of replaying.",
        )
        if to_state == "acknowledged":
            if actor is None:
                raise SchemaError(
                    code="missing_field",
                    message="Acknowledging a HumanEscalation requires an attributed actor",
                    recovery_hint="Provide the acknowledging actor.",
                )
            recorded = current.acknowledgement
            if recorded is None or recorded.actor.to_dict() != actor.to_dict():
                raise mismatch
        elif to_state == "resolved":
            if actor is None:
                raise SchemaError(
                    code="missing_field",
                    message="Resolving a HumanEscalation requires an attributed actor",
                    recovery_hint="Provide the resolving actor.",
                )
            recorded = current.resolution
            if (
                recorded is None
                or recorded.actor.to_dict() != actor.to_dict()
                or recorded.option_id != option_id
                or recorded.reason != reason
            ):
                raise mismatch
        else:
            if reason and current.terminal_reason != reason:
                raise mismatch
            if (
                to_state == "superseded"
                and superseded_by_id
                and current.superseded_by_id != superseded_by_id
            ):
                raise mismatch

    # -- expiry sweep ---------------------------------------------------------

    def expire_sweep(
        self,
        instance_id: str,
        *,
        at: str,
        work_thread_key: str | None = None,
        run_id: str | None = None,
    ) -> tuple[int, int]:
        """Expire every overdue open/acknowledged escalation. Best-effort per
        candidate: one concurrent terminal decision must not stop the sweep.
        Returns (scanned, expired)."""
        _require_utc_datetime(at, "at", "EscalationExpireSweep")
        query = (
            "SELECT escalation_json FROM human_escalations WHERE instance_id=? "
            "AND state IN ('open','acknowledged')"
        )
        params: list = [instance_id]
        if work_thread_key is not None:
            query += " AND work_thread_key=?"
            params.append(work_thread_key)
        with self.store.connect() as connection:
            rows = connection.execute(query, params).fetchall()
        scanned = len(rows)
        expired = 0
        moment = _instant(at)
        for row in rows:
            escalation = HumanEscalation.from_dict(json.loads(row["escalation_json"]))
            if run_id is not None and escalation.run_id != run_id:
                continue
            if not escalation.expires_at or _instant(escalation.expires_at) > moment:
                continue
            try:
                _, changed = self.transition(
                    instance_id,
                    escalation.id,
                    "expired",
                    at=at,
                    reason="Escalation expired without implicit approval.",
                )
                if changed:
                    expired += 1
            except Exception:  # noqa: BLE001 - each candidate is independent (upstream
                pass  # isolates every failure; one bad row must not stop the sweep)
        return scanned, expired

    # -- reads ----------------------------------------------------------------

    def get(self, instance_id: str, escalation_id: str) -> HumanEscalation:
        """Internal, UNREDACTED read; not identity-gated. External consumers
        go through ``personal_views`` / ``admin_views`` (gated + sanitized)."""
        with self.store.connect() as connection:
            row = connection.execute(
                "SELECT escalation_json FROM human_escalations "
                "WHERE instance_id=? AND escalation_id=?",
                (instance_id, escalation_id),
            ).fetchone()
        if row is None:
            raise SchemaError(
                code="not_found",
                message=f"HumanEscalation {escalation_id} not found for {instance_id}",
                recovery_hint="Check the escalation id.",
            )
        return HumanEscalation.from_dict(json.loads(row["escalation_json"]))

    def list_for_thread(
        self, instance_id: str, work_thread_key: str
    ) -> tuple[HumanEscalation, ...]:
        """Internal, UNREDACTED read; not identity-gated (see ``get``)."""
        with self.store.connect() as connection:
            rows = connection.execute(
                "SELECT escalation_json FROM human_escalations WHERE instance_id=? "
                "AND work_thread_key=? ORDER BY created_at, escalation_id",
                (instance_id, work_thread_key),
            ).fetchall()
        return tuple(
            HumanEscalation.from_dict(json.loads(row["escalation_json"])) for row in rows
        )

    def escalation_inputs(
        self, instance_id: str, work_thread_key: str
    ) -> tuple[EscalationInput, ...]:
        """The Phase 1 evaluator feed. Expired escalations map to an inactive
        state — they never block and never authorize."""
        return tuple(
            escalation.to_escalation_input()
            for escalation in self.list_for_thread(instance_id, work_thread_key)
        )

    # -- authorized views (handoff §8 audience mapping) ------------------------

    def personal_views(self, principal: Principal) -> tuple[dict, ...]:
        """Requester-facing escalations (audience requester/work_item_owner)
        for the principal's own instances only."""
        identities = self._require_identities()
        views: list[dict] = []
        for instance_id in sorted(identities.personal_instances(principal)):
            views.extend(
                self._render(instance_id, escalation)
                for escalation in self._all_for_instance(instance_id)
                if AUDIENCE_VIEWS[escalation.audience] == "personal"
            )
        return tuple(views)

    def admin_views(self, principal: Principal) -> tuple[dict, ...]:
        """All escalations across instances, including operator-facing
        audiences; requires a bound global owner."""
        identities = self._require_identities()
        if not identities.is_global_owner(principal):
            raise SchemaError(
                code="permission_denied",
                message="Team escalation views require a bound global owner",
                recovery_hint="Use the personal view, or bind an owner/admin principal.",
            )
        views: list[dict] = []
        for instance_id in sorted(identities.team_instances()):
            views.extend(
                self._render(instance_id, escalation)
                for escalation in self._all_for_instance(instance_id)
            )
        return tuple(views)

    def _require_identities(self) -> IdentityStore:
        if self.identities is None:
            raise SchemaError(
                code="permission_denied",
                message="Escalation views require an identity store",
                recovery_hint="Construct EscalationStore with an IdentityStore.",
            )
        return self.identities

    def _all_for_instance(self, instance_id: str) -> tuple[HumanEscalation, ...]:
        with self.store.connect() as connection:
            rows = connection.execute(
                "SELECT escalation_json FROM human_escalations WHERE instance_id=? "
                "ORDER BY created_at, escalation_id",
                (instance_id,),
            ).fetchall()
        return tuple(
            HumanEscalation.from_dict(json.loads(row["escalation_json"])) for row in rows
        )

    @staticmethod
    def _render(instance_id: str, escalation: HumanEscalation) -> dict:
        """Sanitized, single-line-safe projection for boards/messages."""
        rendered = {
            "instanceId": _clean(instance_id, 80),
            "id": _clean(escalation.id, 80),
            "workThreadKey": _clean(escalation.work_thread_key, 120),
            "class": escalation.escalation_class,
            "audience": escalation.audience,
            "state": escalation.state,
            "blocking": escalation.blocking,
            "summary": _clean(escalation.summary),
            "reason": _clean(escalation.reason),
            "openedAt": escalation.opened_at,
            "expiresAt": escalation.expires_at,
            "options": [
                {
                    "id": _clean(option.id, 60),
                    "label": _clean(option.label, 120),
                    "consequence": _clean(option.consequence),
                }
                for option in escalation.options
            ],
        }
        if escalation.terminal_reason:
            rendered["terminalReason"] = _clean(escalation.terminal_reason)
        return rendered

    # -- internals ------------------------------------------------------------

    @staticmethod
    def _event(
        connection,
        instance_id: str,
        work_thread_key: str,
        event_type: str,
        subject_id: str,
        payload: dict,
        created_at: str,
    ) -> None:
        connection.execute(
            "INSERT INTO completion_governance_events(instance_id,work_thread_key,"
            "event_type,subject_id,payload_json,created_at) VALUES(?,?,?,?,?,?)",
            (
                instance_id,
                work_thread_key,
                event_type,
                subject_id,
                json.dumps(payload, sort_keys=True, separators=(",", ":")),
                created_at,
            ),
        )
