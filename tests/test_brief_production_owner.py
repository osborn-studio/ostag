"""Runnable synthetic server lifespan; never provider/live acceptance evidence."""
import asyncio
from dataclasses import replace
import json
from types import SimpleNamespace

from fastapi.testclient import TestClient as RawTestClient
import pytest

from ostag.briefs.authority import AuthoritySnapshot, TrustedBriefAuthority
from ostag.briefs.generation import GenerationConfig
from ostag.briefs.lifecycle import LifecycleDenied
from ostag.briefs.production_owner import TrustedBriefComposition, validate_production_drain
from ostag.briefs.retention import RetentionPolicy
from ostag.briefs.service import SourceSnapshot
from ostag.briefs.store import PrivateDestination, RequesterContext
from ostag.config import Settings
from ostag.work_monitor.server import create_app
from test_brief_control_bridge import generation, observed
from test_brief_cards import BINDING
from test_brief_card_delivery import ack
from ostag.briefs.card_transport import CardTransport
from test_brief_feishu_transport import CredentialSnapshot
from test_brief_openai_transport import Wire, adapter, envelope, parameters
from test_brief_validation import output
from test_meeting_contracts import ACTOR, grant
from test_meeting_service import at, current_request
import httpx


async def await_business_startup(owner):
    task = getattr(owner, '_startup_task', None)
    if task is not None:
        await asyncio.wait_for(asyncio.shield(task), 6 + owner._reconciliation_budget())


async def in_current_drain(owner, generation, action):
    """Inspect a current proof atomically, never race a saved observed() result."""
    args = dict(boot_id=owner.status.boot_id, generation=generation, **owner.control_identity())
    async with asyncio.timeout(4):
        while True:
            async with owner.control_lock:
                current = owner.status.last_status
                if validate_production_drain(owner, current, **args):
                    return await action(current, args)
            await asyncio.sleep(.01)


class TestClient(RawTestClient):
    """Business tests explicitly await readiness; isolation tests use raw lifespan."""
    __test__ = False

    def __enter__(self):
        import sys
        client = super().__enter__()
        try:
            client.portal.call(await_business_startup, client.app.state.brief_owner)
        except BaseException:
            super().__exit__(*sys.exc_info())
            raise
        return client


def host(tmp_path, **changes):
    directory = tmp_path / 'business'
    directory.mkdir(mode=0o700, exist_ok=True)
    status = tmp_path / 'status'
    status.mkdir(exist_ok=True)
    req = current_request()
    scope = RequesterContext(req.namespace, ACTOR)
    authority = TrustedBriefAuthority(clock=lambda: at(10), monotonic=lambda: 10., max_ttl_seconds=300)
    snapshot = AuthoritySnapshot(1, at(), at(290), permissions=((ACTOR, 'propose'),),
        destinations=((scope, PrivateDestination(ACTOR, 'oc_private')),), grants=(grant(),),
        sources=tuple(SourceSnapshot(scope, s, at(290)) for s in req.sources), meetings=(req,))
    authority.publish(snapshot)
    result = envelope()
    model_output = output(text='Synthetic change requires review')
    model_output['progress'][0]['claim']['source_ids'] = [req.sources[0].source_id]
    result['output'][0]['content'][0]['text'] = json.dumps(model_output)
    wire = Wire(result)
    sends = []
    def send(request):
        sends.append(request)
        return httpx.Response(200, json=ack())
    credential = CredentialSnapshot(BINDING, 'synthetic-token', 'actual_sender', 'app_id',
                                    (('oc_private', ACTOR),))
    c = TrustedBriefComposition(directory, 'a' * 64, authority, adapter(wire),
        GenerationConfig(parameters(max_input_bytes=100000, max_job_micros=300000), 1000000),
        lambda context: scope if context is token else None, lambda: BINDING,
        CardTransport(resolve=lambda: credential, transport=httpx.MockTransport(send)),
        (scope,), RetentionPolicy(at(280)), initialize_new=True)
    token = object()
    c = replace(c, **changes)
    settings = Settings(monitor_server_db=tmp_path / 'monitor.db', brief_status_dir=status,
        brief_control_path=tmp_path / 'control.json', brief_enabled=True, brief_meetings_enabled=True,
        feishu_use_websocket=False, monitor_scheduler_enabled=False)
    return SimpleNamespace(composition=c, settings=settings, request=req, scope=scope,
                           token=token, wire=wire, sends=sends, snapshot=snapshot)


async def open_owner(owner, number=1):
    await owner.request_control(boot_id=owner.status.boot_id, generation=number, admission='on',
                                **owner.control_identity())
    await generation(owner.status, number)


async def pause_owner(owner, number):
    await owner.request_control(boot_id=owner.status.boot_id, generation=number, admission='off',
                                **owner.control_identity())


def completion_handshake(owner):
    """Observe actual cycle completion, after worker and card custody returned.

    A competing primary-store polling read can legitimately see StoreBusy. This
    handshake reads on the existing business loop after its real cycle completes;
    it neither retries a failed operation nor bypasses the running owner.
    """
    from concurrent.futures import Future
    import sqlite3
    completed = Future()
    cycle = owner._cycle
    async def observed_cycle():
        try:
            await cycle()
            with sqlite3.connect(owner.cards.ledger.path) as db:
                rows = db.execute('SELECT state FROM cards').fetchall()
            if rows == [('create_acknowledged',)] and not completed.done():
                with sqlite3.connect(owner.service.store.path) as db:
                    jobs = db.execute('SELECT job_id,state FROM brief_jobs').fetchall()
                completed.set_result(jobs)
        except BaseException as error:
            if not completed.done():
                error.add_note(repr((owner.reason, owner.lifecycle.snapshot(), owner.status.last_status)))
                completed.set_exception(error)
            raise
    owner._cycle = observed_cycle
    return completed


async def completed_cycle(completion):
    wrapped = asyncio.wrap_future(completion)
    wrapped.add_done_callback(lambda f: f.exception() if not f.cancelled() else None)
    return await asyncio.wait_for(asyncio.shield(wrapped), 8)


def test_closed_worker_refusal_does_not_start_cleanup(tmp_path):
    from test_brief_startup_inventory import snapshot
    h = host(tmp_path)
    with TestClient(create_app(settings=h.settings, brief_composition=h.composition)) as client:
        owner = client.app.state.brief_owner
        assert owner.ready and not owner.lifecycle.snapshot().control_valid
        before = snapshot(h.composition.directory)
        client.portal.call(owner._call, owner._cycle)
        assert owner.ready and not owner.lifecycle.snapshot().uncertain
        assert snapshot(h.composition.directory) == before


def test_meeting_through_real_server_lifespan_and_clean_restart(tmp_path):
    h = host(tmp_path)
    app = create_app(settings=h.settings, brief_composition=h.composition)
    with TestClient(app) as client:
        owner = app.state.brief_owner
        assert owner.ready, (owner.reason, owner.report)
        assert owner.service.lifecycle is owner.worker.lifecycle is owner.cards.lifecycle is owner.retention.lifecycle is owner.lifecycle
        assert owner.status._lifecycle is owner.lifecycle
        assert set(p.name for p in h.composition.directory.iterdir()) == {
            'owner.lock', 'brief.db', 'card.db', 'retention.db', 'revision.db', 'callback.db'}
        assert not owner.lifecycle.snapshot().control_valid
        completion = completion_handshake(owner)
        client.portal.call(open_owner, owner)
        result = client.portal.call(app.state.brief_application.submit_meeting, h.token, 'meeting-1', h.request)
        assert result.status == 'queued'
        assert client.portal.call(completed_cycle, completion) == [(result.job_id, 'draft_ready')]
        try:
            exported = client.portal.call(app.state.brief_application.private_export, h.token, result.job_id)
        except Exception:
            print(owner.reason, owner.status.reason, owner.lifecycle.snapshot(), owner.status.last_status)
            raise
        assert 'Private meeting pre-read' in exported
        assert h.wire.calls
        client.portal.call(pause_owner, owner, 2)
        async def checked(current, args):
            assert current['control_generation'] == 2 and current['drain_ack']
            assert validate_production_drain(owner, current, **args)
        client.portal.call(in_current_drain, owner, 2, checked)
        assert client.get('/healthz').json() == {'status': 'ok'}
    app = create_app(settings=h.settings, brief_composition=replace(h.composition, initialize_new=False))
    with TestClient(app):
        owner = app.state.brief_owner
        assert owner.ready, (owner.reason, owner.report)
        assert not owner.lifecycle.snapshot().control_valid


def test_all_off_ignores_composition_without_business_artifacts(tmp_path):
    h = host(tmp_path)
    configured = replace(h.settings, brief_enabled=False, brief_meetings_enabled=False)
    with TestClient(create_app(settings=configured, brief_composition=h.composition)) as client:
        assert client.app.state.brief_application is None
        assert client.get('/healthz').status_code == 200
    assert not list(h.composition.directory.iterdir())


def test_missing_composition_degrades_brief_only(tmp_path):
    h = host(tmp_path)
    with TestClient(create_app(settings=h.settings)) as client:
        assert client.get('/healthz').status_code == 200
        assert client.get('/healthz').headers['X-OStag-Brief-Status'] == 'degraded'
        assert client.app.state.brief_owner.reason == 'trusted_composition_missing'
    assert not list(h.composition.directory.iterdir())


def test_unverified_identity_never_submits(tmp_path):
    h = host(tmp_path)
    with TestClient(create_app(settings=h.settings, brief_composition=h.composition)) as client:
        owner = client.app.state.brief_owner
        assert owner.ready, (owner.reason, owner.report)
        client.portal.call(open_owner, owner)
        with pytest.raises(LifecycleDenied):
            client.portal.call(owner.application.submit_meeting, object(), 'bad', h.request)
        assert not h.wire.calls


def test_manual_job_uses_same_running_service_worker_and_card_owner(tmp_path):
    from test_brief_service import Authority, SCOPE, event
    from test_brief_generation import at as manual_at
    h = host(tmp_path)
    manual = Authority()
    authority = TrustedBriefAuthority(clock=lambda: manual_at(10), monotonic=lambda: 10., max_ttl_seconds=300)
    snapshot = AuthoritySnapshot(1, manual_at(), manual_at(290), permissions=((SCOPE.principal, 'propose'),),
        destinations=((SCOPE, PrivateDestination(SCOPE.principal, 'oc_private')),), grants=(manual.grant,),
        sources=tuple(manual.sources.values()))
    authority.publish(snapshot)
    wire = Wire()
    transport = adapter(wire)
    from ostag.briefs.feishu_transport import CredentialSnapshot
    credential = CredentialSnapshot(BINDING, 'synthetic', 'actual_sender', 'app_id',
                                     (('oc_private', SCOPE.principal),))
    c = replace(h.composition, authority=authority, owners=(SCOPE,),
        model_transport=transport, authenticate=lambda ctx: SCOPE if ctx is h.token else None,
        card_transport=CardTransport(resolve=lambda: credential, transport=httpx.MockTransport(
            lambda req: httpx.Response(200, json=ack()))), policy=RetentionPolicy(manual_at(280)))
    with TestClient(create_app(settings=replace(h.settings, brief_meetings_enabled=False), brief_composition=c)) as client:
        owner = client.app.state.brief_owner
        completion = completion_handshake(owner)
        client.portal.call(open_owner, owner)
        result = client.portal.call(owner.application.submit, h.token, event())
        assert result.status == 'queued'
        assert client.portal.call(completed_cycle, completion) == [(result.job_id, 'draft_ready')]
        assert wire.calls and client.portal.call(owner.application.private_export, h.token, result.job_id)
        authority.publish(replace(snapshot, revision=2, sources=()))
        with pytest.raises(Exception):
            client.portal.call(owner.application.private_export, h.token, result.job_id)
        assert client.get('/healthz').status_code == 200


def test_duplicate_composition_different_status_directory(tmp_path):
    h = host(tmp_path)
    other = tmp_path / 'other-status'
    other.mkdir()
    with TestClient(create_app(settings=h.settings, brief_composition=h.composition)) as first:
        owner = first.app.state.brief_owner
        assert owner.ready
        settings = replace(h.settings, brief_status_dir=other)
        with TestClient(create_app(settings=settings, brief_composition=h.composition)) as second:
            loser = second.app.state.brief_owner
            assert not loser.ready and loser.worker is None
            assert loser._lock is None
            assert second.get('/healthz').status_code == 200
        assert owner.ready and owner._lock is not None
    assert not owner._business.thread.is_alive()
    assert not owner.worker._thread.is_alive()
    assert not owner.worker._notifier.is_alive()


@pytest.mark.parametrize('damage', ['missing', 'foreign', 'wal', 'alias', 'unwritable', 'schema', 'partial'])
def test_incompatible_startup_is_read_only_and_monitor_remains_healthy(tmp_path, damage):
    import os
    import sqlite3
    from test_brief_startup_inventory import snapshot
    h = host(tmp_path)
    with TestClient(create_app(settings=h.settings, brief_composition=h.composition)):
        pass
    path = h.composition.directory / 'card.db'
    if damage in ('missing', 'partial'):
        path.unlink()
    elif damage == 'foreign':
        path.write_bytes(b'foreign bytes')
    elif damage == 'alias':
        path.unlink()
        os.link(h.composition.directory / 'brief.db', path)
    elif damage == 'unwritable':
        path.chmod(0o644)
    else:
        with sqlite3.connect(path) as db:
            db.execute('PRAGMA journal_mode=WAL' if damage == 'wal' else 'PRAGMA user_version=99')
    before = snapshot(h.composition.directory)
    with TestClient(create_app(settings=h.settings, brief_composition=h.composition)) as client:
        owner = client.app.state.brief_owner
        assert not owner.ready and owner.worker is None
        assert client.get('/healthz').headers['X-OStag-Brief-Status'] == 'degraded'
    assert snapshot(h.composition.directory) == before


def test_maintenance_does_not_construct_services_or_write_business_files(tmp_path):
    from test_brief_startup_inventory import snapshot
    h = host(tmp_path)
    with TestClient(create_app(settings=h.settings, brief_composition=h.composition)):
        pass
    before = snapshot(h.composition.directory)
    c = replace(h.composition, initialize_new=False, maintenance=True)
    with TestClient(create_app(settings=h.settings, brief_composition=c)) as client:
        owner = client.app.state.brief_owner
        assert owner.ready and owner.service is owner.worker is owner.cards is None
        async def maintenance():
            await owner.request_control(boot_id=owner.status.boot_id, generation=1, admission='off',
                                        maintenance=True, **owner.control_identity())
            return await observed(owner.status, lambda s: s['drain_ack'])
        assert client.portal.call(maintenance)['maintenance']
        with pytest.raises(LifecycleDenied):
            client.portal.call(owner.application.submit_meeting, h.token, 'no', h.request)
    assert snapshot(h.composition.directory) == before


def test_current_drain_rejects_saved_observation_and_all_identity_mismatches(tmp_path):
    from ostag.briefs.runtime import _status_shape
    h = host(tmp_path)
    with TestClient(create_app(settings=h.settings, brief_composition=h.composition)) as client:
        owner = client.app.state.brief_owner
        client.portal.call(pause_owner, owner, 1)
        args = dict(boot_id=owner.status.boot_id, generation=1, **owner.control_identity())
        async def checks(current, args):
            assert validate_production_drain(owner, current, **args)
            assert _status_shape(current)
            assert not _status_shape(current | {'version': 3.0})
            assert not validate_production_drain(owner, dict(current), **args)
            original = current['production']['image_sha256']
            current['production']['image_sha256'] = 'f' * 64
            assert not validate_production_drain(owner, current, **args)
            current['production']['image_sha256'] = original
            assert validate_production_drain(owner, current, **args)
            for key, value in dict(boot_id='old-boot', generation=0, image_sha256='f'*64,
                                   config_sha256='f'*64, persistence='f'*64).items():
                assert not validate_production_drain(owner, current, **(args | {key: value}))
            return current
        current = client.portal.call(in_current_drain, owner, 1, checks)
        client.portal.call(open_owner, owner, 2)
        assert not validate_production_drain(owner, current, **args)
        async def stale_control():
            await owner.request_control(**(args | dict(admission='on')))
        with pytest.raises(LifecycleDenied):
            client.portal.call(stale_control)


@pytest.mark.parametrize("stop_before_release", [False, True])
def test_pause_during_actual_transport_retains_unknown_and_restart_quarantines(tmp_path, stop_before_release):
    from threading import Event
    h = host(tmp_path)
    entered, completed = Event(), Event()
    held = {}
    async def effect(request):
        if not request.url.path.endswith('input_tokens'):
            held['loop'] = asyncio.get_running_loop()
            held['release'] = held['loop'].create_future()
            entered.set()
            try:
                await asyncio.shield(held['release'])
            except asyncio.CancelledError:
                await asyncio.shield(held['release'])
            finally:
                completed.set()
    h.wire.effect = effect
    app = create_app(settings=h.settings, brief_composition=h.composition)
    with TestClient(app) as client:
        owner = app.state.brief_owner
        client.portal.call(open_owner, owner)
        first = client.portal.call(owner.application.submit_meeting, h.token, 'held', h.request)
        assert first.status == 'queued' and entered.wait(3)
        try:
            client.portal.call(pause_owner, owner, 2)
            state = client.portal.call(observed, owner.status, lambda s: s['control_generation'] == 2)
            assert not state['drain_ack']
            assert owner.lifecycle.snapshot().custody['model'] > 0
            assert owner.worker.run_once().result(1) in ('busy', 'unavailable', 'stopped')
            with pytest.raises(LifecycleDenied):
                client.portal.call(owner.application.submit_meeting, h.token, 'post-pause', h.request)
            assert client.get('/healthz').status_code == 200
            if stop_before_release:
                # Local stop returns, but unresolved remote custody retains the
                # loop and lifetime lock. Never equate loop destruction to drain.
                client.portal.call(owner.stop)
                assert owner.reason == 'shutdown_pending'
                assert owner.worker._thread.is_alive()
                assert owner._lock is not None
                assert not owner.status.last_status['drain_ack']
                assert client.get('/healthz').headers['X-OStag-Brief-Status'] == 'degraded'
                import fcntl
                with (h.composition.directory / 'owner.lock').open('rb') as contender:
                    with pytest.raises(BlockingIOError):
                        fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            held['loop'].call_soon_threadsafe(held['release'].set_result, None)
            assert completed.wait(3)
    assert not owner.worker._thread.is_alive()
    import sqlite3
    with sqlite3.connect(h.composition.directory / 'brief.db') as db:
        assert db.execute('SELECT count(*) FROM brief_jobs').fetchone() == (1,)
        assert db.execute('SELECT attempt FROM brief_jobs WHERE job_id=?', (first.job_id,)).fetchone() == (1,)
        assert db.execute("SELECT count(*) FROM brief_cost_calls WHERE state IN ('started','unknown')").fetchone()[0] > 0
    with TestClient(create_app(settings=h.settings, brief_composition=replace(h.composition, initialize_new=False))) as client:
        owner = client.app.state.brief_owner
        assert not owner.ready and owner.worker is None
        assert 'unknown_remote_effect' in owner.report.blockers
        assert not owner.status.last_status['drain_ack']


def _contender(root, persistence, connection):
    # Interpreter imports are harness setup, not the owner's production deadline.
    connection.send('imported')
    assert connection.poll(15) and connection.recv() == 'start'
    root.mkdir(mode=0o700)
    h = host(root, directory=persistence, initialize_new=False)
    from ostag.briefs.production_owner import BriefProductionOwner
    construct = BriefProductionOwner._construct_owned
    failures = []

    def observed_construct(owner):
        try:
            return construct(owner)
        except Exception as exc:
            failures.append(type(exc).__name__)
            raise

    BriefProductionOwner._construct_owned = observed_construct
    with TestClient(create_app(settings=h.settings, brief_composition=h.composition)) as client:
        owner = client.app.state.brief_owner
        connection.send((owner.ready, owner.worker is None, owner.reason,
                         owner.status.health, failures, client.get('/healthz').status_code))
    connection.close()


def test_duplicate_process_cannot_share_persistence(tmp_path):
    import multiprocessing
    h = host(tmp_path)
    context = multiprocessing.get_context('spawn')
    parent, child = context.Pipe()
    process = context.Process(target=_contender, args=(tmp_path / 'contender', h.composition.directory, child))
    process.start()
    child.close()
    try:
        # Cold imports get a separate harness budget. The real application still
        # has unchanged 5s bootstrap / 500ms IO deadlines and an 8s observation.
        assert parent.poll(30), 'contender interpreter bootstrap missing'
        assert parent.recv() == 'imported'
        with TestClient(create_app(settings=h.settings, brief_composition=h.composition)) as first:
            assert first.app.state.brief_owner.ready
            parent.send('start')
            assert parent.poll(8), 'contender application startup missing'
            assert parent.recv() == (False, True, 'startup_unavailable', 'ok', ['BlockingIOError'], 200)
            process.join(5)
            assert process.exitcode == 0
    finally:
        if process.is_alive():
            process.kill()
            process.join(3)
        parent.close()
        process.close()


def test_partial_construction_joins_created_service_and_releases_owner(tmp_path, monkeypatch):
    from ostag.briefs import production_owner
    h = host(tmp_path)
    def fail(**kwargs):
        raise RuntimeError('synthetic construction failure')
    with monkeypatch.context() as patch:
        patch.setattr(production_owner, 'CardDeliveryService', fail)
        with TestClient(create_app(settings=h.settings, brief_composition=h.composition)) as client:
            owner = client.app.state.brief_owner
            assert not owner.ready and owner.service._closed.is_set()
            assert owner.worker is None and owner._lock is None
            assert not owner._business.thread.is_alive()
            assert client.get('/healthz').status_code == 200
    with TestClient(create_app(settings=h.settings, brief_composition=h.composition)) as client:
        assert client.app.state.brief_owner.ready


def test_busy_startup_does_not_change_any_store(tmp_path):
    import sqlite3
    from test_brief_startup_inventory import snapshot
    h = host(tmp_path)
    with TestClient(create_app(settings=h.settings, brief_composition=h.composition)):
        pass
    before = snapshot(h.composition.directory)
    with sqlite3.connect(h.composition.directory / 'brief.db') as holder:
        holder.execute('BEGIN EXCLUSIVE')
        with TestClient(create_app(settings=h.settings, brief_composition=h.composition)) as client:
            owner = client.app.state.brief_owner
            assert not owner.ready and owner.worker is None
            assert 'brief:locked' in owner.report.blockers
            assert client.get('/healthz').status_code == 200
    assert snapshot(h.composition.directory) == before


def test_slow_reconciliation_keeps_heartbeats_and_recovers_drain(tmp_path, monkeypatch):
    from threading import Event
    import time
    h = host(tmp_path)
    entered, release = Event(), Event()
    with TestClient(create_app(settings=h.settings, brief_composition=h.composition)) as client:
        owner = client.app.state.brief_owner
        original = owner._reconcile
        def stalled():
            entered.set()
            assert release.wait(6)
            return original()
        monkeypatch.setattr(owner, '_reconcile', stalled)
        try:
            client.portal.call(pause_owner, owner, 1)
            assert entered.wait(3)
            first = client.portal.call(observed, owner.status,
                lambda s: s['production']['reason'] == 'reconciliation_pending')
            # A slow legal scan exceeds the old 500ms call budget; real status
            # IPC must keep renewing its unchanged 750ms authority meanwhile.
            time.sleep(.65)
            later = owner.status.last_status
            assert later['request_id'] > first['request_id']
            assert not later['shutdown'] and not later['drain_ack']
            assert owner.ready and owner.status.health == 'ok'
            start = time.monotonic()
            assert client.get('/healthz').status_code == 200
            assert time.monotonic() - start < .3
            with pytest.raises(LifecycleDenied):
                client.portal.call(open_owner, owner, 2)
            assert not owner.lifecycle.snapshot().shutdown
        finally:
            release.set()
        state = client.portal.call(observed, owner.status, lambda s: s['drain_ack'])
        assert state['control_generation'] == 1 and state['production']['reconciled']
    assert not owner._business.thread.is_alive()
    assert owner._lock is None


def test_missing_status_precedes_business_store_or_worker_creation(tmp_path):
    h = host(tmp_path)
    h.settings.brief_status_dir.rmdir()
    with TestClient(create_app(settings=h.settings, brief_composition=h.composition)) as client:
        owner = client.app.state.brief_owner
        assert owner.worker is None and owner._business is None
        assert client.get('/healthz').status_code == 200
    assert list(h.composition.directory.iterdir()) == []


@pytest.mark.parametrize('suffix', ['-wal', '-shm', '-journal'])
def test_new_install_never_consumes_orphan_recovery_evidence(tmp_path, suffix):
    from test_brief_startup_inventory import snapshot
    h = host(tmp_path)
    (h.composition.directory / ('brief.db' + suffix)).write_bytes(b'previous boot evidence')
    lock = h.composition.directory / 'owner.lock'
    lock.touch(mode=0o600)
    before = snapshot(h.composition.directory)
    with TestClient(create_app(settings=h.settings, brief_composition=h.composition)) as client:
        owner = client.app.state.brief_owner
        assert not owner.ready and owner.service is owner.worker is None
        assert client.get('/healthz').status_code == 200
    assert snapshot(h.composition.directory) == before


def test_read_only_business_file_cannot_be_advertised_ready(tmp_path):
    from test_brief_startup_inventory import snapshot
    h = host(tmp_path)
    with TestClient(create_app(settings=h.settings, brief_composition=h.composition)):
        pass
    (h.composition.directory / 'brief.db').chmod(0o400)
    before = snapshot(h.composition.directory)
    with TestClient(create_app(settings=h.settings, brief_composition=h.composition)) as client:
        owner = client.app.state.brief_owner
        assert not owner.ready and owner.worker is None
        assert client.get('/healthz').status_code == 200
    assert snapshot(h.composition.directory) == before


@pytest.mark.parametrize('field', ['card_capacity', 'retention_capacity', 'revision_capacity',
    'callback_capacity', 'revision_chain_limit', 'revision_event_limit'])
def test_invalid_capacity_rejected_before_any_owner_or_store(tmp_path, field):
    h = host(tmp_path)
    with pytest.raises(ValueError):
        replace(h.composition, inventory=replace(h.composition.inventory, **{field: True}))
    assert not list(h.composition.directory.iterdir())


@pytest.mark.parametrize('production', [False, True])
def test_renewal_uses_original_authority_until_ack(monkeypatch, production):
    """Deterministic clock contract evidence, not filesystem or live proof."""
    from ostag.briefs import lifecycle
    now = [10.0]
    monkeypatch.setattr(lifecycle, 'monotonic', lambda: now[0])
    life = lifecycle.BriefLifecycle()
    status = SimpleNamespace(_production=object() if production else None)
    life._bind_file_owner(status)
    epoch, _ = life._file_prepare(status, (1, 'on', False), now[0])
    with pytest.raises(LifecycleDenied):
        life.begin('model')
    life._file_ack(status, epoch, now[0])
    existing = life.begin('worker')
    now[0] = 10.5
    renewal, _ = life._file_prepare(status, (1, 'on', False), now[0])
    assert life._deadline == 10.75
    existing.check()
    if production:
        nested = life.begin('model', parent=existing)
        nested.finish()
    else:
        with pytest.raises(LifecycleDenied):
            life.begin('model', parent=existing)
    now[0] = 10.75
    with pytest.raises(LifecycleDenied):
        existing.check()
    with pytest.raises(LifecycleDenied):
        life._file_ack(status, renewal, 10.5)
    with pytest.raises(LifecycleDenied):
        life.begin('intake')
    existing.finish()


def test_changed_off_invalidates_work_and_stale_publication_ack(monkeypatch):
    from ostag.briefs import lifecycle
    now = [10.0]
    monkeypatch.setattr(lifecycle, 'monotonic', lambda: now[0])
    life = lifecycle.BriefLifecycle()
    status = SimpleNamespace(_production=object())
    life._bind_file_owner(status)
    epoch, _ = life._file_prepare(status, (1, 'on', False), now[0])
    life._file_ack(status, epoch, now[0])
    work = life.begin('worker')
    now[0] = 10.25
    renewal, _ = life._file_prepare(status, (1, 'on', False), now[0])
    now[0] = 10.5
    _, snapshot = life._file_prepare(status, (2, 'off', False), now[0])
    assert snapshot.custody['worker'] == 1 and not snapshot.drained
    with pytest.raises(LifecycleDenied):
        work.check()
    with pytest.raises(LifecycleDenied):
        life.begin('model')
    with pytest.raises(LifecycleDenied):
        life._file_ack(status, renewal, 10.25)
    work.finish()
    assert not life.snapshot().control_valid


def test_operator_generation_cannot_be_reused_before_observation(tmp_path, monkeypatch):
    h = host(tmp_path)
    with TestClient(create_app(settings=h.settings, brief_composition=h.composition)) as client:
        owner = client.app.state.brief_owner
        # Hold the next sampler without changing production authority deadlines.
        # This isolates two operator writes before either has been acknowledged.
        async def check():
            reached, release = asyncio.Event(), asyncio.Event()
            original = owner.status._publish_attached
            async def hold(state):
                reached.set()
                await release.wait()
                return await original(state)
            monkeypatch.setattr(owner.status, '_publish_attached', hold)
            try:
                await asyncio.wait_for(reached.wait(), 2)
                await pause_owner(owner, 1)
                assert owner.lifecycle.snapshot().control_generation is None
                with pytest.raises(LifecycleDenied):
                    await pause_owner(owner, 1)
            finally:
                release.set()
                monkeypatch.setattr(owner.status, '_publish_attached', original)
        client.portal.call(check)


def test_unclaimed_meeting_survives_restart_and_runs_only_after_fresh_control(tmp_path):
    from concurrent.futures import Future
    from threading import Event
    h = host(tmp_path)
    entered, completed = Event(), Future()
    with TestClient(create_app(settings=h.settings, brief_composition=h.composition)) as client:
        owner = client.app.state.brief_owner
        cycle = owner._cycle
        held = {}
        async def before_claim():
            held['loop'] = asyncio.get_running_loop()
            held['release'] = held['loop'].create_future()
            entered.set()
            try:
                await held['release']
                await cycle()
                completed.set_result(None)
            except BaseException as error:
                completed.set_exception(error)
                raise
        owner._cycle = before_claim
        client.portal.call(open_owner, owner)
        assert entered.wait(3)
        try:
            result = client.portal.call(owner.application.submit_meeting, h.token, 'queued-restart', h.request)
            assert result.status == 'queued'
            client.portal.call(pause_owner, owner, 2)
        finally:
            held['loop'].call_soon_threadsafe(held['release'].set_result, None)
            completed.result(3)
        state = client.portal.call(observed, owner.status, lambda s: s['drain_ack'])
        assert state['control_generation'] == 2 and not h.wire.calls and not h.sends
        old_boot = owner.status.boot_id
    with TestClient(create_app(settings=h.settings,
            brief_composition=replace(h.composition, initialize_new=False))) as client:
        owner = client.app.state.brief_owner
        assert owner.ready and owner.report.queued == 1
        assert owner.status.boot_id != old_boot and not owner.lifecycle.snapshot().control_valid
        with pytest.raises(LifecycleDenied):
            client.portal.call(owner.application.status, h.token, result.job_id)
        completion = completion_handshake(owner)
        client.portal.call(open_owner, owner)
        assert client.portal.call(completed_cycle, completion) == [(result.job_id, 'draft_ready')]
        assert h.wire.calls and h.sends


@pytest.mark.parametrize('thread_name', ['brief-owner', 'brief-completion', 'brief-worker'])
def test_thread_start_failure_degrades_only_brief_and_reaps_partial_threads(tmp_path, monkeypatch, thread_name):
    from threading import Thread
    h = host(tmp_path)
    original = Thread.start
    threads = []
    def start(thread):
        if thread.name in ('brief-owner', 'brief-completion', 'brief-worker'):
            threads.append(thread)
        if thread.name == thread_name:
            raise RuntimeError('synthetic thread start failure')
        return original(thread)
    with monkeypatch.context() as patch:
        patch.setattr(Thread, 'start', start)
        with TestClient(create_app(settings=h.settings, brief_composition=h.composition)) as client:
            owner = client.app.state.brief_owner
            assert client.get('/healthz').json() == {'status': 'ok'}
            assert not owner.ready and owner.reason == 'startup_unavailable'
            assert owner.worker is None and owner._lock is None
            assert not any(thread.is_alive() for thread in threads)
            if thread_name == 'brief-owner':
                assert list(h.composition.directory.iterdir()) == []
    with TestClient(create_app(settings=h.settings, brief_composition=h.composition)) as client:
        assert client.app.state.brief_owner.ready


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome', ['success', 'old_generation', 'stale', 'late', 'error', 'quarantined'])
async def test_reconciliation_completion_is_bounded_and_generation_bound(tmp_path, monkeypatch, outcome):
    from concurrent.futures import Future
    from ostag.briefs.production_owner import BriefProductionOwner
    import ostag.briefs.production_owner as module
    h = host(tmp_path)
    owner = BriefProductionOwner(h.settings, h.composition)
    now = [10.0]
    monkeypatch.setattr(module, 'monotonic', lambda: now[0])
    jobs = []
    def submit(function):
        result = Future()
        jobs.append(result)
        return result
    owner._business = SimpleNamespace(submit=submit)
    owner.ready, owner.reason, owner.persistence = True, 'ready', 'disk'
    report = SimpleNamespace(ready=True, persistence='disk', binding='old', active_leases=0, unknown_effects=0)
    owner.report = report
    snapshot = SimpleNamespace(drained=True, uncertain=False, control_generation=1)
    first = await owner.evidence(snapshot)
    assert first['reason'] == 'reconciliation_pending' and not first['reconciled']
    assert len(jobs) == 1
    now[0] = 10.7
    assert not (await owner.evidence(snapshot))['reconciled']
    assert owner.ready and len(jobs) == 1
    fresh = SimpleNamespace(ready=outcome != 'quarantined', persistence='disk', binding='new',
                            active_leases=0, unknown_effects=0)
    if outcome == 'error':
        jobs[0].set_exception(ValueError('synthetic scan failure'))
    else:
        finished = 10.6
        if outcome == 'late':
            finished = now[0] = owner._reconciliation_deadline + .1
        if outcome == 'stale':
            now[0] = 11.5
        jobs[0].set_result((fresh, finished))
    if outcome == 'old_generation':
        snapshot.control_generation = 2
    result = await owner.evidence(snapshot)
    assert result['reconciled'] is (outcome == 'success')
    assert owner.ready is (outcome not in ('late', 'error', 'quarantined'))
    assert not owner.lifecycle.snapshot().shutdown
    if outcome in ('old_generation', 'stale'):
        assert owner.report is report and len(jobs) == 2
    if outcome == 'success':
        assert owner.report is fresh and len(jobs) == 1


@pytest.mark.asyncio
async def test_pending_reconciliation_timeout_retains_custody_and_denies_on(tmp_path, monkeypatch):
    from concurrent.futures import Future
    from ostag.briefs.production_owner import BriefProductionOwner
    import ostag.briefs.production_owner as module
    h = host(tmp_path)
    owner = BriefProductionOwner(h.settings, h.composition)
    now = [10.0]
    monkeypatch.setattr(module, 'monotonic', lambda: now[0])
    pending = Future()
    owner._business = SimpleNamespace(submit=lambda function: pending)
    owner.ready, owner.reason, owner.persistence = True, 'ready', 'disk'
    owner.report = SimpleNamespace(ready=True, binding='old', active_leases=0, unknown_effects=0)
    snapshot = SimpleNamespace(drained=True, uncertain=False, control_generation=1)
    await owner.evidence(snapshot)
    now[0] = owner._reconciliation_deadline + .01
    result = await owner.evidence(snapshot)
    assert result['reason'] == 'reconciliation_timeout'
    assert not result['reconciled'] and not result['ready']
    assert owner._reconciliation is pending and not pending.cancelled()
    assert not owner.lifecycle.snapshot().shutdown
    assert not owner.can_open(dict(admission='on', maintenance=False, **owner.control_identity()))
    # A result completing after budget expiry cannot heal the terminal fault.
    pending.set_result((SimpleNamespace(ready=True, persistence='disk'), now[0]))
    assert not (await owner.evidence(snapshot))['reconciled']


def test_reconciliation_budget_accounts_for_both_five_store_passes(tmp_path):
    from ostag.briefs.production_owner import BriefProductionOwner
    from ostag.briefs.startup_inventory import RuntimeInventoryConfig, ScanBudget
    config = RuntimeInventoryConfig(**{kind + '_budget': ScanBudget(1000, 5000)
                                      for kind in ('brief', 'card', 'retention', 'revision', 'callback')})
    h = host(tmp_path, inventory=config)
    assert BriefProductionOwner(h.settings, h.composition)._reconciliation_budget() == 50.5


def test_shutdown_during_reconciliation_retains_lock_until_actual_completion(tmp_path, monkeypatch):
    from threading import Event
    h = host(tmp_path)
    entered, release = Event(), Event()
    owner = None
    try:
        with TestClient(create_app(settings=h.settings, brief_composition=h.composition)) as client:
            owner = client.app.state.brief_owner
            original = owner._reconcile
            def held():
                entered.set()
                assert release.wait(8)
                return original()
            monkeypatch.setattr(owner, '_reconcile', held)
            client.portal.call(pause_owner, owner, 1)
            assert entered.wait(3)
        assert owner.reason == 'shutdown_pending' and not owner.ready
        assert owner._business.thread.is_alive() and owner._lock is not None
        assert not owner.status.last_status['drain_ack']
    finally:
        release.set()
        if owner is not None:
            owner._business.thread.join(3)
    assert not owner._business.thread.is_alive() and owner._lock is None


def test_pending_scan_invalidates_preceding_drain_proof(tmp_path, monkeypatch):
    from threading import Event
    h = host(tmp_path)
    release = Event()
    with TestClient(create_app(settings=h.settings, brief_composition=h.composition)) as client:
        owner = client.app.state.brief_owner
        client.portal.call(pause_owner, owner, 1)
        original = owner._reconcile
        def held():
            assert release.wait(5)
            return original()
        async def check(state, args):
            monkeypatch.setattr(owner, '_reconcile', held)
            assert validate_production_drain(owner, state, boot_id=owner.status.boot_id,
                generation=1, **owner.control_identity())
            pending = await owner.evidence(owner.lifecycle.snapshot())
            assert not pending['reconciled'] and owner._reconciliation is not None
            assert not validate_production_drain(owner, state, boot_id=owner.status.boot_id,
                generation=1, **owner.control_identity())
        try:
            client.portal.call(in_current_drain, owner, 1, check)
        finally:
            release.set()


def test_invalid_composition_leaves_monitor_available(tmp_path):
    h = host(tmp_path)
    with TestClient(create_app(settings=h.settings, brief_composition=object())) as client:
        assert client.get('/healthz').status_code == 200
        assert not client.app.state.brief_owner.ready
        assert list(h.composition.directory.iterdir()) == []


def test_legal_scan_budget_is_supported_during_startup(tmp_path, monkeypatch):
    import time
    from ostag.briefs.production_owner import BriefProductionOwner
    from ostag.briefs.startup_inventory import RuntimeInventoryConfig, ScanBudget
    config = RuntimeInventoryConfig(**{k + '_budget': ScanBudget(1000, 5000)
        for k in ('brief','card','retention','revision','callback')})
    h = host(tmp_path, inventory=config)
    original = BriefProductionOwner._reconcile
    def slow(self):
        time.sleep(5.1)
        return original(self)
    monkeypatch.setattr(BriefProductionOwner, '_reconcile', slow)
    with TestClient(create_app(settings=h.settings, brief_composition=h.composition)) as client:
        owner = client.app.state.brief_owner
        assert owner.ready, dict(reason=owner.reason, status_reason=owner.status.reason,
            status_health=owner.status.health,
            blockers=None if owner.report is None else owner.report.blockers)
        assert client.get('/healthz').status_code == 200


def test_drain_freshness_is_bound_to_scan_completion_not_only_publication(tmp_path, monkeypatch):
    import time
    import ostag.briefs.production_owner as module
    h = host(tmp_path)
    with TestClient(create_app(settings=h.settings, brief_composition=h.composition)) as client:
        owner = client.app.state.brief_owner
        client.portal.call(pause_owner, owner, 1)
        async def check(state, args):
            assert validate_production_drain(owner, state, boot_id=owner.status.boot_id,
                generation=1, **owner.control_identity())
            assert 0 <= time.time() - state['updated_at'] <= .75
            with monkeypatch.context() as patch:
                patch.setattr(module, 'monotonic', lambda: owner._reconciled_at + .751)
                assert not validate_production_drain(owner, state, boot_id=owner.status.boot_id,
                    generation=1, **owner.control_identity())
        client.portal.call(in_current_drain, owner, 1, check)


def test_monitor_serves_while_business_startup_scan_is_held(tmp_path, monkeypatch):
    from threading import Event
    from fastapi.testclient import TestClient as RawTestClient
    from ostag.briefs.production_owner import BriefProductionOwner
    import time
    h = host(tmp_path)
    entered, release = Event(), Event()
    original = BriefProductionOwner._reconcile
    def held(self):
        entered.set()
        assert release.wait(6)
        return original(self)
    monkeypatch.setattr(BriefProductionOwner, '_reconcile', held)
    try:
        started = time.monotonic()
        with RawTestClient(create_app(settings=h.settings, brief_composition=h.composition)) as client:
            assert time.monotonic() - started < 2
            owner = client.app.state.brief_owner
            assert entered.wait(1) and not owner.ready
            assert client.get('/healthz').status_code == 200
            assert not owner._startup_task.done()
            release.set()
            async def completed():
                await asyncio.wait_for(asyncio.shield(owner._startup_task), 3)
            client.portal.call(completed)
            assert owner.ready, (owner.reason, owner.status.reason)
    finally:
        release.set()


@pytest.mark.parametrize('boundary', ['scan', 'scan_cancel', 'worker_creation'])
def test_shutdown_during_business_startup_retains_and_reaps_owner(tmp_path, monkeypatch, boundary):
    from threading import Event
    import ostag.briefs.production_owner as module
    h = host(tmp_path)
    entered, release = Event(), Event()
    if boundary in ('scan', 'scan_cancel'):
        original = module.BriefProductionOwner._reconcile
        def held(self):
            entered.set()
            assert release.wait(8)
            return original(self)
        monkeypatch.setattr(module.BriefProductionOwner, '_reconcile', held)
    else:
        original = module.BriefWorker
        def held(*args, **kwargs):
            worker = original(*args, **kwargs)
            entered.set()
            assert release.wait(8)
            return worker
        monkeypatch.setattr(module, 'BriefWorker', held)
    owner = None
    try:
        with RawTestClient(create_app(settings=h.settings, brief_composition=h.composition)) as client:
            owner = client.app.state.brief_owner
            assert entered.wait(2)
            assert client.get('/healthz').status_code == 200
            assert not owner.ready
            if boundary == 'worker_creation':
                with pytest.raises(LifecycleDenied):
                    client.portal.call(pause_owner, owner, 1)
                assert not owner.lifecycle.snapshot().shutdown
            if boundary == 'scan_cancel':
                async def cancelled():
                    owner._startup_task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await owner._startup_task
                client.portal.call(cancelled)
        assert owner._lock is not None and owner._business.thread.is_alive()
        assert not owner.ready and owner.lifecycle.snapshot().shutdown
    finally:
        release.set()
        if owner is not None:
            owner._business.thread.join(3)
    assert not owner._business.thread.is_alive() and owner._lock is None
    if owner.worker is not None:
        assert not owner.worker._thread.is_alive() and not owner.worker._notifier.is_alive()


def test_atomic_drain_challenge_samples_under_publication_lock(tmp_path):
    h = host(tmp_path)
    with TestClient(create_app(settings=h.settings, brief_composition=h.composition)) as client:
        owner = client.app.state.brief_owner
        client.portal.call(pause_owner, owner, 1)
        args = dict(boot_id=owner.status.boot_id, generation=1, **owner.control_identity())
        async def check():
            async with asyncio.timeout(4):
                while not await owner.challenge_drain(**args):
                    await asyncio.sleep(.01)
            assert not await owner.challenge_drain(**(args | dict(boot_id='wrong-boot')))
            async with owner.control_lock:
                challenge = asyncio.create_task(owner.challenge_drain(**args))
                await asyncio.sleep(0)
                assert not challenge.done()
                # A change inside the lock must be visible when the challenge
                # acquires it; it cannot validate a pre-lock saved observation.
                owner.lifecycle._file_close(owner.status)
            assert not await challenge
        client.portal.call(check)
