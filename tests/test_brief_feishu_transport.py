"""Real HTTPX boundary tests; no network credentials or live sends."""
import asyncio
from dataclasses import replace
import json

import httpx
import pytest

from ostag.briefs.delivery import DeliveryIntent, FeishuBinding, SendAttempt, receipt, wire_text
from ostag.briefs.feishu_transport import CredentialSnapshot, FeishuTransport, FeishuTransportError
from ostag.briefs.store import PrivateDestination, RequesterContext
from ostag.core.identity import Principal

ACTOR = Principal('feishu', 'ou_alice')
SCOPE = RequesterContext('company', ACTOR)
BINDING = FeishuBinding('tenant', 'installation', 'cli_app', 'bot_identity', 1)
INTENT = DeliveryIntent(SCOPE, 'job', 'revision', 'delivery',
                        PrivateDestination(ACTOR, 'oc_private'), BINDING,
                        wire_text('{"progress":"Awaiting review"}'))
MID = 'om_message_12345'


def credentials(**changes):
    return CredentialSnapshot(**(dict(binding=BINDING, access_token='secret-token',
        sender_id='actual_sender_id', sender_id_type='app_id',
        private_owners=(('oc_private', ACTOR),)) | changes))


def resource(**changes):
    return dict(message_id=MID, msg_type='text', chat_id='oc_private', deleted=False,
                updated=False, sender=dict(id='actual_sender_id', id_type='app_id',
                sender_type='app', tenant_key='tenant'),
                body={'content': json.dumps(json.loads(INTENT.wire_body)['content'])}) | changes


async def test_exact_create_and_get_body_and_explicit_sender_mapping():
    calls, gates = [], []
    def handler(request):
        calls.append(request)
        assert len(gates) == len(calls)
        assert request.url.host == 'open.feishu.cn'
        assert request.headers['authorization'] == 'Bearer secret-token'
        if request.method == 'POST':
            body = json.loads(request.content)
            assert dict(request.url.params) == {'receive_id_type': 'chat_id'}
            assert body['receive_id'] == 'oc_private'
            assert body['msg_type'] == 'text'
            assert json.loads(body['content']) == json.loads(INTENT.wire_body)['content']
            return httpx.Response(200, json={'code': 0, 'data': resource()})
        assert request.url.path == '/open-apis/im/v1/messages/' + MID
        assert not request.url.query
        return httpx.Response(200, json={'code': 0, 'data': {'items': [resource()]}})
    transport = FeishuTransport(resolve=credentials, transport=httpx.MockTransport(handler))
    sent = await transport.send(SendAttempt(INTENT, 'attempt-secret'),
                                before_egress=lambda: gates.append(1))
    assert sent.attempt_token == 'attempt-secret'
    assert receipt(INTENT, MID, sent.messages)
    proof = await transport.observe(INTENT, MID, before_egress=lambda: gates.append(1))
    assert receipt(INTENT, MID, proof)
    assert len(calls) == 2
    assert 'secret-token' not in repr(credentials())
    assert 'Awaiting review' not in repr(sent)


@pytest.mark.parametrize('changes', [
    {'chat_id': 'oc_group'}, {'message_id': 'wrong'}, {'deleted': True}, {'updated': True},
    {'deleted': 0}, {'updated': None}, {'msg_type': 'interactive'}, {'sender': {}},
    {'sender': dict(id='cli_app', id_type='app_id', sender_type='app', tenant_key='tenant')},
    {'sender': dict(id='actual_sender_id', id_type='open_id', sender_type='app', tenant_key='tenant')},
    {'sender': dict(id='actual_sender_id', id_type='app_id', sender_type='user', tenant_key='tenant')},
    {'sender': dict(id='actual_sender_id', id_type='app_id', sender_type='app', tenant_key='other')},
    {'body': {'content': '{"text":"different"}'}}, {'body': {}},
])
async def test_incomplete_or_wrong_resource_is_never_proof(changes):
    t = FeishuTransport(resolve=credentials, transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={'code': 0, 'data': resource(**changes)})))
    result = await t.send(SendAttempt(INTENT, 'token'), before_egress=lambda: None)
    assert result.messages == ()


@pytest.mark.parametrize('snapshot', [
    credentials(binding=replace(BINDING, generation=2)),
    credentials(binding=replace(BINDING, tenant_id='other')),
    credentials(private_owners=()),
    credentials(private_owners=(('oc_private', Principal('feishu', 'ou_bob')),)),
])
async def test_independent_binding_and_private_owner_required(snapshot):
    calls = []
    t = FeishuTransport(resolve=lambda: snapshot, transport=httpx.MockTransport(
        lambda r: calls.append(r)))
    with pytest.raises(FeishuTransportError):
        await t.send(SendAttempt(INTENT, 'token'), before_egress=lambda: None)
    assert not calls


async def test_egress_gate_exception_is_identical_and_cannot_send():
    calls = []
    error = RuntimeError('private gate detail')
    def deny():
        raise error
    t = FeishuTransport(resolve=credentials, transport=httpx.MockTransport(
        lambda r: calls.append(r)))
    with pytest.raises(RuntimeError) as caught:
        await t.send(SendAttempt(INTENT, 'token'), before_egress=deny)
    assert caught.value is error
    assert calls == []


async def test_rotation_during_response_invalidates_proof():
    current = [credentials()]
    async def handler(request):
        await asyncio.sleep(0)
        current[0] = credentials(binding=replace(BINDING, generation=2))
        return httpx.Response(200, json={'code': 0, 'data': resource()})
    t = FeishuTransport(resolve=lambda: current[0], transport=httpx.MockTransport(handler))
    with pytest.raises(FeishuTransportError):
        await t.send(SendAttempt(INTENT, 'token'), before_egress=lambda: None)


@pytest.mark.parametrize('text', ['https://evil.test', '<at user_id="all">all</at>',
                                  'name@example.org', '[click](evil.test)', 'www.evil.test'])
async def test_unsafe_frozen_text_rejected_without_modification(text):
    calls = []
    t = FeishuTransport(resolve=credentials, transport=httpx.MockTransport(
        lambda r: calls.append(r)))
    with pytest.raises(FeishuTransportError):
        await t.send(SendAttempt(replace(INTENT, wire_body=wire_text(text)), 'token'),
                     before_egress=lambda: None)
    assert calls == []


@pytest.mark.parametrize('payload', [
    b'{"code":0,"code":0}', b'{"code":NaN}', b'{"code":1e999}', b'\xff',
    b'[]', b'{} trailing', b'{' + b' ' * 65536 + b'}',
])
async def test_strict_response_json_and_byte_limit(payload):
    t = FeishuTransport(resolve=credentials, transport=httpx.MockTransport(
        lambda r: httpx.Response(200, content=payload)))
    with pytest.raises(FeishuTransportError) as caught:
        await t.send(SendAttempt(INTENT, 'token'), before_egress=lambda: None)
    assert caught.value.__context__ is None and caught.value.__cause__ is None


@pytest.mark.parametrize('code', [True, False, '0', None, 1])
async def test_provider_code_is_exact_integer_zero(code):
    t = FeishuTransport(resolve=credentials, transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={'code': code, 'data': resource()})))
    result = await t.send(SendAttempt(INTENT, 'token'), before_egress=lambda: None)
    assert result.message_id is None and result.messages == ()


@pytest.mark.parametrize('data', [
    {'items': []}, {'items': [resource(), resource()]},
    {'items': [resource()], 'has_more': False}, {'items': [resource()], 'page_token': ''},
    {'items': [resource(message_id='om_another_message')]},
])
async def test_get_exactly_one_candidate_no_pagination(data):
    t = FeishuTransport(resolve=credentials, transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={'code': 0, 'data': data})))
    assert await t.observe(INTENT, MID, before_egress=lambda: None) == ()


@pytest.mark.parametrize('headers', [
    {'Content-Encoding': 'gzip'}, {'Content-Length': '65537'},
    {'Content-Length': '-1'}, {'Content-Length': '9999999999999999999'},
])
async def test_invalid_response_framing_refused(headers):
    t = FeishuTransport(resolve=credentials, transport=httpx.MockTransport(
        lambda r: httpx.Response(200, content=b'{}', headers=headers)))
    with pytest.raises(FeishuTransportError):
        await t.send(SendAttempt(INTENT, 'token'), before_egress=lambda: None)


async def test_redirects_and_provider_failure_have_one_request_no_cookie_or_fallback(monkeypatch):
    monkeypatch.setenv('HTTPS_PROXY', 'https://malicious.proxy.test')
    calls = []
    def handler(r):
        calls.append(r)
        assert 'cookie' not in r.headers
        return httpx.Response(307, headers={'Location': 'https://evil.test',
                                             'Set-Cookie': 'private=secret'}, json={})
    t = FeishuTransport(resolve=credentials, transport=httpx.MockTransport(handler))
    result = await t.send(SendAttempt(INTENT, 'token'), before_egress=lambda: None)
    assert result.messages == () and len(calls) == 1


async def test_resolver_errors_client_errors_and_cleanup_errors_sanitized():
    secret = 'SECRET upstream private body'
    def deny():
        raise ValueError(secret)
    for resolve, handler in ((deny, lambda r: httpx.Response(200, json={})),
                              (credentials, lambda r: deny())):
        t = FeishuTransport(resolve=resolve, transport=httpx.MockTransport(handler))
        with pytest.raises(FeishuTransportError) as caught:
            await t.send(SendAttempt(INTENT, 'token'), before_egress=lambda: None)
        assert secret not in repr(caught.value)
        assert caught.value.__context__ is None and caught.value.__cause__ is None


async def test_revocation_after_client_enter_wait_precedes_final_gate():
    current, calls, gates = [credentials()], [], []
    class Transport(httpx.MockTransport):
        async def __aenter__(self):
            await asyncio.sleep(0)
            current[0] = credentials(private_owners=())
            return self
    t = FeishuTransport(resolve=lambda: current[0], transport=Transport(lambda r: calls.append(r)))
    with pytest.raises(FeishuTransportError):
        await t.send(SendAttempt(INTENT, 'token'), before_egress=lambda: gates.append(1))
    assert calls == gates == []


async def test_revocation_during_cleanup_refuses_proof():
    current = [credentials()]
    class Transport(httpx.MockTransport):
        async def aclose(self):
            await asyncio.sleep(0)
            current[0] = credentials(private_owners=())
    t = FeishuTransport(resolve=lambda: current[0], transport=Transport(
        lambda r: httpx.Response(200, json={'code': 0, 'data': resource()})))
    with pytest.raises(FeishuTransportError):
        await t.send(SendAttempt(INTENT, 'token'), before_egress=lambda: None)


async def test_timeout_and_cancellation_close_owned_resources_without_retry():
    for cancel in (False, True):
        entered, closed = asyncio.Event(), asyncio.Event()
        calls = []
        async def handler(request):
            calls.append(request)
            entered.set()
            await asyncio.Event().wait()
        class Transport(httpx.MockTransport):
            async def aclose(self):
                closed.set()
        t = FeishuTransport(resolve=credentials, transport=Transport(handler), timeout=0.02)
        task = asyncio.create_task(t.send(SendAttempt(INTENT, 'token'), before_egress=lambda: None))
        await entered.wait()
        if cancel:
            task.cancel()
        with pytest.raises(asyncio.CancelledError if cancel else FeishuTransportError):
            await asyncio.wait_for(task, 1)
        assert closed.is_set() and len(calls) == 1


async def test_chunk_limit_closes_stream():
    closed = []
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            for _ in range(257):
                yield b' '
        async def aclose(self):
            closed.append(True)
    t = FeishuTransport(resolve=credentials, transport=httpx.MockTransport(
        lambda r: httpx.Response(200, stream=Stream())))
    with pytest.raises(FeishuTransportError):
        await t.send(SendAttempt(INTENT, 'token'), before_egress=lambda: None)
    assert closed


@pytest.mark.parametrize("error_kind", ["runtime", "core_write", "httpx_write"])
async def test_real_httpcore_gate_after_connection_wait_and_unwrapped_write_error(
        monkeypatch, error_kind):
    """Exercise HTTPX's real HTTP/1.1 path without sockets, using httpcore's backend."""
    import httpcore
    writes, gates = [], []
    original = httpx.AsyncHTTPTransport
    expected = {'runtime': RuntimeError, 'core_write': httpcore.WriteError,
                'httpx_write': httpx.WriteError}[error_kind]('final gate denied')
    class Stream(httpcore.AsyncMockStream):
        async def write(self, buffer, timeout=None):
            writes.append(buffer)
            await super().write(buffer, timeout)
    stream = Stream([b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}'])
    class Backend(httpcore.AsyncMockBackend):
        async def connect_tcp(self, *args, **kwargs):
            await asyncio.sleep(0)
            assert not gates
            return stream
    def factory(**kwargs):
        transport = original(**kwargs)
        transport._pool._network_backend = Backend([])
        return transport
    monkeypatch.setattr(httpx, 'AsyncHTTPTransport', factory)
    def gate():
        gates.append(1)
        raise expected
    with pytest.raises(type(expected)) as caught:
        await FeishuTransport(resolve=credentials).send(SendAttempt(INTENT, 'token'),
                                                        before_egress=gate)
    assert caught.value is expected
    assert gates == [1] and writes == []


async def test_actual_u01_service_composes_with_httpx_and_preserves_canonical_revision(tmp_path):
    from ostag.briefs.delivery import DeliveryService
    from ostag.briefs.store import BriefStore, WorkerContext
    from ostag.briefs.worker import BriefWorker
    from test_brief_service import Authority, SCOPE as source_scope, event
    authority = Authority()
    authority.destination = PrivateDestination(source_scope.principal, 'oc_private')
    briefs = authority.service(BriefStore(tmp_path/'brief.db'))
    worker = BriefWorker(briefs)
    try:
        admitted = briefs.submit(event())
        assert worker.run_once().result(2) == 'draft_ready'
        box = briefs.store.outbox(source_scope, admitted.job_id)
        intent = DeliveryIntent.from_outbox(source_scope, box, BINDING)
        requests = []
        def handler(request):
            requests.append(request)
            content = json.loads(request.content)['content']
            assert json.loads(content)['text'] == box.revision.payload_json
            return httpx.Response(200, json={'code': 0, 'data': resource(body={'content': content})})
        snapshot = credentials(private_owners=(('oc_private', source_scope.principal),))
        transport = FeishuTransport(resolve=lambda: snapshot, transport=httpx.MockTransport(handler))
        briefs.store.enable_delivery()
        delivery = DeliveryService(store=briefs.store, worker=WorkerContext('delivery'),
            transport=transport, authorize=lambda i: None, clock=briefs.clock)
        await delivery.prepare(intent)
        result = await delivery.send(source_scope, intent.delivery_id)
        assert result.state == 'provider_accepted' and result.human_received is False
        assert len(requests) == 1
        assert (await delivery.send(source_scope, intent.delivery_id)).state == 'provider_accepted'
        assert len(requests) == 1
    finally:
        worker.stop()
        briefs.close()
