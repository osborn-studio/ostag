from __future__ import annotations

import pytest

from ostag.core.completion import (
    ActorRef,
    CompletionAssessment,
    CompletionContract,
    CompletionGateResult,
    CompletionTargetSelector,
    CompletionWaiver,
    HumanAcceptanceGate,
    MaterialActionGate,
    VerificationGate,
    gate_from_dict,
)
from ostag.core.schemas import SchemaError

NOW = "2026-07-26T10:00:00+00:00"
DIGEST = "sha256:" + "ab" * 32


def _governed_contract(**overrides):
    values = dict(
        id="contract_1",
        version=1,
        work_thread_id="macmini/task_1",
        cycle=1,
        mode="governed",
        gates=(
            VerificationGate(
                id="tests",
                target_key="delivery",
                evidence_kind="ci.tests",
                minimum_assurance="verified",
            ),
        ),
        max_automatic_retries=0,
        on_satisfied="report_only",
        created_at=NOW,
        target_selectors=(CompletionTargetSelector(key="delivery"),),
    )
    values.update(overrides)
    return CompletionContract(**values)


def test_contract_round_trips_canonical_wire_fields() -> None:
    contract = _governed_contract()
    payload = contract.to_dict()

    assert payload["workThreadId"] == "macmini/task_1"
    assert payload["targetSelectors"][0]["cardinality"] == "exactly_one"
    assert payload["gates"][0]["minimumAssurance"] == "verified"
    assert payload["maxAutomaticRetries"] == 0
    restored = CompletionContract.from_dict(payload)
    assert restored == contract


def test_contract_rejects_duplicate_gate_ids() -> None:
    with pytest.raises(SchemaError) as excinfo:
        _governed_contract(
            gates=(
                MaterialActionGate(id="g", action_family="deploy"),
                MaterialActionGate(id="g", action_family="deploy2"),
            )
        )
    assert excinfo.value.code == "duplicate_key"


def test_contract_rejects_unknown_target_key() -> None:
    with pytest.raises(SchemaError) as excinfo:
        _governed_contract(
            gates=(
                VerificationGate(
                    id="tests",
                    target_key="nope",
                    evidence_kind="ci.tests",
                    minimum_assurance="verified",
                ),
            )
        )
    assert excinfo.value.code == "invalid_reference"


def test_execution_compat_contract_must_not_declare_targets() -> None:
    with pytest.raises(SchemaError) as excinfo:
        _governed_contract(
            mode="execution_compat",
            gates=(MaterialActionGate(id="execution", action_family="executor.run"),),
        )
    assert excinfo.value.code == "invalid_contract"

    compat = _governed_contract(
        mode="execution_compat",
        gates=(MaterialActionGate(id="execution", action_family="executor.run"),),
        target_selectors=(),
    )
    assert compat.mode == "execution_compat"


def test_contract_requires_at_least_one_gate() -> None:
    with pytest.raises(SchemaError):
        _governed_contract(gates=())


def test_gate_from_dict_dispatches_all_five_kinds_round_trip() -> None:
    gates = [
        {"id": "a", "kind": "artifact", "targetKey": "t", "artifactKind": "patch", "minimum": 1},
        {
            "id": "v",
            "kind": "verification",
            "targetKey": "t",
            "evidenceKind": "ci.tests",
            "requiredOutcome": "passed",
            "minimumAssurance": "verified",
            "requiredObservations": ["unit"],
        },
        {
            "id": "e",
            "kind": "external_state",
            "targetKey": "t",
            "provider": "github",
            "requiredState": "merged",
            "minimumAssurance": "reported",
        },
        {"id": "m", "kind": "material_action", "actionFamily": "deploy", "requiredOutcome": "succeeded"},
        {"id": "h", "kind": "human_acceptance", "requiredRole": "owner"},
    ]
    for payload in gates:
        gate = gate_from_dict(payload)
        assert gate.to_dict() == payload or {
            k: v for k, v in gate.to_dict().items() if k != "requiredObservations"
        } == {k: v for k, v in payload.items() if k != "requiredObservations"}
        assert gate_from_dict(gate.to_dict()) == gate
    human = gate_from_dict({"id": "accept", "kind": "human_acceptance", "requiredRole": "owner"})
    assert isinstance(human, HumanAcceptanceGate)
    with pytest.raises(SchemaError):
        gate_from_dict({"id": "x", "kind": "mystery"})
    with pytest.raises(SchemaError) as excinfo:
        gate_from_dict({"id": "h2", "kind": "human_acceptance", "requiredRole": "owner", "bogus": 1})
    assert excinfo.value.code == "unknown_field"


def test_wire_strictness_enums_and_datetimes() -> None:
    # unknown fields rejected (upstream .strict())
    with pytest.raises(SchemaError) as excinfo:
        CompletionContract.from_dict({**_governed_contract().to_dict(), "surprise": True})
    assert excinfo.value.code == "unknown_field"
    # artifact kind vocabulary enforced
    from ostag.core.completion import ArtifactTarget, CompletionArtifact

    with pytest.raises(SchemaError) as excinfo:
        CompletionArtifact(id="art", kind="change_request", recorded_at=NOW)
    assert excinfo.value.code == "invalid_choice"
    ok = CompletionArtifact(
        id="art",
        kind="pull_request",
        recorded_at=NOW,
        target=ArtifactTarget(key="d", provider="github", resource_ref="pr/1", resource_version="v1"),
    )
    assert ok.to_dict()["kind"] == "pull_request"
    # policy scope vocabulary enforced
    with pytest.raises(SchemaError):
        CompletionWaiver(
            id="w",
            contract_id="c",
            contract_version=1,
            cycle=1,
            actor=ActorRef(actor_id="a"),
            reason="r",
            policy_scope="workspace",
            gate_ids=("g",),
            waived_at=NOW,
        )
    # timestamps must be timezone-aware ISO-8601
    with pytest.raises(SchemaError) as excinfo:
        _governed_contract(created_at="yesterday")
    assert excinfo.value.code == "invalid_datetime"
    with pytest.raises(SchemaError):
        _governed_contract(created_at="2026-07-26T10:00:00")  # naive, no tz
    with pytest.raises(SchemaError):
        _governed_contract(created_at="2026-07-26T18:00:00+08:00")  # non-UTC offset
    _governed_contract(created_at="2026-07-26T10:00:00Z")  # Z form accepted
    # gate-level artifact kind vocabulary enforced too
    from ostag.core.completion import ArtifactGate

    with pytest.raises(SchemaError) as excinfo:
        ArtifactGate(id="a", target_key="t", artifact_kind="change_request", minimum=1)
    assert excinfo.value.code == "invalid_choice"


def test_waiver_requires_unique_gate_ids_and_serializes_run_id() -> None:
    with pytest.raises(SchemaError):
        CompletionWaiver(
            id="waiver_1",
            contract_id="contract_1",
            contract_version=1,
            cycle=1,
            actor=ActorRef(actor_id="owner"),
            reason="pilot",
            policy_scope="work_item_override",
            gate_ids=("tests", "tests"),
            waived_at=NOW,
        )
    waiver = CompletionWaiver(
        id="waiver_1",
        contract_id="contract_1",
        contract_version=1,
        cycle=1,
        actor=ActorRef(actor_id="owner", handle="@owner"),
        reason="pilot",
        policy_scope="work_item_override",
        gate_ids=("tests",),
        waived_at=NOW,
        run_id="run_9",
        expires_at="2026-07-27T00:00:00+00:00",
    )
    payload = waiver.to_dict()
    assert payload["runId"] == "run_9"
    assert payload["expiresAt"] == "2026-07-27T00:00:00+00:00"
    assert CompletionWaiver.from_dict(payload) == waiver


def _gate_result() -> CompletionGateResult:
    return CompletionGateResult(
        gate_id="tests",
        state="passed",
        evidence_ids=("ev_1",),
        reason_code="verification_passed",
        reason="ok",
        evaluated_at=NOW,
    )


def test_assessment_validates_digest_and_assessed_by() -> None:
    base = dict(
        id="assessment_1",
        work_thread_id="macmini/task_1",
        contract_id="contract_1",
        contract_version=1,
        cycle=1,
        sequence=1,
        target_bindings=(),
        state="satisfied",
        evidence_backed=True,
        gate_results=(_gate_result(),),
        assessed_at=NOW,
        assessed_by="ostag",
    )
    with pytest.raises(SchemaError) as excinfo:
        CompletionAssessment(input_digest="sha256:short", **base)
    assert excinfo.value.code == "invalid_digest"
    with pytest.raises(SchemaError):
        CompletionAssessment(input_digest=DIGEST, **{**base, "assessed_by": "opentag"})

    assessment = CompletionAssessment(input_digest=DIGEST, **base)
    restored = CompletionAssessment.from_dict(assessment.to_dict())
    assert restored == assessment
    assert restored.to_dict()["assessedBy"] == "ostag"


def test_assessment_superrefine_invariants() -> None:
    from ostag.core.completion import ResolvedCompletionTarget

    base = dict(
        id="assessment_1",
        work_thread_id="macmini/task_1",
        contract_id="contract_1",
        contract_version=1,
        cycle=1,
        sequence=1,
        input_digest=DIGEST,
        target_bindings=(),
        state="satisfied",
        evidence_backed=True,
        gate_results=(_gate_result(),),
        assessed_at=NOW,
        assessed_by="ostag",
    )
    binding = ResolvedCompletionTarget(
        key="d", provider="github", resource_ref="pr/1", resource_version="v1", artifact_id="a1"
    )
    with pytest.raises(SchemaError):  # duplicate binding keys
        CompletionAssessment(**{**base, "target_bindings": (binding, binding)})
    with pytest.raises(SchemaError):  # duplicate gate result ids
        CompletionAssessment(**{**base, "gate_results": (_gate_result(), _gate_result())})
    with pytest.raises(SchemaError):  # waived requires waiver
        CompletionAssessment(**{**base, "state": "waived"})
    waiver = CompletionWaiver(
        id="w1",
        contract_id="contract_1",
        contract_version=1,
        cycle=1,
        actor=ActorRef(actor_id="owner"),
        reason="r",
        policy_scope="work_item_override",
        gate_ids=("tests",),
        waived_at=NOW,
    )
    with pytest.raises(SchemaError):  # waiver implies human assessor
        CompletionAssessment(**{**base, "state": "waived", "waiver": waiver})
    with pytest.raises(SchemaError):  # waiver must target assessed contract
        CompletionAssessment(
            **{
                **base,
                "state": "waived",
                "assessed_by": "human",
                "waiver": CompletionWaiver.from_dict({**waiver.to_dict(), "contractId": "other"}),
            }
        )
    ok = CompletionAssessment(
        **{**base, "state": "waived", "assessed_by": "human", "waiver": waiver}
    )
    assert ok.state == "waived"
