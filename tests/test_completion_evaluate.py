from __future__ import annotations

from ostag.core.completion import (
    ActorRef,
    ArtifactTarget,
    CompletionArtifact,
    CompletionContract,
    CompletionEvaluationInput,
    CompletionEvidenceFact,
    CompletionRunResult,
    CompletionTargetSelector,
    CompletionWaiver,
    EscalationInput,
    EvaluationLineage,
    EvidenceClaim,
    EvidenceProvenance,
    EvidenceSubject,
    ExternalStateGate,
    HumanAcceptanceGate,
    MaterialActionGate,
    MaterialActionReceiptFact,
    VerificationGate,
)
from ostag.core.completion_evaluate import (
    completion_input_digest,
    derive_work_loop_view,
    evaluate_completion,
)

T0 = "2026-07-26T10:00:00+00:00"
T1 = "2026-07-26T11:00:00+00:00"
T2 = "2026-07-26T12:00:00+00:00"


def _compat_contract() -> CompletionContract:
    return CompletionContract(
        id="contract_c",
        version=1,
        work_thread_id="macmini/task_c",
        cycle=1,
        mode="execution_compat",
        gates=(MaterialActionGate(id="execution", action_family="executor.run"),),
        max_automatic_retries=0,
        on_satisfied="report_only",
        created_at=T0,
    )


def _run(run_id: str = "run_1", conclusion: str = "success") -> CompletionRunResult:
    return CompletionRunResult(run_id=run_id, conclusion=conclusion, recorded_at=T1)


def _escalation(state: str = "open", blocking: bool = True) -> EscalationInput:
    return EscalationInput(
        id="esc_1", blocking=blocking, state=state, reason="需要人工确认", opened_at=T1
    )


# --- execution_compat roll-up -------------------------------------------------


def test_compat_pending_without_runs() -> None:
    assessment = evaluate_completion(
        CompletionEvaluationInput(contract=_compat_contract(), evaluated_at=T1)
    )
    assert assessment.state == "pending"
    assert assessment.evidence_backed is False
    assert assessment.accepted_at == ""
    assert assessment.gate_results[0].reason_code == "execution_incomplete"
    assert assessment.gate_results[0].state == "missing"


def test_compat_success_is_satisfied_and_accepted() -> None:
    assessment = evaluate_completion(
        CompletionEvaluationInput(
            contract=_compat_contract(), run_results=(_run(),), evaluated_at=T1
        )
    )
    assert assessment.state == "satisfied"
    assert assessment.evidence_backed is False
    assert assessment.triggered_by_run_id == "run_1"
    assert assessment.accepted_at == assessment.assessed_at
    assert assessment.gate_results[0].reason_code == "execution_succeeded"


def test_compat_failure_is_unsatisfied() -> None:
    assessment = evaluate_completion(
        CompletionEvaluationInput(
            contract=_compat_contract(),
            run_results=(_run(conclusion="failure"),),
            evaluated_at=T1,
        )
    )
    assert assessment.state == "unsatisfied"
    assert assessment.accepted_at == ""


def test_compat_blocking_escalation_wins_over_success() -> None:
    assessment = evaluate_completion(
        CompletionEvaluationInput(
            contract=_compat_contract(),
            run_results=(_run(),),
            blocking_escalations=(_escalation(),),
            evaluated_at=T1,
        )
    )
    assert assessment.state == "blocked"
    assert assessment.accepted_at == ""
    assert any(
        result.gate_id == "human_escalation:esc_1" for result in assessment.gate_results
    )
    # resolved / non-blocking escalations are inactive
    resolved = evaluate_completion(
        CompletionEvaluationInput(
            contract=_compat_contract(),
            run_results=(_run(),),
            blocking_escalations=(_escalation(state="resolved"),),
            evaluated_at=T1,
        )
    )
    assert resolved.state == "satisfied"


# --- governed contract fixture ------------------------------------------------


def _governed_contract(extra_gates=()) -> CompletionContract:
    return CompletionContract(
        id="contract_g",
        version=1,
        work_thread_id="macmini/task_g",
        cycle=1,
        mode="governed",
        gates=(
            VerificationGate(
                id="tests",
                target_key="delivery",
                evidence_kind="ci.tests",
                minimum_assurance="verified",
            ),
            *extra_gates,
        ),
        max_automatic_retries=0,
        on_satisfied="report_only",
        created_at=T0,
        target_selectors=(CompletionTargetSelector(key="delivery"),),
    )


def _delivery_artifact(version: str = "v1", artifact_id: str = "art_1") -> CompletionArtifact:
    return CompletionArtifact(
        id=artifact_id,
        kind="change_request",
        recorded_at=T1,
        target=ArtifactTarget(
            key="delivery", provider="github", resource_ref="pr/7", resource_version=version
        ),
    )


def _evidence(
    *,
    evidence_id: str = "ev_1",
    assurance: str = "verified",
    outcome: str = "passed",
    version: str = "v1",
    kind: str = "ci.tests",
    observed_at: str = T1,
    received_at: str = T1,
    observations: dict[str, str] | None = None,
) -> CompletionEvidenceFact:
    return CompletionEvidenceFact(
        id=evidence_id,
        work_thread_id="macmini/task_g",
        cycle=1,
        kind=kind,
        assurance=assurance,
        subject=EvidenceSubject(provider="github", resource_ref="pr/7", resource_version=version),
        claim=EvidenceClaim(predicate="verification", outcome=outcome, observations=observations or {}),
        provenance=EvidenceProvenance(
            adapter="github", adapter_version="1", payload_digest="sha256:x"
        ),
        observed_at=observed_at,
        received_at=received_at,
    )


def _governed_input(**overrides) -> CompletionEvaluationInput:
    values = dict(
        contract=_governed_contract(),
        run_results=(_run(),),
        artifacts=(_delivery_artifact(),),
        evidence=(_evidence(),),
        evaluated_at=T2,
    )
    values.update(overrides)
    return CompletionEvaluationInput(**values)


# --- governed gate + roll-up semantics ---------------------------------------


def test_governed_verified_evidence_satisfies() -> None:
    assessment = evaluate_completion(_governed_input())
    assert assessment.state == "satisfied"
    assert assessment.evidence_backed is True
    assert assessment.accepted_at == assessment.assessed_at
    assert assessment.target_bindings[0].resource_version == "v1"
    assert assessment.gate_results[0].reason_code == "verification_passed"


def test_reported_agent_claim_cannot_satisfy_verified_gate() -> None:
    assessment = evaluate_completion(
        _governed_input(evidence=(_evidence(assurance="reported"),))
    )
    assert assessment.gate_results[0].state == "unknown"
    assert assessment.gate_results[0].reason_code == "verification_assurance_insufficient"
    assert assessment.state == "blocked"


def test_unverifiable_never_satisfies_even_reported_minimum() -> None:
    contract = CompletionContract.from_dict(
        {
            **_governed_contract().to_dict(),
            "gates": [
                {
                    "id": "tests",
                    "kind": "verification",
                    "targetKey": "delivery",
                    "evidenceKind": "ci.tests",
                    "requiredOutcome": "passed",
                    "minimumAssurance": "reported",
                }
            ],
        }
    )
    reported_ok = evaluate_completion(
        _governed_input(contract=contract, evidence=(_evidence(assurance="reported"),))
    )
    assert reported_ok.state == "satisfied"
    unverifiable = evaluate_completion(
        _governed_input(contract=contract, evidence=(_evidence(assurance="unverifiable"),))
    )
    assert unverifiable.gate_results[0].state == "unknown"


def test_stale_resource_version_is_stale_not_pass() -> None:
    assessment = evaluate_completion(
        _governed_input(evidence=(_evidence(version="v0"),))
    )
    assert assessment.gate_results[0].state == "missing"
    assert assessment.gate_results[0].reason_code == "verification_stale"
    assert assessment.state == "pending"


def test_missing_evidence_is_pending_when_execution_succeeded() -> None:
    assessment = evaluate_completion(_governed_input(evidence=()))
    assert assessment.gate_results[0].reason_code == "verification_missing"
    assert assessment.state == "pending"


def test_failed_evidence_rolls_up_unsatisfied() -> None:
    assessment = evaluate_completion(
        _governed_input(evidence=(_evidence(outcome="failed"),))
    )
    assert assessment.gate_results[0].state == "failed"
    assert assessment.state == "unsatisfied"


def test_conflicting_equally_current_evidence_is_unknown_blocked() -> None:
    assessment = evaluate_completion(
        _governed_input(
            evidence=(
                _evidence(evidence_id="ev_1", outcome="passed"),
                _evidence(evidence_id="ev_2", outcome="failed"),
            )
        )
    )
    assert assessment.gate_results[0].state == "unknown"
    assert assessment.state == "blocked"


def test_newer_evidence_supersedes_older_conflict() -> None:
    assessment = evaluate_completion(
        _governed_input(
            evidence=(
                _evidence(evidence_id="ev_old", outcome="failed", observed_at=T0),
                _evidence(evidence_id="ev_new", outcome="passed", observed_at=T1),
            )
        )
    )
    assert assessment.gate_results[0].state == "passed"
    assert assessment.gate_results[0].evidence_ids == ("ev_new",)


def test_required_observations_must_pass() -> None:
    contract = CompletionContract.from_dict(
        {
            **_governed_contract().to_dict(),
            "gates": [
                {
                    "id": "tests",
                    "kind": "verification",
                    "targetKey": "delivery",
                    "evidenceKind": "ci.tests",
                    "requiredOutcome": "passed",
                    "minimumAssurance": "verified",
                    "requiredObservations": ["unit", "acceptance"],
                }
            ],
        }
    )
    failing = evaluate_completion(
        _governed_input(
            contract=contract,
            evidence=(_evidence(observations={"unit": "passed", "acceptance": "failed"}),),
        )
    )
    assert failing.gate_results[0].state == "failed"
    passing = evaluate_completion(
        _governed_input(
            contract=contract,
            evidence=(_evidence(observations={"unit": "passed", "acceptance": "passed"}),),
        )
    )
    assert passing.gate_results[0].state == "passed"


def test_ambiguous_delivery_targets_are_unknown() -> None:
    assessment = evaluate_completion(
        _governed_input(
            artifacts=(
                _delivery_artifact(version="v1", artifact_id="art_1"),
                _delivery_artifact(version="v2", artifact_id="art_2"),
            )
        )
    )
    assert assessment.gate_results[0].reason_code == "artifact_ambiguous"
    assert assessment.state == "blocked"


def test_execution_failure_dominates_gate_success() -> None:
    assessment = evaluate_completion(
        _governed_input(run_results=(_run(conclusion="failure"),))
    )
    assert assessment.state == "unsatisfied"


def test_external_state_and_human_acceptance_gates() -> None:
    contract = _governed_contract(
        extra_gates=(
            ExternalStateGate(
                id="deploy",
                target_key="delivery",
                provider="github",
                required_state="merged",
                minimum_assurance="verified",
            ),
            HumanAcceptanceGate(id="accept", required_role="owner"),
        )
    )
    deploy_evidence = CompletionEvidenceFact(
        id="ev_deploy",
        work_thread_id="macmini/task_g",
        cycle=1,
        kind="scm.state",
        assurance="verified",
        subject=EvidenceSubject(provider="github", resource_ref="pr/7", resource_version="v1"),
        claim=EvidenceClaim(predicate="state", outcome="merged"),
        provenance=EvidenceProvenance(
            adapter="github", adapter_version="1", payload_digest="sha256:y"
        ),
        observed_at=T1,
        received_at=T1,
    )
    acceptance_evidence = CompletionEvidenceFact(
        id="ev_accept",
        work_thread_id="macmini/task_g",
        cycle=1,
        kind="human.acceptance",
        assurance="verified",
        subject=EvidenceSubject(provider="github", resource_ref="pr/7", resource_version="v1"),
        claim=EvidenceClaim(predicate="role", outcome="owner"),
        provenance=EvidenceProvenance(
            adapter="feishu", adapter_version="1", payload_digest="sha256:z"
        ),
        observed_at=T1,
        received_at=T1,
    )
    assessment = evaluate_completion(
        _governed_input(
            contract=contract,
            evidence=(_evidence(), deploy_evidence, acceptance_evidence),
        )
    )
    assert assessment.state == "satisfied"
    by_gate = {result.gate_id: result for result in assessment.gate_results}
    assert by_gate["deploy"].reason_code == "external_state_satisfied"
    assert by_gate["accept"].reason_code == "human_acceptance_recorded"

    # missing acceptance keeps the thread pending
    without_acceptance = evaluate_completion(
        _governed_input(contract=contract, evidence=(_evidence(), deploy_evidence))
    )
    assert without_acceptance.state == "pending"
    assert "accept" in [
        r.gate_id for r in without_acceptance.gate_results if r.state == "missing"
    ]


def test_material_action_gate_states() -> None:
    contract = _governed_contract(
        extra_gates=(MaterialActionGate(id="apply", action_family="table.update"),)
    )

    def receipts(*outcomes: str, at: str = T1):
        return tuple(
            MaterialActionReceiptFact(
                id=f"receipt_{index}",
                action_family="table.update",
                outcome=outcome,
                observed_at=at,
            )
            for index, outcome in enumerate(outcomes)
        )

    def state_of(*outcomes: str):
        assessment = evaluate_completion(
            _governed_input(contract=contract, material_action_receipts=receipts(*outcomes))
        )
        return {r.gate_id: r for r in assessment.gate_results}["apply"]

    assert state_of("succeeded").state == "passed"
    assert state_of("unknown").state == "unknown"
    assert state_of("failed").state == "failed"
    assert state_of().state == "missing"
    # equally-current conflicting receipts require reconciliation
    assert state_of("succeeded", "failed").reason_code == "material_action_unknown"


# --- waivers ------------------------------------------------------------------


def _waiver(expires_at: str = "", waiver_id: str = "waiver_1") -> CompletionWaiver:
    return CompletionWaiver(
        id=waiver_id,
        contract_id="contract_g",
        contract_version=1,
        cycle=1,
        actor=ActorRef(actor_id="owner", handle="@owner"),
        reason="pilot exception",
        policy_scope="workspace",
        gate_ids=("tests",),
        waived_at=T1,
        expires_at=expires_at,
    )


def test_active_waiver_waives_gate_and_marks_human() -> None:
    assessment = evaluate_completion(
        _governed_input(evidence=(), waivers=(_waiver(expires_at=T2 + "z"),))
    )
    assert assessment.gate_results[0].state == "waived"
    assert assessment.state == "waived"
    assert assessment.assessed_by == "human"
    assert assessment.waiver is not None
    assert assessment.accepted_at == assessment.assessed_at


def test_waiver_expiry_boundary_at_and_before() -> None:
    # expires exactly at evaluation time -> inactive (upstream: expiresAt > evaluatedAt)
    at_boundary = evaluate_completion(
        _governed_input(evidence=(), waivers=(_waiver(expires_at=T2),))
    )
    assert at_boundary.gate_results[0].state == "missing"
    assert at_boundary.state == "pending"
    # expires after evaluation time -> active
    after = evaluate_completion(
        _governed_input(evidence=(), waivers=(_waiver(expires_at="2026-07-26T12:00:01+00:00"),))
    )
    assert after.state == "waived"


def test_waiver_scoped_to_run_id() -> None:
    scoped = CompletionWaiver.from_dict({**_waiver().to_dict(), "runId": "run_other"})
    assessment = evaluate_completion(_governed_input(evidence=(), waivers=(scoped,)))
    assert assessment.gate_results[0].state == "missing"  # waiver bound to a different run

    matching = CompletionWaiver.from_dict({**_waiver().to_dict(), "runId": "run_1"})
    assessment = evaluate_completion(_governed_input(evidence=(), waivers=(matching,)))
    assert assessment.state == "waived"


def test_waived_only_when_everything_else_clean() -> None:
    contract = _governed_contract(
        extra_gates=(MaterialActionGate(id="apply", action_family="table.update"),)
    )
    # tests gate waived but apply gate missing -> pending, not waived
    assessment = evaluate_completion(
        _governed_input(contract=contract, evidence=(), waivers=(_waiver(),))
    )
    assert assessment.state == "pending"


# --- digest & lineage ---------------------------------------------------------


def test_digest_is_order_insensitive_and_stable() -> None:
    a = completion_input_digest(
        _governed_input(
            evidence=(
                _evidence(evidence_id="ev_1"),
                _evidence(evidence_id="ev_2", outcome="passed"),
            )
        )
    )
    b = completion_input_digest(
        _governed_input(
            evidence=(
                _evidence(evidence_id="ev_2", outcome="passed"),
                _evidence(evidence_id="ev_1"),
            )
        )
    )
    assert a == b
    assert a.startswith("sha256:") and len(a) == 7 + 64


def test_digest_changes_with_waiver_validity_window() -> None:
    base = _governed_input(waivers=(_waiver(expires_at="2026-07-26T11:30:00+00:00"),))
    active = completion_input_digest(
        CompletionEvaluationInput(**{**base.__dict__, "evaluated_at": T1})
    )
    expired = completion_input_digest(
        CompletionEvaluationInput(**{**base.__dict__, "evaluated_at": T2})
    )
    assert active != expired


def test_identical_inputs_produce_identical_assessment_ids() -> None:
    first = evaluate_completion(_governed_input())
    second = evaluate_completion(_governed_input())
    assert first.input_digest == second.input_digest
    assert first.id == second.id
    assert first.to_dict() == second.to_dict()


def test_lineage_controls_sequence_and_supersession() -> None:
    assessment = evaluate_completion(
        _governed_input(lineage=EvaluationLineage(sequence=3, supersedes_assessment_id="a_2"))
    )
    assert assessment.sequence == 3
    assert assessment.supersedes_assessment_id == "a_2"
    assert assessment.id.endswith("_3")


# --- WorkLoopView -------------------------------------------------------------


def test_work_loop_view_projection() -> None:
    contract = _governed_contract()
    runs = (_run(),)
    assessment = evaluate_completion(_governed_input(evidence=()))
    view = derive_work_loop_view(contract, runs, assessment)
    assert view.execution == "succeeded"
    assert view.completion == "pending"
    assert view.missing_gate_ids == ("tests",)
    assert "tests" in view.next_action
    payload = view.to_dict()
    assert payload["contract"]["mode"] == "governed"
    assert payload["evidenceBacked"] is True

    satisfied = evaluate_completion(_governed_input())
    done_view = derive_work_loop_view(contract, runs, satisfied)
    assert done_view.next_action == "No completion action is required."
