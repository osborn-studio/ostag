"""U01 synthetic Feishu contract tests; no HTTP or human receipt claims."""
import asyncio
from dataclasses import replace
import json
import sqlite3
from time import sleep

import pytest

from ostag.briefs.delivery import (
    DeliveryIntent, DeliveryService, FeishuBinding, ProviderMessage, SendResponse,
)
from ostag.briefs.store import BriefStore, Conflict, WorkerContext
from ostag.briefs.worker import BriefWorker
from test_brief_service import Authority, SCOPE, event

BINDING = FeishuBinding("tenant-1", "installation-1", "app-1", "bot-1", 1)
WORKER = WorkerContext("delivery-tests")
MESSAGE_ID = "om_message_00000001"


@pytest.fixture
def ready(tmp_path):
    authority = Authority()
    briefs = authority.service(BriefStore(tmp_path / "brief.db"))
    worker = BriefWorker(briefs)
    admitted = briefs.submit(event())
    assert worker.run_once().result(2) == "draft_ready"
    worker.stop()
    box = briefs.store.outbox(SCOPE, admitted.job_id)
    yield briefs, authority, DeliveryIntent.from_outbox(SCOPE, box, BINDING)
    briefs.close()


class Transport:
    def __init__(self):
        self.sends = []
        self.reads = []
        self.send_effect = None
        self.read_effect = None

    async def send(self, attempt, *, before_egress):
        before_egress()
        self.sends.append(attempt)
        if self.send_effect:
            return await self.send_effect(attempt)
        return SendResponse(attempt.token, MESSAGE_ID, (proof(attempt.intent),))

    async def observe(self, intent, message_id, *, before_egress):
        before_egress()
        self.reads.append(message_id)
        if self.read_effect:
            return await self.read_effect(intent)
        return (proof(intent),)


def proof(intent, **changes):
    return ProviderMessage(**(dict(message_id=MESSAGE_ID, binding=intent.binding,
        destination=intent.destination.address, wire_body=intent.wire_body,
        chat_type="private", deleted=False, edited=False) | changes))


def delivery(ready, transport=None, authorize=None, **kwargs):
    briefs, _, _ = ready
    return DeliveryService(store=briefs.store, worker=WORKER, transport=transport or Transport(),
                           authorize=authorize or (lambda intent: None), clock=briefs.clock,
                           **kwargs)


async def test_journey_and_exact_replay(ready):
    briefs, _, intent = ready
    briefs.store.enable_delivery()
    service = delivery(ready)
    accepted = await service.prepare(intent)
    assert accepted.state == "pending"
    assert await service.prepare(intent) == accepted
    result = await service.send(SCOPE, intent.delivery_id)
    assert result.state == "provider_accepted"
    assert result.generation_state == "draft_ready"
    assert result.human_received is False
    assert json.loads(result.receipt_json)["wire_body"].encode() == intent.wire_body
    assert briefs.store.outbox(SCOPE, intent.job_id).state == "pending"
    assert await service.send(SCOPE, intent.delivery_id) == result
    assert len(service.transport.sends) == 1
    with pytest.raises(Conflict):
        await service.prepare(replace(intent, binding=replace(BINDING, generation=2)))


@pytest.mark.parametrize("enable_before_service", [False, True])
async def test_schema4_generation_cost_and_delivery_journey(tmp_path, enable_before_service):
    authority = Authority()
    store = BriefStore(tmp_path / "brief.db")
    if enable_before_service:
        store.enable_delivery()
        store = BriefStore(store.path)
    briefs = authority.service(store)
    if not enable_before_service:
        store.enable_delivery()
    worker = BriefWorker(briefs)
    try:
        admitted = briefs.submit(event())
        assert admitted.status == "queued"
        started_costs = []

        async def inspect_started_cost(_):
            cost = store.cost_record(SCOPE, admitted.job_id, "manual-generation-1")
            assert cost.state == "started"
            assert cost.charged_micros == authority.config.parameters.budget.reservation_micros
            assert store.has_uncertain_cost(WORKER)
            started_costs.append(cost)

        briefs.transport.effect = inspect_started_cost
        assert await asyncio.wrap_future(worker.run_once()) == "draft_ready"
        assert not worker.quarantined
        assert len(started_costs) == len(briefs.transport.calls) == 1
        cost = store.cost_record(SCOPE, admitted.job_id, "manual-generation-1")
        assert cost.reservation_id == started_costs[0].reservation_id
        assert cost.state == "settled" and cost.charged_micros == 200
        assert not store.has_uncertain_cost(WORKER)

        box = store.outbox(SCOPE, admitted.job_id)
        intent = DeliveryIntent.from_outbox(SCOPE, box, BINDING)
        service = delivery((briefs, authority, intent))
        assert (await service.prepare(intent)).state == "pending"
        result = await service.send(SCOPE, intent.delivery_id)
        assert result.state == "provider_accepted" and not result.human_received
        assert result.generation_state == "draft_ready" and result.outbox_state == "pending"

        reopened = BriefStore(store.path)
        assert reopened.cost_record(SCOPE, admitted.job_id, "manual-generation-1") == cost
        assert not reopened.has_uncertain_cost(WORKER)
        assert reopened.delivery_snapshot(WORKER, SCOPE, intent.delivery_id) == result
    finally:
        worker.stop()
        briefs.close()


async def test_lost_response_without_id_never_reads_or_resends(ready):
    ready[0].store.enable_delivery()
    transport = Transport()
    async def lost(_):
        raise RuntimeError("PRIVATE provider response")
    transport.send_effect = lost
    service = delivery(ready, transport)
    await service.prepare(ready[2])
    result = await service.send(SCOPE, ready[2].delivery_id)
    assert result.state == "unknown" and result.first_failure.reason == "transport_error"
    for _ in range(3):
        assert (await service.observe(SCOPE, ready[2].delivery_id)).state == "unknown"
        assert (await service.send(SCOPE, ready[2].delivery_id)).state == "unknown"
    assert len(transport.sends) == 1 and transport.reads == []
    with sqlite3.connect(ready[0].store.path) as db:
        assert "PRIVATE" not in " ".join(db.iterdump())


async def candidate_ready(ready, **kwargs):
    ready[0].store.enable_delivery()
    service = delivery(ready, **kwargs)
    async def partial(attempt):
        return SendResponse(attempt.token, MESSAGE_ID)
    service.transport.send_effect = partial
    await service.prepare(ready[2])
    pending = await service.send(SCOPE, ready[2].delivery_id)
    assert pending.candidate_id == MESSAGE_ID and pending.state == "unknown"
    return service, pending


async def test_exact_candidate_lookup_preserves_first_uncertainty(ready):
    service, initial = await candidate_ready(ready)
    result = await service.observe(SCOPE, ready[2].delivery_id)
    assert result.state == "provider_accepted"
    assert result.uncertainty == initial.uncertainty
    assert result.first_failure == initial.first_failure
    assert result.observations == initial.observations
    reopened = BriefStore(ready[0].store.path)
    assert reopened.delivery_snapshot(WORKER, SCOPE, ready[2].delivery_id) == result
    assert service.transport.reads == [MESSAGE_ID]
    assert len(service.transport.sends) == 1


@pytest.mark.parametrize("change", ["destination", "tenant", "installation", "app", "sender",
                                   "generation", "body", "id", "group", "edited", "deleted",
                                   "multiple", "malformed", "empty", "error"])
async def test_same_text_is_not_proof_of_delivery(ready, change):
    service, initial = await candidate_ready(ready)
    intent = ready[2]
    changes = {
        "destination": dict(destination="someone-elses-private-chat"),
        "tenant": dict(binding=replace(BINDING, tenant_id="other")),
        "installation": dict(binding=replace(BINDING, installation_id="other")),
        "app": dict(binding=replace(BINDING, app_id="other")),
        "sender": dict(binding=replace(BINDING, bot_id="other")),
        "generation": dict(binding=replace(BINDING, generation=2)),
        "body": dict(wire_body=intent.wire_body[:-1] + b" "),
        "id": dict(message_id="om_different_000001"),
        "group": dict(chat_type="group"), "edited": dict(edited=True),
        "deleted": dict(deleted=True),
    }
    async def read(_):
        if change == "multiple":
            return (proof(intent), proof(intent))
        if change == "malformed":
            return {"verified": True, "hash": intent.digest}
        if change == "empty":
            return ()
        if change == "error":
            raise RuntimeError("PRIVATE 429 notfound provider error")
        return (proof(intent, **changes[change]),)
    service.transport.read_effect = read
    result = await service.observe(SCOPE, intent.delivery_id)
    assert result.state == "unknown" and result.receipt_json is None
    assert result.uncertainty == initial.uncertainty
    assert result.first_failure == initial.first_failure
    assert len(service.transport.sends) == 1
    assert service.transport.reads == [MESSAGE_ID]


@pytest.mark.parametrize("field,value", [
    ("extra", True), ("deleted", 0), ("edited", 1), ("message_id", "x"),
    ("message_id", True), ("wire_body", float("nan")), ("chat_type", False),
    ("wire_body", "\ud800"), ("wire_body", "x" * 20000),
])
def test_strict_evidence_parser(ready, field, value):
    from ostag.briefs.delivery import DeliveryError
    with pytest.raises(DeliveryError) as caught:
        ProviderMessage.from_dict(proof(ready[2]).to_dict() | {field: value})
    assert caught.value.__context__ is None and caught.value.__cause__ is None


@pytest.mark.parametrize("generation", [True, 0, -1, 1.2, float("nan"), float("inf"), "1"])
def test_strict_binding_generation(ready, generation):
    from ostag.briefs.delivery import DeliveryError
    raw = proof(ready[2]).to_dict()
    raw["binding"]["generation"] = generation
    with pytest.raises(DeliveryError):
        ProviderMessage.from_dict(raw)


@pytest.mark.parametrize("field", ["revision_id", "job_id", "delivery_id", "wire_body",
                                   "binding", "destination"])
async def test_altered_intent_conflicts_and_never_replaces(ready, field):
    from ostag.briefs.delivery import wire_text
    briefs, _, intent = ready
    briefs.store.enable_delivery()
    service = delivery(ready)
    original = await service.prepare(intent)
    changes = dict(revision_id="different", job_id="different", delivery_id="different",
                   wire_body=wire_text("changed"), binding=replace(BINDING, generation=2),
                   destination=replace(intent.destination, address="other-private"))
    with pytest.raises(Conflict):
        await service.prepare(replace(intent, **{field: changes[field]}))
    assert briefs.store.delivery_snapshot(WORKER, SCOPE, intent.delivery_id) == original
    assert not service.transport.sends


async def test_two_handles_one_send(ready):
    briefs, _, intent = ready
    briefs.store.enable_delivery()
    one = delivery(ready)
    two = DeliveryService(store=BriefStore(briefs.store.path), worker=WorkerContext("second"),
                          transport=one.transport, authorize=lambda _: None, clock=briefs.clock)
    await one.prepare(intent)
    entered, release = asyncio.Event(), asyncio.Event()
    async def hold(attempt):
        entered.set()
        await release.wait()
        return SendResponse(attempt.token, MESSAGE_ID, (proof(attempt.intent),))
    one.transport.send_effect = hold
    first = asyncio.create_task(one.send(SCOPE, intent.delivery_id))
    await entered.wait()
    second = await two.send(SCOPE, intent.delivery_id)
    assert second.state == "started"
    release.set()
    assert (await first).state == "provider_accepted"
    assert len(one.transport.sends) == 1


async def test_pending_restart_can_send_started_restart_cannot(ready):
    briefs, _, intent = ready
    briefs.store.enable_delivery()
    service = delivery(ready)
    pending = await service.prepare(intent)
    reopened = BriefStore(briefs.store.path)
    assert reopened.delivery_snapshot(WORKER, SCOPE, intent.delivery_id) == pending
    attempt = reopened.begin_delivery(WORKER, intent, clock=briefs.clock, precommit=lambda: None)
    assert attempt.token
    newer = delivery(ready)
    newer.store = BriefStore(briefs.store.path)
    result = await newer.send(SCOPE, intent.delivery_id)
    assert result.state == "started" and result.public_status == "delivery: pending_verification"
    assert await newer.observe(SCOPE, intent.delivery_id) == result
    assert not newer.transport.sends and not newer.transport.reads
    assert reopened.begin_delivery(WORKER, intent, clock=briefs.clock,
                                   precommit=lambda: None) is None


async def test_pending_reopen_send(ready):
    ready[0].store.enable_delivery()
    service = delivery(ready)
    await service.prepare(ready[2])
    service.store = BriefStore(service.store.path)
    assert (await service.send(SCOPE, ready[2].delivery_id)).state == "provider_accepted"


async def test_lost_begin_commit_response_is_never_replayed(ready, monkeypatch):
    from ostag.briefs.delivery import DeliveryError
    ready[0].store.enable_delivery()
    service = delivery(ready)
    await service.prepare(ready[2])
    original = service.store.begin_delivery
    def lost(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("PRIVATE committed result lost before IO")
    monkeypatch.setattr(service.store, "begin_delivery", lost)
    with pytest.raises(DeliveryError, match="store_error"):
        await service.send(SCOPE, ready[2].delivery_id)
    monkeypatch.setattr(service.store, "begin_delivery", original)
    result = await service.send(SCOPE, ready[2].delivery_id)
    assert result.state == "started" and result.uncertainty.reason == "send_unverified"
    assert result.first_failure is None and result.candidate_id is None
    assert result.public_status == "delivery: pending_verification"
    assert await service.observe(SCOPE, ready[2].delivery_id) == result
    assert not service.transport.sends and not service.transport.reads
    assert BriefStore(service.store.path).delivery_snapshot(WORKER, SCOPE,
                                                          ready[2].delivery_id) == result


@pytest.mark.parametrize("when", ["pending", "started", "accepted"])
async def test_cancel_preserves_local_intent_and_honest_history(ready, when):
    briefs, _, intent = ready
    briefs.store.enable_delivery()
    service = delivery(ready)
    await service.prepare(intent)
    if when == "started":
        briefs.store.begin_delivery(WORKER, intent, clock=briefs.clock, precommit=lambda: None)
    elif when == "accepted":
        await service.send(SCOPE, intent.delivery_id)
    before = briefs.store.delivery_snapshot(WORKER, SCOPE, intent.delivery_id)
    briefs.store.cancel(SCOPE, intent.job_id, clock=briefs.clock)
    after = await service.send(SCOPE, intent.delivery_id)
    assert after.outbox_state == "cancelled"
    assert after.state == {"pending": "cancelled", "started": "started",
                           "accepted": "provider_accepted"}[when]
    assert after.receipt_json == before.receipt_json and after.uncertainty == before.uncertainty
    assert after.human_received is False
    assert len(service.transport.sends) == (1 if when == "accepted" else 0)


@pytest.mark.parametrize("boundary", ["begin", "observe"])
async def test_revocation_after_sqlite_lock_wait(ready, boundary):
    from ostag.briefs.delivery import DeliveryError
    allowed = True
    def authorize(_):
        if not allowed:
            raise RuntimeError("PRIVATE ACL revoked")
    if boundary == "observe":
        service, initial = await candidate_ready(ready, authorize=authorize)
    else:
        ready[0].store.enable_delivery()
        service = delivery(ready, authorize=authorize)
        initial = await service.prepare(ready[2])
    with sqlite3.connect(service.store.path) as holder:
        holder.execute("BEGIN EXCLUSIVE")
        method = service.send if boundary == "begin" else service.observe
        task = asyncio.create_task(method(SCOPE, ready[2].delivery_id))
        await asyncio.sleep(0.025)
        assert not task.done()
        allowed = False
        holder.rollback()
    with pytest.raises(DeliveryError, match="authorization_denied") as caught:
        await task
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    result = service.store.delivery_snapshot(WORKER, SCOPE, ready[2].delivery_id)
    assert result.state == initial.state and result.receipt_json is None
    assert service.transport.reads == []
    assert len(service.transport.sends) == (1 if boundary == "observe" else 0)


@pytest.mark.parametrize("change", ["revocation", "cancel"])
async def test_actual_egress_after_adapter_await_rechecks_authority(ready, change):
    from ostag.briefs.delivery import DeliveryError
    ready[0].store.enable_delivery()
    allowed = True
    def authorize(_):
        if not allowed:
            raise RuntimeError("PRIVATE revoked before IO")
    service = delivery(ready, authorize=authorize)
    await service.prepare(ready[2])
    entered, release = asyncio.Event(), asyncio.Event()
    actual_io = []
    class Deferred(Transport):
        async def send(self, attempt, *, before_egress):
            entered.set()
            await release.wait()
            before_egress()
            actual_io.append(True)
            return SendResponse(attempt.token, MESSAGE_ID, (proof(attempt.intent),))
    service.transport = Deferred()
    task = asyncio.create_task(service.send(SCOPE, ready[2].delivery_id))
    await entered.wait()
    if change == "revocation":
        allowed = False
    else:
        service.store.cancel(SCOPE, ready[2].job_id, clock=service.clock)
    release.set()
    if change == "revocation":
        with pytest.raises(DeliveryError, match="authorization_denied"):
            await task
    else:
        assert (await task).state == "unknown"
    assert not actual_io
    assert service.store.delivery_snapshot(WORKER, SCOPE, ready[2].delivery_id).receipt_json is None


@pytest.mark.parametrize("change", ["revocation", "cancel", "caller_cancel", "generation"])
async def test_no_late_observation_confirmation(ready, change):
    from ostag.briefs.delivery import DeliveryError
    current_binding = BINDING
    allowed = True
    def authorize(intent):
        if not allowed or intent.binding != current_binding:
            raise RuntimeError("PRIVATE revoked during observe")
    service, initial = await candidate_ready(ready, authorize=authorize)
    entered, release = asyncio.Event(), asyncio.Event()
    async def read(intent):
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()
        return (proof(intent),)
    service.transport.read_effect = read
    task = asyncio.create_task(service.observe(SCOPE, ready[2].delivery_id))
    await entered.wait()
    if change == "revocation":
        allowed = False
    elif change == "generation":
        current_binding = replace(BINDING, generation=2)
    elif change == "cancel":
        service.store.cancel(SCOPE, ready[2].job_id, clock=service.clock)
    else:
        task.cancel()
    if change == "caller_cancel":
        with pytest.raises(asyncio.CancelledError):
            await task
        assert service.active_operations == 1
        release.set()
        await asyncio.sleep(0.01)
    else:
        release.set()
        with pytest.raises(DeliveryError):
            await task
    result = service.store.delivery_snapshot(WORKER, SCOPE, ready[2].delivery_id)
    assert result.state == "unknown" and result.receipt_json is None
    assert result.uncertainty == initial.uncertainty
    assert result.first_failure == initial.first_failure


@pytest.mark.parametrize("action", ["timeout", "stop", "cancel"])
async def test_hostile_async_transport_has_bounded_custody_and_retains_capacity(ready, action):
    from ostag.briefs.delivery import DeliveryError
    ready[0].store.enable_delivery()
    service = delivery(ready, timeout=0.12)
    await service.prepare(ready[2])
    entered, release = asyncio.Event(), asyncio.Event()
    async def hostile(attempt):
        entered.set()
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                continue
        return SendResponse(attempt.token, MESSAGE_ID, (proof(attempt.intent),))
    service.transport.send_effect = hostile
    task = asyncio.create_task(service.send(SCOPE, ready[2].delivery_id))
    await entered.wait()
    if action == "stop":
        service.stop()
    elif action == "cancel":
        task.cancel()
    started = asyncio.get_running_loop().time()
    try:
        with pytest.raises(asyncio.CancelledError if action == "cancel" else DeliveryError):
            await asyncio.wait_for(task, 0.6)
        assert asyncio.get_running_loop().time() - started < 0.6
        assert service.active_operations == 1
        for _ in range(5):
            with pytest.raises(DeliveryError, match="stopped|capacity_exhausted"):
                await service.send(SCOPE, ready[2].delivery_id)
        assert len(service.transport.sends) == 1
        result = service.store.delivery_snapshot(WORKER, SCOPE, ready[2].delivery_id)
        assert result.state == "unknown" and result.receipt_json is None
    finally:
        release.set()
        await asyncio.sleep(0.02)
    assert service.active_operations == 0
    assert service.store.delivery_snapshot(WORKER, SCOPE, ready[2].delivery_id) == result


@pytest.mark.parametrize("action", ["timeout", "cancel", "stop"])
async def test_lock_wait_is_cancellable_and_does_not_start(ready, action):
    from ostag.briefs.delivery import DeliveryError
    ready[0].store.enable_delivery()
    service = delivery(ready, timeout=0.08)
    await service.prepare(ready[2])
    with sqlite3.connect(service.store.path) as holder:
        holder.execute("BEGIN EXCLUSIVE")
        task = asyncio.create_task(service.send(SCOPE, ready[2].delivery_id))
        await asyncio.sleep(0.02)
        if action == "cancel":
            task.cancel()
        elif action == "stop":
            service.stop()
        with pytest.raises(asyncio.CancelledError if action == "cancel" else DeliveryError):
            await asyncio.wait_for(task, 0.5)
        holder.rollback()
    assert service.store.delivery_snapshot(WORKER, SCOPE, ready[2].delivery_id).state == "pending"
    assert not service.transport.sends


async def test_authorization_exception_has_no_chained_private_leak(ready):
    from ostag.briefs.delivery import DeliveryError
    import traceback
    ready[0].store.enable_delivery()
    def private(_):
        raise RuntimeError("private-callback-secret-734")
    service = delivery(ready, authorize=private)
    with pytest.raises(DeliveryError) as caught:
        await service.prepare(ready[2])
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert "private-callback-secret-734" not in "".join(traceback.format_exception(caught.value))
    assert service.store.delivery_snapshot(WORKER, SCOPE, ready[2].delivery_id) is None


async def test_observation_limit_and_final_receipt_independent_of_diagnostic_capacity(ready):
    from ostag.briefs.delivery import MAX_OBSERVATIONS
    service, initial = await candidate_ready(ready)
    for _ in range(40):
        service.store.delivery_unknown(WORKER, SCOPE, ready[2].delivery_id, "not_found",
                                       token=service.transport.sends[0].token, clock=service.clock)
    saturated = service.store.delivery_snapshot(WORKER, SCOPE, ready[2].delivery_id)
    assert len(saturated.observations) == MAX_OBSERVATIONS
    result = await service.observe(SCOPE, ready[2].delivery_id)
    assert result.state == "provider_accepted" and result.receipt_json
    assert result.uncertainty == initial.uncertainty
    assert result.first_failure == initial.first_failure
    assert result.observations == saturated.observations


async def test_explicit_read_retry_budget_survives_restart(ready):
    service, initial = await candidate_ready(ready)
    async def absent(_):
        return ()
    service.transport.read_effect = absent
    for _ in range(35):
        result = await service.observe(SCOPE, ready[2].delivery_id)
    assert result.state == "unknown" and result.read_attempts == 32
    assert len(service.transport.reads) == 32
    assert result.uncertainty == initial.uncertainty
    assert result.first_failure == initial.first_failure
    service.store = BriefStore(service.store.path)
    assert await service.observe(SCOPE, ready[2].delivery_id) == result
    assert len(service.transport.reads) == 32 and len(service.transport.sends) == 1
    with sqlite3.connect(service.store.path) as db:
        assert db.execute("SELECT count(*),sum(settled) FROM brief_delivery_reads").fetchone() == (32, 32)


async def test_clock_rollback_refuses_begin_and_late_receipt(ready):
    from ostag.briefs.delivery import DeliveryError
    briefs, authority, intent = ready
    briefs.store.enable_delivery()
    service = delivery(ready)
    authority.now = 10
    await service.prepare(intent)
    authority.now = 9
    with pytest.raises(DeliveryError, match="clock_error"):
        await service.send(SCOPE, intent.delivery_id)
    assert not service.transport.sends
    authority.now = 11
    async def rollback(attempt):
        authority.now = 10
        return SendResponse(attempt.token, MESSAGE_ID, (proof(attempt.intent),))
    service.transport.send_effect = rollback
    with pytest.raises(DeliveryError, match="clock_error"):
        await service.send(SCOPE, intent.delivery_id)
    assert service.store.delivery_snapshot(WORKER, SCOPE, intent.delivery_id).receipt_json is None
    authority.now = 12
    assert (await service.send(SCOPE, intent.delivery_id)).state == "started"
    assert len(service.transport.sends) == 1


async def test_claim_preserves_uncertainty_even_if_failure_write_is_unavailable(ready):
    """A durable pre-IO marker survives a crash or unavailable post-IO SQLite."""
    ready[0].store.enable_delivery()
    service = delivery(ready)
    await service.prepare(ready[2])
    service.store.begin_delivery(WORKER, ready[2], clock=service.clock, precommit=lambda: None)
    result = BriefStore(service.store.path).delivery_snapshot(WORKER, SCOPE, ready[2].delivery_id)
    assert result.uncertainty is not None
    assert result.uncertainty.reason == "send_unverified"


@pytest.mark.parametrize("schema", [1, 2, 3])
def test_additive_migrations_preserve_every_old_row(tmp_path, schema):
    from ostag.briefs.store import RequesterContext
    from ostag.core.identity import Principal
    from test_brief_generation import SCHEMA1_SQL, at, parameters, GenerationConfig
    path = tmp_path / "migration.db"
    with sqlite3.connect(path) as db:
        db.executescript(SCHEMA1_SQL)
    if schema >= 2:
        old = BriefStore(path)
        scope = RequesterContext("migration-fixture", Principal("feishu", "synthetic-alice"))
        active = old.get(scope, "b22e7058b1ac47f48638e14c2e4e4ddc")
        config = GenerationConfig(parameters(), 100000)
        reserved = old.reserve_cost(active.lease, call_id="old-paid", profile=parameters().cost_profile,
            daily_limit_micros=100000, config_hash=config.config_hash, clock=lambda: at(6))
        old.start_cost(active.lease, reserved.reservation_id, daily_limit_micros=100000,
                       clock=lambda: at(6))
        old.mark_cost_unknown(active.lease.worker, reserved.reservation_id, "timeout",
                              clock=lambda: at(6))
        if schema == 3:
            old.enable_manual()
            job = old.admit(scope, idempotency_key="manual", payload={"synthetic": True},
                grant_id="grant", grant_revision=1, destination=active.destination,
                now=at(6), request_hash="f" * 64)
            old.cancel(scope, job.job_id, clock=lambda: at(6))
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == schema
        tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        before = {t: db.execute(f"SELECT * FROM {t} ORDER BY 1").fetchall() for t in tables}
    migrated = BriefStore(path)
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == max(2, schema)
    migrated.enable_delivery()
    migrated.enable_manual()  # no downgrade and no implicit delivery mutation
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 4
        assert {t: db.execute(f"SELECT * FROM {t} ORDER BY 1").fetchall() for t in tables} == before
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    BriefStore(path).enable_delivery()


def test_actual_pinned_old_binary_rejects_schema4_without_writes(tmp_path):
    """Load the actual pre-U01 store from this repository's immutable baseline."""
    import hashlib
    import sys
    import types
    from pathlib import Path
    baseline = "5e2e79b15489016085e94b95e9e56594ffb68b26"
    # Byte-exact ostag/briefs/store.py from baseline; works without Git history.
    fixture = Path(__file__).resolve().parent / "fixtures/briefs/store-before-u01-5e2e79b.py.txt"
    source = fixture.read_bytes()
    assert hashlib.sha256(source).hexdigest() == (
        "508ab3eadced9b87b570686bed9bb3be59927b4c9461c609eddcad474458d680"
    ), f"legacy store fixture differs from {baseline}:ostag/briefs/store.py"
    module = types.ModuleType("_u01_actual_legacy_store")
    sys.modules[module.__name__] = module
    try:
        exec(compile(source, str(fixture), "exec"), module.__dict__)
        assert module.CURRENT_SCHEMA_VERSION == 2 and module.READABLE_SCHEMA_MAX == 3
        path = tmp_path / "new.db"
        store = BriefStore(path)
        with sqlite3.connect(path) as db:
            assert db.execute("PRAGMA user_version").fetchone()[0] == 2
        store.enable_manual()
        with sqlite3.connect(path) as db:
            assert db.execute("PRAGMA user_version").fetchone()[0] == 3
        module.BriefStore(path)  # old binary actually reads 3
        store.enable_delivery()
        before = path.read_bytes()
        with pytest.raises(module.BriefSchemaError):
            module.BriefStore(path)
        assert path.read_bytes() == before
        BriefStore(path)
    finally:
        del sys.modules[module.__name__]


@pytest.mark.parametrize("tamper", ["future", "foreign", "partial", "trigger", "extra_table",
                                   "delivery_trigger"])
def test_future_foreign_partial_tampered_schema_immutable_failure(tmp_path, tamper):
    from ostag.briefs.store import BriefSchemaError
    store = BriefStore(tmp_path / "bad.db")
    if tamper in ("partial", "delivery_trigger"):
        store.enable_delivery()
    with sqlite3.connect(store.path) as db:
        db.execute({
            "future": "PRAGMA user_version=5", "foreign": "PRAGMA application_id=1",
            "partial": "DROP TABLE brief_delivery_observations",
            "trigger": "CREATE TRIGGER bad BEFORE INSERT ON brief_jobs BEGIN SELECT 1; END",
            "extra_table": "CREATE TABLE extra(x)",
            "delivery_trigger": "CREATE TRIGGER bad BEFORE UPDATE ON brief_deliveries "
                                "BEGIN SELECT RAISE(IGNORE); END",
        }[tamper])
    before = store.path.read_bytes()
    with pytest.raises(BriefSchemaError):
        store.enable_delivery()
    with pytest.raises(BriefSchemaError):
        BriefStore(store.path)
    assert store.path.read_bytes() == before


async def test_reused_schema4_handle_rejects_tampering_before_any_delivery_write(ready):
    from ostag.briefs.delivery import DeliveryError
    ready[0].store.enable_delivery()
    service = delivery(ready)
    await service.prepare(ready[2])
    with sqlite3.connect(service.store.path) as db:
        db.execute("CREATE TRIGGER skip_claim BEFORE UPDATE ON brief_deliveries "
                   "BEGIN SELECT RAISE(IGNORE); END")
    before = service.store.path.read_bytes()
    with pytest.raises(DeliveryError):
        await service.send(SCOPE, ready[2].delivery_id)
    assert service.store.path.read_bytes() == before
    assert not service.transport.sends


async def test_scoped_private_snapshots_and_no_implicit_enablement(ready):
    from ostag.briefs.delivery import DeliveryError
    from ostag.briefs.store import RequesterContext
    from ostag.core.identity import Principal
    service = delivery(ready)
    with pytest.raises(DeliveryError):
        await service.prepare(ready[2])
    with sqlite3.connect(service.store.path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 3
    service.store.enable_delivery()
    result = await service.prepare(ready[2])
    assert ready[2].wire_body.decode() not in repr(result)
    assert result.public_status == "delivery: pending"
    for other in (RequesterContext("other", SCOPE.principal),
                  RequesterContext(SCOPE.namespace, Principal("feishu", "bob")),
                  RequesterContext(SCOPE.namespace, Principal("slack", "alice"))):
        assert service.store.delivery_snapshot(WORKER, other, ready[2].delivery_id) is None
        with pytest.raises(DeliveryError):
            await service.send(other, ready[2].delivery_id)
    assert not service.transport.sends


def test_independent_processes_compete_for_one_opaque_claim(ready):
    import subprocess
    import sys
    from pathlib import Path
    briefs, _, intent = ready
    briefs.store.enable_delivery()
    briefs.store.prepare_delivery(WORKER, intent, clock=briefs.clock, precommit=lambda: None)
    program = '''
import json, sys, time
from ostag.briefs.store import BriefStore, WorkerContext, RequesterContext, StoreBusy
from ostag.briefs.delivery import DeliveryIntent, FeishuBinding
from ostag.core.identity import Principal
store = BriefStore(sys.argv[1])
scope = RequesterContext("synthetic", Principal("feishu", "alice"))
intent = DeliveryIntent.from_outbox(scope, store.outbox(scope, sys.argv[2]),
                                  FeishuBinding("tenant-1", "installation-1", "app-1", "bot-1", 1))
sys.stdin.readline()
end = time.monotonic() + 2
while True:
    try:
        attempt = store.begin_delivery(WorkerContext(sys.argv[3]), intent,
                  clock=lambda: "2026-09-13T00:00:00Z", precommit=lambda: None)
        print(json.dumps(attempt is not None))
        break
    except StoreBusy:
        assert time.monotonic() < end
        time.sleep(0.01)
'''
    children = [subprocess.Popen([sys.executable, "-c", program, str(briefs.store.path),
                                  intent.job_id, f"process-{i}"], stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                 cwd=Path(__file__).resolve().parents[1]) for i in range(2)]
    try:
        for child in children:
            child.stdin.write("go\n")
            child.stdin.flush()
        results = [child.communicate(timeout=4) for child in children]
        assert all(child.returncode == 0 for child in children), results
        assert sorted(json.loads(stdout) for stdout, _ in results) == [False, True]
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=1)


async def test_no_database_work_after_final_policy(ready, monkeypatch):
    ready[0].store.enable_delivery()
    trace = []
    original = sqlite3.connect
    def connection(*args, **kwargs):
        db = original(*args, **kwargs)
        db.set_trace_callback(lambda sql: trace.append(sql))
        return db
    monkeypatch.setattr("ostag.briefs.store.sqlite3.connect", connection)
    def authorize(_):
        trace.append("POLICY")
    service = delivery(ready, authorize=authorize)
    await service.prepare(ready[2])
    await service.send(SCOPE, ready[2].delivery_id)
    # Some gates also run outside transactions; a policy inside an exclusive
    # transaction is followed by COMMIT, with no subsequent reads/writes.
    transaction = False
    gated = False
    for statement in trace:
        if statement == "BEGIN EXCLUSIVE":
            transaction = True
        if transaction and statement == "POLICY":
            gated = True
        elif transaction and gated:
            assert statement == "COMMIT"
            gated = transaction = False


@pytest.mark.parametrize("boundary", ["begin", "response", "observation"])
async def test_direct_store_policy_private_errors_are_sanitized(ready, boundary):
    from ostag.briefs.delivery import DeliveryError
    ready[0].store.enable_delivery()
    service = delivery(ready)
    await service.prepare(ready[2])
    def private():
        raise RuntimeError("PRIVATE internal callback failure")
    if boundary == "begin":
        def call():
            return service.store.begin_delivery(WORKER, ready[2], clock=service.clock,
                                                precommit=private)
    else:
        attempt = service.store.begin_delivery(WORKER, ready[2], clock=service.clock,
                                                precommit=lambda: None)
        if boundary == "response":
            def call():
                return service.store.accept_delivery_response(
                    WORKER, attempt, MESSAGE_ID, (proof(ready[2]),),
                    clock=service.clock, precommit=private)
        else:
            service.store.accept_delivery_response(WORKER, attempt, MESSAGE_ID, (),
                                                     clock=service.clock, precommit=lambda: None)
            observation = service.store.begin_delivery_observation(
                WORKER, ready[2], clock=service.clock, precommit=lambda: None)
            def call():
                return service.store.accept_delivery_observation(
                    WORKER, observation, (proof(ready[2]),),
                    clock=service.clock, precommit=private)
    with pytest.raises(DeliveryError) as caught:
        call()
    assert caught.value.__context__ is None and caught.value.__cause__ is None
    result = service.store.delivery_snapshot(WORKER, SCOPE, ready[2].delivery_id)
    assert result.receipt_json is None


@pytest.mark.parametrize("kind", ["wrong_token", "weak_id", "multiple", "dict", "no_id"])
async def test_direct_response_correlation_and_identity_are_mandatory(ready, kind):
    ready[0].store.enable_delivery()
    service = delivery(ready)
    await service.prepare(ready[2])
    async def respond(attempt):
        if kind == "wrong_token":
            return SendResponse("unrelated-attempt", MESSAGE_ID, (proof(attempt.intent),))
        if kind == "weak_id":
            return SendResponse(attempt.token, "x", (proof(attempt.intent),))
        if kind == "dict":
            return {"message_id": MESSAGE_ID, "approved": True}
        if kind == "no_id":
            return SendResponse(attempt.token, None, (proof(attempt.intent),))
        return SendResponse(attempt.token, MESSAGE_ID, (proof(attempt.intent), proof(attempt.intent)))
    service.transport.send_effect = respond
    result = await service.send(SCOPE, ready[2].delivery_id)
    assert result.state == "unknown" and result.receipt_json is None
    assert result.candidate_id == (MESSAGE_ID if kind == "multiple" else None)
    assert len(service.transport.sends) == 1


async def test_busy_after_send_retries_only_persistence_and_reauthorizes(ready):
    from ostag.briefs.delivery import DeliveryError
    ready[0].store.enable_delivery()
    allowed = True
    def authorize(_):
        if not allowed:
            raise RuntimeError("PRIVATE revoked during acceptance wait")
    service = delivery(ready, authorize=authorize)
    await service.prepare(ready[2])
    locked = asyncio.Event()
    holder = sqlite3.connect(service.store.path)
    async def respond(attempt):
        holder.execute("BEGIN EXCLUSIVE")
        locked.set()
        return SendResponse(attempt.token, MESSAGE_ID, (proof(attempt.intent),))
    service.transport.send_effect = respond
    task = asyncio.create_task(service.send(SCOPE, ready[2].delivery_id))
    try:
        await locked.wait()
        await asyncio.sleep(0.025)
        assert not task.done() and len(service.transport.sends) == 1
        allowed = False
        holder.rollback()
        with pytest.raises(DeliveryError, match="authorization_denied"):
            await task
    finally:
        holder.close()
    result = service.store.delivery_snapshot(WORKER, SCOPE, ready[2].delivery_id)
    assert result.state == "unknown" and result.receipt_json is None
    assert result.first_failure.reason == "authorization_denied"
    assert len(service.transport.sends) == 1


@pytest.mark.parametrize("pre_send_delay", [0, 0.12], ids=["normal", "slow"])
async def test_post_send_storage_unavailable_keeps_durable_initial_uncertainty(
        ready, monkeypatch, pre_send_delay):
    from ostag.briefs.delivery import DeliveryError
    ready[0].store.enable_delivery()
    service = delivery(ready)
    await service.prepare(ready[2])
    begin_delivery = service.store.begin_delivery
    def delayed_claim(*args, **kwargs):
        attempt = begin_delivery(*args, **kwargs)
        # Exercise >80 ms of pre-send work after the durable claim commits.
        sleep(pre_send_delay)
        return attempt
    monkeypatch.setattr(service.store, "begin_delivery", delayed_claim)
    holder = sqlite3.connect(service.store.path)
    locked = asyncio.Event()
    async def respond(attempt):
        holder.execute("BEGIN EXCLUSIVE")
        locked.set()
        raise RuntimeError("PRIVATE lost response with blocked database")
    service.transport.send_effect = respond
    try:
        with pytest.raises(DeliveryError, match="^transport_error$") as error:
            await asyncio.wait_for(service.send(SCOPE, ready[2].delivery_id), timeout=10)
        assert error.value.__context__ is None
        assert locked.is_set() and holder.in_transaction
        assert len(service.transport.sends) == 1
    finally:
        holder.rollback()
        holder.close()
    result = BriefStore(service.store.path).delivery_snapshot(WORKER, SCOPE, ready[2].delivery_id)
    assert result.state == "started" and result.uncertainty.reason == "send_unverified"
    assert result.receipt_json is None
    assert await asyncio.wait_for(service.send(SCOPE, ready[2].delivery_id), timeout=10) == result
    assert len(service.transport.sends) == 1


async def test_lost_receipt_commit_response_still_reads_as_accepted_without_resend(ready, monkeypatch):
    from ostag.briefs.delivery import DeliveryError
    ready[0].store.enable_delivery()
    service = delivery(ready)
    await service.prepare(ready[2])
    original = service.store.accept_delivery_response
    def lost(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("PRIVATE accepted commit response lost")
    monkeypatch.setattr(service.store, "accept_delivery_response", lost)
    with pytest.raises(DeliveryError):
        await service.send(SCOPE, ready[2].delivery_id)
    result = await service.send(SCOPE, ready[2].delivery_id)
    assert result.state == "provider_accepted" and result.receipt_json
    assert len(service.transport.sends) == 1


async def test_full_unicode_wire_body_is_preserved_and_oversize_is_rejected(tmp_path):
    from ostag.briefs.delivery import DeliveryError, wire_text
    from test_brief_store import admit, publish, at, SCOPE as scope, WORKER as worker
    store = BriefStore(tmp_path / "full.db")
    store.enable_delivery()
    job = admit(store)
    claimed = store.claim_one(worker, clock=lambda: at(1))
    store.start_generating(claimed.lease, clock=lambda: at(2))
    text = "私人材料\n\"<literal>&" * 450
    publish(store, claimed.lease, draft={"text": text})
    intent = DeliveryIntent.from_outbox(scope, store.outbox(scope, job.job_id), BINDING)
    service = DeliveryService(store=store, worker=worker, transport=Transport(),
                               authorize=lambda _: None, clock=lambda: at(3))
    await service.prepare(intent)
    accepted = await service.send(scope, intent.delivery_id)
    assert accepted.state == "provider_accepted"
    assert json.loads(accepted.receipt_json)["wire_body"].encode("utf-8") == intent.wire_body
    assert json.loads(json.loads(intent.wire_body)["content"]["text"])["text"] == text
    assert BriefStore(store.path).delivery_snapshot(worker, scope, intent.delivery_id) == accepted
    with pytest.raises(DeliveryError, match="invalid_wire_body"):
        wire_text("私" * 16384)


@pytest.mark.parametrize("kind", ["missing_gate", "repeated_gate"])
async def test_transport_egress_gate_is_mandatory_and_single_use(ready, kind):
    ready[0].store.enable_delivery()
    class BadTransport(Transport):
        async def send(self, attempt, *, before_egress):
            if kind == "repeated_gate":
                before_egress()
                before_egress()
            return SendResponse(attempt.token, MESSAGE_ID, (proof(attempt.intent),))
    service = delivery(ready, transport=BadTransport())
    await service.prepare(ready[2])
    result = await service.send(SCOPE, ready[2].delivery_id)
    assert result.state == "unknown" and result.receipt_json is None


async def test_final_store_clock_also_checks_monotonic_deadline(ready, monkeypatch):
    from ostag.briefs.delivery import DeliveryError
    ready[0].store.enable_delivery()
    tick = [0.0]
    monkeypatch.setattr("ostag.briefs.delivery.monotonic", lambda: tick[0])
    service = delivery(ready, timeout=1)
    await service.prepare(ready[2])
    original = service.store._delivery_final
    def final(clock, stamp, precommit):
        # Simulate deadline crossing in the final clock AFTER local policy.
        def crossing():
            tick[0] = 2.0
            return clock()
        original(crossing, stamp, precommit)
    monkeypatch.setattr(service.store, "_delivery_final", final)
    with pytest.raises(DeliveryError, match="timeout"):
        await service.send(SCOPE, ready[2].delivery_id)
    assert not service.transport.sends
    assert service.store.delivery_snapshot(WORKER, SCOPE, ready[2].delivery_id).state == "pending"


@pytest.mark.parametrize("error", ["domain", "malformed_domain", "sqlite", "cancelled"])
async def test_callback_exception_types_cannot_smuggle_private_details(ready, error):
    from ostag.briefs.delivery import DeliveryError
    import traceback
    ready[0].store.enable_delivery()
    service = delivery(ready)
    await service.prepare(ready[2])
    def private():
        if error == "domain":
            raise DeliveryError("private-callback-secret-935")
        if error == "malformed_domain":
            raise DeliveryError(["private-callback-secret-935"])
        if error == "sqlite":
            raise sqlite3.OperationalError("private-callback-secret-935")
        raise asyncio.CancelledError("private-callback-secret-935")
    with pytest.raises(DeliveryError, match="authorization_denied") as caught:
        service.store.begin_delivery(WORKER, ready[2], clock=service.clock, precommit=private)
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert "private-callback-secret-935" not in "".join(traceback.format_exception(caught.value))
    assert service.store.delivery_snapshot(WORKER, SCOPE, ready[2].delivery_id).state == "pending"


async def test_egress_observes_caller_cancel_before_parent_coroutine_resumes(ready):
    """The parent has a pending cancellation but has not yet handled CancelledError."""
    ready[0].store.enable_delivery()
    actual_io = []
    outer = None
    class RacingTransport(Transport):
        async def send(self, attempt, *, before_egress):
            outer.cancel()
            before_egress()
            actual_io.append(True)
            return SendResponse(attempt.token, MESSAGE_ID, (proof(attempt.intent),))
    service = delivery(ready, transport=RacingTransport())
    await service.prepare(ready[2])
    outer = asyncio.create_task(service.send(SCOPE, ready[2].delivery_id))
    with pytest.raises(asyncio.CancelledError):
        await outer
    assert actual_io == []
    assert service.store.delivery_snapshot(WORKER, SCOPE, ready[2].delivery_id).receipt_json is None


@pytest.mark.parametrize("action", ["timeout", "cancel", "stop", "status_error"])
@pytest.mark.parametrize("sending", [True, False])
async def test_failed_loser_cannot_mutate_winning_operation(ready, monkeypatch, action, sending):
    from ostag.briefs.delivery import DeliveryError
    from ostag.briefs.store import StoreBusy
    briefs, _, intent = ready
    if sending:
        briefs.store.enable_delivery()
        winner = delivery(ready)
        await winner.prepare(intent)
    else:
        winner, _ = await candidate_ready(ready)
    loser = DeliveryService(store=BriefStore(briefs.store.path), worker=WorkerContext("loser"),
                            transport=Transport(), authorize=lambda _: None, clock=briefs.clock,
                            timeout=0.15)
    contended, entered, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    fail_status = False
    original_snapshot = loser.store.delivery_snapshot
    begin_name = "begin_delivery" if sending else "begin_delivery_observation"
    original_begin = getattr(loser.store, begin_name)

    def busy_begin(*args, **kwargs):
        nonlocal fail_status
        contended.set()
        if action == "status_error" and entered.is_set():
            if sending:
                assert original_begin(*args, **kwargs) is None
            # A read budget loser can likewise receive no claim (e.g. exhaustion).
            fail_status = True
            return None
        raise StoreBusy("injected contention")

    def snapshot(*args, **kwargs):
        if fail_status:
            raise RuntimeError("PRIVATE losing status read")
        return original_snapshot(*args, **kwargs)

    class HeldTransport(Transport):
        async def send(self, attempt, *, before_egress):
            entered.set()
            await release.wait()
            return await super().send(attempt, before_egress=before_egress)

        async def observe(self, intent, message_id, *, before_egress):
            entered.set()
            await release.wait()
            return await super().observe(intent, message_id, before_egress=before_egress)

    winner.transport = HeldTransport()
    monkeypatch.setattr(loser.store, begin_name, busy_begin)
    monkeypatch.setattr(loser.store, "delivery_snapshot", snapshot)
    operation = "send" if sending else "observe"
    losing = asyncio.create_task(getattr(loser, operation)(SCOPE, intent.delivery_id))
    await asyncio.wait_for(contended.wait(), 1)
    winning = asyncio.create_task(getattr(winner, operation)(SCOPE, intent.delivery_id))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        before = winner.store.delivery_snapshot(WORKER, SCOPE, intent.delivery_id)
        if sending:
            assert before.state == "started" and before.first_failure is None
        if action == "cancel":
            losing.cancel()
        elif action == "stop":
            loser.stop()
        with pytest.raises(asyncio.CancelledError if action == "cancel" else DeliveryError):
            await asyncio.wait_for(losing, 1)
        after = winner.store.delivery_snapshot(WORKER, SCOPE, intent.delivery_id)
        assert after == before
        assert not loser.transport.sends and not loser.transport.reads
    finally:
        release.set()
        result = await asyncio.wait_for(winning, 1)
    assert result.state == "provider_accepted"
    assert len(winner.transport.sends) == int(sending)
    assert len(winner.transport.reads) == int(not sending)


@pytest.mark.parametrize("wrong", ["owner", "token", "missing"])
async def test_send_diagnostic_requires_actual_owner_and_token(ready, wrong):
    briefs, _, intent = ready
    briefs.store.enable_delivery()
    service = delivery(ready)
    await service.prepare(intent)
    attempt = briefs.store.begin_delivery(WORKER, intent, clock=briefs.clock,
                                          precommit=lambda: None)
    before = briefs.store.delivery_snapshot(WORKER, SCOPE, intent.delivery_id)
    worker = WorkerContext("wrong-owner") if wrong == "owner" else WORKER
    token = "stale-token" if wrong == "token" else None if wrong == "missing" else attempt.token
    with pytest.raises(Conflict):
        briefs.store.delivery_unknown(worker, SCOPE, intent.delivery_id, "timeout",
                                      token=token, clock=briefs.clock)
    assert briefs.store.delivery_snapshot(WORKER, SCOPE, intent.delivery_id) == before
    briefs.store.check_delivery_egress(WORKER, intent, token=attempt.token,
                                      clock=briefs.clock, precommit=lambda: None)
    briefs.store.delivery_unknown(WORKER, SCOPE, intent.delivery_id, "interrupted",
                                  token=attempt.token, clock=briefs.clock)
    unknown = briefs.store.delivery_snapshot(WORKER, SCOPE, intent.delivery_id)
    assert unknown.state == "unknown" and unknown.first_failure.reason == "interrupted"
    with pytest.raises(Conflict):
        briefs.store.delivery_unknown(worker, SCOPE, intent.delivery_id, "stopped",
                                      token=token, clock=briefs.clock)
    assert briefs.store.delivery_snapshot(WORKER, SCOPE, intent.delivery_id) == unknown
    with pytest.raises(Conflict):
        briefs.store.check_delivery_egress(WORKER, intent, token=attempt.token,
                                          clock=briefs.clock, precommit=lambda: None)
    assert unknown.uncertainty == before.uncertainty


@pytest.mark.parametrize("wrong", ["owner", "token", "missing", "ordinal", "bool_ordinal",
                                   "candidate", "intent", "settled"])
async def test_observation_custody_fences_egress_diagnostics_and_receipts(ready, wrong):
    service, _ = await candidate_ready(ready)
    intent = ready[2]
    store = BriefStore(service.store.path)
    reader = WorkerContext("independent-reader")
    claim = store.begin_delivery_observation(reader, intent, clock=service.clock,
                                             precommit=lambda: None)
    other = store.begin_delivery_observation(WORKER, intent, clock=service.clock,
                                             precommit=lambda: None)
    assert other.ordinal == claim.ordinal + 1 and other.token != claim.token
    if wrong == "settled":
        store.delivery_unknown(reader, SCOPE, intent.delivery_id, "not_found",
                               observation=claim, clock=service.clock)
    worker = WORKER if wrong == "owner" else reader
    changes = dict(token="stale-read-token", ordinal=other.ordinal,
                   bool_ordinal=True, candidate="om_other_candidate",
                   intent=replace(intent, binding=replace(BINDING, generation=2)))
    altered = claim
    if wrong in changes:
        field = {"candidate": "message_id", "bool_ordinal": "ordinal"}.get(wrong, wrong)
        altered = replace(claim, **{field: changes[wrong]})
    if wrong == "missing":
        altered = None
    before = store.delivery_snapshot(WORKER, SCOPE, intent.delivery_id)
    with pytest.raises(Conflict):
        store.check_delivery_egress(worker, intent, token=None, observation=altered,
                                   clock=service.clock, precommit=lambda: None)
    with pytest.raises(Conflict):
        store.delivery_unknown(worker, SCOPE, intent.delivery_id, "timeout",
                               observation=altered, clock=service.clock)
    with pytest.raises(Conflict):
        store.accept_delivery_observation(worker, altered, (proof(intent),),
                                          clock=service.clock, precommit=lambda: None)
    assert store.delivery_snapshot(WORKER, SCOPE, intent.delivery_id) == before
    # Another read's failure or stale result cannot consume this independent unit.
    store.check_delivery_egress(WORKER, intent, token=None, observation=other,
                               clock=service.clock, precommit=lambda: None)
    accepted = store.accept_delivery_observation(WORKER, other, (proof(intent),),
                                                clock=service.clock, precommit=lambda: None)
    assert accepted.state == "provider_accepted" and accepted.uncertainty == before.uncertainty
    assert accepted.first_failure == before.first_failure
    assert accepted.observations == before.observations
    assert accepted.read_attempts == 2
    assert BriefStore(store.path).delivery_snapshot(WORKER, SCOPE, intent.delivery_id) == accepted


async def test_lost_observation_begin_commit_has_no_diagnostic_custody(ready, monkeypatch):
    from ostag.briefs.delivery import DeliveryError
    service, initial = await candidate_ready(ready)
    original = service.store.begin_delivery_observation
    def lost(*args, **kwargs):
        assert original(*args, **kwargs) is not None
        raise RuntimeError("PRIVATE read claim commit response lost")
    monkeypatch.setattr(service.store, "begin_delivery_observation", lost)
    with pytest.raises(DeliveryError, match="store_error"):
        await service.observe(SCOPE, ready[2].delivery_id)
    result = BriefStore(service.store.path).delivery_snapshot(WORKER, SCOPE, ready[2].delivery_id)
    assert result == replace(initial, read_attempts=1)
    assert not service.transport.reads and len(service.transport.sends) == 1
    monkeypatch.setattr(service.store, "begin_delivery_observation", original)
    result = await service.observe(SCOPE, ready[2].delivery_id)
    assert result.state == "provider_accepted" and result.read_attempts == 2
    assert len(service.transport.reads) == 1 and len(service.transport.sends) == 1
