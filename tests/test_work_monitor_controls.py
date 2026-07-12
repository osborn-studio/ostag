from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ostag.core.identity import Principal
from ostag.core.run_ledger import RunLedger
from ostag.core.schemas import SchemaError
from ostag.work_monitor.identity import Destination, IdentityError, IdentityStore
from ostag.work_monitor.models import RemoteCommand
from ostag.work_monitor.service import WorkMonitorService
from ostag.work_monitor.store import MonitorStore


NOW = datetime(2026, 7, 13, 0, 0, tzinfo=UTC)


def _service(tmp_path: Path, *, clock=lambda: NOW):
    store = MonitorStore(tmp_path / "monitor.sqlite3")
    with store.transaction() as db:
        db.execute(
            "INSERT INTO instances(instance_id,last_sequence,last_seen_at) VALUES (?,?,?)",
            ("mac", 1, NOW.isoformat()),
        )
        db.execute(
            "INSERT INTO tasks(instance_id,task_id,title,project,status,agent,progress,"
            "duration_seconds,summary,error_class,evidence_ref,version,occurred_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "mac",
                "task-1",
                "Ship",
                "ostag",
                "running",
                "codex",
                10,
                2,
                "ok",
                None,
                None,
                7,
                NOW.isoformat(),
            ),
        )
        db.execute(
            "INSERT INTO events(event_id,instance_id,sequence,kind,occurred_at,payload) "
            "VALUES (?,?,?,?,?,?)",
            (
                "hb",
                "mac",
                1,
                "instance.heartbeat",
                NOW.isoformat(),
                json.dumps(
                    {"per_task_capabilities": {"task-1": ["pause", "resume", "retry", "cancel"]}}
                ),
            ),
        )
    identities = IdentityStore(store.path, enrollment_secret="join")
    owner = Principal("feishu", "owner")
    private = Destination("feishu", "private", "private")
    identities.bind_private(owner=owner, destination=private, enrollment_secret="join")
    identities.grant(owner, private, owner, "write")
    ledger = RunLedger(tmp_path / "ledger.jsonl", clock=clock)
    return WorkMonitorService(store, identities, ledger=ledger, clock=clock), owner, private, ledger


def _command_rows(service: WorkMonitorService):
    with service.store.connect() as db:
        return db.execute("SELECT payload FROM commands ORDER BY rowid").fetchall()


@pytest.mark.parametrize("action", ["pause", "resume", "retry", "cancel"])
def test_control_proposal_creates_principal_bound_receipt_without_command(
    tmp_path: Path, action: str
) -> None:
    service, owner, private, ledger = _service(tmp_path)
    receipt = service.propose_control(owner, private, task_id="task-1", action=action)
    assert receipt.mutation_type == action
    assert receipt.principal == owner
    assert receipt.state == "needs_approval"
    assert _command_rows(service) == []
    assert ledger.get_receipt(receipt.action_id).principal == owner


def test_approval_does_not_enqueue_and_apply_enqueues_exactly_one_versioned_command(
    tmp_path: Path,
) -> None:
    service, owner, private, _ = _service(tmp_path)
    receipt = service.propose_control(owner, private, task_id="task-1", action="pause")
    approved = service.approve_control(owner, private, receipt.action_id)
    assert approved.state == "ready_to_apply"
    assert _command_rows(service) == []

    applying = service.apply_control(owner, private, receipt.action_id)
    assert applying.state == "applying"
    rows = _command_rows(service)
    assert len(rows) == 1
    command = json.loads(rows[0]["payload"])
    assert RemoteCommand.from_dict(command).command_id == command["command_id"]
    assert command["expected_version"] == 7
    assert command["expected_lifecycle"] == "running"
    assert command["requested_by"] == "feishu:owner"
    with pytest.raises(SchemaError):
        service.apply_control(owner, private, receipt.action_id)
    assert len(_command_rows(service)) == 1


def test_controls_require_write_and_group_cancel_is_rejected(tmp_path: Path) -> None:
    service, owner, private, _ = _service(tmp_path)
    reader = Principal("feishu", "reader")
    service.identities.grant(owner, private, reader, "read")
    with pytest.raises(IdentityError, match="write"):
        service.propose_control(reader, private, task_id="task-1", action="pause")

    group = Destination("feishu", "group", "group")
    service.identities.enroll_group(owner, group, via=private, scope="all")
    service.identities.grant(owner, group, owner, "write")
    with pytest.raises(IdentityError, match="private"):
        service.propose_control(owner, group, task_id="task-1", action="cancel")


def test_confirmation_expires_after_five_minutes_and_is_principal_instance_bound(
    tmp_path: Path,
) -> None:
    current = [NOW]
    service, owner, private, _ = _service(tmp_path, clock=lambda: current[0])
    receipt = service.propose_control(owner, private, task_id="task-1", action="cancel")
    attacker = Principal("lark", "owner")
    other = Destination("lark", "private", "private")
    service.identities.bind_private(owner=attacker, destination=other, local_admin=True)
    service.identities.grant(attacker, other, attacker, "write")
    with pytest.raises(IdentityError, match="principal"):
        service.approve_control(attacker, other, receipt.action_id)

    current[0] += timedelta(minutes=5, microseconds=1)
    with pytest.raises(SchemaError) as exc:
        service.approve_control(owner, private, receipt.action_id)
    assert exc.value.code == "stale_receipt"


def test_apply_rejects_stale_task_version_and_cross_instance_replay(tmp_path: Path) -> None:
    service, owner, private, _ = _service(tmp_path)
    receipt = service.propose_control(owner, private, task_id="task-1", action="pause")
    service.approve_control(owner, private, receipt.action_id)
    with service.store.transaction() as db:
        db.execute("UPDATE tasks SET version=8 WHERE instance_id='mac' AND task_id='task-1'")
    with pytest.raises(SchemaError) as exc:
        service.apply_control(owner, private, receipt.action_id)
    assert exc.value.code == "stale_control"
    assert _command_rows(service) == []


@pytest.mark.parametrize(
    ("result", "terminal"),
    [
        ({"ok": True, "status": "paused", "version": 8}, "applied"),
        ({"ok": False, "code": "UNSUPPORTED"}, "failed"),
        ({"ok": False, "code": "COMMAND_REJECTED"}, "failed"),
        ({"ok": False, "code": "INDETERMINATE"}, "failed"),
        ({"ok": True, "status": "running", "version": 8}, "failed"),
    ],
)
def test_ack_maps_receipt_terminal_and_never_applies_failure(
    tmp_path: Path, result: dict, terminal: str
) -> None:
    service, owner, private, ledger = _service(tmp_path)
    receipt = service.propose_control(owner, private, task_id="task-1", action="pause")
    service.approve_control(owner, private, receipt.action_id)
    service.apply_control(owner, private, receipt.action_id)
    with service.store.connect() as db:
        command_id = db.execute("SELECT command_id FROM commands").fetchone()[0]
    with pytest.raises(SchemaError) as cross_instance:
        service.acknowledge_control("other-mac", command_id, result)
    assert cross_instance.value.code == "command_not_found"
    updated = service.acknowledge_control("mac", command_id, result)
    assert updated.state == terminal
    assert ledger.get_receipt(receipt.action_id).state == terminal
    with pytest.raises(SchemaError):
        service.acknowledge_control("mac", command_id, result)
