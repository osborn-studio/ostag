"""Authenticated central HTTP transport for Copilot Relay."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from ostag.work_monitor.device_registry import DeviceRegistry, DeviceRegistryError

from .models import RelayEnvelope
from .store import MessageConflictError, RelaySession, RelayStore, StaleSessionError


def _utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _strict_object(body: bytes, keys: frozenset[str]) -> dict[str, Any]:
    try:
        value = json.loads(body or b"{}")
    except json.JSONDecodeError as exc:
        raise HTTPException(422, "malformed JSON body") from exc
    if not isinstance(value, dict) or set(value) != keys:
        raise HTTPException(422, f"body must contain exactly: {', '.join(sorted(keys))}")
    return value


def _session_dict(session: RelaySession) -> dict[str, object]:
    return {
        "instance_id": session.instance_id,
        "session_id": session.session_id,
        "task_id": session.task_id,
        "run_id": session.run_id,
        "agent_id": session.agent_id,
        "title": session.title,
        "state": session.state,
        "session_version": session.session_version,
        "last_turn_id": session.last_turn_id,
        "latest_summary": session.latest_summary,
        "updated_at": session.updated_at,
    }


def create_copilot_router(
    *,
    dependencies: Callable[[], tuple[Any, Any]],
    bounded_body: Callable[[Request], Awaitable[bytes]],
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    maximum_poll_seconds: float = 10.0,
    write_enabled: bool = True,
    canary_session_ids: frozenset[str] = frozenset(),
) -> APIRouter:
    """Create mixed instance/HMAC and human/Bearer relay routes."""
    router = APIRouter(prefix="/v1/copilot")

    def stores() -> tuple[Any, Any, RelayStore, DeviceRegistry]:
        service, auth = dependencies()
        return service, auth, RelayStore(service.store), DeviceRegistry(service.store)

    def owner(request: Request) -> tuple[Any, Any, DeviceRegistry]:
        service, auth, _, devices = stores()
        principal, destination = auth.human(request)
        if destination.chat_type != "private" or not service.identities.is_global_owner(principal):
            raise HTTPException(403, "private owner credentials required")
        return principal, destination, devices

    def gateway(request: Request, body: bytes) -> str:
        service, auth, _, devices = stores()
        device_id = request.headers.get("X-OStag-Device", "")
        if not device_id:
            return auth.instance(request, body, service.store)
        try:
            device = devices.authenticate(
                device_id=device_id,
                timestamp=request.headers.get("X-OStag-Timestamp", ""),
                nonce=request.headers.get("X-OStag-Nonce", ""),
                signature=request.headers.get("X-OStag-Signature", ""),
                method=request.method,
                path=request.url.path,
                body=body,
            )
        except DeviceRegistryError as exc:
            raise HTTPException(401, "invalid device credentials") from exc
        return device.device_id

    @router.post("/devices/pairing-code")
    async def pairing_code(request: Request) -> dict[str, object]:
        principal, _, devices = owner(request)
        body = await bounded_body(request)
        if body not in (b"", b"{}"):
            raise HTTPException(422, "pairing-code body must be empty")
        return {"pairing_code": devices.create_pairing_code(principal), "expires_in": 600}

    @router.post("/devices/pair")
    async def pair_device(request: Request) -> dict[str, object]:
        _, _, _, devices = stores()
        root = _strict_object(
            await bounded_body(request),
            frozenset({"pairing_code", "device_id", "display_name", "public_key"}),
        )
        if not all(isinstance(value, str) for value in root.values()):
            raise HTTPException(422, "pairing fields must be strings")
        try:
            device = devices.pair_device(
                pairing_code=root["pairing_code"],
                device_id=root["device_id"],
                display_name=root["display_name"],
                public_key=root["public_key"],
            )
        except DeviceRegistryError as exc:
            status = 409 if "pairing code" in str(exc) or "already paired" in str(exc) else 422
            raise HTTPException(status, str(exc)) from exc
        return {"paired": True, "device_id": device.device_id}

    @router.get("/devices")
    async def list_devices(request: Request) -> dict[str, object]:
        principal, _, devices = owner(request)
        return {"devices": [item.public_dict() for item in devices.list_devices(principal)]}

    @router.post("/devices/{device_id}/revoke")
    async def revoke_device(request: Request, device_id: str) -> dict[str, object]:
        principal, _, devices = owner(request)
        try:
            devices.revoke_device(principal, device_id)
        except DeviceRegistryError as exc:
            raise HTTPException(404, str(exc)) from exc
        return {"revoked": True, "device_id": device_id}

    @router.post("/devices/{device_id}/rotate-key")
    async def rotate_device_key(request: Request, device_id: str) -> dict[str, object]:
        body = await bounded_body(request)
        authenticated = gateway(request, body)
        if authenticated != device_id:
            raise HTTPException(403, "device route does not match authenticated device")
        root = _strict_object(body, frozenset({"public_key"}))
        if not isinstance(root["public_key"], str):
            raise HTTPException(422, "public_key must be a string")
        _, _, _, devices = stores()
        try:
            devices.rotate_key(device_id, new_public_key=root["public_key"])
        except DeviceRegistryError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {"rotated": True, "device_id": device_id}

    @router.post("/events")
    async def events(request: Request) -> dict[str, int]:
        service, _, relay, _ = stores()
        body = await bounded_body(request)
        instance_id = gateway(request, body)
        root = _strict_object(body, frozenset({"events"}))
        raw_events = root["events"]
        if not isinstance(raw_events, list) or not 1 <= len(raw_events) <= 100:
            raise HTTPException(422, "events must contain between 1 and 100 envelopes")
        try:
            envelopes = tuple(RelayEnvelope.from_dict(item) for item in raw_events)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        if any(item.binding.instance_id != instance_id for item in envelopes):
            raise HTTPException(403, "envelope instance does not match authenticated instance")

        cursor = relay.instance_cursor(instance_id)
        fresh = [item for item in envelopes if item.sequence > cursor]
        expected = cursor + 1
        if fresh and [item.sequence for item in fresh] != list(
            range(expected, expected + len(fresh))
        ):
            raise HTTPException(409, {"expected_sequence": expected})
        try:
            for envelope in envelopes:
                if envelope.sequence <= cursor:
                    stored = relay.envelope_for_message(envelope.message_id)
                    if stored is None or stored.to_dict() != envelope.to_dict():
                        raise MessageConflictError("historical sequence does not match stored event")
                    continue
                relay.enqueue(envelope, direction="inbound")
                if envelope.kind == "session.upsert":
                    relay.upsert_session(envelope)
                elif envelope.kind == "decision.request":
                    relay.record_decision(envelope)
            new_cursor = cursor if not fresh else fresh[-1].sequence
            if new_cursor != cursor and not relay.advance_instance_cursor(
                instance_id, cursor, new_cursor, updated_at=_utc(clock())
            ):
                raise MessageConflictError("event cursor changed concurrently")
        except (MessageConflictError, StaleSessionError) as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"ack_sequence": new_cursor}

    @router.post("/messages/poll")
    async def poll(request: Request) -> dict[str, list[dict[str, object]]]:
        _, _, relay, _ = stores()
        body = await bounded_body(request)
        instance_id = gateway(request, body)
        if body not in (b"", b"{}"):
            raise HTTPException(422, "poll body must be empty")
        if not write_enabled:
            return {"messages": []}
        try:
            wait_seconds = min(
                maximum_poll_seconds, max(0.0, float(request.query_params.get("wait_seconds", "0")))
            )
        except ValueError as exc:
            raise HTTPException(422, "wait_seconds must be numeric") from exc
        deadline = asyncio.get_running_loop().time() + wait_seconds
        while True:
            now = clock()
            messages = relay.claim_messages(
                direction="outbound",
                owner=instance_id,
                instance_id=instance_id,
                session_ids=canary_session_ids or None,
                now=_utc(now),
                lease_until=_utc(now + timedelta(seconds=30)),
                limit=100,
            )
            if messages or asyncio.get_running_loop().time() >= deadline:
                return {"messages": [item.envelope.to_dict() for item in messages]}
            await asyncio.sleep(min(0.1, max(0.0, deadline - asyncio.get_running_loop().time())))

    @router.post("/messages/ack")
    async def acknowledge(request: Request) -> dict[str, list[str]]:
        _, _, relay, _ = stores()
        body = await bounded_body(request)
        instance_id = gateway(request, body)
        root = _strict_object(body, frozenset({"acks"}))
        raw = root["acks"]
        if not isinstance(raw, list) or not 1 <= len(raw) <= 100:
            raise HTTPException(422, "acks must contain between 1 and 100 items")
        parsed: list[tuple[str, str]] = []
        for item in raw:
            if (
                not isinstance(item, dict)
                or set(item) != {"message_id", "payload_hash"}
                or not isinstance(item["message_id"], str)
                or not item["message_id"]
                or not isinstance(item["payload_hash"], str)
                or len(item["payload_hash"]) != 64
            ):
                raise HTTPException(422, "each acknowledgement requires message_id and SHA-256 hash")
            parsed.append((item["message_id"], item["payload_hash"]))
        try:
            acked = relay.acknowledge_messages(
                tuple(parsed), owner=instance_id, delivered_at=_utc(clock())
            )
        except MessageConflictError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"acked": list(acked)}

    @router.get("/sessions")
    async def sessions(request: Request) -> dict[str, list[dict[str, object]]]:
        service, auth, relay, _ = stores()
        principal, _ = auth.human(request)
        scopes = service.identities.allowed_instances(principal)
        return {"sessions": [_session_dict(item) for item in relay.list_sessions(scopes)]}

    @router.get("/sessions/{instance_id}/{session_id}/snapshot")
    async def snapshot(request: Request, instance_id: str, session_id: str) -> dict[str, object]:
        service, auth, relay, _ = stores()
        principal, _ = auth.human(request)
        scopes = service.identities.allowed_instances(principal)
        if scopes is not None and instance_id not in scopes:
            raise HTTPException(404, "session snapshot not found")
        envelope = relay.latest_snapshot(instance_id, session_id)
        if envelope is None:
            raise HTTPException(404, "session snapshot not found")
        return {"snapshot": envelope.to_dict()}

    return router
