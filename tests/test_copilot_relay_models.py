from dataclasses import FrozenInstanceError

import pytest

from ostag.copilot_relay.models import RELAY_KINDS, RelayEnvelope, SessionBinding


def _payload_for(kind: str) -> dict[str, object]:
    payloads: dict[str, dict[str, object]] = {
        "session.upsert": {
            "agent_id": "codex-cli",
            "title": "Implement relay",
            "state": "awaiting_decision",
            "resume_supported": True,
            "latest_summary": "Waiting for the owner",
        },
        "assistant.snapshot": {
            "assistant_text": "请选择 A 或 B",
            "redaction_count": 0,
            "truncated": False,
        },
        "decision.request": {
            "decision_id": "decision_1",
            "question": "采用哪个方案？",
            "choices": [
                {"choice_id": "a", "label": "方案 A"},
                {"choice_id": "b", "label": "方案 B"},
            ],
            "recommended_choice_id": "b",
            "blocking": True,
            "safe_work": ["运行静态检查"],
        },
        "decision.answer": {
            "decision_id": "decision_1",
            "choice_id": "b",
            "answer": "采用方案 B",
        },
        "instruction.send": {"text": "换个方向，先完成契约测试"},
        "task.create.proposal": {
            "proposal_id": "proposal_1",
            "objective": "新增移动端状态页",
            "repository": "ostag",
            "workspace": "relay-worktree",
            "suggested_agent": "codex-cli",
            "acceptance_criteria": ["测试通过", "飞书可见"],
            "sensitive_operations": [],
        },
        "task.create.confirm": {"proposal_id": "proposal_1", "confirmed": True},
        "delivery.receipt": {
            "related_message_id": "msg_source",
            "state": "applied",
            "summary": "原会话已继续",
            "error_code": None,
        },
    }
    return payloads[kind]


def _envelope(kind: str = "instruction.send", **overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": 1,
        "message_id": f"msg_{kind}",
        "binding": {
            "instance_id": "macmini",
            "task_id": "task_1",
            "run_id": "run_1",
            "session_id": "sess_1",
            "turn_id": "turn_3",
            "session_version": 7,
        },
        "sequence": 9,
        "created_at": "2026-07-14T01:00:00Z",
        "expires_at": "2026-07-14T01:10:00Z",
        "sender": {"platform": "feishu", "actor_id": "ou_owner"},
        "kind": kind,
        "payload": _payload_for(kind),
        "correlation_id": "corr_1",
        "causation_id": None,
    }
    value.update(overrides)
    return value


def _v2_envelope(kind: str = "instruction.send", **overrides: object) -> dict[str, object]:
    value = _envelope(kind)
    value["schema_version"] = 2
    value["binding"] = {
        "instance_id": "device_macbook",
        "runtime_id": "codex-cli",
        "task_id": "task_board",
        "run_id": "run_board",
        "session_id": "session_board",
        "turn_id": "turn_7",
        "session_version": 7,
    }
    value.update(overrides)
    return value


@pytest.mark.parametrize("kind", sorted(RELAY_KINDS))
def test_each_relay_kind_round_trips_with_exact_contract(kind: str) -> None:
    envelope = RelayEnvelope.from_dict(_envelope(kind))

    assert envelope.kind == kind
    assert envelope.binding == SessionBinding(
        instance_id="macmini",
        task_id="task_1",
        run_id="run_1",
        session_id="sess_1",
        turn_id="turn_3",
        session_version=7,
    )
    assert envelope.to_dict() == _envelope(kind)
    with pytest.raises(FrozenInstanceError):
        envelope.sequence = 10  # type: ignore[misc]


def test_v1_binding_derives_legacy_runtime_without_changing_wire_shape() -> None:
    envelope = RelayEnvelope.from_dict(_envelope())

    assert envelope.binding.runtime_id == "legacy"
    assert "runtime_id" not in envelope.to_dict()["binding"]


def test_v2_binding_round_trips_runtime_id_with_exact_wire_shape() -> None:
    envelope = RelayEnvelope.from_dict(_v2_envelope())

    assert envelope.binding == SessionBinding(
        instance_id="device_macbook",
        runtime_id="codex-cli",
        task_id="task_board",
        run_id="run_board",
        session_id="session_board",
        turn_id="turn_7",
        session_version=7,
    )
    assert envelope.to_dict() == _v2_envelope()


@pytest.mark.parametrize("runtime_id", ["", True, "x" * 257])
def test_v2_binding_rejects_invalid_runtime_id(runtime_id: object) -> None:
    raw = _v2_envelope()
    raw["binding"] = dict(raw["binding"], runtime_id=runtime_id)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="runtime_id"):
        RelayEnvelope.from_dict(raw)


def test_v2_binding_requires_runtime_id_and_rejects_unknown_fields() -> None:
    raw = _v2_envelope()
    binding = dict(raw["binding"])  # type: ignore[arg-type]
    binding.pop("runtime_id")
    raw["binding"] = binding
    with pytest.raises(ValueError, match="missing keys.*runtime_id"):
        RelayEnvelope.from_dict(raw)

    raw = _v2_envelope()
    raw["binding"] = dict(raw["binding"], process_id="unsafe")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="unknown keys.*process_id"):
        RelayEnvelope.from_dict(raw)


def test_relay_envelope_rejects_unsupported_schema_version() -> None:
    with pytest.raises(ValueError, match="schema_version"):
        RelayEnvelope.from_dict(_envelope(schema_version=3))


def test_envelope_binding_sender_and_payload_reject_unknown_keys() -> None:
    with pytest.raises(ValueError, match="unknown keys.*shell"):
        RelayEnvelope.from_dict(_envelope() | {"shell": "rm -rf /"})

    raw = _envelope()
    raw["binding"] = dict(raw["binding"]) | {"native_session_id": "secret"}  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="unknown keys.*native_session_id"):
        RelayEnvelope.from_dict(raw)

    raw = _envelope()
    raw["sender"] = {"platform": "feishu", "actor_id": "owner", "admin": True}
    with pytest.raises(ValueError, match="exactly platform and actor_id"):
        RelayEnvelope.from_dict(raw)

    raw = _envelope()
    raw["payload"] = {"text": "继续", "command": "git push"}
    with pytest.raises(ValueError, match="unknown keys.*command"):
        RelayEnvelope.from_dict(raw)


@pytest.mark.parametrize("field", ["schema_version", "sequence"])
@pytest.mark.parametrize("value", [True, 0, -1])
def test_positive_integer_fields_reject_booleans_and_non_positive_values(
    field: str, value: object
) -> None:
    with pytest.raises(ValueError, match=field):
        RelayEnvelope.from_dict(_envelope(**{field: value}))


@pytest.mark.parametrize("value", [True, 0, -1])
def test_session_version_rejects_booleans_and_non_positive_values(value: object) -> None:
    raw = _envelope()
    raw["binding"] = dict(raw["binding"], session_version=value)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="session_version"):
        RelayEnvelope.from_dict(raw)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("created_at", "2026-07-14 01:00:00"),
        ("created_at", "2026-07-14T09:00:00+08:00"),
        ("expires_at", "not-a-time"),
    ],
)
def test_envelope_requires_canonical_utc_timestamps(field: str, value: str) -> None:
    with pytest.raises(ValueError, match=field):
        RelayEnvelope.from_dict(_envelope(**{field: value}))


def test_expiry_must_follow_creation() -> None:
    with pytest.raises(ValueError, match="expires_at"):
        RelayEnvelope.from_dict(_envelope(expires_at="2026-07-14T00:59:59Z"))


def test_decision_choice_tuple_must_be_unique_and_recommendation_must_exist() -> None:
    raw = _envelope("decision.request")
    raw["payload"] = dict(raw["payload"], recommended_choice_id="missing")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="recommended_choice_id"):
        RelayEnvelope.from_dict(raw)

    raw = _envelope("decision.request")
    raw["payload"] = dict(
        raw["payload"],  # type: ignore[arg-type]
        choices=[{"choice_id": "a", "label": "A"}, {"choice_id": "a", "label": "B"}],
    )
    with pytest.raises(ValueError, match="unique"):
        RelayEnvelope.from_dict(raw)


def test_payload_size_and_field_limits_are_enforced() -> None:
    raw = _envelope("instruction.send")
    raw["payload"] = {"text": "x" * 16_001}
    with pytest.raises(ValueError, match="text"):
        RelayEnvelope.from_dict(raw)

    raw = _envelope("decision.request")
    raw["payload"] = dict(
        raw["payload"],  # type: ignore[arg-type]
        choices=[{"choice_id": str(index), "label": "choice"} for index in range(9)],
    )
    with pytest.raises(ValueError, match="choices"):
        RelayEnvelope.from_dict(raw)

    raw = _envelope("decision.request")
    raw["payload"] = dict(
        raw["payload"],  # type: ignore[arg-type]
        choices=[{"choice_id": "long", "label": "x" * 161}],
        recommended_choice_id="long",
    )
    with pytest.raises(ValueError, match="label"):
        RelayEnvelope.from_dict(raw)


def test_kind_payload_shape_mismatch_is_rejected() -> None:
    with pytest.raises(ValueError, match="missing keys|unknown keys"):
        RelayEnvelope.from_dict(_envelope("instruction.send", payload=_payload_for("decision.answer")))
