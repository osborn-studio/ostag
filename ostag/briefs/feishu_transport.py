"""Explicit HTTPX implementation of U01's frozen TEXT transport, not card delivery.

Pinned Feishu create/get fields follow the supplied lark-oapi SDK source: POST
/im/v1/messages?receive_id_type=chat_id has receive_id,msg_type,content (JSON
string); GET /im/v1/messages/{persisted_candidate} has data.items. uuid is omitted;
U01's durable claim, never provider dedup retention, fences retransmission.

A zero-argument trusted LOCAL resolver independently supplies the installed
binding, explicit actual sender id/id_type mapping, token and private chat owners.
It must be bounded/nonblocking/non-reentrant and return immutable snapshots; no
network refresh, environment discovery or user-supplied proof. It is re-read after
resource acquisition, at egress, after response/cleanup awaits and before proof.

HTTP/1.1 only, owned clients/transports, no proxies/env/cookies/redirects/retries.
The httpcore send_request_headers.started trace gate runs after connection/pool
awaits, before HTTP headers; body tracing rechecks local rotation/cancellation.
A private BaseException sentinel prevents HTTPX/httpcore swallowing or translating
ANY final authority exception; the exact exception is restored after cleanup.
MockTransport is the sole injected test seam and gates at handle_async_request.
No caller-controlled URL/client. DNS/TLS setup precedes HTTP authority and carries
no private request bytes. IO already in progress cannot be recalled after revoke.

Unsafe frozen text (link/mention/markup activation syntax) is rejected, never
rewritten. This can make original U01 JSON drafts containing trusted URLs
undeliverable as TEXT. Separately authorized cards handle previews; a versioned
card outbox and actual platform/device certification remain integration work.
Missing SDK evidence never defaults safe; acceptance is not human receipt/read.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import json
import math
import re
from typing import TYPE_CHECKING

from ostag.briefs.delivery import (
    DeliveryIntent, FeishuBinding, ProviderMessage, SendAttempt, SendResponse, wire_text,
)
from ostag.core.identity import Principal

if TYPE_CHECKING:
    import httpx

_ORIGIN = 'https://open.feishu.cn/open-apis'
MAX_RESPONSE_BYTES = 65536
MAX_CHUNKS = 256
MAX_REQUEST_BYTES = 65536
_ID = re.compile(r'[A-Za-z0-9_-]{8,256}\Z')
# Fail closed even on bare domains, email, HTML, Markdown and obfuscated Unicode
# punctuation. It is intentionally stricter than A03's structural prose filter.
_UNSAFE = re.compile(r'[@<>`*]|\]\s*[(:]|://|//|www\.|\b[^\W_]+\.[^\W\d_]{2,}\b|'
                     r'\b(?:mailto|data|javascript|file|tel):', re.I)


class FeishuTransportError(RuntimeError):
    """Only fixed diagnostics cross the credential/HTTP boundary."""


class _GateDenied(BaseException):
    pass


def _fail():
    return FeishuTransportError('Feishu transport unavailable')


@dataclass(frozen=True)
class CredentialSnapshot:
    binding: FeishuBinding
    access_token: str = field(repr=False)
    sender_id: str
    sender_id_type: str
    private_owners: tuple[tuple[str, Principal], ...] = field(repr=False)

    def __post_init__(self):
        if (type(self.binding) is not FeishuBinding or type(self.access_token) is not str
                or not 1 <= len(self.access_token) <= 4096
                or any(not 33 <= ord(c) <= 126 for c in self.access_token)
                or type(self.sender_id) is not str
                or not re.fullmatch(r'[A-Za-z0-9_-]{1,256}', self.sender_id)
                or self.sender_id_type not in ('app_id', 'open_id', 'user_id', 'union_id')
                or type(self.private_owners) is not tuple or len(self.private_owners) > 10000):
            raise _fail()
        seen = set()
        for item in self.private_owners:
            if (type(item) is not tuple or len(item) != 2 or type(item[0]) is not str
                    or not re.fullmatch(r'oc_[A-Za-z0-9_-]{1,250}', item[0])
                    or item[0] in seen or type(item[1]) is not Principal
                    or item[1].platform != 'feishu'):
                raise _fail()
            seen.add(item[0])


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise _fail()
        result[key] = value
    return result


def _constant(value):
    raise _fail()


def _float(value):
    result = float(value)
    if not math.isfinite(result):
        raise _fail()
    return result


def _json(raw):
    if type(raw) is not bytes or len(raw) > MAX_RESPONSE_BYTES:
        raise _fail()
    result = json.loads(raw.decode('utf-8'), object_pairs_hook=_pairs,
                        parse_constant=_constant, parse_float=_float)
    if type(result) is not dict:
        raise _fail()
    return result


def _wire(intent):
    import unicodedata
    if type(intent) is not DeliveryIntent:
        raise _fail()
    value = _json(intent.wire_body)
    if (set(value) != {'msg_type', 'content'} or value['msg_type'] != 'text'
            or type(value['content']) is not dict or set(value['content']) != {'text'}
            or type(value['content']['text']) is not str
            or wire_text(value['content']['text']) != intent.wire_body):
        raise _fail()
    text = value['content']['text']
    # Decode the frozen original revision JSON too: escaped <at>/URLs must not
    # hide behind JSON escapes, even though the transmitted original stays exact.
    checks = [text]
    try:
        nested = _json(text.encode('utf-8'))
    except Exception:
        nested = None
    def collect(value, depth=0):
        if depth > 64:
            raise _fail()
        if type(value) is str:
            checks.append(value)
        elif type(value) is dict:
            for k, v in value.items():
                collect(k, depth+1)
                collect(v, depth+1)
        elif type(value) is list:
            for v in value:
                collect(v, depth+1)
    collect(nested)
    for candidate in checks:
        normalized = unicodedata.normalize('NFKC', candidate)
        if (_UNSAFE.search(normalized)
                or any(unicodedata.category(c) in ('Cf', 'Cs') for c in normalized)):
            raise _fail()
    return value['content']


class FeishuTransport:
    """Implements delivery.FeishuTransport; compose into DeliveryService explicitly."""
    def __init__(self, *, resolve, transport: httpx.MockTransport | None = None, timeout=5.0):
        import httpx
        if (not callable(resolve) or type(timeout) not in (int, float)
                or not math.isfinite(timeout) or not 0 < timeout <= 30
                or (transport is not None and not isinstance(transport, httpx.MockTransport))):
            raise _fail()
        self._resolve, self._transport, self.timeout = resolve, transport, timeout

    def _snapshot(self, intent, original=None):
        snapshot = self._resolve()  # No requested identity is passed as alleged proof.
        if (type(snapshot) is not CredentialSnapshot or snapshot.binding != intent.binding
                or dict(snapshot.private_owners).get(intent.destination.address)
                != intent.scope.principal or not intent.destination.address.startswith('oc_')
                or (original is not None and snapshot != original)):
            raise _fail()
        return snapshot

    async def send(self, attempt: SendAttempt, *, before_egress):
        if type(attempt) is not SendAttempt:
            raise _fail()
        result = await self._exchange(attempt.intent, None, before_egress)
        return SendResponse(attempt.token, result[0], result[1])

    async def observe(self, intent: DeliveryIntent, message_id: str, *, before_egress):
        """Internal U01 API: message_id MUST be the persisted direct-response candidate."""
        if type(message_id) is not str or not _ID.fullmatch(message_id):
            raise _fail()
        result = await self._exchange(intent, message_id, before_egress)
        return result[1]

    async def _exchange(self, intent, message_id, before_egress):
        import httpx
        response = client = None
        gate_error = None
        outcome = None
        cancelled = False
        gated = False
        try:
            content = _wire(intent)
            initial = self._snapshot(intent)
            if not callable(before_egress) or initial.access_token in intent.wire_body.decode('utf-8'):
                raise _fail()
            loop = asyncio.get_running_loop()
            deadline = loop.time() + self.timeout
            def check():
                if asyncio.current_task().cancelling():
                    raise asyncio.CancelledError()
                if loop.time() >= deadline:
                    raise TimeoutError()
            def authority():
                nonlocal gated, gate_error
                check()
                self._snapshot(intent, initial)
                if gated:
                    raise _fail()
                gated = True
                try:
                    before_egress()
                except BaseException as exc:
                    gate_error = exc
                    raise _GateDenied() from None
                check()
            async def trace(name, info):
                if name == 'http11.send_request_headers.started':
                    authority()
                elif name == 'http11.send_request_body.started':
                    check()
                    self._snapshot(intent, initial)
            inner = self._transport
            if inner is not None:
                class MockBoundary(httpx.AsyncBaseTransport):
                    async def __aenter__(self):
                        await inner.__aenter__()
                        return self

                    async def handle_async_request(self, request):
                        authority()
                        return await inner.handle_async_request(request)

                    async def aclose(self):
                        await inner.aclose()
                boundary = MockBoundary()
            else:
                boundary = httpx.AsyncHTTPTransport(retries=0, trust_env=False, http2=False,
                    limits=httpx.Limits(max_connections=1, max_keepalive_connections=0))
            async with asyncio.timeout(self.timeout):
                client = httpx.AsyncClient(transport=boundary, trust_env=False,
                    follow_redirects=False, timeout=self.timeout, http2=False,
                    headers={'Authorization': 'Bearer ' + initial.access_token,
                             'Accept': 'application/json', 'Accept-Encoding': 'identity',
                             'Content-Type': 'application/json'})
                await client.__aenter__()
                self._snapshot(intent, initial)
                client.cookies.clear()
                if message_id is None:
                    body = json.dumps(dict(receive_id=intent.destination.address,
                        msg_type='text', content=json.dumps(content, ensure_ascii=False,
                        separators=(',', ':'))), ensure_ascii=False, separators=(',', ':')).encode()
                    if len(body) > MAX_REQUEST_BYTES:
                        raise _fail()
                    request = client.build_request('POST', _ORIGIN + '/im/v1/messages',
                        params={'receive_id_type': 'chat_id'}, content=body)
                else:
                    request = client.build_request('GET', _ORIGIN + '/im/v1/messages/' + message_id)
                request.extensions['trace'] = trace
                check()
                response = await client.send(request, stream=True)
                if not gated:
                    raise _fail()
                check()
                if response.headers.get('content-encoding', 'identity') != 'identity':
                    raise _fail()
                length = response.headers.get('content-length')
                if length is not None and (len(length) > 10 or not length.isascii()
                    or not length.isdigit() or int(length) > MAX_RESPONSE_BYTES):
                    raise _fail()
                raw, chunks = bytearray(), 0
                # MockTransport's content/json responses are already consumed;
                # real HTTP responses use the raw bounded stream (no decompression).
                if response.is_stream_consumed:
                    if len(response.content) > MAX_RESPONSE_BYTES:
                        raise _fail()
                    raw.extend(response.content)
                else:
                    async for chunk in response.aiter_raw():
                        check()
                        chunks += 1
                        if chunks > MAX_CHUNKS or len(raw) + len(chunk) > MAX_RESPONSE_BYTES:
                            raise _fail()
                        raw.extend(chunk)
                check()
                data = _json(bytes(raw))
                self._snapshot(intent, initial)
                outcome = (response.status_code, data)
        except _GateDenied:
            pass
        except asyncio.CancelledError:
            cancelled = True
        except Exception:
            pass
        finally:
            # No shield/detached cleanup task. Owned real HTTPX resources cooperate
            # with cancellation; hostile synchronous custom hooks are unsupported.
            try:
                async with asyncio.timeout(0.25):
                    if response is not None:
                        await response.aclose()
                    if client is not None:
                        await client.aclose()
            except asyncio.CancelledError:
                cancelled = True
                outcome = None
            except Exception:
                outcome = None
        if gate_error is not None:
            raise gate_error
        if cancelled:
            raise asyncio.CancelledError()
        result = None
        try:
            if outcome is not None:
                check()
                status, data = outcome
                snapshot = self._snapshot(intent, initial)
                result = self._proof(intent, snapshot, status, data, message_id)
                # Parsing/normalization is local; current authorization again is
                # the last infrastructure operation before releasing evidence.
                self._snapshot(intent, initial)
                check()
        except Exception:
            result = None
        if result is None:
            raise _fail()
        return result

    @staticmethod
    def _proof(intent, snapshot, status, data, message_id):
        if status != 200 or type(data.get('code')) is not int or data['code'] != 0:
            return None, ()
        value = data.get('data')
        if message_id is not None:
            if (type(value) is not dict or set(value) != {'items'}
                    or type(value['items']) is not list or len(value['items']) != 1):
                return None, ()
            value = value['items'][0]
        if type(value) is not dict:
            return None, ()
        candidate = value.get('message_id')
        if type(candidate) is not str or not _ID.fullmatch(candidate):
            return None, ()
        if message_id is not None and candidate != message_id:
            return None, ()
        proof = ()
        try:
            sender, body = value.get('sender'), value.get('body')
            if (value.get('msg_type') != 'text' or value.get('chat_id') != intent.destination.address
                    or value.get('deleted') is not False or value.get('updated') is not False
                    or type(sender) is not dict or sender.get('sender_type') != 'app'
                    or sender.get('id') != snapshot.sender_id
                    or sender.get('id_type') != snapshot.sender_id_type
                    or sender.get('tenant_key') != snapshot.binding.tenant_id
                    or type(body) is not dict or type(body.get('content')) is not str):
                return candidate, ()
            full = _json(body['content'].encode('utf-8'))
            if set(full) != {'text'} or type(full['text']) is not str:
                return candidate, ()
            normalized = wire_text(full['text'])
            if normalized == intent.wire_body:
                proof = (ProviderMessage(candidate, snapshot.binding, value['chat_id'],
                                         normalized, 'private', False, False),)
        except Exception:
            pass
        return candidate, proof
