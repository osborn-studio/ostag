"""Independent card-v1 POST transport; never invokes U01 sends or receipt parsing.

Pinned SDK create fields: receive_id, interactive msg_type, content JSON STRING.
Trusted zero-argument CredentialSnapshot resolver independently binds installed
identity, actual sender id/id_type and private owner, rechecked after every await
boundary before IO and acceptance. No token discovery, GET/search/retry/fallback.

Owned HTTPX/httpcore HTTP/1.1 traces gate headers after connection/pool waits.
A one-shot public AsyncByteStream uses the typed service gate's separate body
recheck after header-write waits (plain trusted callbacks support both checks),
without prefetch or HTTP/2 buffering. The bounded chunk then belongs to an in-flight
network write, including backend scheduling and TLS/socket/OS buffers; this is a
local handoff, not atomic remote recall. Cancellation/authority exceptions cannot
be translated into retryable write errors. All outward failures are fixed and context-free.
Only a bounded MockTransport seam is injectable. The service owns the total
monotonic deadline, abandonment and active capacity until this coroutine exits.
No full-card echo or human receipt is inferred from a create acknowledgement.
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict
import hashlib
import json
import math
import re
from typing import TYPE_CHECKING

from ostag.briefs.card_delivery import CardIntent, CardAttempt, CardCreateAck
from ostag.briefs.delivery import _TransportEgressGate
from ostag.briefs.feishu_transport import CredentialSnapshot

if TYPE_CHECKING:
    import httpx

_ORIGIN = 'https://open.feishu.cn/open-apis'
MAX_RESPONSE_BYTES = 65536
MAX_CHUNKS = 256
MAX_REQUEST_BYTES = 65536
_ID = re.compile(r'[A-Za-z0-9_-]{8,256}\Z')
class CardTransportError(RuntimeError):
    """Only fixed diagnostics cross the credential/HTTP boundary."""


class _GateDenied(BaseException):
    pass


def _fail():
    return CardTransportError('Feishu transport unavailable')


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


class CardTransport:
    """Owned one-create HTTP lifecycle; compose once into CardDeliveryService."""
    def __init__(self, *, resolve, transport: httpx.MockTransport | None = None, timeout=5.0):
        import httpx
        if (not callable(resolve) or type(timeout) not in (int, float)
                or not 0 < timeout <= 30 or not math.isfinite(timeout)
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

    async def send(self, attempt: CardAttempt, *, before_egress):
        """Await the service gate at headers and body, after connection/pool waits.

        The async gate owns bounded SQLite retries, then synchronous final local
        authorization. This coroutine sends once; it never retries a failed gate.
        """
        if type(attempt) is not CardAttempt or type(attempt.intent) is not CardIntent:
            raise _fail()
        return await self._exchange(attempt, before_egress)

    async def _exchange(self, attempt, before_egress):
        intent = attempt.intent
        import httpx
        response = client = None
        gate_error = None
        outcome = None
        cancelled = False
        gated = False
        try:
            # Canonical envelope binds the exact frozen card string and private chat.
            body = _json(intent.wire_body)
            if (set(body) != {'receive_id', 'msg_type', 'content'}
                    or body['receive_id'] != intent.destination.address
                    or body['msg_type'] != 'interactive' or type(body['content']) is not str
                    or body['content'] != intent.card_json
                    or len(intent.card_json.encode()) > 16384
                    or hashlib.sha256(intent.wire_body).hexdigest() != intent.digest):
                raise _fail()
            _json(intent.card_json.encode())
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
            async def authority(*, body=False):
                nonlocal gated, gate_error
                check()
                self._snapshot(intent, initial)
                if gated and not body:
                    raise _fail()
                gated = True
                try:
                    if body and type(before_egress) is _TransportEgressGate:
                        await before_egress.before_body()
                    else:
                        await before_egress()
                except BaseException as exc:
                    gate_error = exc
                    raise _GateDenied() from None
                self._snapshot(intent, initial)
                check()
            async def trace(name, info):
                if name == 'http11.send_request_headers.started':
                    await authority()
            inner = self._transport
            if inner is not None:
                class MockBoundary(httpx.AsyncBaseTransport):
                    async def __aenter__(self):
                        await inner.__aenter__()
                        return self

                    async def handle_async_request(self, request):
                        await authority()
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
                request = client.build_request('POST', _ORIGIN + '/im/v1/messages',
                    params={'receive_id_type': 'chat_id'}, content=intent.wire_body)
                if inner is None:
                    class Body(httpx.AsyncByteStream):
                        consumed = False

                        async def __aiter__(self):
                            if self.consumed:
                                raise _fail()
                            self.consumed = True
                            # After the awaited headers, immediately before the
                            # sole bounded private chunk enters the network write.
                            await authority(body=True)
                            yield intent.wire_body
                    request.stream = Body()
                request.extensions['trace'] = trace
                check()
                response = await client.send(request, stream=True)
                if not gated:
                    raise _fail()
                check()
                headers = response.headers.raw
                if (len(headers) > 64 or sum(len(k) + len(v) for k, v in headers) > 16384
                        or len({k.lower() for k, v in headers}) != len(headers)):
                    raise _fail()
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
                if length is not None and int(length) != len(raw):
                    raise _fail()
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
            raise _fail()
        if cancelled:
            raise asyncio.CancelledError()
        result = None
        try:
            if outcome is not None:
                check()
                status, data = outcome
                snapshot = self._snapshot(intent, initial)
                result = self._ack(attempt, snapshot, status, data)
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
    def _ack(attempt, snapshot, status, data):
        if status != 200 or type(data.get('code')) is not int or data['code'] != 0:
            return None
        value = data.get('data')
        if type(value) is not dict:
            return None
        candidate = value.get('message_id')
        sender = value.get('sender')
        if (type(candidate) is not str or not _ID.fullmatch(candidate)
                or value.get('msg_type') != 'interactive'
                or value.get('chat_id') != attempt.intent.destination.address
                or value.get('deleted') is not False or value.get('updated') is not False
                or type(sender) is not dict or sender.get('sender_type') != 'app'
                or sender.get('id') != snapshot.sender_id
                or sender.get('id_type') != snapshot.sender_id_type
                or sender.get('tenant_key') != snapshot.binding.tenant_id):
            return None
        # No full card echo is required or fabricated. This proof belongs only
        # to this create attempt, not U01 receipt acceptance or later observation.
        return CardCreateAck(attempt.owner, attempt.token, attempt.intent.digest, candidate,
                             snapshot.binding, value['chat_id'], snapshot.sender_id,
                             snapshot.sender_id_type, CardTransport._credential_digest(snapshot))

    @staticmethod
    def _credential_digest(snapshot):
        # Ephemeral integrity binding; token and ownership map are never stored in
        # the ledger/receipt. Generation alone does not excuse silent token change.
        return hashlib.sha256(json.dumps(asdict(snapshot), sort_keys=True,
            separators=(',', ':'), ensure_ascii=True).encode()).hexdigest()

    def check_ack(self, attempt, ack):
        """Bounded local check after ledger waits, immediately before ACK commit."""
        valid = False
        try:
            snapshot = self._snapshot(attempt.intent)
            valid = (type(ack) is CardCreateAck
                and ack.credential_digest == self._credential_digest(snapshot)
                and ack.sender_id == snapshot.sender_id
                and ack.sender_id_type == snapshot.sender_id_type)
        except Exception:
            pass
        if not valid:
            raise _fail()
