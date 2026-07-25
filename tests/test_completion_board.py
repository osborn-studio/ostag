from __future__ import annotations

import json
from pathlib import Path

import pytest

from ostag.core.completion import (
    ActorRef,
    CompletionAssessment,
    CompletionContract,
    CompletionGateResult,
    CompletionWaiver,
    MaterialActionGate,
)
from ostag.core.identity import Principal
from ostag.core.schemas import SchemaError
from ostag.work_monitor.completion_board import (
    CompletionBoardService,
    render_completion_board_text,
)
from ostag.work_monitor.completion_store import CompletionStore
from ostag.work_monitor.identity import IdentityStore
from ostag.work_monitor.store import MonitorStore

NOW = "2026-07-26T10:00:00+00:00"
LATER = "2026-07-27T00:00:00+00:00"
DIGEST = "sha256:" + "cd" * 32

ALICE = Principal(platform="feishu", actor_id="alice")
BOB = Principal(platform="feishu", actor_id="bob")
OWNER = Principal(platform="feishu", actor_id="owner")


@pytest.fixture()
def env(tmp_path: Path):
    monitor = MonitorStore(tmp_path / "monitor.db")
    completion = CompletionStore(monitor)
    identities = IdentityStore(tmp_path / "identity.sqlite3")
    # alice owns macbook, bob owns macmini; owner is a bound global owner
    identities.bind_instance(ALICE, "macbook", display_name="A", bound_at=NOW)
    identities.bind_instance(BOB, "macmini", display_name="B", bound_at=NOW)
    with identities._connect() as connection:  # seed a private owner binding
        connection.execute(
            "INSERT INTO monitor_bindings(platform,destination_id,chat_type,owner_platform,"
            "owner_actor_id,role,scope,revoked) VALUES('feishu','p2p_owner','private',"
            "'feishu','owner','owner','',0)"
        )
        connection.commit()
    return monitor, completion, identities


def _seed_thread(
    completion: CompletionStore,
    instance_id: str,
    task_id: str,
    *,
    state: str = "satisfied",
    gate_results=None,
    waiver: CompletionWaiver | None = None,
    evidence_backed: bool = False,
    assessed_by: str = "ostag",
) -> str:
    work_thread = f"{instance_id}/{task_id}"
    contract = CompletionContract(
        id=f"contract_{instance_id}_{task_id}",
        version=1,
        work_thread_id=work_thread,
        cycle=1,
        mode="execution_compat" if not evidence_backed else "governed",
        gates=(MaterialActionGate(id="execution", action_family="executor.run"),),
        max_automatic_retries=0,
        on_satisfied="report_only",
        created_at=NOW,
        target_selectors=(),
    )
    completion.record_contract(instance_id, contract)
    if waiver is not None:
        completion.record_waiver(instance_id, waiver)
    results = gate_results or (
        CompletionGateResult(
            gate_id="execution",
            state="passed",
            evidence_ids=("run_1",),
            reason_code="execution_succeeded",
            reason="ok",
            evaluated_at=NOW,
        ),
    )
    assessment = CompletionAssessment(
        id=f"assessment_{instance_id}_{task_id}_1",
        work_thread_id=work_thread,
        contract_id=contract.id,
        contract_version=1,
        cycle=1,
        sequence=1,
        input_digest=DIGEST,
        target_bindings=(),
        state=state,
        evidence_backed=evidence_backed,
        gate_results=results,
        assessed_at=NOW,
        assessed_by=assessed_by,
        waiver=waiver,
    )
    completion.append_assessment(instance_id, assessment, None)
    return work_thread


def _waiver(instance_id: str, task_id: str, *, expires_at: str = "") -> CompletionWaiver:
    return CompletionWaiver(
        id=f"waiver_{instance_id}_{task_id}",
        contract_id=f"contract_{instance_id}_{task_id}",
        contract_version=1,
        cycle=1,
        actor=ActorRef(actor_id="owner", handle="@owner"),
        reason="pilot exception",
        policy_scope="work_item_override",
        gate_ids=("execution",),
        waived_at=NOW,
        expires_at=expires_at,
    )


def test_personal_board_never_contains_other_instances(env) -> None:
    monitor, completion, identities = env
    _seed_thread(completion, "macbook", "t1")
    _seed_thread(completion, "macmini", "t2")
    service = CompletionBoardService(monitor, completion, identities)

    alice_rows = service.personal_rows(ALICE, now=NOW)
    assert [row.instance_id for row in alice_rows] == ["macbook"]
    # not even hidden JSON may leak the other employee's instance
    dumped = json.dumps([row.to_dict() for row in alice_rows])
    assert "macmini" not in dumped and "t2" not in dumped

    bob_rows = service.personal_rows(BOB, now=NOW)
    assert [row.instance_id for row in bob_rows] == ["macmini"]
    assert "macbook" not in json.dumps([row.to_dict() for row in bob_rows])


def test_admin_board_requires_global_owner(env) -> None:
    monitor, completion, identities = env
    _seed_thread(completion, "macbook", "t1")
    _seed_thread(completion, "macmini", "t2")
    service = CompletionBoardService(monitor, completion, identities)

    with pytest.raises(SchemaError) as excinfo:
        service.admin_rows(ALICE, now=NOW)
    assert excinfo.value.code == "permission_denied"

    rows = service.admin_rows(OWNER, now=NOW)
    assert sorted(row.instance_id for row in rows) == ["macbook", "macmini"]


def test_state_matrix_rows(env) -> None:
    monitor, completion, identities = env

    def gate(state: str, code: str, gate_id: str = "execution"):
        return CompletionGateResult(
            gate_id=gate_id,
            state=state,
            evidence_ids=(),
            reason_code=code,
            reason="r",
            evaluated_at=NOW,
        )

    _seed_thread(completion, "macmini", "sat")  # satisfied compat
    _seed_thread(
        completion,
        "macmini",
        "pend",
        state="pending",
        evidence_backed=True,
        gate_results=(gate("missing", "verification_missing", "tests"),),
    )
    _seed_thread(
        completion,
        "macmini",
        "unsat",
        state="unsatisfied",
        evidence_backed=True,
        gate_results=(gate("failed", "verification_failed", "tests"),),
    )
    _seed_thread(
        completion,
        "macmini",
        "blocked",
        state="blocked",
        evidence_backed=True,
        gate_results=(gate("unknown", "verification_assurance_insufficient", "tests"),),
    )
    service = CompletionBoardService(monitor, completion, identities)
    rows = {row.work_thread_key: row for row in service.personal_rows(BOB, now=NOW)}

    assert rows["macmini/sat"].completion == "satisfied"
    assert rows["macmini/sat"].agent_done is True
    assert rows["macmini/pend"].missing_gate_ids == ("tests",)
    assert "tests" in rows["macmini/pend"].next_action
    assert rows["macmini/unsat"].failed_gate_ids == ("tests",)
    assert rows["macmini/blocked"].blocked_gate_ids == ("tests",)
    text = render_completion_board_text(tuple(rows.values()))
    assert "测试已通过：—" in text
    assert "仍缺证据：缺失 tests" in text


def test_evidence_rows_reflect_gate_codes(env) -> None:
    monitor, completion, identities = env
    results = (
        CompletionGateResult(
            gate_id="tests",
            state="passed",
            evidence_ids=("ev_1",),
            reason_code="verification_passed",
            reason="ok",
            evaluated_at=NOW,
        ),
        CompletionGateResult(
            gate_id="deploy",
            state="passed",
            evidence_ids=("ev_2",),
            reason_code="external_state_satisfied",
            reason="ok",
            evaluated_at=NOW,
        ),
        CompletionGateResult(
            gate_id="accept",
            state="passed",
            evidence_ids=("ev_3",),
            reason_code="human_acceptance_recorded",
            reason="ok",
            evaluated_at=NOW,
        ),
    )
    _seed_thread(
        completion, "macmini", "full", state="satisfied", evidence_backed=True, gate_results=results
    )
    service = CompletionBoardService(monitor, completion, identities)
    row = service.personal_rows(BOB, now=NOW)[0]

    assert row.tests_passed and row.deploy_verified and row.prod_accepted
    text = render_completion_board_text((row,))
    assert "测试已通过：✅" in text
    assert "部署已验证：✅" in text
    assert "生产验收已通过：✅" in text
    assert "例外：—" in text


def test_waiver_badge_active_and_expired(env) -> None:
    monitor, completion, identities = env
    waiver = _waiver("macmini", "w1", expires_at=LATER)
    _seed_thread(
        completion,
        "macmini",
        "w1",
        state="waived",
        evidence_backed=True,
        assessed_by="human",
        waiver=waiver,
        gate_results=(
            CompletionGateResult(
                gate_id="execution",
                state="waived",
                evidence_ids=(),
                reason_code="gate_waived",
                reason="Gate waived by @owner.",
                evaluated_at=NOW,
            ),
        ),
    )
    service = CompletionBoardService(monitor, completion, identities)

    active_row = service.personal_rows(BOB, now=NOW)[0]
    assert active_row.waiver is not None and active_row.waiver.expired is False
    text = render_completion_board_text((active_row,))
    assert "@owner 豁免 execution" in text
    assert f"{LATER} 到期" in text

    expired_row = service.personal_rows(BOB, now="2026-07-28T00:00:00+00:00")[0]
    assert expired_row.waiver is not None and expired_row.waiver.expired is True
    assert "已于" in render_completion_board_text((expired_row,))


def test_external_text_is_sanitized(env) -> None:
    monitor, completion, identities = env
    hostile = "OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwxyz‮ evil"
    waiver = CompletionWaiver(
        id="waiver_hostile",
        contract_id="contract_macmini_h1",
        contract_version=1,
        cycle=1,
        actor=ActorRef(actor_id="owner", handle="@owner"),
        reason=hostile,
        policy_scope="work_item_override",
        gate_ids=("execution",),
        waived_at=NOW,
    )
    _seed_thread(
        completion,
        "macmini",
        "h1",
        state="waived",
        evidence_backed=True,
        assessed_by="human",
        waiver=waiver,
        gate_results=(
            CompletionGateResult(
                gate_id="execution",
                state="waived",
                evidence_ids=(),
                reason_code="gate_waived",
                reason="Gate waived by @owner.",
                evaluated_at=NOW,
            ),
        ),
    )
    service = CompletionBoardService(monitor, completion, identities)
    row = service.personal_rows(BOB, now=NOW)[0]

    assert "sk-abcdefghijklmnopqrstuvwxyz" not in row.waiver.reason
    assert "[REDACTED]" in row.waiver.reason
    assert "‮" not in row.waiver.reason
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in render_completion_board_text((row,))


def test_execution_status_comes_from_tasks_projection(env) -> None:
    monitor, completion, identities = env
    _seed_thread(completion, "macmini", "t9", state="pending", evidence_backed=True,
                 gate_results=(
                     CompletionGateResult(
                         gate_id="tests",
                         state="missing",
                         evidence_ids=(),
                         reason_code="verification_missing",
                         reason="r",
                         evaluated_at=NOW,
                     ),
                 ))
    with monitor.connect() as connection:
        connection.execute(
            "INSERT INTO tasks(instance_id,task_id,title,project,status,agent,progress,"
            "duration_seconds,summary,occurred_at,version) VALUES('macmini','t9','任务','p',"
            "'done','claude-code',100,5,'ok',?,1)",
            (NOW,),
        )
        connection.commit()
    service = CompletionBoardService(monitor, completion, identities)
    row = service.personal_rows(BOB, now=NOW)[0]

    # executor says done, governance says pending — the split the board exists for
    assert row.execution_status == "done"
    assert row.agent_done is True
    assert row.completion == "pending"
    text = render_completion_board_text((row,))
    assert "Agent 已完成：✅" in text
    assert "completion: pending" in text


def test_empty_board_renders_placeholder(env) -> None:
    monitor, completion, identities = env
    service = CompletionBoardService(monitor, completion, identities)
    assert render_completion_board_text(service.personal_rows(ALICE, now=NOW)) == "暂无受治理的完成记录。"
