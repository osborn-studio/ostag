"""Synthetic DeepSeek wire, budgets and production composition checks."""
import asyncio
from dataclasses import replace
import json

import httpx
import pytest

from ostag.briefs.generation import Parameters
from test_brief_openai_transport import SECRET, Stream, parameters, request

MODEL = 'deepseek-flash'


def params(**changes):
    return Parameters(MODEL, replace(parameters().budget,
        input_micros_per_token=1, output_micros_per_token=2, **changes))


def envelope(**changes):
    return dict(object='chat.completion', model=MODEL,
        choices=[dict(index=0, finish_reason='stop', message=dict(role='assistant', content='{}'))],
        usage=dict(prompt_tokens=100, completion_tokens=50, total_tokens=150,
                   prompt_cache_hit_tokens=3, prompt_cache_miss_tokens=97)) | changes


class Wire(httpx.AsyncBaseTransport):
    def __init__(self, data=None, status=200, effect=None, trace=False):
        self.data = envelope() if data is None else data
        self.status, self.effect, self.trace = status, effect, trace
        self.calls, self.streams = [], []
        self.closed = False

    async def handle_async_request(self, req):
        # Consume the actual one-shot authorization-gated byte stream.
        if self.trace:
            await req.extensions['trace']('http11.send_request_headers.started', {})
        raw = b''.join([chunk async for chunk in req.stream])
        self.calls.append((req, json.loads(raw)))
        if self.effect:
            await self.effect(req)
        stream = Stream([json.dumps(self.data).encode()])
        self.streams.append(stream)
        return httpx.Response(self.status, stream=stream)

    async def aclose(self):
        self.closed = True


def adapter(wire):
    from ostag.briefs.deepseek_transport import DeepSeekReadOnlyTransport
    return DeepSeekReadOnlyTransport(api_key=SECRET, transport=wire)


async def test_fixed_origin_non_thinking_json_and_peak_cost(monkeypatch):
    monkeypatch.setenv('DEEPSEEK_BASE_URL', 'https://invalid.example')
    wire = Wire()
    result = await adapter(wire).generate(request(parameters=params()))
    assert result.body == '{}' and result.actual_model_id == MODEL
    assert result.usage.cost_micros == 90  # ceil(97*.3 + 3*.006 + 50*1.2)
    assert len(wire.calls) == 1
    req, payload = wire.calls[0]
    assert str(req.url) == 'https://api.deepseek.com/chat/completions'
    assert payload['thinking'] == {'type': 'disabled'}
    assert payload['response_format'] == {'type': 'json_object'}
    assert payload['messages'] == [{'role': 'user', 'content': request().serialized_input}]
    assert payload['model'] == MODEL and payload['stream'] is False
    assert 'tools' not in payload
    assert wire.closed and all(s.closed for s in wire.streams)


@pytest.mark.parametrize('changes', [
    {'model': 'other'}, {'usage': None},
    {'usage': {'prompt_tokens': True, 'completion_tokens': 1, 'total_tokens': 2}},
    {'usage': {'prompt_tokens': 100, 'completion_tokens': 50, 'total_tokens': 151}},
    {'usage': {'prompt_tokens': 100, 'completion_tokens': 50, 'total_tokens': 150,
               'prompt_cache_hit_tokens': 101, 'prompt_cache_miss_tokens': -1}},
])
async def test_unknown_model_or_usage_never_settles(changes):
    result = await adapter(Wire(envelope(**changes))).generate(request(parameters=params()))
    assert result.usage is None and result.body == '' and result.actual_model_id is None


@pytest.mark.parametrize('finish', ['length', 'tool_calls', 'content_filter'])
async def test_failed_content_preserves_measured_cost(finish):
    data = envelope()
    data['choices'][0]['finish_reason'] = finish
    result = await adapter(Wire(data)).generate(request(parameters=params()))
    assert result.body == '' and result.usage.cost_micros == 90


async def test_overrun_usage_not_clamped():
    data = envelope(usage=dict(prompt_tokens=9000, completion_tokens=2000, total_tokens=11000))
    result = await adapter(Wire(data)).generate(request(parameters=params()))
    assert result.usage.input_tokens == 9000 and result.usage.cost_micros == 5100
    assert result.body == ''


async def test_cancel_and_authorization_block_egress():
    from ostag.briefs.deepseek_transport import DeepSeekTransportError
    for cancelled in (False, True):
        wire = Wire()
        event = asyncio.Event()
        if cancelled:
            event.set()
        def deny():
            raise ValueError(SECRET)
        with pytest.raises(asyncio.CancelledError if cancelled else DeepSeekTransportError) as err:
            await adapter(wire).generate(request(parameters=params(), cancellation=event, reauthorize=deny))
        assert SECRET not in str(err.value) and not wire.calls
        assert err.value.__context__ is None


async def test_requires_framing_headroom_before_network():
    from ostag.briefs.deepseek_transport import DeepSeekTransportError
    wire = Wire()
    with pytest.raises(DeepSeekTransportError):
        await adapter(wire).generate(request(parameters=params(max_input_bytes=100), serialized_input='x'*99))
    assert not wire.calls


async def test_timeout_closes_network():
    async def stall(_):
        await asyncio.Event().wait()
    wire = Wire(effect=stall)
    with pytest.raises(TimeoutError):
        await adapter(wire).generate(request(parameters=params(), remaining_seconds=.01))
    assert wire.closed


def test_production_composition_accepts_deepseek(tmp_path):
    from test_brief_production_owner import host
    h = host(tmp_path)
    c = replace(h.composition, model_transport=adapter(Wire()),
                generation=replace(h.composition.generation, parameters=params()))
    assert c.generation.parameters.expected_model_id == MODEL


def test_profile_defaults_and_secret_loading(tmp_path):
    from ostag.briefs.deepseek_credentials import store_key
    from ostag.briefs.deepseek_profile import production_inputs
    directory = tmp_path / 'secrets'
    store_key(directory, SECRET)
    values = production_inputs(directory)
    assert values['generation'].parameters.expected_model_id == MODEL
    assert values['generation'].daily_limit_micros == 1000000
    assert values['generation'].parameters.budget.reservation_micros <= 100000
    assert SECRET not in repr(values)


async def test_provider_stream_cannot_repeat_authorized_body():
    from ostag.briefs.deepseek_transport import DeepSeekTransportError
    class Replay(httpx.AsyncBaseTransport):
        async def handle_async_request(self, req):
            async for _ in req.stream:
                pass
            async for _ in req.stream:
                pass
            raise AssertionError('second consumption should be refused')
    with pytest.raises(DeepSeekTransportError):
        await adapter(Replay()).generate(request(parameters=params()))


@pytest.mark.parametrize('raw,headers', [
    (b'{"usage":null,"usage":{}}', {}),
    (b'{"value":NaN}', {}),
    (b'{}', {'content-encoding': 'gzip'}),
    (b'{}', {'content-length': '999999999'}),
])
async def test_untrusted_envelope_is_bounded_and_strict(raw, headers):
    from ostag.briefs.deepseek_transport import DeepSeekTransportError
    class Bad(httpx.AsyncBaseTransport):
        async def handle_async_request(self, req):
            async for _ in req.stream:
                pass
            return httpx.Response(200, stream=Stream([raw]), headers=headers)
    with pytest.raises(DeepSeekTransportError):
        await adapter(Bad()).generate(request(parameters=params()))


async def test_deepseek_meeting_through_production_lifespan(tmp_path):
    from test_brief_production_owner import (host, TestClient, create_app, open_owner,
        completion_handshake, completed_cycle)
    from test_brief_validation import output
    h = host(tmp_path)
    data = envelope()
    generated = output(text='Synthetic change requires review')
    generated['progress'][0]['claim']['source_ids'] = [h.request.sources[0].source_id]
    data['choices'][0]['message']['content'] = json.dumps(generated)
    wire = Wire(data, trace=True)
    c = replace(h.composition, model_transport=adapter(wire),
        generation=replace(h.composition.generation, parameters=params()))
    app = create_app(settings=h.settings, brief_composition=c)
    with TestClient(app) as client:
        owner = app.state.brief_owner
        assert owner.ready, owner.reason
        completion = completion_handshake(owner)
        client.portal.call(open_owner, owner)
        result = client.portal.call(app.state.brief_application.submit_meeting,
                                   h.token, 'deepseek-meeting', h.request)
        assert result.status == 'queued'
        assert client.portal.call(completed_cycle, completion) == [(result.job_id, 'draft_ready')]
        exported = client.portal.call(app.state.brief_application.private_export, h.token, result.job_id)
        assert 'Private meeting pre-read' in exported
        assert len(wire.calls) == 1


@pytest.mark.parametrize('trace,count', [(False, 2), (True, 3)])
async def test_reauthorization_at_all_write_boundaries(trace, count):
    calls = []
    wire = Wire(trace=trace)
    result = await adapter(wire).generate(request(parameters=params(),
        reauthorize=lambda: calls.append('checked')))
    assert result.usage is not None and len(calls) == count
    assert len(wire.calls) == 1


@pytest.mark.parametrize('kind', ['subclass', 'duck', 'wrong_model', 'wrong_provider'])
def test_composition_refuses_unaudited_or_mismatched_transport(tmp_path, kind):
    from test_brief_production_owner import host
    from ostag.briefs.deepseek_transport import DeepSeekReadOnlyTransport
    class Subclass(DeepSeekReadOnlyTransport):
        pass
    class Duck:
        async def generate(self, request):
            pass
    h = host(tmp_path)
    transports = dict(subclass=Subclass(api_key=SECRET), duck=Duck(),
                      wrong_model=adapter(Wire()), wrong_provider=h.composition.model_transport)
    generation = h.composition.generation if kind == 'wrong_model' else replace(
        h.composition.generation, parameters=params())
    with pytest.raises(ValueError):
        replace(h.composition, model_transport=transports[kind], generation=generation)


@pytest.mark.parametrize('status,reason', [(400,'invalid provider request'),
    (401,'provider unauthorized'), (402,'provider payment required'),
    (403,'provider unauthorized'), (429,'provider rate limited'), (503,'provider unavailable')])
async def test_http_failures_have_fixed_safe_diagnostics(status, reason):
    from ostag.briefs.deepseek_transport import DeepSeekTransportError
    wire = Wire(data={'error': SECRET}, status=status)
    with pytest.raises(DeepSeekTransportError, match='^'+reason+'$') as error:
        await adapter(wire).generate(request(parameters=params()))
    assert SECRET not in str(error.value) and error.value.__context__ is None
    assert wire.closed and all(s.closed for s in wire.streams)
