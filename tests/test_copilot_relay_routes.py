from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from ostag.config import Settings
from ostag.copilot_relay.models import RelayEnvelope
from ostag.copilot_relay.store import RelayStore
from ostag.core.identity import Principal
from ostag.work_monitor.identity import Destination, IdentityStore
from ostag.work_monitor.projection import Projector
from ostag.work_monitor.server import HumanCredential, ServerAuth, create_app
from ostag.work_monitor.service import WorkMonitorService
from ostag.work_monitor.store import MonitorStore


def _app(
    tmp_path: Path,
    *,
    max_body_bytes: int = 1_048_576,
    write_enabled: bool = True,
    canary_session_ids: tuple[str, ...] = (),
):
    store = MonitorStore(tmp_path / "monitor.sqlite3")
    identities = IdentityStore(store.path)
    destination = Destination("feishu", "owner-chat", "private")
    identities.bind_private(
        owner=Principal("feishu", "owner"), destination=destination, local_admin=True
    )
    service = WorkMonitorService(store, identities)
    return (
        create_app(
            settings=Settings(
                monitor_server_max_body_bytes=max_body_bytes,
                copilot_relay_write_enabled=write_enabled,
                copilot_relay_canary_session_ids=canary_session_ids,
            ),
            service=service,
            projector=Projector(store),
            auth=ServerAuth(
                {"macmini": "instance-secret", "other": "other-secret"},
                human_credentials={
                    "human-secret": HumanCredential(Principal("feishu", "owner"), destination)
                },
            ),
        ),
        store,
    )


def _envelope(
    sequence: int,
    *,
    kind: str = "session.upsert",
    message_id: str | None = None,
    instance_id: str = "macmini",
) -> dict[str, object]:
    payloads: dict[str, dict[str, object]] = {
        "session.upsert": {
            "agent_id": "codex-cli",
            "title": "Copilot Relay",
            "state": "running",
            "resume_supported": True,
            "latest_summary": f"turn {sequence}",
        },
        "assistant.snapshot": {
            "assistant_text": "需要负责人选择 A 或 B",
            "redaction_count": 0,
            "truncated": False,
        },
        "instruction.send": {"text": "继续"},
    }
    return {
        "schema_version": 1,
        "message_id": message_id or f"msg-{instance_id}-{sequence}-{kind}",
        "binding": {
            "instance_id": instance_id,
            "task_id": "task-1",
            "run_id": "run-1",
            "session_id": "session-1",
            "turn_id": f"turn-{sequence}",
            "session_version": sequence,
        },
        "sequence": sequence,
        "created_at": "2026-07-14T01:00:00Z",
        "expires_at": "2026-07-15T01:00:00Z",
        "sender": {"platform": "osstack", "actor_id": "reporter"},
        "kind": kind,
        "payload": payloads[kind],
        "correlation_id": None,
        "causation_id": None,
    }


def _signed(
    body: bytes,
    path: str,
    *,
    instance: str = "macmini",
    secret: str = "instance-secret",
    nonce: str = "nonce-1",
    timestamp: int | None = None,
) -> dict[str, str]:
    stamp = str(timestamp if timestamp is not None else int(datetime.now(UTC).timestamp()))
    canonical = "\n".join(
        ("v1", "POST", path, instance, stamp, nonce, hashlib.sha256(body).hexdigest())
    ).encode()
    return {
        "X-OStag-Instance": instance,
        "X-OStag-Timestamp": stamp,
        "X-OStag-Nonce": nonce,
        "X-OStag-Signature": hmac.new(secret.encode(), canonical, hashlib.sha256).hexdigest(),
        "content-type": "application/json",
    }


def _post(client: TestClient, path: str, payload: object, *, nonce: str = "nonce-1"):
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    return client.post(path, content=body, headers=_signed(body, path, nonce=nonce))


def test_events_are_strict_ordered_projected_and_idempotent(tmp_path: Path) -> None:
    app, store = _app(tmp_path)
    payload = {"events": [_envelope(1)]}
    with TestClient(app) as client:
        assert _post(client, "/v1/copilot/events", payload).json() == {"ack_sequence": 1}
        assert _post(client, "/v1/copilot/events", payload, nonce="nonce-2").json() == {
            "ack_sequence": 1
        }
        gap = _post(
            client, "/v1/copilot/events", {"events": [_envelope(3)]}, nonce="nonce-3"
        )
        assert gap.status_code == 409
        assert gap.json()["detail"] == {"expected_sequence": 2}
    session = RelayStore(store).get_session("macmini", "session-1")
    assert session is not None and session.latest_summary == "turn 1"


def test_events_reject_bad_auth_replay_wrong_instance_and_malformed_envelopes(
    tmp_path: Path,
) -> None:
    app, _ = _app(tmp_path)
    path = "/v1/copilot/events"
    body = json.dumps({"events": [_envelope(1)]}, separators=(",", ":")).encode()
    with TestClient(app) as client:
        bad = _signed(body, path) | {"X-OStag-Signature": "0" * 64}
        assert client.post(path, content=body, headers=bad).status_code == 401
        stale = _signed(body, path, nonce="stale", timestamp=1)
        assert client.post(path, content=body, headers=stale).status_code == 401
        headers = _signed(body, path, nonce="replay")
        assert client.post(path, content=body, headers=headers).status_code == 200
        assert client.post(path, content=body, headers=headers).status_code == 401

        wrong_body = json.dumps({"events": [_envelope(2, instance_id="other")]}, separators=(",", ":")).encode()
        assert (
            client.post(
                path,
                content=wrong_body,
                headers=_signed(wrong_body, path, nonce="wrong-binding"),
            ).status_code
            == 403
        )
        malformed = b'{"events":[{"schema_version":1}]}'
        assert (
            client.post(
                path, content=malformed, headers=_signed(malformed, path, nonce="malformed")
            ).status_code
            == 422
        )
        wrong_key = _signed(body, path, instance="other", secret="instance-secret", nonce="wrong-key")
        assert client.post(path, content=body, headers=wrong_key).status_code == 401


def test_relay_request_body_limit_is_enforced_before_parsing(tmp_path: Path) -> None:
    app, _ = _app(tmp_path, max_body_bytes=64)
    path = "/v1/copilot/events"
    body = json.dumps({"events": [_envelope(1)]}, separators=(",", ":")).encode()
    with TestClient(app) as client:
        response = client.post(
            path, content=body, headers=_signed(body, path, nonce="oversized")
        )
    assert response.status_code == 413


def test_poll_is_instance_scoped_leased_and_redelivered_after_expiry(tmp_path: Path) -> None:
    app, store = _app(tmp_path)
    relay = RelayStore(store)
    relay.enqueue(RelayEnvelope.from_dict(_envelope(1, kind="instruction.send")), direction="outbound")
    relay.enqueue(
        RelayEnvelope.from_dict(_envelope(1, kind="instruction.send", instance_id="other")),
        direction="outbound",
    )
    path = "/v1/copilot/messages/poll"
    with TestClient(app) as client:
        first = client.post(path, content=b"{}", headers=_signed(b"{}", path, nonce="poll-1"))
        assert [item["binding"]["instance_id"] for item in first.json()["messages"]] == [
            "macmini"
        ]
        empty = client.post(path, content=b"{}", headers=_signed(b"{}", path, nonce="poll-2"))
        assert empty.json() == {"messages": []}
        with store.transaction() as connection:
            connection.execute(
                "UPDATE relay_messages SET lease_until='2000-01-01T00:00:00Z' "
                "WHERE message_id=?",
                (first.json()["messages"][0]["message_id"],),
            )
        redelivered = client.post(
            path, content=b"{}", headers=_signed(b"{}", path, nonce="poll-3")
        )
        assert redelivered.json()["messages"] == first.json()["messages"]
        timeout = client.post(
            path + "?wait_seconds=0.01",
            content=b"{}",
            headers=_signed(b"{}", path, nonce="poll-4"),
        )
        assert timeout.status_code == 200 and timeout.json() == {"messages": []}


def test_read_only_mode_does_not_deliver_previously_queued_writes(tmp_path: Path) -> None:
    app, store = _app(tmp_path, write_enabled=False)
    relay = RelayStore(store)
    envelope = RelayEnvelope.from_dict(_envelope(1, kind="instruction.send"))
    relay.enqueue(envelope, direction="outbound")
    path = "/v1/copilot/messages/poll"

    with TestClient(app) as client:
        response = client.post(
            path,
            content=b"{}",
            headers=_signed(b"{}", path, nonce="read-only-poll"),
        )

    assert response.status_code == 200
    assert response.json() == {"messages": []}
    assert relay.envelope_for_message(envelope.message_id) == envelope


def test_canary_mode_only_leases_the_explicit_session(tmp_path: Path) -> None:
    app, store = _app(tmp_path, canary_session_ids=("session-canary",))
    relay = RelayStore(store)
    other = RelayEnvelope.from_dict(
        _envelope(1, kind="instruction.send", message_id="msg-other")
    )
    allowed_dict = _envelope(2, kind="instruction.send", message_id="msg-canary")
    allowed_dict["binding"]["session_id"] = "session-canary"
    allowed = RelayEnvelope.from_dict(allowed_dict)
    relay.enqueue(other, direction="outbound")
    relay.enqueue(allowed, direction="outbound")
    path = "/v1/copilot/messages/poll"

    with TestClient(app) as client:
        response = client.post(
            path, content=b"{}", headers=_signed(b"{}", path, nonce="canary-poll")
        )

    assert response.status_code == 200
    assert [item["message_id"] for item in response.json()["messages"]] == ["msg-canary"]
    assert relay.envelope_for_message("msg-other") == other


def test_ack_hash_mismatch_is_atomic_and_valid_ack_delivers(tmp_path: Path) -> None:
    app, store = _app(tmp_path)
    envelope = RelayEnvelope.from_dict(_envelope(1, kind="instruction.send"))
    RelayStore(store).enqueue(envelope, direction="outbound")
    poll_path = "/v1/copilot/messages/poll"
    ack_path = "/v1/copilot/messages/ack"
    canonical = json.dumps(envelope.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    valid_hash = hashlib.sha256(canonical.encode()).hexdigest()
    with TestClient(app) as client:
        client.post(poll_path, content=b"{}", headers=_signed(b"{}", poll_path, nonce="poll"))
        mismatch = _post(
            client,
            ack_path,
            {"acks": [{"message_id": envelope.message_id, "payload_hash": "0" * 64}]},
            nonce="ack-bad",
        )
        assert mismatch.status_code == 409
        accepted = _post(
            client,
            ack_path,
            {"acks": [{"message_id": envelope.message_id, "payload_hash": valid_hash}]},
            nonce="ack-good",
        )
        assert accepted.json() == {"acked": [envelope.message_id]}
    with sqlite3.connect(store.path) as connection:
        assert connection.execute(
            "SELECT state FROM relay_messages WHERE message_id=?", (envelope.message_id,)
        ).fetchone()[0] == "delivered"


def test_human_session_and_snapshot_reads_use_bearer_role_not_instance_secret(
    tmp_path: Path,
) -> None:
    app, store = _app(tmp_path)
    relay = RelayStore(store)
    session = RelayEnvelope.from_dict(_envelope(1))
    snapshot = RelayEnvelope.from_dict(_envelope(2, kind="assistant.snapshot"))
    relay.enqueue(session, direction="inbound")
    relay.upsert_session(session)
    relay.enqueue(snapshot, direction="inbound")
    human = {"Authorization": "Bearer human-secret"}
    with TestClient(app) as client:
        listed = client.get("/v1/copilot/sessions", headers=human)
        assert listed.status_code == 200
        assert listed.json()["sessions"][0]["session_id"] == "session-1"
        result = client.get(
            "/v1/copilot/sessions/macmini/session-1/snapshot", headers=human
        )
        assert result.json()["snapshot"]["payload"]["assistant_text"].startswith("需要")
        assert client.get("/v1/copilot/sessions").status_code == 401
        assert (
            client.get(
                "/v1/copilot/sessions",
                headers={"X-OStag-Instance": "macmini", "X-OStag-Signature": "anything"},
            ).status_code
            == 401
        )
