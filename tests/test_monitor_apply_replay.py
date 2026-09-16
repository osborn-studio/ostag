"""Replay/idempotency contract for :meth:`WorkMonitorService.apply_control`.

A control that was already authorized, reserved in SQLite, and published to the
run ledger must stay exactly one command: replaying the same action (whether a
delayed concurrent caller or a retry after restart) returns the original
applying receipt without issuing a second command, rewriting the frozen
payload, or appending new ledger bytes. Anything that is not that exact durable
command — a different principal or destination, a moved task, a rebound,
corrupt, or missing command row — must still fail closed.
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from ostag.core.identity import Principal
from ostag.core.run_ledger import RunLedger
from ostag.core.schemas import SchemaError
from ostag.work_monitor.identity import Destination, IdentityError, IdentityStore
from ostag.work_monitor.service import WorkMonitorService
from ostag.work_monitor.store import MonitorStore

NOW = datetime(2026, 7, 13, 0, 0, tzinfo=UTC)


def _seed(tmp_path: Path) -> MonitorStore:
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
    return store


def _service(tmp_path: Path, *, clock=lambda: NOW):
    store = _seed(tmp_path)
    identities = IdentityStore(store.path, enrollment_secret="join")
    owner = Principal("feishu", "owner")
    private = Destination("feishu", "private", "private")
    identities.bind_private(owner=owner, destination=private, enrollment_secret="join")
    identities.grant(owner, private, owner, "write")
    ledger = RunLedger(tmp_path / "ledger.jsonl", clock=clock)
    return WorkMonitorService(store, identities, ledger=ledger, clock=clock), owner, private


def _reopen(tmp_path: Path, *, clock=lambda: NOW) -> WorkMonitorService:
    """A fresh service over the same durable SQLite file and ledger file."""
    store = MonitorStore(tmp_path / "monitor.sqlite3")
    identities = IdentityStore(store.path, enrollment_secret="join")
    ledger = RunLedger(tmp_path / "ledger.jsonl", clock=clock)
    return WorkMonitorService(store, identities, ledger=ledger, clock=clock)


def _commands(service: WorkMonitorService) -> list[dict[str, Any]]:
    with service.store.connect() as db:
        rows = db.execute(
            "SELECT command_id,action_id,instance_id,task_id,status,payload "
            "FROM commands ORDER BY rowid"
        ).fetchall()
    return [dict(row) for row in rows]


def _ledger_bytes(tmp_path: Path) -> bytes:
    return (tmp_path / "ledger.jsonl").read_bytes()


def _reserved(service: WorkMonitorService, owner: Principal, private: Destination, action: str):
    receipt = service.propose_control(owner, private, task_id="task-1", action=action)
    service.approve_control(owner, private, receipt.action_id)
    applying = service.apply_control(owner, private, receipt.action_id)
    return receipt, applying


def test_delayed_concurrent_apply_after_publish_reuses_the_published_command(
    tmp_path: Path,
) -> None:
    service, owner, private = _service(tmp_path)
    receipt = service.propose_control(owner, private, task_id="task-1", action="pause")
    service.approve_control(owner, private, receipt.action_id)

    published = threading.Event()
    observed: dict[str, Any] = {}
    results: dict[str, Any] = {}
    errors: list[BaseException] = []
    original_bound = service._bound_receipt

    def gated(principal, destination, action_id):
        # The replaying caller reads the receipt strictly after the first apply
        # has reserved its command and published the 'applying' state, which is
        # the interleaving that used to raise invalid_transition.
        if threading.current_thread().name == "replayer":
            assert published.wait(timeout=10), "first apply never finished"
            observed["ledger"] = _ledger_bytes(tmp_path)
            observed["commands"] = _commands(service)
        return original_bound(principal, destination, action_id)

    service._bound_receipt = gated

    def apply(name: str) -> None:
        try:
            results[name] = service.apply_control(owner, private, receipt.action_id)
        except BaseException as exc:  # noqa: BLE001 - surfaced by the assertions below
            errors.append(exc)
        finally:
            if name == "primary":
                published.set()

    threads = [
        threading.Thread(target=apply, args=(name,), name=name) for name in ("primary", "replayer")
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)
    assert not any(thread.is_alive() for thread in threads)

    assert errors == []
    assert results["primary"].state == "applying"
    assert results["replayer"].state == "applying"
    assert results["replayer"].action_id == receipt.action_id
    assert results["replayer"].apply_payload == results["primary"].apply_payload
    # The replay hands back durable ledger truth rather than minting a new one.
    assert results["replayer"].to_dict() == service.ledger.get_receipt(receipt.action_id).to_dict()

    commands = _commands(service)
    assert len(commands) == 1
    assert commands[0]["action_id"] == receipt.action_id
    assert commands == observed["commands"]
    assert _ledger_bytes(tmp_path) == observed["ledger"]


def test_sequential_replay_returns_one_pollable_command_and_no_ledger_append(
    tmp_path: Path,
) -> None:
    service, owner, private = _service(tmp_path)
    receipt, applying = _reserved(service, owner, private, "pause")
    reserved = _commands(service)
    ledger_bytes = _ledger_bytes(tmp_path)
    frozen = json.loads(reserved[0]["payload"])

    replayed = service.apply_control(owner, private, receipt.action_id)

    assert replayed.state == "applying"
    assert replayed.action_id == applying.action_id
    assert replayed.apply_payload == applying.apply_payload
    assert replayed.to_dict() == service.ledger.get_receipt(receipt.action_id).to_dict()
    assert replayed.created_at == applying.created_at
    assert _commands(service) == reserved
    assert _ledger_bytes(tmp_path) == ledger_bytes

    claimed = service.poll_commands("mac")
    assert len(claimed) == 1
    assert claimed[0]["command_id"] == frozen["command_id"]
    assert claimed[0]["issued_at"] == frozen["issued_at"]
    assert claimed[0]["expires_at"] == frozen["expires_at"]
    assert service.ledger.get_receipt(receipt.action_id).state == "applying"


def test_replay_after_sqlite_and_ledger_restart_reuses_the_durable_command(
    tmp_path: Path,
) -> None:
    service, owner, private = _service(tmp_path)
    receipt, _ = _reserved(service, owner, private, "cancel")
    reserved = _commands(service)
    ledger_bytes = _ledger_bytes(tmp_path)

    restarted = _reopen(tmp_path)
    replayed = restarted.apply_control(owner, private, receipt.action_id)

    assert replayed.state == "applying"
    assert _commands(restarted) == reserved
    assert _ledger_bytes(tmp_path) == ledger_bytes


def test_lost_ledger_ack_is_recovered_before_replay_becomes_inert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, owner, private = _service(tmp_path)
    receipt = service.propose_control(owner, private, task_id="task-1", action="retry")
    service.approve_control(owner, private, receipt.action_id)
    original = service.ledger.record_receipt_state
    dropped = False

    def drop_first_publish(*args, **kwargs):
        nonlocal dropped
        if kwargs.get("detail") and not dropped:
            dropped = True
            raise OSError("ledger ack lost")
        return original(*args, **kwargs)

    monkeypatch.setattr(service.ledger, "record_receipt_state", drop_first_publish)
    with pytest.raises(OSError, match="ledger ack lost"):
        service.apply_control(owner, private, receipt.action_id)
    reserved = _commands(service)
    assert len(reserved) == 1
    assert service.ledger.get_receipt(receipt.action_id).state == "ready_to_apply"

    recovered = service.apply_control(owner, private, receipt.action_id)
    assert recovered.state == "applying"
    assert _commands(service) == reserved
    assert service.ledger.get_receipt(receipt.action_id).state == "applying"

    ledger_bytes = _ledger_bytes(tmp_path)
    inert = service.apply_control(owner, private, receipt.action_id)
    assert inert.state == "applying"
    assert _commands(service) == reserved
    assert _ledger_bytes(tmp_path) == ledger_bytes


def test_lost_ledger_ack_recovery_refuses_a_command_rebound_to_another_actor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Recovery must own the reservation it adopts, not merely find one.

    The receipt is still ``ready_to_apply`` because its publication was lost,
    so the replay checks that only look at ``applying`` receipts never run. The
    durable command is the sole authority here, and it now belongs to someone
    else.
    """
    service, owner, private = _service(tmp_path)
    receipt = service.propose_control(owner, private, task_id="task-1", action="pause")
    service.approve_control(owner, private, receipt.action_id)
    original = service.ledger.record_receipt_state

    def lose_publication(*args, **kwargs):
        if kwargs.get("detail"):
            raise OSError("synthetic lost ledger acknowledgement")
        return original(*args, **kwargs)

    monkeypatch.setattr(service.ledger, "record_receipt_state", lose_publication)
    with pytest.raises(OSError, match="lost ledger acknowledgement"):
        service.apply_control(owner, private, receipt.action_id)
    monkeypatch.setattr(service.ledger, "record_receipt_state", original)
    assert service.ledger.get_receipt(receipt.action_id).state == "ready_to_apply"

    reserved = _commands(service)
    assert len(reserved) == 1
    rebound = json.loads(reserved[0]["payload"])
    rebound["requested_by"] = "feishu:different-owner"
    with service.store.transaction() as db:
        db.execute(
            "UPDATE commands SET payload=? WHERE command_id=?",
            (json.dumps(rebound, sort_keys=True), reserved[0]["command_id"]),
        )
    ledger_bytes = _ledger_bytes(tmp_path)

    with pytest.raises(SchemaError) as denied:
        service.apply_control(owner, private, receipt.action_id)

    assert denied.value.code == "command_not_found"
    assert _ledger_bytes(tmp_path) == ledger_bytes
    assert service.ledger.get_receipt(receipt.action_id).state == "ready_to_apply"
    assert len(_commands(service)) == 1
    assert json.loads(_commands(service)[0]["payload"])["requested_by"] == "feishu:different-owner"

    # The very same recovery still works once the reservation is this owner's.
    with service.store.transaction() as db:
        db.execute(
            "UPDATE commands SET payload=? WHERE command_id=?",
            (reserved[0]["payload"], reserved[0]["command_id"]),
        )
    recovered = service.apply_control(owner, private, receipt.action_id)
    assert recovered.state == "applying"
    assert _commands(service) == reserved
    assert service.ledger.get_receipt(receipt.action_id).state == "applying"


def test_delayed_ready_snapshot_cannot_publish_after_a_terminal_acknowledgement(
    tmp_path: Path,
) -> None:
    """A stale ``ready_to_apply`` snapshot must not downgrade an applied action.

    The delayed caller reads its receipt before anything else happens, then a
    second caller applies and the instance acknowledges the command. When the
    delayed caller finally resumes it must be denied against the durable
    command state rather than appending another ``applying`` event.
    """
    service, owner, private = _service(tmp_path)
    receipt = service.propose_control(owner, private, task_id="task-1", action="pause")
    service.approve_control(owner, private, receipt.action_id)

    snapshot_taken = threading.Event()
    resume = threading.Event()
    errors: list[BaseException] = []
    results: list[Any] = []
    original_bound = service._bound_receipt

    def gated(principal, destination, action_id):
        bound = original_bound(principal, destination, action_id)
        if threading.current_thread().name == "delayed":
            snapshot_taken.set()
            assert resume.wait(timeout=10), "delayed caller was never released"
        return bound

    service._bound_receipt = gated

    def delayed_apply() -> None:
        try:
            results.append(service.apply_control(owner, private, receipt.action_id))
        except BaseException as exc:  # noqa: BLE001 - surfaced by the assertions below
            errors.append(exc)

    delayed = threading.Thread(target=delayed_apply, name="delayed")
    delayed.start()
    try:
        assert snapshot_taken.wait(timeout=10), "delayed caller never read its receipt"
        assert service.apply_control(owner, private, receipt.action_id).state == "applying"
        commands = _commands(service)
        assert len(commands) == 1
        acknowledged = service.acknowledge_control(
            "mac",
            commands[0]["command_id"],
            {"ok": True, "status": "paused", "code": "OK", "version": 7},
        )
        assert acknowledged.state == "applied"
        ledger_bytes = _ledger_bytes(tmp_path)
        resume.set()
        delayed.join(timeout=10)
        assert not delayed.is_alive()
    finally:
        resume.set()
        delayed.join(timeout=10)

    assert results == []
    assert len(errors) == 1 and isinstance(errors[0], SchemaError), errors
    assert errors[0].code == "command_replay"
    assert service.ledger.get_receipt(receipt.action_id).state == "applied"
    assert _ledger_bytes(tmp_path) == ledger_bytes
    assert len(_commands(service)) == 1
    assert _commands(service)[0]["status"] == "acked"


def test_replay_by_other_principal_or_destination_is_denied(tmp_path: Path) -> None:
    service, owner, private = _service(tmp_path)
    receipt, _ = _reserved(service, owner, private, "pause")
    reserved = _commands(service)
    ledger_bytes = _ledger_bytes(tmp_path)

    attacker = Principal("lark", "owner")
    elsewhere = Destination("lark", "private", "private")
    service.identities.bind_private(owner=attacker, destination=elsewhere, local_admin=True)
    service.identities.grant(attacker, elsewhere, attacker, "write")
    with pytest.raises(IdentityError, match="principal"):
        service.apply_control(attacker, elsewhere, receipt.action_id)

    other = Destination("feishu", "other-private", "private")
    service.identities.bind_private(owner=owner, destination=other, local_admin=True)
    service.identities.grant(owner, other, owner, "write")
    with pytest.raises(IdentityError, match="destination"):
        service.apply_control(owner, other, receipt.action_id)

    reader = Principal("feishu", "reader")
    service.identities.grant(owner, private, reader, "read")
    with pytest.raises(IdentityError, match="write"):
        service.apply_control(reader, private, receipt.action_id)

    assert _commands(service) == reserved
    assert _ledger_bytes(tmp_path) == ledger_bytes


def test_replay_is_denied_after_the_task_moves_or_the_receipt_is_terminal(
    tmp_path: Path,
) -> None:
    service, owner, private = _service(tmp_path)
    receipt, _ = _reserved(service, owner, private, "pause")
    reserved = _commands(service)
    with service.store.transaction() as db:
        db.execute("UPDATE tasks SET version=8 WHERE instance_id='mac' AND task_id='task-1'")
    with pytest.raises(SchemaError) as stale:
        service.apply_control(owner, private, receipt.action_id)
    assert stale.value.code == "stale_control"
    assert _commands(service) == reserved

    with service.store.transaction() as db:
        db.execute("UPDATE tasks SET version=7 WHERE instance_id='mac' AND task_id='task-1'")
    command_id = reserved[0]["command_id"]
    terminal = service.acknowledge_control(
        "mac", command_id, {"ok": True, "code": "OK", "status": "paused", "version": 7}
    )
    assert terminal.state == "applied"
    ledger_bytes = _ledger_bytes(tmp_path)
    with pytest.raises(SchemaError) as replayed:
        service.apply_control(owner, private, receipt.action_id)
    assert replayed.value.code == "invalid_transition"
    assert service.ledger.get_receipt(receipt.action_id).state == "applied"
    assert len(_commands(service)) == 1
    assert _commands(service)[0]["status"] == "acked"
    assert _ledger_bytes(tmp_path) == ledger_bytes


@pytest.mark.parametrize(
    "corruption",
    [
        "delete",
        "invalid_json",
        "rebound_actor",
        "rebound_action",
        "rebound_version",
        "rebound_task",
        "rebound_command_id",
    ],
)
def test_applying_receipt_without_its_exact_command_is_denied(
    tmp_path: Path, corruption: str
) -> None:
    service, owner, private = _service(tmp_path)
    receipt, _ = _reserved(service, owner, private, "pause")
    stored = _commands(service)[0]
    payload = json.loads(stored["payload"])
    with service.store.transaction() as db:
        if corruption == "delete":
            db.execute("DELETE FROM commands WHERE action_id=?", (receipt.action_id,))
        else:
            if corruption == "invalid_json":
                mutated: str = "{not json"
            else:
                if corruption == "rebound_actor":
                    payload["requested_by"] = "feishu:someone-else"
                elif corruption == "rebound_action":
                    payload["action"] = "cancel"
                elif corruption == "rebound_version":
                    payload["expected_version"] = 99
                elif corruption == "rebound_task":
                    payload["task_id"] = "task-2"
                else:
                    payload["command_id"] = "cmd_forged"
                mutated = json.dumps(payload, sort_keys=True)
            db.execute(
                "UPDATE commands SET payload=? WHERE action_id=?", (mutated, receipt.action_id)
            )
    before = _commands(service)
    ledger_bytes = _ledger_bytes(tmp_path)

    with pytest.raises(SchemaError) as denied:
        service.apply_control(owner, private, receipt.action_id)

    assert denied.value.code == "command_not_found"
    assert _commands(service) == before
    assert _ledger_bytes(tmp_path) == ledger_bytes
    assert service.ledger.get_receipt(receipt.action_id).state == "applying"


def test_distinct_actions_reserve_independent_commands_under_replay(tmp_path: Path) -> None:
    service, owner, private = _service(tmp_path)
    pause, _ = _reserved(service, owner, private, "pause")
    resume, _ = _reserved(service, owner, private, "resume")

    commands = _commands(service)
    assert len({row["command_id"] for row in commands}) == 2
    assert {row["action_id"] for row in commands} == {pause.action_id, resume.action_id}

    ledger_bytes = _ledger_bytes(tmp_path)
    assert service.apply_control(owner, private, pause.action_id).state == "applying"
    assert service.apply_control(owner, private, resume.action_id).state == "applying"
    assert _commands(service) == commands
    assert _ledger_bytes(tmp_path) == ledger_bytes
    assert {json.loads(row["payload"])["action"] for row in commands} == {"pause", "resume"}


def test_expiry_and_issue_timestamps_never_move_on_replay(tmp_path: Path) -> None:
    current = [NOW]
    service, owner, private = _service(tmp_path, clock=lambda: current[0])
    receipt, _ = _reserved(service, owner, private, "pause")
    frozen = json.loads(_commands(service)[0]["payload"])

    current[0] += timedelta(minutes=4)
    replayed = service.apply_control(owner, private, receipt.action_id)

    assert replayed.state == "applying"
    assert json.loads(_commands(service)[0]["payload"]) == frozen
    assert frozen["issued_at"] == NOW.isoformat()
    assert frozen["expires_at"] == (NOW + timedelta(minutes=5)).isoformat()

    current[0] += timedelta(minutes=2)
    with pytest.raises(SchemaError) as stale:
        service.apply_control(owner, private, receipt.action_id)
    assert stale.value.code == "stale_receipt"
    assert json.loads(_commands(service)[0]["payload"]) == frozen
