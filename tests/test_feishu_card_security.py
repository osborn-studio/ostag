from __future__ import annotations

import hashlib
import json
import time

from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient

from ostag.core.identity import Principal
from ostag.work_monitor.feishu_card_security import (
    FeishuCardSecurity,
    decrypt_feishu_payload,
    feishu_signature,
)
from ostag.work_monitor.feishu_routes import create_feishu_card_router
from ostag.work_monitor.feishu_routes import issue_feishu_callback
from ostag.work_monitor.identity import Destination, IdentityStore
from ostag.work_monitor.models import MonitorEvent
from ostag.work_monitor.projection import Projector
from ostag.work_monitor.server import SecurityContract, register_feishu_card_router
from ostag.work_monitor.service import WorkMonitorService
from ostag.work_monitor.store import MonitorStore


def _headers(body: bytes, *, key: str = "enc-test", nonce: str = "nonce-fixed",
             timestamp: str = "1700000000") -> dict[str, str]:
    return {
        "X-Lark-Request-Timestamp": timestamp,
        "X-Lark-Request-Nonce": nonce,
        "X-Lark-Signature": feishu_signature(timestamp, nonce, key, body),
        "Content-Type": "application/json",
    }


def _app(security: FeishuCardSecurity) -> FastAPI:
    app = FastAPI()

    @app.post("/callback", dependencies=[Depends(security)])
    async def callback(request: Request):
        verified = request.state.feishu_callback
        return {"kind": verified.kind, "actor": getattr(verified.principal, "actor_id", "")}

    return app


def test_signature_and_decryption_fixed_vectors() -> None:
    body = b'{"encrypt":"ciphertext"}'
    assert feishu_signature("1700000000", "nonce-fixed", "enc-test", body) == (
        "99fa8ef5c278d891e7e19c219028a9304a0c9d09e1ef320255d5c0b791af0d64"
    )
    ciphertext = "AAECAwQFBgcICQoLDA0ODxwbggt+8Cqrzo9mVpkJ9zClJ87DUxbFIhqyvGOQQlYWwQ1JprJBVJ1RtUCLXHxVppVQBXY6obipP5hO8GpfwcI="
    assert json.loads(decrypt_feishu_payload(ciphertext, "enc-test")) == {
        "challenge": "ok", "token": "verify", "type": "url_verification"
    }


def test_official_legacy_url_verification_exact_shape_succeeds(tmp_path) -> None:
    plaintext = b'{"challenge":"official","token":"verify","type":"url_verification"}'
    security = FeishuCardSecurity(
        encrypt_key="enc-test", verification_token="verify", expected_app_id="app",
        store=MonitorStore(tmp_path / "monitor.sqlite3"), clock=lambda: 1_700_000_000,
        decryptor=lambda cipher, key: plaintext,
    )
    app = FastAPI()
    register_feishu_card_router(
        app, create_feishu_card_router(object()),
        security=SecurityContract("verified_platform", security),
    )
    body = b'{"encrypt":"ciphertext"}'
    with TestClient(app) as client:
        response = client.post("/webhooks/feishu/card-action", content=body,
                               headers=_headers(body))
    assert response.json() == {"challenge": "official"}

    extra = b'{"challenge":"x","token":"verify","type":"url_verification","extra":1}'
    other = FeishuCardSecurity(
        encrypt_key="enc-test", verification_token="verify", expected_app_id="app",
        store=MonitorStore(tmp_path / "extra.sqlite3"), clock=lambda: 1_700_000_000,
        decryptor=lambda cipher, key: extra,
    )
    with TestClient(_app(other)) as client:
        assert client.post("/callback", content=body,
                           headers=_headers(body, nonce="extra")).status_code == 422


def test_unsigned_setup_challenge_is_token_verified_but_actions_stay_fail_closed(tmp_path) -> None:
    security = FeishuCardSecurity(
        encrypt_key="enc-test", verification_token="verify", expected_app_id="app",
        store=MonitorStore(tmp_path / "monitor.sqlite3"),
    )
    app = FastAPI()
    register_feishu_card_router(
        app, create_feishu_card_router(object()),
        security=SecurityContract("verified_platform", security),
    )
    with TestClient(app) as client:
        challenge = client.post(
            "/webhooks/feishu/card-action",
            json={"challenge": "setup", "token": "verify", "type": "url_verification"},
        )
        assert challenge.status_code == 200
        assert challenge.json() == {"challenge": "setup"}
        encrypted = client.post(
            "/webhooks/feishu/card-action",
            json={
                "encrypt": "AAECAwQFBgcICQoLDA0ODxwbggt+8Cqrzo9mVpkJ9zClJ87DUxbFIhqyvGOQQlYWwQ1JprJBVJ1RtUCLXHxVppVQBXY6obipP5hO8GpfwcI="
            },
        )
        assert encrypted.status_code == 200
        assert encrypted.json() == {"challenge": "ok"}
        action = client.post(
            "/webhooks/feishu/card-action",
            json={"schema": "2.0", "header": {}, "event": {}},
        )
        assert action.status_code == 401


def test_unsigned_action_emits_safe_shape_and_token_diagnostics(tmp_path, caplog) -> None:
    security = FeishuCardSecurity(
        encrypt_key="TOP-SECRET-ENCRYPT-KEY",
        verification_token="TOP-SECRET-VERIFY-TOKEN",
        expected_app_id="TOP-SECRET-APP-ID",
        store=MonitorStore(tmp_path / "monitor.sqlite3"),
    )
    payload = {
        "schema": "2.0",
        "header": {
            "event_type": "card.action.trigger",
            "token": "TOP-SECRET-VERIFY-TOKEN",
            "app_id": "TOP-SECRET-APP-ID",
        },
        "event": {"secret_user_content": "TOP-SECRET-USER-CONTENT"},
    }

    with caplog.at_level("WARNING", logger="ostag.work_monitor.feishu_card_security"):
        with TestClient(_app(security)) as client:
            response = client.post("/callback", json=payload)

    assert response.status_code == 401
    diagnostic = caplog.text
    assert "feishu_callback_intake" in diagnostic
    assert '"signed_headers_complete": false' in diagnostic
    assert '"url_verification_shape": false' in diagnostic
    assert "feishu_unsigned_callback_rejected" in diagnostic
    assert '"json_object": true' in diagnostic
    assert '"encrypted_envelope": false' in diagnostic
    assert '"schema_v2": true' in diagnostic
    assert '"event_type": "card_action_trigger"' in diagnostic
    assert '"header_token_matches": true' in diagnostic
    assert '"header_app_id_matches": true' in diagnostic
    assert '"legacy_shape": false' in diagnostic
    for secret in (
        "TOP-SECRET-ENCRYPT-KEY", "TOP-SECRET-VERIFY-TOKEN",
        "TOP-SECRET-APP-ID", "TOP-SECRET-USER-CONTENT", "secret_user_content",
    ):
        assert secret not in diagnostic


def test_unsigned_parse_and_decrypt_failures_emit_transport_shape_only(tmp_path, caplog) -> None:
    security = FeishuCardSecurity(
        encrypt_key="TOP-SECRET-ENCRYPT-KEY",
        verification_token="TOP-SECRET-VERIFY-TOKEN",
        expected_app_id="TOP-SECRET-APP-ID",
        store=MonitorStore(tmp_path / "monitor.sqlite3"),
        decryptor=lambda cipher, key: (_ for _ in ()).throw(ValueError("secret failure")),
    )
    with caplog.at_level("WARNING", logger="ostag.work_monitor.feishu_card_security"):
        with TestClient(_app(security)) as client:
            invalid = client.post(
                "/callback", content=b"TOP-SECRET-NOT-JSON",
                headers={"Content-Type": "application/octet-stream", "Content-Encoding": "gzip"},
            )
            encrypted = client.post(
                "/callback", json={"encrypt": "TOP-SECRET-CIPHERTEXT"},
            )

    assert invalid.status_code == 401
    assert encrypted.status_code == 401
    diagnostic = caplog.text
    assert '"stage": "json_parse"' in diagnostic
    assert '"stage": "decrypt"' in diagnostic
    assert '"content_type": "application/octet-stream"' in diagnostic
    assert '"content_encoding": "gzip"' in diagnostic
    assert '"encrypted_envelope": true' in diagnostic
    assert '"encrypted_envelope": false' in diagnostic
    for secret in (
        "TOP-SECRET-NOT-JSON", "TOP-SECRET-CIPHERTEXT", "secret failure",
        "TOP-SECRET-ENCRYPT-KEY", "TOP-SECRET-VERIFY-TOKEN", "TOP-SECRET-APP-ID",
    ):
        assert secret not in diagnostic


def test_bad_signature_and_tamper_never_decrypt(tmp_path) -> None:
    calls: list[str] = []
    security = FeishuCardSecurity(
        encrypt_key="enc-test", verification_token="verify", expected_app_id="app",
        store=MonitorStore(tmp_path / "monitor.sqlite3"), clock=lambda: 1_700_000_000,
        decryptor=lambda cipher, key: calls.append(cipher) or b"{}",
    )
    body = b'{"encrypt":"ciphertext"}'
    with TestClient(_app(security)) as client:
        bad = _headers(body) | {"X-Lark-Signature": "bad"}
        assert client.post("/callback", content=body, headers=bad).status_code == 401
        tampered = b'{"encrypt":"changed"}'
        assert client.post("/callback", content=tampered, headers=_headers(body)).status_code == 401
    assert calls == []


def test_authenticated_envelope_logs_token_and_app_match_without_values(tmp_path, caplog) -> None:
    decrypted = json.dumps({
        "schema": "2.0",
        "header": {
            "event_type": "card.action.trigger",
            "token": "TOP-SECRET-WRONG-TOKEN",
            "app_id": "TOP-SECRET-APP-ID",
        },
        "event": {},
    }).encode()
    security = FeishuCardSecurity(
        encrypt_key="TOP-SECRET-ENCRYPT-KEY",
        verification_token="TOP-SECRET-VERIFY-TOKEN",
        expected_app_id="TOP-SECRET-APP-ID",
        store=MonitorStore(tmp_path / "monitor.sqlite3"),
        clock=lambda: 1_700_000_000,
        decryptor=lambda cipher, key: decrypted,
    )
    body = b'{"encrypt":"TOP-SECRET-CIPHERTEXT"}'

    with caplog.at_level("WARNING", logger="ostag.work_monitor.feishu_card_security"):
        with TestClient(_app(security)) as client:
            response = client.post(
                "/callback", content=body,
                headers=_headers(body, key="TOP-SECRET-ENCRYPT-KEY"),
            )

    assert response.status_code == 401
    diagnostic = caplog.text
    assert "feishu_signature_verified" in diagnostic
    assert '"timestamp_accepted": true' in diagnostic
    assert "feishu_authenticated_payload" in diagnostic
    assert '"schema_v2": true' in diagnostic
    assert '"event_type": "card_action_trigger"' in diagnostic
    assert '"token_matches": false' in diagnostic
    assert '"app_id_matches": true' in diagnostic
    for secret in (
        "TOP-SECRET-WRONG-TOKEN", "TOP-SECRET-VERIFY-TOKEN",
        "TOP-SECRET-APP-ID", "TOP-SECRET-ENCRYPT-KEY", "TOP-SECRET-CIPHERTEXT",
    ):
        assert secret not in diagnostic


def test_bad_signature_emits_safe_transport_diagnostics_only(tmp_path, caplog) -> None:
    body = b'{"encrypt":"TOP-SECRET-CIPHERTEXT"}'
    security = FeishuCardSecurity(
        encrypt_key="TOP-SECRET-ENCRYPT-KEY",
        verification_token="TOP-SECRET-VERIFY-TOKEN",
        expected_app_id="app",
        store=MonitorStore(tmp_path / "monitor.sqlite3"),
    )
    headers = {
        "X-Lark-Request-Timestamp": "1700000000-SECRET",
        "X-Lark-Request-Nonce": "TOP-SECRET-NONCE",
        "X-Lark-Signature": "TOP-SECRET-SIGNATURE",
        "Content-Type": "application/json; charset=utf-8",
        "Content-Encoding": "identity",
    }

    with caplog.at_level("WARNING", logger="ostag.work_monitor.feishu_card_security"):
        with TestClient(_app(security)) as client:
            response = client.post("/callback", content=body, headers=headers)

    assert response.status_code == 401
    diagnostic = caplog.text
    assert "feishu_signature_mismatch" in diagnostic
    assert '"timestamp_present": true' in diagnostic
    assert '"timestamp_length": 17' in diagnostic
    assert '"nonce_present": true' in diagnostic
    assert '"nonce_length": 16' in diagnostic
    assert '"signature_present": true' in diagnostic
    assert '"signature_length": 20' in diagnostic
    assert f'"body_length": {len(body)}' in diagnostic
    assert f'"body_sha256": "{hashlib.sha256(body).hexdigest()[:16]}"' in diagnostic
    assert '"content_encoding": "identity"' in diagnostic
    assert '"content_type": "application/json"' in diagnostic
    assert '"body_has_gzip_magic": false' in diagnostic
    assert '"verification_token_key_matches": false' in diagnostic
    for secret in (
        body.decode(), "TOP-SECRET-CIPHERTEXT", "1700000000-SECRET",
        "TOP-SECRET-NONCE", "TOP-SECRET-SIGNATURE", "TOP-SECRET-ENCRYPT-KEY",
        "TOP-SECRET-VERIFY-TOKEN",
    ):
        assert secret not in diagnostic


def test_signature_is_checked_before_json_and_real_router_has_no_side_effects(tmp_path) -> None:
    calls = {"decryptor": 0, "runner": 0, "service": 0, "receipt": 0}

    class SpyService:
        def propose_control(self, *args, **kwargs):
            calls["service"] += 1
            calls["receipt"] += 1
            raise AssertionError("service called")

    def decryptor(cipher: str, key: str) -> bytes:
        calls["decryptor"] += 1
        raise AssertionError("decryptor called")

    store = MonitorStore(tmp_path / "monitor.sqlite3")
    security = FeishuCardSecurity(
        encrypt_key="enc-test", verification_token="verify", expected_app_id="app", store=store,
        clock=lambda: 1_700_000_000, decryptor=decryptor,
    )
    app = FastAPI()
    register_feishu_card_router(
        app, create_feishu_card_router(SpyService()),
        security=SecurityContract("verified_platform", security),
    )
    with TestClient(app) as client:
        invalid_json = b"not-json"
        response = client.post("/webhooks/feishu/card-action", content=invalid_json,
                               headers=_headers(invalid_json) | {"X-Lark-Signature": "bad"})
        assert response.status_code == 401
        original = b'{"encrypt":"ciphertext"}'
        tampered = b'{"encrypt":"tampered"}'
        response = client.post("/webhooks/feishu/card-action", content=tampered,
                               headers=_headers(original, nonce="tamper"))
        assert response.status_code == 401
    assert calls == {"decryptor": 0, "runner": 0, "service": 0, "receipt": 0}


def test_replay_is_persisted_before_decrypt_and_encrypted_without_key_is_rejected(tmp_path) -> None:
    calls: list[str] = []
    body = b'{"encrypt":"ciphertext"}'
    security = FeishuCardSecurity(
        encrypt_key="enc-test", verification_token="verify", expected_app_id="app",
        store=MonitorStore(tmp_path / "monitor.sqlite3"), clock=lambda: 1_700_000_000,
        decryptor=lambda cipher, key: calls.append(cipher) or json.dumps({
            "schema": "2.0", "header": {
                "event_type": "url_verification", "token": "verify", "app_id": "app",
            }, "event": {"challenge": "ok"},
        }).encode(),
    )
    with TestClient(_app(security)) as client:
        assert client.post("/callback", content=body, headers=_headers(body)).status_code == 200
        assert client.post("/callback", content=body, headers=_headers(body)).status_code == 401
    assert calls == ["ciphertext"]
    no_key = FeishuCardSecurity(
        encrypt_key="", verification_token="verify", expected_app_id="app",
        store=MonitorStore(tmp_path / "no-key.sqlite3"), clock=lambda: 1_700_000_000,
        decryptor=lambda cipher, key: (_ for _ in ()).throw(AssertionError("decrypt called")),
    )
    with TestClient(_app(no_key)) as client:
        assert client.post("/callback", content=body, headers=_headers(body)).status_code == 401


def test_opaque_signed_timestamp_is_accepted_but_nonce_replay_is_rejected(tmp_path) -> None:
    body = b'{"encrypt":"ciphertext"}'
    security = FeishuCardSecurity(
        encrypt_key="enc-test", verification_token="verify", expected_app_id="app",
        store=MonitorStore(tmp_path / "monitor.sqlite3"), clock=lambda: 1_700_000_000,
        decryptor=lambda cipher, key: json.dumps({
            "schema": "2.0",
            "header": {
                "event_type": "url_verification", "token": "verify", "app_id": "app",
            },
            "event": {"challenge": "opaque-time-ok"},
        }).encode(),
    )
    headers = _headers(
        body, timestamp="2026-07-14T12:34:56.789Z", nonce="opaque-time-nonce"
    )

    with TestClient(_app(security)) as client:
        accepted = client.post("/callback", content=body, headers=headers)
        replayed = client.post("/callback", content=body, headers=headers)

    assert accepted.status_code == 200
    assert accepted.json() == {"kind": "challenge", "actor": ""}
    assert replayed.status_code == 401


def test_verified_payload_derives_principal_and_ignores_button_identity(tmp_path) -> None:
    payload = {
        "schema": "2.0",
        "header": {"event_id": "evt", "event_type": "card.action.trigger",
                   "token": "verify", "app_id": "app"},
        "event": {
            "operator": {"open_id": "ou_verified"},
            "context": {"open_chat_id": "oc_verified"},
            "action": {"tag": "button", "value": {
                "command": "pause", "task_id": "task-1",
                "actor": "ou_attacker", "chat_type": "private",
            }},
        },
    }
    body = json.dumps(payload, separators=(",", ":")).encode()
    security = FeishuCardSecurity(
        encrypt_key="enc-test", verification_token="verify", expected_app_id="app",
        store=MonitorStore(tmp_path / "monitor.sqlite3"), clock=lambda: 1_700_000_000,
    )
    with TestClient(_app(security)) as client:
        response = client.post("/callback", content=body, headers=_headers(body))
    assert response.json() == {"kind": "action", "actor": "ou_verified"}


def test_message_event_and_schema_extensions_are_rejected(tmp_path) -> None:
    payload = {"schema": "2.0", "header": {
        "event_type": "im.message.receive_v1", "token": "verify", "app_id": "app"}, "event": {}}
    body = json.dumps(payload, separators=(",", ":")).encode()
    security = FeishuCardSecurity(
        encrypt_key="enc-test", verification_token="verify", expected_app_id="app",
        store=MonitorStore(tmp_path / "monitor.sqlite3"), clock=lambda: 1_700_000_000,
    )
    with TestClient(_app(security)) as client:
        assert client.post("/callback", content=body, headers=_headers(body)).status_code == 422


def test_oversized_body_is_rejected_before_signature_decrypt_or_domain(tmp_path) -> None:
    calls: list[str] = []
    security = FeishuCardSecurity(
        encrypt_key="enc-test", verification_token="verify", expected_app_id="app",
        store=MonitorStore(tmp_path / "monitor.sqlite3"), max_body_bytes=8,
        decryptor=lambda cipher, key: calls.append(cipher) or b"{}",
    )
    body = b'{"encrypt":"ciphertext"}'
    with TestClient(_app(security)) as client:
        response = client.post("/callback", content=body, headers=_headers(body))
    assert response.status_code == 413
    assert calls == []


def test_wrong_verified_app_id_never_reaches_card_service(tmp_path) -> None:
    calls = {"decrypt": 0, "service": 0}
    decrypted = json.dumps({
        "schema": "2.0",
        "header": {"event_type": "card.action.trigger", "token": "verify",
                   "app_id": "attacker-app"},
        "event": {"operator": {"open_id": "ou_user"},
                  "context": {"open_chat_id": "oc_chat"},
                  "action": {"tag": "button", "value": {
                      "command": "pause", "task_id": "task-1"}}},
    }).encode()

    class Service:
        def propose_control(self, *args, **kwargs):
            calls["service"] += 1
            raise AssertionError("service called")

    security = FeishuCardSecurity(
        encrypt_key="enc-test", verification_token="verify", expected_app_id="app",
        store=MonitorStore(tmp_path / "monitor.sqlite3"), clock=lambda: 1_700_000_000,
        decryptor=lambda cipher, key: calls.__setitem__("decrypt", calls["decrypt"] + 1)
        or decrypted,
    )
    app = FastAPI()
    register_feishu_card_router(
        app, create_feishu_card_router(Service()),
        security=SecurityContract("verified_platform", security),
    )
    body = b'{"encrypt":"ciphertext"}'
    with TestClient(app) as client:
        response = client.post("/webhooks/feishu/card-action", content=body,
                               headers=_headers(body))
    assert response.status_code == 401
    assert calls == {"decrypt": 1, "service": 0}


def test_encrypted_opaque_control_proposes_then_confirms_exactly_once(tmp_path) -> None:
    store = MonitorStore(tmp_path / "monitor.sqlite3")
    identities = IdentityStore(store.path)
    principal = Principal("feishu", "ou_user")
    destination = Destination("feishu", "oc_chat", "group")
    identities.enroll_group(principal, destination, scope="all", local_admin=True)
    service = WorkMonitorService(store, identities)
    projector = Projector(store)
    task = {"task_id": "task-1", "title": "Ship", "project": "ostag",
            "status": "running", "agent": "codex", "progress": 20,
            "duration_seconds": 1, "summary": "", "error_class": None,
            "evidence_ref": None, "version": 1}
    projector.apply(MonitorEvent.from_dict({
        "event_id": "evt-1", "instance_id": "runner-1", "sequence": 1,
        "kind": "task.created", "occurred_at": "2026-07-13T00:00:00+00:00",
        "task": task, "per_task_capabilities": {},
    }))
    projector.apply(MonitorEvent.from_dict({
        "event_id": "evt-2", "instance_id": "runner-1", "sequence": 2,
        "kind": "instance.heartbeat", "occurred_at": "2026-07-13T00:00:01+00:00",
        "task": None, "per_task_capabilities": {"task-1": ["pause"]},
    }))
    initial = issue_feishu_callback(
        service, principal, destination, action="pause", task_id="task-1",
        now=int(time.time()),
    )
    payloads: dict[str, bytes] = {}

    def action(callback_id: str) -> bytes:
        return json.dumps({"schema": "2.0", "header": {
            "event_type": "card.action.trigger", "token": "verify", "app_id": "app",
        }, "event": {"operator": {"open_id": "ou_user"},
            "context": {"open_chat_id": "oc_chat"},
            "action": {"tag": "button", "value": {"callback_id": callback_id}}}},
            separators=(",", ":")).encode()

    payloads["first"] = action(initial)
    security = FeishuCardSecurity(
        encrypt_key="enc-test", verification_token="verify", expected_app_id="app",
        store=store, clock=lambda: 1_700_000_000,
        decryptor=lambda cipher, key: payloads[cipher],
    )
    app = FastAPI()
    register_feishu_card_router(
        app, create_feishu_card_router(service),
        security=SecurityContract("verified_platform", security),
    )

    def envelope(cipher: str, nonce: str) -> tuple[bytes, dict[str, str]]:
        body = json.dumps({"encrypt": cipher}, separators=(",", ":")).encode()
        return body, _headers(body, nonce=nonce)

    with TestClient(app) as client:
        body, headers = envelope("first", "first")
        first = client.post("/webhooks/feishu/card-action", content=body, headers=headers).json()
        with store.connect() as db:
            assert db.execute("SELECT COUNT(*) FROM commands").fetchone()[0] == 0
        confirm = first["card"]["elements"][0]["actions"][0]["value"]["callback_id"]
        payloads["confirm"] = action(confirm)
        body, headers = envelope("confirm", "confirm")
        assert client.post("/webhooks/feishu/card-action", content=body,
                           headers=headers).json()["toast"]["type"] == "success"
        with store.connect() as db:
            assert db.execute("SELECT COUNT(*) FROM commands").fetchone()[0] == 1
        body, headers = envelope("confirm", "replay")
        assert client.post("/webhooks/feishu/card-action", content=body,
                           headers=headers).json()["toast"]["type"] == "error"
        with store.connect() as db:
            assert db.execute("SELECT COUNT(*) FROM commands").fetchone()[0] == 1
