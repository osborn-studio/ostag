"""U01 private Feishu delivery observation CORE, with no network implementation.

Conceptual adaptation: amplifthq/opentag 3a3136d, provider-delivery/reconciliation.ts.
This is Python/SQLite semantics, not Slack proof. A05 must implement and certify
an actual Feishu adapter; D01 must separately verify platform acceptance.
No claim is made about Feishu uuid retention or lookup-by-uuid support.

One persisted outbox/revision freezes one deterministic UTF-8 TEXT envelope.
Its domain-separated digest binds namespace/platform/requester, original job,
revision and delivery IDs, resolved private destination and explicit provider
binding (tenant, installation, app, bot sender, credential generation). The JSON
draft is literal text for now; A05 owns rendering/escaping, never public fallback.
A hash/source/grant ID is not authority. Only exact replay coalesces pending work;
replaying started work returns history and NEVER authorizes another send.

Trusted internal callers provide WorkerContext and a mandatory bounded,
nonblocking, non-reentrant local authorization mirror. It checks CURRENT grant,
every source ACL and snapshot, destination ownership/privacy and provider binding
including generation. It must not access this store, call a network, or accept
endpoint/model approval flags. It runs after lock waits, as the final precommit
policy, immediately before egress, and before accepting observations. After the
final policy only clock/cancellation checks and commit are permitted under lock.
The adapter must invoke before_egress immediately at EACH actual send/read, after
any internal awaits, and must honor failures without wrapping or proceeding.

The async transport is trusted code, not an authentication credential or a
sandbox. No retries, redirects, fallback sends, text search, hidden network work,
or detached subtasks are allowed. send returns only a response correlated with
this exact attempt; candidate IDs cannot originate in requester/model payloads.
observe reads exactly the persisted message ID and returns the full original
body, target, tenant/installation/app/bot/generation and unedited/undeleted state.
A transport must verify its actual binding; echoing a requested binding is NOT
proof. Missing identity or full content never establishes provider acceptance.
An entirely lost response with no ID cannot be recovered automatically here.
Create acceptance is NEVER read-user evidence; human_received is always false.

A committed random single-use claim precedes send. Even a lost COMMIT response
before any IO permanently fences retransmission. No lease expiry, absent proof,
restart or cancellation reopens a started claim. Cancellation cancels local
intent, never unsends. started/unknown means pending verification after restart.
The initial send_unverified uncertainty (reason/time/digest) is committed with
claim BEFORE IO; even loss of all subsequent DB access cannot erase it. The first
post-send failure diagnostic is stored separately, immutable once recorded; a
failed diagnostic write leaves the initial marker, never a successful receipt.
Only the matching send owner/token may record send failures. A begin whose
COMMIT response is lost has no returned custody and retains send_unverified.
Each read budget unit has its own persisted owner/token/candidate/ordinal and
can settle once; a losing operation cannot diagnose or invalidate another's IO.
Up to 32 owned read budget records and 32 diagnostic observations are retained;
each full evidence is <=64KiB. Initial uncertainty and final receipt have separate
storage and survive exhausted observation capacity. No built-in polling loop.

Service operations share one finite monotonic deadline, reject UTC rollback,
and use nonwaiting SQLite attempts with bounded async contention waits. SQLite
execution/fsync and trusted synchronous code cannot be preempted: use this core
on an owned loop, not a shared HTTP loop. Cancellation/stop/timeout abandons async
transport without waiting for cancellation acknowledgement; its local slot stays
occupied until it really finishes. No threads/schedulers are created. The service
is event-loop affine; stop() is a thread-safe latch. Cancellation-resistant async
work cannot commit late proof. Synchronously blocking hostile code is outside the
trusted transport contract. Exceptions/diagnostics expose fixed codes only.

accepted/queued, generating and draft_ready remain generation states in A04;
pending/started/unknown/provider_accepted are independent delivery states. Private
detached snapshots require authenticated scope; repr/public_status omit content.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
import hashlib
import json
import math
import re
from threading import Event
from time import monotonic
from typing import Protocol

from ostag.briefs.store import (
    BriefStore, ClockError, Conflict, OutboxSnapshot, PrivateDestination,
    RequesterContext, StoreBusy, WorkerContext, _micros, _scope, _stamp, utc_now,
)

MAX_WIRE_BYTES = 16384
MAX_EVIDENCE_BYTES = 65536
MAX_OBSERVATIONS = 32
_ID = re.compile(r"[A-Za-z0-9_:.@/-]{1,256}\Z")
_MESSAGE_ID = re.compile(r"[A-Za-z0-9_-]{8,256}\Z")
REASONS = frozenset({"transport_error", "invalid_proof", "missing_proof", "timeout",
                     "cancelled", "stopped", "authorization_denied", "clock_error",
                     "store_error", "interrupted", "not_found"})


class DeliveryError(Conflict):
    """Fixed diagnostic only; private callback/provider exceptions are discarded."""


def _reason(error, fallback):
    # Never trust arbitrary exception args, even on a familiar domain type.
    if (type(error) is DeliveryError and len(error.args) == 1
            and type(error.args[0]) is str and error.args[0] in REASONS):
        return error.args[0]
    return fallback


def _id(value):
    if type(value) is not str or not _ID.fullmatch(value):
        raise DeliveryError("invalid_identity")
    return value


def _message_id(value):
    if type(value) is not str or not _MESSAGE_ID.fullmatch(value):
        raise DeliveryError("invalid_message_identity")
    return value


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False)


def _digest(domain, raw):
    return hashlib.sha256(domain.encode("ascii") + b"\x00" + raw).hexdigest()


def wire_text(text: str) -> bytes:
    """Deterministic local TEXT wire contract, not a verified HTTP request schema."""
    if type(text) is not str or len(text) > MAX_WIRE_BYTES:
        raise DeliveryError("invalid_wire_body")
    invalid = False
    try:
        raw = _json({"content": {"text": text}, "msg_type": "text"}).encode("utf-8")
    except UnicodeError:
        invalid = True
    if invalid or len(raw) > MAX_WIRE_BYTES:
        raise DeliveryError("invalid_wire_body")
    return raw


@dataclass(frozen=True)
class FeishuBinding:
    """Trusted installed provider identity; generation is a non-secret rotation counter."""
    tenant_id: str
    installation_id: str
    app_id: str
    bot_id: str
    generation: int

    def __post_init__(self):
        for value in (self.tenant_id, self.installation_id, self.app_id, self.bot_id):
            _id(value)
        if type(self.generation) is not int or not 1 <= self.generation <= 2**63 - 1:
            raise DeliveryError("invalid_binding_generation")

    def to_dict(self):
        return dict(tenant_id=self.tenant_id, installation_id=self.installation_id,
                    app_id=self.app_id, bot_id=self.bot_id, generation=self.generation)


@dataclass(frozen=True)
class DeliveryIntent:
    """Frozen private payload, constructed only from a trusted persisted outbox."""
    scope: RequesterContext
    job_id: str
    revision_id: str
    delivery_id: str
    destination: PrivateDestination
    binding: FeishuBinding
    wire_body: bytes = field(repr=False)

    def __post_init__(self):
        _scope(self.scope)
        for value in (self.job_id, self.revision_id, self.delivery_id):
            _id(value)
        if (self.scope.principal.platform != "feishu"
                or type(self.destination) is not PrivateDestination
                or self.destination.principal != self.scope.principal
                or type(self.binding) is not FeishuBinding):
            raise DeliveryError("invalid_private_binding")
        if type(self.wire_body) is not bytes or not 1 <= len(self.wire_body) <= MAX_WIRE_BYTES:
            raise DeliveryError("invalid_wire_body")
        # Serialization also rejects invalid UTF-8; exact canonical envelope is
        # verified against the original persisted revision at preparation.
        invalid = False
        try:
            self.wire_body.decode("utf-8")
        except UnicodeError:
            invalid = True
        if invalid:
            raise DeliveryError("invalid_wire_body")

    @classmethod
    def from_outbox(cls, scope, outbox: OutboxSnapshot, binding: FeishuBinding):
        return cls(scope, outbox.revision.job_id, outbox.revision.revision_id,
                   outbox.delivery_id, outbox.destination, binding,
                   wire_text(outbox.revision.payload_json))

    def to_dict(self):
        namespace, platform, actor = _scope(self.scope)
        return dict(namespace=namespace, platform=platform, actor_id=actor,
                    job_id=self.job_id, revision_id=self.revision_id, delivery_id=self.delivery_id,
                    destination=self.destination.address, binding=self.binding.to_dict(),
                    wire_body=self.wire_body.decode("utf-8"))

    @property
    def encoded(self):
        return _json(self.to_dict())

    @property
    def digest(self):
        return _digest("ostag/briefs/private-feishu-delivery/v1", self.encoded.encode("utf-8"))


@dataclass(frozen=True)
class SendAttempt:
    """Opaque one-claim capability; never serialize into public status or user input."""
    intent: DeliveryIntent = field(repr=False)
    token: str = field(repr=False)


@dataclass(frozen=True)
class ObservationAttempt:
    """One durable read budget capability, independent of the immutable send claim.

    Internal only: token, owner, candidate and ordinal are checked in SQLite at
    egress and settlement. Settlement consumes this unit even without proof;
    abandoned units remain spent across restart. No expiry or claim replacement.
    """
    intent: DeliveryIntent = field(repr=False)
    token: str = field(repr=False)
    message_id: str
    ordinal: int


@dataclass(frozen=True)
class ProviderMessage:
    """Full trusted exact-message evidence, never constructed from model text.

    These fields are the internal normalized proof contract, NOT claims about
    which fields Feishu exposes. An adapter unable to prove one must refuse it.
    """
    message_id: str
    binding: FeishuBinding
    destination: str
    wire_body: bytes = field(repr=False)
    chat_type: str
    deleted: bool
    edited: bool

    def __post_init__(self):
        _message_id(self.message_id)
        _id(self.destination)
        if (type(self.binding) is not FeishuBinding or type(self.chat_type) is not str
                or self.chat_type not in ("private", "group")
                or type(self.deleted) is not bool or type(self.edited) is not bool
                or type(self.wire_body) is not bytes
                or not 1 <= len(self.wire_body) <= MAX_WIRE_BYTES):
            raise DeliveryError("invalid_proof")
        invalid = False
        try:
            self.wire_body.decode("utf-8")
        except UnicodeError:
            invalid = True
        if invalid:
            raise DeliveryError("invalid_proof")

    def to_dict(self):
        return dict(message_id=self.message_id, binding=self.binding.to_dict(),
                    destination=self.destination, wire_body=self.wire_body.decode("utf-8"),
                    chat_type=self.chat_type, deleted=self.deleted, edited=self.edited)

    @classmethod
    def from_dict(cls, value):
        """Strict adapter parser; parsing establishes structure, NEVER authority."""
        valid = False
        try:
            if (type(value) is dict and set(value) == {
                "message_id", "binding", "destination", "wire_body", "chat_type", "deleted", "edited"
            } and type(value["binding"]) is dict and set(value["binding"]) == {
                "tenant_id", "installation_id", "app_id", "bot_id", "generation"
            } and type(value["wire_body"]) is str and len(value["wire_body"]) <= MAX_WIRE_BYTES):
                result = cls(**(value | {"binding": FeishuBinding(**value["binding"]),
                                        "wire_body": value["wire_body"].encode("utf-8")}))
                valid = True
        except (ValueError, TypeError, DeliveryError):
            pass
        if not valid:
            raise DeliveryError("invalid_proof")
        return result


@dataclass(frozen=True)
class SendResponse:
    """Trusted direct response correlated with exactly the supplied attempt token.

    A candidate ID may be retained without full proof; it authorizes exact-ID
    observation only. It never establishes acceptance by itself.
    """
    attempt_token: str = field(repr=False)
    message_id: str | None
    messages: tuple[ProviderMessage, ...] = field(default=(), repr=False)


class FeishuTransport(Protocol):
    async def send(self, attempt: SendAttempt, *, before_egress: Callable[[], None]) -> SendResponse:
        """Exactly one send, no retries/fallback; call the gate at actual egress."""
        ...

    async def observe(self, intent: DeliveryIntent, message_id: str, *,
                      before_egress: Callable[[], None]) -> tuple[ProviderMessage, ...]:
        """Exactly one exact-ID read, no search; call the gate at actual egress."""
        ...


@dataclass(frozen=True)
class Uncertainty:
    reason: str
    at: str
    evidence_digest: str


@dataclass(frozen=True)
class DeliverySnapshot:
    intent: DeliveryIntent = field(repr=False)
    state: str
    generation_state: str
    outbox_state: str
    created_at: str
    updated_at: str
    started_at: str | None
    candidate_id: str | None
    uncertainty: Uncertainty | None
    first_failure: Uncertainty | None
    receipt_json: str | None = field(repr=False)
    observations: tuple[str, ...] = field(repr=False)
    read_attempts: int

    @property
    def delivery_id(self):
        return self.intent.delivery_id

    @property
    def revision_id(self):
        return self.intent.revision_id

    @property
    def human_received(self):
        return False

    @property
    def public_status(self):
        return "delivery: " + ("pending_verification" if self.state in ("started", "unknown")
                               else self.state)


def receipt(intent, message_id, messages):
    """Return complete bounded evidence only for one exact original message."""
    if (type(messages) is not tuple or len(messages) != 1
            or type(messages[0]) is not ProviderMessage):
        return None
    message = messages[0]
    if (message.message_id != message_id or message.binding != intent.binding
            or message.destination != intent.destination.address
            or message.wire_body != intent.wire_body or message.chat_type != "private"
            or message.deleted or message.edited):
        return None
    encoded = _json(message.to_dict())
    return encoded if len(encoded.encode("utf-8")) <= MAX_EVIDENCE_BYTES else None


class _Operation:
    def __init__(self, service):
        self.service = service
        self.deadline = monotonic() + service.timeout
        self.last_us = 0
        self.cancelled = False
        self.owner = asyncio.current_task()

    def check(self):
        reason = None
        try:
            stamp = _micros(self.service.clock())
        except (Exception, asyncio.CancelledError):
            reason = "clock_error"
        if reason:
            raise DeliveryError(reason)
        if stamp < self.last_us:
            raise DeliveryError("clock_error")
        self.last_us = stamp
        if self.cancelled or self.owner.cancelling():
            # A child may reach egress before the cancelled parent has resumed
            # its exception handler and set our abandoned-operation latch.
            raise DeliveryError("cancelled")
        if self.service._stop.is_set():
            raise DeliveryError("stopped")
        if monotonic() >= self.deadline:
            raise DeliveryError("timeout")

    def clock(self):
        """Store's final clock also fences deadline/stop after policy returns."""
        self.check()
        return _stamp(self.last_us)


class DeliveryService:
    """Trusted loop-affine library; explicitly enable_delivery before using it.

    No constructor migration. Local max_concurrency (1..8) includes abandoned
    transport tasks. Multiple services/processes are fenced by SQLite, not this
    local cap. No raw connection or requester-supplied proof/message-ID API.
    """
    def __init__(self, *, store: BriefStore, worker: WorkerContext, transport: FeishuTransport,
                 authorize: Callable[[DeliveryIntent], None], clock=utc_now,
                 timeout: float = 5.0, max_concurrency: int = 1):
        if (not callable(authorize) or not callable(clock) or type(worker) is not WorkerContext
                or not callable(getattr(transport, "send", None))
                or not callable(getattr(transport, "observe", None))):
            raise DeliveryError("trusted_configuration_required")
        if (type(timeout) not in (int, float) or not math.isfinite(timeout)
                or not 0 < timeout <= 30 or type(max_concurrency) is not int
                or not 1 <= max_concurrency <= 8):
            raise DeliveryError("invalid_operation_bounds")
        self.store, self.worker, self.transport = store, worker, transport
        self.authorize, self.clock = authorize, clock
        self.timeout, self.max_concurrency = timeout, max_concurrency
        self._stop = Event()
        self._active = 0
        self._loop = None

    @property
    def active_operations(self):
        return self._active

    def stop(self):
        self._stop.set()

    def _enter(self):
        loop = asyncio.get_running_loop()
        if self._loop is not None and self._loop is not loop:
            raise DeliveryError("wrong_event_loop")
        self._loop = loop
        if self._stop.is_set():
            raise DeliveryError("stopped")
        if self._active >= self.max_concurrency:
            raise DeliveryError("capacity_exhausted")
        self._active += 1
        return _Operation(self)

    def _policy(self, op, intent):
        denied = False
        try:
            if self.authorize(intent) is not None:
                denied = True
        except (Exception, asyncio.CancelledError):
            denied = True
        if denied:
            raise DeliveryError("authorization_denied")
        op.check()

    async def _db(self, op, method, *args, **kwargs):
        while True:
            op.check()
            reason = None
            try:
                result = method(*args, **kwargs)
            except StoreBusy:
                reason = "busy"
            except DeliveryError as exc:
                reason = _reason(exc, "conflict")
            except ClockError:
                reason = "clock_error"
            except Conflict:
                reason = "conflict"
            except Exception:
                reason = "store_error"
            if reason == "busy":
                await asyncio.sleep(min(0.01, max(0, op.deadline - monotonic())))
                continue
            if reason:
                raise DeliveryError(reason)
            op.check()
            return result

    async def prepare(self, intent: DeliveryIntent) -> DeliverySnapshot:
        op = self._enter()
        try:
            return await self._db(op, self.store.prepare_delivery, self.worker, intent,
                                  clock=op.clock, precommit=lambda: self._policy(op, intent))
        finally:
            self._active -= 1

    async def send(self, scope: RequesterContext, delivery_id: str) -> DeliverySnapshot:
        return await self._execute(scope, delivery_id, sending=True)

    async def observe(self, scope: RequesterContext, delivery_id: str) -> DeliverySnapshot:
        return await self._execute(scope, delivery_id, sending=False)

    def _unknown(self, scope, delivery_id, reason, *, attempt, observation):
        # Best effort, nonwaiting, even after operation deadline. A failed write
        # leaves the durable started claim fenced; restart cannot retry the send.
        try:
            self.store.delivery_unknown(self.worker, scope, delivery_id, reason,
                                        token=attempt.token if attempt is not None else None,
                                        observation=observation, clock=self.clock)
        except Exception:
            pass

    async def _execute(self, scope, delivery_id, *, sending):
        op = self._enter()
        task = None
        attempt = None
        observation = None
        reason = None
        result = None
        try:
            snapshot = await self._db(op, self.store.delivery_snapshot, self.worker, scope,
                                      delivery_id)
            if snapshot is None:
                raise DeliveryError("conflict")
            intent = snapshot.intent
            # Reading historical scoped status is not authorization to send.
            if sending and snapshot.state != "pending":
                return snapshot
            if not sending and (snapshot.state not in ("started", "unknown")
                                or snapshot.candidate_id is None
                                or snapshot.read_attempts >= MAX_OBSERVATIONS):
                return snapshot
            def policy():
                self._policy(op, intent)
            if sending:
                attempt = await self._db(op, self.store.begin_delivery, self.worker, intent,
                                        clock=op.clock, precommit=policy)
                if attempt is None:
                    return await self._db(op, self.store.delivery_snapshot, self.worker,
                                          scope, delivery_id)
            else:
                observation = await self._db(
                    op, self.store.begin_delivery_observation, self.worker,
                    intent, clock=op.clock, precommit=policy)
                if observation is None:
                    return await self._db(op, self.store.delivery_snapshot, self.worker,
                                          scope, delivery_id)

            async def exchange():
                # This runs IN the scheduled coroutine, after all scheduling/DB
                # waits, not when the coroutine object is merely constructed.
                await self._db(op, self.store.check_delivery_egress, self.worker, intent,
                               token=attempt.token if sending else None, observation=observation,
                               clock=op.clock, precommit=policy)
                egress_used = False
                def egress():
                    nonlocal egress_used
                    if egress_used:
                        raise DeliveryError("transport_error")
                    egress_used = True
                    # Adapter may have awaited after entry: recheck persisted
                    # cancellation again, without waiting, then current policy.
                    self.store.check_delivery_egress(
                        self.worker, intent, token=attempt.token if sending else None,
                        observation=observation, clock=op.clock, precommit=policy)
                    policy()
                failure = None
                try:
                    if sending:
                        response = await self.transport.send(attempt, before_egress=egress)
                    else:
                        response = await self.transport.observe(
                            intent, observation.message_id, before_egress=egress)
                except DeliveryError as exc:
                    failure = _reason(exc, "transport_error")
                except asyncio.CancelledError:
                    failure = "cancelled"
                except Exception:
                    failure = "transport_error"
                if failure:
                    return failure, None
                if not egress_used:
                    return "invalid_proof", None
                return None, response

            task = asyncio.create_task(exchange())
            while not task.done():
                op.check()
                await asyncio.wait({task}, timeout=min(0.01, max(0, op.deadline - monotonic())))
            op.check()
            reason, response = task.result()
            if reason is None:
                if sending:
                    valid = (type(response) is SendResponse
                             and type(response.attempt_token) is str
                             and response.attempt_token == attempt.token)
                    candidate = None
                    if valid and response.message_id is not None:
                        try:
                            candidate = _message_id(response.message_id)
                        except DeliveryError:
                            valid = False
                    if not valid:
                        reason = "invalid_proof"
                    else:
                        result = await self._db(
                            op, self.store.accept_delivery_response, self.worker, attempt,
                            candidate, response.messages, clock=op.clock, precommit=policy)
                else:
                    result = await self._db(
                        op, self.store.accept_delivery_observation, self.worker, observation,
                        response, clock=op.clock, precommit=policy)
        except asyncio.CancelledError:
            reason = "cancelled"
        except DeliveryError as exc:
            reason = _reason(exc, "conflict")
        except Exception:
            reason = "store_error"
        finally:
            op.cancelled = True
            if task is not None and not task.done():
                task.cancel()
                def released(done):
                    # Retrieve failures without retaining/logging private traceback.
                    if not done.cancelled():
                        done.exception()
                    self._active -= 1
                task.add_done_callback(released)
            else:
                self._active -= 1
        if reason:
            if attempt is not None or observation is not None:
                self._unknown(scope, delivery_id, reason if reason in REASONS else "store_error",
                              attempt=attempt, observation=observation)
            if reason == "cancelled":
                raise asyncio.CancelledError()
            if reason in ("transport_error", "invalid_proof"):
                # A detached private status may be returned after a sanitized IO
                # failure. No extra network work and no blocking DB retry here.
                try:
                    result = self.store.delivery_snapshot(self.worker, scope, delivery_id)
                except Exception:
                    result = None
            if result is None:
                raise DeliveryError(reason)
        return result


def _snapshot(row, scope, generation_state, outbox_state, observations):
    """Materialize complete detached private state inside the store transaction."""
    from ostag.briefs.store import _stamp
    value = json.loads(row["intent_json"])
    intent = DeliveryIntent(scope, value["job_id"], value["revision_id"], value["delivery_id"],
                            PrivateDestination(scope.principal, value["destination"]),
                            FeishuBinding(**value["binding"]), value["wire_body"].encode("utf-8"))
    if intent.digest != row["intent_digest"]:
        raise DeliveryError("conflict")
    state = row["state"]
    if state == "pending" and outbox_state == "cancelled":
        state = "cancelled"
    uncertainty = (None if row["uncertainty_json"] is None else
                   Uncertainty(**json.loads(row["uncertainty_json"])))
    return DeliverySnapshot(intent, state, generation_state, outbox_state,
                            _stamp(row["created_us"]), _stamp(row["updated_us"]),
                            _stamp(row["started_us"]), row["candidate_id"], uncertainty,
                            None if row["first_failure_json"] is None else
                            Uncertainty(**json.loads(row["first_failure_json"])),
                            row["receipt_json"], observations, row["read_attempts"])


def _uncertainty(reason, stamp, intent_digest):
    evidence = _json(dict(reason=reason, at=stamp, intent_digest=intent_digest))
    return _json(dict(reason=reason, at=stamp, evidence_digest=_digest(
        "ostag/briefs/delivery-uncertainty/v1", evidence.encode("utf-8"))))
