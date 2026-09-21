"""A10 permanent local authority and privacy cleanup contracts."""
import sqlite3
import ctypes
import errno
import os
from pathlib import Path
import stat
import struct
import sys
from types import SimpleNamespace
import pytest
from ostag.briefs.retention import RetentionDenied, RetentionPolicy
from test_brief_revocation import configured
from test_brief_service import SCOPE, event
from test_brief_generation import at
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from threading import Barrier
import httpx
import ostag.briefs.retention as retention
from ostag.briefs.callbacks import BriefCallbackController, CallbackError, CallbackLedger
from ostag.briefs.card_delivery import CardDeliveryError, CardDeliveryService, CardLedger
from ostag.briefs.card_transport import CardTransport
from ostag.briefs.delivery import DeliveryError, DeliveryIntent, DeliveryService
from ostag.briefs.feishu_transport import CredentialSnapshot
from ostag.briefs.generation import PROMPT_TEMPLATE
from ostag.briefs.models import SourceSet
from ostag.briefs.rendering import PrivatePreview
from ostag.briefs.retention import RetentionCoordinator, RetentionLedger
from ostag.briefs.revisions import RevisionError, RevisionLedger, RevisionService
from ostag.briefs.service import ManualRequest, SourceSnapshot
from ostag.briefs.store import BriefStore, Conflict, PrivateDestination, RequesterContext, WorkerContext
from ostag.briefs.worker import BriefWorker, CALL_ID
from ostag.core.identity import Principal
from ostag.work_monitor.identity import Destination
from test_brief_card_delivery import ack, tokens
from test_brief_cards import BINDING, signed_app, signed_post
from test_brief_delivery_observation import Transport
from test_brief_service import request
from test_brief_validation import output




def test_exclusive_expiry_cannot_renew_or_roll_back(tmp_path):
    authority, service, coordinator = configured(tmp_path)
    try:
        job = service.submit(event()).job_id
        coordinator.register(SCOPE, job, policy=RetentionPolicy(at(10)))
        with pytest.raises(RetentionDenied):
            coordinator.register(SCOPE, job, policy=RetentionPolicy(at(20)))
        authority.now = 10
        assert coordinator.status(SCOPE, job).decision == 'expired'
        with pytest.raises(RetentionDenied):
            coordinator.guard(service.store).check(SCOPE, job)
        assert coordinator.cleanup(SCOPE, job).cleanup == 'complete'
        with sqlite3.connect(service.store.path) as db:
            assert 'Prepare a private draft' not in str(tuple(db.iterdump()))
        authority.now = 9
        with pytest.raises(RetentionDenied):
            coordinator.status(SCOPE, job)
    finally:
        service.close()




MARKER = 'SYNTHETICPRIVATEA10SENTINEL713928'


def full_stack(tmp_path):
    targets = dict(cards=CardLedger(tmp_path/'cards.db'),
                   callbacks=CallbackLedger(tmp_path/'callbacks.db'),
                   revisions=RevisionLedger(tmp_path/'revisions.db'))
    for target in targets.values():
        target.initialize()
    authority, briefs, c = configured(tmp_path, **targets)
    authority.destination = PrivateDestination(SCOPE.principal, 'oc_private')
    body = request()
    body['goals'] = MARKER
    for s in body['sources']:
        s['excerpt'] = MARKER
        s['content_sha256'] = hashlib.sha256(MARKER.encode()).hexdigest()
    authority.sources = {s.source_id: SourceSnapshot(SCOPE, s, at(100))
                         for s in ManualRequest.from_dict(body).sources}
    briefs.transport.response = replace(briefs.transport.response, body=json.dumps(output(MARKER)))
    submitted = briefs.submit(event(body))
    policy = RetentionPolicy(at(60))
    c.register(SCOPE, submitted.job_id, policy=policy)
    worker = BriefWorker(briefs)
    try:
        assert worker.run_once().result(3) == 'draft_ready'
    finally:
        worker.stop()
    c.register(SCOPE, submitted.job_id, policy=policy)
    box = briefs.store.outbox(SCOPE, submitted.job_id)
    p = PrivatePreview.from_outbox(SCOPE, box, BINDING,
        SourceSet.from_dict(json.loads(box.revision.provenance_json)['sources']))
    briefs.store.enable_delivery()
    text = DeliveryService(store=briefs.store, worker=WorkerContext('text-worker'),
        transport=Transport(), authorize=lambda i: None, clock=briefs.clock,
        retention=c.guard(briefs.store))
    credential = CredentialSnapshot(BINDING, 'synthetic-token', 'actual_sender', 'app_id',
                                    (('oc_private', SCOPE.principal),))
    sends = []
    def handler(r):
        sends.append(r)
        return httpx.Response(200, json=ack())
    card = CardDeliveryService(ledger=targets['cards'], namespace=SCOPE.namespace,
        load=lambda s, j: p, resolve_binding=lambda: BINDING, authorize=lambda p: None,
        transport=CardTransport(resolve=lambda: credential, transport=httpx.MockTransport(handler)),
        clock=lambda: 1000 + authority.now, retention=c.guard(targets['cards']))
    callback = BriefCallbackController(ledger=targets['callbacks'], namespace=SCOPE.namespace,
        load=lambda s, j: p, resolve_binding=lambda: BINDING, authorize=lambda p: None,
        clock=lambda: 1000 + authority.now, retention=c.guard(targets['callbacks']))
    revision = RevisionService(ledger=targets['revisions'], namespace=SCOPE.namespace,
        load=lambda s, j: p, authorize=lambda p: None, clock=lambda: 1000 + authority.now,
        retention=c.guard(targets['revisions']))
    return authority, briefs, c, p, text, card, callback, revision, sends


def sql_dump(path):
    with sqlite3.connect(path) as db:
        return '\n'.join(db.iterdump())


async def test_real_all_payload_copies_retired_reopen_no_resurrection(tmp_path):
    a, briefs, c, p, text, card, callback, revisions, sends = full_stack(tmp_path)
    try:
        intent = DeliveryIntent.from_outbox(SCOPE, briefs.store.outbox(SCOPE, p.job_id), BINDING)
        from ostag.briefs.delivery import SendResponse
        from test_brief_delivery_observation import MESSAGE_ID
        async def no_proof(attempt):
            return SendResponse(attempt.token, MESSAGE_ID, ())
        text.transport.send_effect = no_proof
        await text.prepare(intent)
        assert (await text.send(SCOPE, intent.delivery_id)).state == 'unknown'
        sent = await text.observe(SCOPE, intent.delivery_id)
        assert text.transport.reads == [MESSAGE_ID]
        assert sent.state == 'provider_accepted' and MARKER in sent.receipt_json
        card_status = await card.deliver(SCOPE, p.job_id, p.revision_id)
        frozen = card.ledger.frozen(SCOPE, card_status.delivery_id)
        refresh = tokens(json.loads(frozen.card_json))['Refresh']
        nav = card.handle_callback(refresh, principal=SCOPE.principal,
            destination=Destination('feishu', 'oc_private', 'private'), tenant_id='tenant',
            app_id='app', message_id='om_message_123')
        assert MARKER in json.dumps(nav)
        token = callback.issue(p, message_id='om_message_123')
        revisions.register(SCOPE, p.job_id)
        correction = revisions.correct(SCOPE, p.job_id, expected_revision_id=p.revision_id,
                                        request_id='correction', text=MARKER)
        revisions.adopt(SCOPE, p.job_id, expected_revision_id=correction.event.revision_id,
                        request_id='adopt')
        for target in (briefs.store, card.ledger, revisions.ledger):
            assert MARKER in sql_dump(target.path)
        # A second unrelated requester is deliberately in the SAME primary file.
        other = RequesterContext('synthetic', Principal('feishu', 'bob'))
        unrelated = briefs.store.admit(other, idempotency_key='other', payload={'private': 'BOB_KEEP'},
            grant_id='other-grant', grant_revision=1, destination=PrivateDestination(other.principal,
            'bob-dm'), now=at())
        c.revoke_job(SCOPE, p.job_id)
        result = c.cleanup(SCOPE, p.job_id)
        assert result.cleanup == 'complete'
        assert not any((result.content_available, result.content_verified, result.reverifiable,
                        result.human_received))
        assert result.remote_cards_may_persist
        for target in c.targets.values():
            assert MARKER not in sql_dump(target.path)
            assert MARKER.encode() not in target.path.read_bytes()
        assert PROMPT_TEMPLATE not in sql_dump(briefs.store.path)
        assert PROMPT_TEMPLATE.encode() not in briefs.store.path.read_bytes()
        assert briefs.store.get(other, unrelated.job_id).payload['private'] == 'BOB_KEEP'
        with pytest.raises(Conflict):
            briefs.store.outbox(SCOPE, p.job_id)
        with pytest.raises(DeliveryError):
            await text.prepare(intent)
        with pytest.raises(DeliveryError):
            await text.send(SCOPE, intent.delivery_id)
        with pytest.raises(CardDeliveryError):
            await card.prepare(SCOPE, p.job_id, p.revision_id)
        assert (await card.send(SCOPE, card_status.delivery_id)).cancelled
        assert len(sends) == 1
        with pytest.raises(CallbackError):
            callback.issue(p, message_id='om_message_123')
        with pytest.raises(RevisionError):
            revisions.correct(SCOPE, p.job_id, expected_revision_id=correction.event.revision_id,
                              request_id='new', text=MARKER)
        from fastapi.testclient import TestClient
        with TestClient(signed_app(tmp_path, callback)) as client:
            assert MARKER not in signed_post(client, token).text
        # Reopen every owned file; authority/progress and safe historical custody survive.
        reopened = RetentionCoordinator(ledger=RetentionLedger(c.ledger.path),
            store=BriefStore(briefs.store.path), cards=CardLedger(card.ledger.path),
            callbacks=CallbackLedger(callback.ledger.path), revisions=RevisionLedger(revisions.ledger.path),
            clock=briefs.clock)
        assert reopened.cleanup(SCOPE, p.job_id).cleanup == 'complete'
        with pytest.raises(RetentionDenied):
            reopened.register(SCOPE, p.job_id, policy=RetentionPolicy(at(60)))
        with pytest.raises(CardDeliveryError):
            reopened.targets['cards'].frozen(SCOPE, card_status.delivery_id)
        with sqlite3.connect(briefs.store.path) as db:
            row = db.execute('SELECT state,candidate_id FROM brief_deliveries').fetchone()
            assert row[0] == 'provider_accepted' and row[1]
            assert db.execute('SELECT count(*) FROM brief_delivery_observations').fetchone()[0] == 0
        assert briefs.store.cost_record(SCOPE, p.job_id, CALL_ID).state == 'settled'
    finally:
        briefs.close()


@pytest.mark.parametrize('stage', ['primary', 'callbacks', 'cards', 'revisions'])
async def test_cleanup_crash_after_each_target_before_receipt_retries(tmp_path, stage, monkeypatch):
    _, briefs, c, p, _, card, callback, revisions, _ = full_stack(tmp_path)
    try:
        await card.prepare(SCOPE, p.job_id, p.revision_id)
        callback.issue(p, message_id='om_message_123')
        revisions.register(SCOPE, p.job_id)
        c.revoke_job(SCOPE, p.job_id)
        original = c._receipt
        def crash(scope, job, kind, identity):
            if kind == stage:
                raise OSError(MARKER)
            return original(scope, job, kind, identity)
        monkeypatch.setattr(c, '_receipt', crash)
        first = c.cleanup(SCOPE, p.job_id)
        assert first.cleanup != 'complete'
        assert MARKER not in repr(first)
        monkeypatch.setattr(c, '_receipt', original)
        assert c.cleanup(SCOPE, p.job_id).cleanup == 'complete'
        assert c.cleanup(SCOPE, p.job_id).cleanup == 'complete'
    finally:
        briefs.close()


@pytest.mark.parametrize('change', ['missing', 'omitted', 'replacement', 'future', 'busy'])
async def test_required_target_cannot_be_omitted_or_swapped(tmp_path, change):
    _, briefs, c, p, _, card, _, _, _ = full_stack(tmp_path)
    holder = None
    try:
        await card.prepare(SCOPE, p.job_id, p.revision_id)
        c.revoke_job(SCOPE, p.job_id)
        original = card.ledger.path
        if change == 'missing':
            original.rename(tmp_path/'saved.db')
        elif change == 'omitted':
            c.targets.pop('cards')
        elif change == 'replacement':
            original.rename(tmp_path/'saved.db')
            replacement = CardLedger(original)
            replacement.initialize()
        elif change == 'future':
            with sqlite3.connect(original) as db:
                db.execute('PRAGMA user_version=999')
        else:
            holder = sqlite3.connect(original)
            holder.execute('BEGIN EXCLUSIVE')
        before = original.read_bytes() if original.exists() else None
        result = c.cleanup(SCOPE, p.job_id)
        assert result.cleanup in ('pending', 'partial') and result.pending_targets > 0
        assert (original.read_bytes() if original.exists() else None) == before
    finally:
        if holder:
            holder.rollback()
            holder.close()
        briefs.close()


@pytest.mark.parametrize('value', [True, False, 1.5, None, {}, [], -1, 10**17,
                                  'yesterday', '2026-09-13T00:00:00+01:00'])
def test_policy_strict_types_sanitized_errors(value):
    with pytest.raises(RetentionDenied) as caught:
        RetentionPolicy(value)
    assert caught.value.__context__ is caught.value.__cause__ is None
    assert str(caught.value) == 'brief retention unavailable'


@pytest.mark.parametrize('kind', ['foreign', 'future', 'trigger', 'wal'])
def test_authority_foreign_future_schema_never_mutated(tmp_path, kind):
    path = tmp_path/'authority.db'
    ledger = RetentionLedger(path)
    if kind == 'foreign':
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE secret (body TEXT)')
            db.execute('INSERT INTO secret VALUES(?)', (MARKER,))
    else:
        ledger.initialize()
        with sqlite3.connect(path) as db:
            if kind == 'future':
                db.execute('PRAGMA user_version=99')
            elif kind == 'wal':
                db.execute('PRAGMA journal_mode=WAL')
            else:
                db.execute('CREATE TRIGGER foreign_trigger AFTER INSERT ON retention_revokes '
                           'BEGIN DELETE FROM retention_revokes; END')
    before = path.read_bytes()
    with pytest.raises(RetentionDenied):
        ledger.initialize()
    assert path.read_bytes() == before


def test_source_revoke_stable_cache_aliases_scope_and_admin(tmp_path):
    a, service, c = configured(tmp_path)
    try:
        job = service.submit(event()).job_id
        c.register(SCOPE, job, policy=RetentionPolicy(at(60)))
        other = RequesterContext(SCOPE.namespace, Principal('feishu', 'bob'))
        with pytest.raises(RetentionDenied):
            c.revoke_source(SCOPE, SCOPE, 'r0')
        def maintenance(actor, owner, operation):
            if actor != SCOPE or owner != SCOPE:
                raise PermissionError(MARKER)
        c.maintenance = maintenance
        with pytest.raises(RetentionDenied):
            c.cleanup_as(other, SCOPE, job)
        with pytest.raises(RetentionDenied):
            c.revoke_source(other, SCOPE, 'r0')
        with pytest.raises(RetentionDenied):
            c.status(other, job)
        assert c.revoke_source(SCOPE, SCOPE, 'r0') == 1
        assert c.revoke_source(SCOPE, SCOPE, 'r0') == 1
        assert c.status(SCOPE, job).decision == 'revoked'
        body = request()
        for source in body['sources']:
            source['source_id'] += '_new_cache_alias'
        a.sources = {s.source_id: SourceSnapshot(SCOPE, s, at(100))
                     for s in ManualRequest.from_dict(body).sources}
        assert service.submit(event(body, event_id='fresh-event')).status == 'denied'
        assert c.cleanup(SCOPE, job).cleanup == 'complete'
        # New versions of a permanently revoked resource are still denied.
        body['sources'][0]['resource_version'] = 'new-version'
        a.sources = {s.source_id: SourceSnapshot(SCOPE, s, at(100))
                     for s in ManualRequest.from_dict(body).sources}
        assert service.submit(event(body, event_id='another-event')).status == 'denied'
    finally:
        service.close()


def test_snapshot_expiry_blocks_fresh_intake_but_allows_new_snapshot(tmp_path):
    a, service, c = configured(tmp_path)
    try:
        job = service.submit(event()).job_id
        c.register(SCOPE, job, policy=RetentionPolicy(at(10)))
        a.now = 10
        body = request()
        for source in body['sources']:
            source['source_id'] += '_cache'
            source['observed_at'] = at(10)
        a.sources = {s.source_id: SourceSnapshot(SCOPE, s, at(100))
                     for s in ManualRequest.from_dict(body).sources}
        assert service.submit(event(body, event_id='cache-new-event')).status == 'denied'
        for source in body['sources']:
            source['resource_version'] = 'genuinely-new-version'
        a.sources = {s.source_id: SourceSnapshot(SCOPE, s, at(100))
                     for s in ManualRequest.from_dict(body).sources}
        new = service.submit(event(body, event_id='new-version-event'))
        assert new.status == 'queued'
        c.register(SCOPE, new.job_id, policy=RetentionPolicy(at(50)))
        assert c.status(SCOPE, new.job_id).decision == 'authorized'
    finally:
        service.close()


def test_capacity_does_not_recycle_permanent_tombstones(tmp_path):
    a, service, c = configured(tmp_path)
    try:
        tiny = RetentionLedger(tmp_path/'tiny.db', capacity=6)
        tiny.initialize()
        c = RetentionCoordinator(ledger=tiny, store=service.store, clock=service.clock)
        job = service.submit(event()).job_id
        c.register(SCOPE, job, policy=RetentionPolicy(at(60)))
        c.revoke_job(SCOPE, job)
        c.cleanup(SCOPE, job)
        second = service.submit(event(event_id='event2')).job_id
        with pytest.raises(RetentionDenied):
            c.register(SCOPE, second, policy=RetentionPolicy(at(60)))
        assert c.status(SCOPE, job).decision == 'revoked'
        assert c.status(SCOPE, job).cleanup == 'complete'
    finally:
        service.close()


def test_multi_connection_registration_and_revoke_never_renew(tmp_path):
    _, service, c = configured(tmp_path)
    try:
        job = service.submit(event()).job_id
        barrier = Barrier(2)
        def register(expiry):
            peer = RetentionCoordinator(ledger=RetentionLedger(c.ledger.path),
                store=BriefStore(service.store.path), clock=service.clock)
            barrier.wait()
            try:
                peer.register(SCOPE, job, policy=RetentionPolicy(at(expiry)))
                return expiry
            except RetentionDenied:
                return None
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(register, [30, 60]))
        assert sum(r is not None for r in results) == 1
        expiry = next(r for r in results if r is not None)
        c.register(SCOPE, job, policy=RetentionPolicy(at(expiry)))
        c.revoke_job(SCOPE, job)
        with pytest.raises(RetentionDenied):
            c.register(SCOPE, job, policy=RetentionPolicy(at(expiry)))
    finally:
        service.close()


def test_lost_tombstone_commit_ack_stays_revoked(tmp_path, monkeypatch):
    from contextlib import contextmanager
    _, service, c = configured(tmp_path)
    try:
        job = service.submit(event()).job_id
        c.register(SCOPE, job, policy=RetentionPolicy(at(60)))
        original = c.ledger.transaction
        @contextmanager
        def lost_ack(clock):
            with original(clock) as db:
                yield db
            raise OSError(MARKER)
        monkeypatch.setattr(c.ledger, 'transaction', lost_ack)
        with pytest.raises(RetentionDenied) as caught:
            c.revoke_job(SCOPE, job)
        assert caught.value.__context__ is None
        monkeypatch.setattr(c.ledger, 'transaction', original)
        peer = RetentionCoordinator(ledger=RetentionLedger(c.ledger.path),
            store=BriefStore(service.store.path), clock=service.clock)
        assert peer.status(SCOPE, job).decision == 'revoked'
        with pytest.raises(RetentionDenied):
            peer.guard(peer.store).check(SCOPE, job)
        assert peer.cleanup(SCOPE, job).cleanup == 'complete'
    finally:
        service.close()


def test_retired_unknown_spend_can_settle_without_restoring_prompt(tmp_path):
    from ostag.briefs.store import CostProfile, Usage
    _, service, c = configured(tmp_path)
    try:
        job_id = service.submit(event()).job_id
        c.register(SCOPE, job_id, policy=RetentionPolicy(at(60)))
        worker = WorkerContext('numeric-settlement')
        job = service.store.claim_one(worker, clock=service.clock)
        service.store.start_generating(job.lease, clock=service.clock)
        profile = CostProfile(json.dumps({'prompt_template': MARKER}, separators=(',', ':'),
                                        sort_keys=True), 'a'*64, 100, 1, 100)
        cost = service.store.reserve_cost(job.lease, call_id='unknown', profile=profile, config_hash='b'*64,
            daily_limit_micros=1000, clock=service.clock)
        service.store.start_cost(job.lease, cost.reservation_id, daily_limit_micros=1000,
                                 clock=service.clock)
        service.store.mark_cost_unknown(worker, cost.reservation_id, 'transport_error',
                                        clock=service.clock)
        c.revoke_job(SCOPE, job_id)
        assert c.cleanup(SCOPE, job_id).cleanup == 'complete'
        retained = service.store.cost_record(SCOPE, job_id, 'unknown')
        assert retained.state == 'unknown' and retained.charged_micros == 100
        settled = service.store.settle_cost(worker, cost.reservation_id, Usage(2, 3, 6),
                                            clock=service.clock)
        assert settled.state == 'settled' and settled.charged_micros == 6
        assert MARKER not in sql_dump(service.store.path)
        assert MARKER.encode() not in service.store.path.read_bytes()
    finally:
        service.close()


def test_authority_gate_uses_clock_after_actual_retention_lock_wait(tmp_path, monkeypatch):
    from contextlib import contextmanager
    from threading import Event
    a, service, c = configured(tmp_path)
    try:
        job = service.submit(event()).job_id
        c.register(SCOPE, job, policy=RetentionPolicy(at(10)))
        attempting = Event()
        original = c.ledger._connection
        @contextmanager
        def traced(**kwargs):
            with original(**kwargs) as db:
                db.set_trace_callback(lambda sql: attempting.set() if sql == 'BEGIN IMMEDIATE' else None)
                yield db
        monkeypatch.setattr(c.ledger, '_connection', traced)
        def check():
            with pytest.raises(RetentionDenied):
                c.guard(service.store).check(SCOPE, job)
        with sqlite3.connect(c.ledger.path) as holder, ThreadPoolExecutor(1) as pool:
            holder.execute('BEGIN IMMEDIATE')
            future = pool.submit(check)
            assert attempting.wait(1)
            a.now = 10
            holder.rollback()
            future.result(1)
        assert c.status(SCOPE, job).decision == 'expired'
    finally:
        service.close()


def test_concurrent_cleanup_keeps_unrelated_primary_payload(tmp_path):
    _, service, c = configured(tmp_path)
    try:
        job = service.submit(event()).job_id
        c.register(SCOPE, job, policy=RetentionPolicy(at(60)))
        other = service.submit(event(event_id='unrelated')).job_id
        before = service.store.get(SCOPE, other)
        c.revoke_job(SCOPE, job)
        barrier = Barrier(2)
        def cleanup(_):
            peer = RetentionCoordinator(ledger=RetentionLedger(c.ledger.path),
                store=BriefStore(service.store.path), clock=service.clock)
            barrier.wait()
            return peer.cleanup(SCOPE, job)
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(cleanup, range(2)))
        assert all(r.decision == 'revoked' for r in results)
        assert c.cleanup(SCOPE, job).cleanup == 'complete'
        assert service.store.get(SCOPE, other) == before
    finally:
        service.close()


def test_replaced_shared_authority_does_not_reauthorize_old_handles(tmp_path):
    _, service, c = configured(tmp_path)
    try:
        job = service.submit(event()).job_id
        c.register(SCOPE, job, policy=RetentionPolicy(at(60)))
        c.ledger.path.rename(tmp_path/'original-authority.db')
        replacement = RetentionLedger(c.ledger.path)
        replacement.initialize()
        with pytest.raises(RetentionDenied):
            c.guard(service.store).check(SCOPE, job)
        with pytest.raises(RetentionDenied):
            c.register(SCOPE, job, policy=RetentionPolicy(at(90)))
        assert service.submit(event(event_id='new')).status == 'denied'
    finally:
        service.close()


def test_earlier_shared_snapshot_expiry_after_guard_commit_is_permanent(tmp_path, monkeypatch):
    from contextlib import contextmanager
    a, service, c = configured(tmp_path)
    try:
        first = service.submit(event()).job_id
        second = service.submit(event(event_id='second')).job_id
        c.register(SCOPE, first, policy=RetentionPolicy(at(10)))
        c.register(SCOPE, second, policy=RetentionPolicy(at(50)))
        a.now = 9
        original = c.ledger.transaction
        @contextmanager
        def advance_after_commit(clock):
            with original(clock) as db:
                yield db
            a.now = 10
        monkeypatch.setattr(c.ledger, 'transaction', advance_after_commit)
        with pytest.raises(RetentionDenied):
            c.guard(service.store).check(SCOPE, second)
        monkeypatch.setattr(c.ledger, 'transaction', original)
        assert c.status(SCOPE, second).decision == 'expired'
        a.now = 9
        with pytest.raises(RetentionDenied):
            c.guard(service.store).check(SCOPE, second)
    finally:
        service.close()


async def test_other_requester_survives_in_every_owned_ledger(tmp_path):
    from test_brief_service import Authority
    _, briefs, c, p, _, card, callback, revisions, _ = full_stack(tmp_path)
    bob = RequesterContext(SCOPE.namespace, Principal('feishu', 'bob'))
    authority = Authority()
    authority.destination = PrivateDestination(bob.principal, 'oc_bob')
    authority.grant = replace(authority.grant, principal=bob.principal,
                              allowed_audience=(bob.principal,))
    body = request()
    body['goals'] = 'BOBKEEP'
    for source in body['sources']:
        source['allowed_audience'] = [bob.principal.to_dict()]
    authority.sources = {s.source_id: SourceSnapshot(bob, s, at(100))
                         for s in ManualRequest.from_dict(body).sources}
    other = authority.service(briefs.store, retention=c.guard(briefs.store))
    other.transport.response = replace(other.transport.response, body=json.dumps(output('BOBKEEP')))
    try:
        submitted = other.submit(event(body, sender_id='bob', event_id='bob-event'))
        assert submitted.status == 'queued'
        c.register(bob, submitted.job_id, policy=RetentionPolicy(at(60)))
        worker = BriefWorker(other)
        try:
            assert worker.run_once().result(3) == 'draft_ready'
        finally:
            worker.stop()
        c.register(bob, submitted.job_id, policy=RetentionPolicy(at(60)))
        box = briefs.store.outbox(bob, submitted.job_id)
        second = PrivatePreview.from_outbox(bob, box, BINDING,
            SourceSet.from_dict(json.loads(box.revision.provenance_json)['sources']))
        card2 = CardDeliveryService(ledger=card.ledger, namespace=SCOPE.namespace,
            load=lambda s, j: second, resolve_binding=lambda: BINDING, authorize=lambda p: None,
            transport=card.transport, clock=lambda: 1000, retention=c.guard(card.ledger))
        callback2 = BriefCallbackController(ledger=callback.ledger, namespace=SCOPE.namespace,
            load=lambda s, j: second, resolve_binding=lambda: BINDING, authorize=lambda p: None,
            clock=lambda: 1000, retention=c.guard(callback.ledger))
        revision2 = RevisionService(ledger=revisions.ledger, namespace=SCOPE.namespace,
            load=lambda s, j: second, authorize=lambda p: None, clock=lambda: 1000,
            retention=c.guard(revisions.ledger))
        await card.prepare(SCOPE, p.job_id, p.revision_id)
        prepared2 = await card2.prepare(bob, second.job_id, second.revision_id)
        frozen2 = card.ledger.frozen(bob, prepared2.delivery_id)
        callback.issue(p, message_id='om_message_123')
        token2 = callback2.issue(second, message_id='om_bob_message')
        revisions.register(SCOPE, p.job_id)
        revision2.register(bob, second.job_id)
        revision2.correct(bob, second.job_id, expected_revision_id=second.revision_id,
                          request_id='bob-correction', text='BOBKEEP')
        c.revoke_job(SCOPE, p.job_id)
        assert c.cleanup(SCOPE, p.job_id).cleanup == 'complete'
        assert card.ledger.frozen(bob, prepared2.delivery_id) == frozen2
        assert revision2.read(bob, second.job_id).corrections[0].text == 'BOBKEEP'
        assert callback.ledger.lookup(token2, clock=lambda: 1000)
        assert 'BOBKEEP' in briefs.store.outbox(bob, second.job_id).revision.payload_json
        assert c.status(bob, second.job_id).decision == 'authorized'
        for target in (briefs.store, card.ledger, revisions.ledger):
            assert MARKER not in sql_dump(target.path)
            assert 'BOBKEEP' in sql_dump(target.path)
    finally:
        other.close()
        briefs.close()


def test_grant_revoke_is_permanent_and_requires_maintenance_scope(tmp_path):
    _, service, c = configured(tmp_path)
    try:
        job = service.submit(event()).job_id
        c.register(SCOPE, job, policy=RetentionPolicy(at(60)))
        with pytest.raises(RetentionDenied):
            c.revoke_grant(SCOPE, SCOPE, 'grant')
        c.maintenance = lambda actor, owner, operation: None if actor == owner == SCOPE else False
        assert c.revoke_grant(SCOPE, SCOPE, 'grant') == 1
        assert c.revoke_grant(SCOPE, SCOPE, 'grant') == 1
        assert service.submit(event(event_id='new-grant-event')).status == 'denied'
        assert c.status(SCOPE, job).decision == 'revoked'
    finally:
        service.close()


@pytest.mark.parametrize('table,column', [
    ('brief_jobs', 'payload_json'), ('brief_jobs', 'state'), ('brief_jobs', 'fence'),
    ('brief_outbox', 'state'), ('brief_revisions', 'payload_json'),
    ('brief_revisions', 'provenance_json'), ('brief_cost_profiles', 'profile_json'),
    ('brief_deliveries', 'intent_json'), ('brief_deliveries', 'receipt_json'),
    ('brief_delivery_observations', None), ('brief_callbacks', 'binding'),
    ('brief_callbacks', 'spent'), ('cards', 'preview_json'), ('cards', 'card_json'),
    ('cards', 'wire_body'), ('cards', 'response_card'), ('cards', 'cancelled'),
    ('card_tokens', None), ('revision_roots', 'snapshot'), ('revision_events', 'record'),
])
async def test_cleanup_verifies_every_payload_and_fence_write(tmp_path, monkeypatch, table, column):
    """Real A04/U01/card/A06 data; SQLite silently ignores one retirement column."""
    from contextlib import contextmanager
    import ostag.briefs.retention as retention
    from ostag.briefs.delivery import SendResponse
    from test_brief_delivery_observation import MESSAGE_ID
    _, briefs, c, p, text, card, callback, revisions, _ = full_stack(tmp_path)
    try:
        intent = DeliveryIntent.from_outbox(SCOPE, briefs.store.outbox(SCOPE, p.job_id), BINDING)
        async def no_proof(attempt):
            return SendResponse(attempt.token, MESSAGE_ID, ())
        text.transport.send_effect = no_proof
        await text.prepare(intent)
        await text.send(SCOPE, intent.delivery_id)
        await text.observe(SCOPE, intent.delivery_id)
        sent = await card.deliver(SCOPE, p.job_id, p.revision_id)
        frozen = card.ledger.frozen(SCOPE, sent.delivery_id)
        card.handle_callback(tokens(json.loads(frozen.card_json))['Refresh'],
            principal=SCOPE.principal, destination=Destination('feishu', 'oc_private', 'private'),
            tenant_id='tenant', app_id='app', message_id='om_message_123')
        callback.issue(p, message_id='om_message_123')
        if table == 'brief_callbacks' and column == 'binding':
            # An already spent capability still has a private binding to erase.
            with sqlite3.connect(callback.ledger.path) as db:
                db.execute('UPDATE brief_callbacks SET spent=1')
        revisions.register(SCOPE, p.job_id)
        revisions.correct(SCOPE, p.job_id, expected_revision_id=p.revision_id,
                          request_id='private-correction', text=MARKER)
        original = retention.owned_retirement
        ignored = []
        class DeleteFault:
            def __init__(self, db):
                self.db = db

            def execute(self, sql, args=()):
                if column is None and sql.startswith('DELETE FROM ' + table + ' '):
                    ignored.append(True)
                    return self.db.execute('SELECT 1 WHERE 0')
                return self.db.execute(sql, args)

        @contextmanager
        def faulty(target, **kwargs):
            with original(target, **kwargs) as db:
                def authorize(action, name, field, *_):
                    if action == sqlite3.SQLITE_UPDATE and (name, field) == (table, column):
                        ignored.append(True)
                        return sqlite3.SQLITE_IGNORE
                    return sqlite3.SQLITE_OK
                db.set_authorizer(authorize)
                yield DeleteFault(db)
        monkeypatch.setattr(retention, 'owned_retirement', faulty)
        c.revoke_job(SCOPE, p.job_id)
        result = c.cleanup(SCOPE, p.job_id)
        assert ignored, 'fault must reach a populated real ledger'
        assert result.cleanup != 'complete'
        monkeypatch.setattr(retention, 'owned_retirement', original)
        assert c.cleanup(SCOPE, p.job_id).cleanup == 'complete'
        for target in c.targets.values():
            assert MARKER not in sql_dump(target.path)
            assert MARKER.encode() not in target.path.read_bytes()
        assert PROMPT_TEMPLATE not in sql_dump(briefs.store.path)
    finally:
        briefs.close()


@pytest.mark.parametrize('operation', ['check', 'intake', 'register', 'status'])
def test_shared_snapshot_deadline_after_last_sql_release(tmp_path, monkeypatch, operation):
    """Post-SQL processing time, deliberately not described as a lock wait."""
    from contextlib import contextmanager
    a, service, c = configured(tmp_path)
    try:
        first = service.submit(event()).job_id
        second = service.submit(event(event_id='second')).job_id
        c.register(SCOPE, first, policy=RetentionPolicy(at(10)))
        c.register(SCOPE, second, policy=RetentionPolicy(at(60)))
        a.now = 9
        original = c.ledger.transaction
        # Registration returns status, so cross the boundary in its LAST transaction.
        remaining = 2 if operation == 'register' else 1
        @contextmanager
        def advance(clock):
            nonlocal remaining
            with original(clock) as db:
                yield db
            remaining -= 1
            if remaining == 0:
                a.now = 10
        monkeypatch.setattr(c.ledger, 'transaction', advance)
        if operation == 'status':
            assert c.status(SCOPE, second).decision == 'expired'
        else:
            with pytest.raises(RetentionDenied):
                if operation == 'check':
                    c.guard(service.store).check(SCOPE, second)
                elif operation == 'intake':
                    c.guard(service.store).intake(SCOPE, ManualRequest.from_dict(request()).sources)
                else:
                    c.register(SCOPE, second, policy=RetentionPolicy(at(60)))
        monkeypatch.setattr(c.ledger, 'transaction', original)
        assert c.status(SCOPE, second).decision == 'expired'
    finally:
        service.close()


@pytest.mark.parametrize('stage', ['reset', 'receipt', 'before_commit', 'lost_ack'])
def test_cleanup_progress_is_verified_and_replayed(tmp_path, monkeypatch, stage):
    from contextlib import contextmanager
    _, briefs, c, p, _, _, _, _, _ = full_stack(tmp_path)
    try:
        c.revoke_job(SCOPE, p.job_id)
        if stage == 'reset':
            assert c.cleanup(SCOPE, p.job_id).cleanup == 'complete'
        original = c.ledger.transaction
        faults = []
        @contextmanager
        def faulty(clock):
            writes = []
            with original(clock) as db:
                def authorize(action, name, field, *_):
                    if (action, name, field) == (sqlite3.SQLITE_UPDATE, 'retention_targets', 'done'):
                        writes.append(True)
                        faults.append(True)
                        if stage in ('reset', 'receipt'):
                            return sqlite3.SQLITE_IGNORE
                    return sqlite3.SQLITE_OK
                db.set_authorizer(authorize)
                yield db
                if writes and stage == 'before_commit':
                    raise OSError(MARKER)
            if writes and stage == 'lost_ack':
                raise OSError(MARKER)
        monkeypatch.setattr(c.ledger, 'transaction', faulty)
        if stage == 'reset':
            # If reset is silently ignored, old all-done receipts must not be
            # returned as evidence that this cleanup operation succeeded.
            with pytest.raises(RetentionDenied):
                c.cleanup(SCOPE, p.job_id)
        else:
            try:
                result = c.cleanup(SCOPE, p.job_id)
                assert result.cleanup != 'complete'
            except RetentionDenied as exc:
                assert exc.__context__ is None
        assert faults
        monkeypatch.setattr(c.ledger, 'transaction', original)
        assert c.cleanup(SCOPE, p.job_id).cleanup == 'complete'
    finally:
        briefs.close()


def _hide_python_birthtime(monkeypatch):
    original = Path.stat
    def without_birthtime(path, **kwargs):
        sample = original(path, **kwargs)
        return SimpleNamespace(**{name: getattr(sample, name) for name in dir(sample)
            if name.startswith('st_') and name not in ('st_birthtime', 'st_birthtime_ns')})
    monkeypatch.setattr(Path, 'stat', without_birthtime)


@pytest.fixture
def mocked_linux_statx(monkeypatch):
    """ABI simulation only; this fixture is NOT real Linux acceptance on macOS."""
    _hide_python_birthtime(monkeypatch)
    monkeypatch.setattr(retention, 'sys', SimpleNamespace(platform='linux'), raising=False)
    # macOS lacks O_PATH. Only this simulated call uses a normal local read fd.
    local_os = SimpleNamespace(**vars(os))
    local_os.O_PATH = getattr(os, 'O_PATH', os.O_RDONLY)
    monkeypatch.setattr(retention, 'os', local_os, raising=False)
    state = dict(mask=0x901, seconds=1700000000, nanos=123456789,
                 inode_delta=0, major_delta=0, minor_delta=0, mode=stat.S_IFREG,
                 error=0, after=None, calls=[], missing_symbol=False)
    def statx(fd, name, flags, mask, result):
        assert name == b'' and flags == 0x1100 and mask == 0x901
        state['calls'].append(fd)
        sample = os.fstat(fd)
        if state['error']:
            ctypes.set_errno(state['error'])
            return -1
        # Independent official UAPI byte offsets, not the implementation's fields.
        wire = bytearray(256)
        struct.pack_into('=I', wire, 0x00, state['mask'])
        struct.pack_into('=H', wire, 0x1c, state['mode'])
        struct.pack_into('=Q', wire, 0x20, sample.st_ino + state['inode_delta'])
        struct.pack_into('=qI', wire, 0x50, state['seconds'], state['nanos'])
        struct.pack_into('=II', wire, 0x88, os.major(sample.st_dev) + state['major_delta'],
                         os.minor(sample.st_dev) + state['minor_delta'])
        ctypes.memmove(result, bytes(wire), len(wire))
        if state['after']:
            state['after']()
        return 0
    def library(name):
        assert name is None
        return SimpleNamespace() if state['missing_symbol'] else SimpleNamespace(statx=statx)
    monkeypatch.setattr(ctypes, 'CDLL', library)
    return state


def test_linux_missing_python_birthtime_uses_trusted_statx(tmp_path, mocked_linux_statx):
    path = tmp_path/'owned.db'
    path.touch()
    ledger = RetentionLedger(path)
    ledger.initialize()
    assert RetentionLedger(path)._pinned == ledger._pinned
    assert mocked_linux_statx['calls']
    # The exact nanosecond birth time participates, even for a recycled inode.
    mocked_linux_statx['nanos'] += 1
    with pytest.raises(RetentionDenied):
        ledger.initialize()
    for fd in set(mocked_linux_statx['calls']):
        with pytest.raises(OSError):
            os.fstat(fd)


@pytest.mark.parametrize(('field', 'value'), [
    ('mask', 0x101), ('mask', 0x801), ('mask', 0x900),
    ('inode_delta', 1), ('major_delta', 1), ('minor_delta', 1),
    ('mode', stat.S_IFLNK), ('mode', stat.S_IFDIR),
    ('nanos', 1000000000), ('nanos', 0xffffffff),
    ('error', errno.ENOSYS), ('error', errno.EOPNOTSUPP), ('error', errno.EACCES),
    ('error', errno.EIO), ('missing_symbol', True),
])
def test_linux_statx_untrustworthy_identity_denied(tmp_path, mocked_linux_statx, field, value):
    path = tmp_path/'SENSITIVE-IDENTITY.db'
    path.touch()
    mocked_linux_statx[field] = value
    with pytest.raises(RetentionDenied) as caught:
        RetentionLedger(path)
    assert str(caught.value) == 'brief retention unavailable'
    assert caught.value.__context__ is None and caught.value.__cause__ is None
    assert len(mocked_linux_statx['calls']) == (0 if field == 'missing_symbol' else 1)
    for fd in set(mocked_linux_statx['calls']):
        with pytest.raises(OSError):
            os.fstat(fd)


@pytest.mark.parametrize(('seconds', 'nanos'), [(-1, 999999999), (0, 0), (1, 0)])
def test_linux_statx_signed_seconds_and_valid_nanoseconds(
        tmp_path, mocked_linux_statx, seconds, nanos):
    path = tmp_path/'owned.db'
    path.touch()
    mocked_linux_statx.update(seconds=seconds, nanos=nanos, mask=0xfff)
    sample = path.stat()
    assert retention._identity(RetentionLedger(path)) == retention._digest([
        sample.st_dev, sample.st_ino, seconds * 1000000000 + nanos])


def test_linux_statx_replacement_during_sample_denied(tmp_path, mocked_linux_statx):
    path = tmp_path/'owned.db'
    path.touch()
    def replace_during_sample():
        path.rename(tmp_path/'old.db')
        path.touch()
    mocked_linux_statx['after'] = replace_during_sample
    with pytest.raises(RetentionDenied):
        RetentionLedger(path)


def test_missing_birthtime_on_unsupported_platform_denied(tmp_path, monkeypatch):
    path = tmp_path/'owned.db'
    path.touch()
    _hide_python_birthtime(monkeypatch)
    monkeypatch.setattr(retention, 'sys', SimpleNamespace(platform='unsupported'), raising=False)
    with pytest.raises(RetentionDenied):
        RetentionLedger(path)


@pytest.mark.parametrize('kind', ['missing', 'directory', 'symlink', 'fifo'])
def test_identity_requires_existing_regular_file(tmp_path, kind):
    path = tmp_path/'SENSITIVE-IDENTITY.db'
    if kind == 'directory':
        path.mkdir()
    elif kind == 'symlink':
        other = tmp_path/'other.db'
        other.touch()
        path.symlink_to(other)
    elif kind == 'fifo':
        os.mkfifo(path)
    target = RetentionLedger.__new__(RetentionLedger)
    target.path = path
    with pytest.raises(RetentionDenied) as caught:
        retention._identity(target)
    assert caught.value.__context__ is None
    assert str(caught.value) == 'brief retention unavailable'


@pytest.mark.parametrize('mock_linux', [False, True], ids=['native', 'mocked-linux-ABI'])
def test_identity_reopen_updates_vacuum_and_restore(tmp_path, monkeypatch, request, mock_linux):
    """The native case MUST use real libc/kernel statx on Linux, never skip it."""
    if mock_linux:
        request.getfixturevalue('mocked_linux_statx')
    elif sys.platform == 'linux':
        _hide_python_birthtime(monkeypatch)
    path = tmp_path/'owned.db'
    ledger = RetentionLedger(path)
    ledger.initialize()
    pinned = ledger._pinned
    before = path.read_bytes()
    with sqlite3.connect(path) as db:
        db.execute('UPDATE retention_meta SET highwater=1')
        db.commit()
        db.execute('VACUUM')
    later = path.stat().st_mtime_ns + 10000000000
    os.utime(path, ns=(later, later))
    assert retention._identity(ledger) == pinned
    assert RetentionLedger(path)._pinned == pinned
    ledger.initialize()
    # Restoring identical original bytes into another file cannot restore identity.
    path.rename(tmp_path/'original.db')
    path.write_bytes(before)
    assert retention._identity(RetentionLedger(path)) != pinned
    with pytest.raises(RetentionDenied):
        ledger.initialize()
    path.unlink()
    with pytest.raises(RetentionDenied):
        ledger.initialize()


def test_linux_statx_abi_layout():
    assert ctypes.sizeof(retention._StatxTimestamp) == 16
    assert ctypes.sizeof(retention._Statx) == 256
    expected = dict(mask=0x00, blksize=0x04, attributes=0x08, nlink=0x10,
        uid=0x14, gid=0x18, mode=0x1c, spare0=0x1e, ino=0x20, size=0x28,
        blocks=0x30, attributes_mask=0x38, atime=0x40, btime=0x50, ctime=0x60,
        mtime=0x70, rdev_major=0x80, rdev_minor=0x84, dev_major=0x88,
        dev_minor=0x8c, tail=0x90)
    assert {name: getattr(retention._Statx, name).offset
            for name, _ in retention._Statx._fields_} == expected


def test_linux_statx_opened_file_must_match_initial_sample(
        tmp_path, monkeypatch, mocked_linux_statx):
    path = tmp_path/'owned.db'
    path.touch()
    original = retention.os.open
    def swap_before_open(name, flags):
        assert flags == retention.os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC
        path.rename(tmp_path/'old.db')
        path.touch()
        return original(name, flags)
    monkeypatch.setattr(retention.os, 'open', swap_before_open)
    with pytest.raises(RetentionDenied):
        RetentionLedger(path)
    assert mocked_linux_statx['calls'] == []


def test_linux_statx_loader_error_is_sanitized(tmp_path, monkeypatch, mocked_linux_statx):
    path = tmp_path/'SENSITIVE-IDENTITY.db'
    path.touch()
    def unavailable(_):
        raise OSError(str(path))
    monkeypatch.setattr(ctypes, 'CDLL', unavailable)
    target = RetentionLedger.__new__(RetentionLedger)
    target.path = path
    with pytest.raises(RetentionDenied) as caught:
        retention._identity(target)
    assert str(caught.value) == 'brief retention unavailable'
    assert caught.value.__context__ is None and caught.value.__cause__ is None


def test_final_mirror_uses_minimum_shared_snapshot_expiry(tmp_path):
    a, service, c = configured(tmp_path)
    try:
        first = service.submit(event()).job_id
        second = service.submit(event(event_id='second')).job_id
        c.register(SCOPE, first, policy=RetentionPolicy(at(10)))
        c.register(SCOPE, second, policy=RetentionPolicy(at(60)))
        a.now = 9
        guard = c.guard(service.store)
        def final_mirror():
            a.now = 10
        with pytest.raises(RetentionDenied):
            guard.check(SCOPE, second, authorize=final_mirror)
        assert c.status(SCOPE, second).decision == 'expired'
        a.now = 9
        with pytest.raises(RetentionDenied):
            guard.check(SCOPE, second, authorize=lambda: None)
    finally:
        service.close()


@pytest.mark.parametrize('change', ['valid', 'rollback', 'another_requester'])
def test_final_mirror_clock_and_requester_isolation(tmp_path, change):
    from test_brief_service import Authority
    a, service, c = configured(tmp_path)
    bob = RequesterContext(SCOPE.namespace, Principal('feishu', 'bob'))
    other_authority = Authority()
    other_authority.destination = PrivateDestination(bob.principal, 'oc_bob')
    other_authority.grant = replace(other_authority.grant, principal=bob.principal,
                                   allowed_audience=(bob.principal,))
    body = request()
    for source in body['sources']:
        source['allowed_audience'] = [bob.principal.to_dict()]
    other_authority.sources = {s.source_id: SourceSnapshot(bob, s, at(100))
                              for s in ManualRequest.from_dict(body).sources}
    other = other_authority.service(service.store, retention=c.guard(service.store))
    try:
        job = service.submit(event()).job_id
        other_job = other.submit(event(body, sender_id='bob', event_id='bob-event')).job_id
        c.register(SCOPE, job, policy=RetentionPolicy(at(60)))
        c.register(bob, other_job, policy=RetentionPolicy(at(60)))
        a.now = 9
        calls = []
        def final_mirror():
            calls.append(True)
            if change == 'rollback':
                a.now = 8
            elif change == 'another_requester':
                c.revoke_job(bob, other_job)
        guard = c.guard(service.store)
        if change == 'rollback':
            with pytest.raises(RetentionDenied):
                guard.check(SCOPE, job, authorize=final_mirror)
        else:
            assert guard.check(SCOPE, job, authorize=final_mirror) is None
            assert c.status(SCOPE, job).decision == 'authorized'
        assert calls == [True]
    finally:
        other.close()
        service.close()


def test_other_coordinator_revocation_has_durable_read_decision_boundary(tmp_path):
    """No claim of instantaneous process-wide consistency between two owners.

    Mutation in a mirror violates its nonblocking contract. This deliberately
    adversarial example records the unavoidable ordering: the durable read may
    decide first, but the next gate must see the other coordinator's tombstone.
    """
    a, service, c = configured(tmp_path)
    try:
        job = service.submit(event()).job_id
        c.register(SCOPE, job, policy=RetentionPolicy(at(60)))
        second_ledger = RetentionLedger(c.ledger.path)
        second_ledger.initialize()
        other = RetentionCoordinator(ledger=second_ledger, store=service.store,
                                     clock=lambda: at(a.now))
        def final_mirror():
            other.revoke_job(SCOPE, job)
        guard = c.guard(service.store)
        assert guard.check(SCOPE, job, authorize=final_mirror) is None
        with pytest.raises(RetentionDenied):
            guard.check(SCOPE, job, authorize=lambda: None)
    finally:
        service.close()
@pytest.mark.parametrize('state', ['started', 'unknown', 'settled'])
@pytest.mark.parametrize('when', ['before', 'after'])
def test_retired_cost_diagnostic_redaction_and_late_settlement(tmp_path, state, when):
    from ostag.briefs.store import CostProfile, Usage
    _, service, c = configured(tmp_path)
    try:
        job_id = service.submit(event()).job_id
        c.register(SCOPE, job_id, policy=RetentionPolicy(at(60)))
        worker = WorkerContext('cost-reconciler')
        job = service.store.claim_one(worker, clock=service.clock)
        service.store.start_generating(job.lease, clock=service.clock)
        cost = service.store.reserve_cost(job.lease, call_id='late-cost',
            profile=CostProfile('{}', 'a'*64, 100, 1, 100), config_hash='b'*64,
            daily_limit_micros=1000, clock=service.clock)
        service.store.start_cost(job.lease, cost.reservation_id,
                                daily_limit_micros=1000, clock=service.clock)
        usage = Usage(2, 3, 6)
        if state == 'unknown':
            service.store.mark_cost_unknown(worker, cost.reservation_id, 'timeout', clock=service.clock)
        elif state == 'settled':
            service.store.settle_cost(worker, cost.reservation_id, usage, clock=service.clock)
        if when == 'before':
            # Existing unconfigured/active diagnostic API stays compatible.
            active = service.store.mark_cost_unknown(worker, cost.reservation_id, MARKER,
                                                     clock=service.clock)
            assert active.diagnostic == MARKER
        c.revoke_job(SCOPE, job_id)
        assert c.cleanup(SCOPE, job_id).cleanup == 'complete'
        reopened = BriefStore(service.store.path)
        if when == 'after':
            reopened.mark_cost_unknown(worker, cost.reservation_id, MARKER, clock=service.clock)
        retained = reopened.cost_record(SCOPE, job_id, 'late-cost')
        assert retained.diagnostic == 'retired'
        assert retained.state == ('settled' if state == 'settled' else 'unknown')
        assert retained.charged_micros == (6 if state == 'settled' else 100)
        assert retained.config_hash == cost.config_hash and retained.charged_day == cost.charged_day
        for diagnostic in (MARKER, 'ANOTHERPRIVATEIDENTIFIER', 'checkpoint_denied', 'timeout', 'crash'):
            result = reopened.mark_cost_unknown(worker, cost.reservation_id, diagnostic,
                                                 clock=service.clock)
            assert result.diagnostic == (diagnostic if diagnostic in (
                'checkpoint_denied', 'timeout', 'crash') else 'retired')
            assert reopened.mark_cost_unknown(worker, cost.reservation_id, diagnostic,
                                              clock=service.clock) == result
        settled = reopened.settle_cost(worker, cost.reservation_id, usage, clock=service.clock)
        assert settled.state == 'settled' and settled.charged_micros == 6
        assert reopened.settle_cost(worker, cost.reservation_id, usage, clock=service.clock) == settled
        with pytest.raises(Conflict):
            reopened.settle_cost(worker, cost.reservation_id, Usage(2, 3, 7), clock=service.clock)
        with pytest.raises(Conflict):
            reopened.release_cost(worker, cost.reservation_id, clock=service.clock)
        for scope in (replace(SCOPE, namespace='other'),
                      replace(SCOPE, principal=Principal('feishu', 'other'))):
            assert reopened.cost_record(scope, job_id, 'late-cost') is None
        assert c.status(SCOPE, job_id).cleanup == 'complete'
        assert MARKER not in sql_dump(reopened.path)
        assert MARKER.encode() not in reopened.path.read_bytes()
        assert b'ANOTHERPRIVATEIDENTIFIER' not in reopened.path.read_bytes()
    finally:
        service.close()


async def test_retired_send_custody_updates_without_observations(tmp_path):
    from test_brief_delivery_observation import MESSAGE_ID, proof
    _, briefs, c, p, text, _, _, _, _ = full_stack(tmp_path)
    try:
        intent = DeliveryIntent.from_outbox(SCOPE, briefs.store.outbox(SCOPE, p.job_id), BINDING)
        await text.prepare(intent)
        attempt = briefs.store.begin_delivery(text.worker, intent, clock=briefs.clock,
                                              precommit=lambda: None)
        c.revoke_job(SCOPE, p.job_id)
        assert c.cleanup(SCOPE, p.job_id).cleanup == 'complete'
        reopened = BriefStore(briefs.store.path)
        before = sql_dump(reopened.path)
        for worker, scope, token in (
                (WorkerContext('wrong-owner'), SCOPE, attempt.token),
                (text.worker, replace(SCOPE, namespace='other'), attempt.token),
                (text.worker, replace(SCOPE, principal=Principal('feishu', 'other')), attempt.token),
                (text.worker, SCOPE, 'wrong-token')):
            with pytest.raises(Conflict):
                reopened.delivery_unknown(worker, scope, intent.delivery_id, 'timeout',
                                          token=token, clock=briefs.clock)
        assert sql_dump(reopened.path) == before
        for reason in ('authorization_denied', 'timeout', 'timeout'):
            reopened.delivery_unknown(text.worker, SCOPE, intent.delivery_id, reason,
                                      token=attempt.token, clock=briefs.clock)
            assert c.status(SCOPE, p.job_id).cleanup == 'complete'
        with sqlite3.connect(reopened.path) as db:
            row = db.execute('SELECT state,token,owner_id,uncertainty_json,first_failure_json,'
                             'intent_json,receipt_json FROM brief_deliveries').fetchone()
            assert row[:3] == ('unknown', attempt.token, text.worker.owner_id)
            assert json.loads(row[3])['reason'] == 'send_unverified'
            assert json.loads(row[4])['reason'] == 'authorization_denied'
            assert row[5:] == ('{"retired":true}', None)
            assert db.execute('SELECT count(*) FROM brief_delivery_observations').fetchone()[0] == 0
        for call in (
                lambda: reopened.begin_delivery(text.worker, intent, clock=briefs.clock,
                                                 precommit=lambda: None),
                lambda: reopened.prepare_delivery(text.worker, intent, clock=briefs.clock,
                                                   precommit=lambda: None),
                lambda: reopened.accept_delivery_response(text.worker, attempt, MESSAGE_ID,
                    (proof(intent),), clock=briefs.clock, precommit=lambda: None),
                lambda: reopened.begin_delivery_observation(text.worker, intent, clock=briefs.clock,
                                                             precommit=lambda: None)):
            with pytest.raises(Conflict):
                call()
        assert c.status(SCOPE, p.job_id).cleanup == 'complete'
        assert MARKER not in sql_dump(reopened.path)
        assert MARKER.encode() not in reopened.path.read_bytes()
    finally:
        briefs.close()
def test_cleanup_verifies_cost_diagnostic_redaction(tmp_path, monkeypatch):
    from contextlib import contextmanager
    _, briefs, c, p, text, _, _, _, _ = full_stack(tmp_path)
    try:
        cost = briefs.store.cost_record(SCOPE, p.job_id, CALL_ID)
        briefs.store.mark_cost_unknown(text.worker, cost.reservation_id, MARKER, clock=briefs.clock)
        original = retention.owned_retirement
        ignored = []

        @contextmanager
        def faulty(target, **kwargs):
            with original(target, **kwargs) as db:
                def authorize(action, name, field, *_):
                    if (action, name, field) == (
                            sqlite3.SQLITE_UPDATE, 'brief_cost_calls', 'diagnostic'):
                        ignored.append(True)
                        return sqlite3.SQLITE_IGNORE
                    return sqlite3.SQLITE_OK
                db.set_authorizer(authorize)
                yield db

        monkeypatch.setattr(retention, 'owned_retirement', faulty)
        c.revoke_job(SCOPE, p.job_id)
        assert c.cleanup(SCOPE, p.job_id).cleanup != 'complete'
        assert ignored
        monkeypatch.setattr(retention, 'owned_retirement', original)
        assert c.cleanup(SCOPE, p.job_id).cleanup == 'complete'
        assert briefs.store.cost_record(SCOPE, p.job_id, CALL_ID).charged_micros == cost.charged_micros
        assert MARKER not in sql_dump(briefs.store.path)
        assert MARKER.encode() not in briefs.store.path.read_bytes()
    finally:
        briefs.close()
@pytest.mark.parametrize('state', ['reserved', 'released', 'started', 'unknown', 'settled'])
def test_retirement_preserves_safe_cost_evidence_and_release_rules(tmp_path, state):
    from ostag.briefs.store import CostProfile, LeaseLost, Usage
    _, service, c = configured(tmp_path)
    try:
        job_id = service.submit(event()).job_id
        c.register(SCOPE, job_id, policy=RetentionPolicy(at(60)))
        worker = WorkerContext('cost-custodian')
        job = service.store.claim_one(worker, clock=service.clock)
        service.store.start_generating(job.lease, clock=service.clock)
        profile = CostProfile('{}', 'a'*64, 100, 1, 100)
        cost = service.store.reserve_cost(job.lease, call_id='custody', profile=profile,
            config_hash='b'*64, daily_limit_micros=1000, clock=service.clock)
        if state in ('started', 'unknown', 'settled'):
            cost = service.store.start_cost(job.lease, cost.reservation_id,
                                            daily_limit_micros=1000, clock=service.clock)
        if state == 'released':
            cost = service.store.release_cost(worker, cost.reservation_id, clock=service.clock)
        elif state == 'unknown':
            cost = service.store.mark_cost_unknown(worker, cost.reservation_id, 'transport_error',
                                                   clock=service.clock)
        elif state == 'settled':
            cost = service.store.settle_cost(worker, cost.reservation_id, Usage(1, 2, 101),
                                             clock=service.clock)
        c.revoke_job(SCOPE, job_id)
        assert c.cleanup(SCOPE, job_id).cleanup == 'complete'
        reopened = BriefStore(service.store.path)
        assert reopened.cost_record(SCOPE, job_id, 'custody') == cost
        assert reopened.has_uncertain_cost(worker) == (state in ('started', 'unknown'))
        for call in (
                lambda: reopened.start_cost(job.lease, cost.reservation_id,
                    daily_limit_micros=1000, clock=service.clock),
                lambda: reopened.reserve_cost(job.lease, call_id='custody', profile=profile,
                    config_hash='b'*64, daily_limit_micros=1000, clock=service.clock)):
            with pytest.raises(LeaseLost):
                call()
        if state in ('reserved', 'released'):
            released = reopened.release_cost(worker, cost.reservation_id, clock=service.clock)
            assert released.state == 'released' and released.charged_micros == 0
            assert reopened.release_cost(worker, cost.reservation_id, clock=service.clock) == released
            with pytest.raises(Conflict):
                reopened.settle_cost(worker, cost.reservation_id, Usage(1, 2, 3), clock=service.clock)
        else:
            with pytest.raises(Conflict):
                reopened.release_cost(worker, cost.reservation_id, clock=service.clock)
        assert c.status(SCOPE, job_id).cleanup == 'complete'
    finally:
        service.close()


async def test_retired_observation_custody_cannot_repopulate_history(tmp_path):
    from test_brief_delivery_observation import MESSAGE_ID, proof
    _, briefs, c, p, text, _, _, _, _ = full_stack(tmp_path)
    try:
        intent = DeliveryIntent.from_outbox(SCOPE, briefs.store.outbox(SCOPE, p.job_id), BINDING)
        await text.prepare(intent)
        attempt = briefs.store.begin_delivery(text.worker, intent, clock=briefs.clock,
                                              precommit=lambda: None)
        briefs.store.accept_delivery_response(text.worker, attempt, MESSAGE_ID, (),
                                              clock=briefs.clock, precommit=lambda: None)
        observation = briefs.store.begin_delivery_observation(text.worker, intent,
            clock=briefs.clock, precommit=lambda: None)
        c.revoke_job(SCOPE, p.job_id)
        assert c.cleanup(SCOPE, p.job_id).cleanup == 'complete'
        reopened = BriefStore(briefs.store.path)
        before = sql_dump(reopened.path)
        for _ in range(2):
            with pytest.raises(Conflict):
                reopened.delivery_unknown(text.worker, SCOPE, intent.delivery_id, 'timeout',
                                          observation=observation, clock=briefs.clock)
            with pytest.raises(Conflict):
                reopened.accept_delivery_observation(text.worker, observation, (proof(intent),),
                    clock=briefs.clock, precommit=lambda: None)
        assert sql_dump(reopened.path) == before
        with sqlite3.connect(reopened.path) as db:
            assert db.execute('SELECT state,read_attempts,candidate_id FROM brief_deliveries'
                              ).fetchone() == ('unknown', 1, MESSAGE_ID)
            assert db.execute('SELECT owner_id,token,settled FROM brief_delivery_reads'
                              ).fetchone() == (text.worker.owner_id, observation.token, 0)
        assert c.status(SCOPE, p.job_id).cleanup == 'complete'
        assert MARKER not in sql_dump(reopened.path)
        assert MARKER.encode() not in reopened.path.read_bytes()
    finally:
        briefs.close()


def test_lifecycle_binding_preserves_explicit_admin_authorizer(tmp_path):
    from ostag.briefs.lifecycle import BriefLifecycle
    calls = []
    life = BriefLifecycle()
    life.apply(1, 'on', False)
    def admin(actor, scope, operation):
        calls.append((actor, scope, operation))
    authority, service, coordinator = configured(tmp_path, lifecycle=life, maintenance=admin)
    try:
        job = service.submit(event()).job_id
        coordinator.register(SCOPE, job, policy=RetentionPolicy(at(60)))
        grant_id = authority.grant.grant_id
        assert coordinator.revoke_grant(SCOPE, SCOPE, grant_id) == 1
        assert calls == [(SCOPE, SCOPE, 'revoke_grant')]
        assert coordinator.status(SCOPE, job).decision == 'revoked'
        life.apply(2, 'off', True)
        before = (coordinator.ledger.path.read_bytes(), service.store.path.read_bytes())
        with pytest.raises(RetentionDenied):
            coordinator.cleanup_as(SCOPE, SCOPE, job)
        assert calls[-1] == (SCOPE, SCOPE, 'cleanup')
        assert before == (coordinator.ledger.path.read_bytes(), service.store.path.read_bytes())
    finally:
        service.close()
