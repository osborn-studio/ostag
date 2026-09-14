"""Pinned A04 text-only Responses HTTP library adapter (no SDK/Backend.run).

Profile/tariff version: openai-gpt-4.1-standard-2026-09-14, inspected 2026-09-14 UTC.
Sources:
https://developers.openai.com/api/docs/models/gpt-4.1
https://developers.openai.com/api/docs/guides/token-counting
https://developers.openai.com/api/reference/typescript/resources/responses/subresources/input_tokens
https://www.python-httpx.org/async/

Only gpt-4.1-2025-04-14, standard/default tier, text input/output is audited.
Context 1,047,576; output maximum 32,768. Public list tariff in microUSD/token:
Uncached input 2, cached input 0.5, output 8. Usage.cost_micros is the integer
ceiling of observed tokens at this tariff, NOT an invoice or billing guarantee.
Absent optional cached detail assumes uncached; present invalid detail cannot
provide a discount. Future profiles/tariffs require explicit revalidation before
deployment. Measured overruns are never clamped to admission estimates.

The count endpoint sends private data and counts role/boundary framing using
exactly the same input-affecting payload as create. Its cost is not asserted to
be zero. A03 starts its reservation before transport entry: any preflight failure
may leave unknown spend/quarantine. No zero-cost refund is inferred here; a future
separately reviewed preflight/ledger split would be needed to improve recovery.
This includes local profile/configuration rejection before any egress. Unknown
model/tier evidence also stays unpriced and requires trusted manual reconciliation.

The mandatory trusted synchronous reauthorize callback owns current grant/source/
ACL, lease, charged UTC day and original wall deadline checks. Its blocking DB
work MUST precede its final authorization; after that only clock/cancel checks.
A returned positive finite remaining time only shrinks the original monotonic
whole-call deadline. None leaves it unchanged. This is not model-editable data.
Callback exceptions may contain private material. Only recognized domain signals
cross the boundary as NEW fixed exceptions: checkpoint_denied/cancelled/timeout,
LeaseLost, and SchemaError mapped to checkpoint_denied. Arbitrary messages,
unknown reasons and original exception chains never cross this boundary.

Clients and responses are owned per call, avoiding cross-loop client reuse. The
optional AsyncBaseTransport hook is trusted internal code, transferred to the
client for closure; it must cooperate with cancellation/close like httpx's real
transport. No arbitrary client, endpoint, auth/config discovery, proxy, redirect,
retry, tools, files, conversation, background processing or response storage.
Cancellation closes local resources; it cannot establish remote termination.

HTTP bodies are capped at 6 MiB + 16 KiB (1 MiB input escaped at worst 6x plus
fixed framing); response envelopes at 8 MiB before UTF-8 decoding/JSON allocation,
count at 4 KiB, and each response at 4096 network chunks. Compression is explicitly
refused, including unsolicited encoding, so decompression cannot allocate an
unbounded envelope. Extracted model text has the separate frozen byte limit.
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

MODEL_ID = "gpt-4.1-2025-04-14"
TARIFF_VERSION = "openai-gpt-4.1-standard-2026-09-14"
TARIFF_SOURCE = "https://developers.openai.com/api/docs/models/gpt-4.1"
CONTEXT_TOKENS = 1047576
MAX_OUTPUT_TOKENS = 32768
MAX_REQUEST_BYTES = 6 * 1048576 + 16384
MAX_ENVELOPE_BYTES = 8 * 1048576
MAX_COUNT_BYTES = 4096
MAX_CHUNKS = 4096
_ORIGIN = "https://api.openai.com/v1"
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


class OpenAITransportError(RuntimeError):
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
            raise OpenAITransportError("invalid provider JSON")
        result[key] = value
    return result


def _nonfinite(_):
    raise OpenAITransportError("invalid provider JSON")


def _float(value):
    number = float(value)
    if not math.isfinite(number):
        raise OpenAITransportError("invalid provider JSON")
    return number


def _json(raw):
    value = json.loads(raw.decode("utf-8"), object_pairs_hook=_object,
                       parse_constant=_nonfinite, parse_float=_float)
    if type(value) is not dict:
        raise OpenAITransportError("invalid provider envelope")
    return value


def _usage(data):
    """Unknown pricing/modality/identity evidence never becomes expected usage."""
    if (data.get("object") != "response" or data.get("model") != MODEL_ID
            or data.get("service_tier") != "default"):
        return None
    usage = data.get("usage")
    required = {"input_tokens", "output_tokens", "total_tokens"}
    optional = {"input_tokens_details", "output_tokens_details"}
    if (type(usage) is not dict or not required <= usage.keys()
            or usage.keys() - required - optional):
        return None
    i, o, total = (usage[k] for k in ("input_tokens", "output_tokens", "total_tokens"))
    if not all(_integer(n) for n in (i, o, total)) or total != i + o:
        return None
    cached = 0
    if "input_tokens_details" in usage:
        detail = usage["input_tokens_details"]
        if type(detail) is not dict or detail.keys() != {"cached_tokens"}:
            return None
        cached = detail["cached_tokens"]
        if not _integer(cached) or cached > i:
            return None
    if "output_tokens_details" in usage:
        detail = usage["output_tokens_details"]
        if (type(detail) is not dict or detail.keys() != {"reasoning_tokens"}
                or not _integer(detail["reasoning_tokens"]) or detail["reasoning_tokens"] != 0):
            return None
    return Usage(i, o, ((i - cached) * 4 + cached + o * 16 + 1) // 2)


def _text(data, limit):
    if (data.get("status") != "completed" or data.get("error") is not None
            or data.get("incomplete_details") is not None):
        return ""
    output = data.get("output")
    if type(output) is not list or len(output) != 1:
        return ""
    message = output[0]
    if (type(message) is not dict or message.get("type") != "message"
            or message.get("role") != "assistant" or message.get("status") != "completed"
            or message.keys() - {"type", "id", "role", "status", "content"}):
        return ""
    content = message.get("content")
    if type(content) is not list or not content:
        return ""
    parts, size = [], 0
    for block in content:
        if (type(block) is not dict or block.get("type") != "output_text"
                or type(block.get("text")) is not str
                or block.keys() - {"type", "text", "annotations", "logprobs"}
                or block.get("annotations", []) != [] or block.get("logprobs", []) != []):
            return ""
        try:
            size += len(block["text"].encode("utf-8"))
        except UnicodeError:
            return ""
        if size > limit:
            return ""
        parts.append(block["text"])
    return "".join(parts)


class OpenAIReadOnlyTransport:
    """Explicit secret plus optional trusted network hook; no environment defaults."""

    def __init__(self, *, api_key: str, transport: httpx.AsyncBaseTransport | None = None):
        if (type(api_key) is not str or not api_key or len(api_key) > 4096
                or any(ord(c) < 33 or ord(c) > 126 for c in api_key)):
            raise OpenAITransportError("explicit API key required")
        try:
            import httpx
        except ImportError:
            raise OpenAITransportError("install the openai optional dependency") from None
        if transport is not None and not isinstance(transport, httpx.AsyncBaseTransport):
            raise OpenAITransportError("trusted async transport required")
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
                raise OpenAITransportError("fixed transport request required")
            request.__post_init__()
            budget = request.parameters.budget
            if (request.parameters.expected_model_id != MODEL_ID
                    or budget.input_micros_per_token < 2 or budget.output_micros_per_token < 8
                    or budget.max_output_tokens > MAX_OUTPUT_TOKENS
                    or not callable(request.reauthorize)):
                raise OpenAITransportError("unsupported provider profile or authorization")
            if self._key in request.serialized_input:
                raise OpenAITransportError("credential in model input")
            loop = asyncio.get_running_loop()
            owner = asyncio.current_task()
            deadline = loop.time() + request.remaining_seconds
            fixed = dict(model=MODEL_ID, input=request.serialized_input, tools=[],
                         tool_choice="none", parallel_tool_calls=False, truncation="disabled",
                         text={"format": {"type": "text"}})
            create = fixed | dict(store=False, background=False, stream=False,
                                  max_output_tokens=budget.max_output_tokens, service_tier="default")
            bodies = [json.dumps(p, ensure_ascii=True, separators=(",", ":")).encode("ascii")
                      for p in (fixed, create)]
            if any(len(body) > MAX_REQUEST_BYTES for body in bodies):
                raise OpenAITransportError("provider request too large")

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
                        trust_env=False, follow_redirects=False,
                        limits=httpx.Limits(max_connections=1, max_keepalive_connections=0),
                        transport=self._transport,
                        headers={"Authorization": "Bearer " + self._key,
                                 "Content-Type": "application/json", "Accept": "application/json",
                                 "Accept-Encoding": "identity"},
                    ) as client:
                        async def post(path, body, bound):
                            nonlocal deadline, authorization_reason
                            # Build before the final gate. No DB or scheduling await
                            # follows authorization before entering actual HTTP IO.
                            client.cookies.clear()  # Explicit bearer auth only, even after count.
                            outgoing = client.build_request("POST", _ORIGIN + path, content=body)
                            check()
                            try:
                                remaining = request.reauthorize()
                            except Exception as exc:
                                authorization_reason = _authorization_reason(exc)
                            if authorization_reason is not None:
                                # Unwind owned resources without carrying the
                                # callback's object, text, context or traceback.
                                raise OpenAITransportError("provider transport failed")
                            if remaining is not None:
                                if not _seconds(remaining):
                                    raise OpenAITransportError("invalid authorization time")
                                deadline = min(deadline, loop.time() + remaining)
                                timer.reschedule(deadline)
                            check()  # catches a stalled synchronous callback too
                            outgoing.extensions["timeout"] = httpx.Timeout(
                                deadline - loop.time()).as_dict()
                            response = await client.send(outgoing, stream=True)
                            try:
                                check()
                                if response.headers.get("content-encoding", "identity") != "identity":
                                    raise OpenAITransportError("unsupported response encoding")
                                length = response.headers.get("content-length")
                                if length is not None and (not length.isascii() or not length.isdigit()
                                                           or len(length) > 10 or int(length) > bound):
                                    raise OpenAITransportError("invalid response size")
                                raw, chunks = bytearray(), 0
                                async for chunk in response.aiter_raw():
                                    check()
                                    chunks += 1
                                    if chunks > MAX_CHUNKS or len(raw) + len(chunk) > bound:
                                        raise OpenAITransportError("provider response too large")
                                    raw.extend(chunk)
                                check()
                                return response.status_code, _json(raw)
                            finally:
                                await response.aclose()

                        status, count = await post("/responses/input_tokens", bodies[0], MAX_COUNT_BYTES)
                        n = count.get("input_tokens")
                        if (status != 200 or count.keys() != {"object", "input_tokens"}
                                or count.get("object") != "response.input_tokens" or not _integer(n)
                                or n > budget.max_input_bytes
                                or n + budget.max_output_tokens > CONTEXT_TOKENS):
                            raise OpenAITransportError("invalid provider input count")
                        status, data = await post("/responses", bodies[1], MAX_ENVELOPE_BYTES)
                        usage = _usage(data)
                        # Only known correct-profile evidence can be priced. Invalid
                        # content still returns usage so A03 can settle actual spend.
                        body = ""
                        if usage is not None and status == 200 and usage.input_tokens == n:
                            body = _text(data, request.max_output_bytes)
                        if self._key in body:
                            body = ""
                        check()
                        return TransportResponse(
                            body, data["model"] if usage is not None else None, usage)

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
        except OpenAITransportError as exc:
            if (type(exc) is OpenAITransportError and len(exc.args) == 1
                    and type(exc.args[0]) is str):
                failure = _DIAGNOSTICS.get(exc.args[0], failure)
        except Exception:
            pass
        if authorization_reason in _STOP_REASONS:
            raise _Stopped(authorization_reason)
        if authorization_reason == "lease_lost":
            raise LeaseLost("authorization lease lost")
        if authorization_reason is not None:
            raise OpenAITransportError("provider transport failed")
        if failure == "cancelled":
            raise asyncio.CancelledError
        if failure == "timeout":
            raise TimeoutError("provider transport timed out")
        raise OpenAITransportError(failure)
