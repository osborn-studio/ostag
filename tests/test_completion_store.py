from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from ostag.core.completion import (
    ActorRef,
    CompletionAssessment,
    CompletionContract,
    CompletionEvidenceFact,
    CompletionGateResult,
    CompletionWaiver,
    EvidenceClaim,
    EvidenceProvenance,
    EvidenceSubject,
    MaterialActionGate,
)
from ostag.core.schemas import SchemaError
from ostag.work_monitor.completion_store import CompletionStore
from ostag.work_monitor.store import MIGRATIONS, MonitorStore

NOW = "2026-07-26T10:00:00+00:00"
DIGEST_A = "sha256:" + "aa" * 32
DIGEST_B = "sha256:" + "bb" * 32


def _store(tmp_path: Path) -> CompletionStore:
    return CompletionStore(MonitorStore(tmp_path / "monitor.db"))


def _contract(work_thread: str = "macmini/task_1") -> CompletionContract:
    return CompletionContract(
        id=f"contract_{work_thread.replace('/', '_')}",
        version=1,
        work_thread_id=work_thread,
        cycle=1,
        mode="execution_compat",
        gates=(MaterialActionGate(id="execution", action_family="executor.run"),),
        max_automatic_retries=0,
        on_satisfied="report_only",
        created_at=NOW,
    )


def _assessment(
    *,
    work_thread: str = "macmini/task_1",
    sequence: int = 1,
    digest: str = DIGEST_A,
    supersedes: str = "",
    state: str = "satisfied",
) -> CompletionAssessment:
    return CompletionAssessment(
        id=f"assessment_{work_thread.replace('/', '_')}_{sequence}",
        work_thread_id=work_thread,
        contract_id=f"contract_{work_thread.replace('/', '_')}",
        contract_version=1,
        cycle=1,
        sequence=sequence,
        input_digest=digest,
        target_bindings=(),
        state=state,
        evidence_backed=False,
        gate_results=(
            CompletionGateResult(
                gate_id="execution",
                state="passed",
                evidence_ids=("run_1",),
                reason_code="execution_succeeded",
                reason="ok",
                evaluated_at=NOW,
            ),
        ),
        assessed_at=NOW,
        assessed_by="ostag",
        supersedes_assessment_id=supersedes,
    )


def test_migration_v21_is_additive_from_pre_upgrade_fixture(tmp_path: Path) -> None:
    """A database created at the previous schema version opens and keeps its data."""
    db_path = tmp_path / "monitor.db"
    previous_version = len(MIGRATIONS) - 1
    connection = sqlite3.connect(db_path)
    try:
        for migration in MIGRATIONS[:previous_version]:
            for statement in migration.statements:
                try:
                    connection.execute(statement)
                except sqlite3.OperationalError:
                    pass  # duplicate-column idempotency mirrors MonitorStore._migrate
        connection.execute(
            "INSERT INTO tasks(instance_id,task_id,title,project,status,agent,progress,"
            "duration_seconds,summary,occurred_at) VALUES('macmini','t1','旧任务','p',"
            "'done','codex',100,5,'legacy row','2026-07-01T00:00:00+00:00')"
        )
        connection.execute(f"PRAGMA user_version = {previous_version}")
        connection.commit()
    finally:
        connection.close()

    store = MonitorStore(db_path)  # triggers migration to v21
    with store.connect() as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        assert version == len(MIGRATIONS)
        row = conn.execute(
            "SELECT title FROM tasks WHERE instance_id='macmini' AND task_id='t1'"
        ).fetchone()
        assert row["title"] == "旧任务"
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'completion_%'"
            )
        }
    assert tables == {
        "completion_contracts",
        "completion_evidence",
        "completion_waivers",
        "completion_assessments",
        "completion_threads",
    }


def test_contract_insert_or_verify_immutable(tmp_path: Path) -> None:
    store = _store(tmp_path)
    contract = _contract()
    assert store.record_contract("macmini", contract) is True
    assert store.record_contract("macmini", contract) is False  # idempotent replay
    mutated = CompletionContract.from_dict({**contract.to_dict(), "createdAt": "2027-01-01T00:00:00+00:00"})
    with pytest.raises(SchemaError) as excinfo:
        store.record_contract("macmini", mutated)
    assert excinfo.value.code == "immutable_record"
    assert store.get_contract("macmini", contract.id).work_thread_id == "macmini/task_1"


def test_evidence_insert_or_verify_immutable(tmp_path: Path) -> None:
    store = _store(tmp_path)
    fact = CompletionEvidenceFact(
        id="ev_1",
        work_thread_id="macmini/task_1",
        cycle=1,
        kind="ci.tests",
        assurance="verified",
        subject=EvidenceSubject(provider="github", resource_ref="pr/1", resource_version="v1"),
        claim=EvidenceClaim(predicate="verification", outcome="passed"),
        provenance=EvidenceProvenance(adapter="github", adapter_version="1", payload_digest="sha256:x"),
        observed_at=NOW,
        received_at=NOW,
    )
    assert store.record_evidence("macmini", fact) is True
    assert store.record_evidence("macmini", fact) is False
    tampered = CompletionEvidenceFact.from_dict({**fact.to_dict(), "assurance": "reported"})
    with pytest.raises(SchemaError):
        store.record_evidence("macmini", tampered)
    assert len(store.list_evidence("macmini", "macmini/task_1", 1)) == 1


def test_waiver_round_trip(tmp_path: Path) -> None:
    store = _store(tmp_path)
    waiver = CompletionWaiver(
        id="waiver_1",
        contract_id="contract_macmini_task_1",
        contract_version=1,
        cycle=1,
        actor=ActorRef(actor_id="owner", handle="@owner"),
        reason="pilot",
        policy_scope="workspace",
        gate_ids=("execution",),
        waived_at=NOW,
        run_id="run_1",
        expires_at="2026-07-27T00:00:00+00:00",
    )
    assert store.record_waiver("macmini", waiver) is True
    assert store.record_waiver("macmini", waiver) is False
    stored = store.list_waivers("macmini", waiver.contract_id, 1, 1)
    assert stored == (waiver,)


def test_append_recorded_then_duplicate_then_conflict(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.record_contract("macmini", _contract())

    first = store.append_assessment("macmini", _assessment(), None)
    assert first.outcome == "recorded"
    assert store.current_assessment("macmini", "macmini/task_1").sequence == 1

    replay = store.append_assessment("macmini", _assessment(), None)
    assert replay.outcome == "duplicate"

    # stale expected head -> conflict, nothing written
    stale = store.append_assessment(
        "macmini",
        _assessment(sequence=2, digest=DIGEST_B, supersedes=""),
        None,
    )
    assert stale.outcome == "conflict"
    assert stale.current_assessment is not None
    assert stale.current_assessment.sequence == 1

    head = store.current_assessment("macmini", "macmini/task_1")
    second = store.append_assessment(
        "macmini",
        _assessment(sequence=2, digest=DIGEST_B, supersedes=head.id),
        head.id,
    )
    assert second.outcome == "recorded"
    assert store.current_assessment("macmini", "macmini/task_1").sequence == 2


def test_append_enforces_immutability_supersession_and_sequence(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.record_contract("macmini", _contract())
    store.append_assessment("macmini", _assessment(), None)
    head = store.current_assessment("macmini", "macmini/task_1")

    # same id, different content -> immutability error
    tampered = CompletionAssessment.from_dict(
        {**_assessment().to_dict(), "state": "unsatisfied"}
    )
    with pytest.raises(SchemaError) as excinfo:
        store.append_assessment("macmini", tampered, head.id)
    assert excinfo.value.code == "immutable_record"

    # same input digest, different id/content -> immutability error
    same_digest = _assessment(sequence=2, digest=DIGEST_A, supersedes=head.id)
    with pytest.raises(SchemaError):
        store.append_assessment("macmini", same_digest, head.id)

    # supersession must match expected head
    with pytest.raises(SchemaError) as excinfo:
        store.append_assessment(
            "macmini", _assessment(sequence=2, digest=DIGEST_B, supersedes="wrong"), head.id
        )
    assert excinfo.value.code == "invalid_supersession"

    # sequence must be exactly max+1
    with pytest.raises(SchemaError) as excinfo:
        store.append_assessment(
            "macmini", _assessment(sequence=5, digest=DIGEST_B, supersedes=head.id), head.id
        )
    assert excinfo.value.code == "invalid_sequence"


def test_append_requires_existing_thread(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(SchemaError) as excinfo:
        store.append_assessment("macmini", _assessment(), None)
    assert excinfo.value.code == "not_found"


def test_instance_isolation_no_cross_instance_reads(tmp_path: Path) -> None:
    """Baseline decision 6.3: completion rows are never visible across instances."""
    store = _store(tmp_path)
    store.record_contract("macmini", _contract())
    store.append_assessment("macmini", _assessment(), None)

    # employee B's instance sees nothing from instance A
    assert store.current_assessment("macbook", "macmini/task_1") is None
    assert store.list_evidence("macbook", "macmini/task_1", 1) == ()
    with pytest.raises(SchemaError):
        store.get_contract("macbook", "contract_macmini_task_1")

    # and the same work-thread key under another instance is an independent thread
    store.record_contract("macbook", _contract())
    outcome = store.append_assessment("macbook", _assessment(), None)
    assert outcome.outcome == "recorded"
