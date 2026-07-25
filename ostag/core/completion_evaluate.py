"""Pure completion evaluator ported from OpenTag v0.7.0.

Function-for-function port of ``packages/governance/src/evaluate.ts`` at the
pinned upstream commit ``758fb5a6`` (``v0.7.0^{}``). No IO, no clocks: the
evaluation time is injected (``evaluated_at``) or derived from the latest
input timestamp exactly as upstream does. Identical canonical inputs produce
identical ``inputDigest`` values.

Declared divergence (mapping note): all sorts and digest key ordering use
Python code-point (byte-wise) string ordering, NOT JavaScript
``localeCompare`` ICU collation — e.g. ``ev_1`` vs ``ev-2`` order differently
across the two. Digest values are therefore an OStag-local idempotency key:
deterministic and stable within OStag, but NOT byte-comparable with digests
computed by the upstream TypeScript implementation. Timestamp comparisons
assume a uniform ISO-8601 UTC format, which the models validate.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from ostag.core.completion import (
    ArtifactGate,
    CompletionArtifact,
    CompletionAssessment,
    CompletionEvaluationInput,
    CompletionEvidenceFact,
    CompletionGate,
    CompletionGateResult,
    CompletionWaiver,
    ExternalStateGate,
    HumanAcceptanceGate,
    MaterialActionGate,
    MaterialActionReceiptFact,
    ResolvedCompletionTarget,
    VerificationGate,
    WorkLoopView,
)

HUMAN_ACCEPTANCE_EVIDENCE_KIND = "human.acceptance"


def _canonicalize(value: Any) -> Any:
    if isinstance(value, list):
        return [_canonicalize(item) for item in value]
    if isinstance(value, dict):
        return {key: _canonicalize(value[key]) for key in sorted(value)}
    return value


def _dump(value: Any) -> str:
    return json.dumps(_canonicalize(value), separators=(",", ":"), ensure_ascii=False)


def completion_input_digest(input_value: CompletionEvaluationInput) -> str:
    """Mirror upstream ``completionInputDigest`` (lineage excluded)."""
    waiver_evaluation_time = input_value.evaluated_at or _latest_timestamp(input_value)
    ordered = {
        "contract": input_value.contract.to_dict(),
        "runResults": [
            item.to_dict()
            for item in sorted(input_value.run_results, key=lambda item: item.run_id)
        ],
        "artifacts": [
            item.to_dict() for item in sorted(input_value.artifacts, key=lambda item: item.id)
        ],
        "evidence": [
            item.to_dict() for item in sorted(input_value.evidence, key=lambda item: item.id)
        ],
        "materialActionReceipts": [
            item.to_dict()
            for item in sorted(input_value.material_action_receipts, key=lambda item: item.id)
        ],
        "waivers": [
            item.to_dict() for item in sorted(input_value.waivers, key=lambda item: item.id)
        ],
        "blockingEscalations": [
            item.to_dict()
            for item in sorted(input_value.blocking_escalations, key=lambda item: item.id)
        ],
        "waiverValidity": sorted(
            (
                {"id": waiver.id, "active": waiver.expires_at > waiver_evaluation_time}
                for waiver in input_value.waivers
                if waiver.expires_at
            ),
            key=lambda item: item["id"],
        ),
    }
    digest = hashlib.sha256(_dump(ordered).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _latest_timestamp(input_value: CompletionEvaluationInput) -> str:
    timestamps = [input_value.contract.created_at]
    timestamps.extend(item.recorded_at for item in input_value.run_results)
    timestamps.extend(item.recorded_at for item in input_value.artifacts)
    for item in input_value.evidence:
        timestamps.extend((item.observed_at, item.received_at))
    timestamps.extend(item.observed_at for item in input_value.material_action_receipts)
    timestamps.extend(item.waived_at for item in input_value.waivers)
    for item in input_value.blocking_escalations:
        timestamps.append(item.opened_at)
        if item.resolved_at:
            timestamps.append(item.resolved_at)
    return max(timestamps) if timestamps else input_value.contract.created_at


def _active_blocking_escalations(input_value: CompletionEvaluationInput):
    return tuple(item for item in input_value.blocking_escalations if item.active)


def _assessment_timestamp(input_value: CompletionEvaluationInput, evaluation_time: str) -> str:
    boundaries = [
        waiver.expires_at
        for waiver in input_value.waivers
        if waiver.expires_at and waiver.expires_at <= evaluation_time
    ]
    return max([_latest_timestamp(input_value), *boundaries])


def _assurance_accepted(actual: str, minimum: str) -> bool:
    if actual == "unverifiable":
        return False
    if minimum == "verified":
        return actual == "verified"
    return actual in ("verified", "reported")


def _current_run_id(input_value: CompletionEvaluationInput) -> str:
    ordered = sorted(
        input_value.run_results, key=lambda item: (item.recorded_at, item.run_id)
    )
    return ordered[-1].run_id if ordered else ""


def _valid_waiver(
    waiver: CompletionWaiver,
    gate_id: str,
    input_value: CompletionEvaluationInput,
    evaluated_at: str,
) -> bool:
    current_run = _current_run_id(input_value)
    return (
        waiver.contract_id == input_value.contract.id
        and waiver.contract_version == input_value.contract.version
        and waiver.cycle == input_value.contract.cycle
        and (not waiver.run_id or waiver.run_id == current_run)
        and gate_id in waiver.gate_ids
        and (not waiver.expires_at or waiver.expires_at > evaluated_at)
    )


def _waiver_for_gate(
    input_value: CompletionEvaluationInput, gate_id: str, evaluated_at: str
) -> CompletionWaiver | None:
    candidates = [
        waiver
        for waiver in input_value.waivers
        if _valid_waiver(waiver, gate_id, input_value, evaluated_at)
    ]
    candidates.sort(key=lambda waiver: (_reversed_text(waiver.waived_at), waiver.id))
    return candidates[0] if candidates else None


def _reversed_text(value: str) -> tuple[int, ...]:
    """Sort helper for descending lexicographic order on one key."""
    return tuple(-ord(char) for char in value)


def _resolved_targets(
    input_value: CompletionEvaluationInput,
) -> tuple[
    tuple[ResolvedCompletionTarget, ...],
    dict[str, list[CompletionArtifact]],
    set[str],
]:
    artifacts_by_target: dict[str, list[CompletionArtifact]] = {}
    for artifact in input_value.artifacts:
        if artifact.target is None:
            continue
        artifacts_by_target.setdefault(artifact.target.key, []).append(artifact)
    bindings: list[ResolvedCompletionTarget] = []
    ambiguous_keys: set[str] = set()
    for selector in input_value.contract.target_selectors:
        candidates = sorted(
            artifacts_by_target.get(selector.key, []), key=lambda item: item.id
        )
        identities: dict[str, list[CompletionArtifact]] = {}
        for artifact in candidates:
            assert artifact.target is not None
            identity = _dump(
                [
                    artifact.target.provider,
                    artifact.target.resource_ref,
                    artifact.target.resource_version,
                ]
            )
            identities.setdefault(identity, []).append(artifact)
        if len(identities) > 1:
            ambiguous_keys.add(selector.key)
            continue
        first_group = next(iter(identities.values()), None)
        candidate = first_group[0] if first_group else None
        if candidate is None or candidate.target is None:
            continue
        bindings.append(
            ResolvedCompletionTarget(
                key=selector.key,
                provider=candidate.target.provider,
                resource_ref=candidate.target.resource_ref,
                resource_version=candidate.target.resource_version,
                artifact_id=candidate.id,
            )
        )
    return tuple(bindings), artifacts_by_target, ambiguous_keys


def _authoritative_evidence(
    facts: list[CompletionEvidenceFact],
) -> tuple[list[CompletionEvidenceFact], bool]:
    if not facts:
        return [], False
    ordered = sorted(
        facts,
        key=lambda item: (
            _reversed_text(item.observed_at),
            _reversed_text(item.received_at),
            item.id,
        ),
    )
    latest = ordered[0]
    tied = [
        item
        for item in ordered
        if item.observed_at == latest.observed_at and item.received_at == latest.received_at
    ]
    claims = {
        _dump({"assurance": item.assurance, "claim": item.claim.to_dict()}) for item in tied
    }
    return tied, len(claims) > 1


def _current_receipts(
    receipts: list[MaterialActionReceiptFact],
) -> tuple[list[MaterialActionReceiptFact], bool]:
    if not receipts:
        return [], False
    ordered = sorted(receipts, key=lambda item: (_reversed_text(item.observed_at), item.id))
    latest = ordered[0]
    tied = [item for item in ordered if item.observed_at == latest.observed_at]
    return tied, len({item.outcome for item in tied}) > 1


def _target_evidence(
    evidence: tuple[CompletionEvidenceFact, ...],
    target: ResolvedCompletionTarget,
    kind: str = "",
) -> list[CompletionEvidenceFact]:
    return [
        item
        for item in evidence
        if (not kind or item.kind == kind)
        and item.subject.provider == target.provider
        and item.subject.resource_ref == target.resource_ref
        and item.subject.resource_version == target.resource_version
    ]


def _stale_target_evidence(
    evidence: tuple[CompletionEvidenceFact, ...],
    target: ResolvedCompletionTarget,
    kind: str = "",
) -> list[CompletionEvidenceFact]:
    return [
        item
        for item in evidence
        if (not kind or item.kind == kind)
        and item.subject.provider == target.provider
        and item.subject.resource_ref == target.resource_ref
        and item.subject.resource_version != target.resource_version
    ]


def _result(
    gate_id: str,
    state: str,
    evidence_ids: list[str],
    reason_code: str,
    reason: str,
    evaluated_at: str,
    target_key: str = "",
) -> CompletionGateResult:
    return CompletionGateResult(
        gate_id=gate_id,
        state=state,
        evidence_ids=tuple(evidence_ids),
        reason_code=reason_code,
        reason=reason,
        evaluated_at=evaluated_at,
        target_key=target_key,
    )


def _evaluate_gate(
    gate: CompletionGate,
    input_value: CompletionEvaluationInput,
    target_by_key: dict[str, ResolvedCompletionTarget],
    artifacts_by_target: dict[str, list[CompletionArtifact]],
    ambiguous_keys: set[str],
    evaluated_at: str,
    waiver_evaluation_time: str,
) -> CompletionGateResult:
    gate_target_key = getattr(gate, "target_key", "")
    waiver = _waiver_for_gate(input_value, gate.id, waiver_evaluation_time)
    if waiver is not None:
        return _result(
            gate.id,
            "waived",
            [],
            "gate_waived",
            f"Gate waived by {waiver.actor.display}.",
            evaluated_at,
            gate_target_key,
        )
    target = target_by_key.get(gate_target_key) if gate_target_key else None
    if gate_target_key and gate_target_key in ambiguous_keys:
        return _result(
            gate.id,
            "unknown",
            [],
            "artifact_ambiguous",
            "Multiple delivery targets are present for this work cycle.",
            evaluated_at,
            gate_target_key,
        )
    if isinstance(gate, ArtifactGate):
        candidates = artifacts_by_target.get(gate.target_key, [])
        matching = [item for item in candidates if item.kind == gate.artifact_kind]
        if target is None or len(matching) < gate.minimum:
            return _result(
                gate.id,
                "missing",
                [],
                "artifact_missing",
                f"Missing {gate.artifact_kind} artifact for the current delivery target.",
                evaluated_at,
                gate.target_key,
            )
        return _result(
            gate.id,
            "passed",
            sorted(item.id for item in matching),
            "artifact_requirement_satisfied",
            f"{len(matching)} matching {gate.artifact_kind} artifact(s) recorded.",
            evaluated_at,
            gate.target_key,
        )
    if isinstance(gate, VerificationGate):
        if target is None:
            return _result(
                gate.id,
                "missing",
                [],
                "verification_missing",
                "The delivery target has not been resolved.",
                evaluated_at,
                gate.target_key,
            )
        matching = _target_evidence(input_value.evidence, target, gate.evidence_kind)
        if not matching:
            stale = _stale_target_evidence(input_value.evidence, target, gate.evidence_kind)
            return _result(
                gate.id,
                "missing",
                sorted(item.id for item in stale),
                "verification_stale" if stale else "verification_missing",
                "Verification exists only for a different resource version."
                if stale
                else "Required verification evidence has not arrived.",
                evaluated_at,
                gate.target_key,
            )
        authoritative, conflicted = _authoritative_evidence(matching)
        if conflicted:
            return _result(
                gate.id,
                "unknown",
                sorted(item.id for item in authoritative),
                "verification_assurance_insufficient",
                "Equally current authoritative verification observations conflict.",
                evaluated_at,
                gate.target_key,
            )
        assured = [
            item
            for item in authoritative
            if _assurance_accepted(item.assurance, gate.minimum_assurance)
        ]
        if not assured:
            return _result(
                gate.id,
                "unknown",
                sorted(item.id for item in authoritative),
                "verification_assurance_insufficient",
                f"Verification requires {gate.minimum_assurance} evidence.",
                evaluated_at,
                gate.target_key,
            )

        def observations_passed(item: CompletionEvidenceFact) -> bool:
            return all(
                item.claim.observations.get(name) == "passed"
                for name in gate.required_observations
            )

        passed = next(
            (
                item
                for item in assured
                if item.claim.outcome == gate.required_outcome and observations_passed(item)
            ),
            None,
        )
        if passed is None:
            return _result(
                gate.id,
                "failed",
                sorted(item.id for item in assured),
                "verification_failed",
                "Verified evidence does not satisfy the required outcome and observations.",
                evaluated_at,
                gate.target_key,
            )
        return _result(
            gate.id,
            "passed",
            [passed.id],
            "verification_passed",
            "Required verification passed for the current resource version.",
            evaluated_at,
            gate.target_key,
        )
    if isinstance(gate, ExternalStateGate):
        if target is None:
            return _result(
                gate.id,
                "missing",
                [],
                "external_state_missing",
                "The delivery target has not been resolved.",
                evaluated_at,
                gate.target_key,
            )
        if target.provider != gate.provider:
            return _result(
                gate.id,
                "unknown",
                [],
                "external_state_subject_mismatch",
                "The resolved target provider does not match this gate.",
                evaluated_at,
                gate.target_key,
            )
        matching = [
            item
            for item in _target_evidence(input_value.evidence, target)
            if item.claim.predicate == "state"
        ]
        if not matching:
            stale = [
                item
                for item in _stale_target_evidence(input_value.evidence, target)
                if item.claim.predicate == "state"
            ]
            return _result(
                gate.id,
                "missing",
                sorted(item.id for item in stale),
                "external_state_stale" if stale else "external_state_missing",
                "External state exists only for a different resource version."
                if stale
                else "Required external state evidence has not arrived.",
                evaluated_at,
                gate.target_key,
            )
        authoritative, conflicted = _authoritative_evidence(matching)
        if conflicted:
            return _result(
                gate.id,
                "unknown",
                sorted(item.id for item in authoritative),
                "external_state_assurance_insufficient",
                "Equally current authoritative external-state observations conflict.",
                evaluated_at,
                gate.target_key,
            )
        assured = [
            item
            for item in authoritative
            if _assurance_accepted(item.assurance, gate.minimum_assurance)
        ]
        if not assured:
            return _result(
                gate.id,
                "unknown",
                sorted(item.id for item in authoritative),
                "external_state_assurance_insufficient",
                f"External state requires {gate.minimum_assurance} evidence.",
                evaluated_at,
                gate.target_key,
            )
        satisfied = next(
            (item for item in assured if item.claim.outcome == gate.required_state), None
        )
        if satisfied is None:
            return _result(
                gate.id,
                "failed",
                sorted(item.id for item in assured),
                "external_state_mismatch",
                f"Verified external state is not {gate.required_state}.",
                evaluated_at,
                gate.target_key,
            )
        return _result(
            gate.id,
            "passed",
            [satisfied.id],
            "external_state_satisfied",
            f"Verified external state is {gate.required_state}.",
            evaluated_at,
            gate.target_key,
        )
    if isinstance(gate, MaterialActionGate):
        receipts = [
            item
            for item in input_value.material_action_receipts
            if item.action_family == gate.action_family
        ]
        current, conflicted = _current_receipts(receipts)
        if conflicted:
            return _result(
                gate.id,
                "unknown",
                sorted(item.id for item in current),
                "material_action_unknown",
                "Equally current material action receipts conflict and require reconciliation.",
                evaluated_at,
                gate.target_key,
            )
        succeeded = next(
            (item for item in current if item.outcome == gate.required_outcome), None
        )
        if succeeded is not None:
            return _result(
                gate.id,
                "passed",
                [succeeded.id],
                "material_action_succeeded",
                "The required material action has a succeeded receipt.",
                evaluated_at,
                gate.target_key,
            )
        unknown = next((item for item in current if item.outcome == "unknown"), None)
        if unknown is not None:
            return _result(
                gate.id,
                "unknown",
                [unknown.id],
                "material_action_unknown",
                "The material action outcome is unknown and requires reconciliation.",
                evaluated_at,
                gate.target_key,
            )
        failed = next((item for item in current if item.outcome == "failed"), None)
        if failed is not None:
            return _result(
                gate.id,
                "failed",
                [failed.id],
                "material_action_failed",
                "The required material action failed.",
                evaluated_at,
                gate.target_key,
            )
        return _result(
            gate.id,
            "missing",
            [],
            "material_action_missing",
            "No receipt exists for the required material action.",
            evaluated_at,
            gate.target_key,
        )
    assert isinstance(gate, HumanAcceptanceGate)
    acceptance = next(
        (
            item
            for item in input_value.evidence
            if item.kind == HUMAN_ACCEPTANCE_EVIDENCE_KIND
            and item.claim.predicate == "role"
            and item.claim.outcome == gate.required_role
            and item.assurance == "verified"
        ),
        None,
    )
    if acceptance is not None:
        return _result(
            gate.id,
            "passed",
            [acceptance.id],
            "human_acceptance_recorded",
            f"Acceptance recorded for role {gate.required_role}.",
            evaluated_at,
            gate.target_key,
        )
    return _result(
        gate.id,
        "missing",
        [],
        "human_acceptance_missing",
        f"Acceptance from role {gate.required_role} is missing.",
        evaluated_at,
        gate.target_key,
    )


def _escalation_gate_results(
    escalations, evaluated_at: str
) -> list[CompletionGateResult]:
    return [
        CompletionGateResult(
            gate_id=f"human_escalation:{escalation.id}",
            state="unknown",
            evidence_ids=(escalation.id,),
            reason_code="human_acceptance_missing",
            reason=escalation.reason,
            evaluated_at=evaluated_at,
        )
        for escalation in escalations
    ]


def _assessment_id(input_digest: str, sequence: int) -> str:
    return f"assessment_{input_digest[len('sha256:'):len('sha256:') + 24]}_{sequence}"


def _compatibility_assessment(
    input_value: CompletionEvaluationInput, input_digest: str, evaluated_at: str
) -> CompletionAssessment:
    successful = next(
        (item for item in input_value.run_results if item.conclusion == "success"), None
    )
    blocking = _active_blocking_escalations(input_value)
    sequence = input_value.lineage.sequence if input_value.lineage else 1
    if blocking:
        state = "blocked"
    elif successful is not None:
        state = "satisfied"
    elif input_value.run_results:
        state = "unsatisfied"
    else:
        state = "pending"
    execution_gate = CompletionGateResult(
        gate_id=input_value.contract.gates[0].id if input_value.contract.gates else "execution",
        state="passed"
        if successful is not None
        else ("failed" if input_value.run_results else "missing"),
        evidence_ids=(successful.run_id,) if successful is not None else (),
        reason_code="execution_succeeded" if successful is not None else "execution_incomplete",
        reason="Executor run succeeded under the compatibility contract."
        if successful is not None
        else "No successful executor result is available.",
        evaluated_at=evaluated_at,
    )
    return CompletionAssessment(
        id=_assessment_id(input_digest, sequence),
        work_thread_id=input_value.contract.work_thread_id,
        contract_id=input_value.contract.id,
        contract_version=input_value.contract.version,
        cycle=input_value.contract.cycle,
        sequence=sequence,
        input_digest=input_digest,
        target_bindings=(),
        state=state,
        evidence_backed=False,
        gate_results=(execution_gate, *_escalation_gate_results(blocking, evaluated_at)),
        assessed_at=evaluated_at,
        assessed_by="ostag",
        triggered_by_run_id=successful.run_id if successful is not None else "",
        supersedes_assessment_id=(
            input_value.lineage.supersedes_assessment_id if input_value.lineage else ""
        ),
        accepted_at=evaluated_at if successful is not None and not blocking else "",
    )


def evaluate_completion(input_value: CompletionEvaluationInput) -> CompletionAssessment:
    evaluation_time = input_value.evaluated_at or _latest_timestamp(input_value)
    evaluated_at = _assessment_timestamp(input_value, evaluation_time)
    digest_input = CompletionEvaluationInput(
        contract=input_value.contract,
        run_results=input_value.run_results,
        artifacts=input_value.artifacts,
        evidence=input_value.evidence,
        material_action_receipts=input_value.material_action_receipts,
        waivers=input_value.waivers,
        blocking_escalations=input_value.blocking_escalations,
        evaluated_at=evaluation_time,
    )
    input_digest = completion_input_digest(digest_input)
    if input_value.contract.mode == "execution_compat":
        return _compatibility_assessment(input_value, input_digest, evaluated_at)

    bindings, artifacts_by_target, ambiguous_keys = _resolved_targets(input_value)
    target_by_key = {target.key: target for target in bindings}
    gate_results = [
        _evaluate_gate(
            gate,
            input_value,
            target_by_key,
            artifacts_by_target,
            ambiguous_keys,
            evaluated_at,
            evaluation_time,
        )
        for gate in input_value.contract.gates
    ]
    has_execution_success = any(
        item.conclusion == "success" for item in input_value.run_results
    )
    has_unknown = any(result.state == "unknown" for result in gate_results)
    has_failed = any(result.state == "failed" for result in gate_results)
    has_missing = any(result.state == "missing" for result in gate_results)
    has_waived = any(result.state == "waived" for result in gate_results)
    blocking = _active_blocking_escalations(input_value)
    effective_gate_results = (
        *gate_results,
        *_escalation_gate_results(blocking, evaluated_at),
    )
    if blocking:
        state = "blocked"
    elif not has_execution_success and input_value.run_results:
        state = "unsatisfied"
    elif has_unknown:
        state = "blocked"
    elif has_failed:
        state = "unsatisfied"
    elif has_missing or not has_execution_success:
        state = "pending"
    elif has_waived:
        state = "waived"
    else:
        state = "satisfied"
    sequence = input_value.lineage.sequence if input_value.lineage else 1
    active_waiver: CompletionWaiver | None = None
    if state == "waived":
        candidates = [
            waiver
            for waiver in input_value.waivers
            if any(
                result.state == "waived"
                and _valid_waiver(waiver, result.gate_id, input_value, evaluation_time)
                for result in gate_results
            )
        ]
        candidates.sort(key=lambda waiver: (_reversed_text(waiver.waived_at), waiver.id))
        active_waiver = candidates[0] if candidates else None
    last_run = input_value.run_results[-1] if input_value.run_results else None
    return CompletionAssessment(
        id=_assessment_id(input_digest, sequence),
        work_thread_id=input_value.contract.work_thread_id,
        contract_id=input_value.contract.id,
        contract_version=input_value.contract.version,
        cycle=input_value.contract.cycle,
        sequence=sequence,
        input_digest=input_digest,
        target_bindings=bindings,
        state=state,
        evidence_backed=True,
        gate_results=effective_gate_results,
        assessed_at=evaluated_at,
        assessed_by="human" if active_waiver is not None else "ostag",
        triggered_by_run_id=last_run.run_id if last_run is not None else "",
        supersedes_assessment_id=(
            input_value.lineage.supersedes_assessment_id if input_value.lineage else ""
        ),
        accepted_at=evaluated_at if state in ("satisfied", "waived") else "",
        waiver=active_waiver,
    )


def derive_work_loop_view(
    contract, run_results, assessment: CompletionAssessment
) -> WorkLoopView:
    ordered = sorted(run_results, key=lambda item: item.recorded_at)
    latest = ordered[-1] if ordered else None
    if latest is None:
        execution = "idle"
    elif latest.conclusion == "success":
        execution = "succeeded"
    elif latest.conclusion == "failure":
        execution = "failed"
    else:
        execution = latest.conclusion
    missing = tuple(
        result.gate_id for result in assessment.gate_results if result.state == "missing"
    )
    failed = tuple(
        result.gate_id for result in assessment.gate_results if result.state == "failed"
    )
    blocked = tuple(
        result.gate_id for result in assessment.gate_results if result.state == "unknown"
    )
    if assessment.state in ("satisfied", "waived"):
        next_action = "No completion action is required."
    elif blocked:
        next_action = f"Reconcile blocked gate(s): {', '.join(blocked)}."
    elif failed:
        next_action = f"Repair failed gate(s): {', '.join(failed)}."
    else:
        next_action = f"Provide evidence for gate(s): {', '.join(missing)}."
    return WorkLoopView(
        work_thread_id=assessment.work_thread_id,
        execution=execution,
        completion=assessment.state,
        evidence_backed=assessment.evidence_backed,
        contract_id=contract.id,
        contract_version=contract.version,
        contract_cycle=contract.cycle,
        contract_mode=contract.mode,
        current_assessment=assessment,
        target_bindings=assessment.target_bindings,
        missing_gate_ids=missing,
        failed_gate_ids=failed,
        blocked_gate_ids=blocked,
        next_action=next_action,
    )
