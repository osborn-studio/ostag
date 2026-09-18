"""Explicit trusted local composition control; unrelated to runtime.py status.

No control is acquired from events, models, files or clocks. A snapshot describes
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
from threading import Lock
from types import MappingProxyType
from typing import Mapping

from ostag.core.schemas import SchemaError

_KINDS = ('intake', 'worker', 'model', 'card', 'send', 'cleanup', 'cancel')


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

    def apply(self, control_generation, admission, maintenance):
        """Trusted bounded control. Only an exact, still-valid replay is a no-op."""
        with self._lock:
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

    def _permitted(self, kind):
        return (self._valid and not self._shutdown and not self._maintenance
                and (kind in ('cleanup', 'cancel')
                     or (self._admission == 'on' and not any(self._operations.values()))))

    def begin(self, kind, *, parent=None):
        if (type(kind) is not str or kind not in _KINDS
                or (parent is not None
                    and (type(parent) is not Operation or parent._owner is not self))):
            raise LifecycleDenied()
        with self._lock:
            if (not self._permitted(kind) or len(self._operations) >= self._capacity
                    or (parent is not None and not self._current(parent))):
                raise LifecycleDenied()
            op = Operation(self, self._epoch, kind)
            self._operations[op] = False
            return op

    def _current(self, op):
        return (op in self._operations and op._epoch == self._epoch
                and self._permitted(op._kind))

    def _check(self, op):
        with self._lock:
            if not self._current(op):
                raise LifecycleDenied()

    def _check_release(self, op):
        with self._lock:
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
        with self._lock:
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
