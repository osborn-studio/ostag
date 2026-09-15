"""A06 trusted local contracts; no live card, platform or authenticated HTTP proof."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
import json
import sqlite3
from threading import Barrier, Event

import pytest

from ostag.briefs.revisions import (
    RevisionLedger, RevisionService, RevisionDenied, RevisionConflict,
    RevisionInvalid, RevisionClockError, RevisionStorageError, RevisionLimit,
    MAX_CORRECTION_BYTES,
)
from ostag.briefs.rendering import PrivatePreview, defang, render_private
from ostag.briefs.store import BriefStore, PrivateDestination, RequesterContext
from ostag.briefs.models import SourceSet
from ostag.briefs.validation import Coverage
from ostag.core.identity import Principal
from test_brief_cards import BINDING, SCOPE, preview
from test_brief_service import Authority, event
from ostag.briefs.worker import BriefWorker


def service(path, *, current=None, now=None, authorize=None, capacity=1000,
            chain_limit=32, event_limit=256):
    current = current if current is not None else [replace(preview(), outbox_id='outbox')]
    now = now if now is not None else [1000]
    ledger = RevisionLedger(path, capacity=capacity, chain_limit=chain_limit,
                            event_limit=event_limit)
    ledger.initialize()
    return RevisionService(ledger=ledger, namespace=SCOPE.namespace,
        load=lambda scope, job: current[0], authorize=authorize or (lambda p: None),
        clock=lambda: now[0])


def correct(s, expected='revision', request='edit1', text='Human correction', **kwargs):
    return s.correct(SCOPE, 'job', expected_revision_id=expected,
                     request_id=request, text=text, **kwargs)


def adopt(s, expected, request='adopt1'):
    return s.adopt(SCOPE, 'job', expected_revision_id=expected, request_id=request)


def withdraw(s, expected, request='withdraw1'):
    return s.withdraw(SCOPE, 'job', expected_revision_id=expected, request_id=request)


def dump(path):
    with sqlite3.connect(path) as db:
        return tuple(db.iterdump())


def test_revision_chain_parent_editor_exact_text_and_detached_view(tmp_path):
    s = service(tmp_path/'revisions.db')
    root = s.register(SCOPE, 'job')
    assert root.state.head_revision_id == 'revision'
    a = correct(s, text='  Exact\nHUMAN input 👩🏽‍💻  ', section='progress', item_id='c1')
    b = correct(s, a.event.revision_id, 'edit2', 'Second annotation')
    view = s.read(SCOPE, 'job')
    assert view.original == replace(preview(), outbox_id='outbox')
    assert view.root_digest == root.root_digest
    assert [e.parent_revision_id for e in view.corrections] == ['revision', a.event.revision_id]
    assert [e.actor for e in view.corrections] == [SCOPE.principal, SCOPE.principal]
    assert view.corrections[0].text == '  Exact\nHUMAN input 👩🏽‍💻  '
    assert view.corrections[0].request_id == 'edit1'
    assert view.corrections[0].timestamp_us == 1000000000
    assert view.state.head_revision_id == b.event.revision_id
    assert view.state.business_approved is False
    with pytest.raises(FrozenInstanceError):
        view.corrections[0].text = 'overwrite'
    for obj in (s, s.ledger, a, view, view.corrections[0]):
        assert 'Exact' not in repr(obj) and 'alice' not in repr(obj)


def test_stale_a05_card_cannot_confirm_new_revision_and_historical_replay(tmp_path):
    path = tmp_path/'revisions.db'
    s = service(path)
    s.register(SCOPE, 'job')
    original_card = render_private(preview())
    before = json.dumps(original_card)
    r = correct(s)
    with pytest.raises(RevisionConflict):
        adopt(s, preview().revision_id)
    chosen = r.event.revision_id
    accepted = adopt(s, chosen)
    assert accepted.current.adopted_revision_id == chosen
    assert accepted.current.business_approved is False
    removed = withdraw(s, chosen)
    assert removed.event.operation == 'withdraw'
    assert removed.event.actor == SCOPE.principal
    assert removed.current.adopted_revision_id is None
    reopened = service(path)
    historical = adopt(reopened, chosen)
    assert historical.replayed is True
    assert historical.event == accepted.event
    assert historical.current.adopted_revision_id is None
    new = correct(reopened, chosen, 'edit2')
    assert adopt(reopened, chosen).current.head_revision_id == new.event.revision_id
    assert len(reopened.read(SCOPE, 'job').events) == 4
    assert json.dumps(original_card) == before


def test_exact_idempotency_and_new_correction_invalidates_adoption(tmp_path):
    s = service(tmp_path/'r.db')
    s.register(SCOPE, 'job')
    first = correct(s)
    assert correct(s).event == first.event
    assert correct(s).replayed
    for kwargs in ({'text': 'changed'}, {'section': 'progress'}, {'request': 'edit1',
                    'expected': first.event.revision_id}):
        with pytest.raises(RevisionConflict):
            correct(s, **kwargs)
    with pytest.raises(RevisionConflict):
        adopt(s, 'revision', 'edit1')
    adopt(s, first.event.revision_id)
    second = correct(s, first.event.revision_id, 'edit2')
    assert second.current.adopted_revision_id is None
    assert adopt(s, first.event.revision_id).current.adopted_revision_id is None
    assert len(s.read(SCOPE, 'job').events) == 3


def test_real_a04_complete_outbox_root_revisions_leave_original_bytes_unchanged(tmp_path):
    authority = Authority()
    authority.destination = PrivateDestination(SCOPE.principal, 'oc_private')
    original = authority.service(BriefStore(tmp_path/'brief.db'))
    worker = BriefWorker(original)
    try:
        submitted = original.submit(event())
        assert worker.run_once().result(timeout=5) == 'draft_ready'
        box = original.store.outbox(SCOPE, submitted.job_id)
        sources = SourceSet.from_dict(json.loads(box.revision.provenance_json)['sources'])
        p = PrivatePreview.from_outbox(SCOPE, box, BINDING, sources)
        frozen = dump(original.store.path)
        raw_sources = json.dumps(sources.to_dict())
        current = [p]
        s = service(tmp_path/'r.db', current=current)
        s.register(SCOPE, p.job_id)
        r = s.correct(SCOPE, p.job_id, expected_revision_id=p.revision_id,
                      request_id='human1', text='The human disagrees with this draft')
        pages = s.render(SCOPE, p.job_id).pages
        assert any('The human disagrees' in page for page in pages)
        s.adopt(SCOPE, p.job_id, expected_revision_id=r.event.revision_id, request_id='content1')
        s.withdraw(SCOPE, p.job_id, expected_revision_id=r.event.revision_id, request_id='undo1')
        reopened = service(tmp_path/'r.db', current=current)
        replay = reopened.adopt(SCOPE, p.job_id, expected_revision_id=r.event.revision_id,
                                request_id='content1')
        assert replay.replayed and replay.current.adopted_revision_id is None
        assert dump(original.store.path) == frozen
        assert json.dumps(sources.to_dict()) == raw_sources
        assert original.store.outbox(SCOPE, p.job_id) == box
    finally:
        worker.stop()
        original.close()


@pytest.mark.parametrize('operation', ['read', 'render', 'correct', 'adopt', 'withdraw', 'register'])
@pytest.mark.parametrize('scope', [RequesterContext('other', SCOPE.principal),
    RequesterContext(SCOPE.namespace, Principal('feishu', 'bob'))])
def test_cross_scope_never_releases_private_state(tmp_path, scope, operation):
    s = service(tmp_path/'r.db')
    s.register(SCOPE, 'job')
    r = correct(s)
    adopt(s, r.event.revision_id)
    before = dump(s.ledger.path)
    kwargs = {} if operation in ('read', 'render', 'register') else {
        'expected_revision_id': r.event.revision_id, 'request_id': 'foreign'}
    if operation == 'correct':
        kwargs['text'] = 'changed'
    with pytest.raises(RevisionDenied) as caught:
        getattr(s, operation)(scope, 'job', **kwargs)
    assert caught.value.__context__ is caught.value.__cause__ is None
    assert 'Human' not in str(caught.value) and 'alice' not in repr(caught.value)
    assert dump(s.ledger.path) == before


@pytest.mark.parametrize('change', ['content', 'link', 'destination', 'generation', 'grant',
    'excerpt', 'outbox', 'revision', 'state', 'window', 'purpose', 'audience'])
def test_full_root_identity_is_frozen_even_with_same_revision_id(tmp_path, change):
    current = [replace(preview(), outbox_id='outbox')]
    s = service(tmp_path/'r.db', current=current)
    s.register(SCOPE, 'job')
    p = current[0]
    if change == 'content':
        p = replace(preview(text='Changed private claim'), outbox_id='outbox')
    elif change == 'link':
        p = replace(p, draft=replace(p.draft, links=(replace(p.draft.links[0],
                                                           url='https://other.example/source'),)))
    elif change == 'destination':
        p = replace(p, destination=replace(p.destination, address='oc_elsewhere'))
    elif change == 'generation':
        p = replace(p, binding=replace(p.binding, generation=2))
    elif change in ('grant', 'excerpt'):
        source = replace(p.sources.sources[0], **({'grant_revision': 2} if change == 'grant'
                                                else {'excerpt': 'Changed raw excerpt'}))
        p = replace(p, sources=replace(p.sources, sources=(source,)))
    elif change in ('outbox', 'revision', 'state'):
        key = {'outbox': 'outbox_id', 'revision': 'revision_id', 'state': 'state'}[change]
        p = replace(p, **{key: 'cancelled' if change == 'state' else 'other'})
    else:
        sources = p.sources
        if change == 'window':
            changes = {'window': replace(sources.window, display_timezone='Asia/Shanghai')}
        elif change == 'purpose':
            changes = {'purpose': 'different'}
        else:
            changes = {'allowed_audience': (SCOPE.principal, Principal('feishu', 'bob'))}
        sources = replace(sources, **changes,
                          sources=tuple(replace(src, **changes) for src in sources.sources))
        p = replace(p, sources=sources)
    current[0] = p
    before = dump(s.ledger.path)
    for fn in (lambda: s.read(SCOPE, 'job'), lambda: s.render(SCOPE, 'job'), lambda: correct(s),
               lambda: s.register(SCOPE, 'job'), lambda: adopt(s, 'revision')):
        with pytest.raises(RevisionDenied):
            fn()
    assert dump(s.ledger.path) == before


@pytest.mark.parametrize('operation', ['register', 'correct', 'adopt', 'withdraw', 'read', 'render'])
def test_final_policy_failure_rolls_back_every_effect(tmp_path, operation):
    s = service(tmp_path/'r.db')
    if operation != 'register':
        s.register(SCOPE, 'job')
    if operation == 'withdraw':
        adopt(s, 'revision')
    before = dump(s.ledger.path)
    checks = []
    def authorize(p):
        checks.append(p)
        if len(checks) == 2:
            raise RuntimeError('PRIVATE excerpt /secret/path token')
    s._authorize = authorize
    fn = {'register': lambda: s.register(SCOPE, 'job'), 'correct': lambda: correct(s),
          'adopt': lambda: adopt(s, 'revision'), 'withdraw': lambda: withdraw(s, 'revision'),
          'read': lambda: s.read(SCOPE, 'job'), 'render': lambda: s.render(SCOPE, 'job')}[operation]
    with pytest.raises(RevisionDenied) as caught:
        fn()
    assert len(checks) == 2
    assert caught.value.__cause__ is caught.value.__context__ is None
    assert 'PRIVATE' not in str(caught.value)
    assert dump(s.ledger.path) == before


def test_revocation_after_render_cannot_release_pages(tmp_path, monkeypatch):
    import ostag.briefs.revisions as revisions
    allowed = [True]
    def authorize(p):
        if not allowed[0]:
            raise RuntimeError('PRIVATE source revoked')
    s = service(tmp_path/'r.db', authorize=authorize)
    s.register(SCOPE, 'job')
    real = revisions._render
    def render_then_revoke(view):
        result = real(view)
        allowed[0] = False
        return result
    monkeypatch.setattr(revisions, '_render', render_then_revoke)
    with pytest.raises(RevisionDenied):
        s.render(SCOPE, 'job')


def test_lock_wait_then_revocation_is_seen_before_commit(tmp_path):
    allowed, entered = [True], Event()
    s = service(tmp_path/'r.db')
    s.register(SCOPE, 'job')
    before = dump(s.ledger.path)
    def authorize(p):
        if not allowed[0]:
            raise RuntimeError('revoked')
        entered.set()
    s._authorize = authorize
    with sqlite3.connect(s.ledger.path) as blocker, ThreadPoolExecutor(1) as pool:
        blocker.execute('BEGIN IMMEDIATE')
        task = pool.submit(correct, s)
        assert entered.wait(1)
        allowed[0] = False
        blocker.rollback()
        with pytest.raises(RevisionDenied):
            task.result(timeout=2)
    assert dump(s.ledger.path) == before


@pytest.mark.parametrize('same_request', [False, True])
def test_two_connections_serialize_head_and_deduplicate_event(tmp_path, same_request):
    path = tmp_path/'r.db'
    one, two = service(path), service(path)
    barrier = Barrier(2)
    def register(s):
        barrier.wait()
        return s.register(SCOPE, 'job')
    with ThreadPoolExecutor(2) as pool:
        roots = list(pool.map(register, (one, two)))
    assert roots[0].root_digest == roots[1].root_digest
    def run(args):
        s, request = args
        barrier.wait()
        try:
            return correct(s, request=request)
        except RevisionConflict:
            return None
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(run, ((one, 'edit1'), (two, 'edit1' if same_request else 'edit2'))))
    assert sum(r is not None for r in results) == (2 if same_request else 1)
    if same_request:
        assert results[0].event == results[1].event
        assert sorted(r.replayed for r in results) == [False, True]
    reopened = service(path)
    assert len(reopened.read(SCOPE, 'job').corrections) == 1
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT count(*) FROM revision_roots').fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM revision_events').fetchone()[0] == 1


def test_distinct_adoptions_serialize_and_same_withdraw_deduplicates(tmp_path):
    path = tmp_path/'r.db'
    one, two = service(path), service(path)
    one.register(SCOPE, 'job')
    with ThreadPoolExecutor(2) as pool:
        receipts = list(pool.map(lambda args: adopt(args[0], 'revision', args[1]),
                                ((one, 'a1'), (two, 'a2'))))
    assert len({r.event.event_id for r in receipts}) == 2
    with ThreadPoolExecutor(2) as pool:
        receipts = list(pool.map(lambda s: withdraw(s, 'revision'), (one, two)))
    assert receipts[0].event == receipts[1].event
    assert sorted(r.replayed for r in receipts) == [False, True]
    assert len(one.read(SCOPE, 'job').events) == 3


@pytest.mark.parametrize('kind', ['foreign', 'future', 'wrong_app', 'extra_table', 'trigger',
                                   'view', 'ddl', 'malformed', 'wal', 'empty_versioned'])
def test_reject_bad_database_without_mutating_bytes(tmp_path, kind):
    path = tmp_path/'r.db'
    if kind == 'malformed':
        path.write_bytes(b'private invalid sqlite database')
    elif kind in ('foreign', 'empty_versioned'):
        with sqlite3.connect(path) as db:
            if kind == 'foreign':
                db.execute('CREATE TABLE private_notes(secret TEXT)')
            else:
                db.execute('PRAGMA user_version=12')
    else:
        service(path)
        with sqlite3.connect(path) as db:
            sql = {'future': 'PRAGMA user_version=99', 'wrong_app': 'PRAGMA application_id=42',
                   'extra_table': 'CREATE TABLE private_notes(secret TEXT)',
                   'trigger': 'CREATE TRIGGER extra AFTER INSERT ON revision_events '
                              'BEGIN SELECT 1; END',
                   'view': 'CREATE VIEW extra AS SELECT * FROM revision_meta',
                   'ddl': 'ALTER TABLE revision_meta ADD COLUMN surprise INTEGER',
                   'wal': 'PRAGMA journal_mode=WAL'}[kind]
            db.execute(sql)
    before = path.read_bytes()
    with pytest.raises(RevisionStorageError):
        RevisionLedger(path).initialize()
    assert path.read_bytes() == before


@pytest.mark.parametrize('kind', ['meta', 'digest', 'actor', 'parent', 'ordinal', 'timestamp',
                                   'json_bool', 'json_duplicate', 'json_huge'])
def test_durable_integrity_checked_before_private_release(tmp_path, kind):
    import hashlib
    path = tmp_path/'r.db'
    s = service(path)
    s.register(SCOPE, 'job')
    correct(s)
    with sqlite3.connect(path) as db:
        if kind == 'meta':
            db.execute('UPDATE revision_meta SET highwater=-1')
        elif kind == 'digest':
            db.execute("UPDATE revision_roots SET digest='wrong'")
        else:
            raw = db.execute('SELECT record FROM revision_events').fetchone()[0]
            value = json.loads(raw)
            if kind == 'actor':
                value['actor']['actor_id'] = 'bob'
            elif kind == 'parent':
                value['parent_revision_id'] = 'wrong'
            elif kind == 'ordinal':
                value['ordinal'] = 1
            elif kind == 'timestamp':
                value['timestamp_us'] += 1
            elif kind == 'json_bool':
                value['ordinal'] = True
            raw = json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(',', ':'))
            if kind == 'json_duplicate':
                raw = raw[:-1] + ',"text":"private"}'
            elif kind == 'json_huge':
                raw = '{"text":"' + 'x'*65536 + '"}'
            db.execute('UPDATE revision_events SET record=?,digest=?',
                       (raw, hashlib.sha256(raw.encode()).hexdigest()))
    before = path.read_bytes()
    with pytest.raises((RevisionStorageError, RevisionInvalid)):
        s.read(SCOPE, 'job')
    assert path.read_bytes() == before


@pytest.mark.parametrize('bad', [True, False, None, '1000', float('nan'), float('inf'), -1, 10**11])
def test_clock_strict_types_and_values(tmp_path, bad):
    s = service(tmp_path/'r.db', now=[bad])
    before = dump(s.ledger.path)
    with pytest.raises(RevisionClockError):
        s.register(SCOPE, 'job')
    assert dump(s.ledger.path) == before


def test_clock_highwater_across_restart_and_final_rollback(tmp_path):
    path, now = tmp_path/'r.db', [1000]
    s = service(path, now=now)
    s.register(SCOPE, 'job')
    now[0] = 1001
    first = correct(s)
    before = dump(path)
    reopened = service(path, now=[1000])
    with pytest.raises(RevisionClockError):
        correct(reopened, first.event.revision_id, 'edit2')
    assert dump(path) == before
    for samples in ([1001, 1000], [1001, 1001, 1000], [1001, 1001, True]):
        ticks = iter(samples)
        s._clock = lambda: next(ticks)
        with pytest.raises(RevisionClockError):
            correct(s, first.event.revision_id, 'edit2')
        assert dump(path) == before


@pytest.mark.parametrize('kwargs', [{'text': ''}, {'text': ' '}, {'text': True},
    {'text': '\ud800'}, {'text': '密'*(MAX_CORRECTION_BYTES//3+1)},
    {'text': 'x'*(MAX_CORRECTION_BYTES+1)}, {'expected': None}, {'expected': True},
    {'request': '@all'}, {'section': 'source'}, {'item_id': 'c1'}, {'section': True},
    {'section': 'progress', 'item_id': True}, {'section': 'progress', 'item_id': 'absent'}])
def test_strict_mutation_inputs_are_inert_and_bounded(tmp_path, kwargs):
    s = service(tmp_path/'r.db')
    s.register(SCOPE, 'job')
    before = dump(s.ledger.path)
    with pytest.raises(RevisionInvalid):
        correct(s, **kwargs)
    assert dump(s.ledger.path) == before


def test_durable_chain_event_and_global_row_caps_keep_replay_and_withdraw(tmp_path):
    path = tmp_path/'r.db'
    s = service(path, chain_limit=1, event_limit=3, capacity=4)
    s.register(SCOPE, 'job')
    first = correct(s)
    with pytest.raises(RevisionLimit):
        correct(s, first.event.revision_id, 'edit2')
    adopt(s, first.event.revision_id)
    withdraw(s, first.event.revision_id)
    reopened = service(path, chain_limit=1, event_limit=3, capacity=4)
    assert correct(reopened).replayed
    assert adopt(reopened, first.event.revision_id).current.adopted_revision_id is None
    with pytest.raises(RevisionLimit):
        adopt(reopened, first.event.revision_id, 'newadopt')
    with pytest.raises(RevisionStorageError):
        service(path)  # Cannot relax the durable bounds on restart.
    assert len(reopened.read(SCOPE, 'job').events) == 3


def test_every_gap_correction_and_trusted_link_survives_unicode_rendering(tmp_path):
    gaps = tuple(f'gap {i} ' + '密'*900 for i in range(15))
    p = replace(preview(Coverage(False, gaps)), outbox_id='outbox')
    s = service(tmp_path/'r.db', current=[p])
    s.register(SCOPE, 'job')
    text = '<script>alert(1)</script> https://evil.test @all [click](javascript:bad)\n' + (
        '👩🏽‍💻\u202e\x00a\u0301 ' * 170)
    first = correct(s, text=text)
    correct(s, first.event.revision_id, 'edit2', 'Second correction stays visible')
    rendered = s.render(SCOPE, 'job')
    assert isinstance(rendered.pages, tuple)
    correction_parts, gap_parts, links = [], [], []
    for raw in rendered.pages:
        assert len(raw.encode('utf-8')) <= 16384
        assert 'unverified human input' in raw and 'not business approval' in raw
        card = json.loads(raw)
        for element in card['elements']:
            if 'text' in element:
                content = element['text']['content']
                if content.startswith('Exact correction'):
                    correction_parts.append(content.split(': ', 1)[1])
                if content.startswith('Coverage gap'):
                    gap_parts.append(content.split(': ', 1)[1])
            for action in element.get('actions', []):
                links.append(action['url'])
                assert 'value' not in action
        for attack in ('<script>', '@all', 'https://evil.test', 'javascript:bad'):
            assert attack not in raw
    assert ''.join(correction_parts) == defang(text) + defang('Second correction stays visible')
    assert ''.join(gap_parts) == ''.join(defang(gap) for gap in gaps)
    assert links == ['https://trusted.example/source']


@pytest.mark.parametrize('failure', ['loader', 'authorizer', 'sqlite', 'clock'])
def test_callback_and_sqlite_errors_have_fixed_no_context_diagnostics(tmp_path, failure):
    s = service(tmp_path/'r.db')
    s.register(SCOPE, 'job')
    def fail(*args):
        raise RuntimeError('PRIVATE excerpt token /secret/path')
    expected = RevisionDenied
    if failure == 'loader':
        s._load = fail
    elif failure == 'authorizer':
        s._authorize = fail
    elif failure == 'sqlite':
        s.ledger._verify = fail
        expected = RevisionStorageError
    else:
        s._clock = fail
        expected = RevisionClockError
    with pytest.raises(expected) as caught:
        s.read(SCOPE, 'job')
    assert 'PRIVATE' not in str(caught.value)
    assert caught.value.__cause__ is caught.value.__context__ is None


def test_adoption_reserves_capacity_for_retraction(tmp_path):
    s = service(tmp_path/'r.db', capacity=3, event_limit=2)
    s.register(SCOPE, 'job')
    adopt(s, 'revision')
    with pytest.raises(RevisionLimit):
        adopt(s, 'revision', 'another')
    assert withdraw(s, 'revision').current.adopted_revision_id is None


def test_future_event_cannot_hide_behind_later_wall_clock(tmp_path):
    import hashlib
    now = [1000]
    s = service(tmp_path/'r.db', now=now)
    s.register(SCOPE, 'job')
    correct(s)
    with sqlite3.connect(s.ledger.path) as db:
        value = json.loads(db.execute('SELECT record FROM revision_events').fetchone()[0])
        value['timestamp_us'] += 1
        raw = json.dumps(value, sort_keys=True, separators=(',', ':'))
        db.execute('UPDATE revision_events SET record=?,digest=?',
                   (raw, hashlib.sha256(raw.encode()).hexdigest()))
    now[0] = 1010
    before = dump(s.ledger.path)
    with pytest.raises(RevisionStorageError):
        s.read(SCOPE, 'job')
    assert dump(s.ledger.path) == before


@pytest.mark.parametrize('fault', ['root_ignore', 'event_error', 'final_sql_error', 'commit_error'])
def test_sqlite_faults_are_atomic_or_idempotently_recoverable(tmp_path, monkeypatch, fault):
    from contextlib import contextmanager
    path = tmp_path/'r.db'
    s = service(path)
    if fault != 'root_ignore':
        s.register(SCOPE, 'job')
    before = dump(path)
    class FaultConnection(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            if fault == 'root_ignore' and sql.startswith('INSERT INTO revision_roots'):
                return super().execute('SELECT 1')
            if ((fault == 'event_error' and sql.startswith('INSERT INTO revision_events')) or
                    (fault == 'final_sql_error' and sql.startswith('UPDATE revision_meta'))):
                raise sqlite3.OperationalError('PRIVATE sqlite /path excerpt')
            return super().execute(sql, parameters)

        def commit(self):
            super().commit()
            if fault == 'commit_error':
                raise sqlite3.OperationalError('PRIVATE lost commit response')

    @contextmanager
    def connection(**kwargs):
        db = sqlite3.connect(path, isolation_level=None, factory=FaultConnection)
        db.row_factory = sqlite3.Row
        try:
            yield db
        finally:
            db.close()
    with monkeypatch.context() as patch:
        patch.setattr(s.ledger, '_connection', connection)
        with pytest.raises(RevisionStorageError) as caught:
            s.register(SCOPE, 'job') if fault == 'root_ignore' else correct(s)
        assert caught.value.__context__ is caught.value.__cause__ is None
    if fault == 'commit_error':
        replay = correct(service(path))
        assert replay.replayed
        assert len(s.read(SCOPE, 'job').events) == 1
    else:
        assert dump(path) == before


@pytest.mark.parametrize('flag', [True, False, 1, {}, 'approved'])
def test_authorizer_must_raise_on_denial_and_never_return_auth_flags(tmp_path, flag):
    s = service(tmp_path/'r.db', authorize=lambda p: flag)
    with pytest.raises(RevisionDenied):
        s.register(SCOPE, 'job')


def test_oversized_root_and_forged_bool_generation_fail_closed(tmp_path):
    from test_brief_validation import evidence, output, validate
    sources = evidence(excerpt='密'*4000, count=100)
    draft = validate(output(), sources=sources)
    p = replace(preview(), sources=sources, draft=draft, outbox_id='outbox')
    s = service(tmp_path/'r.db', current=[p])
    with pytest.raises(RevisionLimit):
        s.register(SCOPE, 'job')
    current = [replace(preview(), outbox_id='outbox')]
    s = service(tmp_path/'r.db', current=current)
    object.__setattr__(current[0].binding, 'generation', True)
    try:
        with pytest.raises(RevisionDenied):
            s.register(SCOPE, 'job')
    finally:
        object.__setattr__(current[0].binding, 'generation', 1)


def test_long_valid_editor_item_and_unicode_trusted_url_still_render(tmp_path):
    p = replace(preview(), outbox_id='outbox')
    actor = Principal('feishu', 'a' + '@'*255)
    scope = RequesterContext(SCOPE.namespace, actor)
    source_set = replace(p.sources, allowed_audience=(actor,), sources=tuple(
        replace(src, allowed_audience=(actor,)) for src in p.sources.sources))
    item = replace(p.draft.progress[0], id='c' + '@'*255)
    url = 'https://trusted.example/' + '密'*1900
    p = replace(p, scope=scope, destination=PrivateDestination(actor, 'oc_private'),
        sources=source_set, draft=replace(p.draft, progress=(item,),
                                        links=(replace(p.draft.links[0], url=url),)))
    s = service(tmp_path/'r.db', current=[p])
    s.register(scope, 'job')
    s.correct(scope, 'job', expected_revision_id='revision', request_id='_'*128,
              text='x'*MAX_CORRECTION_BYTES, section='progress', item_id=item.id)
    pages = s.render(scope, 'job').pages
    assert all(len(page.encode()) <= 16384 for page in pages)
    actions = [a for page in pages for e in json.loads(page)['elements']
               for a in e.get('actions', [])]
    assert [a['url'] for a in actions] == [url]


def test_maximum_root_identifiers_and_links_are_not_truncated(tmp_path):
    p = replace(preview(), outbox_id='outbox', job_id='_'*128, revision_id='-'*128)
    url = 'https://trusted.example/' + '密'*1970
    p = replace(p, draft=replace(p.draft, links=(replace(p.draft.links[0], url=url),)))
    s = service(tmp_path/'r.db', current=[p])
    s.register(SCOPE, p.job_id)
    result = s.render(SCOPE, p.job_id)
    assert all(len(raw.encode()) <= 16384 for raw in result.pages)
    assert any(url == action['url'] for raw in result.pages
               for element in json.loads(raw)['elements']
               for action in element.get('actions', []))


def test_explicit_initialize_and_mutation_revision_are_required(tmp_path):
    from ostag.briefs.revisions import RevisionError
    ledger = RevisionLedger(tmp_path/'uninitialized.db')
    s = RevisionService(ledger=ledger, namespace=SCOPE.namespace,
        load=lambda scope, job: replace(preview(), outbox_id='outbox'), authorize=lambda p: None)
    with pytest.raises(RevisionStorageError):
        s.register(SCOPE, 'job')
    assert not ledger.path.exists()
    ledger.initialize()
    s.register(SCOPE, 'job')
    for operation in ('correct', 'adopt', 'withdraw'):
        kwargs = {'text': 'human'} if operation == 'correct' else {}
        with pytest.raises(RevisionError):
            getattr(s, operation)(SCOPE, 'job', request_id='missing_revision', **kwargs)
    assert s.read(SCOPE, 'job').events == ()


def test_schema_drift_after_open_and_busy_failure_do_not_write(tmp_path):
    path = tmp_path/'r.db'
    s = service(path)
    s.register(SCOPE, 'job')
    with sqlite3.connect(path) as blocker:
        blocker.execute('BEGIN EXCLUSIVE')
        with pytest.raises(RevisionStorageError):
            correct(s)
        blocker.rollback()
    with sqlite3.connect(path) as db:
        db.execute('PRAGMA user_version=99')
    before = path.read_bytes()
    with pytest.raises(RevisionStorageError):
        correct(s)
    assert path.read_bytes() == before
