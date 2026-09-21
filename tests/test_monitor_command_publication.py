"""A durable reservation is not executable until its owning apply is published."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
import hashlib
import json
import sqlite3
import threading

import pytest

from ostag.core.schemas import SchemaError
from test_monitor_apply_replay import NOW, _reopen, _reserved, _service


ACK = {"ok": True, "code": "OK", "status": "paused", "version": 7}


def _snapshot(service):
    with service.store.connect() as db:
        tables = [
            [dict(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY rowid")]
            for table in ("commands", "notification_outbox", "tasks")
        ]
    return (*tables, service.ledger.path.read_bytes())


def _unpublished(tmp_path, monkeypatch):
    service, owner, private = _service(tmp_path)
    receipt = service.propose_control(owner, private, task_id="task-1", action="pause")
    service.approve_control(owner, private, receipt.action_id)
    append = service.ledger.append

    def fail_publication(event):
        if event.payload.get("to_state") == "applying":
            raise OSError("publication unavailable")
        return append(event)

    monkeypatch.setattr(service.ledger, "append", fail_publication)
    with pytest.raises(OSError, match="publication unavailable"):
        service.apply_control(owner, private, receipt.action_id)
    assert service.ledger.get_receipt(receipt.action_id).state == "ready_to_apply"
    rows = _snapshot(service)[0]
    assert len(rows) == 1 and rows[0]["status"] == "pending"
    return service, owner, private, receipt, append


def _poll_together(*services):
    barrier = threading.Barrier(len(services))

    def poll(service):
        barrier.wait(timeout=10)
        return service.poll_commands("mac")

    with ThreadPoolExecutor(max_workers=len(services)) as pool:
        return list(pool.map(poll, services))


@pytest.mark.parametrize("restart", [False, True])
def test_unpublished_reservation_is_never_leased(tmp_path, monkeypatch, restart):
    service, _, _, _, _ = _unpublished(tmp_path, monkeypatch)
    if restart:
        service = _reopen(tmp_path)
    before = _snapshot(service)
    assert _poll_together(service, _reopen(tmp_path)) == [(), ()]
    assert _snapshot(service) == before


@pytest.mark.parametrize("result", [ACK, {"ok": False, "code": "FAILED"}])
def test_unpublished_ack_has_no_side_effects(tmp_path, monkeypatch, result):
    service, _, _, _, _ = _unpublished(tmp_path, monkeypatch)
    service = _reopen(tmp_path)
    before = _snapshot(service)
    with pytest.raises(SchemaError):
        service.acknowledge_control("mac", before[0][0]["command_id"], result)
    assert _snapshot(service) == before


def test_low_level_store_claim_remains_a_storage_primitive(tmp_path, monkeypatch):
    service, _, _, _, _ = _unpublished(tmp_path, monkeypatch)
    command = json.loads(_snapshot(service)[0][0]["payload"])
    assert service.store.claim_pending_commands(
        "mac", claimed_at=NOW.isoformat(), lease_until=(NOW + timedelta(seconds=30)).isoformat()
    ) == (command,)


def test_retry_publishes_same_reservation_and_terminal_ack_recovers(tmp_path, monkeypatch):
    service, owner, private, receipt, append = _unpublished(tmp_path, monkeypatch)
    before = _snapshot(service)
    assert service.poll_commands("mac") == ()
    monkeypatch.setattr(service.ledger, "append", append)
    service = _reopen(tmp_path)
    assert service.apply_control(owner, private, receipt.action_id).state == "applying"
    assert _snapshot(service)[0] == before[0]
    command = json.loads(before[0][0]["payload"])
    claimed = _poll_together(service, _reopen(tmp_path))
    assert sorted(map(len, claimed)) == [0, 1]
    assert [item for batch in claimed for item in batch] == [command]
    assert _reopen(tmp_path).poll_commands("mac") == ()

    append = service.ledger.append

    def fail_terminal(event):
        if event.payload.get("to_state") == "applied":
            raise OSError("terminal unavailable")
        return append(event)

    monkeypatch.setattr(service.ledger, "append", fail_terminal)
    with pytest.raises(OSError, match="terminal unavailable"):
        service.acknowledge_control("mac", command["command_id"], ACK)
    unsynced = _snapshot(service)
    assert unsynced[0][0]["status"] == "acked"
    assert unsynced[0][0]["ledger_synced"] == 0
    assert len(unsynced[1]) == 1
    service = _reopen(tmp_path)
    assert service.acknowledge_control("mac", command["command_id"], ACK).state == "applied"
    synced = _snapshot(service)
    assert synced[0][0]["ledger_synced"] == 1
    assert synced[0][0]["payload"] == before[0][0]["payload"]
    assert synced[1] == unsynced[1]
    assert service.acknowledge_control("mac", command["command_id"], ACK).state == "applied"
    assert _snapshot(service) == synced


@pytest.mark.parametrize("operation", ["poll", "ack"])
@pytest.mark.parametrize(
    "corruption",
    [
        "requested_by",
        "instance_id",
        "task_id",
        "action",
        "expected_version",
        "expected_lifecycle",
        "command_id",
        "invalid_json",
        "missing_receipt",
        "receipt_principal",
        "receipt_state",
        "receipt_payload",
        "rebound_receipt",
    ],
)
def test_invalid_ownership_is_inert(tmp_path, operation, corruption):
    service, owner, private = _service(tmp_path)
    receipt, _ = _reserved(service, owner, private, "pause")
    row = _snapshot(service)[0][0]
    if corruption == "rebound_receipt":
        # Even identical frozen bindings do not make another receipt the owner.
        other = replace(service.ledger.get_receipt(receipt.action_id), action_id="other")
        service.ledger.record_receipt(other)
        with service.store.transaction() as db:
            db.execute("UPDATE commands SET action_id=?", (other.action_id,))
    elif corruption.startswith("receipt_"):
        events = service.ledger.events()
        if corruption == "receipt_state":
            events[-1]["payload"]["to_state"] = "ready_to_apply"
        else:
            proposed = events[0]["payload"]["receipt"]
            proposed["principal" if corruption == "receipt_principal" else "apply_payload"] = None
        service.ledger.path.write_text("".join(json.dumps(event) + "\n" for event in events))
    else:
        command = json.loads(row["payload"])
        if corruption == "missing_receipt":
            with service.store.transaction() as db:
                db.execute("UPDATE commands SET action_id='missing'")
        else:
            command[corruption] = 8 if corruption == "expected_version" else "elsewhere"
            payload = "{invalid" if corruption == "invalid_json" else json.dumps(command)
            with service.store.transaction() as db:
                db.execute("UPDATE commands SET payload=?", (payload,))
    before = _snapshot(service)
    if operation == "poll":
        assert service.poll_commands("mac") == ()
    else:
        with pytest.raises(SchemaError):
            service.acknowledge_control("mac", row["command_id"], ACK)
    assert _snapshot(service) == before


@pytest.mark.parametrize("operation", ["poll", "ack"])
def test_publication_is_read_under_commands_write_lock(tmp_path, monkeypatch, operation):
    service, owner, private = _service(tmp_path)
    _reserved(service, owner, private, "pause")
    get_receipt = service.ledger.get_receipt
    observed = []

    def check_lock(*args, **kwargs):
        with sqlite3.connect(service.store.path, timeout=0) as competitor:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                competitor.execute("BEGIN IMMEDIATE")
        observed.append(True)
        return get_receipt(*args, **kwargs)

    monkeypatch.setattr(service.ledger, "get_receipt", check_lock)
    if operation == "poll":
        assert len(service.poll_commands("mac")) == 1
    else:
        command_id = _snapshot(service)[0][0]["command_id"]
        assert service.acknowledge_control("mac", command_id, ACK).state == "applied"
    assert observed


def test_published_command_keeps_lease_and_expiration_semantics(tmp_path):
    now = [NOW]
    service, owner, private = _service(tmp_path, clock=lambda: now[0])
    _reserved(service, owner, private, "pause")
    first = service.poll_commands("mac")
    assert len(first) == 1
    assert service.poll_commands("mac") == ()
    now[0] += timedelta(seconds=30)
    assert service.poll_commands("mac") == first
    now[0] += timedelta(minutes=5)
    before = _snapshot(service)
    assert service.poll_commands("mac") == ()
    assert _snapshot(service) == before


def test_old_acked_reservation_still_reconciles_without_publication(tmp_path, monkeypatch):
    service, _, _, receipt, _ = _unpublished(tmp_path, monkeypatch)
    command_id = _snapshot(service)[0][0]["command_id"]
    # Model a pre-fix ACK persisted before its terminal ledger append failed.
    canonical = json.dumps(ACK, sort_keys=True, separators=(",", ":"))
    ack_hash = hashlib.sha256(canonical.encode()).hexdigest()
    with service.store.transaction() as db:
        db.execute(
            "UPDATE commands SET status='acked',ack_hash=?,ack_payload=?",
            (ack_hash, json.dumps({"terminal_state": "applied", "result": ACK})),
        )
    service = _reopen(tmp_path)
    assert service.acknowledge_control("mac", command_id, ACK).state == "applied"
    assert service.ledger.get_receipt(receipt.action_id).state == "applied"
    synced = _snapshot(service)
    assert synced[0][0]["ledger_synced"] == 1
    assert synced[1] == []
    assert service.acknowledge_control("mac", command_id, ACK).state == "applied"
    assert _snapshot(service) == synced
