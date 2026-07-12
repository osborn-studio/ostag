from __future__ import annotations

import json

from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient

from ostag.work_monitor.feishu_card_security import (
    FeishuCardSecurity,
    decrypt_feishu_payload,
    feishu_signature,
)
from ostag.work_monitor.feishu_routes import create_feishu_card_router
from ostag.work_monitor.server import SecurityContract, register_feishu_card_router
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
