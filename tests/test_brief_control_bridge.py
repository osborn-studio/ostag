"""The attached observation is local custody, never a deployment drain ACK."""
import asyncio
import json
import time

import pytest

from ostag.briefs.lifecycle import BriefLifecycle, LifecycleDenied
from ostag.briefs.runtime import BriefStatusOwner, validate_drain_status


def control(owner, generation, admission='off', maintenance=False):
    value = dict(version=1, boot_id=owner.boot_id, generation=generation,
                 admission=admission, maintenance=maintenance)
    temporary = owner.control_path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value))
    temporary.replace(owner.control_path)


async def observed(owner, predicate):
    async with asyncio.timeout(4):
        while owner.last_status is None or not predicate(owner.last_status):
            await asyncio.sleep(.01)
    return owner.last_status


async def generation(owner, number):
    return await observed(owner, lambda s: s['control_generation'] == number
                          and s['control_valid'])


def owner_at(tmp_path, life):
    directory = tmp_path / 'status'
    directory.mkdir(exist_ok=True)
    return BriefStatusOwner(directory, tmp_path / 'control.json', lifecycle=life)


def test_binding_closes_immediately_and_is_exclusive_for_lifetime(tmp_path):
    life = BriefLifecycle()
    life.apply(1, 'on', False)
    op = life.begin('model')
    op.uncertain()
    owner_at(tmp_path, life)
    with pytest.raises(LifecycleDenied):
        op.check()
    with pytest.raises(LifecycleDenied):
        life.apply(2, 'on', False)
    with pytest.raises(LifecycleDenied):
        owner_at(tmp_path, life)
    op.finish()
    assert life.snapshot().custody['model'] == 1
    assert life.snapshot().uncertain


async def test_real_child_seals_zero_and_cannot_prove_production_drain(tmp_path):
    life = BriefLifecycle()
    owner = owner_at(tmp_path, life)
    await owner.start()
    try:
        assert owner.last_status['version'] == 2
        assert not life.snapshot().control_valid
        control(owner, 1)
        status = await generation(owner, 1)
        assert status['local_drained'] and not status['drain_ack']
        assert set(status['custody']) == {
            'intake', 'worker', 'model', 'card', 'send', 'cleanup', 'cancel'}
        for kind in status['custody']:
            with pytest.raises(LifecycleDenied):
                life.begin(kind)
        assert not validate_drain_status(status, owner.boot_id, 1, now=time.time())
        control(owner, 2, 'on')
        await generation(owner, 2)
        op = life.begin('model')
        control(owner, 3)
        status = await generation(owner, 3)
        assert status['custody']['model'] == 1 and not status['local_drained']
        control(owner, 4, 'on')
        await generation(owner, 4)
        with pytest.raises(LifecycleDenied):
            op.check()
        op.finish()
    finally:
        await owner.stop()
    assert life.snapshot().shutdown
    assert owner.last_status['lifecycle'] == 'stopped'
    with pytest.raises(LifecycleDenied):
        owner_at(tmp_path, life)


async def test_wrong_boot_missing_replay_and_maintenance_busy_fail_closed(tmp_path):
    from uuid import uuid4

    life = BriefLifecycle()
    owner = owner_at(tmp_path, life)
    await owner.start()
    try:
        control(owner, 1, 'on')
        await generation(owner, 1)
        op = life.begin('cleanup')
        control(owner, 2, maintenance=True)
        status = await observed(owner, lambda s: s['component']['reason'] == 'control_denied')
        assert not status['maintenance'] and status['custody']['cleanup'] == 1
        with pytest.raises(LifecycleDenied):
            op.check()
        op.finish()
        control(owner, 3, maintenance=True)
        assert (await generation(owner, 3))['local_drained']
        with pytest.raises(LifecycleDenied):
            life.begin('cancel')
        # Changed replay and regression are never accepted by the child.
        control(owner, 3, 'on')
        await observed(owner, lambda s: s['component']['reason'] == 'control_stale')
        control(owner, 2, 'on')
        await observed(owner, lambda s: s['component']['reason'] == 'control_stale')
        control(owner, 4, 'on')
        await generation(owner, 4)
        owner.control_path.write_text(json.dumps(dict(version=1, boot_id=str(uuid4()),
            generation=5, admission='on', maintenance=False)))
        await observed(owner, lambda s: s['component']['reason'] == 'control_wrong_boot')
        with pytest.raises(LifecycleDenied):
            life.begin('intake')
        owner.control_path.unlink()
        await observed(owner, lambda s: s['component']['reason'] == 'control_missing')
    finally:
        await owner.stop()


async def test_parent_loop_stall_expires_operations_and_old_epoch(tmp_path):
    from threading import Event, Thread
    from ostag.briefs.lifecycle import _AUTHORITY_LIFETIME

    life = BriefLifecycle()
    owner = owner_at(tmp_path, life)
    await owner.start()
    errors = []
    done = Event()
    try:
        control(owner, 1, 'on')
        await generation(owner, 1)
        op = life.begin('model')

        def during_stall():
            try:
                # Deliberately cross the documented lease bound with the parent
                # loop stopped. This timeout IS the behavior under test.
                Event().wait(_AUTHORITY_LIFETIME + .05)
                for action in (op.check, op.check_release, lambda: life.begin('cleanup')):
                    with pytest.raises(LifecycleDenied):
                        action()
                assert life.snapshot().custody['model'] == 1
            except BaseException as exc:
                errors.append(exc)
            finally:
                done.set()

        thread = Thread(target=during_stall)
        thread.start()
        assert done.wait(2)  # Intentionally stall this asyncio loop.
        thread.join(1)
        assert not errors
        control(owner, 2, 'on')
        await generation(owner, 2)
        with pytest.raises(LifecycleDenied):
            op.check()
        op.finish()
    finally:
        await owner.stop()


async def test_clock_failure_and_rollback_cannot_extend_authority(tmp_path, monkeypatch):
    from ostag.briefs import lifecycle

    life = BriefLifecycle()
    owner = owner_at(tmp_path, life)
    await owner.start()
    try:
        control(owner, 1, 'on')
        await generation(owner, 1)
        op = life.begin('model')
        with monkeypatch.context() as patch:
            patch.setattr(lifecycle, 'monotonic', lambda: 0)
            with pytest.raises(LifecycleDenied):
                op.check()
        # A good clock cannot erase the latched failure.
        with pytest.raises(LifecycleDenied):
            life.begin('cancel')
        op.uncertain()
        op.finish()
        assert life.snapshot().custody['model'] == 1
    finally:
        await owner.stop()


def test_attached_schema_is_exact_bounded_and_never_a_waiver(tmp_path):
    from copy import deepcopy
    from ostag.briefs.runtime import _attached_shape, _observation, _status_shape
    from uuid import uuid4

    life = BriefLifecycle()
    life.apply(1, 'off', False)
    status = _observation(life.snapshot(), str(uuid4()), 1, 'running', 'closed')
    assert _attached_shape(status)
    mutations = [
        ('version', True), ('version', 3), ('protocol', 'unknown'), ('request_id', True),
        ('request_id', 2**63), ('control_generation', 2**63), ('control_valid', 1),
        ('maintenance', 1), ('uncertain', True), ('shutdown', True), ('drain_ack', True),
        ('local_drained', 1), ('updated_at', float('nan')), ('updated_at', 10**400),
        ('custody', dict(status['custody'], model=True)),
        ('custody', dict(status['custody'], model=-1)),
        ('custody', dict(status['custody'], model=257)),
        ('custody', dict(status['custody'], model=200, worker=200)),
        ('component', {'status': 'ok', 'reason': 'private exception'}),
        ('reconciled', True), ('active_leases', 0),
    ]
    for field, value in mutations:
        altered = deepcopy(status)
        altered[field] = value
        assert not _status_shape(altered), field
        assert not validate_drain_status(altered, status['boot_id'], 1, now=0), field
    assert not validate_drain_status(status, status['boot_id'], 1, now=0)


def test_concrete_binding_only_and_standalone_cleanup_stays_supported(tmp_path):
    class Pretend(BriefLifecycle):
        pass
    for bad in ({}, lambda: {}, Pretend()):
        with pytest.raises(LifecycleDenied):
            owner_at(tmp_path, bad)
    life = BriefLifecycle()
    life.apply(1, 'off', False)
    for kind in ('cleanup', 'cancel'):
        op = life.begin(kind)
        op.finish()
    assert life.snapshot().drained


@pytest.mark.parametrize('bad', ['exception', 'nan'])
async def test_failed_clock_denies_without_holding_lifecycle_mutex(tmp_path, monkeypatch, bad):
    from ostag.briefs import lifecycle

    life = BriefLifecycle()
    owner = owner_at(tmp_path, life)
    await owner.start()
    try:
        control(owner, 1, 'on')
        await generation(owner, 1)
        op = life.begin('model')

        def failed_clock():
            assert life._lock.acquire(blocking=False), 'clock called under lifecycle mutex'
            life._lock.release()
            if bad == 'exception':
                raise RuntimeError('private clock failure')
            return float('nan')

        with monkeypatch.context() as patch:
            patch.setattr(lifecycle, 'monotonic', failed_clock)
            with pytest.raises(LifecycleDenied):
                op.check_release()
        assert not life.snapshot().control_valid
        with pytest.raises(LifecycleDenied):
            life.begin('intake')
        op.finish()
    finally:
        await owner.stop()


async def test_owner_reuse_rejected_before_child_spawn(tmp_path, monkeypatch):
    life = BriefLifecycle()
    owner = owner_at(tmp_path, life)
    await owner.start()
    await owner.stop()

    def forbidden():
        pytest.fail('reused owner started child work')

    monkeypatch.setattr(owner, '_spawn', forbidden)
    with pytest.raises(RuntimeError):
        await owner.start()
    with pytest.raises(LifecycleDenied):
        owner_at(tmp_path, life)


async def test_ack_replaces_previous_zero_before_opening_to_other_threads(tmp_path, monkeypatch):
    life = BriefLifecycle()
    owner = owner_at(tmp_path, life)
    await owner.start()
    try:
        control(owner, 1)
        assert (await generation(owner, 1))['local_drained']
        original = life._file_ack
        checked = asyncio.Event()

        def accepted(*args):
            original(*args)
            if life.snapshot().admission == 'on':
                op = life.begin('model')
                try:
                    assert owner.last_status['control_generation'] == 2
                    assert owner.last_status['admission'] == 'on'
                    assert not owner.last_status['local_drained']
                    checked.set()
                finally:
                    op.finish()

        monkeypatch.setattr(life, '_file_ack', accepted)
        control(owner, 2, 'on')
        await asyncio.wait_for(checked.wait(), 3)
    finally:
        await owner.stop()
