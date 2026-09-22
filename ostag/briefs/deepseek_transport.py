"""Fixed official DeepSeek text-only Chat Completions adapter.

Profile inspected 2026-09-22: https://api-docs.deepseek.com/quick_start/pricing/
https://api-docs.deepseek.com/api/create-chat-completion/
Non-thinking deepseek-flash (a mutable provider alias, NOT an immutable model).
No tools, retries, redirects, proxy/environment discovery or endpoint overrides.
Peak USD rates: input .30, cache .006, output 1.20 per million tokens.
Ledger cost is a conservative peak-price estimate, not an invoice: off-peak
and RMB account billing differ. Missing cache detail uses full input price.

Uses the same bounded HTTPX one-shot egress/reauthorization and cleanup design
as openai_transport. Revocation cannot recall bytes or prove zero remote spend.
Input bytes + 512 framing tokens are conservative admission, not exact counting;
unknown usage quarantines spend and measured overruns are never clamped.
Provider alias/pricing drift requires re-evaluation before continued activation.
"""
from __future__ import annotations

import asyncio
import json
import math
from typing import TYPE_CHECKING

from ostag.briefs.generation import TransportRequest, TransportResponse, _Stopped
from ostag.briefs.models import SchemaError
from ostag.briefs.store import LeaseLost, Usage

if TYPE_CHECKING:
    import httpx

MODEL_ID = "deepseek-flash"
TARIFF_VERSION = "deepseek-flash-peak-usd-2026-09-22"
TARIFF_SOURCE = "https://api-docs.deepseek.com/quick_start/pricing/"
CONTEXT_TOKENS = 1000000
MAX_OUTPUT_TOKENS = 8192
FRAMING_RESERVE = 512
MAX_REQUEST_BYTES = 6 * 1048576 + 16384
MAX_ENVELOPE_BYTES = 8 * 1048576
MAX_COUNT_BYTES = 4096
MAX_CHUNKS = 4096
_ORIGIN = "https://api.deepseek.com"
_STOP_REASONS = frozenset({"checkpoint_denied", "cancelled", "timeout"})
# Public error classes are also constructible by hooks. Their text is NOT
# inherently safe: return only canonical strings from this fixed allowlist.
_DIAGNOSTICS = {message: message for message in (
    "provider transport failed", "fixed transport request required",
    "unsupported provider profile or authorization", "credential in model input",
    "provider request too large", "invalid authorization time",
    "unsupported response encoding", "invalid response size",
    "provider response too large", "invalid provider input count",
    "invalid provider JSON", "invalid provider envelope",
)}


class DeepSeekTransportError(RuntimeError):
    """Adapter emissions contain fixed diagnostics, never upstream/private text."""


def _authorization_reason(error):
    """Retain only domain meaning, never the callback exception or its traceback."""
    if type(error) is _Stopped:
        reason = error.reason
        if type(reason) is str and reason in _STOP_REASONS:
            return reason
    elif type(error) is LeaseLost:
        return "lease_lost"
    elif type(error) is SchemaError:
        return "checkpoint_denied"
    return "provider transport failed"


def _integer(value):
    return type(value) is int and value >= 0


def _seconds(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise DeepSeekTransportError("invalid provider JSON")
        result[key] = value
    return result


def _nonfinite(_):
    raise DeepSeekTransportError("invalid provider JSON")


def _float(value):
    number = float(value)
    if not math.isfinite(number):
        raise DeepSeekTransportError("invalid provider JSON")
    return number


def _json(raw):
    value = json.loads(raw.decode("utf-8"), object_pairs_hook=_object,
                       parse_constant=_nonfinite, parse_float=_float)
    if type(value) is not dict:
        raise DeepSeekTransportError("invalid provider envelope")
    return value


def _usage(data):
    """Conservative peak USD estimate, never the provider invoice or exact charge."""
    if data.get("object") != "chat.completion" or data.get("model") != MODEL_ID:
        return None
    usage = data.get("usage")
    if type(usage) is not dict:
        return None
    required = {"prompt_tokens", "completion_tokens", "total_tokens"}
    optional = {"prompt_cache_hit_tokens", "prompt_cache_miss_tokens",
                "prompt_tokens_details", "completion_tokens_details"}
    if not required <= usage.keys() or usage.keys() - required - optional:
        return None
    i, o, total = (usage[k] for k in ("prompt_tokens", "completion_tokens", "total_tokens"))
    if not all(_integer(n) for n in (i, o, total)) or total != i + o:
        return None
    cached = 0
    if "prompt_cache_hit_tokens" in usage or "prompt_cache_miss_tokens" in usage:
        cached, miss = usage.get("prompt_cache_hit_tokens"), usage.get("prompt_cache_miss_tokens")
        if not _integer(cached) or not _integer(miss) or cached + miss != i:
            return None
    if "prompt_tokens_details" in usage:
        detail = usage["prompt_tokens_details"]
        if (type(detail) is not dict or detail.keys() != {"cached_tokens"}
                or not _integer(detail["cached_tokens"]) or detail["cached_tokens"] > i):
            return None
        if "prompt_cache_hit_tokens" in usage and detail["cached_tokens"] != cached:
            return None
        cached = detail["cached_tokens"]
    if "completion_tokens_details" in usage:
        detail = usage["completion_tokens_details"]
        if (type(detail) is not dict or detail.keys() != {"reasoning_tokens"}
                or not _integer(detail["reasoning_tokens"]) or detail["reasoning_tokens"] > o):
            return None
    return Usage(i, o, ((i - cached) * 300 + cached * 6 + o * 1200 + 999) // 1000)


def _text(data, limit):
    choices = data.get("choices")
    if type(choices) is not list or len(choices) != 1:
        return ""
    choice = choices[0]
    if (type(choice) is not dict or choice.get("finish_reason") != "stop"
            or type(choice.get("index")) is not int or choice["index"] != 0):
        return ""
    message = choice.get("message")
    if (type(message) is not dict or message.get("role") != "assistant"
            or message.keys() - {"role", "content", "reasoning_content"}
            or message.get("reasoning_content") not in (None, "")
            or type(message.get("content")) is not str):
        return ""
    try:
        return message["content"] if len(message["content"].encode()) <= limit else ""
    except UnicodeError:
        return ""


class DeepSeekReadOnlyTransport:
    """Explicit secret plus optional trusted network hook; no environment defaults."""

    def __init__(self, *, api_key: str, transport: httpx.AsyncBaseTransport | None = None):
        if (type(api_key) is not str or not api_key or len(api_key) > 4096
                or any(ord(c) < 33 or ord(c) > 126 for c in api_key)):
            raise DeepSeekTransportError("explicit API key required")
        try:
            import httpx
        except ImportError:
            raise DeepSeekTransportError("install the deepseek optional dependency") from None
        if transport is not None and not isinstance(transport, httpx.AsyncBaseTransport):
            raise DeepSeekTransportError("trusted async transport required")
        self._key = api_key
        self._transport = transport

    async def generate(self, request: TransportRequest) -> TransportResponse:
        import httpx

        # Both callback and network failures cross a sanitizing boundary. Save
        # only allowlisted domain meaning and emit new exceptions after cleanup,
        # outside all handlers, without retaining upstream objects or chains.
        failure = "provider transport failed"
        authorization_reason = None
        try:
            if type(request) is not TransportRequest:
                raise DeepSeekTransportError("fixed transport request required")
            request.__post_init__()
            budget = request.parameters.budget
            if (request.parameters.expected_model_id != MODEL_ID
                    or budget.input_micros_per_token < 1 or budget.output_micros_per_token < 2
                    or budget.max_output_tokens > MAX_OUTPUT_TOKENS
                    or not callable(request.reauthorize)):
                raise DeepSeekTransportError("unsupported provider profile or authorization")
            if self._key in request.serialized_input:
                raise DeepSeekTransportError("credential in model input")
            loop = asyncio.get_running_loop()
            owner = asyncio.current_task()
            deadline = loop.time() + request.remaining_seconds
            # Byte-based admission with explicit framing headroom. This is a
            # conservative local estimate, not a provider token-count receipt.
            estimated = len(request.serialized_input.encode("utf-8")) + FRAMING_RESERVE
            if (estimated > budget.max_input_bytes
                    or estimated + budget.max_output_tokens > CONTEXT_TOKENS):
                raise DeepSeekTransportError("invalid provider input count")
            create = dict(model=MODEL_ID,
                messages=[dict(role="user", content=request.serialized_input)],
                thinking={"type": "disabled"}, response_format={"type": "json_object"},
                stream=False, max_tokens=budget.max_output_tokens)
            body = json.dumps(create, ensure_ascii=True, separators=(",", ":")).encode("ascii")
            if len(body) > MAX_REQUEST_BYTES:
                raise DeepSeekTransportError("provider request too large")

            def check():
                task = asyncio.current_task()
                if (request.cancellation.is_set()
                        or (owner is not None and owner.cancelling())
                        or (task is not None and task.cancelling())):
                    raise asyncio.CancelledError
                if loop.time() >= deadline:
                    raise TimeoutError

            async def run():
                nonlocal deadline
                check()
                async with asyncio.timeout_at(deadline) as timer:
                    async with httpx.AsyncClient(
                        trust_env=False, follow_redirects=False, http2=False,
                        limits=httpx.Limits(max_connections=1, max_keepalive_connections=0),
                        transport=self._transport,
                        headers={"Authorization": "Bearer " + self._key,
                                 "Content-Type": "application/json", "Accept": "application/json",
                                 "Accept-Encoding": "identity"},
                    ) as client:
                        async def post(path, body, bound):
                            client.cookies.clear()  # Explicit bearer auth only.
                            outgoing = client.build_request("POST", _ORIGIN + path, content=body)
                            def authorize_body():
                                nonlocal deadline, authorization_reason
                                check()
                                try:
                                    remaining = request.reauthorize()
                                except Exception as exc:
                                    authorization_reason = _authorization_reason(exc)
                                if authorization_reason is not None:
                                    # Unwind owned resources without carrying the
                                    # callback's object, text, context or traceback.
                                    raise DeepSeekTransportError("provider transport failed")
                                if remaining is not None:
                                    if not _seconds(remaining):
                                        raise DeepSeekTransportError("invalid authorization time")
                                    deadline = min(deadline, loop.time() + remaining)
                                    timer.reschedule(deadline)
                                check()  # catches a stalled synchronous callback too

                            class Body(httpx.AsyncByteStream):
                                consumed = False

                                async def __aiter__(self):
                                    if self.consumed:
                                        raise DeepSeekTransportError("provider transport failed")
                                    self.consumed = True
                                    authorize_body()
                                    yield body

                            async def trace(name, info):
                                if name == "http11.send_request_headers.started":
                                    authorize_body()

                            # Public HTTPX stream boundary: httpcore consumes this
                            # only after pool/DNS/TCP/TLS AND header-write awaits.
                            # Keep the frozen content for trusted test transports;
                            # production IO consumes stream, never Request.content.
                            outgoing.stream = Body()
                            outgoing.extensions["trace"] = trace
                            authorize_body()
                            outgoing.extensions["timeout"] = httpx.Timeout(
                                deadline - loop.time()).as_dict()
                            response = await client.send(outgoing, stream=True)
                            try:
                                check()
                                if response.headers.get("content-encoding", "identity") != "identity":
                                    raise DeepSeekTransportError("unsupported response encoding")
                                length = response.headers.get("content-length")
                                if length is not None and (not length.isascii() or not length.isdigit()
                                                           or len(length) > 10 or int(length) > bound):
                                    raise DeepSeekTransportError("invalid response size")
                                raw, chunks = bytearray(), 0
                                async for chunk in response.aiter_raw():
                                    check()
                                    chunks += 1
                                    if chunks > MAX_CHUNKS or len(raw) + len(chunk) > bound:
                                        raise DeepSeekTransportError("provider response too large")
                                    raw.extend(chunk)
                                check()
                                return response.status_code, _json(raw)
                            finally:
                                await response.aclose()

                        status, data = await post("/chat/completions", body, MAX_ENVELOPE_BYTES)
                        usage = _usage(data)
                        # Only known correct-profile evidence can be priced. Invalid
                        # content still returns usage so A03 can settle actual spend.
                        output = ""
                        if (usage is not None and status == 200
                                and usage.input_tokens <= budget.max_input_bytes
                                and usage.output_tokens <= budget.max_output_tokens):
                            output = _text(data, request.max_output_bytes)
                        if self._key in output:
                            output = ""
                        check()
                        return TransportResponse(
                            output, data["model"] if usage is not None else None, usage)

            check()  # Pending caller cancellation must not spawn private IO.
            call = asyncio.create_task(run())
            cancelled = asyncio.create_task(request.cancellation.wait())
            try:
                done, _ = await asyncio.wait((call, cancelled),
                    timeout=max(0, deadline - loop.time()), return_when=asyncio.FIRST_COMPLETED)
                check()
                if call not in done:
                    raise TimeoutError
                result = call.result()
            finally:
                for task in (call, cancelled):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(call, cancelled, return_exceptions=True)
            check()
            return result
        except asyncio.CancelledError:
            failure = "cancelled"
        except (TimeoutError, httpx.TimeoutException):
            failure = "timeout"
        except DeepSeekTransportError as exc:
            if (type(exc) is DeepSeekTransportError and len(exc.args) == 1
                    and type(exc.args[0]) is str):
                failure = _DIAGNOSTICS.get(exc.args[0], failure)
        except Exception:
            pass
        if authorization_reason in _STOP_REASONS:
            raise _Stopped(authorization_reason)
        if authorization_reason == "lease_lost":
            raise LeaseLost("authorization lease lost")
        if authorization_reason is not None:
            raise DeepSeekTransportError("provider transport failed")
        if failure == "cancelled":
            raise asyncio.CancelledError
        if failure == "timeout":
            raise TimeoutError("provider transport timed out")
        raise DeepSeekTransportError(failure)
