"""A10 synthetic local revocation; no provider/network credentials."""
from threading import Event
import pytest
from ostag.briefs.retention import RetentionCoordinator, RetentionLedger, RetentionPolicy
from ostag.briefs.store import BriefStore, LeaseLost
from ostag.briefs.worker import BriefWorker
from test_brief_service import Authority, SCOPE, event
from test_brief_generation import at
import asyncio
from contextlib import contextmanager
import sqlite3
import httpx
from ostag.briefs.generation import GenerationConfig
from ostag.briefs.retention import RetentionDenied
from ostag.briefs.worker import CALL_ID
from test_brief_openai_transport import Wire, adapter, parameters




def configured(tmp_path, **targets):
    authority = Authority()
    store = BriefStore(tmp_path / 'brief.db')
    ledger = RetentionLedger(tmp_path / 'retention.db')
    ledger.initialize()
    coordinator = RetentionCoordinator(ledger=ledger, store=store,
        clock=lambda: at(authority.now), **targets)
    service = authority.service(store, retention=coordinator.guard(store))
    return authority, service, coordinator


def test_revoke_active_generation_blocks_late_result_and_durable_fence(tmp_path):
    authority, service, coordinator = configured(tmp_path)
    entered, release = Event(), Event()
    async def pause(_):
        entered.set()
        assert release.wait(3)
    service.transport.effect = pause
    submitted = service.submit(event())
    coordinator.register(SCOPE, submitted.job_id, policy=RetentionPolicy(at(60)))
    worker = BriefWorker(service)
    future = worker.run_once()
    try:
        assert entered.wait(2)
        lease = service.store.get(SCOPE, submitted.job_id).lease
        result = coordinator.revoke_job(SCOPE, submitted.job_id)
        assert result.decision == 'revoked'
        with pytest.raises(LeaseLost):
            service.store.assert_current(lease, clock=service.clock)
        release.set()
        assert future.result(3) != 'draft_ready'
        assert service.store.outbox(SCOPE, submitted.job_id) is None
        assert coordinator.cleanup(SCOPE, submitted.job_id).cleanup == 'complete'
    finally:
        release.set()
        worker.stop()
        service.close()





@pytest.mark.parametrize('phase', ['connection', 'count', 'response'])
@pytest.mark.parametrize('operation', ['revoke', 'expire'])
def test_actual_a04_provider_gates_after_await_and_between_count_create(
        tmp_path, monkeypatch, phase, operation):
    a, service, c = configured(tmp_path)
    entered, release = Event(), Event()
    async def effect(req):
        if ((phase == 'count' and req.url.path.endswith('input_tokens'))
                or (phase == 'response' and req.url.path.endswith('/responses'))):
            entered.set()
            assert release.wait(3)
    wire = Wire(effect=effect)
    service.config = a.config = GenerationConfig(parameters(), 1000000)
    service.transport = adapter(wire)
    if phase == 'connection':
        original = httpx.AsyncClient.__aenter__
        async def waiting(client):
            result = await original(client)
            entered.set()
            # Client context-entry wait only; actual transport waits are covered below.
            await asyncio.to_thread(release.wait, 3)
            return result
        monkeypatch.setattr(httpx.AsyncClient, '__aenter__', waiting)
    job = service.submit(event()).job_id
    c.register(SCOPE, job, policy=RetentionPolicy(at(10)))
    worker = BriefWorker(service)
    future = worker.run_once()
    try:
        assert entered.wait(2)
        lease = service.store.get(SCOPE, job).lease
        if operation == 'revoke':
            c.revoke_job(SCOPE, job)
        else:
            a.now = 10
            c.expire(SCOPE, job)
        with pytest.raises(LeaseLost):
            service.store.assert_current(lease, clock=service.clock)
        release.set()
        assert future.result(3) != 'draft_ready'
        assert len(wire.calls) == {'connection': 0, 'count': 1, 'response': 2}[phase]
        assert service.store.outbox(SCOPE, job) is None
        cost = service.store.cost_record(SCOPE, job, CALL_ID)
        assert cost.state in ('unknown', 'settled') and cost.charged_micros > 0
        assert c.cleanup(SCOPE, job).cleanup == 'complete'
        assert service.store.cost_record(SCOPE, job, CALL_ID).charged_micros == cost.charged_micros
    finally:
        release.set()
        worker.stop()
        service.close()


def test_completed_provider_reply_revoke_during_actual_completion_lock_wait(tmp_path, monkeypatch):
    _, service, c = configured(tmp_path)
    job = service.submit(event()).job_id
    c.register(SCOPE, job, policy=RetentionPolicy(at(60)))
    before, proceed, attempting = Event(), Event(), Event()
    original_complete = service.store.complete
    original_connection = service.store._connection
    def complete(*args, **kwargs):
        before.set()
        assert proceed.wait(3)
        return original_complete(*args, **kwargs)
    @contextmanager
    def traced(**kwargs):
        with original_connection(**kwargs) as db:
            db.set_trace_callback(lambda sql: attempting.set() if sql == 'BEGIN EXCLUSIVE' else None)
            yield db
    monkeypatch.setattr(service.store, 'complete', complete)
    worker = BriefWorker(service)
    future = worker.run_once()
    try:
        assert before.wait(2)
        holder = sqlite3.connect(service.store.path)
        holder.execute('BEGIN IMMEDIATE')
        try:
            monkeypatch.setattr(service.store, '_connection', traced)
            proceed.set()
            assert attempting.wait(1)  # SQLite traced BEGIN while another writer owns the lock.
            result = c.revoke_job(SCOPE, job)  # Tombstone succeeds; primary fence may be pending.
            assert result.decision == 'revoked' and result.cleanup == 'pending'
        finally:
            holder.rollback()
            holder.close()
        assert future.result(3) != 'draft_ready'
        with sqlite3.connect(service.store.path) as db:
            assert db.execute('SELECT count(*) FROM brief_revisions').fetchone()[0] == 0
            assert db.execute('SELECT count(*) FROM brief_outbox').fetchone()[0] == 0
        assert c.cleanup(SCOPE, job).cleanup == 'complete'
    finally:
        proceed.set()
        worker.stop()
        service.close()


@pytest.mark.parametrize('change', ['valid', 'expiry', 'rewind'])
def test_completion_clock_after_confirmed_reader_lock_wait(tmp_path, monkeypatch, change):
    """A host UTC rollback is a ClockError denial, never permission to publish.

    Keep this distinct from expiry (LeaseLost). A real competing reader forces
    SQLITE_BUSY before advancing/rewinding the trusted clock; no sleep assumes
    that the publisher has entered SQLite. Mirrors and retirement are unchanged.
    """
    from concurrent.futures import ThreadPoolExecutor
    from ostag.briefs.store import ClockError, WorkerContext

    authority, service, coordinator = configured(tmp_path)
    attempted, release = Event(), Event()
    original = service.store._connection
    try:
        job = service.submit(event()).job_id
        coordinator.register(SCOPE, job, policy=RetentionPolicy(at(60)))
        claimed = service.store.claim_one(WorkerContext('clock-wait'), clock=service.clock)
        authority.now = 1
        service.store.start_generating(claimed.lease, clock=service.clock)
        before = service.store.get(SCOPE, job)

        @contextmanager
        def contended(**kwargs):
            with original(**kwargs) as db:
                class Connection:
                    def __getattr__(self, name):
                        return getattr(db, name)

                    def execute(self, sql, *args):
                        if sql == 'BEGIN EXCLUSIVE':
                            db.execute('PRAGMA busy_timeout=1')
                            with pytest.raises(sqlite3.OperationalError) as caught:
                                db.execute(sql, *args)
                            assert caught.value.sqlite_errorcode == sqlite3.SQLITE_BUSY
                            attempted.set()
                            assert release.wait(3)
                            db.execute('PRAGMA busy_timeout=1000')
                        return db.execute(sql, *args)
                yield Connection()

        with sqlite3.connect(service.store.path) as reader:
            reader.execute('BEGIN')
            reader.execute('SELECT * FROM brief_jobs').fetchall()
            with ThreadPoolExecutor(max_workers=1) as pool:
                with monkeypatch.context() as patch:
                    patch.setattr(service.store, '_connection', contended)
                    future = pool.submit(service.store.complete, claimed.lease,
                        draft={}, provenance={'model': 'synthetic'}, validation_version='v1',
                        clock=service.clock, precommit=lambda: coordinator.guard(service.store).check(
                            SCOPE, job))
                    try:
                        assert attempted.wait(2)
                        if change == 'expiry':
                            authority.now = claimed.execution_seconds
                        elif change == 'rewind':
                            authority.now = 0
                    finally:
                        reader.rollback()
                        release.set()
                    if change == 'valid':
                        assert future.result(3).job_id == job
                    else:
                        with pytest.raises(ClockError if change == 'rewind' else LeaseLost):
                            future.result(3)
        reopened = BriefStore(service.store.path)
        if change == 'valid':
            assert reopened.outbox(SCOPE, job) is not None
        else:
            assert reopened.get(SCOPE, job) == before
            assert reopened.outbox(SCOPE, job) is None
            with sqlite3.connect(reopened.path) as db:
                assert db.execute('SELECT count(*) FROM brief_revisions').fetchone()[0] == 0
    finally:
        release.set()
        service.close()


@pytest.mark.parametrize('failure', ['untracked', 'missing', 'busy', 'removed_guard', 'flag'])
def test_enabled_worker_never_skips_unavailable_retention_gate(tmp_path, failure):
    _, service, c = configured(tmp_path)
    job = service.submit(event()).job_id
    holder = None
    if failure != 'untracked':
        c.register(SCOPE, job, policy=RetentionPolicy(at(60)))
    if failure == 'missing':
        c.ledger.path.rename(tmp_path/'unavailable.db')
    elif failure == 'busy':
        holder = sqlite3.connect(c.ledger.path)
        holder.execute('BEGIN EXCLUSIVE')
    elif failure == 'removed_guard':
        service.retention = None
    elif failure == 'flag':
        service.retention = True
    worker = BriefWorker(service)
    try:
        assert worker.run_once().result(3) != 'draft_ready'
        assert not service.transport.calls
        assert service.store.outbox(SCOPE, job) is None
    finally:
        if holder:
            holder.rollback()
            holder.close()
        worker.stop()
        service.close()


@pytest.mark.parametrize('phase', ['untracked', 'count', 'response'])
def test_denied_generation_reports_local_checkpoint_not_provider_or_store_fault(tmp_path, phase):
    """Exclusive expiry/untracked denial keeps A04's existing sanitized cause.

    No revoke, expire() or local cancellation signal participates here, so the
    gate alone must deny. Reporting a purely local authority outcome as
    transport_error would assert a provider fault that never happened, and
    store_error would hide the denial as infrastructure unavailability.
    """
    a, service, c = configured(tmp_path)
    entered, release = Event(), Event()
    async def effect(req):
        if ((phase == 'count' and req.url.path.endswith('input_tokens'))
                or (phase == 'response' and req.url.path.endswith('/responses'))):
            entered.set()
            assert release.wait(3)
    wire = Wire(effect=effect)
    service.config = a.config = GenerationConfig(parameters(), 1000000)
    service.transport = adapter(wire)
    job = service.submit(event()).job_id
    if phase != 'untracked':
        c.register(SCOPE, job, policy=RetentionPolicy(at(10)))
    worker = BriefWorker(service)
    future = worker.run_once()
    try:
        if phase != 'untracked':
            assert entered.wait(2)
            a.now = 10  # exclusive expiry only; nothing else fences this attempt
            release.set()
        assert future.result(3) != 'draft_ready'
        assert service.store.failure_cause(SCOPE, job) == 'checkpoint_denied'
        assert service.store.outbox(SCOPE, job) is None
        assert len(wire.calls) == {'untracked': 0, 'count': 1, 'response': 2}[phase]
        cost = service.store.cost_record(SCOPE, job, CALL_ID)
        if phase == 'untracked':
            assert cost is None  # denied before any reservation exists
        else:
            # Conservative unknown/settled spend, diagnosed as a local denial.
            assert cost.diagnostic == 'checkpoint_denied'
            assert cost.state in ('unknown', 'settled') and cost.charged_micros > 0
    finally:
        release.set()
        worker.stop()
        service.close()


async def test_text_delivery_denial_reason_is_local_authorization(tmp_path):
    from test_brief_retention import full_stack
    from ostag.briefs.delivery import DeliveryError, DeliveryIntent
    from test_brief_cards import BINDING
    _, briefs, c, p, text, _, _, _, _ = full_stack(tmp_path)
    try:
        intent = DeliveryIntent.from_outbox(SCOPE, briefs.store.outbox(SCOPE, p.job_id), BINDING)
        await text.prepare(intent)
        c.revoke_job(SCOPE, p.job_id)
        for call in (lambda: text.prepare(intent),
                     lambda: text.send(SCOPE, intent.delivery_id),
                     lambda: text.observe(SCOPE, intent.delivery_id)):
            with pytest.raises(DeliveryError) as caught:
                await call()
            # Identical classification to the original authorize() callback.
            assert caught.value.args == ('authorization_denied',)
        assert not text.transport.sends and not text.transport.reads
    finally:
        briefs.close()


@pytest.mark.parametrize('change', ['cancel', 'cancel_and_revoke', 'revoke',
                                    'conflict', 'store_error', 'late_revoke'])
async def test_legacy_text_cancellation_keeps_local_failure_custody(tmp_path, monkeypatch, change):
    """An awaited local cancellation returns unknown, never a remote failure/retry."""
    from ostag.briefs.delivery import DeliveryError, DeliveryIntent, DeliveryService
    from ostag.briefs.store import Conflict, WorkerContext
    from test_brief_delivery_observation import BINDING, Transport
    authority = Authority()
    briefs = authority.service(BriefStore(tmp_path / 'legacy.db'))
    worker = BriefWorker(briefs)
    entered, release = asyncio.Event(), asyncio.Event()
    calls, allowed = [], True
    task = None

    def authorize(_):
        if not allowed:
            raise RuntimeError('PRIVATE authority failure')

    async def waiting(attempt, *, before_egress):
        entered.set()
        await release.wait()
        before_egress()
        calls.append(attempt)
        raise AssertionError('denied send escaped')

    try:
        job = briefs.submit(event()).job_id
        assert worker.run_once().result(3) == 'draft_ready'
        worker.stop()
        briefs.store.enable_delivery()
        intent = DeliveryIntent.from_outbox(SCOPE, briefs.store.outbox(SCOPE, job), BINDING)
        text = DeliveryService(store=briefs.store, worker=WorkerContext('legacy-text'),
            transport=Transport(), authorize=authorize, clock=briefs.clock)
        text.transport.send = waiting
        await text.prepare(intent)
        task = asyncio.create_task(text.send(SCOPE, intent.delivery_id))
        await asyncio.wait_for(entered.wait(), 2)
        if change in ('cancel', 'cancel_and_revoke', 'late_revoke'):
            briefs.store.cancel(SCOPE, job, clock=briefs.clock)
        if change in ('revoke', 'cancel_and_revoke'):
            allowed = False
        if change in ('conflict', 'store_error'):
            def deny(*args, **kwargs):
                raise (Conflict if change == 'conflict' else RuntimeError)('PRIVATE failure')
            monkeypatch.setattr(briefs.store, 'check_delivery_egress', deny)
        if change == 'late_revoke':
            original = briefs.store.delivery_snapshot
            def snapshot(*args, **kwargs):
                nonlocal allowed
                result = original(*args, **kwargs)
                allowed = False  # changes during fallback DB read, before private release
                return result
            monkeypatch.setattr(briefs.store, 'delivery_snapshot', snapshot)
        release.set()
        if change == 'cancel':
            result = await asyncio.wait_for(task, 2)
            assert result.state == 'unknown' and result.generation_state == 'cancelled'
            assert result.first_failure.reason == 'conflict'
            assert result.receipt_json is None and not result.human_received
            assert (await text.send(SCOPE, intent.delivery_id)).state == 'unknown'
        else:
            expected = ('authorization_denied' if 'revoke' in change else change)
            with pytest.raises(DeliveryError, match=expected) as caught:
                await asyncio.wait_for(task, 2)
            assert caught.value.__context__ is None
        assert not calls
        row = briefs.store.delivery_snapshot(text.worker, SCOPE, intent.delivery_id)
        assert row.state == 'unknown' and row.receipt_json is None
        assert row.first_failure.reason != 'transport_error'
    finally:
        release.set()
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        worker.stop()
        briefs.close()


async def test_text_awaited_egress_and_late_acceptance_fail_closed(tmp_path):
    from test_brief_retention import full_stack
    from ostag.briefs.delivery import DeliveryIntent, DeliveryError
    from test_brief_cards import BINDING
    _, briefs, c, p, text, _, _, _, _ = full_stack(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    sends = []
    async def waiting(attempt, *, before_egress):
        entered.set()
        await release.wait()
        before_egress()
        sends.append(attempt)
        raise AssertionError('retired egress')
    text.transport.send = waiting
    try:
        intent = DeliveryIntent.from_outbox(SCOPE, briefs.store.outbox(SCOPE, p.job_id), BINDING)
        await text.prepare(intent)
        task = asyncio.create_task(text.send(SCOPE, intent.delivery_id))
        await asyncio.wait_for(entered.wait(), 2)
        c.revoke_job(SCOPE, p.job_id)
        assert c.cleanup(SCOPE, p.job_id).cleanup == 'complete'
        release.set()
        with pytest.raises((DeliveryError, RetentionDenied)):
            await task
        assert not sends
        with sqlite3.connect(briefs.store.path) as db:
            assert db.execute('SELECT intent_json,receipt_json FROM brief_deliveries').fetchone() == (
                '{"retired":true}', None)
    finally:
        release.set()
        briefs.close()


@pytest.mark.parametrize('phase', ['egress', 'ack'])
async def test_card_awaited_egress_and_ack_cannot_repopulate(tmp_path, monkeypatch, phase):
    from test_brief_retention import full_stack, MARKER, sql_dump
    from ostag.briefs.card_delivery import CardCreateAck, CardDeliveryError
    from test_brief_cards import BINDING
    _, briefs, c, p, _, card, _, _, _ = full_stack(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    egress, accepted = [], []
    original_ack = card.ledger.acknowledge
    def acknowledge(*args, **kwargs):
        accepted.append(True)
        return original_ack(*args, **kwargs)
    monkeypatch.setattr(card.ledger, 'acknowledge', acknowledge)
    class WaitingTransport:
        async def send(self, attempt, *, before_egress):
            if phase == 'ack':
                await before_egress()
                egress.append(attempt)
            entered.set()
            await release.wait()
            if phase == 'egress':
                await before_egress()
                egress.append(attempt)
            return CardCreateAck(owner=attempt.owner, token=attempt.token,
                request_digest=attempt.intent.digest, binding=BINDING, destination='oc_private',
                message_id='om_message_123', sender_id='actual_sender', sender_id_type='app_id')
        def check_ack(self, attempt, ack):
            return ack
    card.transport = WaitingTransport()
    try:
        prepared = await card.prepare(SCOPE, p.job_id, p.revision_id)
        task = asyncio.create_task(card.send(SCOPE, prepared.delivery_id))
        await asyncio.wait_for(entered.wait(), 2)
        c.revoke_job(SCOPE, p.job_id)
        assert c.cleanup(SCOPE, p.job_id).cleanup == 'complete'
        release.set()
        try:
            result = await task
            assert result.cancelled and not result.content_verified
        except CardDeliveryError:
            pass
        assert len(egress) == len(accepted) == (1 if phase == 'ack' else 0)
        assert MARKER not in sql_dump(card.ledger.path)
        with sqlite3.connect(card.ledger.path) as db:
            assert db.execute('SELECT count(*) FROM card_tokens').fetchone()[0] == 0
            assert db.execute('SELECT preview_json FROM cards').fetchone()[0] == '{"retired":true}'
    finally:
        release.set()
        briefs.close()


@pytest.mark.parametrize('late', [False, True])
async def test_text_observation_guard_after_real_await(tmp_path, late):
    from test_brief_retention import full_stack, MARKER, sql_dump
    from ostag.briefs.delivery import DeliveryIntent, DeliveryError, SendResponse
    from test_brief_cards import BINDING
    from test_brief_delivery_observation import MESSAGE_ID, proof
    _, briefs, c, p, text, _, _, _, _ = full_stack(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    reads = []
    async def unknown(attempt):
        return SendResponse(attempt.token, MESSAGE_ID, ())
    text.transport.send_effect = unknown
    async def observe(intent, message_id, *, before_egress):
        if late:
            before_egress()
            reads.append(message_id)
        entered.set()
        await release.wait()
        if not late:
            before_egress()
            reads.append(message_id)
        return (proof(intent),)
    text.transport.observe = observe
    try:
        intent = DeliveryIntent.from_outbox(SCOPE, briefs.store.outbox(SCOPE, p.job_id), BINDING)
        await text.prepare(intent)
        sent = await text.send(SCOPE, intent.delivery_id)
        assert sent.state == 'unknown'
        task = asyncio.create_task(text.observe(SCOPE, intent.delivery_id))
        await asyncio.wait_for(entered.wait(), 2)
        c.revoke_job(SCOPE, p.job_id)
        assert c.cleanup(SCOPE, p.job_id).cleanup == 'complete'
        release.set()
        with pytest.raises((DeliveryError, RetentionDenied)):
            await task
        assert len(reads) == int(late)
        assert MARKER not in sql_dump(briefs.store.path)
        with sqlite3.connect(briefs.store.path) as db:
            assert db.execute('SELECT count(*) FROM brief_delivery_observations').fetchone()[0] == 0
    finally:
        release.set()
        briefs.close()


def test_signed_callback_revoke_after_render_before_private_release(tmp_path, monkeypatch):
    from test_brief_retention import full_stack, MARKER
    from test_brief_cards import signed_app, signed_post
    from fastapi.testclient import TestClient
    import ostag.briefs.callbacks as callbacks
    _, briefs, c, p, _, _, callback, _, _ = full_stack(tmp_path)
    try:
        token = callback.issue(p, message_id='om_message_123')
        original = callbacks.render_private
        def render(*args, **kwargs):
            result = original(*args, **kwargs)
            c.revoke_job(SCOPE, p.job_id)
            return result
        monkeypatch.setattr(callbacks, 'render_private', render)
        with TestClient(signed_app(tmp_path, callback)) as client:
            response = signed_post(client, token)
        assert response.status_code == 200 and MARKER not in response.text
        assert c.cleanup(SCOPE, p.job_id).cleanup == 'complete'
    finally:
        briefs.close()


@pytest.mark.parametrize('operation', ['correct', 'adopt'])
def test_a06_revocation_during_actual_ledger_lock_wait(tmp_path, monkeypatch, operation):
    from concurrent.futures import ThreadPoolExecutor
    from test_brief_retention import full_stack, MARKER
    from ostag.briefs.revisions import RevisionError
    _, briefs, c, p, _, _, _, revisions, _ = full_stack(tmp_path)
    attempting = Event()
    try:
        revisions.register(SCOPE, p.job_id)
        original = revisions.ledger._connection
        @contextmanager
        def traced(**kwargs):
            with original(**kwargs) as db:
                db.set_trace_callback(lambda sql: attempting.set() if sql == 'BEGIN EXCLUSIVE' else None)
                yield db
        monkeypatch.setattr(revisions.ledger, '_connection', traced)
        def operation_call():
            with pytest.raises(RevisionError):
                getattr(revisions, operation)(SCOPE, p.job_id,
                    expected_revision_id=p.revision_id, request_id='race',
                    **({'text': MARKER} if operation == 'correct' else {}))
        with sqlite3.connect(revisions.ledger.path) as holder, ThreadPoolExecutor(1) as pool:
            holder.execute('BEGIN IMMEDIATE')
            future = pool.submit(operation_call)
            assert attempting.wait(1)
            # Tombstone commits independently; revoke does not acquire the A06 lock.
            c.revoke_job(SCOPE, p.job_id)
            holder.rollback()
            future.result(2)
        with sqlite3.connect(revisions.ledger.path) as db:
            assert db.execute('SELECT count(*) FROM revision_events').fetchone()[0] == 0
        assert c.cleanup(SCOPE, p.job_id).cleanup == 'complete'
    finally:
        briefs.close()


def test_missing_guard_on_real_private_services_fails_closed(tmp_path):
    from test_brief_retention import full_stack
    from ostag.briefs.callbacks import CallbackError
    from ostag.briefs.revisions import RevisionError
    _, briefs, _, p, _, _, callback, revisions, _ = full_stack(tmp_path)
    try:
        callback.retention = None
        revisions.retention = True
        with pytest.raises(CallbackError):
            callback.prepare_preview(SCOPE, p.job_id, p.revision_id)
        with pytest.raises(RevisionError):
            revisions.register(SCOPE, p.job_id)
    finally:
        briefs.close()


class RetentionContention:
    """Own a real writer; release only after SQLite itself reports SQLITE_BUSY.

    A test connection proxy uses a 1ms native busy budget for the first BEGIN,
    then retries with the normal 50ms budget after the event-coordinated release.
    This is a witnessed native lock wait, not a pre-lock trace callback race.
    Both attempts use the actual gate connection/transaction. Total contention
    must finish within the production 50ms limit; no arbitrary sleeps. Requiring
    COMMIT prevents a timeout denial masquerading as an authority-ordering fix.
    """
    def __init__(self, monkeypatch, coordinator, change):
        from concurrent.futures import ThreadPoolExecutor
        self.pool = ThreadPoolExecutor(1)
        self.owned, self.attempted, self.released = Event(), Event(), Event()
        self.armed = False
        self.committed = False
        self.change = change
        self.path = coordinator.ledger.path
        original = coordinator.ledger._connection

        @contextmanager
        def connection(**kwargs):
            with original(**kwargs) as db:
                if self.armed:
                    self.armed = False
                    self.future = self.pool.submit(self.hold)
                    assert self.owned.wait(2)
                    race = self
                    class ContendedConnection:
                        def __getattr__(self, name):
                            return getattr(db, name)

                        def execute(self, sql, *args):
                            if sql != 'BEGIN IMMEDIATE':
                                return db.execute(sql, *args)
                            from time import monotonic
                            start = monotonic()
                            db.execute('PRAGMA busy_timeout=1')
                            try:
                                db.execute(sql, *args)
                            except sqlite3.OperationalError as exc:
                                assert exc.sqlite_errorcode == sqlite3.SQLITE_BUSY
                            else:
                                raise AssertionError('contender did not encounter the writer')
                            race.attempted.set()
                            assert race.released.wait(2)
                            db.execute('PRAGMA busy_timeout=50')
                            result = db.execute(sql, *args)
                            assert monotonic() - start < 0.05
                            return result

                        def commit(self):
                            db.commit()
                            race.committed = True
                    yield ContendedConnection()
                else:
                    yield db
        monkeypatch.setattr(coordinator.ledger, '_connection', connection)

    def hold(self):
        # This connection is created, locked, rolled back and closed by its owner.
        with sqlite3.connect(self.path, isolation_level=None) as db:
            db.execute('BEGIN IMMEDIATE')
            self.owned.set()
            assert self.attempted.wait(2)
            self.change()
            db.rollback()
            self.released.set()

    def arm(self):
        self.armed = True

    def close(self):
        try:
            self.future.result(2)
            assert self.attempted.is_set() and self.committed
        finally:
            self.pool.shutdown(wait=True)


def _original_mirror(authority, revoked):
    def authorize(_):
        if authority.now >= 10 or revoked[0]:
            raise PermissionError('original_authority_denied')
    return authorize


def _combined_mirror(authority, revoked, coordinator, job, change, final_ready):
    original = _original_mirror(authority, revoked)
    def authorize(value):
        original(value)
        if final_ready.is_set():
            final_ready.clear()
            if change == 'retention_expiry':
                authority.now = 60  # equality must deny after this callback
            elif change == 'retention_revoke':
                # Deliberate contract violation: defensive local signal coverage,
                # not permission for A11 mirrors to perform blocking mutations.
                coordinator.revoke_job(SCOPE, job)
    return authorize


@pytest.mark.parametrize('path', ['preview', 'signed_callback', 'revision_render',
                                  'revision_correct', 'revision_adopt'])
@pytest.mark.parametrize('change', ['expiry', 'revoke', 'retention_expiry', 'retention_revoke'])
def test_original_authority_after_retention_contention(tmp_path, monkeypatch, path, change):
    from test_brief_retention import full_stack, MARKER
    from ostag.briefs.callbacks import CallbackError
    from ostag.briefs.revisions import RevisionError
    from test_brief_cards import signed_app, signed_post
    from fastapi.testclient import TestClient
    import ostag.briefs.callbacks as callbacks

    a, briefs, c, p, _, _, callback, revisions, _ = full_stack(tmp_path)
    revoked = [False]
    final_ready = Event()
    callback._authorize = revisions._authorize = _combined_mirror(
        a, revoked, c, p.job_id, change, final_ready)
    revisions.register(SCOPE, p.job_id)
    token = callback.issue(p, message_id='om_message_123')
    a.now = 9
    def change_authority():
        if change == 'expiry':
            a.now = 10
        elif change == 'revoke':
            revoked[0] = True
        else:
            final_ready.set()
    race = RetentionContention(monkeypatch, c, change_authority)
    if path in ('preview', 'signed_callback'):
        original = callbacks.render_private
        def render(*args, **kwargs):
            result = original(*args, **kwargs)
            race.arm()
            return result
        monkeypatch.setattr(callbacks, 'render_private', render)
    else:
        original = revisions._fresh
        calls = 0
        def fresh(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:  # final fence, after rendering/event INSERT
                race.arm()
            return original(*args, **kwargs)
        monkeypatch.setattr(revisions, '_fresh', fresh)
    try:
        if path == 'signed_callback':
            with TestClient(signed_app(tmp_path, callback)) as client:
                response = signed_post(client, token)
            assert MARKER not in response.text
        elif path == 'preview':
            with pytest.raises(CallbackError):
                callback.prepare_preview(SCOPE, p.job_id, p.revision_id)
        else:
            operation = path.removeprefix('revision_')
            kwargs = {} if operation == 'render' else dict(
                expected_revision_id=p.revision_id, request_id='ordering-race')
            if operation == 'correct':
                kwargs['text'] = MARKER
            with pytest.raises(RevisionError):
                getattr(revisions, operation)(SCOPE, p.job_id, **kwargs)
            with sqlite3.connect(revisions.ledger.path) as db:
                assert db.execute('SELECT count(*) FROM revision_events').fetchone()[0] == 0
    finally:
        race.close()
        briefs.close()


@pytest.mark.parametrize('kind', ['text', 'card'])
@pytest.mark.parametrize('calls', ['valid', 'missing', 'repeated', 'swallowed_repeat'])
async def test_retention_transport_gate_preserves_single_send_custody(tmp_path, kind, calls):
    from test_brief_retention import full_stack
    from test_brief_cards import BINDING
    from ostag.briefs.delivery import DeliveryIntent
    _, briefs, _, p, text, card, _, _, _ = full_stack(tmp_path)
    target = card if kind == 'card' else text
    original = target.transport.send

    async def send(attempt, *, before_egress):
        async def call():
            if kind == 'card':
                await before_egress()
            else:
                before_egress()

        if calls == 'repeated':
            await call()
        if calls == 'missing':
            async def no_gate():
                pass
            return await original(attempt, before_egress=no_gate if kind == 'card' else lambda: None)
        response = await original(attempt, before_egress=before_egress)
        if calls == 'swallowed_repeat':
            try:
                await call()
            except Exception:
                pass
        return response

    target.transport.send = send
    try:
        if kind == 'card':
            prepared = await card.prepare(SCOPE, p.job_id, p.revision_id)
        else:
            intent = DeliveryIntent.from_outbox(SCOPE, briefs.store.outbox(SCOPE, p.job_id), BINDING)
            prepared = await text.prepare(intent)
        result = await target.send(SCOPE, prepared.delivery_id)
        expected = ('create_acknowledged' if kind == 'card' else 'provider_accepted')
        assert result.state == (expected if calls == 'valid' else 'unknown')
        if kind == 'text' and calls != 'valid':
            assert result.receipt_json is None
    finally:
        briefs.close()


@pytest.mark.parametrize('phase', ['count', 'create', 'response', 'completion'])
@pytest.mark.parametrize('change', ['acl', 'grant', 'grant_revision', 'destination',
                                   'permission', 'expiry', 'valid'])
def test_generation_original_mirrors_after_retention_contention(
        tmp_path, monkeypatch, phase, change):
    """Real A04 gates must reread mutable authority after a witnessed SQL wait."""
    from dataclasses import replace
    from ostag.briefs.store import PrivateDestination
    from ostag.config import Settings

    a, service, c = configured(tmp_path)
    wire = Wire()
    service.config = a.config = GenerationConfig(parameters(), 1000000)
    service.transport = adapter(wire)
    if change == 'expiry':
        a.grant = replace(a.grant, expires_at=at(10))
    job = service.submit(event()).job_id
    c.register(SCOPE, job, policy=RetentionPolicy(at(60)))
    a.now = 9

    def change_authority():
        if change == 'acl':
            a.sources.pop('s0')
        elif change == 'grant':
            a.grant = replace(a.grant, revoked_at=at(9))
        elif change == 'grant_revision':
            a.grant = replace(a.grant, revision=2)
        elif change == 'destination':
            a.destination = PrivateDestination(SCOPE.principal, 'changed-private-dm')
        elif change == 'permission':
            a.settings = Settings(default_permission_level='read')
        elif change == 'expiry':
            a.now = 10

    race = RetentionContention(monkeypatch, c, change_authority)
    if phase in ('count', 'create'):
        original = httpx.AsyncClient.build_request
        def build(client, method, url, **kwargs):
            result = original(client, method, url, **kwargs)
            if str(url).endswith('/responses/input_tokens' if phase == 'count' else '/responses'):
                race.arm()  # Immediately before the real adapter's final reauthorize.
            return result
        monkeypatch.setattr(httpx.AsyncClient, 'build_request', build)
    elif phase == 'response':
        original = service.transport.generate
        async def generate(request):
            result = await original(request)
            race.arm()  # Response received; actual settlement precedes acceptance.
            return result
        monkeypatch.setattr(service.transport, 'generate', generate)
    else:
        original = service.store.complete
        def complete(*args, **kwargs):
            race.arm()  # Actual primary SQL writes precede the final precommit gate.
            return original(*args, **kwargs)
        monkeypatch.setattr(service.store, 'complete', complete)
    worker = BriefWorker(service)
    try:
        result = worker.run_once().result(3)
        if change == 'valid':
            assert result == 'draft_ready'
            assert len(wire.calls) == 2
            assert service.store.outbox(SCOPE, job) is not None
        else:
            assert len(wire.calls) == {'count': 0, 'create': 1,
                                       'response': 2, 'completion': 2}[phase]
            assert result == 'needs_input'
            assert service.store.outbox(SCOPE, job) is None
            with sqlite3.connect(service.store.path) as db:
                assert db.execute('SELECT count(*) FROM brief_revisions').fetchone()[0] == 0
            assert service.store.failure_cause(SCOPE, job) in (
                'checkpoint_denied', 'authorization_denied')
        cost = service.store.cost_record(SCOPE, job, CALL_ID)
        assert cost.state == ('unknown' if change != 'valid' and phase in ('count', 'create')
                              else 'settled')
        assert cost.charged_micros > 0
    finally:
        worker.stop()
        race.close()
        service.close()


@pytest.mark.parametrize('change', ['expiry', 'rollback', 'job_revoke', 'source_revoke',
                                   'grant_revoke', 'unrelated', 'valid'])
def test_generation_final_retention_fence_after_original_mirror(tmp_path, monkeypatch, change):
    from ostag.briefs.service import ManualRequest
    from ostag.briefs.store import RequesterContext
    from ostag.core.identity import Principal
    from ostag.core.schemas import SchemaError
    from test_brief_service import request

    a, service, c = configured(tmp_path, maintenance=lambda *args: None)
    job = service.submit(event()).job_id
    c.register(SCOPE, job, policy=RetentionPolicy(at(60)))
    a.now = 9
    final_ready = Event()
    original = service.grants
    def grants(identifier):
        result = original(identifier)
        if final_ready.is_set():
            final_ready.clear()
            if change == 'expiry':
                a.now = 60
            elif change == 'rollback':
                a.now = 8
            elif change == 'job_revoke':
                c.revoke_job(SCOPE, job)
            elif change == 'source_revoke':
                c.revoke_source(SCOPE, SCOPE, 'r0')
            elif change == 'grant_revoke':
                c.revoke_grant(SCOPE, SCOPE, 'grant')
            elif change == 'unrelated':
                other = RequesterContext(SCOPE.namespace, Principal('feishu', 'bob'))
                c.revoke_source(SCOPE, other, 'r0')
        return result
    # Reentrant mutations deliberately violate the production mirror contract;
    # completed signals from this coordinator must still deny defensively.
    service.grants = grants
    race = RetentionContention(monkeypatch, c, final_ready.set)
    race.arm()
    try:
        def check():
            service.authorize_current(ManualRequest.from_dict(request()), SCOPE,
                a.destination, stopped=lambda: False, job_id=job)
        if change in ('valid', 'unrelated'):
            check()
        else:
            with pytest.raises(SchemaError):
                check()
        assert not final_ready.is_set(), 'original mirrors must follow the retention wait'
        if change == 'expiry':
            assert c.status(SCOPE, job).decision == 'expired'
    finally:
        race.close()
        service.close()


@pytest.mark.parametrize('phase', ['initial', 'precommit'])
@pytest.mark.parametrize('change', ['acl', 'retention_expiry', 'valid'])
def test_intake_original_mirror_after_retention_contention(tmp_path, monkeypatch, phase, change):
    a, service, c = configured(tmp_path)
    job = service.submit(event()).job_id
    c.register(SCOPE, job, policy=RetentionPolicy(at(60)))
    a.now = 9
    final_ready = Event()
    original_lookup = service.sources
    def lookup(*args):
        value = original_lookup(*args)
        if final_ready.is_set():
            final_ready.clear()
            a.now = 60
        return value
    service.sources = lookup
    def change_authority():
        if change == 'acl':
            a.sources.pop('s0')
        elif change == 'retention_expiry':
            final_ready.set()
    race = RetentionContention(monkeypatch, c, change_authority)
    if phase == 'initial':
        race.arm()
    else:
        original_admit = service.store.admit
        def admit(*args, **kwargs):
            race.arm()
            return original_admit(*args, **kwargs)
        monkeypatch.setattr(service.store, 'admit', admit)
    try:
        result = service.submit(event(event_id='new-event'))
        if change == 'valid':
            assert result.status == 'queued'
        else:
            assert result.status in ('denied', 'invalid_request')
            assert service.store.get_by_event(SCOPE, 'new-event') is None
    finally:
        race.close()
        service.close()


@pytest.mark.parametrize('kind,phase', [('text', 'egress'), ('card', 'egress'),
    ('text', 'ack'), ('card', 'ack'), ('text', 'read_egress'),
    ('text', 'read_accept'), ('text', 'private_result')])
@pytest.mark.parametrize('change', ['expiry', 'revoke', 'retention_expiry', 'retention_revoke'])
async def test_transport_original_authority_after_retention_contention(
        tmp_path, monkeypatch, kind, phase, change):
    import json
    from test_brief_retention import full_stack
    from test_brief_cards import BINDING
    from test_brief_card_delivery import ack
    from test_brief_feishu_transport import resource, MID
    from ostag.briefs.delivery import DeliveryIntent, DeliveryError
    from ostag.briefs.feishu_transport import CredentialSnapshot, FeishuTransport

    a, briefs, c, p, text, card, _, _, _ = full_stack(tmp_path)
    intent = DeliveryIntent.from_outbox(SCOPE, briefs.store.outbox(SCOPE, p.job_id), BINDING)
    calls, revoked = [], [False]
    final_ready = Event()
    text.authorize = card._authorize = _combined_mirror(
        a, revoked, c, p.job_id, change, final_ready)
    credential = CredentialSnapshot(BINDING, 'synthetic-token', 'actual_sender_id', 'app_id',
                                    (('oc_private', SCOPE.principal),))
    def handler(request):
        calls.append(request.method)
        data = resource(body={'content': json.dumps(json.loads(intent.wire_body)['content'])})
        if phase.startswith('read') and request.method == 'POST':
            data.pop('body')  # preserve the candidate but require a later read
        if request.method == 'GET':
            data = {'items': [data]}
        return httpx.Response(200, json={'code': 0, 'data': data})
    text.transport = FeishuTransport(resolve=lambda: credential,
                                    transport=httpx.MockTransport(handler))
    if kind == 'card':
        card.transport._transport = httpx.MockTransport(
            lambda request: (calls.append(request.method), httpx.Response(200, json=ack()))[1])
        prepared = await card.prepare(SCOPE, p.job_id, p.revision_id)
    else:
        prepared = await text.prepare(intent)
        if phase.startswith('read'):
            assert (await text.send(SCOPE, intent.delivery_id)).candidate_id == MID
            calls.clear()
    a.now = 9
    def change_authority():
        if change == 'expiry':
            a.now = 10
        elif change == 'revoke':
            revoked[0] = True
        else:
            final_ready.set()
    race = RetentionContention(monkeypatch, c, change_authority)
    service = card if kind == 'card' else text
    if phase in ('egress', 'read_egress'):
        transport_call = getattr(service.transport, 'observe' if phase == 'read_egress' else 'send')
        async def transport(*args, before_egress):
            # Both real adapters invoke their gate at the HTTPX transport boundary.
            # Each service's gate has a SQL precommit check and a final check.
            method = '_current' if kind == 'card' else '_policy'
            original = getattr(service, method)
            checks = 0
            def final(*a, **kw):
                nonlocal checks
                checks += 1
                if checks == 2:
                    race.arm()
                return original(*a, **kw)
            monkeypatch.setattr(service, method, final)
            return await transport_call(*args, before_egress=before_egress)
        monkeypatch.setattr(service.transport, 'observe' if phase == 'read_egress' else 'send', transport)
    elif phase == 'private_result':
        original = text._retained_result
        def result(*args):
            race.arm()
            return original(*args)
        monkeypatch.setattr(text, '_retained_result', result)
    else:
        target = card.ledger if kind == 'card' else briefs.store
        method = ('acknowledge' if kind == 'card' else 'accept_delivery_observation'
                  if phase == 'read_accept' else 'accept_delivery_response')
        original = getattr(target, method)
        def accepting(*args, **kwargs):
            race.arm()
            return original(*args, **kwargs)
        monkeypatch.setattr(target, method, accepting)
    try:
        if kind == 'card':
            result = await card.send(SCOPE, prepared.delivery_id)
            assert result.state == 'unknown'
            assert not result.content_verified
        else:
            with pytest.raises(DeliveryError) as caught:
                await (text.observe(SCOPE, intent.delivery_id) if phase.startswith('read')
                       else text.send(SCOPE, intent.delivery_id))
            assert caught.value.args == ('authorization_denied',)
        assert len(calls) == (0 if phase in ('egress', 'read_egress') else 1)
        if phase != 'private_result':
            with sqlite3.connect(card.ledger.path if kind == 'card' else briefs.store.path) as db:
                table = 'cards' if kind == 'card' else 'brief_deliveries'
                assert db.execute('SELECT state FROM ' + table).fetchone()[0] == 'unknown'
                if kind == 'text':
                    assert db.execute('SELECT receipt_json FROM brief_deliveries').fetchone()[0] is None
    finally:
        race.close()
        briefs.close()
@pytest.mark.parametrize('phase', ['egress', 'failure', 'reply'])
async def test_completed_cleanup_survives_actual_late_text_transport(tmp_path, phase):
    from test_brief_retention import full_stack, MARKER, sql_dump
    from ostag.briefs.delivery import DeliveryIntent, DeliveryError, SendResponse
    from ostag.briefs.store import Conflict, WorkerContext
    from test_brief_cards import BINDING
    from test_brief_delivery_observation import MESSAGE_ID, proof
    _, briefs, c, p, text, _, _, _, _ = full_stack(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    handed_off = []
    task = None

    async def waiting(attempt, *, before_egress):
        if phase != 'egress':
            before_egress()
            handed_off.append(attempt)
        entered.set()
        await release.wait()
        if phase == 'egress':
            before_egress()
            handed_off.append(attempt)
        if phase == 'reply':
            return SendResponse(attempt.token, MESSAGE_ID, (proof(attempt.intent),))
        raise RuntimeError(MARKER)

    text.transport.send = waiting
    try:
        intent = DeliveryIntent.from_outbox(SCOPE, briefs.store.outbox(SCOPE, p.job_id), BINDING)
        await text.prepare(intent)
        task = asyncio.create_task(text.send(SCOPE, intent.delivery_id))
        await asyncio.wait_for(entered.wait(), 2)
        c.revoke_job(SCOPE, p.job_id)
        assert c.cleanup(SCOPE, p.job_id).cleanup == 'complete'
        release.set()
        with pytest.raises(DeliveryError) as caught:
            await asyncio.wait_for(task, 2)
        assert caught.value.args == (('transport_error' if phase == 'failure' else 'conflict'),)
        assert caught.value.__context__ is None
        assert len(handed_off) == (0 if phase == 'egress' else 1)
        assert c.status(SCOPE, p.job_id).cleanup == 'complete'
        with sqlite3.connect(briefs.store.path) as db:
            row = db.execute('SELECT state,first_failure_json,intent_json,receipt_json '
                             'FROM brief_deliveries').fetchone()
            assert row[0] == 'unknown'
            assert caught.value.args[0] in row[1]
            assert row[2:] == ('{"retired":true}', None)
            assert db.execute('SELECT count(*) FROM brief_delivery_observations').fetchone()[0] == 0
        reopened = BriefStore(briefs.store.path)
        with pytest.raises(Conflict):
            reopened.delivery_snapshot(WorkerContext('reopened-reader'), SCOPE, intent.delivery_id)
        for operation in (text.send, text.observe):
            with pytest.raises(DeliveryError):
                await operation(SCOPE, intent.delivery_id)
        assert len(handed_off) == (0 if phase == 'egress' else 1)
        assert not text.transport.reads
        assert c.cleanup(SCOPE, p.job_id).cleanup == 'complete'
        assert MARKER not in sql_dump(reopened.path)
        assert MARKER.encode() not in reopened.path.read_bytes()
    finally:
        release.set()
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        briefs.close()


@contextmanager
def _loopback_tls_certificate(tmp_path):
    """Ephemeral self-signed fixture; no remote endpoint or credentials."""
    import ssl
    import subprocess
    key, cert = tmp_path / 'key.pem', tmp_path / 'cert.pem'
    subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
                    '-keyout', str(key), '-out', str(cert), '-days', '1',
                    '-subj', '/CN=synthetic.invalid'], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    yield context


class LoopbackTLS:
    """Real TCP/TLS + HTTP/1.1, with an event barrier in the network backend.

    The only rewiring is a loopback destination and fixture TLS trust. Production
    request URL/auth, HTTPX, httpcore framing and socket writes remain exercised.
    Captures even partial bodies so aborting a leaked request cannot hide it.
    """
    def __init__(self, context, monkeypatch, reply, phase='tls', connection=1):
        self.context, self.monkeypatch, self.reply = context, monkeypatch, reply
        self.phase, self.connection = phase, connection
        self.arrived, self.release = Event(), Event()
        self.bodies, self.headers, self.tasks = [], [], set()
        self.connections = 0

    async def __aenter__(self):
        from httpcore._backends.auto import AutoBackend
        try:
            self.server = await asyncio.start_server(self.handle, '127.0.0.1', 0, ssl=self.context)
        except PermissionError:
            pytest.skip('sandbox forbids loopback bind; parent must run real TCP/TLS gate')
        local_port = self.server.sockets[0].getsockname()[1]
        fixture = self
        original = AutoBackend.connect_tcp
        class Stream:
            def __init__(self, stream, number):
                self.stream, self.number = stream, number
            def __getattr__(self, name):
                return getattr(self.stream, name)
            async def pause(self, phase):
                if fixture.phase == phase and self.number == fixture.connection:
                    fixture.arrived.set()
                    assert await asyncio.to_thread(fixture.release.wait, 3)
            async def start_tls(self, ssl_context, server_hostname=None, timeout=None):
                import ssl
                # This applies ONLY to our freshly generated loopback certificate.
                ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                ssl_context.check_hostname = False
                ssl_context.verify_mode = ssl.CERT_NONE
                self.stream = await self.stream.start_tls(ssl_context, server_hostname, timeout)
                await self.pause('tls')
                return self
            async def write(self, buffer, timeout=None):
                await self.stream.write(buffer, timeout)
                if buffer.startswith((b'POST ', b'GET ')):
                    await self.pause('headers')
        async def connect(backend, host, port, *args, **kwargs):
            assert host in ('api.openai.com', 'open.feishu.cn') and port == 443
            fixture.connections += 1
            number = fixture.connections
            return Stream(await original(backend, '127.0.0.1', local_port, *args, **kwargs), number)
        self.monkeypatch.setattr(AutoBackend, 'connect_tcp', connect)
        return self

    async def handle(self, reader, writer):
        import json
        self.tasks.add(asyncio.current_task())
        body = bytearray()
        try:
            header = await reader.readuntil(b'\r\n\r\n')
            self.headers.append(header)
            fields = dict(line.lower().split(b':', 1) for line in header.split(b'\r\n')[1:]
                          if b':' in line)
            if b'chunked' in fields.get(b'transfer-encoding', b''):
                while True:
                    n = int((await reader.readline()).strip(), 16)
                    if not n:
                        await reader.readline()
                        break
                    body.extend(await reader.readexactly(n))
                    assert await reader.readexactly(2) == b'\r\n'
            else:
                n = int(fields.get(b'content-length', b'0'))
                while len(body) < n:
                    chunk = await reader.read(n - len(body))
                    if not chunk:
                        return
                    body.extend(chunk)
            payload = json.dumps(self.reply(header, bytes(body))).encode()
            writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: '
                         + str(len(payload)).encode() + b'\r\nConnection: close\r\n\r\n' + payload)
            await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            if body:
                self.bodies.append(bytes(body))
            writer.close()
            try:
                # A denied/cancelled client may never finish close_notify. Keep
                # the capture, but do not inherit asyncio's long TLS shutdown
                # timeout (or leak a handler into the next test's event loop).
                async with asyncio.timeout(0.25):
                    await writer.wait_closed()
            except (ConnectionError, TimeoutError):
                writer.transport.abort()
            finally:
                self.tasks.discard(asyncio.current_task())

    async def __aexit__(self, *args):
        self.release.set()
        self.server.close()
        await self.server.wait_closed()
        if self.tasks:
            tasks = list(self.tasks)
            # First drain bytes already sent. After the client operation ends,
            # an idle peer can remain when cancellation raced TLS establishment.
            # Cancel its read only after this grace period; handle's finally
            # preserves partial captures and bounds TLS teardown independently.
            _, pending = await asyncio.wait(tasks, timeout=1)
            for task in pending:
                task.cancel()
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for result in results:
                if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
                    raise result


class BackendWait(LoopbackTLS):
    """Socket-free complement: real HTTPX/httpcore, awaited network backend seam."""
    async def __aenter__(self):
        import json
        import httpcore
        from httpcore._backends.auto import AutoBackend
        fixture = self
        class Stream(httpcore.AsyncMockStream):
            async def pause(self, phase):
                if fixture.phase == phase and self.number == fixture.connection:
                    fixture.arrived.set()
                    assert await asyncio.to_thread(fixture.release.wait, 3)
            async def start_tls(self, *args, **kwargs):
                await self.pause('tls')
                return self
            async def write(self, buffer, timeout=None):
                if buffer.startswith((b'POST ', b'GET ')):
                    fixture.headers.append(buffer)
                    payload = json.dumps(fixture.reply(buffer, b'')).encode()
                    self._buffer = [b'HTTP/1.1 200 OK\r\nContent-Length: '
                                    + str(len(payload)).encode()
                                    + b'\r\nConnection: close\r\n\r\n' + payload]
                    await self.pause('headers')
                elif buffer:
                    fixture.bodies.append(buffer)
                await super().write(buffer, timeout)
        async def connect(backend, host, port, *args, **kwargs):
            assert host in ('api.openai.com', 'open.feishu.cn') and port == 443
            fixture.connections += 1
            stream = Stream([])
            stream.number = fixture.connections
            return stream
        self.monkeypatch.setattr(AutoBackend, 'connect_tcp', connect)
        return self

    async def __aexit__(self, *args):
        self.release.set()


@pytest.mark.parametrize('shutdown', ['timeout', 'stall'])
@pytest.mark.parametrize('idle_peer', [False, True])
async def test_loopback_tls_shutdown_is_bounded_and_preserves_capture(
        monkeypatch, shutdown, idle_peer):
    """A denied client need not complete TLS close_notify on every platform."""
    wire = LoopbackTLS(None, monkeypatch, lambda header, body: {'ok': True})
    entered = asyncio.Event()

    class Reader(asyncio.StreamReader):
        async def readuntil(self, separator):
            entered.set()
            return await super().readuntil(separator)

    reader = Reader()
    if not idle_peer:
        reader.feed_data(b'POST / HTTP/1.1\r\nContent-Length: 7\r\n\r\nprivate')
        reader.feed_eof()

    class Writer:
        aborted = False

        @property
        def transport(self):
            return self

        def write(self, data):
            pass

        async def drain(self):
            pass

        def close(self):
            pass

        async def wait_closed(self):
            if shutdown == 'timeout':
                raise TimeoutError('SSL shutdown timed out')
            await asyncio.Event().wait()

        def abort(self):
            self.aborted = True

    writer = Writer()
    if idle_peer:
        class Server:
            def close(self):
                pass

            async def wait_closed(self):
                pass

        wire.server = Server()
        handler = asyncio.create_task(wire.handle(reader, writer))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            await asyncio.wait_for(wire.__aexit__(), 2)
            assert handler.cancelled()
        finally:
            if not handler.done():
                handler.cancel()
            await asyncio.gather(handler, return_exceptions=True)
    else:
        await asyncio.wait_for(wire.handle(reader, writer), 1)
    assert writer.aborted
    assert wire.bodies == ([] if idle_peer else [b'private'])
    assert not wire.tasks


@pytest.mark.parametrize('network', ['backend', 'loopback'])
@pytest.mark.parametrize('phase', ['tls', 'headers'])
@pytest.mark.parametrize('connection', [1, 2], ids=['count', 'create'])
@pytest.mark.parametrize('change', ['valid', 'expiry', 'revoke'])
async def test_provider_real_tls_wait_retains_no_private_body_after_denial(
        tmp_path, monkeypatch, phase, connection, change, network):
    from test_brief_openai_transport import envelope
    from ostag.briefs.openai_transport import OpenAIReadOnlyTransport
    a, service, coordinator = configured(tmp_path)
    service.config = a.config = GenerationConfig(parameters(), 1000000)
    service.transport = OpenAIReadOnlyTransport(api_key='synthetic-token')
    job = service.submit(event()).job_id
    coordinator.register(SCOPE, job, policy=RetentionPolicy(at(10)))
    worker = BriefWorker(service)
    def reply(header, body):
        return ({'object': 'response.input_tokens', 'input_tokens': 100}
                if b'/input_tokens ' in header else envelope())
    try:
        with _loopback_tls_certificate(tmp_path) as context:
            async with (LoopbackTLS if network == 'loopback' else BackendWait)(
                    context, monkeypatch, reply, phase, connection) as wire:
                future = asyncio.wrap_future(worker.run_once())
                assert await asyncio.to_thread(wire.arrived.wait, 3)
                if change == 'expiry':
                    a.now = 10  # No expire()/cleanup: boundary equality alone denies.
                elif change == 'revoke':
                    coordinator.revoke_job(SCOPE, job)
                wire.release.set()
                result = await asyncio.wait_for(future, 3)
            assert len(wire.bodies) == (2 if change == 'valid' else connection - 1)
            cost = service.store.cost_record(SCOPE, job, CALL_ID)
            assert cost.charged_micros > 0
            if change != 'valid':
                assert result != 'draft_ready'
                assert service.store.outbox(SCOPE, job) is None
                assert cost.state == 'unknown'
            else:
                assert result == 'draft_ready'
                assert cost.state == 'settled'
    finally:
        worker.stop()
        service.close()


@pytest.mark.parametrize('kind', ['text', 'card'])
@pytest.mark.parametrize('network', ['backend', 'loopback'])
@pytest.mark.parametrize('phase', ['tls', 'headers'])
@pytest.mark.parametrize('change', ['valid', 'expiry', 'revoke'])
async def test_feishu_real_tls_wait_rechecks_retention_before_private_body(
        tmp_path, monkeypatch, kind, phase, change, network):
    import json
    from test_brief_retention import full_stack, MARKER
    from test_brief_card_delivery import ack
    from test_brief_cards import BINDING
    from test_brief_feishu_transport import resource
    from ostag.briefs.delivery import DeliveryIntent, DeliveryError
    from ostag.briefs.feishu_transport import CredentialSnapshot, FeishuTransport
    a, briefs, coordinator, p, text, card, _, _, _ = full_stack(tmp_path)
    intent = DeliveryIntent.from_outbox(SCOPE, briefs.store.outbox(SCOPE, p.job_id), BINDING)
    credential = CredentialSnapshot(BINDING, 'synthetic-token', 'actual_sender_id', 'app_id',
                                    (('oc_private', SCOPE.principal),))
    text.transport = FeishuTransport(resolve=lambda: credential)
    card.transport._transport = None
    target = card if kind == 'card' else text
    prepared = (await card.prepare(SCOPE, p.job_id, p.revision_id) if kind == 'card'
                else await text.prepare(intent))
    def reply(header, body):
        return ack() if kind == 'card' else {'code': 0, 'data': resource(
            body={'content': json.dumps(json.loads(intent.wire_body)['content'])})}
    try:
        with _loopback_tls_certificate(tmp_path) as context:
            async with (LoopbackTLS if network == 'loopback' else BackendWait)(
                    context, monkeypatch, reply, phase) as wire:
                task = asyncio.create_task(target.send(SCOPE, prepared.delivery_id))
                assert await asyncio.to_thread(wire.arrived.wait, 3)
                if change == 'expiry':
                    a.now = 60
                elif change == 'revoke':
                    coordinator.revoke_job(SCOPE, p.job_id)
                wire.release.set()
                try:
                    result = await asyncio.wait_for(task, 3)
                except DeliveryError:
                    assert kind == 'text' and change != 'valid'
                else:
                    if kind == 'card':
                        assert result.state == ('create_acknowledged' if change == 'valid' else 'unknown')
            assert any(MARKER.encode() in body for body in wire.bodies) == (change == 'valid')
    finally:
        briefs.close()


@pytest.mark.parametrize('change', ['valid', 'rotate', 'false_check'])
async def test_card_ack_current_credential_after_real_retention_lock(tmp_path, monkeypatch, change):
    import json
    from dataclasses import replace
    from test_brief_retention import full_stack
    from test_brief_card_delivery import tokens
    from test_brief_card_navigation import invoke
    from ostag.briefs.card_delivery import CardDeliveryError
    _, briefs, coordinator, p, _, card, _, _, _ = full_stack(tmp_path)
    current = [card.transport._resolve()]
    card.transport._resolve = lambda: current[0]
    def rotate():
        if change == 'rotate':
            current[0] = replace(current[0], access_token='rotated-synthetic-token')
    race = RetentionContention(monkeypatch, coordinator, rotate)
    original = card.ledger.acknowledge
    def acknowledge(*args, **kwargs):
        race.arm()
        return original(*args, **kwargs)
    monkeypatch.setattr(card.ledger, 'acknowledge', acknowledge)
    if change == 'false_check':
        monkeypatch.setattr(card.transport, 'check_ack', lambda *args: False)
    try:
        result = await card.deliver(SCOPE, p.job_id, p.revision_id)
        assert result.state == ('create_acknowledged' if change == 'valid' else 'unknown')
        token = tokens(json.loads(card.ledger.frozen(SCOPE, result.delivery_id).card_json))['Refresh']
        if change == 'valid':
            assert invoke(card, token)
        else:
            with pytest.raises(CardDeliveryError):
                invoke(card, token)
        with sqlite3.connect(card.ledger.path) as db:
            assert db.execute("SELECT count(*) FROM cards WHERE state='create_acknowledged'").fetchone()[0] == (
                1 if change == 'valid' else 0)
    finally:
        race.close()
        briefs.close()
