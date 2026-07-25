"""SQLite persistence for completion governance (OpenTag v0.7 back-port).

Implements the upstream append semantics from
``packages/store/src/repository.ts::appendCompletionAssessment`` at pinned
commit ``758fb5a6``: duplicate-by-id and duplicate-by-input-digest checks with
immutability enforcement, work-thread head compare-and-swap with
``recorded | duplicate | conflict`` outcomes, supersession/sequence
invariants — all inside one ``BEGIN IMMEDIATE`` transaction on the existing
:class:`ostag.work_monitor.store.MonitorStore` database.

Every row carries ``instance_id`` so scoped reads join through the existing
personal/admin authorization boundary (Phase 0 baseline decision 6.3); no
completion record is queryable by task id alone.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from ostag.core.completion import (
    CompletionAssessment,
    CompletionContract,
    CompletionEvidenceFact,
    CompletionWaiver,
)
from ostag.core.schemas import SchemaError
from ostag.work_monitor.store import MonitorStore


@dataclass(frozen=True)
class AppendOutcome:
    outcome: str  # "recorded" | "duplicate" | "conflict"
    assessment: CompletionAssessment | None
    current_assessment: CompletionAssessment | None = None


class CompletionStore:
    """Owns completion-governance rows on the monitor database."""

    def __init__(self, store: MonitorStore) -> None:
        self.store = store

    # -- contracts ------------------------------------------------------------

    def record_contract(self, instance_id: str, contract: CompletionContract) -> bool:
        """Insert-or-verify an immutable contract row. Returns True when created."""
        payload = json.dumps(contract.to_dict(), sort_keys=True, separators=(",", ":"))
        with self.store.transaction() as connection:
            row = connection.execute(
                "SELECT contract_json FROM completion_contracts "
                "WHERE instance_id=? AND contract_id=? AND contract_version=?",
                (instance_id, contract.id, contract.version),
            ).fetchone()
            if row is not None:
                if row["contract_json"] != payload:
                    raise SchemaError(
                        code="immutable_record",
                        message=(
                            f"CompletionContract {contract.id} v{contract.version} is immutable"
                        ),
                        recovery_hint="Create a new contract version instead of editing.",
                    )
                return False
            connection.execute(
                "INSERT INTO completion_contracts(instance_id,contract_id,contract_version,"
                "work_thread_key,cycle,mode,contract_json,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    instance_id,
                    contract.id,
                    contract.version,
                    contract.work_thread_id,
                    contract.cycle,
                    contract.mode,
                    payload,
                    contract.created_at,
                ),
            )
            connection.execute(
                "INSERT OR IGNORE INTO completion_threads(instance_id,work_thread_key,"
                "current_assessment_id,created_at,updated_at) VALUES(?,?,'',?,?)",
                (instance_id, contract.work_thread_id, contract.created_at, contract.created_at),
            )
            return True

    def get_contract(
        self, instance_id: str, contract_id: str, contract_version: int
    ) -> CompletionContract:
        with self.store.connect() as connection:
            row = connection.execute(
                "SELECT contract_json FROM completion_contracts "
                "WHERE instance_id=? AND contract_id=? AND contract_version=?",
                (instance_id, contract_id, contract_version),
            ).fetchone()
        if row is None:
            raise SchemaError(
                code="not_found",
                message=(
                    f"CompletionContract {contract_id} v{contract_version} not found "
                    f"for {instance_id}"
                ),
                recovery_hint="Record the contract before evaluating it.",
            )
        return CompletionContract.from_dict(json.loads(row["contract_json"]))

    # -- evidence -------------------------------------------------------------

    def record_evidence(self, instance_id: str, fact: CompletionEvidenceFact) -> bool:
        """Insert-or-verify an immutable evidence fact. Returns True when created."""
        payload = json.dumps(fact.to_dict(), sort_keys=True, separators=(",", ":"))
        with self.store.transaction() as connection:
            row = connection.execute(
                "SELECT evidence_json FROM completion_evidence "
                "WHERE instance_id=? AND evidence_id=?",
                (instance_id, fact.id),
            ).fetchone()
            if row is not None:
                if row["evidence_json"] != payload:
                    raise SchemaError(
                        code="immutable_record",
                        message=f"CompletionEvidenceFact {fact.id} is immutable",
                        recovery_hint="Record a new fact id instead of editing.",
                    )
                return False
            connection.execute(
                "INSERT INTO completion_evidence(instance_id,evidence_id,work_thread_key,"
                "cycle,evidence_json,observed_at,received_at) VALUES(?,?,?,?,?,?,?)",
                (
                    instance_id,
                    fact.id,
                    fact.work_thread_id,
                    fact.cycle,
                    payload,
                    fact.observed_at,
                    fact.received_at,
                ),
            )
            return True

    def list_evidence(
        self, instance_id: str, work_thread_key: str, cycle: int
    ) -> tuple[CompletionEvidenceFact, ...]:
        with self.store.connect() as connection:
            rows = connection.execute(
                "SELECT evidence_json FROM completion_evidence WHERE instance_id=? "
                "AND work_thread_key=? AND cycle=? ORDER BY evidence_id",
                (instance_id, work_thread_key, cycle),
            ).fetchall()
        return tuple(
            CompletionEvidenceFact.from_dict(json.loads(row["evidence_json"])) for row in rows
        )

    # -- waivers --------------------------------------------------------------

    def record_waiver(self, instance_id: str, waiver: CompletionWaiver) -> bool:
        """Insert-or-verify an immutable waiver bound to an existing contract snapshot."""
        with self.store.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM completion_waivers WHERE instance_id=? AND waiver_id=?",
                (instance_id, waiver.id),
            ).fetchone()
            if row is not None:
                same = (
                    row["contract_id"] == waiver.contract_id
                    and row["contract_version"] == waiver.contract_version
                    and row["cycle"] == waiver.cycle
                    and row["run_id"] == waiver.run_id
                    and row["actor_id"] == waiver.actor.actor_id
                    and row["actor_handle"] == waiver.actor.handle
                    and row["reason"] == waiver.reason
                    and row["scope"] == waiver.scope
                    and row["policy_scope"] == waiver.policy_scope
                    and tuple(json.loads(row["gate_ids_json"])) == waiver.gate_ids
                    and row["waived_at"] == waiver.waived_at
                    and row["expires_at"] == waiver.expires_at
                )
                if not same:
                    raise SchemaError(
                        code="immutable_record",
                        message=f"CompletionWaiver {waiver.id} is immutable",
                        recovery_hint="Issue a new waiver id instead of editing one.",
                    )
                return False
            contract_row = connection.execute(
                "SELECT cycle FROM completion_contracts WHERE instance_id=? "
                "AND contract_id=? AND contract_version=?",
                (instance_id, waiver.contract_id, waiver.contract_version),
            ).fetchone()
            if contract_row is None:
                raise SchemaError(
                    code="not_found",
                    message=(
                        f"CompletionWaiver {waiver.id} references unknown contract "
                        f"{waiver.contract_id} v{waiver.contract_version}"
                    ),
                    recovery_hint="Record the contract snapshot before waiving its gates.",
                )
            if int(contract_row["cycle"]) != waiver.cycle:
                raise SchemaError(
                    code="invalid_reference",
                    message=(
                        f"CompletionWaiver {waiver.id} cycle {waiver.cycle} does not match "
                        f"contract cycle {contract_row['cycle']}"
                    ),
                    recovery_hint="Issue the waiver for the contract's current cycle.",
                )
            connection.execute(
                "INSERT INTO completion_waivers(instance_id,waiver_id,contract_id,"
                "contract_version,cycle,run_id,actor_id,actor_handle,reason,scope,"
                "policy_scope,gate_ids_json,waived_at,expires_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    instance_id,
                    waiver.id,
                    waiver.contract_id,
                    waiver.contract_version,
                    waiver.cycle,
                    waiver.run_id,
                    waiver.actor.actor_id,
                    waiver.actor.handle,
                    waiver.reason,
                    waiver.scope,
                    waiver.policy_scope,
                    json.dumps(list(waiver.gate_ids)),
                    waiver.waived_at,
                    waiver.expires_at,
                ),
            )
            return True

    def list_waivers(
        self, instance_id: str, contract_id: str, contract_version: int, cycle: int
    ) -> tuple[CompletionWaiver, ...]:
        from ostag.core.completion import ActorRef

        with self.store.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM completion_waivers WHERE instance_id=? AND contract_id=? "
                "AND contract_version=? AND cycle=? ORDER BY waiver_id",
                (instance_id, contract_id, contract_version, cycle),
            ).fetchall()
        return tuple(
            CompletionWaiver(
                id=row["waiver_id"],
                contract_id=row["contract_id"],
                contract_version=row["contract_version"],
                cycle=row["cycle"],
                actor=ActorRef(actor_id=row["actor_id"], handle=row["actor_handle"]),
                reason=row["reason"],
                policy_scope=row["policy_scope"],
                gate_ids=tuple(json.loads(row["gate_ids_json"])),
                waived_at=row["waived_at"],
                run_id=row["run_id"],
                scope=row["scope"],
                expires_at=row["expires_at"],
            )
            for row in rows
        )

    # -- assessments ----------------------------------------------------------

    def append_assessment(
        self,
        instance_id: str,
        assessment: CompletionAssessment,
        expected_current_assessment_id: str | None,
    ) -> AppendOutcome:
        """Upstream ``appendCompletionAssessment`` semantics, transactionally."""
        payload = json.dumps(assessment.to_dict(), sort_keys=True, separators=(",", ":"))
        with self.store.transaction() as connection:
            duplicate_by_id = connection.execute(
                "SELECT assessment_json FROM completion_assessments "
                "WHERE instance_id=? AND assessment_id=?",
                (instance_id, assessment.id),
            ).fetchone()
            if duplicate_by_id is not None:
                if duplicate_by_id["assessment_json"] != payload:
                    raise SchemaError(
                        code="immutable_record",
                        message=f"CompletionAssessment {assessment.id} is immutable",
                        recovery_hint="Append a new assessment; never rewrite one.",
                    )
                return AppendOutcome(
                    outcome="duplicate",
                    assessment=CompletionAssessment.from_dict(
                        json.loads(duplicate_by_id["assessment_json"])
                    ),
                )
            duplicate_by_input = connection.execute(
                "SELECT assessment_json FROM completion_assessments WHERE instance_id=? "
                "AND work_thread_key=? AND cycle=? AND input_digest=?",
                (
                    instance_id,
                    assessment.work_thread_id,
                    assessment.cycle,
                    assessment.input_digest,
                ),
            ).fetchone()
            if duplicate_by_input is not None:
                if duplicate_by_input["assessment_json"] != payload:
                    raise SchemaError(
                        code="immutable_record",
                        message=(
                            f"CompletionAssessment input digest {assessment.input_digest} "
                            "is immutable"
                        ),
                        recovery_hint="Identical inputs must map to identical assessments.",
                    )
                return AppendOutcome(
                    outcome="duplicate",
                    assessment=CompletionAssessment.from_dict(
                        json.loads(duplicate_by_input["assessment_json"])
                    ),
                )
            thread = connection.execute(
                "SELECT current_assessment_id FROM completion_threads "
                "WHERE instance_id=? AND work_thread_key=?",
                (instance_id, assessment.work_thread_id),
            ).fetchone()
            if thread is None:
                raise SchemaError(
                    code="not_found",
                    message=f"Completion thread {assessment.work_thread_id} does not exist",
                    recovery_hint="Record the contract first (it seeds the thread head).",
                )
            head = thread["current_assessment_id"] or None
            expected = expected_current_assessment_id or None
            if head != expected:
                current = None
                if head:
                    current_row = connection.execute(
                        "SELECT assessment_json FROM completion_assessments "
                        "WHERE instance_id=? AND assessment_id=?",
                        (instance_id, head),
                    ).fetchone()
                    if current_row is not None:
                        current = CompletionAssessment.from_dict(
                            json.loads(current_row["assessment_json"])
                        )
                return AppendOutcome(
                    outcome="conflict", assessment=None, current_assessment=current
                )
            if (assessment.supersedes_assessment_id or None) != expected:
                raise SchemaError(
                    code="invalid_supersession",
                    message=(
                        "CompletionAssessment supersession must match the expected "
                        "work-thread assessment head"
                    ),
                    recovery_hint="Set supersedesAssessmentId to the current head id.",
                )
            previous = connection.execute(
                "SELECT MAX(sequence) AS max_sequence FROM completion_assessments "
                "WHERE instance_id=? AND work_thread_key=? AND cycle=?",
                (instance_id, assessment.work_thread_id, assessment.cycle),
            ).fetchone()
            expected_sequence = int(previous["max_sequence"] or 0) + 1
            if assessment.sequence != expected_sequence:
                raise SchemaError(
                    code="invalid_sequence",
                    message=(
                        f"CompletionAssessment sequence must be {expected_sequence} "
                        f"for cycle {assessment.cycle}"
                    ),
                    recovery_hint="Re-evaluate with lineage.sequence set to the next value.",
                )
            connection.execute(
                "INSERT INTO completion_assessments(instance_id,assessment_id,"
                "work_thread_key,contract_id,contract_version,cycle,sequence,"
                "supersedes_assessment_id,input_digest,state,assessment_json,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    instance_id,
                    assessment.id,
                    assessment.work_thread_id,
                    assessment.contract_id,
                    assessment.contract_version,
                    assessment.cycle,
                    assessment.sequence,
                    assessment.supersedes_assessment_id,
                    assessment.input_digest,
                    assessment.state,
                    payload,
                    assessment.assessed_at,
                ),
            )
            previous_current_state = ""
            if head:
                previous_row = connection.execute(
                    "SELECT state FROM completion_assessments "
                    "WHERE instance_id=? AND assessment_id=?",
                    (instance_id, head),
                ).fetchone()
                if previous_row is not None:
                    previous_current_state = previous_row["state"]
            updated = connection.execute(
                "UPDATE completion_threads SET current_assessment_id=?,updated_at=? "
                "WHERE instance_id=? AND work_thread_key=? AND current_assessment_id=?",
                (
                    assessment.id,
                    assessment.assessed_at,
                    instance_id,
                    assessment.work_thread_id,
                    expected_current_assessment_id or "",
                ),
            )
            if updated.rowcount != 1:
                raise SchemaError(
                    code="conflict",
                    message="CompletionAssessment head compare-and-swap failed",
                    recovery_hint="Re-read the thread head and retry the append.",
                )
            # Upstream steps 8-9: governance events inside the same transaction.
            connection.execute(
                "INSERT INTO completion_governance_events(instance_id,work_thread_key,"
                "event_type,subject_id,payload_json,created_at) VALUES(?,?,?,?,?,?)",
                (
                    instance_id,
                    assessment.work_thread_id,
                    "completion_assessment.appended",
                    assessment.id,
                    json.dumps(
                        {
                            "contractId": assessment.contract_id,
                            "contractVersion": assessment.contract_version,
                            "cycle": assessment.cycle,
                            "sequence": assessment.sequence,
                            "state": assessment.state,
                            "inputDigest": assessment.input_digest,
                            "supersedesAssessmentId": assessment.supersedes_assessment_id
                            or None,
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    assessment.assessed_at,
                ),
            )
            accepted = assessment.state in ("satisfied", "waived")
            was_accepted = previous_current_state in ("satisfied", "waived")
            if accepted and not was_accepted and assessment.accepted_at:
                thread_created_row = connection.execute(
                    "SELECT created_at FROM completion_threads "
                    "WHERE instance_id=? AND work_thread_key=?",
                    (instance_id, assessment.work_thread_id),
                ).fetchone()
                elapsed_ms = 0
                if thread_created_row is not None:
                    from datetime import datetime

                    try:
                        accepted_at = datetime.fromisoformat(
                            assessment.accepted_at.replace("Z", "+00:00")
                        )
                        created_at = datetime.fromisoformat(
                            str(thread_created_row["created_at"]).replace("Z", "+00:00")
                        )
                        elapsed_ms = max(
                            0, int((accepted_at - created_at).total_seconds() * 1000)
                        )
                    except ValueError:
                        elapsed_ms = 0
                connection.execute(
                    "INSERT INTO completion_governance_events(instance_id,work_thread_key,"
                    "event_type,subject_id,payload_json,created_at) VALUES(?,?,?,?,?,?)",
                    (
                        instance_id,
                        assessment.work_thread_id,
                        "success_metric.observed",
                        assessment.id,
                        json.dumps(
                            {
                                "metric": "time_to_verified_completion_ms",
                                "value": elapsed_ms,
                                "acceptedAt": assessment.accepted_at,
                                "contractId": assessment.contract_id,
                                "contractVersion": assessment.contract_version,
                                "cycle": assessment.cycle,
                                "state": assessment.state,
                                "evidenceBacked": assessment.evidence_backed,
                            },
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        assessment.accepted_at,
                    ),
                )
            return AppendOutcome(outcome="recorded", assessment=assessment)

    def current_assessment(
        self, instance_id: str, work_thread_key: str
    ) -> CompletionAssessment | None:
        with self.store.connect() as connection:
            thread = connection.execute(
                "SELECT current_assessment_id FROM completion_threads "
                "WHERE instance_id=? AND work_thread_key=?",
                (instance_id, work_thread_key),
            ).fetchone()
            if thread is None or not thread["current_assessment_id"]:
                return None
            row = connection.execute(
                "SELECT assessment_json FROM completion_assessments "
                "WHERE instance_id=? AND assessment_id=?",
                (instance_id, thread["current_assessment_id"]),
            ).fetchone()
        if row is None:
            return None
        return CompletionAssessment.from_dict(json.loads(row["assessment_json"]))
