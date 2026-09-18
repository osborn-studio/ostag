"""Local control counts custody; it never attests remote cancellation."""
from dataclasses import FrozenInstanceError
from threading import Barrier, Thread

import pytest

from ostag.briefs.lifecycle import BriefLifecycle, LifecycleDenied


def test_closed_monotonic_control_and_epoch_invalidation():
    life = BriefLifecycle()
    with pytest.raises(LifecycleDenied):
        life.begin('intake')
    life.apply(1, 'on', False)
    token = life.begin('intake')
    life.apply(1, 'on', False)
    token.check()
    life.apply(2, 'off', False)
    life.apply(3, 'on', False)
    with pytest.raises(LifecycleDenied):
        token.check()
    token.finish()
    life.apply(4, 'off', False)
    assert life.snapshot().drained
    with pytest.raises(FrozenInstanceError):
        life.snapshot().drained = False


def test_invalid_control_fails_closed_and_never_revives_tokens():
    for invalid in (True, 0, -1, 2**63, '2'):
        life = BriefLifecycle()
        life.apply(1, 'on', False)
        token = life.begin('worker')
        with pytest.raises(LifecycleDenied):
            life.apply(invalid, 'on', False)
        assert not life.snapshot().control_valid
        life.apply(2, 'on', False)
        with pytest.raises(LifecycleDenied):
            token.check()
        token.finish()
    with pytest.raises(LifecycleDenied):
        life.apply(2, 'off', False)
    with pytest.raises(LifecycleDenied):
        life.apply(1, 'on', False)


def test_maintenance_and_uncertainty_are_conservative():
    life = BriefLifecycle()
    life.apply(1, 'on', False)
    op = life.begin('send')
    with pytest.raises(LifecycleDenied):
        life.apply(2, 'off', True)
    op.uncertain()
    op.finish()  # Cannot erase an unknown exchange.
    life.apply(3, 'off', False)
    assert life.snapshot().custody['send'] == 1
    assert life.snapshot().uncertain and not life.snapshot().drained
    with pytest.raises(LifecycleDenied):
        life.begin('card')
    life.apply(4, "on", False)
    with pytest.raises(LifecycleDenied):
        life.begin("model")
    cancellation = life.begin("cancel")
    cancellation.finish()
    assert life.snapshot().custody["send"] == 1
    life.shutdown()
    with pytest.raises(LifecycleDenied):
        life.apply(5, 'on', False)


def test_bounded_custody_and_concurrent_immutable_snapshots():
    life = BriefLifecycle(capacity=4)
    life.apply(1, 'on', False)
    barrier = Barrier(4)
    def exercise():
        op = life.begin('intake')
        barrier.wait(timeout=2)
        snapshot = life.snapshot()
        with pytest.raises(TypeError):
            snapshot.custody['intake'] = 0
        op.finish()
    threads = [Thread(target=exercise) for _ in range(3)]
    for thread in threads:
        thread.start()
    barrier.wait(timeout=2)
    for thread in threads:
        thread.join(2)
        assert not thread.is_alive()
    ops = [life.begin('intake') for _ in range(4)]
    with pytest.raises(LifecycleDenied):
        life.begin('intake')
    for op in ops:
        op.finish()
    life.apply(2, 'off', True)
    assert life.snapshot().drained
    with pytest.raises(LifecycleDenied):
        life.begin('cleanup')
