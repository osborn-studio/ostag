from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime
from pathlib import Path

from ostag.main import main_async
from ostag.work_monitor.reporter import Reporter, TransportResponse


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
    serialized = json.dumps([event.to_dict() for event in events])
    assert (
        "private" not in serialized
        and "hunter2" not in serialized
        and "Bearer abc" not in serialized
    )

    restarted = _reporter(tmp_path)
    assert restarted.tail_ledger_once() == 0
    assert len(restarted.pending_events()) == 4


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


def test_poll_commands_delegates_and_acknowledges_results(tmp_path: Path) -> None:
    command = {
        "command_id": "c1",
        "task_id": "t1",
        "instance_id": "macbook",
        "action": "cancel",
        "expected_status": "running",
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
