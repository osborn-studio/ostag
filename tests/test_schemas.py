from __future__ import annotations

import pytest

from ostag.core.identity import Principal
from ostag.core.schemas import (
    ActionReceipt,
    ApplyPlan,
    ApprovalDecision,
    Artifact,
    ConnectorCapability,
    ContextPacket,
    LedgerEvent,
    PermissionGrant,
    Run,
    RunActor,
    RunIntent,
    RunSource,
    SchemaError,
    new_id,
    prune,
)


def test_action_receipt_round_trips_canonical_principal() -> None:
    receipt = ActionReceipt(
        action_id="action_control",
        run_id="run_control",
        title="Pause",
        target_system="work_monitor",
        target_object="mac/task-1",
        mutation_type="pause",
        principal=Principal("feishu", "owner"),
    )
    assert ActionReceipt.from_dict(receipt.to_dict()).principal == Principal("feishu", "owner")
    assert receipt.with_state("needs_approval").principal == receipt.principal


def _sample_run() -> Run:
    return Run(
        run_id="run_abc123",
        source=RunSource(platform="dingtalk", channel_id="cid1", message_id="msg1"),
        actor=RunActor(actor_id="user1", display_name="Osborn"),
        intent=RunIntent(raw_text="帮我更新需求表", normalized_type="requirement"),
    )


def test_run_round_trip() -> None:
    run = _sample_run()
    restored = Run.from_dict(run.to_dict())

    assert restored.run_id == run.run_id
    assert restored.source.platform == "dingtalk"
    assert restored.actor.actor_id == "user1"
    assert restored.intent.raw_text == "帮我更新需求表"
    assert restored.status == "queued"


def test_run_rejects_invalid_status_and_platform() -> None:
    with pytest.raises(SchemaError) as excinfo:
        _sample_run().with_status("exploded")
    assert excinfo.value.code == "invalid_choice"
    assert excinfo.value.recovery_hint

    with pytest.raises(SchemaError):
        RunSource(platform="msn", channel_id="c1")


def test_run_with_status_updates_timestamp_only_forward() -> None:
    run = _sample_run()
    updated = run.with_status("running")

    assert updated.status == "running"
    assert updated.created_at == run.created_at
    assert updated.updated_at >= run.updated_at


def test_action_receipt_round_trip_and_commands() -> None:
    receipt = ActionReceipt(
        action_id="action_1",
        run_id="run_1",
        title="更新需求 REQ-9 状态",
        target_system="dingtalk_ai_table",
        target_object="REQ-9",
        mutation_type="update",
        risk="low",
    )
    restored = ActionReceipt.from_dict(receipt.to_dict())

    assert restored.state == "proposed"
    assert restored.commands["approve"] == "approve action_1"
    assert restored.commands["continue"] == "continue run_1"
    assert restored.to_dict()["commands"]["apply"] == "apply action_1"


def test_action_receipt_validates_mutation_and_state() -> None:
    with pytest.raises(SchemaError):
        ActionReceipt(
            action_id="a",
            run_id="r",
            title="t",
            target_system="s",
            target_object="o",
            mutation_type="explode",
        )
    with pytest.raises(SchemaError):
        ActionReceipt(
            action_id="a",
            run_id="r",
            title="t",
            target_system="s",
            target_object="o",
            mutation_type="update",
            state="unknown",
        )


def test_context_packet_hash_is_stable_and_content_sensitive() -> None:
    packet = ContextPacket(
        context_packet_id="ctx_1",
        run_id="run_1",
        source_thread_summary="讨论了新需求",
        citations=("dingtalk://msg1",),
        created_at="2026-07-04T00:00:00+00:00",
    )
    same = ContextPacket.from_dict(packet.to_dict())
    different = ContextPacket(
        context_packet_id="ctx_1",
        run_id="run_1",
        source_thread_summary="内容变了",
        citations=("dingtalk://msg1",),
        created_at="2026-07-04T00:00:00+00:00",
    )

    assert packet.content_hash() == same.content_hash()
    assert packet.content_hash() != different.content_hash()


def test_artifact_requires_path_or_url() -> None:
    with pytest.raises(SchemaError) as excinfo:
        Artifact(artifact_id="art_1", run_id="run_1", name="report.md")
    assert excinfo.value.code == "missing_location"

    artifact = Artifact(artifact_id="art_1", run_id="run_1", name="report.md", path="/tmp/r.md")
    assert Artifact.from_dict(artifact.to_dict()).path == "/tmp/r.md"


def test_permission_grant_ordering() -> None:
    grant = PermissionGrant(
        grant_id="grant_1", actor_id="user1", platform="slack", permission_level="write"
    )

    assert grant.allows("read")
    assert grant.allows("propose")
    assert grant.allows("write")
    assert not grant.allows("admin")


def test_approval_decision_round_trip() -> None:
    decision = ApprovalDecision(
        decision_id="dec_1",
        action_id="action_1",
        run_id="run_1",
        actor_id="user1",
        decision="approve",
    )
    assert ApprovalDecision.from_dict(decision.to_dict()).decision == "approve"

    with pytest.raises(SchemaError):
        ApprovalDecision(
            decision_id="dec_2",
            action_id="action_1",
            run_id="run_1",
            actor_id="user1",
            decision="maybe",
        )


def test_apply_plan_requires_steps() -> None:
    with pytest.raises(SchemaError) as excinfo:
        ApplyPlan(plan_id="plan_1", action_id="action_1", run_id="run_1")
    assert excinfo.value.code == "empty_plan"

    plan = ApplyPlan(
        plan_id="plan_1", action_id="action_1", run_id="run_1", steps=("update record",)
    )
    assert ApplyPlan.from_dict(plan.to_dict()).requires_approval is True


def test_ledger_event_round_trip_and_type_validation() -> None:
    event = LedgerEvent(
        event_id="evt_1", run_id="run_1", event_type="run_created", payload={"k": "v"}
    )
    assert LedgerEvent.from_dict(event.to_dict()).payload == {"k": "v"}

    with pytest.raises(SchemaError):
        LedgerEvent(event_id="evt_2", run_id="run_1", event_type="mystery")


def test_connector_capability_round_trip() -> None:
    capability = ConnectorCapability(
        platform="dingtalk",
        connector="ai_table",
        capabilities={"can_read_records": True, "can_delete_records": False},
        limits={"batch_size": 30},
    )
    restored = ConnectorCapability.from_dict(capability.to_dict())

    assert restored.can("can_read_records")
    assert not restored.can("can_delete_records")
    assert restored.destructive_requires_approval is True
    assert restored.default_mode == "propose"


def test_prune_drops_null_and_empty_noise() -> None:
    payload = {"a": None, "b": "", "c": [], "d": {}, "e": {"f": None, "g": 1}, "h": 0}

    assert prune(payload) == {"e": {"g": 1}, "h": 0}


def test_new_id_prefixes() -> None:
    run_id = new_id("run")
    assert run_id.startswith("run_")
    assert len(run_id) == len("run_") + 12
