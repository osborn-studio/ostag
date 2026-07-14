"""Fail-closed verification for Feishu card callbacks."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from fastapi import HTTPException, Request

from ostag.core.identity import Principal

from .identity import Destination
from .store import MonitorStore


logger = logging.getLogger(__name__)


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
        expected_app_id: str,
        store: MonitorStore | Callable[[], MonitorStore],
        max_body_bytes: int = 1_048_576,
        max_clock_skew_seconds: int = 300,
        clock: Callable[[], float] = time.time,
        decryptor: Callable[[str, str], bytes] = decrypt_feishu_payload,
    ) -> None:
        if not expected_app_id:
            raise ValueError("expected Feishu app_id is required")
        self.encrypt_key = encrypt_key
        self.verification_token = verification_token
        self.expected_app_id = expected_app_id
        self.store = store
        self.max_body_bytes = max_body_bytes
        self.max_clock_skew_seconds = max_clock_skew_seconds
        self.clock = clock
        self.decryptor = decryptor

    async def __call__(self, request: Request) -> VerifiedFeishuCallback:
        # 1. Capture the exact transport bytes and signature inputs.
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > self.max_body_bytes:
                    raise HTTPException(413, "request body too large")
            except ValueError as exc:
                raise HTTPException(400, "invalid content-length") from exc
        captured = bytearray()
        async for chunk in request.stream():
            captured.extend(chunk)
            if len(captured) > self.max_body_bytes:
                raise HTTPException(413, "request body too large")
        body = bytes(captured)
        timestamp = request.headers.get("X-Lark-Request-Timestamp", "")
        nonce = request.headers.get("X-Lark-Request-Nonce", "")
        supplied = request.headers.get("X-Lark-Signature", "")
        self._log_callback_intake(
            request, body=body, timestamp=timestamp, nonce=nonce, supplied=supplied
        )

        # Feishu's developer-console URL probe is sent before server callback mode
        # is activated and carries no signature headers. Permit only the exact,
        # token-authenticated setup challenge in that bootstrap state; actions and
        # partially signed requests remain fail-closed below.
        if not timestamp and not nonce and not supplied:
            encrypted_envelope = False
            try:
                bootstrap = json.loads(body)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                self._log_unsigned_transport_failure(
                    request, body=body, stage="json_parse", encrypted_envelope=False
                )
                raise HTTPException(401, "unsigned Feishu callback is not a setup challenge") from exc
            if isinstance(bootstrap, dict) and set(bootstrap) == {"encrypt"} \
                    and isinstance(bootstrap.get("encrypt"), str):
                encrypted_envelope = True
                try:
                    bootstrap = json.loads(
                        self.decryptor(bootstrap["encrypt"], self.encrypt_key)
                    )
                except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                    self._log_unsigned_transport_failure(
                        request, body=body, stage="decrypt", encrypted_envelope=True
                    )
                    raise HTTPException(
                        401, "unsigned Feishu callback is not a setup challenge"
                    ) from exc
            if not isinstance(bootstrap, dict) or set(bootstrap) != {
                "challenge", "token", "type",
            }:
                self._log_unsigned_callback_rejected(
                    bootstrap, body=body, encrypted_envelope=encrypted_envelope
                )
                raise HTTPException(401, "unsigned Feishu callback is not a setup challenge")
            verified = self._verify_payload(bootstrap)
            request.state.feishu_callback = verified
            return verified

        # 2. Verify the original signature before any JSON parsing or decryption.
        if not self.encrypt_key:
            raise HTTPException(401, "FEISHU_ENCRYPT_KEY is required for card callbacks")
        expected = feishu_signature(timestamp, nonce, self.encrypt_key, body)
        if not timestamp or not nonce or not hmac.compare_digest(supplied, expected):
            verification_expected = feishu_signature(
                timestamp, nonce, self.verification_token, body
            ) if self.verification_token else ""
            content_encoding = request.headers.get("content-encoding", "").lower().strip()
            if content_encoding not in {"identity", "gzip", "br", "deflate"}:
                content_encoding = "other" if content_encoding else "absent"
            content_type = request.headers.get("content-type", "").lower().split(";", 1)[0].strip()
            if content_type not in {"application/json", "application/octet-stream"}:
                content_type = "other" if content_type else "absent"
            diagnostic = {
                "body_has_gzip_magic": body.startswith(b"\x1f\x8b"),
                "body_length": len(body),
                "body_sha256": hashlib.sha256(body).hexdigest()[:16],
                "content_encoding": content_encoding,
                "content_type": content_type,
                "nonce_length": len(nonce),
                "nonce_present": bool(nonce),
                "signature_length": len(supplied),
                "signature_present": bool(supplied),
                "timestamp_length": len(timestamp),
                "timestamp_present": bool(timestamp),
                "transfer_encoding_present": "transfer-encoding" in request.headers,
                "verification_token_key_matches": bool(
                    supplied and verification_expected
                    and hmac.compare_digest(supplied, verification_expected)
                ),
            }
            logger.warning(
                "feishu_signature_mismatch %s",
                json.dumps(diagnostic, sort_keys=True),
            )
            raise HTTPException(401, "invalid Feishu callback signature")

        # 3. Enforce freshness and persist replay state before decrypting.
        # Feishu defines this header as an opaque part of the signed bytes; its
        # official SDK does not require a Unix-integer representation. Replay
        # retention is therefore based on trusted server receive time. Domain
        # actions remain protected by their own five-minute, one-use capability.
        now = int(self.clock())
        logger.warning(
            "feishu_signature_verified %s",
            json.dumps({"timestamp_accepted": bool(timestamp)}, sort_keys=True),
        )
        replay_store = self.store() if callable(self.store) else self.store
        nonce_accepted = replay_store.consume_instance_nonce(
            "feishu-card", nonce, now=now,
            expires_at=now + self.max_clock_skew_seconds,
        )
        logger.warning(
            "feishu_replay_check %s",
            json.dumps({"nonce_accepted": nonce_accepted}, sort_keys=True),
        )
        if not nonce_accepted:
            raise HTTPException(401, "replayed Feishu callback")

        # 4. Only authenticated and non-replayed bytes may be parsed/decrypted.
        try:
            envelope = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise HTTPException(400, "invalid callback JSON") from exc
        if not isinstance(envelope, dict):
            raise HTTPException(400, "invalid callback envelope")
        ciphertext = envelope.get("encrypt")
        if ciphertext is not None:
            if set(envelope) != {"encrypt"} or not isinstance(ciphertext, str) or not ciphertext:
                raise HTTPException(400, "invalid encrypted callback envelope")
            try:
                payload = json.loads(self.decryptor(ciphertext, self.encrypt_key))
            except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise HTTPException(400, "invalid encrypted callback") from exc
        else:
            payload = envelope

        self._log_authenticated_payload(payload)
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
        if not set(header) <= allowed_header:
            raise HTTPException(422, "invalid callback header")
        if not self._token(header.get("token")):
            raise HTTPException(401, "invalid verification token")
        app_id = header.get("app_id")
        if not isinstance(app_id, str) or not hmac.compare_digest(app_id, self.expected_app_id):
            raise HTTPException(401, "invalid callback app_id")
        if header.get("event_type") == "url_verification":
            if set(event) != {"challenge"} or not isinstance(event["challenge"], str) or not event["challenge"]:
                raise HTTPException(422, "invalid challenge")
            return VerifiedFeishuCallback(dict(payload), None, None, "challenge")
        if header.get("event_type") != "card.action.trigger":
            raise HTTPException(422, "only card actions are accepted")
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
        # Feishu open_chat_id values use the oc_ prefix for private and group
        # chats alike. Resolve the type from the server-owned authorization
        # binding; unknown destinations retain the stricter group posture.
        chat_type = self._destination_chat_type(chat_id)
        principal = Principal("feishu", actor_id)
        destination = Destination("feishu", chat_id, chat_type)
        return VerifiedFeishuCallback(dict(payload), principal, destination, "action")

    def _token(self, candidate: object) -> bool:
        return isinstance(candidate, str) and bool(self.verification_token) and hmac.compare_digest(
            candidate, self.verification_token
        )

    def _destination_chat_type(self, chat_id: str) -> str:
        binding_store = self.store() if callable(self.store) else self.store
        with binding_store.connect() as connection:
            table_exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='monitor_bindings'"
            ).fetchone()
            if table_exists is None:
                return "group"
            row = connection.execute(
                "SELECT chat_type FROM monitor_bindings "
                "WHERE platform='feishu' AND destination_id=? AND revoked=0",
                (chat_id,),
            ).fetchone()
        if row is not None and str(row["chat_type"]) in {"private", "group"}:
            return str(row["chat_type"])
        return "group"

    def _log_unsigned_callback_rejected(
        self, payload: object, *, body: bytes, encrypted_envelope: bool
    ) -> None:
        header = payload.get("header") if isinstance(payload, dict) else None
        header = header if isinstance(header, dict) else {}
        event_type = header.get("event_type")
        if event_type == "card.action.trigger":
            event_type_kind = "card_action_trigger"
        elif event_type == "url_verification":
            event_type_kind = "url_verification"
        else:
            event_type_kind = "other" if event_type is not None else "absent"
        legacy_shape = bool(
            isinstance(payload, dict)
            and {"open_id", "open_message_id", "action", "token"} <= set(payload)
        )
        diagnostic = {
            "body_length": len(body),
            "body_sha256": hashlib.sha256(body).hexdigest()[:16],
            "encrypted_envelope": encrypted_envelope,
            "event_type": event_type_kind,
            "header_app_id_matches": bool(
                header.get("app_id") and hmac.compare_digest(
                    str(header["app_id"]), self.expected_app_id
                )
            ),
            "header_token_matches": self._token(header.get("token")),
            "json_object": isinstance(payload, dict),
            "legacy_shape": legacy_shape,
            "legacy_token_matches": bool(
                isinstance(payload, dict) and self._token(payload.get("token"))
            ),
            "schema_v2": bool(
                isinstance(payload, dict) and payload.get("schema") == "2.0"
            ),
        }
        logger.warning(
            "feishu_unsigned_callback_rejected %s",
            json.dumps(diagnostic, sort_keys=True),
        )

    @staticmethod
    def _log_unsigned_transport_failure(
        request: Request, *, body: bytes, stage: str, encrypted_envelope: bool
    ) -> None:
        content_encoding = request.headers.get("content-encoding", "").lower().strip()
        if content_encoding not in {"identity", "gzip", "br", "deflate"}:
            content_encoding = "other" if content_encoding else "absent"
        content_type = request.headers.get("content-type", "").lower().split(";", 1)[0].strip()
        if content_type not in {"application/json", "application/octet-stream"}:
            content_type = "other" if content_type else "absent"
        diagnostic = {
            "body_has_gzip_magic": body.startswith(b"\x1f\x8b"),
            "body_length": len(body),
            "body_sha256": hashlib.sha256(body).hexdigest()[:16],
            "content_encoding": content_encoding,
            "content_type": content_type,
            "encrypted_envelope": encrypted_envelope,
            "stage": stage,
            "transfer_encoding_present": "transfer-encoding" in request.headers,
        }
        logger.warning(
            "feishu_unsigned_callback_transport_failure %s",
            json.dumps(diagnostic, sort_keys=True),
        )

    def _log_callback_intake(
        self,
        request: Request,
        *,
        body: bytes,
        timestamp: str,
        nonce: str,
        supplied: str,
    ) -> None:
        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            payload = None
        payload_dict = payload if isinstance(payload, dict) else {}
        header = payload_dict.get("header")
        header = header if isinstance(header, dict) else {}
        event_type = header.get("event_type")
        if event_type == "card.action.trigger":
            event_type_kind = "card_action_trigger"
        elif event_type == "url_verification":
            event_type_kind = "url_verification"
        else:
            event_type_kind = "other" if event_type is not None else "absent"
        content_encoding = request.headers.get("content-encoding", "").lower().strip()
        if content_encoding not in {"identity", "gzip", "br", "deflate"}:
            content_encoding = "other" if content_encoding else "absent"
        content_type = request.headers.get("content-type", "").lower().split(";", 1)[0].strip()
        if content_type not in {"application/json", "application/octet-stream"}:
            content_type = "other" if content_type else "absent"
        known_legacy_fields = {"open_id", "open_message_id", "action", "token"}
        diagnostic = {
            "body_has_gzip_magic": body.startswith(b"\x1f\x8b"),
            "body_length": len(body),
            "body_sha256": hashlib.sha256(body).hexdigest()[:16],
            "content_encoding": content_encoding,
            "content_type": content_type,
            "encrypted_envelope": set(payload_dict) == {"encrypt"},
            "event_type": event_type_kind,
            "header_app_id_matches": bool(
                header.get("app_id") and hmac.compare_digest(
                    str(header["app_id"]), self.expected_app_id
                )
            ),
            "header_token_matches": self._token(header.get("token")),
            "json_object": isinstance(payload, dict),
            "legacy_shape": known_legacy_fields <= set(payload_dict),
            "schema_v2": payload_dict.get("schema") == "2.0",
            "signed_headers_any": bool(timestamp or nonce or supplied),
            "signed_headers_complete": bool(timestamp and nonce and supplied),
            "top_level_token_matches": self._token(payload_dict.get("token")),
            "url_verification_shape": set(payload_dict) == {"challenge", "token", "type"},
            "v1_event_envelope": bool(payload_dict.get("uuid") and payload_dict.get("event")),
        }
        logger.warning("feishu_callback_intake %s", json.dumps(diagnostic, sort_keys=True))

    def _log_authenticated_payload(self, payload: object) -> None:
        payload_dict = payload if isinstance(payload, dict) else {}
        header = payload_dict.get("header")
        header = header if isinstance(header, dict) else {}
        event_type = header.get("event_type")
        if event_type == "card.action.trigger":
            event_type_kind = "card_action_trigger"
        elif event_type == "url_verification":
            event_type_kind = "url_verification"
        else:
            event_type_kind = "other" if event_type is not None else "absent"
        diagnostic = {
            "app_id_matches": bool(
                header.get("app_id") and hmac.compare_digest(
                    str(header["app_id"]), self.expected_app_id
                )
            ),
            "event_type": event_type_kind,
            "json_object": isinstance(payload, dict),
            "schema_v2": payload_dict.get("schema") == "2.0",
            "token_matches": self._token(header.get("token")),
        }
        logger.warning(
            "feishu_authenticated_payload %s",
            json.dumps(diagnostic, sort_keys=True),
        )
