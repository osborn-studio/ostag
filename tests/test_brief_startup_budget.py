"""Real spawn boundaries: interpreter time must not consume filesystem budgets."""
import asyncio
import functools
import json
import multiprocessing
from types import SimpleNamespace

import pytest

from ostag.briefs import runtime
from ostag.briefs.lifecycle import BriefLifecycle, LifecycleDenied
from test_brief_control_bridge import control, owner_at
from brief_process_helpers import _bootstrap_delay, _bad_ready, _blocked_io


@pytest.fixture
def spawn_probe(monkeypatch):
    context = multiprocessing.get_context('spawn')
    processes, endpoints, entered = [], [], []

    def install(delay=.8):
        def pipe(*args, **kwargs):
            pair = context.Pipe(*args, **kwargs)
            endpoints.extend(pair)
            return pair

        def process(*, target, args, daemon):
            event = context.Event()
            entered.append(event)
            child = context.Process(target=_bootstrap_delay,
                args=(target, args, delay if isinstance(delay, (int, float))
                      else delay[len(processes)], event), daemon=daemon)
            processes.append(child)
            return child

        monkeypatch.setattr(runtime.multiprocessing, 'get_context', lambda _: SimpleNamespace(
            Pipe=pipe, Process=process))
        return SimpleNamespace(processes=processes, endpoints=endpoints, entered=entered)

    return install


async def wait_until(predicate):
    async with asyncio.timeout(4):
        while not predicate():
            await asyncio.sleep(.01)


def assert_reaped(owner, probe):
    assert owner._process is None
    assert all(process._closed for process in probe.processes)
    assert all(endpoint.closed for endpoint in probe.endpoints)


@pytest.mark.parametrize('attached', [False, True])
async def test_slow_bootstrap_can_run_and_stop(tmp_path, spawn_probe, attached):
    probe = spawn_probe()
    life = BriefLifecycle() if attached else None
    owner = owner_at(tmp_path, life)
    starting = asyncio.create_task(owner.start())
    try:
        await wait_until(lambda: probe.entered and probe.entered[0].is_set())
        control(owner, 1, 'on')
        assert not starting.done()
        if life:
            with pytest.raises(LifecycleDenied):
                life.begin('intake')
        await asyncio.wait_for(starting, 4)
        assert owner.health == 'ok'
        assert owner.last_status['lifecycle'] == 'running'
        assert owner.last_status['control_valid']
        if life:
            life.begin('intake').finish()
    finally:
        await owner.stop()
        await starting
    terminal = json.loads((owner.status_dir / 'status.json').read_text())
    assert terminal['lifecycle'] == 'stopped' and not terminal['drain_ack']
    assert len(probe.processes) == 1
    assert_reaped(owner, probe)


@pytest.mark.parametrize('attached', [False, True])
async def test_startup_timeout_is_bounded_and_fail_closed(
    tmp_path, monkeypatch, spawn_probe, attached,
):
    monkeypatch.setattr(runtime, '_STARTUP_TIMEOUT', .4)
    probe = spawn_probe(delay=60)
    life = BriefLifecycle() if attached else None
    owner = owner_at(tmp_path, life)
    await asyncio.wait_for(owner.start(), 2)
    await asyncio.wait_for(owner.stop(), 2)
    assert owner.health == 'degraded' and owner.reason == 'startup_timeout'
    assert owner.last_status is None
    assert not (owner.status_dir / 'owner.lock').exists()
    if life:
        with pytest.raises(LifecycleDenied):
            life.begin('intake')
    assert len(probe.processes) == 1
    assert_reaped(owner, probe)


@pytest.mark.parametrize('attached', [False, True])
@pytest.mark.parametrize('action', ['stop', 'cancel_start', 'cancel_owner'])
async def test_handshake_interruption_reaps_without_opening_writer(
    tmp_path, spawn_probe, attached, action,
):
    probe = spawn_probe(delay=60)
    life = BriefLifecycle() if attached else None
    owner = owner_at(tmp_path, life)
    starting = asyncio.create_task(owner.start())
    try:
        await wait_until(lambda: probe.entered and probe.entered[0].is_set())
        if action == 'stop':
            await asyncio.wait_for(owner.stop(), 2)
            await starting
        else:
            task = starting if action == 'cancel_start' else owner._task
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 2)
            if action == 'cancel_owner':
                await starting
        assert owner.last_status is None
        assert not (owner.status_dir / 'owner.lock').exists()
        if life:
            with pytest.raises(LifecycleDenied):
                life.begin('intake')
        assert len(probe.processes) == 1
        assert_reaped(owner, probe)
    finally:
        if not owner._task.done():
            await owner.stop()


@pytest.mark.parametrize('attached', [False, True])
@pytest.mark.parametrize('mode', ['eof', 'malformed', 'wrong_boot', 'extra', 'partial'])
async def test_invalid_readiness_never_reaches_writer(
    tmp_path, monkeypatch, spawn_probe, attached, mode,
):
    monkeypatch.setattr(runtime, '_bootstrap', functools.partial(_bad_ready, mode=mode))
    monkeypatch.setattr(runtime, '_STARTUP_TIMEOUT', .8)
    probe = spawn_probe(delay=0)
    owner = owner_at(tmp_path, BriefLifecycle() if attached else None)
    await asyncio.wait_for(owner.start(), 2)
    await asyncio.wait_for(owner.stop(), 2)
    assert owner.health == 'degraded' and owner.last_status is None
    assert not (owner.status_dir / 'owner.lock').exists()
    assert len(probe.processes) == 1
    assert_reaped(owner, probe)


async def test_failed_process_start_closes_all_endpoints(tmp_path, monkeypatch, spawn_probe):
    probe = spawn_probe(delay=0)

    def fail(self):
        raise OSError('private process failure')

    monkeypatch.setattr(multiprocessing.process.BaseProcess, 'start', fail)
    owner = owner_at(tmp_path, None)
    await owner.start()
    await owner.stop()
    assert owner.health == 'degraded' and owner.reason == 'worker_failed'
    assert_reaped(owner, probe)


def block_io(monkeypatch, attached, *, opening=False):
    entered = multiprocessing.get_context('spawn').Event()
    monkeypatch.setattr(runtime, '_serve_attached' if attached else '_serve', functools.partial(
        _blocked_io, attached=attached, opening=opening, entered=entered))
    return entered


@pytest.mark.parametrize('attached', [False, True])
async def test_writer_open_still_has_the_io_deadline(tmp_path, monkeypatch, spawn_probe, attached):
    entered = block_io(monkeypatch, attached, opening=True)
    probe = spawn_probe(delay=0)
    owner = owner_at(tmp_path, BriefLifecycle() if attached else None)
    starting = asyncio.create_task(owner.start())
    try:
        await wait_until(entered.is_set)
        # Bound actual IO after entry, independently of interpreter startup.
        await asyncio.wait_for(starting, .85)
        assert owner.health == 'degraded' and owner.reason == 'io_timeout'
        await asyncio.wait_for(owner.stop(), 3)
        assert owner.last_status is None
        assert not (owner.status_dir / 'owner.lock').exists()
        assert len(probe.processes) == 2  # Exactly one bounded terminal attempt.
        assert_reaped(owner, probe)
    finally:
        await owner.stop()
        await starting


@pytest.mark.parametrize('attached', [False, True])
@pytest.mark.parametrize('recovery', ['success', 'timeout', 'cancel'])
async def test_terminal_recovery_also_waits_for_bootstrap(
    tmp_path, monkeypatch, spawn_probe, attached, recovery,
):
    entered = block_io(monkeypatch, attached)
    monkeypatch.setattr(runtime, '_STARTUP_TIMEOUT', 2)
    probe = spawn_probe(delay=[.8, .8 if recovery == 'success' else 60])
    life = BriefLifecycle() if attached else None
    owner = owner_at(tmp_path, life)
    await owner.start()
    try:
        assert owner.health == 'ok'
        control(owner, 2, 'on')
        await wait_until(entered.is_set)
        await wait_until(lambda: len(probe.entered) == 2 and probe.entered[1].is_set())
        assert probe.processes[0]._closed
        if life:
            with pytest.raises(LifecycleDenied):
                life.begin('intake')
        if recovery == 'cancel':
            owner._task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(owner._task, 2)
        else:
            await asyncio.wait_for(owner.stop(), 4)
            status = json.loads((owner.status_dir / 'status.json').read_text())
            if recovery == 'success':
                assert status['lifecycle'] == 'stopped' and not status['drain_ack']
            else:
                assert owner.health == 'degraded' and owner.reason == 'status_unavailable'
        assert len(probe.processes) == 2
        assert_reaped(owner, probe)
    finally:
        if not owner._task.cancelled():
            await owner.stop()
