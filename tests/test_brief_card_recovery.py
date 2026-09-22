"""Explicit trusted recovery never renews send or lifetime source authority."""
import asyncio
from contextlib import closing, contextmanager
from dataclasses import replace
import json
import sqlite3
import stat
import time
from types import SimpleNamespace

import pytest

import ostag.briefs.card_delivery as module
from ostag.briefs.card_delivery import CardDeliveryError, CardLedger, MAX_EPOCH
from ostag.briefs.store import PrivateDestination, RequesterContext
from ostag.core.identity import Principal
from ostag.work_monitor.identity import Destination
from test_brief_card_delivery import BINDING, SCOPE, make_service, preview, tokens
from test_brief_card_navigation import invoke


async def recover(service, delivery_id, **changes):
    args = dict(expected_epoch=0, message_id='om_message_123',
                destination=Destination('feishu', 'oc_private', 'private'),
                tenant_id='tenant', app_id='app', page=0, ttl=300)
    return await service.recover_preview(SCOPE, delivery_id, **(args | changes))


async def test_expired_ack_recovery_preserves_send_and_navigates(tmp_path):
    now = [1000]
    service = make_service(tmp_path/'cards.db', now=now)
    first = await service.deliver(SCOPE, 'job', 'revision', ttl=1)
    frozen = service.ledger.frozen(SCOPE, first.delivery_id)
    with sqlite3.connect(service.ledger.path) as db:
        historical = db.execute('SELECT * FROM cards').fetchone()[:18]
    old = tokens(json.loads(frozen.card_json))['Refresh']
    now[0] = 1001
    with pytest.raises(CardDeliveryError):
        invoke(service, old)

    card = await recover(service, first.delivery_id)
    assert 'Private draft' in json.dumps(card)
    assert service.ledger.status(SCOPE, first.delivery_id).epoch == 1
    assert 'Private draft' in json.dumps(invoke(service, tokens(card)['Refresh']))
    assert service.ledger.frozen(SCOPE, first.delivery_id) == frozen
    assert (await service.prepare(SCOPE, 'job', 'revision')).delivery_id == first.delivery_id
    assert (await service.deliver(SCOPE, 'job', 'revision')).state == 'create_acknowledged'
    assert (await service.send(SCOPE, first.delivery_id)).state == 'create_acknowledged'
    with sqlite3.connect(service.ledger.path) as db:
        assert db.execute('SELECT * FROM cards').fetchone()[:18] == historical
    with pytest.raises(CardDeliveryError):
        invoke(service, old)


async def test_lost_recovery_and_callback_responses_need_new_cas_after_restart(tmp_path):
    path, now = tmp_path/'cards.db', [1000]
    service = make_service(path, now=now)
    first = await service.deliver(SCOPE, 'job', 'revision')
    old = tokens(json.loads(service.ledger.frozen(SCOPE, first.delivery_id).card_json))['Refresh']
    lost = invoke(service, old)
    recovered = await recover(service, first.delivery_id, expected_epoch=1)
    reopened = make_service(path, now=now)
    with pytest.raises(CardDeliveryError):
        await recover(reopened, first.delivery_id, expected_epoch=1)
    assert reopened.ledger.status(SCOPE, first.delivery_id).epoch == 2
    replacement = await recover(reopened, first.delivery_id, expected_epoch=2)
    for token in (old, tokens(lost)['Refresh'], tokens(recovered)['Refresh']):
        with pytest.raises(CardDeliveryError):
            invoke(reopened, token)
    assert 'Private draft' in json.dumps(invoke(reopened, tokens(replacement)['Refresh']))


async def test_competing_same_epoch_has_one_response(tmp_path):
    path = tmp_path/'cards.db'
    services = [make_service(path), make_service(path)]
    first = await services[0].deliver(SCOPE, 'job', 'revision')
    results = await asyncio.gather(*(recover(s, first.delivery_id) for s in services),
                                   return_exceptions=True)
    assert sum(type(r) is dict for r in results) == 1
    assert sum(type(r) is CardDeliveryError for r in results) == 1
    assert services[0].ledger.status(SCOPE, first.delivery_id).epoch == 1


@pytest.mark.parametrize('changes', [
    {'expected_epoch': True}, {'expected_epoch': False}, {'expected_epoch': 0.0},
    {'expected_epoch': -1}, {'expected_epoch': MAX_EPOCH}, {'expected_epoch': 10**80},
    {'page': True}, {'page': False}, {'page': 0.0}, {'page': -1}, {'page': 65536},
    {'ttl': True}, {'ttl': False}, {'ttl': 0}, {'ttl': -1}, {'ttl': 301},
    {'ttl': float('inf')}, {'ttl': float('nan')}, {'ttl': '10'}, {'ttl': 10**80},
    {'message_id': 'om_other_123'}, {'message_id': True},
    {'tenant_id': 'other'}, {'app_id': 'other'},
    {'destination': Destination('feishu', 'oc_other', 'private')},
    {'destination': Destination('feishu', 'oc_private', 'group')},
    {'destination': 'oc_private'},
])
async def test_bad_arguments_never_spend_rightful_epoch(tmp_path, changes):
    service = make_service(tmp_path/'cards.db')
    first = await service.deliver(SCOPE, 'job', 'revision')
    with pytest.raises(CardDeliveryError) as caught:
        await recover(service, first.delivery_id, **changes)
    assert caught.value.__context__ is caught.value.__cause__ is None
    assert service.ledger.status(SCOPE, first.delivery_id).epoch == 0


@pytest.mark.parametrize('change', ['user', 'namespace', 'binding', 'generation', 'body',
                                 'revision', 'job', 'outbox', 'source', 'mapping'])
async def test_wrong_current_identity_never_recovers(tmp_path, change):
    original = replace(preview(), outbox_id='original_outbox')
    current = [original]
    binding = [BINDING]
    service = make_service(tmp_path/'cards.db', current=current, binding=lambda: binding[0])
    first = await service.deliver(SCOPE, 'job', 'revision')
    scope = SCOPE
    if change == 'user':
        scope = RequesterContext(SCOPE.namespace, Principal('feishu', 'bob'))
    elif change == 'namespace':
        scope = RequesterContext('other', SCOPE.principal)
    elif change == 'binding':
        binding[0] = replace(BINDING, app_id='other')
    elif change == 'generation':
        binding[0] = replace(BINDING, generation=2)
    elif change == 'body':
        current[0] = replace(preview(text='PRIVATE substituted'), outbox_id='original_outbox')
    elif change in ('job', 'revision', 'outbox'):
        current[0] = replace(original, **{change + '_id': 'other'})
    elif change == 'source':
        current[0] = replace(original, sources=replace(original.sources, sources=tuple(
            replace(s, excerpt='PRIVATE source replacement') for s in original.sources.sources)))
    elif change == 'mapping':
        current[0] = replace(original, destination=PrivateDestination(SCOPE.principal, 'oc_other'))
    with pytest.raises(CardDeliveryError) as caught:
        await service.recover_preview(scope, first.delivery_id, expected_epoch=0,
            message_id='om_message_123', destination=Destination('feishu', 'oc_private', 'private'),
            tenant_id='tenant', app_id='app')
    assert caught.value.__context__ is caught.value.__cause__ is None
    assert service.ledger.status(SCOPE, first.delivery_id).epoch == 0


@pytest.mark.parametrize('state', ['pending', 'started', 'unknown', 'cancelled', 'retired'])
async def test_only_live_ack_eligible(tmp_path, state):
    service = make_service(tmp_path/'cards.db')
    first = await service.prepare(SCOPE, 'job', 'revision')
    if state != 'pending':
        await service.send(SCOPE, first.delivery_id)
    if state in ('started', 'unknown'):
        with sqlite3.connect(service.ledger.path) as db:
            db.execute('UPDATE cards SET state=?', (state,))
    elif state == 'cancelled':
        service.cancel(SCOPE, first.delivery_id)
    elif state == 'retired':
        service.ledger.retire_local(SCOPE, 'job')
    with pytest.raises(CardDeliveryError):
        await recover(service, first.delivery_id)
    assert service.ledger.status(SCOPE, first.delivery_id).epoch == 0


async def test_window_expiry_equality_and_rollback_survive_restart(tmp_path):
    path, now = tmp_path/'cards.db', [1000]
    service = make_service(path, now=now)
    first = await service.deliver(SCOPE, 'job', 'revision', ttl=1)
    now[0] = 1001
    card = await recover(service, first.delivery_id, ttl=2)
    token = tokens(card)['Refresh']
    now[0] = 1003
    with pytest.raises(CardDeliveryError):
        invoke(service, token)
    now[0] = 1002
    reopened = make_service(path, now=now)
    with pytest.raises(CardDeliveryError):
        invoke(reopened, token)
    with pytest.raises(CardDeliveryError):
        await recover(reopened, first.delivery_id, expected_epoch=1)
    now[0] = 1003
    fresh = await recover(reopened, first.delivery_id, expected_epoch=1)
    assert 'Private draft' in json.dumps(invoke(reopened, tokens(fresh)['Refresh']))


async def test_recovery_navigation_keeps_window_and_max_epoch(tmp_path):
    now = [1000]
    service = make_service(tmp_path/'cards.db', now=now)
    first = await service.deliver(SCOPE, 'job', 'revision')
    with sqlite3.connect(service.ledger.path) as db:
        db.execute('UPDATE cards SET epoch=?', (MAX_EPOCH - 2,))
    card = await recover(service, first.delivery_id, expected_epoch=MAX_EPOCH - 2, ttl=2)
    now[0] = 1001
    last = invoke(service, tokens(card)['Refresh'])
    assert service.ledger.status(SCOPE, first.delivery_id).epoch == MAX_EPOCH
    with pytest.raises(CardDeliveryError):
        await recover(service, first.delivery_id, expected_epoch=MAX_EPOCH)
    with pytest.raises(CardDeliveryError):
        invoke(service, tokens(last)['Refresh'])
    with sqlite3.connect(service.ledger.path) as db:
        assert db.execute('SELECT issued,expires FROM card_windows').fetchone() == (
            1000000000, 1002000000)


@pytest.mark.parametrize('revoke', ['grant', 'binding', 'body', 'cancel', 'stop', 'deadline',
                                  'expiry', 'error'])
async def test_render_unlocked_and_late_failure_permanently_spends(tmp_path, monkeypatch, revoke):
    path, now, denied = tmp_path/'cards.db', [1000], [False]
    current = [replace(preview(), outbox_id='original_outbox')]
    binding = [BINDING]
    def authorize(p):
        if denied[0]:
            raise PermissionError('PRIVATE grant')
    service = make_service(path, now=now, current=current, authorize=authorize,
                           binding=lambda: binding[0])
    first = await service.deliver(SCOPE, 'job', 'revision')
    old = tokens(json.loads(service.ledger.frozen(SCOPE, first.delivery_id).card_json))['Refresh']
    original = module._controls
    def render(*args):
        # A second real SQLite writer can acquire while rendering is in progress.
        with sqlite3.connect(path, timeout=0) as db:
            db.execute('BEGIN IMMEDIATE')
            assert db.execute('SELECT epoch FROM cards').fetchone()[0] == 1
        card = original(*args)
        if revoke == 'grant':
            denied[0] = True
        elif revoke == 'binding':
            binding[0] = replace(BINDING, generation=2)
        elif revoke == 'body':
            current[0] = replace(preview(text='PRIVATE replacement'), outbox_id='original_outbox')
        elif revoke == 'cancel':
            service.cancel(SCOPE, first.delivery_id)
        elif revoke == 'stop':
            service.stop()
        elif revoke == 'deadline':
            time.sleep(0.03)
        elif revoke == 'expiry':
            now[0] += 1
        else:
            raise RuntimeError('PRIVATE render error')
        return card
    monkeypatch.setattr(module, '_controls', render)
    if revoke == 'deadline':
        service.timeout = 0.02
    with pytest.raises(CardDeliveryError) as caught:
        await recover(service, first.delivery_id, ttl=1)
    assert caught.value.__context__ is caught.value.__cause__ is None
    assert 'PRIVATE' not in str(caught.value)
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT epoch,response_card FROM cards').fetchone() == (1, None)
        assert db.execute('SELECT count(*) FROM card_tokens').fetchone()[0] == 0
    reopened = make_service(path, now=now)
    with pytest.raises(CardDeliveryError):
        await recover(reopened, first.delivery_id)
    with pytest.raises(CardDeliveryError):
        invoke(reopened, old)


@pytest.mark.parametrize('phase', ['reserve', 'publish', 'release'])
async def test_database_wait_rechecks_authority(tmp_path, monkeypatch, phase):
    denied, attempting = [False], asyncio.Event()
    def authorize(p):
        if denied[0]:
            raise PermissionError('PRIVATE authority')
    service = make_service(tmp_path/'cards.db', authorize=authorize)
    first = await service.deliver(SCOPE, 'job', 'revision')
    holder = sqlite3.connect(service.ledger.path)
    original = service.ledger.recovery_step
    locked = [False]
    def step(*args, **kwargs):
        this_phase = ('reserve' if kwargs.get('ttl') is not None else
                      'publish' if kwargs.get('rendered') is not None else 'release')
        if this_phase == phase:
            if not locked[0]:
                holder.execute('BEGIN IMMEDIATE')
                locked[0] = True
            attempting.set()
        return original(*args, **kwargs)
    monkeypatch.setattr(service.ledger, 'recovery_step', step)
    task = asyncio.create_task(recover(service, first.delivery_id))
    try:
        await asyncio.wait_for(attempting.wait(), 1)
        assert not task.done()
        denied[0] = True
        holder.rollback()
        with pytest.raises(CardDeliveryError):
            await task
    finally:
        holder.close()
    assert service.active_operations == 0
    assert service.ledger.status(SCOPE, first.delivery_id).epoch == (0 if phase == 'reserve' else 1)
    if phase != 'reserve':
        with sqlite3.connect(service.ledger.path) as db:
            assert db.execute('SELECT count(*) FROM card_tokens').fetchone()[0] == 0


@pytest.mark.parametrize('after_commit', [2, 3])
async def test_commit_wait_and_final_release_denial_cannot_restore_epoch(
        tmp_path, monkeypatch, after_commit):
    denied = [False]
    def authorize(p):
        if denied[0]:
            raise PermissionError('PRIVATE late denial')
    path = tmp_path/'cards.db'
    service = make_service(path, authorize=authorize)
    first = await service.deliver(SCOPE, 'job', 'revision')
    original, commits = service.ledger.transaction, []
    @contextmanager
    def transaction(*args, **kwargs):
        with original(*args, **kwargs) as item:
            yield item
        commits.append(1)
        if len(commits) == after_commit:
            denied[0] = True
    monkeypatch.setattr(service.ledger, 'transaction', transaction)
    with pytest.raises(CardDeliveryError):
        await recover(service, first.delivery_id)
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT epoch,response_card FROM cards').fetchone() == (1, None)
        assert db.execute('SELECT count(*) FROM card_tokens').fetchone()[0] == 0
    reopened = make_service(path)
    with pytest.raises(CardDeliveryError):
        await recover(reopened, first.delivery_id)


@pytest.mark.parametrize('failure', ['stop', 'cancel', 'deadline'])
@pytest.mark.parametrize('observer_delay', [0, 0.2])
async def test_capacity_and_abandonment_during_lock_wait(
        tmp_path, monkeypatch, failure, observer_delay):
    service = make_service(tmp_path/'cards.db')
    first = await service.deliver(SCOPE, 'job', 'revision')
    # Control only this module's operation deadline, not asyncio's watchdog clock.
    # A slow observer must not turn stop/cancel coverage into a timeout test.
    monotonic = [100.0]
    monkeypatch.setattr(module, 'time', SimpleNamespace(**(vars(time) | {'monotonic': lambda: monotonic[0]})))
    service.timeout = 0.1
    waiting = asyncio.Event()
    original = service.ledger.recovery_step

    def observed_busy(*args, **kwargs):
        try:
            return original(*args, **kwargs)
        except module._Busy:
            waiting.set()
            raise

    monkeypatch.setattr(service.ledger, 'recovery_step', observed_busy)
    with closing(sqlite3.connect(service.ledger.path)) as holder:
        holder.execute('BEGIN IMMEDIATE')
        task = asyncio.create_task(recover(service, first.delivery_id))
        try:
            await asyncio.wait_for(waiting.wait(), 2)
            await asyncio.sleep(observer_delay)
            assert not task.done()
            assert service.active_operations == 1
            with pytest.raises(CardDeliveryError):
                await recover(service, first.delivery_id)
            if failure == 'stop':
                service.stop()
            elif failure == 'cancel':
                task.cancel()
            else:
                monotonic[0] += 0.2
            expected = asyncio.CancelledError if failure == 'cancel' else CardDeliveryError
            with pytest.raises(expected):
                await asyncio.wait_for(task, 2)
        finally:
            if not task.done():
                task.cancel()
            try:
                await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 2)
            finally:
                holder.rollback()
    assert service.active_operations == 0
    assert service.ledger.status(SCOPE, first.delivery_id).epoch == 0


async def test_recovery_lock_wait_expires_with_real_monotonic_clock(tmp_path, monkeypatch):
    service = make_service(tmp_path/'cards.db')
    first = await service.deliver(SCOPE, 'job', 'revision')
    service.timeout = 0.2
    busy_hits = []
    original = service.ledger.recovery_step

    def observed_busy(*args, **kwargs):
        try:
            return original(*args, **kwargs)
        except module._Busy:
            busy_hits.append(1)
            raise

    monkeypatch.setattr(service.ledger, 'recovery_step', observed_busy)
    with closing(sqlite3.connect(service.ledger.path)) as holder:
        holder.execute('BEGIN IMMEDIATE')
        started = time.monotonic()
        task = asyncio.create_task(recover(service, first.delivery_id))
        try:
            with pytest.raises(CardDeliveryError):
                await asyncio.wait_for(task, 5)
            assert busy_hits
            assert time.monotonic() - started >= service.timeout
        finally:
            if not task.done():
                task.cancel()
            try:
                await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 2)
            finally:
                holder.rollback()
    assert service.active_operations == 0
    assert service.ledger.status(SCOPE, first.delivery_id).epoch == 0


async def test_explicit_v1_migration_preserves_all_historical_rows(tmp_path):
    path = tmp_path/'cards.db'
    service = make_service(path)
    first = await service.deliver(SCOPE, 'job', 'revision')
    frozen = service.ledger.frozen(SCOPE, first.delivery_id)
    with sqlite3.connect(path) as db:
        db.execute('DROP TABLE card_windows')
        db.execute('PRAGMA user_version=1')
        before_rows = {name: db.execute(f'SELECT * FROM {name}').fetchall()
                       for name in ('card_meta', 'cards', 'card_tokens')}
        before_schema = db.execute("SELECT sql FROM sqlite_master WHERE name='cards'").fetchone()
    before_bytes = path.read_bytes()
    with pytest.raises(CardDeliveryError):
        service.ledger.initialize()
    assert path.read_bytes() == before_bytes
    service.ledger.migrate_v1()
    service.ledger.initialize()
    with sqlite3.connect(path) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 2
        assert db.execute('SELECT count(*) FROM card_windows').fetchone()[0] == 0
        assert db.execute("SELECT sql FROM sqlite_master WHERE name='cards'").fetchone() == before_schema
        for name, rows in before_rows.items():
            assert db.execute(f'SELECT * FROM {name}').fetchall() == rows
    assert service.ledger.frozen(SCOPE, first.delivery_id) == frozen
    old = tokens(json.loads(frozen.card_json))['Refresh']
    assert 'Private draft' in json.dumps(invoke(service, old))
    service._clock = lambda: 1300
    card = await recover(service, first.delivery_id, expected_epoch=1)
    assert 'Private draft' in json.dumps(invoke(service, tokens(card)['Refresh']))


@pytest.mark.parametrize('foreign', ['table', 'view', 'trigger', 'future', 'unowned', 'ddl'])
def test_migration_refuses_unknown_without_mutation(tmp_path, foreign):
    path = tmp_path/'cards.db'
    ledger = CardLedger(path)
    ledger.initialize()
    with sqlite3.connect(path) as db:
        db.execute('DROP TABLE card_windows')
        db.execute('PRAGMA user_version=1')
        if foreign == 'table':
            db.execute('CREATE TABLE alien(x)')
        elif foreign == 'view':
            db.execute('CREATE VIEW alien AS SELECT * FROM cards')
        elif foreign == 'trigger':
            db.execute('CREATE TRIGGER alien AFTER UPDATE ON cards BEGIN SELECT 1; END')
        elif foreign == 'future':
            db.execute('PRAGMA user_version=3')
        elif foreign == 'unowned':
            db.execute('PRAGMA application_id=0')
        else:
            db.execute('ALTER TABLE card_tokens ADD COLUMN extra TEXT')
    before = path.read_bytes()
    with pytest.raises(CardDeliveryError):
        ledger.migrate_v1()
    assert path.read_bytes() == before


def test_migration_failure_is_atomic_and_v2_is_not_downgraded(tmp_path, monkeypatch):
    path = tmp_path/'cards.db'
    ledger = CardLedger(path)
    ledger.initialize()
    before = path.read_bytes()
    with pytest.raises(CardDeliveryError):
        ledger.migrate_v1()
    assert path.read_bytes() == before
    with sqlite3.connect(path) as db:
        db.execute('DROP TABLE card_windows')
        db.execute('PRAGMA user_version=1')
    before = path.read_bytes()
    original = ledger._verify
    def verify(db, *, version=2):
        if version == 2:
            raise RuntimeError('PRIVATE migration failure')
        return original(db, version=version)
    monkeypatch.setattr(ledger, '_verify', verify)
    with pytest.raises(CardDeliveryError) as caught:
        ledger.migrate_v1()
    assert caught.value.__context__ is caught.value.__cause__ is None
    assert path.read_bytes() == before


async def test_new_private_database_and_journal_owner_only_existing_not_broadened(tmp_path):
    path = tmp_path/'cards.db'
    service = make_service(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    await service.deliver(SCOPE, 'job', 'revision')
    with service.ledger.transaction(lambda: 1000) as (db, now):
        db.execute('UPDATE cards SET epoch=1')
        journal = path.with_name(path.name + '-journal')
        assert journal.exists()
        assert stat.S_IMODE(journal.stat().st_mode) & 0o077 == 0
    path.chmod(0o640)
    service.ledger.initialize()
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    path.chmod(0o600)
    service.ledger.initialize()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


async def test_adapter_disabled_and_exact_delegation_without_transport(tmp_path, monkeypatch):
    from ostag.adapters.feishu import FeishuAdapter
    from ostag.config import Settings
    service = make_service(tmp_path/'cards.db')
    first = await service.deliver(SCOPE, 'job', 'revision')
    args = dict(expected_epoch=0, message_id='om_message_123',
                destination=Destination('feishu', 'oc_private', 'private'),
                tenant_id='tenant', app_id='app', page=0, ttl=2)
    with pytest.raises(CardDeliveryError):
        await FeishuAdapter(Settings()).recover_brief_card(SCOPE, first.delivery_id, **args)
    called = []
    original = service.recover_preview
    async def delegated(*positional, **keywords):
        called.append((positional, keywords))
        return await original(*positional, **keywords)
    def forbidden(*args, **kwargs):
        pytest.fail('recovery must not call transport or legacy send')
    adapter = FeishuAdapter(Settings(), brief_card_service=service)
    monkeypatch.setattr(service, 'recover_preview', delegated)
    for target, names in ((service.transport, ('send', 'check_ack')),
                          (adapter, ('send_card', 'send_message', 'reply'))):
        for name in names:
            monkeypatch.setattr(target, name, forbidden)
    card = await adapter.recover_brief_card(SCOPE, first.delivery_id, **args)
    assert 'Private draft' in json.dumps(card)
    assert called == [((SCOPE, first.delivery_id), args)]
    status = service.ledger.status(SCOPE, first.delivery_id)
    assert status.content_verified is status.human_received is False


@pytest.mark.parametrize('denial', ['retention', 'source', 'grant', 'mapping', 'retired'])
async def test_recovery_cannot_renew_original_lifetime_authority(tmp_path, denial):
    from test_brief_generation import at
    from test_brief_retention import full_stack
    from test_brief_service import ManualRequest, request
    a, briefs, coordinator, p, _, service, _, _, _ = full_stack(tmp_path)
    try:
        body = request() | {'sources': [s.to_dict() for s in p.sources.sources]}
        original_request = ManualRequest.from_dict(body)
        service._authorize = lambda p: briefs._authorize_mirror(
            original_request, p.scope, p.destination, stopped=lambda: False)
        first = await service.deliver(SCOPE, p.job_id, p.revision_id, ttl=1)
        a.now = 1
        card = await recover(service, first.delivery_id)
        if denial == 'retention':
            a.now = 60
        elif denial == 'source':
            a.sources = {key: replace(value, fresh_until=at(1)) for key, value in a.sources.items()}
        elif denial == 'grant':
            a.grant = replace(a.grant, expires_at=at(1))
        elif denial == 'mapping':
            a.destination = PrivateDestination(SCOPE.principal, 'oc_other')
        else:
            coordinator.revoke_job(SCOPE, p.job_id)
            coordinator.cleanup(SCOPE, p.job_id)
        with pytest.raises(CardDeliveryError):
            await recover(service, first.delivery_id, expected_epoch=1)
        with pytest.raises(CardDeliveryError):
            invoke(service, tokens(card)['Refresh'])
    finally:
        briefs.close()


@pytest.mark.parametrize('phase', ['reserve', 'publish'])
async def test_original_revision_rechecked_after_real_retention_wait(tmp_path, monkeypatch, phase):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from test_brief_retention import full_stack
    _, briefs, coordinator, p, _, service, _, _, _ = full_stack(tmp_path)
    waiting, locked, armed = Event(), Event(), Event()
    current = [p]
    service._load = lambda s, j: current[0]
    original_connection = coordinator.ledger._connection
    @contextmanager
    def traced(**kwargs):
        with original_connection(**kwargs) as db:
            db.set_trace_callback(lambda sql: waiting.set()
                                  if armed.is_set() and sql == 'BEGIN IMMEDIATE' else None)
            yield db
    def holder():
        with sqlite3.connect(coordinator.ledger.path) as db:
            db.execute('BEGIN IMMEDIATE')
            locked.set()
            assert waiting.wait(2)
            current[0] = replace(p, revision_id='revoked_revision')
            db.rollback()
    try:
        first = await service.deliver(SCOPE, p.job_id, p.revision_id)
        monkeypatch.setattr(coordinator.ledger, '_connection', traced)
        with ThreadPoolExecutor(1) as pool:
            future = [None]
            def arm():
                armed.set()
                future[0] = pool.submit(holder)
                assert locked.wait(1)
            if phase == 'reserve':
                arm()
            else:
                original_controls = module._controls
                def render(*args):
                    card = original_controls(*args)
                    arm()
                    return card
                monkeypatch.setattr(module, '_controls', render)
            with pytest.raises(CardDeliveryError):
                await recover(service, first.delivery_id)
            future[0].result(1)
        assert service.ledger.status(SCOPE, first.delivery_id).epoch == (
            0 if phase == 'reserve' else 1)
    finally:
        briefs.close()


async def test_nonzero_recovery_page_and_navigation_window_do_not_slide(tmp_path):
    from ostag.briefs.validation import Coverage
    p = replace(preview(Coverage(False, tuple('gap ' + str(i) + '密' * 900 for i in range(10)))),
                outbox_id='original_outbox')
    now = [1000]
    service = make_service(tmp_path/'cards.db', current=[p], now=now)
    first = await service.deliver(SCOPE, 'job', 'revision', ttl=1)
    now[0] = 1001
    card = await recover(service, first.delivery_id, page=1, ttl=3.5)
    assert 'Page 2 of' in json.dumps(card)
    now[0] = 1004
    previous = invoke(service, tokens(card)['Previous'])
    assert 'Page 1 of' in json.dumps(previous)
    now[0] = 1004.5
    with pytest.raises(CardDeliveryError):
        invoke(service, tokens(previous)['Refresh'])


async def test_render_page_missing_spends_but_recovery_can_resume_with_new_epoch(tmp_path):
    service = make_service(tmp_path/'cards.db')
    first = await service.deliver(SCOPE, 'job', 'revision')
    with pytest.raises(CardDeliveryError):
        await recover(service, first.delivery_id, page=65535)
    assert service.ledger.status(SCOPE, first.delivery_id).epoch == 1
    card = await recover(service, first.delivery_id, expected_epoch=1)
    assert 'Private draft' in json.dumps(card)


@pytest.mark.parametrize('phase', ['reserve', 'publish'])
async def test_lost_commit_response_cleans_tokens_without_reusing_cas(tmp_path, monkeypatch, phase):
    service = make_service(tmp_path/'cards.db')
    first = await service.deliver(SCOPE, 'job', 'revision')
    original = service.ledger.recovery_step
    def lost(*args, **kwargs):
        result = original(*args, **kwargs)
        if (kwargs.get('ttl') is not None if phase == 'reserve'
                else kwargs.get('rendered') is not None):
            raise OSError('PRIVATE commit result lost')
        return result
    monkeypatch.setattr(service.ledger, 'recovery_step', lost)
    with pytest.raises(CardDeliveryError) as caught:
        await recover(service, first.delivery_id)
    assert caught.value.__context__ is caught.value.__cause__ is None
    with sqlite3.connect(service.ledger.path) as db:
        assert db.execute('SELECT epoch,response_card FROM cards').fetchone() == (1, None)
        assert db.execute('SELECT count(*) FROM card_tokens').fetchone()[0] == 0
    with pytest.raises(CardDeliveryError):
        await recover(make_service(service.ledger.path), first.delivery_id)


async def test_cancel_after_reservation_does_not_restore_epoch(tmp_path, monkeypatch):
    service = make_service(tmp_path/'cards.db')
    first = await service.deliver(SCOPE, 'job', 'revision')
    original = module._controls
    def render(*args):
        card = original(*args)
        asyncio.current_task().cancel()
        return card
    monkeypatch.setattr(module, '_controls', render)
    with pytest.raises(asyncio.CancelledError):
        await asyncio.create_task(recover(service, first.delivery_id))
    assert service.active_operations == 0
    assert service.ledger.status(SCOPE, first.delivery_id).epoch == 1
    with pytest.raises(CardDeliveryError):
        await recover(make_service(service.ledger.path), first.delivery_id)


async def test_submicrosecond_ttl_never_rounds_up_into_live_authority(tmp_path):
    service = make_service(tmp_path/'cards.db')
    first = await service.deliver(SCOPE, 'job', 'revision')
    with pytest.raises(CardDeliveryError):
        await recover(service, first.delivery_id, ttl=0.0000001)
    with sqlite3.connect(service.ledger.path) as db:
        assert db.execute('SELECT epoch,response_card FROM cards').fetchone() == (1, None)
        assert db.execute('SELECT count(*) FROM card_tokens').fetchone()[0] == 0


@pytest.mark.parametrize('phase', ['initial', 'lost_claim', 'ack'])
async def test_delivery_status_retries_real_sqlite_contention(tmp_path, monkeypatch, phase):
    """A competing writer's commit must not turn a status read into failure."""
    service = make_service(tmp_path/'cards.db')
    await service.prepare(SCOPE, 'job', 'revision')
    holder = sqlite3.connect(service.ledger.path)
    original = service.ledger.status
    waiting, reads = asyncio.Event(), []

    def status(*args):
        reads.append(1)
        if len(reads) == (1 if phase == 'initial' else 2):
            holder.execute('BEGIN EXCLUSIVE')
            waiting.set()
        return original(*args)

    def lost_claim(*args, **kwargs):
        # A different process already owns the one permitted send attempt.
        holder.execute("UPDATE cards SET state='started'")
        holder.commit()
        return None

    monkeypatch.setattr(service.ledger, 'status', status)
    if phase == 'lost_claim':
        monkeypatch.setattr(service.ledger, 'begin', lost_claim)
    task = asyncio.create_task(service.deliver(SCOPE, 'job', 'revision'))
    try:
        await asyncio.wait_for(waiting.wait(), 1)
        assert not task.done()
        holder.rollback()
        result = await task
        assert result.state == ('started' if phase == 'lost_claim' else 'create_acknowledged')
        assert service.active_operations == 0
        with sqlite3.connect(service.ledger.path) as db:
            assert db.execute('SELECT first_failure FROM cards').fetchone() == (None,)
    finally:
        holder.close()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
