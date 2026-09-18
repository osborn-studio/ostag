"""Explicit local custody, optionally sealed by runtime's file-control owner.

Standalone control is never acquired from events or models. A snapshot describes
only operations admitted through this object, never remote cancellation, durable
queue emptiness or a deploy ACK. Counts overlap (e.g. worker plus model), and
include permanently uncertain receipts, not just executing tasks. Pausing cannot
undo a COMMIT already in progress;
custody covers that interval and the caller must check again before release.

Tokens have no destructor. Lost/GC'd tokens remain counted. Exchange exceptions,
cancellation and loop teardown latch uncertainty permanently: only constructing
a separately reconciled composition can establish a new baseline. Even successful
late return cannot clear a previously latched unknown. No lock encloses caller
code, SQLite, transport, clock access or await. Trusted bindings must retain the
same lifecycle for their whole lifetime; legacy construction is unchanged.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from threading import Lock
from time import monotonic
from types import MappingProxyType
from typing import Mapping

from ostag.core.schemas import SchemaError

_KINDS = ('intake', 'worker', 'model', 'card', 'send', 'cleanup', 'cancel')
_AUTHORITY_LIFETIME = 0.75


def _clock():
    # Clock access is outside the lifecycle mutex, including exceptional clocks.
    try:
        value = monotonic()
        return value if type(value) in (int, float) and math.isfinite(value) else None
    except Exception:
        return None


class LifecycleDenied(SchemaError):
    def __init__(self):
        super().__init__('lifecycle_denied', 'Local lifecycle denied.',
                         'Use current trusted local control.')


@dataclass(frozen=True)
class LifecycleSnapshot:
    control_generation: int | None
    control_valid: bool
    admission: str
    maintenance: bool
    shutdown: bool
    custody: Mapping[str, int]
    uncertain: bool
    drained: bool


class Operation:
    """Opaque epoch pin and custody receipt. finish is explicit, never GC-driven."""
    def __init__(self, owner, epoch, kind):
        self._owner, self._epoch, self._kind = owner, epoch, kind

    def check(self):
        self._owner._check(self)

    def check_release(self):
        """Fence a public return after actual executor completion released custody."""
        self._owner._check_release(self)

    def allowed(self):
        try:
            self.check()
            return True
        except LifecycleDenied:
            return False

    def finish(self):
        self._owner._finish(self)

    def uncertain(self):
        self._owner._uncertain(self)


class BriefLifecycle:
    def __init__(self, *, capacity=64):
        if type(capacity) is not int or not 1 <= capacity <= 256:
            raise LifecycleDenied()
        self._lock = Lock()
        self._capacity = capacity
        self._generation = None
        self._accepted = None
        self._valid = False
        self._admission, self._maintenance = 'off', False
        self._shutdown = False
        self._epoch = 0
        self._operations = {}  # token -> permanently uncertain boolean
        self._worker = None
        self._file_owner = None
        self._publishing = False
        self._ready = False
        self._deadline = None
        self._last_clock = None
        self._clock_failed = False

    def _close_locked(self):
        self._valid = self._ready = False
        self._admission, self._maintenance = 'off', False
        self._deadline = None
        self._epoch += 1

    def _time(self):
        # Capture the clock floor BEFORE calling the clock, so concurrent reads
        # finishing in reverse order do not look like a system clock rollback.
        with self._lock:
            before = self._last_clock
        return _clock(), before

    def _fresh_locked(self, reading):
        if self._file_owner is None:
            return
        if reading is None:  # Binding won a race with an unbound operation.
            self._close_locked()
            return
        now, before = reading
        if now is None or (before is not None and now < before):
            self._clock_failed = True
        if now is not None:
            self._last_clock = max(now, self._last_clock or now)
            now = self._last_clock
        if self._clock_failed or (self._deadline is not None and now >= self._deadline):
            self._close_locked()
        return now

    def _bind_file_owner(self, owner):
        """Lifetime binding, before spawn or filesystem waits; never resets custody."""
        with self._lock:
            if self._file_owner is not None or self._shutdown:
                raise LifecycleDenied()
            self._file_owner = owner
            self._close_locked()

    def _file_close(self, owner):
        with self._lock:
            if self._file_owner is not owner:
                raise LifecycleDenied()
            self._close_locked()

    def _file_prepare(self, owner, control, sampled_at):
        """Seal new receipts through publication; existing work may only finish.

        An unchanged on lease permits existing checks until its ORIGINAL expiry.
        New/on control cannot authorize anything until _file_ack. The returned
        observation follows application of off and precedes any new admission.
        """
        reading = self._time()
        with self._lock:
            now = self._fresh_locked(reading)
            if self._file_owner is not owner or self._shutdown or self._clock_failed:
                raise LifecycleDenied()
            self._publishing = True
            if control is None:
                self._close_locked()
            else:
                generation, admission, maintenance = control
                update = (generation, admission, maintenance)
                if (sampled_at is None or now < sampled_at
                        or now >= sampled_at + _AUTHORITY_LIFETIME
                        or type(generation) is not int or not 0 < generation < 2**63
                        or type(admission) is not str or admission not in ('on', 'off')
                        or type(maintenance) is not bool
                        or (maintenance and (admission == 'on' or self._operations))
                        or (self._generation is not None and generation < self._generation)
                        or (generation == self._generation and update != self._accepted)):
                    self._close_locked()
                    raise LifecycleDenied()
                if update != self._accepted or not self._valid:
                    self._close_locked()
                    self._generation, self._accepted = generation, update
                    self._valid = True
                    self._admission, self._maintenance = admission, maintenance
                    self._deadline = sampled_at + _AUTHORITY_LIFETIME
            return self._epoch, self._snapshot_locked()

    def _file_ack(self, owner, epoch, sampled_at):
        """Acceptance point: exact publication ACK plus an unexpired sample.

        A newer pause, expiry, shutdown or clock fault cannot be undone by a
        queued ACK. No callback/IO runs under this mutex. This is local authority
        only, never evidence of durable startup reconciliation.
        """
        reading = self._time()
        with self._lock:
            now = self._fresh_locked(reading)
            if (self._file_owner is not owner or self._shutdown or self._clock_failed
                    or epoch != self._epoch or sampled_at is None or now < sampled_at
                    or now >= sampled_at + _AUTHORITY_LIFETIME):
                self._close_locked()
                raise LifecycleDenied()
            self._publishing = False
            if self._valid:
                self._deadline = sampled_at + _AUTHORITY_LIFETIME
                self._ready = True

    def apply(self, control_generation, admission, maintenance):
        """Trusted bounded control. Only an exact, still-valid replay is a no-op."""
        with self._lock:
            if self._file_owner is not None:
                raise LifecycleDenied()
            update = (control_generation, admission, maintenance)
            valid = (type(control_generation) is int and 0 < control_generation < 2**63
                     and type(admission) is str and admission in ('on', 'off')
                     and type(maintenance) is bool and not (maintenance and admission == 'on'))
            if (self._shutdown or not valid
                    or (self._generation is not None and control_generation < self._generation)
                    or (control_generation == self._generation
                        and (update != self._accepted or not self._valid))
                    or (maintenance and self._operations)):
                self._valid = False
                self._admission, self._maintenance = 'off', False
                self._epoch += 1
                raise LifecycleDenied()
            if update == self._accepted:
                return
            self._generation, self._accepted = control_generation, update
            self._valid = True
            self._admission, self._maintenance = admission, maintenance
            self._epoch += 1

    def shutdown(self):
        with self._lock:
            if not self._shutdown:
                self._shutdown = True
                self._admission = 'off'
                self._epoch += 1
                if self._file_owner is not None:
                    self._close_locked()

    def _permitted(self, kind):
        return (self._valid and not self._shutdown and not self._maintenance
                and (self._file_owner is None or (self._ready and self._admission == 'on'))
                and (kind in ('cleanup', 'cancel')
                     or (self._admission == 'on' and not any(self._operations.values()))))

    def begin(self, kind, *, parent=None):
        if (type(kind) is not str or kind not in _KINDS
                or (parent is not None
                    and (type(parent) is not Operation or parent._owner is not self))):
            raise LifecycleDenied()
        reading = self._time() if self._file_owner is not None else None
        with self._lock:
            self._fresh_locked(reading)
            if (self._publishing or not self._permitted(kind)
                    or len(self._operations) >= self._capacity
                    or (parent is not None and not self._current(parent))):
                raise LifecycleDenied()
            op = Operation(self, self._epoch, kind)
            self._operations[op] = False
            return op

    def _current(self, op):
        return (op in self._operations and op._epoch == self._epoch
                and self._permitted(op._kind))

    def _check(self, op):
        reading = self._time() if self._file_owner is not None else None
        with self._lock:
            self._fresh_locked(reading)
            if not self._current(op):
                raise LifecycleDenied()

    def _check_release(self, op):
        reading = self._time() if self._file_owner is not None else None
        with self._lock:
            self._fresh_locked(reading)
            if op._epoch != self._epoch or not self._permitted(op._kind):
                raise LifecycleDenied()

    def _finish(self, op):
        with self._lock:
            if op in self._operations and not self._operations[op]:
                del self._operations[op]

    def _uncertain(self, op):
        with self._lock:
            if op in self._operations:
                self._operations[op] = True

    def own_worker(self, owner):
        """Lifetime owner; rejection precedes creation of any worker threads."""
        with self._lock:
            if self._shutdown or (self._worker is not None and self._worker is not owner):
                raise LifecycleDenied()
            self._worker = owner

    def snapshot(self):
        reading = self._time() if self._file_owner is not None else None
        with self._lock:
            self._fresh_locked(reading)
            return self._snapshot_locked()

    def _snapshot_locked(self):
        counts = dict.fromkeys(_KINDS, 0)
        for op in self._operations:
            counts[op._kind] += 1
        uncertain = any(self._operations.values())
        drained = (self._valid and self._admission == 'off' and not self._shutdown
                   and not self._operations)
        return LifecycleSnapshot(self._generation, self._valid, self._admission,
                                 self._maintenance, self._shutdown,
                                 MappingProxyType(counts), uncertain, drained)

    def cleanup(self, retention, scope, job):
        """One explicit A10 cleanup; no discovery, scanning or scheduler.

        A10 owns bounded target cleanup/tombstones. Custody spans all its IO;
        maintenance cannot start while it runs. Pause may not undo retirement.
        """
        from ostag.briefs.retention import RetentionCoordinator
        if type(retention) is not RetentionCoordinator:
            raise LifecycleDenied()
        op = self.begin('cleanup')
        try:
            op.check()
            result = retention.cleanup(scope, job)
            op.check()
            return result
        finally:
            op.finish()


def check_binding(lifecycle):
    if lifecycle is not None and type(lifecycle) is not BriefLifecycle:
        raise LifecycleDenied()
