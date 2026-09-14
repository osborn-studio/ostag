"""Private previews and independently owned durable callback capabilities."""
from dataclasses import replace
import json
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from ostag.briefs.callbacks import BriefCallbackController, CallbackLedger, CallbackError
from ostag.briefs.delivery import FeishuBinding
from ostag.briefs.rendering import PrivatePreview, render_private, render_group_status
from ostag.briefs.store import PrivateDestination, RequesterContext
from ostag.briefs.validation import Coverage, SourceLink
from ostag.work_monitor.identity import Destination
from test_brief_validation import ACTOR, evidence, output, validate

BINDING = FeishuBinding('tenant', 'installation', 'app', 'bot', 1)
SCOPE = RequesterContext('synthetic', ACTOR)
DEST = PrivateDestination(ACTOR, 'oc_private')


def preview(coverage=None, text='Private draft awaiting review'):
    sources = evidence()
    draft = validate(output(text), sources=sources, coverage=coverage,
                     links=(SourceLink('s0', 'https://trusted.example/source'),))
    return PrivatePreview(SCOPE, 'job', 'revision', DEST, BINDING, sources, draft, 'draft_ready')


def controller(path, now, *, authorize=None, load=None, resolve=None, capacity=20):
    ledger = CallbackLedger(path, capacity=capacity)
    ledger.initialize()
    return BriefCallbackController(ledger=ledger, namespace='synthetic',
        resolve_binding=resolve or (lambda: BINDING), load=load or (lambda scope, job: preview()),
        authorize=authorize or (lambda p: None), clock=lambda: now[0])


def invoke(c, token, **changes):
    args = dict(principal=ACTOR, destination=Destination('feishu', 'oc_private', 'private'),
                tenant_id='tenant', app_id='app', message_id='om_message_123')
    return c.handle_callback(token, **(args | changes))


def test_preview_has_plain_text_trusted_links_notice_and_no_private_repr():
    p = preview(Coverage(False, ('missing @all <b>html</b> https://evil.test **bold**',)))
    card = render_private(p)
    raw = json.dumps(card, ensure_ascii=False)
    assert len(raw.encode()) <= 16384
    assert 'semantic review required' in raw
    assert 'source-reported' in raw and 'proposals' in raw
    assert 'https://trusted.example/source' in raw
    for active in ('@all', '<b>', 'https://evil.test', '**bold**'):
        assert active not in raw
    assert 'plain_text' in raw and '"markdown"' not in raw
    assert 'Private draft' not in repr(p)
    assert 'callback_id' not in raw


def test_every_gap_survives_bounded_pagination_and_notice_on_each_page():
    p = preview(Coverage(False, tuple(f'gap {i} ' + '密' * 900 for i in range(40))))
    cards = []
    page = 0
    while True:
        card = render_private(p, page=page)
        raw = json.dumps(card, ensure_ascii=False)
        assert len(raw.encode()) <= 16384
        assert 'semantic review required' in raw
        cards.append(raw)
        page += 1
        if page == int(re.search(r'Page \d+ of (\d+)', raw)[1]):
            break
        assert page < 500
    combined = ''.join(cards)
    assert all(f'gap {i} ' in combined for i in range(40))


def test_group_status_is_fixed_and_contains_only_safe_job_identity():
    card = render_group_status('job@all.example', 'provider_accepted')
    raw = json.dumps(card)
    assert 'received' not in raw and 'read' not in raw
    assert '@all' not in raw and '.example' not in raw
    assert 'Private draft' not in raw
    assert 'pending verification' in json.dumps(render_group_status('job', 'unknown'))


def test_restart_replay_and_wrong_binding_cannot_burn(tmp_path):
    now = [1000.0]
    c = controller(tmp_path/'callback.db', now)
    token = c.issue(preview(), message_id='om_message_123', action='refresh')
    assert token.startswith('br_') and len(token) >= 25
    with pytest.raises(CallbackError):
        invoke(c, token, tenant_id='other')
    reopened = controller(tmp_path/'callback.db', now)
    assert 'Private draft' in json.dumps(invoke(reopened, token))
    with pytest.raises(CallbackError):
        invoke(c, token)


def test_two_contenders_only_one_private_effect(tmp_path):
    now = [1000.0]
    path = tmp_path/'callback.db'
    c = controller(path, now)
    token = c.issue(preview(), message_id='om_message_123')
    other = controller(path, now)
    def go(candidate):
        try:
            invoke(candidate, token)
            return 1
        except CallbackError:
            return 0
    with ThreadPoolExecutor(2) as pool:
        assert sum(pool.map(go, (c, other))) == 1


def test_expiry_is_exclusive_and_clock_rollback_conservative(tmp_path):
    now = [1000.0]
    c = controller(tmp_path/'callback.db', now)
    token = c.issue(preview(), message_id='om_message_123', ttl=10)
    now[0] = 1010
    with pytest.raises(CallbackError):
        invoke(c, token)
    now[0] = 1001
    with pytest.raises(CallbackError):
        invoke(c, token)


def signed_app(tmp_path, c):
    from fastapi import FastAPI
    from ostag.work_monitor.feishu_card_security import FeishuCardSecurity
    from ostag.work_monitor.feishu_routes import create_feishu_card_router
    from ostag.work_monitor.identity import IdentityStore
    from ostag.work_monitor.server import SecurityContract, register_feishu_card_router
    from ostag.work_monitor.store import MonitorStore
    store = MonitorStore(tmp_path/'monitor.db')
    identities = IdentityStore(store.path)
    identities.bind_private(owner=ACTOR, destination=Destination('feishu', 'oc_private', 'private'),
                            local_admin=True)
    app = FastAPI()
    register_feishu_card_router(app, create_feishu_card_router(object(), briefs=c),
        security=SecurityContract('verified_platform', FeishuCardSecurity(
            encrypt_key='encrypt-test', verification_token='verify', expected_app_id='app',
            store=store)))
    return app


def signed_post(client, token, *, nonce=None, mutate=None, signed=True):
    from ostag.work_monitor.feishu_card_security import feishu_signature
    import secrets
    payload = {'schema': '2.0', 'header': {'event_id': 'event',
        'event_type': 'card.action.trigger', 'token': 'verify', 'app_id': 'app',
        'tenant_key': 'tenant'}, 'event': {'operator': {'open_id': 'alice', 'tenant_key': 'tenant'},
        'action': {'tag': 'button', 'value': {'callback_id': token}},
        'context': {'open_chat_id': 'oc_private', 'open_message_id': 'om_message_123'}}}
    if mutate:
        mutate(payload)
    body = json.dumps(payload).encode()
    headers = {'Content-Type': 'application/json'}
    if signed:
        nonce = nonce or secrets.token_hex(12)
        headers |= {'X-Lark-Request-Timestamp': '1000', 'X-Lark-Request-Nonce': nonce,
                    'X-Lark-Signature': feishu_signature('1000', nonce, 'encrypt-test', body)}
    return client.post('/webhooks/feishu/card-action', content=body, headers=headers)


@pytest.mark.parametrize('mutation', [
    lambda p: p['event']['operator'].update(open_id='bob'),
    lambda p: p['event']['context'].update(open_chat_id='oc_group'),
    lambda p: p['event']['context'].update(open_message_id='om_other_123'),
    lambda p: p['header'].update(tenant_key='other'),
    lambda p: p['header'].update(app_id='other'),
    lambda p: p['event']['operator'].update(tenant_key='other'),
    lambda p: p['event']['action']['value'].update(actor='alice', revision='revision'),
    lambda p: p['event']['action'].update(tag='select_static'),
])
def test_signed_route_wrong_fields_never_burn_or_leak(tmp_path, mutation):
    from fastapi.testclient import TestClient
    c = controller(tmp_path/'callback.db', [1000])
    token = c.issue(preview(), message_id='om_message_123')
    with TestClient(signed_app(tmp_path, c)) as client:
        denied = signed_post(client, token, mutate=mutation)
        assert 'Private draft' not in denied.text
        assert 'trusted.example' not in denied.text
        good = signed_post(client, token)
        assert good.status_code == 200 and 'Private draft' in good.text
        assert 'Private draft' not in signed_post(client, token).text


@pytest.mark.parametrize('change', ['generation', 'revision', 'grant', 'source', 'mapping'])
def test_signed_route_current_revocation_cannot_consume_rightful_token(tmp_path, change):
    from fastapi.testclient import TestClient
    current = [preview()]
    installed = [BINDING]
    denied = [False]
    def authorize(p):
        if denied[0]:
            raise PermissionError('PRIVATE ACL ERROR')
    c = controller(tmp_path/'callback.db', [1000], load=lambda s, j: current[0],
                   resolve=lambda: installed[0], authorize=authorize)
    token = c.issue(current[0], message_id='om_message_123')
    if change == 'generation':
        installed[0] = replace(BINDING, generation=2)
    elif change == 'revision':
        current[0] = replace(current[0], revision_id='revision2')
    else:
        denied[0] = True
    with TestClient(signed_app(tmp_path, c)) as client:
        response = signed_post(client, token)
        assert 'Private draft' not in response.text and 'PRIVATE ACL' not in response.text
        current[0], installed[0], denied[0] = preview(), BINDING, False
        assert 'Private draft' in signed_post(client, token).text


def test_signed_http_restart_expiry_unsigned_and_disabled(tmp_path):
    from fastapi.testclient import TestClient
    now = [1000]
    c = controller(tmp_path/'callback.db', now)
    token = c.issue(preview(), message_id='om_message_123', ttl=5)
    with TestClient(signed_app(tmp_path, c)) as client:
        assert signed_post(client, token, signed=False).status_code == 401
    c = controller(tmp_path/'callback.db', now)
    with TestClient(signed_app(tmp_path, c)) as client:
        first = signed_post(client, token, nonce='same-nonce')
        assert 'Private draft' in first.text
        assert signed_post(client, token, nonce='same-nonce').status_code == 401
    token = c.issue(preview(), message_id='om_message_123', ttl=5)
    now[0] = 1005
    with TestClient(signed_app(tmp_path, c)) as client:
        assert 'Private draft' not in signed_post(client, token).text
    with TestClient(signed_app(tmp_path, None)) as client:
        # object() monitor has no store: a fallback would crash instead of generic denial.
        response = signed_post(client, token)
        assert response.status_code == 200 and 'unavailable' in response.text


def test_signed_route_two_controllers_contend_for_one_private_response(tmp_path):
    from fastapi.testclient import TestClient
    now = [1000]
    path = tmp_path/'callback.db'
    c = controller(path, now)
    token = c.issue(preview(), message_id='om_message_123')
    clients = [TestClient(signed_app(tmp_path, candidate))
               for candidate in (c, controller(path, now))]
    with clients[0], clients[1], ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda client: signed_post(client, token), clients))
    assert sum('Private draft' in r.text for r in results) == 1


def test_owned_ledger_init_capacity_lock_and_no_private_storage(tmp_path):
    path = tmp_path/'callback.db'
    ledger = CallbackLedger(path, capacity=1)
    assert not path.exists()
    with pytest.raises(Exception):
        ledger.lookup('br_'+'a'*32, clock=lambda: 1000)
    assert not path.exists()
    c = controller(path, [1000], capacity=1)
    token = c.issue(preview(), message_id='om_message_123')
    with pytest.raises(CallbackError):
        c.issue(preview(), message_id='om_message_123')
    with sqlite3.connect(path) as db:
        rows = db.execute('SELECT * FROM brief_callbacks').fetchall()
        assert len(rows) == 1
        assert 'Private draft' not in str(rows) and token not in str(rows)
        db.execute('BEGIN IMMEDIATE')
        with pytest.raises(CallbackError):
            invoke(c, token)
        db.rollback()
    assert 'Private draft' in json.dumps(invoke(c, token))
    with pytest.raises(CallbackError):
        CallbackLedger(path, capacity=2).initialize()
    foreign = tmp_path/'foreign.db'
    with sqlite3.connect(foreign) as db:
        db.execute('CREATE TABLE user_notes(body TEXT)')
    with pytest.raises(CallbackError):
        CallbackLedger(foreign).initialize()


def test_authorization_after_claim_and_before_release_and_crash_stays_spent(tmp_path, monkeypatch):
    import ostag.briefs.callbacks as callbacks
    denied = [False]
    checks = []
    def authorize(p):
        checks.extend(s.source_id for s in p.sources.sources)
        if denied[0]:
            raise PermissionError('VERY PRIVATE SOURCE ERROR')
    c = controller(tmp_path/'callback.db', [1000], authorize=authorize)
    token = c.issue(preview(), message_id='om_message_123')
    original = callbacks.render_private
    def render_then_revoke(*args, **kwargs):
        result = original(*args, **kwargs)
        denied[0] = True
        return result
    monkeypatch.setattr(callbacks, 'render_private', render_then_revoke)
    with pytest.raises(CallbackError) as caught:
        invoke(c, token)
    assert caught.value.__context__ is None and caught.value.__cause__ is None
    assert 'VERY PRIVATE' not in repr(caught.value)
    denied[0] = False
    monkeypatch.setattr(callbacks, 'render_private', original)
    with pytest.raises(CallbackError):
        invoke(controller(tmp_path/'callback.db', [1000]), token)
    assert checks


def test_adapter_is_disabled_by_default_and_never_uses_send_or_reply(tmp_path, monkeypatch):
    from ostag.adapters.feishu import FeishuAdapter
    from ostag.config import Settings
    adapter = FeishuAdapter(Settings())
    with pytest.raises(CallbackError):
        adapter.prepare_brief_preview(SCOPE, 'job', 'revision')
    def forbidden(*args, **kwargs):
        pytest.fail('legacy send/reply used')
    monkeypatch.setattr(adapter, 'reply', forbidden)
    adapter._brief_controller = controller(tmp_path/'callback.db', [1000])
    card = adapter.prepare_brief_preview(SCOPE, 'job', 'revision')
    assert 'Private draft' in json.dumps(card) and 'callback_id' not in json.dumps(card)


def test_source_links_cannot_escape_frozen_ids_and_callback_values_exact(tmp_path):
    from ostag.briefs.rendering import RenderError
    p = preview()
    with pytest.raises(Exception):
        replace(p, draft=replace(p.draft, links=(SourceLink('unknown', 'https://evil.test'),)))
    with pytest.raises(RenderError):
        render_private(p, callback_id='private JSON body')
    c = controller(tmp_path/'callback.db', [1000])
    token = c.issue(p, message_id='om_message_123')
    card = render_private(p, callback_id=token)
    values = [a['value'] for e in card['elements'] for a in e.get('actions', []) if 'value' in a]
    assert values == [{'callback_id': token}]


def test_real_stored_revision_composes_without_schema_or_text_changes(tmp_path):
    from ostag.briefs.store import BriefStore
    from ostag.briefs.worker import BriefWorker
    from ostag.briefs.models import SourceSet
    from test_brief_service import Authority, event
    authority = Authority()
    authority.destination = DEST
    service = authority.service(BriefStore(tmp_path/'briefs.db'))
    worker = BriefWorker(service)
    try:
        admitted = service.submit(event())
        assert worker.run_once().result(2) == 'draft_ready'
        box = service.store.outbox(SCOPE, admitted.job_id)
        original = box.revision.payload_json
        sources = SourceSet.from_dict(json.loads(box.revision.provenance_json)['sources'])
        p = PrivatePreview.from_outbox(SCOPE, box, BINDING, sources)
        assert 'semantic review required' in json.dumps(render_private(p))
        assert service.store.outbox(SCOPE, admitted.job_id).revision.payload_json == original
    finally:
        worker.stop()
        service.close()


def _process_claim(path, token, queue, start):
    c = controller(path, [1000])
    start.wait(2)
    try:
        invoke(c, token)
        queue.put(True)
    except CallbackError:
        queue.put(False)


def test_two_processes_atomically_claim_one_capability(tmp_path):
    import multiprocessing
    context = multiprocessing.get_context('fork')
    path = tmp_path/'callback.db'
    c = controller(path, [1000])
    token = c.issue(preview(), message_id='om_message_123')
    queue, start = context.Queue(), context.Event()
    processes = [context.Process(target=_process_claim, args=(path, token, queue, start))
                 for _ in range(2)]
    try:
        for process in processes:
            process.start()
        start.set()
        assert sum(queue.get(timeout=3) for _ in processes) == 1
        for process in processes:
            process.join(3)
            assert process.exitcode == 0
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(3)
        queue.close()
        queue.join_thread()


def test_no_reentrant_authority_and_rollback_after_claim(tmp_path, monkeypatch):
    import ostag.briefs.callbacks as callbacks
    now = [1000]
    c = controller(tmp_path/'callback.db', now)
    token = c.issue(preview(), message_id='om_message_123')
    original = callbacks.render_private
    now[0] = 1005
    def render(*args, **kwargs):
        card = original(*args, **kwargs)
        now[0] = 1004
        return card
    monkeypatch.setattr(callbacks, 'render_private', render)
    with pytest.raises(CallbackError):
        invoke(c, token)
    # Claim remains spent after reopening even when wall time catches up.
    now[0] = 1006
    with pytest.raises(CallbackError):
        invoke(controller(tmp_path/'callback.db', now), token)


def test_callback_authorization_denied_after_db_wait_does_not_burn(tmp_path):
    import threading
    import time
    path = tmp_path/'callback.db'
    denied = threading.Event()
    entered = threading.Event()
    def authorize(p):
        if denied.is_set():
            raise PermissionError('PRIVATE')
    c = controller(path, [1000], authorize=authorize)
    token = c.issue(preview(), message_id='om_message_123')
    # Contention at lookup is also an authorization boundary: after waiting for
    # it, the controller must freshly load and authorize before claiming.
    with sqlite3.connect(path) as db:
        db.execute('BEGIN IMMEDIATE')
        def call():
            entered.set()
            with pytest.raises(CallbackError):
                invoke(c, token)
        with ThreadPoolExecutor(1) as pool:
            future = pool.submit(call)
            assert entered.wait(1)
            denied.set()
            time.sleep(0.01)
            db.rollback()
            future.result(1)
    denied.clear()
    assert 'Private draft' in json.dumps(invoke(c, token))


def test_oversized_trusted_link_is_visibly_omitted_without_losing_gap_or_notice():
    p = preview()
    p = replace(p, draft=replace(p.draft,
        links=(SourceLink('s0', 'https://trusted.example/' + '🙂' * 1900),)))
    card = render_private(p)
    raw = json.dumps(card)
    assert len(raw.encode()) <= 16384
    assert 'link omitted' in raw
    assert 'missing page' in raw and 'semantic review required' in raw


def test_all_untrusted_source_labels_and_model_mentions_are_defanged():
    p = preview(text='Ask @all for **review**')
    sources = replace(p.sources, sources=(replace(p.sources.sources[0],
        resource_id='owner@evil.test', resource_version='https://evil.test/<at>'),))
    p = replace(p, sources=sources)
    raw = json.dumps(render_private(p), ensure_ascii=False)
    for active in ('@all', '**review**', 'owner@evil.test', 'https://evil.test', '<at>'):
        assert active not in raw
    assert '⟦U+0040⟧' in raw and 'semantic review required' in raw
