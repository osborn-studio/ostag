"""Actual child IPC and service custody; event barriers control the IO races."""
import asyncio
import functools
import json
import multiprocessing
from threading import Event

import pytest

from ostag.briefs.lifecycle import BriefLifecycle, LifecycleDenied
from ostag.briefs.worker import BriefWorker
from test_brief_control_bridge import control, generation, observed, owner_at
from test_brief_lifecycle_integration import briefs, cards
from test_brief_cards import SCOPE as CARD_SCOPE
from test_brief_service import event


async def wait_event(event):
    async with asyncio.timeout(4):
        while not event.is_set():
            await asyncio.sleep(.01)


def _controlled_child(connection, status_dir, control_path, boot_id, *, entered, release, mode):
    """Spawn-safe barrier AFTER temp write, BEFORE final identity recheck/rename."""
    from ostag.briefs import runtime

    write = runtime.os.write
    send = runtime._send
    held = False

    def controlled_write(fd, raw):
        nonlocal held
        value = json.loads(raw)
        result = write(fd, raw)
        if not held and value['lifecycle'] == 'running' and value['control_generation'] == 2:
            held = True
            entered.set()
            if not release.acquire(timeout=4):
                raise TimeoutError
            if mode == 'failure':
                raise PermissionError('private source exception must never escape')
        return result

    def controlled_send(pipe, value):
        if (mode == 'stale' and value['phase'] == 'publish'
                and value['data']['control_generation'] == 2):
            value = value | {'request_id': value['request_id'] + 1}
        send(pipe, value)

    runtime.os.write = controlled_write
    runtime._send = controlled_send
    runtime._serve_attached(connection, status_dir, control_path, boot_id)


def child_barrier(monkeypatch, mode='hold'):
    context = multiprocessing.get_context('spawn')
    entered, release = context.Event(), context.Semaphore(0)
    monkeypatch.setattr('ostag.briefs.runtime._serve_attached', functools.partial(
        _controlled_child, entered=entered, release=release, mode=mode))
    return entered, release


async def test_real_worker_model_custody_survives_pause_and_owner_stop(tmp_path):
    life, service = briefs(tmp_path, opened=False)
    owner = owner_at(tmp_path, life)
    entered, release = Event(), Event()

    async def held(_):
        entered.set()
        assert release.wait(4)

    service.transport.effect = held
    worker = BriefWorker(service)
    await owner.start()
    try:
        assert service.submit(event()).status == 'unavailable'
        control(owner, 1, 'on')
        await generation(owner, 1)
        assert service.submit(event()).status == 'queued'
        future = worker.run_once()
        await wait_event(entered)
        control(owner, 2)
        status = await generation(owner, 2)
        assert status['custody']['worker'] == status['custody']['model'] == 1
        assert not status['local_drained']
        await owner.stop()
        assert owner.last_status['custody']['model'] == 1
        assert owner.last_status['custody']['worker'] == 1
        assert not owner.last_status['drain_ack']
        release.set()
        assert await asyncio.wait_for(asyncio.wrap_future(future), 3) != 'draft_ready'
        assert not life.snapshot().drained
        assert life.snapshot().custody['model'] == 0  # Normal adapter completion.
    finally:
        release.set()
        await owner.stop()
        await asyncio.to_thread(worker.stop)
        service.close()


@pytest.mark.parametrize('publication', ['seal', 'io_timeout'])
async def test_real_card_send_uncertainty_survives_seal_stop_and_manual_apply(
        tmp_path, monkeypatch, publication):
    life = BriefLifecycle()
    owner = owner_at(tmp_path, life)
    entered, release, suppressed, returned = (asyncio.Event() for _ in range(4))
    boundary = asyncio.Lock()
    publish = owner._publish
    send_tasks = []
    timeouts = []
    task = None

    async def ordered_publish(lifecycle):
        async with boundary:
            try:
                return await publish(lifecycle)
            except TimeoutError:
                timeouts.append(lifecycle)
                raise

    # Control replacement during sample/publish is deliberately fatal. Also,
    # synchronous card SQLite work must not consume an outstanding IPC budget.
    # Order these test actions between complete, real child publications.
    monkeypatch.setattr(owner, '_publish', ordered_publish)
    if publication == 'io_timeout':
        writing, finish_write = child_barrier(monkeypatch)

    class Transport:
        async def send(self, attempt, *, before_egress):
            send_tasks.append(asyncio.current_task())
            await before_egress()
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                suppressed.set()
                await release.wait()
            returned.set()
            return None

        def check_ack(self, *args):
            return None

    service = cards(tmp_path / 'cards.db', life, transport=Transport())
    await owner.start()
    try:
        with pytest.raises(LifecycleDenied):
            await service.prepare(CARD_SCOPE, 'job', 'revision')
        async with boundary:
            control(owner, 1, 'on')
        await generation(owner, 1)
        async with boundary:
            task = asyncio.create_task(service.deliver(CARD_SCOPE, 'job', 'revision'))
            await asyncio.wait_for(entered.wait(), 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await asyncio.wait_for(suppressed.wait(), 2)
            control(owner, 2)
        if publication == 'seal':
            status = await generation(owner, 2)
            assert status['custody']['send'] == 1 and status['uncertain']
            assert not status['local_drained']
            await owner.stop()
            assert not timeouts
        else:
            process = owner._process
            await wait_event(writing)
            # Withhold the child write until the REAL 500 ms IPC budget expires.
            # Waiting for a later running generation would be unsound: this
            # single-use owner shuts down permanently and reaps the child.
            await asyncio.wait_for(asyncio.shield(owner._task), 3)
            assert timeouts == ['running']
            assert process._closed and owner._process is None
            assert life.snapshot().shutdown
        status = json.loads((owner.status_dir / 'status.json').read_text())
        assert status == owner.last_status
        assert status['lifecycle'] == 'stopped' and status['control_generation'] == 2
        assert not status['control_valid'] and not status['drain_ack']
        assert status['custody']['send'] == status['custody']['card'] == 1
        assert status['uncertain'] and not status['local_drained']
        with pytest.raises(LifecycleDenied):
            life.apply(3, 'on', False)
        release.set()
        await asyncio.wait_for(returned.wait(), 2)
        assert await asyncio.wait_for(asyncio.gather(*send_tasks), 2) == [None]
        assert life.snapshot().uncertain
        assert life.snapshot().custody['card'] == 1  # Abandoned operation stays unknown.
        assert owner.last_status['custody']['send'] == 1
    finally:
        release.set()
        service.stop()
        if publication == 'io_timeout':
            finish_write.release()
        try:
            if task is not None:
                if not task.done():
                    task.cancel()
                await asyncio.wait_for(asyncio.gather(task, *send_tasks, return_exceptions=True), 2)
        finally:
            await owner.stop()
        assert owner._process is None
        assert service.active_operations == 0


@pytest.mark.parametrize('mode', ['replacement', 'failure', 'stale'])
async def test_child_write_wait_never_opens_failed_or_replaced_control(tmp_path, monkeypatch, mode):
    entered, release = child_barrier(monkeypatch, mode)
    life = BriefLifecycle()
    owner = owner_at(tmp_path, life)
    await owner.start()
    try:
        control(owner, 1, 'on' if mode == 'failure' else 'off')
        await generation(owner, 1)
        op = life.begin('model') if mode == 'failure' else None
        control(owner, 2, 'on')
        await wait_event(entered)
        if op is not None:
            with pytest.raises(LifecycleDenied):
                op.check()
        assert not life._ready
        for kind in ('intake', 'cleanup', 'cancel'):
            with pytest.raises(LifecycleDenied):
                life.begin(kind)
        # The parent's event loop continues to run while filesystem work waits.
        tick = asyncio.Event()
        asyncio.get_running_loop().call_soon(tick.set)
        await asyncio.wait_for(tick.wait(), .2)
        if mode == 'replacement':
            # Same content in a new inode also invalidates the sample identity.
            control(owner, 2, 'on')
        release.release()
        await asyncio.wait_for(asyncio.shield(owner._task), 3)
        assert owner._process is None
        with pytest.raises(LifecycleDenied):
            life.begin('intake')
        status = json.loads((owner.status_dir / 'status.json').read_text())
        assert status['lifecycle'] == 'stopped' and not status['drain_ack']
        assert 'private source' not in json.dumps(status)
        if op is not None:
            assert status['custody']['model'] == 1
            op.finish()
    finally:
        release.release()
        await owner.stop()


async def test_off_is_applied_before_child_write_and_counts_cannot_grow(tmp_path, monkeypatch):
    entered, release = child_barrier(monkeypatch)
    life = BriefLifecycle()
    owner = owner_at(tmp_path, life)
    await owner.start()
    try:
        control(owner, 1, 'on')
        await generation(owner, 1)
        op = life.begin('cleanup')
        control(owner, 2)
        await wait_event(entered)
        with pytest.raises(LifecycleDenied):
            op.check()
        for kind in ('cleanup', 'cancel', 'model'):
            with pytest.raises(LifecycleDenied):
                life.begin(kind)
        op.finish()
        release.release()
        status = await generation(owner, 2)
        # Capture preceded completion; conservative overcount is allowed.
        assert status['custody']['cleanup'] == 1 and not status['local_drained']
        status = await observed(owner, lambda s: s['local_drained'])
        assert sum(status['custody'].values()) == 0
        with pytest.raises(LifecycleDenied):
            life.begin('cancel')
    finally:
        release.release()
        await owner.stop()


@pytest.mark.parametrize('cancel', [False, True])
async def test_pending_publication_reaped_before_terminal_and_no_late_reopen(tmp_path, monkeypatch, cancel):
    entered, release = child_barrier(monkeypatch)
    life = BriefLifecycle()
    owner = owner_at(tmp_path, life)
    await owner.start()
    try:
        control(owner, 1, 'on')
        await generation(owner, 1)
        op = life.begin('model')
        op.uncertain()
        control(owner, 2)
        await wait_event(entered)
        process = owner._process
        if cancel:
            owner._task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await owner._task
        else:
            stopping = asyncio.create_task(owner.stop())
            async with asyncio.timeout(1):
                while not life.snapshot().shutdown:
                    await asyncio.sleep(.01)
            with pytest.raises(LifecycleDenied):
                life.begin('cancel')
            await asyncio.wait_for(stopping, 3)
        assert owner._process is None
        assert process._closed  # join and close, not merely terminate requested
        terminal = (owner.status_dir / 'status.json').read_bytes()
        status = json.loads(terminal)
        assert status['lifecycle'] == 'stopped' and status['custody']['model'] == 1
        assert status['uncertain'] and not status['local_drained']
        release.release()
        assert (owner.status_dir / 'status.json').read_bytes() == terminal
        op.finish()
        assert life.snapshot().custody['model'] == 1
    finally:
        release.release()
        if not cancel:
            await owner.stop()


async def test_attached_contention_and_supported_schema_transition(tmp_path):
    from ostag.briefs.runtime import BriefStatusOwner

    directory = tmp_path / 'status'
    directory.mkdir()
    legacy = BriefStatusOwner(directory, tmp_path / 'control.json')
    await legacy.start()
    await legacy.stop()
    first_life = BriefLifecycle()
    first = owner_at(tmp_path, first_life)
    await first.start()
    second_life = BriefLifecycle()
    second_life.apply(1, 'on', False)
    second = owner_at(tmp_path, second_life)
    try:
        await second.start()
        assert second.reason == 'ownership_conflict'
        assert second.last_status is None
        assert second_life.snapshot().shutdown
        assert json.loads((directory / 'status.json').read_text())['boot_id'] == first.boot_id
        control(first, 1)
        assert (await generation(first, 1))['version'] == 2
    finally:
        await second.stop()
        await first.stop()
    # The valid supported v2 can also transition back to disabled v1.
    legacy = BriefStatusOwner(directory, tmp_path / 'control.json')
    await legacy.start()
    try:
        assert legacy.last_status['version'] == 1
        assert not legacy.last_status['control_valid']
    finally:
        await legacy.stop()


async def test_invalid_and_unsafe_controls_close_actual_admission(tmp_path):
    import os

    life = BriefLifecycle()
    owner = owner_at(tmp_path, life)
    await owner.start()
    try:
        control(owner, 1, 'on')
        await generation(owner, 1)
        owner.control_path.write_bytes(b'x' * 4097)
        await observed(owner, lambda s: s['component']['reason'] == 'control_invalid')
        for raw in (b'{}', b'{"version":1,"version":1}', b'\xff'):
            previous = owner.last_status['request_id']
            owner.control_path.write_bytes(raw)
            await observed(owner, lambda s: s['request_id'] > previous)
            with pytest.raises(LifecycleDenied):
                life.begin('cleanup')
        owner.control_path.unlink()
        target = tmp_path / 'unsafe-target'
        target.write_text('untouched')
        for kind in ('symlink', 'hardlink', 'fifo'):
            if kind == 'symlink':
                owner.control_path.symlink_to(target)
            elif kind == 'hardlink':
                os.link(target, owner.control_path)
            else:
                os.mkfifo(owner.control_path)
            previous = owner.last_status['request_id']
            await observed(owner, lambda s: s['request_id'] > previous)
            assert not life.snapshot().control_valid
            owner.control_path.unlink()
        assert target.read_text() == 'untouched'
        control(owner, 2)
        assert (await generation(owner, 2))['local_drained']
    finally:
        await owner.stop()


async def test_corrupt_status_cannot_be_overwritten_or_open_business(tmp_path):
    life = BriefLifecycle()
    owner = owner_at(tmp_path, life)
    path = owner.status_dir / 'status.json'
    raw = '{"version":99,"reconciled":true}'
    path.write_text(raw)
    await owner.start()
    await owner.stop()
    assert owner.last_status is None
    assert path.read_text() == raw
    assert life.snapshot().shutdown
    with pytest.raises(LifecycleDenied):
        life.begin('intake')
