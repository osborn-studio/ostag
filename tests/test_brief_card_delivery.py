"""Independent card-v1 outbox; never mutate or reuse U01 delivery."""
import asyncio
from contextlib import contextmanager
from dataclasses import replace
import json
from queue import Empty
import sqlite3
from time import monotonic, sleep

import httpx
import pytest

from ostag.briefs.card_delivery import CardDeliveryError, CardDeliveryService, CardLedger
from ostag.briefs.card_transport import CardTransport
from ostag.briefs.feishu_transport import CredentialSnapshot
from test_brief_cards import ACTOR, BINDING, SCOPE, preview


def make_service(path, *, current=None, now=None, authorize=None, handler=None,
                 transport=None, capacity=20, timeout=1, binding=None):
    current = current if current is not None else [replace(preview(), outbox_id='original_outbox')]
    now = now if now is not None else [1000]
    ledger = CardLedger(path, capacity=capacity)
    ledger.initialize()
    credential = CredentialSnapshot(BINDING, 'secret-token', 'actual_sender', 'app_id',
                                   (('oc_private', ACTOR),))
    if transport is None:
        transport = CardTransport(resolve=lambda: credential, transport=httpx.MockTransport(
            handler or (lambda r: httpx.Response(200, json=ack()))))
    return CardDeliveryService(ledger=ledger, namespace=SCOPE.namespace,
        load=lambda s, j: current[0], resolve_binding=binding or (lambda: BINDING),
        authorize=authorize or (lambda p: None), transport=transport, clock=lambda: now[0],
        timeout=timeout)


def ack(**changes):
    return {'code': 0, 'data': dict(message_id='om_message_123', msg_type='interactive',
        chat_id='oc_private', deleted=False, updated=False, sender=dict(id='actual_sender',
        id_type='app_id', sender_type='app', tenant_key='tenant')) | changes}


def tokens(card):
    return {a['text']['content']: a['value']['callback_id']
            for e in card['elements'] for a in e.get('actions', []) if 'value' in a}


async def _wait_for_card_entry(task, entered):
    waiter = asyncio.create_task(entered.wait())
    try:
        done, _ = await asyncio.wait({task, waiter}, timeout=3,
                                     return_when=asyncio.FIRST_COMPLETED)
        if task in done:
            task.result()
            pytest.fail('card operation completed before held transport entry')
        assert waiter in done, 'card transport did not reach entry'
    finally:
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)


async def _release_card(service, task, release, exchanges):
    if not task.done():
        service.stop()
    release.set()
    # Join the owner before its exchange: it may still be scheduling that task.
    for tasks in ([task], exchanges):
        if not tasks:
            continue
        done, pending = await asyncio.wait(set(tasks), timeout=3)
        for completed in done:
            if not completed.cancelled():
                completed.exception()
        assert not pending, 'released card operation did not finish'
    assert service.active_operations == 0


async def test_frozen_prepare_reopen_one_actual_send(tmp_path):
    path, calls, now = tmp_path/'cards.db', [], [1000]
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=ack())
    c = make_service(path, now=now, handler=handler)
    first = await c.prepare(SCOPE, 'job', 'revision')
    original = c.ledger.frozen(SCOPE, first.delivery_id)
    now[0] += 1
    c2 = make_service(path, now=now, handler=handler)
    assert await c2.prepare(SCOPE, 'job', 'revision') == first
    assert c2.ledger.frozen(SCOPE, first.delivery_id) == original
    result = await c.deliver(SCOPE, 'job', 'revision')
    assert result.state == 'create_acknowledged'
    assert result.content_verified is result.human_received is False
    assert (await c2.deliver(SCOPE, 'job', 'revision')).state == 'create_acknowledged'
    assert len(calls) == 1
    assert json.loads(calls[0].content)['content'] == original.card_json
    assert 'Private draft' not in repr(first) and 'bc_' not in repr(original)
    assert 'secret-token' not in repr(result) and 'om_message' not in repr(result)


async def test_same_revision_changed_content_conflicts(tmp_path):
    current = [replace(preview(), outbox_id='original_outbox')]
    c = make_service(tmp_path/'cards.db', current=current)
    first = await c.prepare(SCOPE, 'job', 'revision')
    current[0] = replace(preview(text='Changed private draft'), outbox_id='original_outbox')
    with pytest.raises(CardDeliveryError):
        await c.prepare(SCOPE, 'job', 'revision')
    with pytest.raises(CardDeliveryError):
        await c.send(SCOPE, first.delivery_id)


async def test_lost_response_never_resends_or_activates(tmp_path):
    calls = []
    def lost(request):
        calls.append(request)
        raise RuntimeError('PRIVATE BODY TOKEN')
    path = tmp_path/'cards.db'
    c = make_service(path, handler=lost)
    result = await c.deliver(SCOPE, 'job', 'revision')
    assert result.state == 'unknown'
    c2 = make_service(path, handler=lost)
    assert (await c2.deliver(SCOPE, 'job', 'revision')).state == 'unknown'
    assert len(calls) == 1
    with sqlite3.connect(path) as db:
        row = db.execute('SELECT uncertainty,first_failure,message_id FROM cards').fetchone()
    assert 'send_unverified' in row[0] and row[2] is None and 'PRIVATE' not in str(row)


async def test_capacity_and_expired_send_tombstone(tmp_path):
    now = [1000]
    c = make_service(tmp_path/'cards.db', now=now, capacity=1)
    first = await c.prepare(SCOPE, 'job', 'revision', ttl=1)
    now[0] = 1001
    with pytest.raises(CardDeliveryError):
        await c.send(SCOPE, first.delivery_id)
    assert (await c.prepare(SCOPE, 'job', 'revision')).delivery_id == first.delivery_id
    assert c.ledger.frozen(SCOPE, first.delivery_id).expires == 1001000000


async def test_two_claimants_loser_cannot_poison_winner(tmp_path):
    entered, finish = asyncio.Event(), asyncio.Event()
    calls, exchanges = [], []
    async def handler(request):
        exchanges.append(asyncio.current_task())
        calls.append(request)
        entered.set()
        await finish.wait()
        return httpx.Response(200, json=ack())
    path = tmp_path/'cards.db'
    c = make_service(path, handler=handler)
    c2 = make_service(path, handler=handler)
    first = await c.prepare(SCOPE, 'job', 'revision')
    task = asyncio.create_task(c.send(SCOPE, first.delivery_id))
    try:
        await _wait_for_card_entry(task, entered)
        assert (await c2.send(SCOPE, first.delivery_id)).state == 'started'
        finish.set()
        assert (await task).state == 'create_acknowledged'
        assert len(calls) == 1
    finally:
        await _release_card(c, task, finish, exchanges)
        c2.stop()


@pytest.mark.parametrize('fail_refresh', [False, True])
async def test_stale_status_losing_claim_refresh_never_diagnoses_winner(
        tmp_path, monkeypatch, fail_refresh):
    entered, finish = asyncio.Event(), asyncio.Event()
    calls = []
    async def handler(request):
        calls.append(request)
        entered.set()
        await finish.wait()
        return httpx.Response(200, json=ack())
    path = tmp_path/'cards.db'
    winner = make_service(path, handler=handler, timeout=3)
    loser = make_service(path, handler=handler)
    first = await winner.prepare(SCOPE, 'job', 'revision')
    task = asyncio.create_task(winner.send(SCOPE, first.delivery_id))
    real_status = loser.ledger.status
    reads = []
    def stale_then_refresh(scope, delivery_id):
        reads.append(delivery_id)
        if len(reads) == 1:
            return replace(real_status(scope, delivery_id), state='pending')
        if fail_refresh:
            raise OSError('PRIVATE losing status read failed')
        return real_status(scope, delivery_id)
    monkeypatch.setattr(loser.ledger, 'status', stale_then_refresh)
    try:
        await asyncio.wait_for(entered.wait(), 2)
        with sqlite3.connect(path) as db:
            before = db.execute('SELECT * FROM cards').fetchone()
        if fail_refresh:
            with pytest.raises(CardDeliveryError) as caught:
                await loser.send(SCOPE, first.delivery_id)
            assert caught.value.__context__ is caught.value.__cause__ is None
            assert 'PRIVATE' not in str(caught.value)
        else:
            assert (await loser.send(SCOPE, first.delivery_id)).state == 'started'
        assert reads == [first.delivery_id, first.delivery_id]
        with sqlite3.connect(path) as db:
            assert db.execute('SELECT * FROM cards').fetchone() == before
        assert loser.active_operations == 0
    finally:
        finish.set()
        result = await task
    assert result.state == 'create_acknowledged' and len(calls) == 1
    reopened = make_service(path, handler=handler)
    assert (await reopened.send(SCOPE, first.delivery_id)).state == 'create_acknowledged'
    assert len(calls) == 1


@pytest.mark.parametrize('foreign', ['table', 'trigger', 'future', 'view'])
def test_reject_unknown_owned_schema_without_modifying(tmp_path, foreign):
    path = tmp_path/'cards.db'
    ledger = CardLedger(path)
    ledger.initialize()
    with sqlite3.connect(path) as db:
        if foreign == 'table':
            db.execute('CREATE TABLE private_notes(x TEXT)')
        elif foreign == 'trigger':
            db.execute('CREATE TRIGGER surprise AFTER UPDATE ON cards BEGIN SELECT 1; END')
        elif foreign == 'view':
            db.execute('CREATE VIEW surprise AS SELECT * FROM cards')
        else:
            db.execute('PRAGMA user_version=3')
    before = path.read_bytes()
    with pytest.raises(CardDeliveryError):
        ledger.initialize()
    assert path.read_bytes() == before


async def test_expired_begin_cannot_be_reopened_by_clock_rollback(tmp_path):
    path, now = tmp_path/'cards.db', [1000]
    c = make_service(path, now=now)
    first = await c.prepare(SCOPE, 'job', 'revision', ttl=1)
    now[0] = 1001
    with pytest.raises(CardDeliveryError):
        await c.send(SCOPE, first.delivery_id)
    now[0] = 1000
    reopened = make_service(path, now=now)
    with pytest.raises(CardDeliveryError):
        await reopened.send(SCOPE, first.delivery_id)


async def test_lost_begin_commit_response_has_no_owned_diagnostic(tmp_path, monkeypatch):
    path = tmp_path/'cards.db'
    c = make_service(path)
    first = await c.prepare(SCOPE, 'job', 'revision')
    begin = c.ledger.begin
    def lost(*args, **kwargs):
        begin(*args, **kwargs)
        raise OSError('PRIVATE COMMIT RESPONSE LOST')
    monkeypatch.setattr(c.ledger, 'begin', lost)
    with pytest.raises(CardDeliveryError):
        await c.send(SCOPE, first.delivery_id)
    with sqlite3.connect(path) as db:
        state, uncertainty, failure = db.execute(
            'SELECT state,uncertainty,first_failure FROM cards').fetchone()
    assert state == 'started' and 'send_unverified' in uncertainty and failure is None
    assert (await make_service(path).send(SCOPE, first.delivery_id)).state == 'started'


async def test_ack_commit_failure_cannot_separately_activate(tmp_path, monkeypatch):
    from contextlib import contextmanager
    from test_brief_card_navigation import invoke
    c = make_service(tmp_path/'cards.db')
    first = await c.prepare(SCOPE, 'job', 'revision')
    token = tokens(json.loads(c.ledger.frozen(SCOPE, first.delivery_id).card_json))['Refresh']
    original = c.ledger.transaction
    @contextmanager
    def reject_ack(*args, **kwargs):
        with original(*args, **kwargs) as item:
            yield item
            row = item[0].execute('SELECT state FROM cards').fetchone()
            if row[0] == 'create_acknowledged':
                raise OSError('ACK commit failed')
    monkeypatch.setattr(c.ledger, 'transaction', reject_ack)
    assert (await c.send(SCOPE, first.delivery_id)).state == 'unknown'
    with pytest.raises(CardDeliveryError):
        invoke(c, token)


@pytest.mark.parametrize('mode', ['timeout', 'cancel', 'stop'])
async def test_abandoned_hostile_transport_keeps_capacity_and_cannot_activate(tmp_path, mode):
    entered, release, exited = asyncio.Event(), asyncio.Event(), asyncio.Event()
    exchanges = []
    class Hostile:
        def check_ack(self, attempt, ack):
            pass

        async def send(self, attempt, *, before_egress):
            from ostag.briefs.card_delivery import CardCreateAck
            exchanges.append(asyncio.current_task())
            await before_egress()
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
            exited.set()
            return CardCreateAck(attempt.owner, attempt.token, attempt.intent.digest,
                'om_message_123', BINDING, 'oc_private', 'actual_sender', 'app_id')
    c = make_service(tmp_path/'cards.db', transport=Hostile(),
                     timeout=0.04 if mode == 'timeout' else 5)
    first = await c.prepare(SCOPE, 'job', 'revision')
    task = asyncio.create_task(c.send(SCOPE, first.delivery_id))
    try:
        await _wait_for_card_entry(task, entered)
        if mode == 'cancel':
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            if mode == 'stop':
                c.stop()
            assert (await task).state == 'unknown'
        assert c.active_operations == 1
        with pytest.raises(CardDeliveryError):
            await c.send(SCOPE, first.delivery_id)
    finally:
        await _release_card(c, task, release, exchanges)
    assert exited.is_set()
    assert c.active_operations == 0
    assert c.ledger.status(SCOPE, first.delivery_id).state == 'unknown'


@pytest.mark.parametrize('case', ['claimants', 'hostile'])
async def test_card_entry_failure_terminates_wrapper_and_retrieves_task(tmp_path, monkeypatch, case):
    import gc
    import sys
    module, original = sys.modules[__name__], make_service
    services, errors = [], []
    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()
    before = asyncio.all_tasks()

    def stopped(*args, **kwargs):
        service = original(*args, **kwargs)
        services.append(service)
        send = service.send
        async def fail_before_entry(*args, **kwargs):
            service.stop()
            return await send(*args, **kwargs)
        service.send = fail_before_entry
        return service

    monkeypatch.setattr(module, 'make_service', stopped)
    loop.set_exception_handler(lambda loop, context: errors.append(context))
    task = asyncio.create_task(test_two_claimants_loser_cannot_poison_winner(tmp_path)
        if case == 'claimants' else
        test_abandoned_hostile_transport_keeps_capacity_and_cannot_activate(tmp_path, 'stop'))
    try:
        done, _ = await asyncio.wait({task}, timeout=3)
        assert done, 'wrapper kept waiting after the service failed'
        with pytest.raises(CardDeliveryError):
            task.result()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        for service in services:
            service.stop()
        gc.collect()
        await asyncio.sleep(0)
        loop.set_exception_handler(previous)
    assert not errors
    assert not (asyncio.all_tasks() - before)
    assert all(service.active_operations == 0 for service in services)


async def test_cross_instance_cancel_during_response_blocks_activation(tmp_path):
    from test_brief_card_navigation import invoke
    path = tmp_path/'cards.db'
    other = make_service(path)
    delivery = [None]
    async def handler(r):
        await asyncio.sleep(0)
        other.cancel(SCOPE, delivery[0])
        return httpx.Response(200, json=ack())
    c = make_service(path, handler=handler)
    first = await c.prepare(SCOPE, 'job', 'revision')
    delivery[0] = first.delivery_id
    token = tokens(json.loads(c.ledger.frozen(SCOPE, first.delivery_id).card_json))['Refresh']
    result = await c.send(SCOPE, first.delivery_id)
    assert result.cancelled and result.state == 'unknown'
    with pytest.raises(CardDeliveryError):
        invoke(c, token)


def _process_delivery(path, queue, start, claims, loser_finished, deadline, index,
                      fail_initialization=False):
    async def run():
        def handler(r):
            queue.put((index, 'sent'), timeout=3)
            # Keep the winning send in flight until the competing claim returns.
            assert loser_finished.wait(3)
            return httpx.Response(200, json=ack())
        if fail_initialization:
            sleep(2.5)  # Startup cost exceeds the separate 2 s failure-detection budget.
        # The fixture deliberately blocks for the 3 s claim/loser handshakes.
        # This operation budget does not change CardLedger's 50 ms busy bound.
        c = make_service(path, handler=handler, timeout=10)
        begin = c.ledger.begin
        first_claim = True

        def competing_claim(*args, **kwargs):
            nonlocal first_claim
            if first_claim:
                first_claim = False
                claims.wait(timeout=3)
            return begin(*args, **kwargs)

        c.ledger.begin = competing_claim
        try:
            queue.put((index, 'initialized'), timeout=3)
            if fail_initialization:
                raise RuntimeError('synthetic child initialization failure')
            # Both children use the parent's deadline, including serial startup.
            assert start.wait(max(0, deadline - monotonic())), 'parent startup deadline'
            result = await c.deliver(SCOPE, 'job', 'revision')
            if result.state == 'started':
                loser_finished.set()
            queue.put((index, result.state), timeout=3)
        finally:
            c.stop()
    try:
        asyncio.run(run())
    except BaseException:
        queue.put((index, 'failed'), timeout=3)
        raise
    finally:
        queue.close()
        queue.join_thread()


def _process_message(queue, processes, deadline, *, starting=False):
    """Poll lifecycle, not delivery: a dead child cannot produce a later result."""
    while True:
        remaining = deadline - monotonic()
        assert remaining > 0, 'child coordination deadline expired'
        try:
            index, message = queue.get(timeout=min(0.05, remaining))
        except Empty:
            started = [p for p in processes if p.pid is not None]
            failed = [p for p in started if p.exitcode is not None
                      and (starting or p.exitcode != 0)]
            assert not failed, f'child exited before result: {[p.exitcode for p in failed]}'
            assert not all(p.exitcode is not None for p in started), 'children exited without result'
        else:
            assert message != 'failed', f'child {index} failed'
            return index, message


@contextmanager
def _delivery_processes(path, *, startup_budget=15, fail_child=None):
    import multiprocessing
    context = multiprocessing.get_context('spawn')
    queue, start = context.Queue(maxsize=8), context.Event()
    claims, loser_finished = context.Barrier(2), context.Event()
    deadline = monotonic() + startup_budget
    processes = [context.Process(target=_process_delivery,
                                 args=(path, queue, start, claims, loser_finished,
                                       deadline, index, index == fail_child))
                 for index in range(2)]
    try:
        yield processes, queue, start, deadline
    finally:
        # No release of the start gate on failure: cleanup must not initiate sends.
        try:
            for process in processes:
                if process.pid is None:
                    continue
                if process.is_alive():
                    process.terminate()
                process.join(1)
                if process.is_alive():
                    process.kill()
                    process.join(3)
            assert all(p.pid is None or not p.is_alive() for p in processes), 'child not reaped'
        finally:
            try:
                for process in processes:
                    if process.pid is None or not process.is_alive():
                        process.close()
            finally:
                queue.close()
                queue.join_thread()


@pytest.mark.parametrize('second_delay', [0, 0.2], ids=['normal', 'delayed_second_child'])
def test_two_processes_prepare_send_exactly_one(tmp_path, second_delay):
    path = tmp_path/'cards.db'
    with _delivery_processes(path) as (processes, queue, start, deadline):
        for index, process in enumerate(processes):
            if index == 1 and second_delay:
                # A controlled startup delay, while child 1 is already waiting.
                sleep(second_delay)
                assert processes[0].is_alive() and not start.is_set()
            process.start()
            # Initialization has a real 50 ms busy bound; it is not the race
            # under test. Each child initializes before either can prepare/send.
            assert _process_message(queue, processes, deadline,
                                    starting=True) == (index, 'initialized')
        start.set()
        result_deadline = monotonic() + 12
        results = [_process_message(queue, processes, result_deadline) for _ in range(3)]
        assert sorted(message for _, message in results) == [
            'create_acknowledged', 'sent', 'started']
        sender = next(index for index, message in results if message == 'sent')
        assert (sender, 'create_acknowledged') in results
        assert (1 - sender, 'started') in results
        for process in processes:
            process.join(5)
            assert process.exitcode == 0
        with sqlite3.connect(path) as db:
            assert db.execute('SELECT count(*) FROM cards').fetchone()[0] == 1
            assert db.execute('SELECT state FROM cards').fetchone()[0] == 'create_acknowledged'


@pytest.mark.parametrize('failure', ['parent_setup', 'child_initialization',
                                    'child_exit', 'expired_deadline'])
def test_process_startup_failure_reaps_children_and_closes_queue(tmp_path, failure):
    import multiprocessing
    previous_children = {child.pid for child in multiprocessing.active_children()}
    with _delivery_processes(
            tmp_path/'cards.db',
            fail_child=1 if failure == 'child_initialization' else None) as resources:
        processes, queue, start, deadline = resources
        processes[0].start()
        assert _process_message(queue, processes, deadline, starting=True) == (0, 'initialized')
        if failure == 'child_initialization':
            processes[1].start()
            # Spawn/import and the injected 2.5 s startup cost use the same
            # generous deadline as normal serial initialization, not detection time.
            assert _process_message(queue, processes, deadline, starting=True) == (1, 'initialized')
        reason = {
            'parent_setup': 'synthetic setup failure',
            'child_initialization': 'child 1 failed|child.*exited',
            'child_exit': 'child.*exited',
            'expired_deadline': 'child coordination deadline expired',
        }[failure]
        failed_at = monotonic()
        with pytest.raises((RuntimeError, AssertionError), match=reason):
            if failure == 'parent_setup':
                raise RuntimeError('synthetic setup failure')
            if failure == 'child_exit':
                processes[0].terminate()  # No failure message: observe the child exit.
            # An expired shared deadline must fail now, not start a new 5 s wait.
            detection_deadline = failed_at - 1 if failure == 'expired_deadline' else failed_at + 2
            _process_message(queue, processes, detection_deadline, starting=True)
        assert monotonic() - failed_at < 3
        cleanup_at = monotonic()
    # Cleanup permits join(1) + kill/join(3) per child, independently of detection.
    assert monotonic() - cleanup_at < 10
    assert not start.is_set()
    assert {child.pid for child in multiprocessing.active_children()} <= previous_children
    for process in processes:
        with pytest.raises(ValueError, match='closed'):
            process.is_alive()
    with pytest.raises(ValueError, match='closed'):
        queue.get_nowait()


def test_initialize_busy_is_bounded_and_does_not_retry(tmp_path, monkeypatch):
    from contextlib import closing, contextmanager
    from time import monotonic

    ledger = CardLedger(tmp_path / 'cards.db')
    ledger.initialize()
    connection, statements = ledger._connection, []

    @contextmanager
    def traced(**kwargs):
        with connection(**kwargs) as db:
            assert db.execute('PRAGMA busy_timeout').fetchone()[0] == 50
            db.set_trace_callback(statements.append)
            yield db

    monkeypatch.setattr(ledger, '_connection', traced)
    with closing(sqlite3.connect(ledger.path)) as holder:
        holder.execute('BEGIN IMMEDIATE')
        started = monotonic()
        with pytest.raises(CardDeliveryError, match='^brief card unavailable$') as caught:
            ledger.initialize()
        elapsed = monotonic() - started
        assert .04 <= elapsed < 1
        assert statements.count('BEGIN IMMEDIATE') == 1
        assert caught.value.__context__ is caught.value.__cause__ is None
        assert holder.in_transaction  # Failure happened while the real lock remained held.
        holder.rollback()


async def test_scalar_bounds_and_no_uninitialized_database_creation(tmp_path):
    path = tmp_path/'missing.db'
    ledger = CardLedger(path)
    with pytest.raises(CardDeliveryError):
        ledger.frozen(SCOPE, 'delivery')
    assert not path.exists()
    for capacity in (True, False, 0, 10001, 1.0):
        with pytest.raises(CardDeliveryError):
            CardLedger(path, capacity=capacity)
    c = make_service(tmp_path/'cards.db')
    for ttl in (True, False, float('nan'), float('inf'), 301, 0, 10**50):
        with pytest.raises(CardDeliveryError):
            await c.prepare(SCOPE, 'job', 'revision', ttl=ttl)
    for value in (True, None, '/', 'bad.id', 'x'*257):
        with pytest.raises(CardDeliveryError):
            await c.prepare(SCOPE, value, 'revision')


async def test_async_lock_wait_reauthorizes_and_does_not_spend_pending(tmp_path):
    denied, calls = [False], []
    def authorize(p):
        if denied[0]:
            raise PermissionError('PRIVATE grant')
    def handler(r):
        calls.append(r)
        return httpx.Response(200, json=ack())
    c = make_service(tmp_path/'cards.db', authorize=authorize, handler=handler)
    first = await c.prepare(SCOPE, 'job', 'revision')
    with sqlite3.connect(c.ledger.path) as db:
        db.execute('BEGIN IMMEDIATE')
        task = asyncio.create_task(c.send(SCOPE, first.delivery_id))
        await asyncio.sleep(0.02)
        denied[0] = True
        db.rollback()
        with pytest.raises(CardDeliveryError):
            await task
    assert calls == [] and c.ledger.status(SCOPE, first.delivery_id).state == 'pending'
    denied[0] = False
    assert (await c.send(SCOPE, first.delivery_id)).state == 'create_acknowledged'


async def test_permanent_capacity_status_scope_and_strict_integer_columns(tmp_path):
    from ostag.briefs.store import RequesterContext
    from ostag.core.identity import Principal
    current = [replace(preview(), outbox_id='original_outbox')]
    c = make_service(tmp_path/'cards.db', capacity=1, current=current)
    first = await c.deliver(SCOPE, 'job', 'revision')
    with pytest.raises(CardDeliveryError):
        c.ledger.status(RequesterContext(SCOPE.namespace, Principal('feishu', 'bob')), first.delivery_id)
    current[0] = replace(current[0], revision_id='revision2', outbox_id='second_outbox')
    with pytest.raises(CardDeliveryError):
        await c.prepare(SCOPE, 'job', 'revision2')
    with sqlite3.connect(c.ledger.path) as db:
        for column in ('epoch', 'page', 'issued', 'expires', 'cancelled'):
            with pytest.raises(sqlite3.IntegrityError):
                db.execute(f'UPDATE cards SET {column}=?', (1.5,))
        assert db.execute('PRAGMA journal_mode').fetchone()[0] == 'delete'
        assert db.execute('PRAGMA user_version').fetchone()[0] == 2
    frozen = c.ledger.frozen(SCOPE, first.delivery_id)
    for field in ('issued', 'expires'):
        with pytest.raises(CardDeliveryError):
            replace(frozen, **{field: True})


async def test_first_failure_stays_fixed_on_retries_and_cancel(tmp_path):
    calls = []
    def handler(r):
        calls.append(r)
        raise RuntimeError('SECRET first failure')
    c = make_service(tmp_path/'cards.db', handler=handler)
    first = await c.deliver(SCOPE, 'job', 'revision')
    with sqlite3.connect(c.ledger.path) as db:
        before = db.execute('SELECT uncertainty,first_failure FROM cards').fetchone()
    c.cancel(SCOPE, first.delivery_id)
    assert (await c.send(SCOPE, first.delivery_id)).state == 'unknown'
    with sqlite3.connect(c.ledger.path) as db:
        assert db.execute('SELECT uncertainty,first_failure FROM cards').fetchone() == before
    assert len(calls) == 1


async def test_service_stop_default_adapter_and_nonreentrant_loader(tmp_path):
    from ostag.adapters.feishu import FeishuAdapter
    from ostag.config import Settings
    with pytest.raises(CardDeliveryError):
        await FeishuAdapter(Settings()).deliver_brief_card(SCOPE, 'job', 'revision')
    c = make_service(tmp_path/'cards.db')
    original = c._load
    c._load = lambda s, j: c._loaded(s, j, 'revision')
    with pytest.raises(CardDeliveryError):
        await c.prepare(SCOPE, 'job', 'revision')
    c._load = original
    assert (await c.prepare(SCOPE, 'job', 'revision')).state == 'pending'
    c.stop()
    with pytest.raises(CardDeliveryError):
        await c.prepare(SCOPE, 'job', 'revision')


async def test_expiry_during_begin_final_check_cannot_reopen_after_restart(tmp_path):
    now, advance = [1000], [False]
    def authorize(p):
        if advance[0]:
            now[0] = 1002
    path = tmp_path/'cards.db'
    c = make_service(path, now=now, authorize=authorize)
    first = await c.prepare(SCOPE, 'job', 'revision', ttl=1)
    advance[0] = True
    with pytest.raises(CardDeliveryError):
        await c.send(SCOPE, first.delivery_id)
    now[0], advance[0] = 1000, False
    reopened = make_service(path, now=now)
    # A final-check expiration may abandon an inert claim; either historical
    # started or denial is safe, but it can never reach the HTTP transport again.
    calls = []
    reopened.transport._transport = httpx.MockTransport(lambda r: calls.append(r))
    try:
        await reopened.send(SCOPE, first.delivery_id)
    except CardDeliveryError:
        pass
    assert calls == []
