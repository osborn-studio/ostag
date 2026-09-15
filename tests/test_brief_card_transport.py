"""Real owned HTTP stack; synthetic credentials and no network."""
import json

import httpx
import pytest

from ostag.briefs.card_delivery import CardDeliveryError
from test_brief_card_delivery import SCOPE, ack, make_service


async def test_exact_interactive_wire_and_no_echo_required(tmp_path, monkeypatch):
    monkeypatch.setenv('HTTPS_PROXY', 'https://invalid.example')
    requests = []
    def handler(r):
        requests.append(r)
        assert str(r.url) == ('https://open.feishu.cn/open-apis/im/v1/messages'
                              '?receive_id_type=chat_id')
        assert r.method == 'POST' and 'cookie' not in r.headers
        body = json.loads(r.content)
        assert set(body) == {'receive_id', 'msg_type', 'content'}
        assert body['receive_id'] == 'oc_private' and body['msg_type'] == 'interactive'
        assert type(body['content']) is str and 'trusted.example' in body['content']
        return httpx.Response(200, json=ack())
    c = make_service(tmp_path/'cards.db', handler=handler)
    assert (await c.deliver(SCOPE, 'job', 'revision')).state == 'create_acknowledged'
    assert len(requests) == 1


@pytest.mark.parametrize('change', [
    {'chat_id': 'oc_group'}, {'message_id': True}, {'message_id': 'bad'},
    {'deleted': 0}, {'updated': None}, {'msg_type': 'text'}, {'sender': {}},
    {'sender': dict(id='app', id_type='app_id', sender_type='app', tenant_key='tenant')},
    {'sender': dict(id='actual_sender', id_type='open_id', sender_type='app', tenant_key='tenant')},
    {'sender': dict(id='actual_sender', id_type='app_id', sender_type='user', tenant_key='tenant')},
    {'sender': dict(id='actual_sender', id_type='app_id', sender_type='app', tenant_key='other')},
])
async def test_wrong_ack_is_unknown(tmp_path, change):
    c = make_service(tmp_path/'cards.db', handler=lambda r: httpx.Response(200, json=ack(**change)))
    assert (await c.deliver(SCOPE, 'job', 'revision')).state == 'unknown'


@pytest.mark.parametrize('raw', [b'{"code":false}', b'{"code":0,"code":0}', b'{"code":NaN}',
    b'{"code":1e999}', b'\xff', b'[]', b'{} trailing', b' ' * 65537])
async def test_bounded_strict_response_and_fixed_diagnostics(tmp_path, raw):
    c = make_service(tmp_path/'cards.db', handler=lambda r: httpx.Response(200, content=raw))
    assert (await c.deliver(SCOPE, 'job', 'revision')).state == 'unknown'


async def test_authority_error_is_private(tmp_path):
    def deny(p):
        raise ValueError('SECRET PRIVATE SOURCE')
    c = make_service(tmp_path/'cards.db', authorize=deny)
    with pytest.raises(CardDeliveryError) as caught:
        await c.deliver(SCOPE, 'job', 'revision')
    assert caught.value.__context__ is caught.value.__cause__ is None
    assert 'SECRET' not in repr(caught.value)


@pytest.mark.parametrize('revoke', ['grant', 'binding', 'cancel'])
async def test_actual_httpcore_connection_wait_gate_prevents_private_writes(tmp_path, monkeypatch, revoke):
    import asyncio
    from dataclasses import replace
    import httpcore
    from ostag.briefs.card_transport import CardTransport
    from test_brief_card_delivery import BINDING
    writes, denied, current, did = [], [False], [BINDING], [None]
    c = make_service(tmp_path/'cards.db')
    c.transport = CardTransport(resolve=c.transport._resolve)
    def authorize(p):
        if denied[0]:
            raise httpcore.WriteError('SECRET GRANT')
    c._authorize = authorize
    original = httpx.AsyncHTTPTransport
    class Stream(httpcore.AsyncMockStream):
        async def write(self, buffer, timeout=None):
            writes.append(buffer)
            await super().write(buffer, timeout)
    stream = Stream([b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}'])
    class Backend(httpcore.AsyncMockBackend):
        async def connect_tcp(self, *args, **kwargs):
            await asyncio.sleep(0)
            if revoke == 'grant':
                denied[0] = True
            elif revoke == 'binding':
                current[0] = replace(BINDING, generation=2)
            else:
                make_service(tmp_path/'cards.db').cancel(SCOPE, did[0])
            return stream
    def factory(**kwargs):
        t = original(**kwargs)
        t._pool._network_backend = Backend([])
        return t
    monkeypatch.setattr(httpx, 'AsyncHTTPTransport', factory)
    c._binding = lambda: current[0]
    first = await c.prepare(SCOPE, 'job', 'revision')
    did[0] = first.delivery_id
    assert (await c.send(SCOPE, first.delivery_id)).state == 'unknown'
    assert writes == []


async def test_actual_httpcore_success_and_revoke_between_headers_body(tmp_path, monkeypatch):
    import asyncio
    import httpcore
    from ostag.briefs.card_transport import CardTransport
    original = httpx.AsyncHTTPTransport
    for revoke in (False, True):
        writes, denied = [], [False]
        c = make_service(tmp_path/('revoke.db' if revoke else 'success.db'))
        c.transport = CardTransport(resolve=c.transport._resolve)
        def authorize(p):
            if denied[0]:
                raise PermissionError('private grant revoked')
        c._authorize = authorize
        payload = json.dumps(ack()).encode()
        class Stream(httpcore.AsyncMockStream):
            async def write(self, buffer, timeout=None):
                writes.append(buffer)
                await asyncio.sleep(0)
                if revoke:
                    denied[0] = True
                await super().write(buffer, timeout)
        stream = Stream([b'HTTP/1.1 200 OK\r\nContent-Length: ' + str(len(payload)).encode()
                         + b'\r\nContent-Type: application/json\r\n\r\n' + payload])
        class Backend(httpcore.AsyncMockBackend):
            async def connect_tcp(self, *args, **kwargs):
                await asyncio.sleep(0)
                return stream
        def factory(**kwargs):
            t = original(**kwargs)
            t._pool._network_backend = Backend([])
            return t
        monkeypatch.setattr(httpx, 'AsyncHTTPTransport', factory)
        result = await c.deliver(SCOPE, 'job', 'revision')
        if revoke:
            assert result.state == 'unknown' and len(writes) == 1
            assert b'Private draft' not in b''.join(writes)
        else:
            assert result.state == 'create_acknowledged'
            assert b'Private draft' in b''.join(writes)


@pytest.mark.parametrize('when', ['enter', 'response', 'cleanup'])
async def test_independent_credential_rotation_after_await_denies(tmp_path, when):
    import asyncio
    from dataclasses import replace
    from ostag.briefs.card_transport import CardTransport
    c = make_service(tmp_path/'cards.db')
    snapshot = [c.transport._resolve()]
    calls = []
    def revoke():
        snapshot[0] = replace(snapshot[0], private_owners=())
    async def handler(r):
        calls.append(r)
        await asyncio.sleep(0)
        if when == 'response':
            revoke()
        return httpx.Response(200, json=ack())
    class Transport(httpx.MockTransport):
        async def __aenter__(self):
            await asyncio.sleep(0)
            if when == 'enter':
                revoke()
            return self
        async def aclose(self):
            await asyncio.sleep(0)
            if when == 'cleanup':
                revoke()
    c.transport = CardTransport(resolve=lambda: snapshot[0], transport=Transport(handler))
    assert (await c.deliver(SCOPE, 'job', 'revision')).state == 'unknown'
    assert len(calls) == (0 if when == 'enter' else 1)


@pytest.mark.parametrize('headers', [
    [('Content-Length', '65537')], [('Content-Length', '-1')],
    [('Content-Length', '2'), ('Content-Length', '2')],
    [('Content-Encoding', 'gzip')], [('X-Private', 'x'*16385)],
    [('X-'+str(i), 'v') for i in range(65)],
])
async def test_response_headers_bounded_and_unambiguous(tmp_path, headers):
    c = make_service(tmp_path/'cards.db', handler=lambda r:
        httpx.Response(200, content=b'{}', headers=headers))
    assert (await c.deliver(SCOPE, 'job', 'revision')).state == 'unknown'


async def test_chunks_and_resources_bounded(tmp_path):
    closed = []
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            for _ in range(257):
                yield b' '
        async def aclose(self):
            closed.append(True)
    c = make_service(tmp_path/'cards.db', handler=lambda r: httpx.Response(200, stream=Stream()))
    assert (await c.deliver(SCOPE, 'job', 'revision')).state == 'unknown'
    assert closed


async def test_rotation_inside_final_egress_authority_prevents_send(tmp_path):
    from dataclasses import replace
    from ostag.briefs.card_transport import CardTransport
    calls = []
    c = make_service(tmp_path/'cards.db')
    current = [c.transport._resolve()]
    c.transport = CardTransport(resolve=lambda: current[0], transport=httpx.MockTransport(
        lambda r: (calls.append(r), httpx.Response(200, json=ack()))[1]))
    first = await c.prepare(SCOPE, 'job', 'revision')
    original = c.ledger.egress
    def gate(*args, **kwargs):
        original(*args, **kwargs)
        current[0] = replace(current[0], access_token='rotated-secret')
    c.ledger.egress = gate
    assert (await c.send(SCOPE, first.delivery_id)).state == 'unknown'
    assert calls == []


async def test_credential_rotation_after_response_before_ack_commit_denied(tmp_path):
    from dataclasses import replace
    from ostag.briefs.card_transport import CardTransport
    c = make_service(tmp_path/'cards.db')
    current = [c.transport._resolve()]
    c.transport = CardTransport(resolve=lambda: current[0], transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json=ack())))
    first = await c.prepare(SCOPE, 'job', 'revision')
    original = c.ledger.acknowledge
    def rotate(*args, **kwargs):
        current[0] = replace(current[0], access_token='rotated-secret')
        return original(*args, **kwargs)
    c.ledger.acknowledge = rotate
    assert (await c.send(SCOPE, first.delivery_id)).state == 'unknown'
