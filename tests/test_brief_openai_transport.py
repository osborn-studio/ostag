"""A04 HTTP boundary tests: synthetic traffic, real httpx streams and SQLite.

No credential, live model, billing invoice, semantic or platform acceptance.
"""
import asyncio
from dataclasses import fields, replace
import json

import httpx
import pytest

from ostag.briefs.generation import Budget, GenerationConfig, Parameters, TransportRequest, Usage
from ostag.briefs.store import BriefStore
from ostag.briefs.worker import BriefWorker
from test_brief_generation import FakeTransport, at
from test_brief_service import Authority, SCOPE, event
from test_brief_validation import output

MODEL = "gpt-4.1-2025-04-14"
SECRET = "synthetic-secret-not-a-credential"


def parameters(**changes):
    budget = Budget(max_job_micros=100000, max_input_bytes=8000, max_output_bytes=16000,
                    max_output_tokens=1000, input_micros_per_token=2,
                    output_micros_per_token=8, max_calls=1, timeout_seconds=180)
    return Parameters(MODEL, replace(budget, **changes))


def request(**changes):
    p = changes.pop("parameters", parameters())
    return TransportRequest(**(dict(serialized_input='{"private":"材料","tools":["evil"]}',
        parameters=p, max_output_bytes=p.budget.max_output_bytes, remaining_seconds=1.0,
        cancellation=asyncio.Event(), charged_day="2026-09-13", deadline=at(180),
        reauthorize=lambda: None) | changes))


def envelope(**changes):
    return dict(object="response", id="resp_synthetic", model=MODEL, status="completed",
        service_tier="default", error=None, incomplete_details=None,
        output=[dict(type="message", id="msg_synthetic", role="assistant", status="completed",
                     content=[dict(type="output_text", text=json.dumps(output()),
                                   annotations=[], logprobs=[])])],
        usage=dict(input_tokens=100, output_tokens=50, total_tokens=150,
                   input_tokens_details={"cached_tokens": 3},
                   output_tokens_details={"reasoning_tokens": 0})) | changes


class Stream(httpx.AsyncByteStream):
    def __init__(self, chunks, stall=False):
        self.chunks, self.stall, self.closed = chunks, stall, False
        self.entered = asyncio.Event()

    async def __aiter__(self):
        self.entered.set()
        for chunk in self.chunks:
            yield chunk
        if self.stall:
            await asyncio.Event().wait()

    async def aclose(self):
        self.closed = True


class Wire(httpx.AsyncBaseTransport):
    """Only the network boundary is fake; client/build/send/stream are real."""
    def __init__(self, result=None, count=100, effect=None):
        self.result = envelope() if result is None else result
        self.count, self.effect = count, effect
        self.calls, self.streams = [], []
        self.closed = False

    async def handle_async_request(self, req):
        self.calls.append(req)
        if self.effect:
            response = await self.effect(req)
            if response is not None:
                return response
        data = ({"object": "response.input_tokens", "input_tokens": self.count}
                if req.url.path.endswith("input_tokens") else self.result)
        stream = Stream([json.dumps(data).encode()])
        self.streams.append(stream)
        return httpx.Response(200, stream=stream, headers={"content-type": "application/json"})

    async def aclose(self):
        self.closed = True


def adapter(wire):
    from ostag.briefs.openai_transport import OpenAIReadOnlyTransport
    return OpenAIReadOnlyTransport(api_key=SECRET, transport=wire)


async def test_generator_supplies_private_reauthorization_callback(tmp_path):
    authority = Authority()
    seen = []
    async def inspect(req):
        callback = getattr(req, "reauthorize", None)
        assert callable(callback), "each HTTP egress needs a trusted current authorization seam"
        seen.append(callback())
        field = next(f for f in fields(req) if f.name == "reauthorize")
        assert not field.repr and not field.compare
    service = authority.service(BriefStore(tmp_path / "brief.db"),
                                transport=FakeTransport(effect=inspect))
    service.submit(event())
    worker = BriefWorker(service)
    try:
        assert await asyncio.wrap_future(worker.run_once()) == "draft_ready"
        assert len(seen) == 1 and 0 < seen[0] <= 180
    finally:
        worker.stop()
        service.close()


@pytest.mark.parametrize("at_call", [0, 1])
async def test_generator_checkpoint_lease_loss_keeps_domain_cost_reason(tmp_path, at_call):
    from ostag.briefs.generation import generate
    from ostag.briefs.store import LeaseLost
    from ostag.briefs.validation import Coverage
    from test_brief_generation import WORKER, admit
    from test_brief_validation import evidence

    store = BriefStore(tmp_path / "lease-checkpoint.db")
    admit(store)
    job = store.claim_one(WORKER, clock=at)
    store.start_generating(job.lease, clock=at)
    wire = Wire()
    checks = 0

    def checkpoint(phase, lease, sources):
        nonlocal checks
        checks += 1
        # Admission and the outer scheduled prepare succeed; the per-egress
        # checkpoint loses its guard while the persisted lease is still valid.
        if checks >= 3 and len(wire.calls) == at_call:
            raise LeaseLost(SECRET) from RuntimeError(SECRET)

    result = await generate(store=store, lease=job.lease, call_id="lease-checkpoint",
        sources=evidence(), coverage=Coverage(False, ("manual only",)), goal="Private draft",
        config=GenerationConfig(parameters(), 1000000), transport=adapter(wire),
        checkpoint=checkpoint, cancellation=asyncio.Event(), clock=at)
    assert result.status == "lease_lost" and result.draft is None
    cost = store.cost_record(SCOPE, job.job_id, "lease-checkpoint")
    assert cost.diagnostic == "lease_lost" and cost.state == "unknown"
    assert cost.charged_micros > 0
    assert len(wire.calls) == at_call and store.outbox(SCOPE, job.job_id) is None
    assert wire.closed and all(s.closed for s in wire.streams)
    assert SECRET.encode() not in store.path.read_bytes()


@pytest.mark.parametrize("at_call", [0, 1])
@pytest.mark.parametrize("kind", ["cancelled", "timeout"])
async def test_private_callback_control_exceptions_are_sanitized(at_call, kind):
    from ostag.briefs.openai_transport import OpenAITransportError

    wire = Wire()
    error = asyncio.CancelledError(SECRET) if kind == "cancelled" else TimeoutError(SECRET)

    def gate():
        if len(wire.calls) == at_call:
            raise error from RuntimeError(SECRET)

    expected = asyncio.CancelledError if kind == "cancelled" else OpenAITransportError
    with pytest.raises(expected) as caught:
        await adapter(wire).generate(request(reauthorize=gate))
    assert caught.value is not error
    assert_sanitized_error(caught.value, "gate")
    assert len(wire.calls) == at_call
    assert wire.closed and all(s.closed for s in wire.streams)


@pytest.mark.parametrize("at_call", [0, 1])
async def test_task_cancel_requested_in_authorization_prevents_next_http(at_call):
    wire = Wire()
    def gate():
        if len(wire.calls) == at_call:
            asyncio.current_task().cancel()
    with pytest.raises(asyncio.CancelledError):
        await adapter(wire).generate(request(reauthorize=gate))
    assert len(wire.calls) == at_call
    assert wire.closed and all(s.closed for s in wire.streams)


@pytest.mark.parametrize("case,diagnostic", [
    ("profile", "unsupported provider profile or authorization"),
    ("gate", "unsupported provider profile or authorization"),
    ("secret", "credential in model input"),
    ("count", "invalid provider input count"),
    ("time", "invalid authorization time"),
])
async def test_fixed_rejection_diagnostics_remain_distinct(case, diagnostic):
    from ostag.briefs.openai_transport import OpenAITransportError
    wire = Wire(count=-1 if case == "count" else 100)
    changes = {
        "profile": {"parameters": replace(parameters(), expected_model_id="gpt-4.1")},
        "gate": {"reauthorize": None},
        "secret": {"serialized_input": SECRET},
        "count": {},
        "time": {"reauthorize": lambda: False},
    }
    with pytest.raises(OpenAITransportError, match=f"^{diagnostic}$") as caught:
        await adapter(wire).generate(request(**changes[case]))
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert len(wire.calls) == (1 if case == "count" else 0)


@pytest.mark.parametrize("scheduled", [False, True])
async def test_pending_caller_cancellation_cannot_spawn_http(scheduled):
    wire = Wire()
    async def caller():
        if scheduled:
            asyncio.get_running_loop().call_soon(asyncio.current_task().cancel)
        else:
            asyncio.current_task().cancel()
        return await adapter(wire).generate(request())
    with pytest.raises(asyncio.CancelledError):
        await asyncio.create_task(caller())
    assert not wire.calls


async def test_count_cookies_do_not_become_generation_authentication():
    async def effect(req):
        if req.url.path.endswith("input_tokens"):
            stream = Stream([b'{"object":"response.input_tokens","input_tokens":100}'])
            wire.streams.append(stream)
            return httpx.Response(200, stream=stream, headers={"set-cookie": "session=untrusted"})
    wire = Wire(effect=effect)
    result = await adapter(wire).generate(request())
    assert result.body and len(wire.calls) == 2
    assert all("cookie" not in req.headers for req in wire.calls)


async def test_success_exact_framing_payload_and_cache_rounding():
    wire = Wire()
    checkpoints = []
    req = request(reauthorize=lambda: checkpoints.append(len(wire.calls)))
    result = await adapter(wire).generate(req)
    assert result.body == json.dumps(output())
    assert result.actual_model_id == MODEL
    assert result.usage == Usage(100, 50, 596)  # ceil(97*2 + 3/2 + 50*8)
    assert checkpoints == [0, 1]
    assert [str(r.url) for r in wire.calls] == [
        "https://api.openai.com/v1/responses/input_tokens", "https://api.openai.com/v1/responses"]
    count, create = [json.loads(r.content) for r in wire.calls]
    fixed = dict(model=MODEL, input=req.serialized_input, tools=[], tool_choice="none",
                 parallel_tool_calls=False, truncation="disabled", text={"format": {"type": "text"}})
    assert count == fixed
    assert create == fixed | dict(store=False, background=False, stream=False,
                                 max_output_tokens=1000, service_tier="default")
    assert len(req.serialized_input.encode()) < 100  # provider framing is actually counted
    for r in wire.calls:
        assert r.method == "POST" and r.headers["authorization"] == "Bearer " + SECRET
        assert SECRET.encode() not in r.content
    assert wire.closed and all(s.closed for s in wire.streams)


@pytest.mark.parametrize("change", ["alias", "input_rate", "output_rate", "output_limit",
                                     "missing_gate", "invalid_gate"])
async def test_invalid_profile_never_egresses(change):
    wire = Wire()
    kwargs = {}
    if change == "alias":
        kwargs["parameters"] = replace(parameters(), expected_model_id="gpt-4.1")
    elif change in ("input_rate", "output_rate", "output_limit"):
        kwargs["parameters"] = parameters(**{
            "input_rate": {"input_micros_per_token": 1},
            "output_rate": {"output_micros_per_token": 7},
            "output_limit": {"max_output_tokens": 32769, "max_job_micros": 1000000},
        }[change])
    else:
        kwargs["reauthorize"] = None if change == "missing_gate" else True
    with pytest.raises((ValueError, RuntimeError)):
        await adapter(wire).generate(request(**kwargs))
    assert not wire.calls


@pytest.mark.parametrize("revoke", [False, True])
async def test_real_service_worker_http_sqlite_journey(tmp_path, revoke):
    authority = Authority()
    authority.config = GenerationConfig(parameters(), 1000000)
    async def effect(req):
        if revoke and req.url.path.endswith("input_tokens"):
            authority.sources.pop("s0")
    wire = Wire(effect=effect)
    service = authority.service(BriefStore(tmp_path / "brief.db"), transport=adapter(wire))
    admitted = service.submit(event())
    worker = BriefWorker(service)
    try:
        assert await asyncio.wrap_future(worker.run_once()) == (
            "needs_input" if revoke else "draft_ready")
        assert len(wire.calls) == (1 if revoke else 2)
        box = service.store.outbox(SCOPE, admitted.job_id)
        cost = service.store.cost_record(SCOPE, admitted.job_id, "manual-generation-1")
        if revoke:
            assert box is None and cost.state == "unknown" and cost.charged_micros > 0
        else:
            assert box.state == "pending" and box.destination.address == "alice-dm"
            assert service.store.get(SCOPE, admitted.job_id).state == "draft_ready"
            assert cost.state == "settled" and cost.usage == Usage(100, 50, 596)
            assert SECRET not in box.revision.provenance_json
    finally:
        worker.stop()
        service.close()


@pytest.mark.parametrize("phase", ["count", "create"])
@pytest.mark.parametrize("failure", [429, 500, 302, "disconnect", "timeout"])
async def test_http_failure_never_replays_and_sanitizes(phase, failure):
    async def effect(req):
        if req.url.path.endswith("input_tokens") != (phase == "count"):
            return None
        if failure == "disconnect":
            raise httpx.ReadError(SECRET, request=req)
        if failure == "timeout":
            raise httpx.ReadTimeout(SECRET, request=req)
        stream = Stream([json.dumps({"error": SECRET}).encode()])
        wire.streams.append(stream)
        return httpx.Response(failure, stream=stream, headers={"location": "https://evil.test/"})
    wire = Wire(effect=effect)
    try:
        result = await adapter(wire).generate(request())
        assert phase == "create" and not result.body and result.usage is None
    except (RuntimeError, TimeoutError) as exc:
        assert SECRET not in str(exc) and exc.__cause__ is None and exc.__context__ is None
    assert len(wire.calls) == (1 if phase == "count" else 2)
    assert wire.closed and all(s.closed for s in wire.streams)


@pytest.mark.parametrize("count", [True, -1, 1.0, "100", None, 8001, 10**30])
async def test_invalid_or_unbounded_count_prevents_create(count):
    wire = Wire(count=count)
    with pytest.raises(RuntimeError):
        await adapter(wire).generate(request())
    assert len(wire.calls) == 1 and wire.closed


async def test_context_includes_preflight_framing():
    p = parameters(max_input_bytes=1047576, max_output_tokens=1000, max_job_micros=3000000)
    wire = Wire(count=1047576)
    with pytest.raises(RuntimeError):
        await adapter(wire).generate(request(parameters=p))
    assert len(wire.calls) == 1


@pytest.mark.parametrize("change", [
    {"model": None}, {"model": "gpt-4.1"}, {"model": "different-snapshot"},
    {"model": {"expected": MODEL}}, {"object": "chat.completion"}, {"object": None},
    {"service_tier": "priority"}, {"service_tier": "flex"}, {"service_tier": None},
    {"usage": None}, {"usage": {"input_tokens": 100, "output_tokens": 50}},
])
async def test_uncertain_identity_or_usage_never_priced_as_expected(change):
    forged = json.dumps({"actual_model_id": MODEL, "usage": envelope()["usage"]})
    data = envelope(**change)
    data["output"][0]["content"][0]["text"] = forged
    result = await adapter(Wire(result=data)).generate(request())
    assert result.usage is None and result.actual_model_id is None and result.body == ""


@pytest.mark.parametrize("field,value", [
    ("input_tokens", True), ("input_tokens", -1), ("input_tokens", "100"),
    ("output_tokens", 50.0), ("output_tokens", False), ("total_tokens", 151),
    ("total_tokens", None), ("audio_tokens", 0),
    ("input_tokens_details", None), ("input_tokens_details", {}),
    ("input_tokens_details", {"cached_tokens": True}),
    ("input_tokens_details", {"cached_tokens": -1}),
    ("input_tokens_details", {"cached_tokens": 101}),
    ("input_tokens_details", {"cached_tokens": 3, "audio_tokens": 0}),
    ("output_tokens_details", {"reasoning_tokens": 1}),
    ("output_tokens_details", {"reasoning_tokens": 51}),
    ("output_tokens_details", {"reasoning_tokens": False}),
    ("output_tokens_details", {}), ("output_tokens_details", None),
])
async def test_strict_usage_and_known_text_modalities_only(field, value):
    result = await adapter(Wire(result=envelope(usage=envelope()["usage"] | {field: value}))).generate(
        request())
    assert result.usage is None and not result.body


async def test_absent_cache_detail_is_conservatively_uncached():
    usage = envelope()["usage"]
    usage.pop("input_tokens_details")
    usage.pop("output_tokens_details")
    result = await adapter(Wire(result=envelope(usage=usage))).generate(request())
    assert result.usage == Usage(100, 50, 600)


@pytest.mark.parametrize("bad", ["incomplete", "failed", "queued", "missing_status", "error",
    "incomplete_details", "tool", "extra_action", "refusal", "image", "role", "message_status",
    "empty", "multiple_messages", "unknown_content_field", "annotations", "oversized", "surrogate",
    "secret", "count_mismatch"])
async def test_rejected_model_content_preserves_known_usage(bad):
    data = envelope()
    message = data["output"][0]
    block = message["content"][0]
    if bad in ("incomplete", "failed", "queued"):
        data["status"] = bad
    elif bad == "missing_status":
        data.pop("status")
    elif bad in ("error", "incomplete_details"):
        data[bad] = {"message": "untrusted details"}
    elif bad == "tool":
        message["type"] = "function_call"
    elif bad == "extra_action":
        data["output"].append({"type": "web_search_call"})
    elif bad in ("refusal", "image"):
        block["type"] = bad
    elif bad == "role":
        message["role"] = "user"
    elif bad == "message_status":
        message["status"] = "incomplete"
    elif bad == "empty":
        data["output"] = []
    elif bad == "multiple_messages":
        data["output"] *= 2
    elif bad == "unknown_content_field":
        block["action"] = "run command"
    elif bad == "annotations":
        block["annotations"] = [{"type": "file_citation"}]
    elif bad in ("oversized", "surrogate", "secret"):
        block["text"] = {"oversized": "字" * 6000, "surrogate": "\ud800", "secret": SECRET}[bad]
    elif bad == "count_mismatch":
        data["usage"]["input_tokens"] = 101
        data["usage"]["total_tokens"] = 151
    result = await adapter(Wire(result=data)).generate(request())
    assert result.body == "" and result.actual_model_id == MODEL
    assert result.usage == (Usage(101, 50, 598) if bad == "count_mismatch" else Usage(100, 50, 596))


async def test_multiple_text_blocks_have_bounded_exact_join():
    data = envelope()
    block = data["output"][0]["content"][0]
    expected = block["text"]
    data["output"][0]["content"] = [block | {"text": expected[:20]}, block | {"text": expected[20:]}]
    result = await adapter(Wire(result=data)).generate(request())
    assert result.body == expected


async def test_actual_overrun_is_not_clamped():
    usage = envelope()["usage"] | {"input_tokens": 9000, "output_tokens": 40000, "total_tokens": 49000}
    result = await adapter(Wire(result=envelope(usage=usage))).generate(request())
    assert result.usage == Usage(9000, 40000, 337996)
    assert result.usage.cost_micros > parameters().budget.reservation_micros and not result.body


@pytest.mark.parametrize("phase", ["count", "create"])
@pytest.mark.parametrize("bad", ["utf8", "duplicate", "nan", "infinity", "overflow_float",
    "array", "malformed", "oversize", "chunks", "compressed", "length", "negative_length"])
async def test_envelope_bounds_before_parse_and_no_replay(phase, bad):
    import gzip
    from ostag.briefs.openai_transport import MAX_COUNT_BYTES, MAX_ENVELOPE_BYTES, MAX_CHUNKS
    bound = MAX_COUNT_BYTES if phase == "count" else MAX_ENVELOPE_BYTES
    raw = {"utf8": b'\xff', "duplicate": b'{"model":"wrong","model":"right"}',
           "nan": b'{"n":NaN}', "infinity": b'{"n":Infinity}',
           "overflow_float": b'{"n":1e999}', "array": b'[]', "malformed": b'{'}
    chunks = [raw.get(bad, b'{}')]
    headers = {}
    if bad == "oversize":
        chunks = [b' ' * (bound // 2), b' ' * (bound // 2 + 1)]
    elif bad == "chunks":
        chunks = [b' '] * (MAX_CHUNKS + 1)
    elif bad == "compressed":
        chunks, headers = [gzip.compress(b' ' * (bound + 1))], {"content-encoding": "gzip"}
    elif bad in ("length", "negative_length"):
        headers = {"content-length": str(bound + 1) if bad == "length" else "-1"}
    stream = Stream(chunks)
    async def effect(req):
        if req.url.path.endswith("input_tokens") == (phase == "count"):
            return httpx.Response(200, stream=stream, headers=headers)
    wire = Wire(effect=effect)
    with pytest.raises(RuntimeError):
        await adapter(wire).generate(request())
    assert stream.closed and wire.closed and len(wire.calls) == (1 if phase == "count" else 2)


@pytest.mark.parametrize("phase", ["count", "create"])
@pytest.mark.parametrize("wait_on", ["headers", "body"])
@pytest.mark.parametrize("stop", ["timeout", "event", "task"])
async def test_whole_call_timeout_and_cancellation_close_resources(phase, wait_on, stop):
    entered = asyncio.Event()
    stream = Stream([b'{'], stall=True)
    req = request(remaining_seconds=0.06 if stop == "timeout" else 1)
    async def effect(http_request):
        if http_request.url.path.endswith("input_tokens") == (phase == "count"):
            entered.set()
            if wait_on == "headers":
                await asyncio.Event().wait()
            return httpx.Response(200, stream=stream)
    wire = Wire(effect=effect)
    task = asyncio.create_task(adapter(wire).generate(req))
    await entered.wait()
    if wait_on == "body":
        await stream.entered.wait()
    if stop == "event":
        req.cancellation.set()
    elif stop == "task":
        task.cancel()
    with pytest.raises(TimeoutError if stop == "timeout" else asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
    assert wire.closed and all(s.closed for s in wire.streams)
    assert wait_on == "headers" or stream.closed
    assert len(wire.calls) == (1 if phase == "count" else 2)


@pytest.mark.parametrize("bad", [True, False, 0, -1, float("nan"), float("inf"), "1"])
async def test_invalid_callback_time_never_authorizes_egress(bad):
    wire = Wire()
    with pytest.raises(RuntimeError):
        await adapter(wire).generate(request(reauthorize=lambda: bad))
    assert not wire.calls and wire.closed


@pytest.mark.parametrize("at_call", [0, 1])
@pytest.mark.parametrize("stop", ["deny", "cancel", "stall"])
async def test_each_gate_rechecks_after_scheduling_and_stalled_callback(at_call, stop):
    import time
    wire = Wire()
    req = request(remaining_seconds=0.02)
    def gate():
        if len(wire.calls) == at_call:
            if stop == "deny":
                raise RuntimeError(SECRET)
            if stop == "cancel":
                req.cancellation.set()
            else:
                time.sleep(0.03)  # trusted synchronous callback exhausted overall time
        return 180.0  # must never extend original 20ms deadline
    req = replace(req, reauthorize=gate)
    with pytest.raises((RuntimeError, asyncio.CancelledError, TimeoutError)):
        await adapter(wire).generate(req)
    assert len(wire.calls) == at_call and wire.closed


async def test_time_budget_is_shared_by_count_and_create():
    async def effect(req):
        await asyncio.sleep(0.1)
    wire = Wire(effect=effect)
    with pytest.raises(TimeoutError):
        await adapter(wire).generate(request(remaining_seconds=0.15))
    assert len(wire.calls) == 2 and wire.closed


async def test_fresh_callback_time_shrinks_body_timeout():
    async def effect(req):
        await asyncio.Event().wait()
    wire = Wire(effect=effect)
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(adapter(wire).generate(request(reauthorize=lambda: 0.02)), 0.5)
    assert len(wire.calls) == 1 and wire.closed


async def test_outer_generator_scheduling_delay_cannot_start_late_http():
    import time
    from ostag.briefs.generation import _bounded_call
    wire = Wire()
    req = request(remaining_seconds=0.01)
    def prepare():
        time.sleep(0.03)
        return req
    try:
        result, _ = await _bounded_call(adapter(wire), req, prepare)
        assert result is None
    except Exception as exc:
        assert getattr(exc, "reason", None) == "timeout"
    assert not wire.calls, "initial prepare scheduling must not reset monotonic deadline"


def test_import_safe_without_optional_httpx():
    import subprocess
    import sys
    code = '''
import builtins
original = builtins.__import__
def deny(name, *args, **kwargs):
    if name.split('.')[0] in ('httpx', 'openai'):
        raise ImportError('optional dependency unavailable')
    return original(name, *args, **kwargs)
builtins.__import__ = deny
from ostag.briefs.openai_transport import OpenAIReadOnlyTransport, OpenAITransportError
try:
    OpenAIReadOnlyTransport(api_key='[REDACTED_SECRET]')
except OpenAITransportError as exc:
    assert str(exc) == 'install the openai optional dependency'
else:
    raise AssertionError('construction needs explicit optional dependency')
'''
    result = subprocess.run([sys.executable, "-c", code], text=True, capture_output=True,
                            env={"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"}, timeout=5)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("key", [None, "", True, "has space", "line\nbreak", "\u00e9", "x" * 4097])
def test_explicit_key_validation_has_fixed_diagnostics(key):
    from ostag.briefs.openai_transport import OpenAIReadOnlyTransport
    with pytest.raises(RuntimeError, match="explicit API key required") as exc:
        OpenAIReadOnlyTransport(api_key=key)
    assert exc.value.__cause__ is None


@pytest.mark.parametrize("override", ["base_url", "endpoint", "client", "proxy", "model", "tools",
                                     "service_tier", "max_retries"])
def test_public_constructor_has_no_configuration_escape_hatch(override):
    from ostag.briefs.openai_transport import OpenAIReadOnlyTransport
    with pytest.raises(TypeError):
        OpenAIReadOnlyTransport(api_key=SECRET, **{override: "forbidden"})


async def test_production_client_ignores_environment_and_bounds_pool(monkeypatch, caplog):
    from ostag.briefs.openai_transport import OpenAIReadOnlyTransport
    for name in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_ORG_ID", "OPENAI_PROJECT_ID",
                 "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "SSL_CERT_FILE", "NETRC"):
        monkeypatch.setenv(name, "https://environment-must-not-be-used.invalid")
    wire = Wire()
    pools = []
    async def boundary(transport, req):
        pools.append(transport._pool)
        return await wire.handle_async_request(req)
    # Exercise construction of the actual production client and owned default
    # transport; intercept only its network boundary, never adapter internals.
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", boundary)
    transport = OpenAIReadOnlyTransport(api_key=SECRET)
    assert SECRET not in repr(transport)
    result = await transport.generate(request())
    assert result.usage == Usage(100, 50, 596) and len(wire.calls) == 2
    assert len({id(p) for p in pools}) == 1
    assert pools[0]._max_connections == 1 and pools[0]._max_keepalive_connections == 0
    assert pools[0]._retries == 0
    for req in wire.calls:
        assert req.headers["authorization"] == "Bearer " + SECRET
        assert req.url.host == "api.openai.com"
        assert req.headers["accept-encoding"] == "identity"
        assert "openai-organization" not in req.headers and "openai-project" not in req.headers
    assert SECRET not in caplog.text and "材料" not in caplog.text
    assert all(s.closed for s in wire.streams)


async def test_secret_in_model_input_is_denied_before_egress():
    wire = Wire()
    with pytest.raises(RuntimeError) as exc:
        await adapter(wire).generate(request(serialized_input=SECRET))
    assert SECRET not in str(exc.value) and not wire.calls


async def test_worst_case_escaped_full_http_input_stays_bounded_without_truncation():
    from ostag.briefs.openai_transport import MAX_REQUEST_BYTES
    p = parameters(max_input_bytes=1048576, max_job_micros=3000000)
    text = "\x00" * 1048576
    wire = Wire()
    result = await adapter(wire).generate(request(parameters=p, serialized_input=text))
    assert result.body
    for req in wire.calls:
        assert 6 * 1048576 < len(req.content) <= MAX_REQUEST_BYTES
        assert json.loads(req.content)["input"] == text
    with pytest.raises(ValueError):
        request(parameters=p, serialized_input=text + "x")


async def test_count_shape_must_be_exact_object():
    async def effect(req):
        stream = Stream([b'{"object":"response.input_tokens","input_tokens":100,"extra":true}'])
        wire.streams.append(stream)
        return httpx.Response(200, stream=stream)
    wire = Wire(effect=effect)
    with pytest.raises(RuntimeError):
        await adapter(wire).generate(request())
    assert len(wire.calls) == 1 and all(s.closed for s in wire.streams)


async def test_cancelled_before_start_has_no_egress():
    wire = Wire()
    req = request()
    req.cancellation.set()
    with pytest.raises(asyncio.CancelledError):
        await adapter(wire).generate(req)
    assert not wire.calls


@pytest.mark.parametrize("bad", ["incomplete", "http_error", "overrun", "count_mismatch"])
async def test_rejected_response_settles_actual_sqlite_usage(tmp_path, bad):
    authority = Authority()
    authority.config = GenerationConfig(parameters(), 1000000)
    data = envelope()
    expected = Usage(100, 50, 596)
    if bad == "incomplete":
        data["status"] = "incomplete"
    elif bad == "overrun":
        data["usage"] |= {"output_tokens": 40000, "total_tokens": 40100}
        expected = Usage(100, 40000, 320196)
    elif bad == "count_mismatch":
        data["usage"] |= {"input_tokens": 101, "total_tokens": 151}
        expected = Usage(101, 50, 598)
    async def effect(req):
        if bad == "http_error" and req.url.path.endswith("/responses"):
            stream = Stream([json.dumps(data | {"error": {"message": SECRET}}).encode()])
            wire.streams.append(stream)
            return httpx.Response(500, stream=stream)
    wire = Wire(result=data, effect=effect)
    service = authority.service(BriefStore(tmp_path / "brief.db"), transport=adapter(wire))
    admitted = service.submit(event())
    worker = BriefWorker(service)
    try:
        assert await asyncio.wrap_future(worker.run_once()) == "needs_input"
        assert service.store.outbox(SCOPE, admitted.job_id) is None
        cost = service.store.cost_record(SCOPE, admitted.job_id, "manual-generation-1")
        assert cost.usage == expected and cost.charged_micros == expected.cost_micros
        assert cost.state == "settled" and len(wire.calls) == 2
    finally:
        worker.stop()
        service.close()


@pytest.mark.parametrize("change", ["grant", "expiry", "cancel", "day"])
async def test_count_boundary_authorization_changes_block_create_and_draft(tmp_path, change):
    authority = Authority()
    # A valid original lease crosses midnight for the charged-day test.
    if change == "day":
        authority.now = 86399
        authority.grant = replace(authority.grant, expires_at="2026-09-15T00:00:00Z")
        authority.sources = {k: replace(s, fresh_until="2026-09-15T00:00:00Z")
                             for k, s in authority.sources.items()}
    authority.config = GenerationConfig(parameters(), 1000000)
    async def effect(req):
        if req.url.path.endswith("input_tokens"):
            if change == "grant":
                authority.grant = replace(authority.grant, revision=2)
            elif change == "expiry":
                authority.now = 181
            elif change == "day":
                authority.now = 86400
            else:
                service.store.cancel(SCOPE, admitted.job_id, clock=service.clock)
    wire = Wire(effect=effect)
    service = authority.service(BriefStore(tmp_path / "brief.db"), transport=adapter(wire))
    admitted = service.submit(event())
    worker = BriefWorker(service)
    try:
        assert await asyncio.wrap_future(worker.run_once()) in ("needs_input", "cancelled")
        assert len(wire.calls) == 1 and service.store.outbox(SCOPE, admitted.job_id) is None
        cost = service.store.cost_record(SCOPE, admitted.job_id, "manual-generation-1")
        assert cost.state == "unknown" and cost.charged_micros > 0
        assert cost.charged_day == "2026-09-13"
        expected = {"grant": "checkpoint_denied", "expiry": "lease_lost",
                    "cancel": "lease_lost", "day": "timeout"}[change]
        assert cost.diagnostic == expected
        assert service.store.failure_cause(SCOPE, admitted.job_id) == (
            "cancelled" if change == "cancel" else expected)
    finally:
        worker.stop()
        service.close()


@pytest.mark.parametrize("at_call", [0, 1])
@pytest.mark.parametrize("reason", ["checkpoint_denied", "cancelled", "timeout",
                                    "lease_lost", "schema", "trusted_bug"])
async def test_trusted_authorization_exception_preserves_only_sanitized_domain_reason(at_call, reason):
    from ostag.briefs.generation import _Stopped
    from ostag.briefs.models import SchemaError
    from ostag.briefs.openai_transport import OpenAITransportError
    from ostag.briefs.store import LeaseLost

    error = (LeaseLost(SECRET) if reason == "lease_lost" else
             SchemaError("denied", SECRET, SECRET) if reason == "schema" else
             RuntimeError(SECRET) if reason == "trusted_bug" else
             _Stopped(reason))
    error.args = (SECRET,)  # Even recognized signals may carry private diagnostic text.
    expected_type = (LeaseLost if reason == "lease_lost" else
                     OpenAITransportError if reason == "trusted_bug" else _Stopped)
    wire = Wire()
    def gate():
        if len(wire.calls) == at_call:
            raise error from RuntimeError(SECRET)
    with pytest.raises(expected_type) as caught:
        await adapter(wire).generate(request(reauthorize=gate))
    assert caught.value is not error
    if expected_type is _Stopped:
        assert caught.value.reason == ("checkpoint_denied" if reason == "schema" else reason)
    else:
        assert str(caught.value) == ("authorization lease lost" if reason == "lease_lost"
                                     else "provider transport failed")
    assert_sanitized_error(caught.value, "gate")
    assert len(wire.calls) == at_call
    assert wire.closed and all(s.closed for s in wire.streams)


def assert_sanitized_error(error, source):
    import traceback
    assert error.__context__ is None and error.__cause__ is None
    assert SECRET not in str(error) and SECRET not in repr(error)
    assert SECRET not in "".join(traceback.format_exception(error))
    assert source not in [frame.name for frame in traceback.extract_tb(error.__traceback__)]


@pytest.mark.parametrize("at_call", [0, 1])
@pytest.mark.parametrize("kind", ["runtime", "unknown_stop", "provider", "provider_known",
                                  "provider_str_override"])
async def test_callback_private_errors_never_escape(at_call, kind, caplog):
    from ostag.briefs.generation import _Stopped
    from ostag.briefs.openai_transport import OpenAITransportError

    class PrivateDiagnostic(OpenAITransportError):
        def __str__(self):
            return SECRET

    error = {"runtime": RuntimeError(SECRET), "unknown_stop": _Stopped(SECRET),
             "provider": OpenAITransportError(SECRET),
             "provider_known": OpenAITransportError("invalid provider JSON"),
             "provider_str_override": PrivateDiagnostic("invalid provider JSON")}[kind]
    wire = Wire()
    def gate():
        if len(wire.calls) == at_call:
            raise error from RuntimeError(SECRET)
    with pytest.raises(OpenAITransportError, match="^provider transport failed$") as caught:
        await adapter(wire).generate(request(reauthorize=gate))
    assert caught.value is not error
    assert_sanitized_error(caught.value, "gate")
    assert SECRET not in caplog.text
    assert len(wire.calls) == at_call
    assert wire.closed and all(s.closed for s in wire.streams)


@pytest.mark.parametrize("phase", ["count", "create"])
@pytest.mark.parametrize("kind", ["provider", "provider_known", "provider_str_override",
                                  "lease", "stop", "schema"])
async def test_network_hook_errors_have_only_fixed_public_diagnostics(phase, kind, caplog):
    from ostag.briefs.generation import _Stopped
    from ostag.briefs.models import SchemaError
    from ostag.briefs.openai_transport import OpenAITransportError
    from ostag.briefs.store import LeaseLost

    class PrivateDiagnostic(OpenAITransportError):
        def __str__(self):
            return SECRET

    error = {"provider": OpenAITransportError(SECRET),
             "provider_known": OpenAITransportError("invalid provider JSON"),
             "provider_str_override": PrivateDiagnostic("invalid provider JSON"),
             "lease": LeaseLost(SECRET), "stop": _Stopped("checkpoint_denied"),
             "schema": SchemaError("denied", SECRET, SECRET)}[kind]
    async def effect(req):
        if req.url.path.endswith("input_tokens") == (phase == "count"):
            raise error from RuntimeError(SECRET)
    wire = Wire(effect=effect)
    expected = "invalid provider JSON" if kind == "provider_known" else "provider transport failed"
    with pytest.raises(OpenAITransportError, match=f"^{expected}$") as caught:
        await adapter(wire).generate(request())
    assert caught.value is not error
    assert_sanitized_error(caught.value, "effect")
    assert SECRET not in caplog.text
    assert len(wire.calls) == (1 if phase == "count" else 2)
    assert wire.closed and all(s.closed for s in wire.streams)


@pytest.mark.parametrize("change", ["acl", "schema", "private_runtime"])
async def test_count_boundary_source_denial_records_sanitized_reason(tmp_path, change, caplog):
    from ostag.briefs.models import SchemaError

    authority = Authority()
    authority.config = GenerationConfig(parameters(), 1000000)
    counted = False
    def lookup(scope, source_id):
        if counted:
            if change == "acl":
                return None
            if change == "schema":
                raise SchemaError("denied", SECRET, SECRET)
            raise RuntimeError(SECRET)
        return authority.lookup(scope, source_id)
    async def effect(req):
        nonlocal counted
        if req.url.path.endswith("input_tokens"):
            counted = True
    wire = Wire(effect=effect)
    service = authority.service(BriefStore(tmp_path / "brief.db"),
                                sources=lookup, transport=adapter(wire))
    admitted = service.submit(event())
    worker = BriefWorker(service)
    try:
        assert await asyncio.wrap_future(worker.run_once()) == "needs_input"
        expected = "transport_error" if change == "private_runtime" else "checkpoint_denied"
        cost = service.store.cost_record(SCOPE, admitted.job_id, "manual-generation-1")
        assert cost.diagnostic == expected and cost.state == "unknown"
        assert cost.charged_micros > 0 and cost.charged_day == "2026-09-13"
        assert service.store.failure_cause(SCOPE, admitted.job_id) == expected
        assert service.store.outbox(SCOPE, admitted.job_id) is None
        assert service.store.get(SCOPE, admitted.job_id).state != "draft_ready"
        assert len(wire.calls) == 1
        assert wire.closed and all(s.closed for s in wire.streams)
        assert SECRET not in caplog.text and SECRET.encode() not in service.store.path.read_bytes()
    finally:
        worker.stop()
        service.close()
