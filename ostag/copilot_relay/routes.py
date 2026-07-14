"""Authenticated central HTTP transport for Copilot Relay."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Request

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

    def stores() -> tuple[Any, Any, RelayStore]:
        service, auth = dependencies()
        return service, auth, RelayStore(service.store)

    @router.post("/events")
    async def events(request: Request) -> dict[str, int]:
        service, auth, relay = stores()
        body = await bounded_body(request)
        instance_id = auth.instance(request, body, service.store)
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
        service, auth, relay = stores()
        body = await bounded_body(request)
        instance_id = auth.instance(request, body, service.store)
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
        service, auth, relay = stores()
        body = await bounded_body(request)
        instance_id = auth.instance(request, body, service.store)
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
        service, auth, relay = stores()
        principal, _ = auth.human(request)
        scopes = service.identities.allowed_instances(principal)
        return {"sessions": [_session_dict(item) for item in relay.list_sessions(scopes)]}

    @router.get("/sessions/{instance_id}/{session_id}/snapshot")
    async def snapshot(request: Request, instance_id: str, session_id: str) -> dict[str, object]:
        service, auth, relay = stores()
        principal, _ = auth.human(request)
        scopes = service.identities.allowed_instances(principal)
        if scopes is not None and instance_id not in scopes:
            raise HTTPException(404, "session snapshot not found")
        envelope = relay.latest_snapshot(instance_id, session_id)
        if envelope is None:
            raise HTTPException(404, "session snapshot not found")
        return {"snapshot": envelope.to_dict()}

    return router
