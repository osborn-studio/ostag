"""Fail-closed verification for Feishu card callbacks."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from fastapi import HTTPException, Request

from ostag.core.identity import Principal

from .identity import Destination
from .store import MonitorStore


@dataclass(frozen=True)
class VerifiedFeishuCallback:
    payload: dict[str, Any]
    principal: Principal | None
    destination: Destination | None
    kind: str


def feishu_signature(timestamp: str, nonce: str, encrypt_key: str, body: bytes) -> str:
    """Return the platform-defined SHA-256 callback signature over the raw body."""
    return hashlib.sha256(timestamp.encode() + nonce.encode() + encrypt_key.encode() + body).hexdigest()


def decrypt_feishu_payload(ciphertext: str, encrypt_key: str) -> bytes:
    """Decrypt a Feishu AES-256-CBC callback envelope."""
    try:
        from Crypto.Cipher import AES
        from Crypto.Util.Padding import unpad
    except ImportError as exc:  # supplied by the declared lark-oapi dependency
        raise RuntimeError("Feishu encrypted callbacks require the feishu extra") from exc
    try:
        encrypted = base64.b64decode(ciphertext, validate=True)
        key = hashlib.sha256(encrypt_key.encode()).digest()
        return unpad(AES.new(key, AES.MODE_CBC, iv=encrypted[:16]).decrypt(encrypted[16:]), 16)
    except (ValueError, TypeError) as exc:
        raise ValueError("invalid Feishu ciphertext") from exc


class FeishuCardSecurity:
    """Security dependency whose ordering is part of the public contract."""

    def __init__(
        self,
        *,
        encrypt_key: str,
        verification_token: str,
        store: MonitorStore,
        max_clock_skew_seconds: int = 300,
        clock: Callable[[], float] = time.time,
        decryptor: Callable[[str, str], bytes] = decrypt_feishu_payload,
    ) -> None:
        self.encrypt_key = encrypt_key
        self.verification_token = verification_token
        self.store = store
        self.max_clock_skew_seconds = max_clock_skew_seconds
        self.clock = clock
        self.decryptor = decryptor

    async def __call__(self, request: Request) -> VerifiedFeishuCallback:
        # 1. Capture the exact transport bytes and signature inputs.
        body = await request.body()
        timestamp = request.headers.get("X-Lark-Request-Timestamp", "")
        nonce = request.headers.get("X-Lark-Request-Nonce", "")
        supplied = request.headers.get("X-Lark-Signature", "")
        try:
            envelope = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise HTTPException(400, "invalid callback JSON") from exc
        if not isinstance(envelope, dict):
            raise HTTPException(400, "invalid callback envelope")
        ciphertext = envelope.get("encrypt")

        # 2. Verify the original signature before parsing/decrypting trusted content.
        if not self.encrypt_key:
            if ciphertext is not None:
                raise HTTPException(401, "encrypted callback requires FEISHU_ENCRYPT_KEY")
            raise HTTPException(401, "FEISHU_ENCRYPT_KEY is required for card callbacks")
        expected = feishu_signature(timestamp, nonce, self.encrypt_key, body)
        if not timestamp or not nonce or not hmac.compare_digest(supplied, expected):
            raise HTTPException(401, "invalid Feishu callback signature")

        # 3. Enforce freshness and persist replay state before decrypting.
        try:
            signed_at = int(timestamp)
        except ValueError as exc:
            raise HTTPException(401, "invalid Feishu callback timestamp") from exc
        now = int(self.clock())
        if abs(now - signed_at) > self.max_clock_skew_seconds:
            raise HTTPException(401, "stale Feishu callback")
        if not self.store.consume_instance_nonce(
            "feishu-card", nonce, now=now,
            expires_at=max(now, signed_at) + self.max_clock_skew_seconds,
        ):
            raise HTTPException(401, "replayed Feishu callback")

        # 4. Only authenticated ciphertext reaches the decryptor.
        if ciphertext is not None:
            if set(envelope) != {"encrypt"} or not isinstance(ciphertext, str) or not ciphertext:
                raise HTTPException(400, "invalid encrypted callback envelope")
            try:
                payload = json.loads(self.decryptor(ciphertext, self.encrypt_key))
            except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise HTTPException(400, "invalid encrypted callback") from exc
        else:
            payload = envelope

        verified = self._verify_payload(payload)
        request.state.feishu_callback = verified
        return verified

    def _verify_payload(self, payload: object) -> VerifiedFeishuCallback:
        if not isinstance(payload, dict):
            raise HTTPException(422, "callback payload must be an object")
        # URL verification is the sole non-action callback accepted here.
        if set(payload) == {"challenge", "token", "type"}:
            if payload.get("type") != "url_verification" or not self._token(payload.get("token")):
                raise HTTPException(401, "invalid verification token")
            challenge = payload.get("challenge")
            if not isinstance(challenge, str) or not challenge:
                raise HTTPException(422, "invalid challenge")
            return VerifiedFeishuCallback(dict(payload), None, None, "challenge")

        required = {"schema", "header", "event"}
        if set(payload) != required or payload.get("schema") != "2.0":
            raise HTTPException(422, "unsupported Feishu callback schema")
        header, event = payload.get("header"), payload.get("event")
        if not isinstance(header, dict) or not isinstance(event, dict):
            raise HTTPException(422, "invalid Feishu card action")
        allowed_header = {"event_id", "event_type", "create_time", "token", "app_id", "tenant_key"}
        if not set(header) <= allowed_header or header.get("event_type") != "card.action.trigger":
            raise HTTPException(422, "only card actions are accepted")
        if not self._token(header.get("token")):
            raise HTTPException(401, "invalid verification token")
        required_event = {"operator", "action", "context"}
        if not required_event <= set(event) or not set(event) <= required_event | {"token", "host"}:
            raise HTTPException(422, "invalid Feishu card action fields")
        operator, action, context = event["operator"], event["action"], event["context"]
        if not all(isinstance(item, Mapping) for item in (operator, action, context)):
            raise HTTPException(422, "invalid Feishu card action objects")
        if not set(operator) <= {"open_id", "user_id", "union_id", "tenant_key"}:
            raise HTTPException(422, "invalid card operator fields")
        if not set(action) <= {"tag", "value", "timezone"}:
            raise HTTPException(422, "invalid card action fields")
        if not set(context) <= {"open_message_id", "open_chat_id"}:
            raise HTTPException(422, "invalid card context fields")
        actor_id = operator.get("open_id")
        chat_id = context.get("open_chat_id")
        value = action.get("value")
        if not isinstance(actor_id, str) or not actor_id or not isinstance(chat_id, str) or not chat_id:
            raise HTTPException(422, "verified actor and chat are required")
        if action.get("tag") != "button" or not isinstance(value, dict):
            raise HTTPException(422, "invalid card button action")
        # Destination type is derived from the verified destination id, never button data.
        chat_type = "private" if chat_id.startswith("ou_") else "group"
        principal = Principal("feishu", actor_id)
        destination = Destination("feishu", chat_id, chat_type)
        return VerifiedFeishuCallback(dict(payload), principal, destination, "action")

    def _token(self, candidate: object) -> bool:
        return isinstance(candidate, str) and bool(self.verification_token) and hmac.compare_digest(
            candidate, self.verification_token
        )
