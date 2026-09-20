"""Bootstrap inert capabilities and original-message epoch navigation."""
from dataclasses import replace
import json

import pytest

from ostag.briefs.card_delivery import CardDeliveryError
from ostag.briefs.validation import Coverage
from ostag.work_monitor.identity import Destination
from test_brief_card_delivery import ACTOR, SCOPE, make_service, preview, tokens


def invoke(c, token, **changes):
    return c.handle_callback(token, **(dict(principal=ACTOR,
        destination=Destination('feishu', 'oc_private', 'private'), tenant_id='tenant',
        app_id='app', message_id='om_message_123') | changes))


async def test_inert_before_ack_wrong_message_no_burn_epoch_and_no_replay(tmp_path):
    p = replace(preview(Coverage(False, tuple('gap ' + str(i) + '密' * 900 for i in range(10)))),
                outbox_id='original_outbox')
    c = make_service(tmp_path/'cards.db', current=[p])
    first = await c.prepare(SCOPE, 'job', 'revision')
    original = c.ledger.frozen(SCOPE, first.delivery_id)
    controls = tokens(json.loads(original.card_json))
    with pytest.raises(CardDeliveryError):
        invoke(c, controls['Next'])
    await c.send(SCOPE, first.delivery_id)
    with pytest.raises(CardDeliveryError):
        invoke(c, controls['Next'], message_id='om_other_123')
    second = invoke(c, controls['Next'])
    assert 'Page 2 of' in json.dumps(second)
    with pytest.raises(CardDeliveryError):
        invoke(c, controls['Refresh'])
    third = invoke(c, tokens(second)['Refresh'])
    assert 'Page 2 of' in json.dumps(third)
    previous = invoke(c, tokens(third)['Previous'])
    assert 'Page 1 of' in json.dumps(previous)
    assert 'semantic review required' in json.dumps(previous)
    assert original == c.ledger.frozen(SCOPE, first.delivery_id)


async def test_lost_callback_response_stays_spent_after_restart(tmp_path):
    path = tmp_path/'cards.db'
    c = make_service(path)
    first = await c.deliver(SCOPE, 'job', 'revision')
    token = tokens(json.loads(c.ledger.frozen(SCOPE, first.delivery_id).card_json))['Refresh']
    invoke(c, token)  # Response lost; the displayed old button cannot recover it.
    with pytest.raises(CardDeliveryError):
        invoke(make_service(path), token)


async def test_unchanged_revision_changed_digest_denies_without_burning(tmp_path):
    current = [replace(preview(), outbox_id='original_outbox')]
    c = make_service(tmp_path/'cards.db', current=current)
    first = await c.deliver(SCOPE, 'job', 'revision')
    token = tokens(json.loads(c.ledger.frozen(SCOPE, first.delivery_id).card_json))['Refresh']
    old = current[0]
    current[0] = replace(preview(text='Substituted body'), outbox_id='original_outbox')
    with pytest.raises(CardDeliveryError):
        invoke(c, token)
    current[0] = old
    assert 'Private draft' in json.dumps(invoke(c, token))


def signed_app(tmp_path, service):
    from fastapi import FastAPI
    from ostag.work_monitor.feishu_card_security import FeishuCardSecurity
    from ostag.work_monitor.feishu_routes import create_feishu_card_router
    from ostag.work_monitor.identity import IdentityStore
    from ostag.work_monitor.server import SecurityContract, register_feishu_card_router
    from ostag.work_monitor.store import MonitorStore
    store = MonitorStore(tmp_path/'monitor.db')
    IdentityStore(store.path).bind_private(owner=ACTOR,
        destination=Destination('feishu', 'oc_private', 'private'), local_admin=True)
    app = FastAPI()
    register_feishu_card_router(app,
        create_feishu_card_router(object(), brief_cards=service),
        security=SecurityContract('verified_platform', FeishuCardSecurity(
            encrypt_key='encrypt-test', verification_token='verify', expected_app_id='app',
            store=store, clock=lambda: 1000)))
    return app


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
async def test_actual_signed_callback_bad_identity_no_burn(tmp_path, mutation):
    from fastapi.testclient import TestClient
    from test_brief_cards import signed_post
    c = make_service(tmp_path/'cards.db')
    first = await c.deliver(SCOPE, 'job', 'revision')
    token = tokens(json.loads(c.ledger.frozen(SCOPE, first.delivery_id).card_json))['Refresh']
    with TestClient(signed_app(tmp_path, c)) as client:
        bad = signed_post(client, token, mutate=mutation)
        assert 'Private draft' not in bad.text and 'trusted.example' not in bad.text
        good = signed_post(client, token)
        assert good.status_code == 200 and 'Private draft' in good.text
        assert 'Private draft' not in signed_post(client, token).text


async def test_signed_disabled_unsigned_inert_and_expiry(tmp_path):
    from fastapi.testclient import TestClient
    from test_brief_cards import signed_post
    now = [1000]
    c = make_service(tmp_path/'cards.db', now=now)
    first = await c.prepare(SCOPE, 'job', 'revision', ttl=1)
    token = tokens(json.loads(c.ledger.frozen(SCOPE, first.delivery_id).card_json))['Refresh']
    with TestClient(signed_app(tmp_path, c)) as client:
        assert signed_post(client, token, signed=False).status_code == 401
        assert 'card' not in signed_post(client, token).json()
    await c.send(SCOPE, first.delivery_id)
    with TestClient(signed_app(tmp_path, None)) as client:
        assert 'card' not in signed_post(client, token).json()
    now[0] = 1001
    with TestClient(signed_app(tmp_path, c)) as client:
        assert 'card' not in signed_post(client, token).json()
    now[0] = 1000
    with TestClient(signed_app(tmp_path, make_service(tmp_path/'cards.db', now=now))) as client:
        assert 'card' not in signed_post(client, token).json()


async def test_same_epoch_different_buttons_concurrency_one_response(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from fastapi.testclient import TestClient
    from test_brief_cards import signed_post
    path = tmp_path/'cards.db'
    p = replace(preview(Coverage(False, tuple('gap ' + str(i) + '密'*900 for i in range(10)))),
                outbox_id='original_outbox')
    c = make_service(path, current=[p])
    first = await c.deliver(SCOPE, 'job', 'revision')
    buttons = tokens(json.loads(c.ledger.frozen(SCOPE, first.delivery_id).card_json))
    clients = [TestClient(signed_app(tmp_path, svc))
               for svc in (c, make_service(path, current=[p]))]
    with clients[0], clients[1], ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(signed_post, client, token)
                   for client, token in zip(clients, (buttons['Next'], buttons['Refresh']))]
        assert sum('card' in f.result().json() for f in futures) == 1


@pytest.mark.parametrize('revoke', ['grant', 'clock', 'cancel'])
async def test_final_render_denial_stays_spent_across_restart(tmp_path, monkeypatch, revoke):
    import ostag.briefs.card_delivery as module
    path, now, denied = tmp_path/'cards.db', [1000], [False]
    def authorize(p):
        if denied[0]:
            raise PermissionError('PRIVATE SOURCE DETAILS')
    c = make_service(path, now=now, authorize=authorize)
    first = await c.deliver(SCOPE, 'job', 'revision')
    token = tokens(json.loads(c.ledger.frozen(SCOPE, first.delivery_id).card_json))['Refresh']
    original = module._controls
    def render(*args, **kwargs):
        result = original(*args, **kwargs)
        if revoke == 'grant':
            denied[0] = True
        elif revoke == 'clock':
            now[0] = 1300
        else:
            c.stop()
        return result
    monkeypatch.setattr(module, '_controls', render)
    with pytest.raises(CardDeliveryError) as caught:
        invoke(c, token)
    assert caught.value.__context__ is caught.value.__cause__ is None
    monkeypatch.setattr(module, '_controls', original)
    denied[0], now[0] = False, 1301
    with pytest.raises(CardDeliveryError):
        invoke(make_service(path, now=now), token)


async def test_all_pages_bounded_all_gaps_and_adversarial_text(tmp_path):
    p = replace(preview(Coverage(False, tuple(f'gap {i} ' + '密'*900 for i in range(40))),
                        text='Ask @all for **review**'), outbox_id='original_outbox')
    c = make_service(tmp_path/'cards.db', current=[p], timeout=5)
    first = await c.deliver(SCOPE, 'job', 'revision')
    card = json.loads(c.ledger.frozen(SCOPE, first.delivery_id).card_json)
    raws = []
    for _ in range(200):
        raw = json.dumps(card)
        raws.append(raw)
        assert len(raw.encode()) <= 16384
        assert 'semantic review required' in raw and '40 coverage gaps' in raw
        assert '@all' not in raw and '**review**' not in raw
        buttons = tokens(card)
        if 'Next' not in buttons:
            break
        card = invoke(c, buttons['Next'])
    else:
        pytest.fail('unbounded pagination')
    assert all(f'gap {i} ' in ''.join(raws) for i in range(40))


async def test_full_actual_a04_first_http_ack_signed_navigation_original_text_unchanged(
        tmp_path, monkeypatch):
    import httpx
    from fastapi.testclient import TestClient
    from ostag.adapters.feishu import FeishuAdapter
    from ostag.briefs.delivery import DeliveryIntent
    from ostag.briefs.models import SourceSet
    from ostag.briefs.rendering import PrivatePreview
    from ostag.briefs.store import BriefStore
    from ostag.briefs.validation import SourceLink
    from ostag.briefs.worker import BriefWorker
    import ostag.briefs.worker as worker_module
    from ostag.config import Settings
    from test_brief_card_delivery import BINDING, ack
    from test_brief_cards import signed_post
    from test_brief_service import Authority, ManualRequest, event, request

    authority = Authority()
    authority.destination = preview().destination
    briefs = authority.service(BriefStore(tmp_path/'briefs.db'))
    worker = BriefWorker(briefs)
    actual_generate = worker_module.generate
    trusted_links = (SourceLink('s0', 'https://trusted.example/source'),)
    async def trusted_collection_generate(**kwargs):
        # The unchanged A04 worker calls the real A03 generator. This local
        # trusted connector seam supplies links; model output cannot supply them.
        return await actual_generate(**kwargs, links=trusted_links)
    monkeypatch.setattr(worker_module, 'generate', trusted_collection_generate)
    try:
        body = request()
        body['coverage'] = Coverage(False, tuple('gap '+str(i)+' '+'x'*40 for i in range(10))).to_dict()
        admitted = briefs.submit(event(body))
        assert worker.run_once().result(3) == 'draft_ready'
        original = briefs.store.outbox(SCOPE, admitted.job_id)
        text = DeliveryIntent.from_outbox(SCOPE, original, BINDING)
        def load(scope, job):
            box = briefs.store.outbox(scope, job)
            assert box == original  # Original revision, destination and cancellation mirror.
            sources = SourceSet.from_dict(json.loads(box.revision.provenance_json)['sources'])
            return PrivatePreview.from_outbox(scope, box, BINDING, sources, links=trusted_links)
        p = load(SCOPE, admitted.job_id)
        calls = []
        def handler(r):
            calls.append(r)
            return httpx.Response(200, json=ack())
        c = make_service(tmp_path/'cards.db', current=[p], handler=handler)
        c._load = load
        def authorize(p):
            briefs.authorize_current(ManualRequest.from_dict(body), p.scope, p.destination,
                                     stopped=lambda: False)
        c._authorize = authorize
        def forbidden(*args, **kwargs):
            pytest.fail('legacy send/reply invoked')
        adapter = FeishuAdapter(Settings(), brief_card_service=c)
        for name in ('send_card', 'send_message', 'reply'):
            monkeypatch.setattr(adapter, name, forbidden)
        first = await adapter.deliver_brief_card(SCOPE, admitted.job_id, p.revision_id)
        assert first.state == 'create_acknowledged' and len(calls) == 1
        card = json.loads(c.ledger.frozen(SCOPE, first.delivery_id).card_json)
        with TestClient(signed_app(tmp_path, c)) as client:
            for action, page in (('Next', 2), ('Refresh', 2), ('Previous', 1)):
                response = signed_post(client, tokens(card)[action])
                assert response.status_code == 200 and 'card' in response.json()
                card = response.json()['card']['data']
                assert f'Page {page} of' in json.dumps(card)
        assert briefs.store.outbox(SCOPE, admitted.job_id) == original
        assert DeliveryIntent.from_outbox(SCOPE, original, BINDING) == text
        assert c.ledger.frozen(SCOPE, first.delivery_id).delivery_id != text.delivery_id
    finally:
        worker.stop()
        briefs.close()


async def test_epoch_bound_and_pruning_cannot_reactivate_original_tokens(tmp_path):
    import sqlite3
    c = make_service(tmp_path/'cards.db')
    first = await c.deliver(SCOPE, 'job', 'revision')
    old = tokens(json.loads(c.ledger.frozen(SCOPE, first.delivery_id).card_json))['Refresh']
    card = invoke(c, old)
    fresh = tokens(card)['Refresh']
    with sqlite3.connect(c.ledger.path) as db:
        assert db.execute('SELECT count(*) FROM card_tokens').fetchone()[0] <= 3
        db.execute('UPDATE cards SET epoch=1024')
        db.execute('UPDATE card_tokens SET epoch=1024')
    for token in (old, fresh):
        with pytest.raises(CardDeliveryError):
            invoke(c, token)
    with sqlite3.connect(c.ledger.path) as db:
        assert db.execute('SELECT epoch FROM cards').fetchone()[0] == 1024


def test_render_navigation_page_and_longest_identity_bounds():
    from ostag.briefs.rendering import RenderError, render_navigation, render_private
    p = preview(Coverage(False, tuple(str(i)+'密'*900 for i in range(10))))
    for page in (True, -1, 65536, 1.0):
        with pytest.raises(RenderError):
            render_navigation(p, page=page)
    with pytest.raises(RenderError):
        render_navigation(p, controls={'Refresh': 'br_' + 'a'*32})
    # Existing worst-case metadata may exhaust the conservative 16KiB bound;
    # navigation must preserve the legacy refusal, never silently truncate.
    from test_brief_validation import evidence
    from ostag.briefs.validation import SourceLink
    p = replace(preview(Coverage(False, ('_'*28,))), job_id='_'*128, revision_id='-'*128,
                sources=evidence(count=3))
    p = replace(p, draft=replace(p.draft, links=tuple(
        SourceLink('s'+str(i), 'https://trusted.example/'+'密'*800) for i in range(2))))
    with pytest.raises(RenderError):
        render_private(p, callback_id='br_'+'a'*64)
    with pytest.raises(RenderError):
        render_navigation(p, controls={'Refresh': 'bc_'+'a'*32, 'Next': 'bc_'+'b'*32})
