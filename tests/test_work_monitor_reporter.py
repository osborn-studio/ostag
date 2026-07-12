from __future__ import annotations

import hashlib
import hmac
import json
import io
import sys
from datetime import UTC, datetime
from pathlib import Path

from ostag.main import main_async
from ostag.core.run_ledger import RunLedger
from ostag.core.schemas import LedgerEvent
from ostag.work_monitor.reporter import Reporter, TransportResponse
from ostag.work_monitor.models import RemoteCommand


NOW = datetime(2026, 7, 12, 1, 0, tzinfo=UTC)


class FakeTransport:
    def __init__(self, responses: list[TransportResponse] | None = None) -> None:
        self.responses = responses or []
        self.requests: list[tuple[str, str, bytes, dict[str, str]]] = []

    def request(
        self, method: str, url: str, body: bytes, headers: dict[str, str]
    ) -> TransportResponse:
        self.requests.append((method, url, body, headers))
        return (
            self.responses.pop(0)
            if self.responses
            else TransportResponse(200, b'{"ack_sequence":999}')
        )


def _record(
    event_type: str, payload: dict[str, object], *, event_id: str = "ledger-1"
) -> dict[str, object]:
    return {
        "event_id": event_id,
        "run_id": "run-42",
        "event_type": event_type,
        "created_at": "2026-07-12T01:00:00+00:00",
        "payload": payload,
    }


def _reporter(tmp_path: Path, transport: FakeTransport | None = None) -> Reporter:
    return Reporter(
        instance_id="macbook",
        secret="top-secret",
        central_url="https://monitor.example",
        ledger_path=tmp_path / "ledger.jsonl",
        spool_path=tmp_path / "spool.sqlite3",
        transport=transport or FakeTransport(),
        clock=lambda: NOW,
    )


def test_tail_maps_ledger_events_redacts_and_restarts_without_duplicates(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.jsonl"
    records = [
        _record(
            "run_created",
            {"run": {"intent": {"raw_text": "Ship token=private"}, "executor_id": "codex"}},
        ),
        _record(
            "run_status_changed", {"status": "running", "detail": "Bearer abc"}, event_id="ledger-2"
        ),
        _record(
            "action_proposed",
            {"receipt": {"summary": "Approve password=hunter2"}},
            event_id="ledger-3",
        ),
        _record("run_finished", {"status": "succeeded", "detail": "done"}, event_id="ledger-4"),
    ]
    ledger.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")

    reporter = _reporter(tmp_path)
    assert reporter.tail_ledger_once() == 4
    events = reporter.pending_events()
    assert [event.kind for event in events] == [
        "task.created",
        "task.started",
        "task.awaiting_approval",
        "task.completed",
    ]
    assert [event.task.version for event in events] == [1, 2, 3, 4]
    assert {event.task.title for event in events} == {"Ship [REDACTED]"}
    assert {event.task.agent for event in events} == {"codex"}
    serialized = json.dumps([event.to_dict() for event in events])
    assert (
        "private" not in serialized
        and "hunter2" not in serialized
        and "Bearer abc" not in serialized
    )

    restarted = _reporter(tmp_path)
    assert restarted.tail_ledger_once() == 0
    assert len(restarted.pending_events()) == 4


def test_tail_consumes_real_runledger_stage_receipt_and_terminal_records(tmp_path: Path) -> None:
    ledger = RunLedger(tmp_path / "ledger.jsonl")
    ledger.append(
        LedgerEvent(
            "l1",
            "run-real",
            "run_created",
            {"run": {"intent": {"raw_text": "Real task"}, "executor_id": "codex"}},
        )
    )
    ledger.append(
        LedgerEvent(
            "l2",
            "run-real",
            "run_status_changed",
            {"status": "running", "detail": "stage: executing"},
        )
    )
    ledger.append(
        LedgerEvent(
            "l3", "run-real", "action_proposed", {"receipt": {"summary": "approval required"}}
        )
    )
    ledger.append(LedgerEvent("l4", "run-real", "run_finished", {"status": "cancelled"}))
    reporter = _reporter(tmp_path)
    assert reporter.tail_ledger_once() == 4
    assert [event.kind for event in reporter.pending_events()] == [
        "task.created",
        "task.started",
        "task.awaiting_approval",
        "task.cancelled",
    ]


def test_cursor_advances_only_after_spool_commit_and_handles_rotation(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text(
        json.dumps(_record("run_status_changed", {"status": "running"})) + "\n", encoding="utf-8"
    )
    reporter = _reporter(tmp_path)
    reporter._before_spool_commit = lambda: (_ for _ in ()).throw(RuntimeError("disk full"))
    try:
        reporter.tail_ledger_once()
    except RuntimeError:
        pass
    reporter._before_spool_commit = lambda: None
    assert reporter.status()["ledger_offset"] == 0
    assert reporter.tail_ledger_once() == 1

    ledger.rename(tmp_path / "ledger.old")
    ledger.write_text(
        json.dumps(_record("run_finished", {"status": "failed"}, event_id="new")) + "\n",
        encoding="utf-8",
    )
    assert reporter.tail_ledger_once() == 1
    assert [e.kind for e in reporter.pending_events()] == ["task.started", "task.failed"]


def test_rotated_ledger_overlap_does_not_mutate_task_version_for_duplicate_event_id(
    tmp_path: Path,
) -> None:
    ledger = tmp_path / "ledger.jsonl"
    record = _record("run_status_changed", {"status": "running"}, event_id="same")
    ledger.write_text(json.dumps(record) + "\n", encoding="utf-8")
    reporter = _reporter(tmp_path)
    assert reporter.tail_ledger_once() == 1
    assert reporter.pending_events()[0].task.version == 1
    ledger.rename(tmp_path / "old")
    ledger.write_text(json.dumps(record) + "\n", encoding="utf-8")
    assert reporter.tail_ledger_once() == 0
    assert reporter.pending_events()[0].task.version == 1


def test_flush_signs_exact_body_and_only_deletes_acknowledged_sequences(tmp_path: Path) -> None:
    transport = FakeTransport([TransportResponse(200, b'{"ack_sequence":1}')])
    reporter = _reporter(tmp_path, transport)
    reporter.enqueue(
        {
            "event_id": "external-1",
            "kind": "task.progress",
            "occurred_at": "2026-07-12T01:00:00Z",
            "task": {
                "task_id": "os-1",
                "title": "Bridge",
                "project": "osstack",
                "status": "running",
                "agent": "codex",
                "progress": 20,
                "duration_seconds": 4,
                "summary": "ok",
                "error_class": None,
                "evidence_ref": "osstack:1",
                "version": 1,
            },
        }
    )
    reporter.enqueue(
        {
            "event_id": "external-2",
            "kind": "task.progress",
            "occurred_at": "2026-07-12T01:00:01Z",
            "task": {
                "task_id": "os-2",
                "title": "Bridge 2",
                "project": "osstack",
                "status": "running",
                "agent": "codex",
                "progress": 30,
                "duration_seconds": 5,
                "summary": "ok",
                "error_class": None,
                "evidence_ref": "osstack:2",
                "version": 1,
            },
        }
    )
    assert reporter.flush_once() == 1
    _, _, body, headers = transport.requests[0]
    expected = hmac.new(
        b"top-secret", headers["X-OStag-Timestamp"].encode() + b"\n" + body, hashlib.sha256
    ).hexdigest()
    assert headers["X-OStag-Signature"] == expected
    assert [e.sequence for e in reporter.pending_events()] == [2]


def test_server_error_keeps_spool_and_records_backoff_across_restart(tmp_path: Path) -> None:
    reporter = _reporter(tmp_path, FakeTransport([TransportResponse(500, b"error")]))
    reporter.heartbeat()
    assert reporter.flush_once() == 0
    status = reporter.status()
    assert status["pending_events"] == 1 and status["failure_count"] == 1
    assert status["next_attempt_at"]
    assert _reporter(tmp_path).status()["failure_count"] == 1


def test_sequences_remain_monotonic_after_spool_is_fully_acknowledged(tmp_path: Path) -> None:
    transport = FakeTransport([TransportResponse(200, b'{"ack_sequence":1}')])
    reporter = _reporter(tmp_path, transport)
    assert reporter.heartbeat().sequence == 1
    assert reporter.flush_once() == 1
    assert reporter.heartbeat().sequence == 2


def test_duplicate_event_is_idempotent_without_sequence_gap(tmp_path: Path) -> None:
    reporter = _reporter(tmp_path, FakeTransport([TransportResponse(200, b'{"ack_sequence":1}')]))
    first = reporter.heartbeat()
    payload = first.to_dict()
    payload.pop("instance_id")
    payload.pop("sequence")
    duplicate = reporter.enqueue(payload)
    assert duplicate.sequence == first.sequence
    assert len(reporter.pending_events()) == 1
    assert reporter.status()["next_sequence"] == 2
    assert reporter.flush_once() == 1
    assert reporter.enqueue(payload).sequence == 1
    assert reporter.pending_events() == []


def test_malformed_or_out_of_range_2xx_ack_keeps_spool_and_backs_off(tmp_path: Path) -> None:
    for body in (b"not-json", b'{"ack_sequence":999}', b'{"ack_sequence":-1}'):
        path = tmp_path / str(abs(hash(body)))
        reporter = _reporter(path, FakeTransport([TransportResponse(200, body)]))
        reporter.heartbeat()
        assert reporter.flush_once() == 0
        assert reporter.status()["pending_events"] == 1
        assert reporter.status()["failure_count"] == 1


def test_poll_commands_delegates_and_acknowledges_results(tmp_path: Path) -> None:
    command = {
        "command_id": "c1",
        "task_id": "t1",
        "instance_id": "macbook",
        "action": "cancel",
        "expected_version": 1,
        "expected_lifecycle": "running",
        "requested_by": "u1",
        "issued_at": "2026-07-12T00:59:00Z",
        "expires_at": "2026-07-12T01:05:00Z",
    }
    transport = FakeTransport(
        [
            TransportResponse(200, json.dumps({"commands": [command]}).encode()),
            TransportResponse(200, b"{}"),
        ]
    )
    seen = []
    reporter = _reporter(tmp_path, transport)
    assert reporter.poll_commands_once(lambda cmd: seen.append(cmd) or {"status": "cancelled"}) == 1
    assert seen[0].command_id == "c1"
    assert json.loads(transport.requests[1][2])["result"]["status"] == "cancelled"


def test_command_ack_failure_is_persisted_and_destructive_action_not_reexecuted(
    tmp_path: Path,
) -> None:
    command = {
        "command_id": "c1",
        "task_id": "t1",
        "instance_id": "macbook",
        "action": "cancel",
        "expected_version": 1,
        "expected_lifecycle": "running",
        "requested_by": "u1",
        "issued_at": "2026-07-12T00:59:00Z",
        "expires_at": "2026-07-12T01:05:00Z",
    }
    transport = FakeTransport(
        [
            TransportResponse(200, json.dumps({"commands": [command]}).encode()),
            TransportResponse(500, b""),
            TransportResponse(200, json.dumps({"commands": [command]}).encode()),
            TransportResponse(200, b"{}"),
        ]
    )
    calls: list[str] = []
    reporter = _reporter(tmp_path, transport)
    assert (
        reporter.poll_commands_once(lambda c: calls.append(c.command_id) or {"status": "cancelled"})
        == 0
    )
    restarted = _reporter(tmp_path, transport)
    assert (
        restarted.poll_commands_once(
            lambda c: calls.append(c.command_id) or {"status": "cancelled"}
        )
        == 1
    )
    assert calls == ["c1"]
    assert restarted.command_states() == {"c1": "acked"}


def test_replayed_command_id_with_different_signed_payload_fails_closed(tmp_path: Path) -> None:
    original = {
        "command_id": "c1",
        "task_id": "t1",
        "instance_id": "macbook",
        "action": "cancel",
        "expected_version": 1,
        "expected_lifecycle": "running",
        "requested_by": "u1",
        "issued_at": "2026-07-12T00:59:00Z",
        "expires_at": "2026-07-12T01:05:00Z",
    }
    changed = {**original, "task_id": "t2"}
    transport = FakeTransport(
        [
            TransportResponse(200, json.dumps({"commands": [original]}).encode()),
            TransportResponse(500, b""),
            TransportResponse(200, json.dumps({"commands": [changed]}).encode()),
        ]
    )
    calls: list[str] = []
    reporter = _reporter(tmp_path, transport)
    assert (
        reporter.poll_commands_once(lambda c: calls.append(c.task_id) or {"status": "cancelled"})
        == 0
    )
    assert (
        reporter.poll_commands_once(lambda c: calls.append(c.task_id) or {"status": "cancelled"})
        == 0
    )
    assert calls == ["t1"]
    assert len(transport.requests) == 3


def test_unverified_execution_is_persisted_and_never_reexecutes_on_ack_retry(
    tmp_path: Path,
) -> None:
    command = {
        "command_id": "c1",
        "task_id": "t1",
        "instance_id": "macbook",
        "action": "cancel",
        "expected_version": 1,
        "expected_lifecycle": "running",
        "requested_by": "u1",
        "issued_at": "2026-07-12T00:59:00Z",
        "expires_at": "2026-07-12T01:05:00Z",
    }
    transport = FakeTransport(
        [
            TransportResponse(200, json.dumps({"commands": [command]}).encode()),
            TransportResponse(500, b""),
            TransportResponse(200, json.dumps({"commands": [command]}).encode()),
            TransportResponse(200, b"{}"),
        ]
    )
    calls: list[str] = []
    reporter = _reporter(tmp_path, transport)

    def execute(command: RemoteCommand) -> dict[str, object]:
        calls.append(command.command_id)
        return {"ok": False, "code": "CANCEL_NOT_CONFIRMED"}

    assert reporter.poll_commands_once(execute) == 0
    assert reporter.command_states() == {"c1": "executed_unverified"}
    assert reporter.poll_commands_once(execute) == 1
    assert calls == ["c1"]


def test_run_once_schedules_heartbeat_and_poll_at_independent_intervals(tmp_path: Path) -> None:
    now = [NOW]
    transport = FakeTransport(
        [TransportResponse(200, b'{"ack_sequence":1}'), TransportResponse(200, b'{"commands":[]}')]
    )
    reporter = Reporter(
        instance_id="macbook",
        secret="top-secret",
        central_url="https://monitor.example",
        ledger_path=tmp_path / "ledger",
        spool_path=tmp_path / "spool",
        transport=transport,
        clock=lambda: now[0],
        interval_seconds=30,
        command_poll_seconds=10,
        capabilities=lambda: frozenset({"cancel"}),
    )
    first = reporter.run_once(lambda _: {})
    assert first["heartbeat"] is True and first["polled"] is True
    upload = json.loads(transport.requests[0][2])
    assert upload["supported_actions"] == ["cancel"]
    assert reporter.run_once(lambda _: {})["polled"] is False


async def test_monitor_ingest_file_stdin_status_and_dry_run_example(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    event = Path("examples/work_monitor_event.json")
    spool = tmp_path / "spool.sqlite3"
    monkeypatch.setenv("OSTAG_MONITOR_INSTANCE_ID", "demo")
    monkeypatch.setenv("OSTAG_MONITOR_SPOOL_DB", str(spool))
    assert await main_async(["monitor", "ingest", "--file", str(event), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["queued"] is True and "secret" not in payload
    assert await main_async(["monitor", "reporter", "status", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["pending_events"] == 1

    stdin_payload = event.read_text(encoding="utf-8").replace("osstack-task-demo-v1", "stdin-event")
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin_payload))
    assert await main_async(["monitor", "ingest", "--stdin", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["sequence"] == 2


async def test_reporter_run_once_offline_and_start_missing_credentials_never_leak_secret(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.setenv("OSTAG_MONITOR_INSTANCE_ID", "demo")
    monkeypatch.setenv("OSTAG_MONITOR_INSTANCE_SECRET", "never-print-this")
    monkeypatch.setenv("OSTAG_MONITOR_SPOOL_DB", str(tmp_path / "spool"))
    assert await main_async(["monitor", "reporter", "run-once", "--json"]) == 0
    output = capsys.readouterr().out
    assert "never-print-this" not in output
    monkeypatch.setenv("OSTAG_MONITOR_ENABLED", "true")
    assert await main_async(["monitor", "reporter", "start", "--json"]) == 1
    output = capsys.readouterr().out
    assert "monitor_not_configured" in output and "never-print-this" not in output
