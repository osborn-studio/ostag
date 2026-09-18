"""Real SQLite and service owners; barriers identify the contested boundary."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import sqlite3
from threading import Event

import pytest

from ostag.briefs.card_delivery import CardDeliveryError, CardDeliveryService
from ostag.briefs.generation import GenerationConfig
from ostag.briefs.lifecycle import BriefLifecycle, LifecycleDenied
from ostag.briefs.retention import RetentionPolicy
from ostag.briefs.store import BriefStore
from ostag.briefs.worker import BriefWorker
from test_brief_card_delivery import make_service, tokens
from test_brief_card_navigation import invoke
from test_brief_card_recovery import recover
from test_brief_cards import SCOPE as CARD_SCOPE
from test_brief_generation import at
from test_brief_openai_transport import Wire, adapter, parameters
from test_brief_revocation import configured
from test_brief_service import Authority, SCOPE, event


def briefs(tmp_path, *, opened=True):
    life = BriefLifecycle()
    authority = Authority()
    service = authority.service(BriefStore(tmp_path/'brief.db', busy_timeout=1), lifecycle=life)
    if opened:
        life.apply(1, 'on', False)
    return life, service


def cards(path, life, **kwargs):
    original = make_service(path, **kwargs)
    return CardDeliveryService(ledger=original.ledger, namespace=original.namespace,
        load=original._load, resolve_binding=original._binding, authorize=original._authorize,
        transport=original.transport, clock=original._clock, timeout=original.timeout,
        lifecycle=life)


def trace_write_wait(store, monkeypatch):
    attempting = Event()
    connection = store._connection
    @contextmanager
    def traced(**kwargs):
        with connection(**kwargs) as db:
            db.set_trace_callback(lambda sql: attempting.set() if sql == 'BEGIN EXCLUSIVE' else None)
            yield db
    monkeypatch.setattr(store, '_connection', traced)
    return attempting


def test_closed_then_new_work_and_normal_pause_drain(tmp_path, monkeypatch):
    life, service = briefs(tmp_path, opened=False)
    worker = BriefWorker(service)
    try:
        assert service.submit(event()).status == 'unavailable'
        assert worker.run_once().result(2) == 'unavailable'
        def forbidden(*a, **k):
            pytest.fail('second owner started a thread')
        with monkeypatch.context() as patch:
            patch.setattr('ostag.briefs.worker.Thread.start', forbidden)
            with pytest.raises(LifecycleDenied):
                BriefWorker(service)
        life.apply(1, 'on', False)
        job = service.submit(event())
        assert worker.run_once().result(3) == 'draft_ready'
        assert service.store.outbox(SCOPE, job.job_id) is not None
        life.apply(2, 'off', False)
        assert life.snapshot().drained
        life.apply(3, 'on', False)
        assert service.submit(event(event_id='new-event')).status == 'queued'
        assert worker.run_once().result(3) == 'draft_ready'
        life.apply(4, 'off', True)
        assert life.snapshot().drained
    finally:
        worker.stop()
        worker.stop()
        service.close()


def test_admission_sqlite_wait_pause_resume_rolls_back(tmp_path, monkeypatch):
    life, service = briefs(tmp_path)
    attempting = trace_write_wait(service.store, monkeypatch)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool, sqlite3.connect(service.store.path) as holder:
            holder.execute('BEGIN IMMEDIATE')
            future = pool.submit(service.submit, event())
            assert attempting.wait(2)
            assert life.snapshot().custody['intake'] == 1
            life.apply(2, 'off', False)
            life.apply(3, 'on', False)
            holder.rollback()
            assert future.result(3).status != 'queued'
        assert service.store.get_by_event(SCOPE, 'event1') is None
        assert service.submit(event()).status == 'queued'
    finally:
        service.close()


def test_worker_claim_wait_is_fenced_after_pause_resume(tmp_path, monkeypatch):
    life, service = briefs(tmp_path)
    job = service.submit(event())
    attempting = trace_write_wait(service.store, monkeypatch)
    worker = BriefWorker(service)
    try:
        with sqlite3.connect(service.store.path) as holder:
            holder.execute('BEGIN IMMEDIATE')
            future = worker.run_once()
            assert attempting.wait(2)
            assert life.snapshot().custody['worker'] == 1
            life.apply(2, 'off', False)
            life.apply(3, 'on', False)
            holder.rollback()
        assert future.result(3) == 'unavailable'
        assert service.store.get(SCOPE, job.job_id).state == 'queued'
        assert service.store.get(SCOPE, job.job_id).attempt == 0
        assert worker.run_once().result(3) == 'draft_ready'
    finally:
        worker.stop()
        service.close()


def test_pause_after_claim_before_provider_reconciles(tmp_path, monkeypatch):
    life, service = briefs(tmp_path)
    job = service.submit(event())
    entered, release = Event(), Event()
    original = service.store.claim_one
    def held(*args, **kwargs):
        result = original(*args, **kwargs)
        entered.set()
        assert release.wait(3)
        return result
    monkeypatch.setattr(service.store, 'claim_one', held)
    worker = BriefWorker(service)
    future = worker.run_once()
    try:
        assert entered.wait(2)
        life.apply(2, 'off', False)
        life.apply(3, 'on', False)
        release.set()
        assert future.result(3) == 'needs_input'
        assert not service.transport.calls
        assert service.store.outbox(SCOPE, job.job_id) is None
        life.apply(4, 'off', False)
        assert life.snapshot().drained
    finally:
        release.set()
        worker.stop()
        service.close()


async def test_cancelled_intake_keeps_running_and_queued_custody(tmp_path, monkeypatch):
    life, service = briefs(tmp_path)
    entered, release, finished = Event(), Event(), Event()
    original = service._submit
    def held(*args):
        entered.set()
        assert release.wait(3)
        try:
            return original(*args)
        finally:
            finished.set()
    monkeypatch.setattr(service, '_submit', held)
    task = asyncio.create_task(service.handle_event(event()))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        service.close()
        life.apply(2, 'off', False)
        assert life.snapshot().custody['intake'] == 1
        assert not life.snapshot().drained
        release.set()
        assert await asyncio.to_thread(finished.wait, 2)
        # Joining the real executor, not the cancelled public task, proves completion.
        await asyncio.to_thread(service._executor.shutdown, wait=True)
        assert life.snapshot().drained
        assert service.store.get_by_event(SCOPE, 'event1') is None
    finally:
        release.set()
        service.close()


def test_actual_provider_per_egress_after_count_wait(tmp_path):
    life, service = briefs(tmp_path)
    entered, release = Event(), Event()
    async def effect(req):
        if req.url.path.endswith('input_tokens'):
            entered.set()
            assert release.wait(3)
    wire = Wire(effect=effect)
    service.config = GenerationConfig(parameters(), 1000000)
    service.transport = adapter(wire)
    job = service.submit(event())
    worker = BriefWorker(service)
    future = worker.run_once()
    try:
        assert entered.wait(2)
        assert life.snapshot().custody['model'] == 1
        life.apply(2, 'off', False)
        life.apply(3, 'on', False)
        release.set()
        assert future.result(3) != 'draft_ready'
        assert len(wire.calls) == 1
        assert service.store.outbox(SCOPE, job.job_id) is None
        assert life.snapshot().uncertain
    finally:
        release.set()
        worker.stop()
        service.close()


def test_detached_suppressing_model_shutdown_never_drains(tmp_path):
    life, service = briefs(tmp_path)
    entered, suppressed, returned = Event(), Event(), Event()
    release = None
    async def effect(request):
        nonlocal release
        release = asyncio.Event()
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            suppressed.set()
            await release.wait()
        returned.set()
    service.transport.effect = effect
    service.submit(event())
    worker = BriefWorker(service)
    future = worker.run_once()
    try:
        assert entered.wait(2)
        life.apply(2, 'off', False)
        assert future.result(3) != 'draft_ready'
        assert suppressed.wait(2)
        assert life.snapshot().custody['model'] == 1
        assert not life.snapshot().drained
        # Permit normal inner completion before teardown; unknown cost stays latched.
        worker._loop.call_soon_threadsafe(release.set)
        assert returned.wait(2)
        worker.stop()
        life.shutdown()
        assert not life.snapshot().drained
        assert life.snapshot().uncertain
    finally:
        worker.stop()
        service.close()


async def test_cards_closed_success_pause_and_private_release_fence(tmp_path, monkeypatch):
    life = BriefLifecycle()
    c = cards(tmp_path/'cards.db', life)
    with pytest.raises(LifecycleDenied):
        await c.prepare(CARD_SCOPE, 'job', 'revision')
    life.apply(1, 'on', False)
    sent = await c.deliver(CARD_SCOPE, 'job', 'revision')
    frozen = c.ledger.frozen(CARD_SCOPE, sent.delivery_id)
    token = tokens(json.loads(frozen.card_json))['Refresh']
    life.apply(2, 'off', False)
    assert life.snapshot().drained
    with pytest.raises(CardDeliveryError):
        invoke(c, token)
    with pytest.raises(LifecycleDenied):
        await recover(c, sent.delivery_id)
    life.apply(3, 'on', False)
    original = c._current
    calls = 0
    def pause_at_final_release(*args, **kwargs):
        nonlocal calls
        original(*args, **kwargs)
        calls += 1
        if calls == 4:  # Final original-authority check after both transactions.
            life.apply(4, 'off', False)
            life.apply(5, 'on', False)
    monkeypatch.setattr(c, '_current', pause_at_final_release)
    with pytest.raises(CardDeliveryError):
        invoke(c, token)
    assert calls == 4
    life.apply(6, 'off', False)
    assert life.snapshot().drained


async def test_detached_send_keeps_custody_after_public_cancel_and_stop(tmp_path):
    life = BriefLifecycle()
    life.apply(1, 'on', False)
    entered, suppressed, release, returned = (asyncio.Event() for _ in range(4))
    original = make_service(tmp_path/'cards.db')
    class Suppressing:
        async def send(self, attempt, *, before_egress):
            await before_egress()
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                suppressed.set()
                await release.wait()
            returned.set()
            return None
        def check_ack(self, *args):
            return None
    c = cards(original.ledger.path, life, transport=Suppressing())
    task = asyncio.create_task(c.deliver(CARD_SCOPE, 'job', 'revision'))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(suppressed.wait(), 2)
        life.apply(2, 'off', False)
        c.stop()
        assert life.snapshot().custody['send'] == 1
        assert not life.snapshot().drained
        release.set()
        await asyncio.wait_for(returned.wait(), 2)
        life.shutdown()
        assert life.snapshot().uncertain and not life.snapshot().drained
    finally:
        release.set()


def test_explicit_cleanup_is_tracked_and_maintenance_denies_it(tmp_path, monkeypatch):
    authority, service, coordinator = configured(tmp_path)
    life = BriefLifecycle()
    life.apply(1, 'on', False)
    job = service.submit(event()).job_id
    coordinator.register(SCOPE, job, policy=RetentionPolicy(at(1)))
    authority.now = 1
    entered, release = Event(), Event()
    original = service.store.retire_local
    def held(*args):
        entered.set()
        assert release.wait(3)
        return original(*args)
    monkeypatch.setattr(service.store, 'retire_local', held)
    try:
        life.apply(2, 'off', True)
        with pytest.raises(LifecycleDenied):
            life.cleanup(coordinator, SCOPE, job)
        life.apply(3, 'off', False)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(life.cleanup, coordinator, SCOPE, job)
            assert entered.wait(2)
            assert life.snapshot().custody['cleanup'] == 1
            with pytest.raises(LifecycleDenied):
                life.apply(4, 'off', True)
            release.set()
            with pytest.raises(LifecycleDenied):
                future.result(3)
        assert coordinator.status(SCOPE, job).cleanup == 'complete'
        life.apply(5, 'off', False)
        assert life.snapshot().drained
        assert 'Prepare a private draft' not in service.store.path.read_bytes().decode(errors='ignore')
    finally:
        release.set()
        service.close()


async def test_executor_queue_survives_cancel_and_close_until_consumed(tmp_path, monkeypatch):
    life, service = briefs(tmp_path)
    release, submitted = Event(), Event()
    original_submit, original_work = service._executor.submit, service._submit
    submissions = []
    executions = []
    def held(*args):
        executions.append(args[0].event_id)
        assert release.wait(3)
        return original_work(*args)
    def submit(*args):
        future = original_submit(*args)
        submissions.append(future)
        if len(submissions) == 5:
            submitted.set()
        return future
    monkeypatch.setattr(service, '_submit', held)
    monkeypatch.setattr(service._executor, 'submit', submit)
    tasks = [asyncio.create_task(service.handle_event(event(event_id=f'queued-{i}')))
             for i in range(5)]
    try:
        assert await asyncio.to_thread(submitted.wait, 2)
        assert life.snapshot().custody['intake'] == 5
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        service.close()
        life.apply(2, 'off', False)
        assert life.snapshot().custody['intake'] == 5
        release.set()
        await asyncio.to_thread(service._executor.shutdown, wait=True)
        assert len(executions) == 5
        assert life.snapshot().drained
    finally:
        release.set()
        service.close()


async def test_card_prepare_wait_does_not_use_resumed_epoch(tmp_path, monkeypatch):
    life = BriefLifecycle()
    life.apply(1, 'on', False)
    c = cards(tmp_path/'cards.db', life)
    entered = asyncio.Event()
    original = c.ledger.prepare
    def attempting(*args, **kwargs):
        entered.set()
        return original(*args, **kwargs)
    monkeypatch.setattr(c.ledger, 'prepare', attempting)
    with sqlite3.connect(c.ledger.path) as holder:
        holder.execute('BEGIN IMMEDIATE')
        task = asyncio.create_task(c.prepare(CARD_SCOPE, 'job', 'revision'))
        await asyncio.wait_for(entered.wait(), 2)
        life.apply(2, 'off', False)
        life.apply(3, 'on', False)
        holder.rollback()
    with pytest.raises(CardDeliveryError):
        await task
    with sqlite3.connect(c.ledger.path) as db:
        assert db.execute('SELECT count(*) FROM cards').fetchone()[0] == 0
    assert await c.prepare(CARD_SCOPE, 'job', 'revision') is not None


async def test_card_actual_transport_rechecks_after_connection_wait(tmp_path, monkeypatch):
    import httpx
    life = BriefLifecycle()
    life.apply(1, 'on', False)
    entered, release = asyncio.Event(), asyncio.Event()
    sends = []
    c = cards(tmp_path/'cards.db', life, handler=lambda r: sends.append(r))
    original = httpx.AsyncClient.__aenter__
    async def held(client):
        result = await original(client)
        entered.set()
        await release.wait()
        return result
    monkeypatch.setattr(httpx.AsyncClient, '__aenter__', held)
    task = asyncio.create_task(c.deliver(CARD_SCOPE, 'job', 'revision'))
    await asyncio.wait_for(entered.wait(), 2)
    life.apply(2, 'off', False)
    life.apply(3, 'on', False)
    release.set()
    with pytest.raises(LifecycleDenied):
        await task
    assert sends == []
    assert life.snapshot().uncertain


def test_recovery_wait_pause_resume_preserves_original_fence(tmp_path, monkeypatch):
    from ostag.briefs.store import WorkerContext
    life = BriefLifecycle()
    life.apply(1, 'on', False)
    authority = Authority()
    store = BriefStore(tmp_path/'brief.db', busy_timeout=1)
    service = authority.service(store, lifecycle=life)
    submitted = service.submit(event())
    claimed = store.claim_one(WorkerContext('previous'), clock=service.clock)
    authority.now = 31
    attempting = trace_write_wait(store, monkeypatch)
    worker = BriefWorker(service)
    try:
        with sqlite3.connect(store.path) as holder:
            holder.execute('BEGIN IMMEDIATE')
            future = worker.run_once()
            assert attempting.wait(2)
            life.apply(2, 'off', False)
            life.apply(3, 'on', False)
            holder.rollback()
        assert future.result(3) == 'unavailable'
        current = store.get(SCOPE, submitted.job_id)
        assert current.fence == claimed.fence and current.attempt == claimed.attempt
        assert current.state == 'collecting'
        assert worker.run_once().result(3) == 'draft_ready'
    finally:
        worker.stop()
        service.close()


def test_bounded_stop_of_running_model_retains_custody(tmp_path):
    life, service = briefs(tmp_path)
    entered, release = Event(), Event()
    async def held(_):
        entered.set()
        assert release.wait(3)
    service.transport.effect = held
    service.submit(event())
    worker = BriefWorker(service)
    worker.run_once()
    try:
        assert entered.wait(2)
        assert worker.stop(timeout=0) == 'local_stopping'
        service.close()
        life.apply(2, 'off', False)
        snapshot = life.snapshot()
        assert snapshot.custody['model'] == snapshot.custody['worker'] == 1
        assert not snapshot.drained
        life.shutdown()
        assert not life.snapshot().drained
    finally:
        release.set()
        worker._thread.join(3)
        assert not worker._thread.is_alive()
        worker.stop()
        service.close()


async def test_recovery_final_clock_cannot_revive_old_epoch(tmp_path, monkeypatch):
    life = BriefLifecycle()
    life.apply(1, 'on', False)
    c = cards(tmp_path/'cards.db', life)
    sent = await c.deliver(CARD_SCOPE, 'job', 'revision')
    original = c.ledger.recovery_step
    def paused_after_commit(*args, **kwargs):
        result = original(*args, **kwargs)
        if kwargs.get('rendered') is not None:
            life.apply(2, 'off', False)
            life.apply(3, 'on', False)
        return result
    monkeypatch.setattr(c.ledger, 'recovery_step', paused_after_commit)
    with pytest.raises(CardDeliveryError):
        await recover(c, sent.delivery_id)
    with sqlite3.connect(c.ledger.path) as db:
        assert db.execute('SELECT count(*) FROM card_tokens').fetchone()[0] == 0
    life.apply(4, 'off', False)
    assert life.snapshot().drained


async def test_shared_card_observer_does_not_invent_custody_or_poison_sender(tmp_path):
    import httpx
    from test_brief_card_delivery import ack
    life = BriefLifecycle()
    life.apply(1, 'on', False)
    entered, release = asyncio.Event(), asyncio.Event()
    async def handler(request):
        entered.set()
        await release.wait()
        return httpx.Response(200, json=ack())
    sender = cards(tmp_path/'cards.db', life, handler=handler)
    observer = cards(sender.ledger.path, life)
    task = asyncio.create_task(sender.deliver(CARD_SCOPE, 'job', 'revision'))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert (await observer.deliver(CARD_SCOPE, 'job', 'revision')).state == 'started'
        snapshot = life.snapshot()
        assert snapshot.custody['card'] == snapshot.custody['send'] == 1
        assert not snapshot.uncertain
        release.set()
        assert (await task).state == 'create_acknowledged'
        life.apply(2, 'off', False)
        assert life.snapshot().drained
    finally:
        release.set()
