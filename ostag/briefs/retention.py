"""A10 opt-in LOCAL retention authority. No scheduler, server wiring or remote recall.

Trusted composition lifecycle (A11 must serialize admission/dispatch): initialize
RetentionLedger; construct a coordinator with ALL owned target instances; pass
coordinator.guard(target) as retention= to each service; submit a stored A04 job;
register(scope, job_id, policy=RetentionPolicy(expires_at)); only then dispatch.
After completion, repeat the SAME registration to pin the original outbox/revision
before enabling previews/delivery/revisions. Registration reads the authoritative
BriefStore, never a request-supplied payload, manifest, file path or source cache.
A crash between admission and registration leaves an untracked, denied job.
Policy/clock and maintenance authorizer are injected LOCAL trusted composition,
not HTTP fields. RequesterContext must come from authenticated adapter identity.

revoke_job is scoped to that requester. revoke_source/revoke_grant require an
injected maintenance(actor, owner_scope, operation) returning None. cleanup and
status are scoped; cleanup_as requires the same administrative authorization.
Call expire(scope, job) or status to persist expiry; then cleanup, retrying until
complete. Every gate independently observes exclusive expiry without a sweeper.

Resource keys use scope + trusted resource_id, independent of source/job/delivery
aliases and grants. Grant keys use scope + grant_id. Snapshot keys use resource +
resource_version + content digest, NOT cache source_id/observed_at/grant aliases.
Expired snapshots never return; a genuinely newer version/content may be admitted.
Resource/grant revocation is permanent, including newer snapshots. For unknown
versions the same resource/content is conservatively the same snapshot. These
hashes identify trusted frozen metadata; they are NOT ACLs or re-verification.
Original current source/grant/private-binding authorizers remain mandatory.

Lock order: a service finishes primary SQL/waits/rendering, then uses one bounded
(50ms busy timeout) retention transaction, then its original nonblocking current
source/grant/private-binding mirror, then the captured minimum retention expiry
and clock-rollback fence, then existing operation clock/deadline/stop checks.
This is the combined logical authorization point; only the existing target
COMMIT or immediate release/transport handoff follows. OS scheduling/fsync and
already handed-off bytes cannot be made atomically revocable by this fence.

Compatibility: check() still returns None, and services without retention still
invoke their original mirror once. No schema or external authorizer signature
changes. A11 must supply nonblocking, non-reentrant current mirrors covering ALL
original authority, and one shared coordinator for local cancellation visibility.
For registered jobs the final fence observes completed cancellation signals from
THAT coordinator (including defensive denial if a callback violates its contract
by revoking). Unregistered intake has no job signal; its final fence covers the
captured snapshot deadline, and registration must recheck durable tombstones.
It is NOT an instantaneous process-wide/cross-instance mirror: a concurrent
revocation after the durable retention read, or a commit whose signal has not
yet published, may follow that read's decision and cannot retract it. Another
coordinator/process is observed by the NEXT durable check. A11 must serialize
such mutations with admission/dispatch if it needs a stronger ordering; adding
another SQL read after the final mirror would recreate the original defect.
Likewise each original mutable mirror is current at its own observation, not
an atomic snapshot of all authorities. Concurrent changes after that observation
can follow the decision. Final clock fences close elapsed-deadline gaps; they
do not turn sequential mirror reads into a distributed revocation transaction.

Retention NEVER holds a transaction while acquiring any target DB or invoking external code. This narrow local-I/O
exception is intentional; unavailable authority denies. No process cache grants
permission. Tombstone commit precedes local cancellation signal, primary fence,
and per-target cleanup. Each stage can be replayed after lost acknowledgement.
Signals precede waiting for primary SQL. Cross-database atomicity is not claimed.

Target identity pins owned local file identity (device/inode/birth time); reopening
that file is supported, replacing/restoring/copying it is NOT silently adopted.
Birth time comes from Python's native stat fields or Linux libc statx. A Linux
deployment requires statx and filesystem STATX_BTIME support; missing capabilities
fail closed and block launch. No inode-only or ctime/mtime identity fallback.
Target identities are PERSISTED in retention_targets, so they compare only within
one host filesystem AND interpreter build: Linux statx gives integer nanoseconds,
while macOS/BSD expose float st_birthtime before 3.12 and integer st_birthtime_ns
after. Moving owned files or crossing that supported-interpreter boundary voids
every stored identity, denying already registered jobs and holding their cleanup
pending until a deliberate migration re-pins them. That is fail-closed, never a
silent adoption. Changing the target set requires a future explicit migration,
not omission during cleanup. Tombstones/capacity slots never recycle. Current SQL cleanup uses secure_delete and owned-file VACUUM;
this does not erase external WAL/backups, SSD snapshots, RAM or remote cards.
Historical sends/spend remain facts, never current content verification/receipt.
"""
from __future__ import annotations

from contextlib import contextmanager
import ctypes
from dataclasses import dataclass, field
from functools import wraps
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat as stat_mode
import sys
from threading import Lock

from ostag.briefs.store import BriefStore, RequesterContext, _micros

_APP = 0x42525431
_RETIRED = '{"retired":true}'
_SCHEMA = (
    'CREATE TABLE retention_meta (id INTEGER PRIMARY KEY CHECK(id=1), '
    'highwater INTEGER NOT NULL, capacity INTEGER NOT NULL)',
    'CREATE TABLE retention_jobs (scope TEXT NOT NULL, job TEXT NOT NULL, '
    'binding TEXT NOT NULL, expires INTEGER NOT NULL, decision TEXT NOT NULL, '
    'revision TEXT, delivery TEXT, PRIMARY KEY(scope,job))',
    'CREATE TABLE retention_sources (scope TEXT NOT NULL, snapshot TEXT NOT NULL, '
    'resource TEXT NOT NULL, grant_key TEXT NOT NULL, expires INTEGER NOT NULL, '
    'expired INTEGER NOT NULL, PRIMARY KEY(scope,snapshot,grant_key))',
    'CREATE TABLE retention_links (scope TEXT NOT NULL, job TEXT NOT NULL, '
    'snapshot TEXT NOT NULL, grant_key TEXT NOT NULL, PRIMARY KEY(scope,job,snapshot,grant_key))',
    'CREATE TABLE retention_revokes (scope TEXT NOT NULL, kind TEXT NOT NULL, '
    'identity TEXT NOT NULL, PRIMARY KEY(scope,kind,identity))',
    'CREATE TABLE retention_targets (scope TEXT NOT NULL, job TEXT NOT NULL, '
    'kind TEXT NOT NULL, identity TEXT NOT NULL, done INTEGER NOT NULL, '
    'PRIMARY KEY(scope,job,kind))',
)


class RetentionDenied(RuntimeError):
    def __init__(self):
        super().__init__('brief retention unavailable')


def _safe(fn):
    @wraps(fn)
    def call(*args, **kwargs):
        failed = False
        try:
            return fn(*args, **kwargs)
        except Exception:
            failed = True
        if failed:
            raise RetentionDenied()
    return call


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True)


def _digest(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _id(value):
    if (type(value) is not str or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/@-]{0,255}', value)
            or '://' in value):
        raise RetentionDenied()
    return value


def _scope(scope):
    if type(scope) is not RequesterContext:
        raise RetentionDenied()
    scope.__post_init__()
    return _digest([scope.namespace, scope.principal.platform, scope.principal.actor_id])


def _time(value):
    # Integers are UTC microseconds, never booleans/floats or Unix seconds.
    if type(value) is str:
        value = _micros(value)
    if type(value) is not int or not 0 <= value < 10**17:
        raise RetentionDenied()
    return value


@dataclass(frozen=True, repr=False)
class RetentionPolicy:
    expires_at: str | int = field(repr=False)

    @_safe
    def __post_init__(self):
        object.__setattr__(self, 'expires_at', _time(self.expires_at))


@dataclass(frozen=True)
class RetentionStatus:
    decision: str
    cleanup: str
    pending_targets: int
    content_available: bool = False
    content_verified: bool = False
    reverifiable: bool = False
    human_received: bool = False
    remote_cards_may_persist: bool = True


class RetentionLedger:
    @_safe
    def __init__(self, path, *, capacity=10000):
        if type(capacity) is not int or not 1 <= capacity <= 100000:
            raise RetentionDenied()
        self.path, self.capacity = Path(path), capacity
        self._pinned = _identity(self) if self.path.exists() else None

    def _verify(self, db):
        if self._pinned is not None and _identity(self) != self._pinned:
            raise RetentionDenied()
        actual = [tuple(r) for r in db.execute(
            "SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_autoindex_%' "
            'ORDER BY name')]
        expected = sorted([('table', s.split()[2], s) for s in _SCHEMA], key=lambda r: r[1])
        if (actual != expected or db.execute('PRAGMA application_id').fetchone()[0] != _APP
                or db.execute('PRAGMA user_version').fetchone()[0] != 1
                or db.execute('PRAGMA journal_mode').fetchone()[0] != 'delete'):
            raise RetentionDenied()
        rows = db.execute('SELECT * FROM retention_meta').fetchall()
        if (len(rows) != 1 or tuple(rows[0]) != (1, rows[0]['highwater'], self.capacity)
                or type(rows[0]['highwater']) is not int):
            raise RetentionDenied()
        _time(rows[0]['highwater'])
        count = sum(db.execute('SELECT count(*) FROM ' + sql.split()[2]).fetchone()[0]
                    for sql in _SCHEMA[1:])
        if count > self.capacity:
            raise RetentionDenied()

    @contextmanager
    def _connection(self, *, create=False):
        if (self.path.is_symlink() or (not create and self._pinned is None)
                or (self._pinned is not None and _identity(self) != self._pinned)):
            raise RetentionDenied()
        db = sqlite3.connect(self.path.resolve().as_uri() +
            ('?mode=rwc' if create else '?mode=rw'), uri=True, timeout=0.05, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            yield db
        finally:
            db.close()

    @_safe
    def initialize(self):
        with self._connection(create=True) as db:
            def empty():
                return (not db.execute('SELECT name FROM sqlite_master').fetchall()
                        and db.execute('PRAGMA application_id').fetchone()[0] == 0
                        and db.execute('PRAGMA user_version').fetchone()[0] == 0
                        and db.execute('PRAGMA journal_mode').fetchone()[0] == 'delete')
            if not empty():
                self._verify(db)
            db.execute('PRAGMA synchronous=EXTRA')
            db.execute('BEGIN IMMEDIATE')
            if empty():
                for ddl in _SCHEMA:
                    db.execute(ddl)
                db.execute('INSERT INTO retention_meta VALUES(1,0,?)', (self.capacity,))
                db.execute(f'PRAGMA application_id={_APP}')
                db.execute('PRAGMA user_version=1')
            self._verify(db)
            db.commit()
            self._pinned = _identity(self)

    @contextmanager
    def transaction(self, clock):
        with self._connection() as db:
            self._verify(db)
            db.execute('PRAGMA synchronous=EXTRA')
            db.execute('BEGIN IMMEDIATE')
            self._verify(db)
            stamp = _time(clock())
            if stamp < db.execute('SELECT highwater FROM retention_meta').fetchone()[0]:
                raise RetentionDenied()
            db.execute('UPDATE retention_meta SET highwater=?', (stamp,))
            yield db
            self._verify(db)
            db.commit()


def _kind(target):
    from ostag.briefs.card_delivery import CardLedger
    from ostag.briefs.callbacks import CallbackLedger
    from ostag.briefs.revisions import RevisionLedger
    kinds = {BriefStore: 'primary', CardLedger: 'cards', CallbackLedger: 'callbacks',
             RevisionLedger: 'revisions'}
    if type(target) not in kinds:
        raise RetentionDenied()
    return kinds[type(target)]


class _StatxTimestamp(ctypes.Structure):
    _fields_ = [('seconds', ctypes.c_int64), ('nanos', ctypes.c_uint32),
                ('reserved', ctypes.c_int32)]


class _Statx(ctypes.Structure):
    # Fixed Linux UAPI prefix, with the unused extensible tail kept opaque.
    # https://raw.githubusercontent.com/torvalds/linux/master/include/uapi/linux/stat.h
    _fields_ = [
        ('mask', ctypes.c_uint32), ('blksize', ctypes.c_uint32),
        ('attributes', ctypes.c_uint64), ('nlink', ctypes.c_uint32),
        ('uid', ctypes.c_uint32), ('gid', ctypes.c_uint32),
        ('mode', ctypes.c_uint16), ('spare0', ctypes.c_uint16),
        ('ino', ctypes.c_uint64), ('size', ctypes.c_uint64),
        ('blocks', ctypes.c_uint64), ('attributes_mask', ctypes.c_uint64),
        ('atime', _StatxTimestamp), ('btime', _StatxTimestamp),
        ('ctime', _StatxTimestamp), ('mtime', _StatxTimestamp),
        ('rdev_major', ctypes.c_uint32), ('rdev_minor', ctypes.c_uint32),
        ('dev_major', ctypes.c_uint32), ('dev_minor', ctypes.c_uint32),
        ('tail', ctypes.c_uint64 * 14),
    ]


def _linux_birthtime(path, sample):
    """One statx call on an O_PATH fd; no subprocess, syscall-number guess or retry.

    TYPE/INO/BTIME must be returned, not just requested: unsupported fields may
    contain fabricated values. Device fields are unconditional in the UAPI.
    Correlate fstat, statx and the named file while the descriptor pins its inode.
    This samples local file identity, not an atomic SQLite connection/path lock;
    existing ledger checks still revalidate at their transaction boundaries.
    Trusted composition must use owned local storage, not a remote filesystem
    with potentially unbounded metadata I/O or externally rewritable DB files.
    """
    if (sys.platform != 'linux' or ctypes.sizeof(_Statx) != 256
            or ctypes.sizeof(_StatxTimestamp) != 16
            or _StatxTimestamp.seconds.offset != 0 or _StatxTimestamp.nanos.offset != 8
            or _StatxTimestamp.reserved.offset != 12
            or any(getattr(_Statx, name).offset != offset for name, offset in (
                ('mask', 0), ('mode', 0x1c), ('ino', 0x20), ('btime', 0x50),
                ('dev_major', 0x88), ('dev_minor', 0x8c), ('tail', 0x90)))):
        raise RetentionDenied()
    # Resolve only libc's named wrapper in this process. Missing symbol denies.
    statx = ctypes.CDLL(None).statx
    statx.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_uint,
                     ctypes.POINTER(_Statx)]
    statx.restype = ctypes.c_int
    fd = os.open(path, os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        opened = os.fstat(fd)
        expected = (sample.st_dev, sample.st_ino)
        if (not stat_mode.S_ISREG(opened.st_mode)
                or (opened.st_dev, opened.st_ino) != expected):
            raise RetentionDenied()
        result = _Statx()
        required = 0x00000001 | 0x00000100 | 0x00000800  # TYPE | INO | BTIME
        # AT_EMPTY_PATH | AT_SYMLINK_NOFOLLOW, from include/uapi/linux/fcntl.h.
        if (statx(fd, b'', 0x1000 | 0x100, required, ctypes.byref(result)) != 0
                or result.mask & required != required
                or not stat_mode.S_ISREG(result.mode)
                or result.ino != opened.st_ino
                or (result.dev_major, result.dev_minor) != (
                    os.major(opened.st_dev), os.minor(opened.st_dev))
                or not 0 <= result.btime.nanos < 1000000000):
            raise RetentionDenied()
        current = path.stat(follow_symlinks=False)
        if (not stat_mode.S_ISREG(current.st_mode)
                or (current.st_dev, current.st_ino) != expected):
            raise RetentionDenied()
        return result.btime.seconds * 1000000000 + result.btime.nanos
    finally:
        os.close(fd)


@_safe
def _identity(target):
    if type(target) is not RetentionLedger:
        _kind(target)
    stat = target.path.stat(follow_symlinks=False)
    if not stat_mode.S_ISREG(stat.st_mode):
        raise RetentionDenied()
    birth = getattr(stat, 'st_birthtime_ns', getattr(stat, 'st_birthtime', None))
    if birth is None:
        birth = _linux_birthtime(target.path, stat)
    return _digest([stat.st_dev, stat.st_ino, birth])


@contextmanager
def owned_retirement(target, *, compact=False):
    """Sanctioned helpers only. Exact schema before mutation; no external paths."""
    kind = _kind(target)
    before = _identity(target)
    db = sqlite3.connect(target.path.resolve().as_uri() + '?mode=rw', uri=True,
                         timeout=0.05, isolation_level=None)
    db.row_factory = sqlite3.Row
    try:
        verify = target._inspect if kind == 'primary' else target._verify
        verify(db)
        db.execute('PRAGMA synchronous=EXTRA')
        db.execute('PRAGMA secure_delete=ON')
        db.execute('BEGIN EXCLUSIVE')
        verify(db)
        if _identity(target) != before:
            raise RetentionDenied()
        yield db
        verify(db)
        if _identity(target) != before:
            raise RetentionDenied()
        db.commit()
        if compact:
            # Earlier ordinary updates may have left bytes in free pages before
            # A10 enabled secure_delete. Rebuild this exact owned DB from its
            # remaining logical rows; unrelated scopes are retained. Failure is
            # pending cleanup, even though the scoped SQL retirement committed.
            verify(db)
            db.execute('VACUUM')
            verify(db)
            if _identity(target) != before:
                raise RetentionDenied()
    finally:
        db.close()


def configure_guard(guard, target):
    """Called by constructors; a bool/callable is never a retention authority."""
    if guard is not None:
        if type(guard) is not RetentionGuard or guard.target is not target:
            raise RetentionDenied()
        guard.validate()
    return guard is not None


def check_guard(service, scope, job, revision=None, *, authorize=None):
    if service._retention_enabled:
        guard = service.retention
        if (type(guard) is not RetentionGuard or not callable(getattr(guard, 'check', None))
                or guard.target is not getattr(service, 'store', getattr(service, 'ledger', None))):
            raise RetentionDenied()
        if authorize is None:
            guard.check(scope, job, revision)
        else:
            guard.check(scope, job, revision, authorize=authorize)
    elif authorize is not None and authorize() is not None:
        raise RetentionDenied()


class RetentionGuard:
    def __init__(self, coordinator, target):
        self.coordinator, self.target = coordinator, target
        self.identity = _identity(target)

    @_safe
    def validate(self):
        if (type(self.coordinator) is not RetentionCoordinator
                or self.coordinator.targets.get(_kind(self.target)) is not self.target
                or _identity(self.target) != self.identity):
            raise RetentionDenied()

    @_safe
    def check(self, scope, job, revision=None, *, authorize=None):
        """Bounded durable read, original mirror, then an exclusive expiry fence.

        Existing check callers still receive None. authorize is an optional
        internal composition hook, never requester input. It must be the same
        bounded nonblocking current mirror used without retention; no network,
        SQL, reentrancy or mutation is permitted by that contract. The final
        clock fence also denies if even this local callback crosses expiry.
        """
        self.validate()
        stamp, deadline = self.coordinator._check(scope, job, revision)
        if authorize is not None:
            if not callable(authorize) or authorize() is not None:
                raise RetentionDenied()
            self.coordinator._finish(scope, job, stamp, deadline)

    @_safe
    def intake(self, scope, sources, *, authorize=None):
        """Same mirror contract as check(); existing callers still receive None.

        Intake has no registered job/cancellation signal yet. Its durable
        snapshot decision precedes the mirror and final expiry fence. Concurrent
        tombstones after that decision are observed at registration/next gate;
        A11 must serialize admission/registration/dispatch as documented above.
        """
        self.validate()
        self.coordinator._intake(scope, sources, authorize=authorize)

    def watch(self, scope, job, signal):
        self.coordinator._watch(scope, job, signal)

    def unwatch(self, scope, job, signal):
        self.coordinator._unwatch(scope, job, signal)


def _keys(scope, source):
    from ostag.briefs.models import SourceRef
    if type(source) is not SourceRef or source.namespace != scope.namespace:
        raise RetentionDenied()
    # Trusted collected identities; never excerpt bytes in authority metadata.
    resource = _digest([_scope(scope), source.resource_id])
    return (resource, _digest([resource, source.resource_version, source.content_sha256]),
            _digest([_scope(scope), source.grant_id]))


class RetentionCoordinator:
    @_safe
    def __init__(self, *, ledger, store, clock, cards=None, callbacks=None, revisions=None,
                 maintenance=None, lifecycle=None):
        from ostag.briefs.lifecycle import check_binding
        check_binding(lifecycle)
        self.lifecycle = lifecycle
        # maintenance is the existing three-argument administrative authorizer,
        # not the lifecycle's read-only maintenance flag. cleanup() separately
        # acquires lifecycle custody and denies off/maintenance admissions.
        if (type(ledger) is not RetentionLedger or type(store) is not BriefStore
                or not callable(clock) or (maintenance is not None and not callable(maintenance))):
            raise RetentionDenied()
        self.ledger, self.store, self.clock, self.maintenance = ledger, store, clock, maintenance
        self.targets = {'primary': store}
        for kind, target in (('cards', cards), ('callbacks', callbacks), ('revisions', revisions)):
            if target is not None:
                if _kind(target) != kind:
                    raise RetentionDenied()
                self.targets[kind] = target
        self.identities = {kind: _identity(target) for kind, target in self.targets.items()}
        if len(set(self.identities.values())) != len(self.targets):
            raise RetentionDenied()
        self._signals, self._lock = {}, Lock()
        self._cancelled = set()  # Denial only; durable reads still grant every decision.

    @_safe
    def guard(self, target):
        if self.targets.get(_kind(target)) is not target:
            raise RetentionDenied()
        return RetentionGuard(self, target)

    def _watch(self, scope, job, signal):
        with self._lock:
            self._signals.setdefault((_scope(scope), job), set()).add(signal)

    def _unwatch(self, scope, job, signal):
        with self._lock:
            signals = self._signals.get((_scope(scope), job), set())
            signals.discard(signal)
            if not signals:
                self._signals.pop((_scope(scope), job), None)

    def _signal(self, scope, job):
        with self._lock:
            key = (_scope(scope), job)
            self._cancelled.add(key)
            for signal in self._signals.get(key, ()):
                signal.set()

    def _targets(self):
        actual = {kind: _identity(target) for kind, target in self.targets.items()}
        if actual != self.identities:
            raise RetentionDenied()
        return actual

    @staticmethod
    def _blocked(db, key, resource, snapshot, grant, stamp):
        if db.execute('SELECT 1 FROM retention_revokes WHERE scope=? AND '
            "((kind='resource' AND identity=?) OR (kind='grant' AND identity=?))",
                (key, resource, grant)).fetchone():
            return 'revoked'
        # Any job's immutable earlier expiry also retires its pinned snapshots,
        # even if nobody has polled that job since its deadline.
        elapsed = db.execute('SELECT 1 FROM retention_links l JOIN retention_jobs j '
            'ON j.scope=l.scope AND j.job=l.job WHERE l.scope=? AND l.snapshot=? '
            'AND j.expires<=?', (key, snapshot, stamp)).fetchone()
        rows = db.execute('SELECT expires,expired FROM retention_sources '
                          'WHERE scope=? AND snapshot=?', (key, snapshot)).fetchall()
        if elapsed or any(row['expired'] or stamp >= row['expires'] for row in rows):
            db.execute('UPDATE retention_sources SET expired=1 WHERE scope=? AND snapshot=?',
                       (key, snapshot))
            return 'expired'
        return None

    @staticmethod
    def _snapshot_deadline(db, key, snapshot):
        source = db.execute('SELECT min(expires) FROM retention_sources WHERE scope=? '
                            'AND snapshot=?', (key, snapshot)).fetchone()[0]
        job = db.execute('SELECT min(j.expires) FROM retention_links l JOIN retention_jobs j '
                         'ON j.scope=l.scope AND j.job=l.job WHERE l.scope=? AND l.snapshot=?',
                         (key, snapshot)).fetchone()[0]
        return min(v for v in (source, job, 10**17) if v is not None)

    def _deadline(self, db, key, row):
        """Effective authority is bounded by EVERY pinned snapshot, across jobs.

        Retain the minimum outside the transaction so final release cannot
        outlive an earlier shared-source deadline during SQL/commit processing.
        """
        deadline = row['expires']
        for link in db.execute('SELECT snapshot FROM retention_links WHERE scope=? AND job=?',
                               (key, row['job'])).fetchall():
            deadline = min(deadline, self._snapshot_deadline(db, key, link['snapshot']))
        return deadline

    def _decision(self, db, key, job, stamp):
        row = db.execute('SELECT * FROM retention_jobs WHERE scope=? AND job=?',
                         (key, job)).fetchone()
        if row is None:
            raise RetentionDenied()
        reason = row['decision']
        if reason == 'authorized':
            links = db.execute('SELECT s.* FROM retention_links l JOIN retention_sources s '
                'ON l.scope=s.scope AND l.snapshot=s.snapshot AND l.grant_key=s.grant_key '
                'WHERE l.scope=? AND l.job=?', (key, job)).fetchall()
            for source in links:
                blocked = self._blocked(db, key, source['resource'], source['snapshot'],
                                        source['grant_key'], stamp)
                if blocked:
                    reason = blocked
                    break
            if reason == 'authorized' and stamp >= row['expires']:
                reason = 'expired'
            if reason != 'authorized':
                db.execute('UPDATE retention_jobs SET decision=? WHERE scope=? AND job=?',
                           (reason, key, job))
                if reason == 'expired':
                    db.execute('UPDATE retention_sources SET expired=1 WHERE scope=? AND snapshot '
                        'IN (SELECT snapshot FROM retention_links WHERE scope=? AND job=?)',
                        (key, key, job))
        return row, reason

    def _intake(self, scope, sources, *, authorize=None):
        key, stamp = _scope(scope), _time(self.clock())
        identities = [_keys(scope, s) for s in sources]
        denied, deadline = False, 10**17
        with self.ledger.transaction(self.clock) as db:
            stamp = db.execute('SELECT highwater FROM retention_meta').fetchone()[0]
            for resource, snapshot, grant in identities:
                denied |= self._blocked(db, key, resource, snapshot, grant, stamp) is not None
                deadline = min(deadline, self._snapshot_deadline(db, key, snapshot))
        if not denied and authorize is not None:
            if not callable(authorize) or authorize() is not None:
                raise RetentionDenied()
        final = _time(self.clock())
        if final >= deadline:
            with self.ledger.transaction(lambda: final) as db:
                for resource, snapshot, grant in identities:
                    self._blocked(db, key, resource, snapshot, grant, final)
        if denied or not stamp <= final < deadline:
            raise RetentionDenied()

    @_safe
    def register(self, scope, job_id, *, policy):
        if type(policy) is not RetentionPolicy:
            raise RetentionDenied()
        key, job_id = _scope(scope), _id(job_id)
        # Read outside retention transaction. Concurrent retirement wins via the
        # permanent authority row; detached active copies can never re-register.
        job = self.store.get(scope, job_id)
        if job is None or job.state == 'cancelled':
            raise RetentionDenied()
        from ostag.briefs.service import ManualRequest
        payload = json.loads(job.payload_json)
        request = ManualRequest.from_dict(payload['request'])
        if (payload['kind'] != 'manual-brief-v1' or job.grant_id != request.grant_id
                or job.grant_revision != request.grant_revision):
            raise RetentionDenied()
        request.source_set(scope)
        identities = [_keys(scope, s) for s in request.sources]
        if not identities:
            raise RetentionDenied()
        box = self.store.outbox(scope, job_id)
        revision, delivery = (None, None) if box is None else (
            box.revision.revision_id, box.delivery_id)
        targets, stamp = self._targets(), _time(self.clock())
        binding = _digest([job.content_hash, job.grant_id, job.grant_revision,
                           job.destination.address])
        denied = False
        with self.ledger.transaction(self.clock) as db:
            stamp = db.execute('SELECT highwater FROM retention_meta').fetchone()[0]
            old = db.execute('SELECT * FROM retention_jobs WHERE scope=? AND job=?',
                             (key, job_id)).fetchone()
            if old is not None:
                _, reason = self._decision(db, key, job_id, stamp)
                pinned = {r['kind']: r['identity'] for r in db.execute(
                    'SELECT * FROM retention_targets WHERE scope=? AND job=?', (key, job_id))}
                if (old['binding'] != binding or old['expires'] != policy.expires_at
                        or pinned != targets or old['revision'] not in (None, revision)
                        or old['delivery'] not in (None, delivery)):
                    raise RetentionDenied()
                denied = reason != 'authorized'
                if not denied:
                    db.execute('UPDATE retention_jobs SET revision=?,delivery=? '
                               'WHERE scope=? AND job=?', (revision, delivery, key, job_id))
            else:
                for resource, snapshot, grant in identities:
                    denied |= self._blocked(db, key, resource, snapshot, grant, stamp) is not None
                denied |= stamp >= policy.expires_at
                if not denied:
                    db.execute('INSERT INTO retention_jobs VALUES(?,?,?,?,?,?,?)',
                        (key, job_id, binding, policy.expires_at, 'authorized', revision, delivery))
                    for resource, snapshot, grant in identities:
                        prior = db.execute('SELECT min(expires) FROM retention_sources '
                            'WHERE scope=? AND snapshot=?', (key, snapshot)).fetchone()[0]
                        expiry = policy.expires_at if prior is None else min(prior, policy.expires_at)
                        db.execute('INSERT OR IGNORE INTO retention_sources VALUES(?,?,?,?,?,0)',
                                   (key, snapshot, resource, grant, expiry))
                        db.execute('INSERT OR IGNORE INTO retention_links VALUES(?,?,?,?)',
                                   (key, job_id, snapshot, grant))
                    for kind, identity in targets.items():
                        db.execute('INSERT INTO retention_targets VALUES(?,?,?,?,0)',
                                   (key, job_id, kind, identity))
        if denied:
            raise RetentionDenied()
        result = self.status(scope, job_id)
        if result.decision != 'authorized':
            raise RetentionDenied()
        return result

    def _check(self, scope, job, revision):
        key, stamp = _scope(scope), _time(self.clock())
        targets = self._targets()
        with self.ledger.transaction(self.clock) as db:
            stamp = db.execute('SELECT highwater FROM retention_meta').fetchone()[0]
            row, reason = self._decision(db, key, _id(job), stamp)
            pinned = {r['kind']: r['identity'] for r in db.execute(
                'SELECT * FROM retention_targets WHERE scope=? AND job=?', (key, job))}
            deadline = self._deadline(db, key, row)
            denied = (reason != 'authorized' or targets != pinned
                      or (revision is not None and revision != row['revision']))
        final = _time(self.clock())
        if final >= deadline:
            # Even an expiry observed at the final post-commit clock is durable;
            # a later clock rollback cannot revive that pinned snapshot.
            with self.ledger.transaction(lambda: final) as db:
                self._decision(db, key, job, final)
        if denied or not stamp <= final < deadline:
            raise RetentionDenied()
        return final, deadline

    def _finish(self, scope, job, stamp, deadline):
        """No successful-path I/O after the original authority mirror."""
        final = _time(self.clock())
        if final >= deadline:
            # Denial may do I/O: persist snapshot expiry just as _check does.
            # No successful release or effect can follow this branch.
            with self.ledger.transaction(lambda: final) as db:
                self._decision(db, _scope(scope), job, final)
            raise RetentionDenied()
        if not stamp <= final < deadline or (_scope(scope), job) in self._cancelled:
            raise RetentionDenied()

    @_safe
    def status(self, scope, job):
        key, stamp = _scope(scope), _time(self.clock())
        with self.ledger.transaction(self.clock) as db:
            stamp = db.execute('SELECT highwater FROM retention_meta').fetchone()[0]
            job_row, reason = self._decision(db, key, _id(job), stamp)
            deadline = self._deadline(db, key, job_row)
            rows = db.execute('SELECT * FROM retention_targets WHERE scope=? AND job=?',
                              (key, job)).fetchall()
        # A receipt does not certify a swapped/omitted target. Revalidate identity
        # even after completion; missing files never silently become success.
        def available(row):
            try:
                target = self.targets[row['kind']]
                if _identity(target) != row['identity']:
                    return False
                # Outside retention transaction: never reverse the service lock order.
                with sqlite3.connect(target.path.resolve().as_uri() + '?mode=rw', uri=True,
                                     timeout=0.05) as db:
                    db.row_factory = sqlite3.Row
                    db.execute('BEGIN')
                    (target._inspect if row['kind'] == 'primary' else target._verify)(db)
                    if reason != 'authorized':
                        target._verify_retired(db, scope, job)
                    if _identity(target) != row['identity']:
                        return False
                return True
            except Exception:
                return False
        pending = sum(not r['done'] or not available(r) for r in rows)
        final = _time(self.clock())
        if final < stamp:
            raise RetentionDenied()
        if reason == 'authorized' and final >= deadline:
            with self.ledger.transaction(lambda: final) as db:
                _, reason = self._decision(db, key, job, final)
        cleanup = ('not_requested' if reason == 'authorized' else
                   'complete' if pending == 0 else 'pending' if pending == len(rows) else 'partial')
        return RetentionStatus(reason, cleanup, pending, content_available=reason == 'authorized')

    def _cancel(self, scope, job):
        self._signal(scope, job)
        try:
            with self.ledger.transaction(self.clock) as db:
                row = db.execute("SELECT identity FROM retention_targets WHERE scope=? AND job=? "
                                 "AND kind='primary'", (_scope(scope), job)).fetchone()
            if row is None or _identity(self.store) != row['identity']:
                return
            self.store.retention_cancel(scope, job)
        except Exception:
            pass  # Persisted tombstone still denies; cleanup retries primary fence.

    @_safe
    def revoke_job(self, scope, job):
        key, stamp = _scope(scope), _time(self.clock())
        with self.ledger.transaction(self.clock) as db:
            stamp = db.execute('SELECT highwater FROM retention_meta').fetchone()[0]
            self._decision(db, key, _id(job), stamp)
            db.execute("UPDATE retention_jobs SET decision='revoked' WHERE scope=? AND job=?",
                       (key, job))
        self._cancel(scope, job)
        return self.status(scope, job)

    def _admin(self, actor, scope, operation):
        _scope(actor)
        _scope(scope)
        if not callable(self.maintenance) or self.maintenance(actor, scope, operation) is not None:
            raise RetentionDenied()

    @_safe
    def revoke_source(self, actor, scope, resource_id):
        return self._revoke(actor, scope, 'resource', resource_id)

    @_safe
    def revoke_grant(self, actor, scope, grant_id):
        return self._revoke(actor, scope, 'grant', grant_id)

    def _revoke(self, actor, scope, kind, identifier):
        self._admin(actor, scope, 'revoke_' + kind)
        key = _scope(scope)
        identity = _digest([key, _id(identifier)])
        with self.ledger.transaction(self.clock) as db:
            db.execute('INSERT OR IGNORE INTO retention_revokes VALUES(?,?,?)', (key, kind, identity))
            column = 'resource' if kind == 'resource' else 'grant_key'
            jobs = [r[0] for r in db.execute('SELECT DISTINCT l.job FROM retention_links l '
                'JOIN retention_sources s ON l.scope=s.scope AND l.snapshot=s.snapshot '
                'AND l.grant_key=s.grant_key WHERE s.scope=? AND s.' + column + '=?',
                (key, identity))]
            for job in jobs:
                db.execute("UPDATE retention_jobs SET decision='revoked' WHERE scope=? AND job=?",
                           (key, job))
        for job in jobs:
            self._cancel(scope, job)
        return len(jobs)

    @_safe
    def expire(self, scope, job):
        result = self.status(scope, job)
        if result.decision == 'authorized':
            raise RetentionDenied()
        self._cancel(scope, job)
        return result

    @_safe
    def cleanup_as(self, actor, scope, job):
        self._admin(actor, scope, 'cleanup')
        return self.cleanup(scope, job)

    def _receipt(self, scope, job, kind, identity):
        # Separate durable progress boundary; a lost commit ACK is safe to replay.
        self._progress(scope, job, kind, identity, 1)

    def _progress(self, scope, job, kind, identity, done):
        with self.ledger.transaction(self.clock) as db:
            key = _scope(scope)
            db.execute('UPDATE retention_targets SET done=? WHERE scope=? AND job=? '
                       'AND kind=? AND identity=?', (done, key, job, kind, identity))
            row = db.execute('SELECT identity,done FROM retention_targets '
                             'WHERE scope=? AND job=? AND kind=?', (key, job, kind)).fetchone()
            if row is None or tuple(row) != (identity, done):
                raise RetentionDenied()

    @_safe
    def cleanup(self, scope, job):
        op = None if self.lifecycle is None else self.lifecycle.begin('cleanup')
        try:
            return self._cleanup(scope, job)
        finally:
            if op is not None:
                op.finish()

    def _cleanup(self, scope, job):
        key = _scope(scope)
        if self.status(scope, job).decision == 'authorized':
            raise RetentionDenied()
        self._signal(scope, job)
        with self.ledger.transaction(self.clock) as db:
            rows = db.execute('SELECT * FROM retention_targets WHERE scope=? AND job=? '
                              "ORDER BY CASE kind WHEN 'primary' THEN 0 ELSE 1 END,kind",
                              (key, job)).fetchall()
        for row in rows:
            # A failed reset cannot fall back to old success receipts. Propagate
            # unavailable/ignored progress rather than fabricate completion.
            self._progress(scope, job, row['kind'], row['identity'], 0)
            try:
                target = self.targets.get(row['kind'])
                if target is None or _identity(target) != row['identity']:
                    break
                # Always verify/retire again, even if a prior receipt exists.
                # No new active content can pass a composed gate after tombstone.
                target.retire_local(scope, job)
                if _identity(target) != row['identity']:
                    break
                self._receipt(scope, job, row['kind'], row['identity'])
            except Exception:
                break
        return self.status(scope, job)
