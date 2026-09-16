"""Opt-in read-only private previews with a separate owned SQLite capability ledger.

Explicit retention= adds the typed A10 gate from retention.py. Its bounded local
SQLite authority read is the documented exception to the no-I/O policy-callback
contract; it never acquires this ledger from inside a retention transaction.
Original current authorizers and final clocks remain required. Retirement is a
sanctioned opt-in exception to append-only content, never to send/event identity.


No constructor initializes a DB; call CallbackLedger.initialize explicitly. This
is NOT MonitorStore callback storage and changes no brief/U01 schema. Capabilities
bind installation/tenant/app/generation, namespace/platform/actor, original private
chat, original job/revision, source purpose, preview/refresh, page and message ID.
Expiry is exclusive and <=300s. Claim commits before rendering; crashes stay spent.

Composition supplies bounded local synchronous load, binding and authorization
mirrors. They must be nonblocking, non-reentrant, perform no network IO, and never
access this callback ledger. authorize(snapshot) must return None ONLY after
checking EVERY frozen source and grant, CURRENT original revision, private chat
ownership and installed binding. Hashes/IDs alone confer no authority. Loading a
trusted stored revision is separate from this current authorization check.
SQLite has a 50ms lock wait; OS SQLite/fsync and trusted synchronous infrastructure
cannot be preempted. Contention fails closed. Per-controller reentry fails closed.
The final source/grant/mapping check follows all DB waits and rendering, directly
before private release. No initial card sends, cancel effects or server defaults.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import dataclass, field
import hashlib
import json
import math
from pathlib import Path
import re
import secrets
import sqlite3
from threading import Lock
import time

from ostag.briefs.delivery import FeishuBinding
from ostag.briefs.rendering import PrivatePreview, render_private
from ostag.briefs.store import _retirement_method, RequesterContext
from ostag.core.identity import Principal
from ostag.work_monitor.identity import Destination

_APP_ID = 0x42524342
_TOKEN = re.compile(r'br_[A-Za-z0-9_-]{22,64}\Z')
_MESSAGE = re.compile(r'[A-Za-z0-9_-]{8,256}\Z')


class CallbackError(RuntimeError):
    """Fixed diagnostic; never carries provider, policy, DB or private contexts."""


def _failure():
    return CallbackError('brief preview unavailable')


def _stamp(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value < 10**11:
        raise _failure()
    return int(value * 1000000)


def _hash(token):
    if type(token) is not str or not _TOKEN.fullmatch(token):
        raise _failure()
    return hashlib.sha256(token.encode('ascii')).hexdigest()


@dataclass(frozen=True)
class _Capability:
    key: str = field(repr=False)
    issued: int
    expires: int


class CallbackLedger:
    def __init__(self, path, *, capacity=1000):
        if type(capacity) is not int or not 1 <= capacity <= 10000:
            raise _failure()
        self.path = Path(path)
        self.capacity = capacity

    @_retirement_method
    def retire_local(self, scope, job_id):
        """Erase capability bindings; retain an inert scoped job tombstone."""
        from ostag.briefs.retention import owned_retirement
        with owned_retirement(self, compact=True) as db:
            for row in db.execute('SELECT * FROM brief_callbacks').fetchall():
                key = json.loads(row['binding'])
                if (key.get('namespace') == scope.namespace
                        and key.get('platform') == scope.principal.platform
                        and key.get('actor') == scope.principal.actor_id and key.get('job') == job_id):
                    safe = {name: key[name] for name in ('namespace', 'platform', 'actor', 'job')}
                    safe['retired'] = True
                    db.execute('UPDATE brief_callbacks SET binding=?,spent=1 WHERE digest=?',
                               (json.dumps(safe, sort_keys=True, separators=(',', ':')), row['digest']))
            self._verify_retired(db, scope, job_id)

    def _verify_retired(self, db, scope, job_id):
        """Spent capabilities still require complete binding redaction."""
        for row in db.execute('SELECT * FROM brief_callbacks').fetchall():
            key = json.loads(row['binding'])
            if (key.get('namespace') == scope.namespace
                    and key.get('platform') == scope.principal.platform
                    and key.get('actor') == scope.principal.actor_id and key.get('job') == job_id):
                safe = {name: key[name] for name in ('namespace', 'platform', 'actor', 'job')}
                safe['retired'] = True
                if (row['binding'] != json.dumps(safe, sort_keys=True, separators=(',', ':'))
                        or row['spent'] != 1):
                    raise _failure()

    def initialize(self):
        """Create only an empty owned database, or verify its exact version/capacity."""
        failed = False
        try:
            with self._connect(create=True) as db:
                db.execute('BEGIN IMMEDIATE')
                app = db.execute('PRAGMA application_id').fetchone()[0]
                tables = {r[0] for r in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
                if (app == 0 and not tables
                        and not db.execute('SELECT name FROM sqlite_master').fetchall()
                        and db.execute('PRAGMA user_version').fetchone()[0] == 0
                        and db.execute('PRAGMA journal_mode').fetchone()[0] == 'delete'):
                    db.execute(f'PRAGMA application_id={_APP_ID}')
                    db.execute('PRAGMA user_version=1')
                    db.execute('CREATE TABLE callback_meta '
                               '(id INTEGER PRIMARY KEY CHECK(id=1), highwater INTEGER NOT NULL, '
                               'capacity INTEGER NOT NULL)')
                    db.execute('INSERT INTO callback_meta VALUES(1,0,?)', (self.capacity,))
                    db.execute('CREATE TABLE brief_callbacks '
                               '(digest TEXT PRIMARY KEY, binding TEXT NOT NULL, '
                               'issued INTEGER NOT NULL, expires INTEGER NOT NULL, '
                               'spent INTEGER NOT NULL CHECK(spent IN(0,1)))')
                self._verify(db)
                db.commit()
        except (Exception, asyncio.CancelledError):
            failed = True
        if failed:
            raise _failure()

    @contextmanager
    def _connect(self, *, create=False):
        if self.path.is_symlink():
            raise _failure()
        # mode=rw prevents an accidental operational call creating a new ledger.
        uri = self.path.resolve().as_uri() + ('?mode=rwc' if create else '?mode=rw')
        db = sqlite3.connect(uri, uri=True, timeout=0.05, isolation_level=None)
        try:
            db.execute('PRAGMA synchronous=FULL')
            if not create:
                self._verify(db)
            yield db
        finally:
            db.close()

    def _verify(self, db):
        schema = (
            'CREATE TABLE callback_meta (id INTEGER PRIMARY KEY CHECK(id=1), '
            'highwater INTEGER NOT NULL, capacity INTEGER NOT NULL)',
            'CREATE TABLE brief_callbacks (digest TEXT PRIMARY KEY, binding TEXT NOT NULL, '
            'issued INTEGER NOT NULL, expires INTEGER NOT NULL, '
            'spent INTEGER NOT NULL CHECK(spent IN(0,1)))',
        )
        actual = [tuple(r) for r in db.execute(
            "SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_autoindex_%' "
            'ORDER BY name')]
        expected = sorted([('table', s.split()[2], s) for s in schema], key=lambda r: r[1])
        if actual != expected or db.execute('PRAGMA journal_mode').fetchone()[0] != 'delete':
            raise _failure()
        if (db.execute('PRAGMA application_id').fetchone()[0] != _APP_ID
                or db.execute('PRAGMA user_version').fetchone()[0] != 1
                or tuple(db.execute('SELECT capacity FROM callback_meta WHERE id=1').fetchone() or ())
                != (self.capacity,)):
            raise _failure()

    @staticmethod
    def _time(db, clock):
        now = _stamp(clock())
        prior = db.execute('SELECT highwater FROM callback_meta WHERE id=1').fetchone()[0]
        if now < prior:
            raise _failure()
        db.execute('UPDATE callback_meta SET highwater=? WHERE id=1', (now,))
        return now

    def issue(self, key, *, ttl, clock, check, final_check=None):
        token = 'br_' + secrets.token_urlsafe(24)
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            now = self._time(db, clock)
            retired = db.execute('SELECT binding FROM brief_callbacks WHERE binding LIKE ?',
                                 ('%"retired":true%',)).fetchall()
            if retired:
                try:
                    frozen = json.loads(key)
                except (TypeError, ValueError):
                    frozen = None
                for row in retired:
                    old = json.loads(row[0])
                    if (type(frozen) is dict and old.get('retired') and all(
                            old.get(k) == frozen.get(k)
                            for k in ('namespace', 'platform', 'actor', 'job'))):
                        raise _failure()
            db.execute("DELETE FROM brief_callbacks WHERE expires<=? AND binding NOT LIKE ?",
                       (now, '%"retired":true%'))
            if db.execute('SELECT count(*) FROM brief_callbacks').fetchone()[0] >= self.capacity:
                db.commit()
                raise _failure()
            check()
            now = self._time(db, clock)
            db.execute('INSERT INTO brief_callbacks VALUES(?,?,?,?,0)',
                       (_hash(token), key, now, now + int(ttl * 1000000)))
            if final_check is not None:
                final_check()
                if not now <= _stamp(clock()) < now + int(ttl * 1000000):
                    raise _failure()
            db.commit()
        return token

    def lookup(self, token, *, clock):
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            now = self._time(db, clock)
            row = db.execute('SELECT binding,issued,expires,spent FROM brief_callbacks '
                             'WHERE digest=?', (_hash(token),)).fetchone()
            db.commit()  # Persist the observed time even for an expired attempt.
        if row is None or row[3] or not row[1] <= now < row[2]:
            raise _failure()
        return _Capability(*row[:3])

    def claim(self, token, cap, *, clock, check, final_check=None):
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            now = self._time(db, clock)
            row = db.execute('SELECT binding,issued,expires,spent FROM brief_callbacks '
                             'WHERE digest=?', (_hash(token),)).fetchone()
            if row != (cap.key, cap.issued, cap.expires, 0) or not cap.issued <= now < cap.expires:
                db.commit()
                raise _failure()
            # Authorization is AFTER BEGIN's lock wait, before consumption.
            check()
            now = self._time(db, clock)
            if now >= cap.expires:
                db.commit()
                raise _failure()
            db.execute('UPDATE brief_callbacks SET spent=1 WHERE digest=? AND spent=0',
                       (_hash(token),))
            if final_check is not None:
                final_check()
                if not now <= _stamp(clock()) < cap.expires:
                    raise _failure()
            db.commit()


class BriefCallbackController:
    """Explicit composition only; all public failures discard exception contexts."""
    def __init__(self, *, ledger: CallbackLedger, namespace: str, resolve_binding,
                 load, authorize, clock=time.time, retention=None):
        if (type(ledger) is not CallbackLedger or type(namespace) is not str
                or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', namespace)
                or not all(callable(c) for c in (resolve_binding, load, authorize, clock))):
            raise _failure()
        from ostag.briefs.retention import configure_guard
        self._retention_enabled = configure_guard(retention, ledger)
        self.retention = retention
        self.ledger, self.namespace = ledger, namespace
        self._binding, self._load, self._authorize, self._clock = resolve_binding, load, authorize, clock
        self._lock = Lock()
        self._last_time = 0

    def _now(self):
        value = self._clock()
        stamp = _stamp(value)
        if stamp < self._last_time:
            raise _failure()
        self._last_time = stamp
        return value

    def _safe(self, operation):
        result, failed = None, False
        if not self._lock.acquire(blocking=False):
            raise _failure()
        try:
            result = operation()
        except (Exception, asyncio.CancelledError):
            failed = True
        finally:
            self._lock.release()
        if failed:
            raise _failure()
        return result

    def _current(self, p):
        if (type(p) is not PrivatePreview or p.scope.namespace != self.namespace
                or self._load(p.scope, p.job_id) != p):
            raise _failure()
        binding = self._binding()
        if type(binding) is not FeishuBinding or binding != p.binding:
            raise _failure()
        # Retention I/O precedes the final nonblocking ALL-source mirror.
        from ostag.briefs.retention import check_guard
        check_guard(self, p.scope, p.job_id, p.revision_id,
                    authorize=lambda: self._authorize(p))

    @staticmethod
    def _key(p, action, page, message_id):
        if (action not in ('preview', 'refresh') or type(page) is not int or not 0 <= page < 65536
                or type(message_id) is not str or not _MESSAGE.fullmatch(message_id)):
            raise _failure()
        return json.dumps(dict(namespace=p.scope.namespace, platform=p.scope.principal.platform,
            actor=p.scope.principal.actor_id, destination=p.destination.address,
            binding=p.binding.to_dict(), job=p.job_id, revision=p.revision_id,
            purpose=p.sources.purpose, action=action, page=page, message=message_id),
            sort_keys=True, separators=(',', ':'))

    def prepare_preview(self, scope, job_id, revision_id, *, page=0):
        """Local authorized preview only; no provider sends or implicit tokens."""
        def operation():
            p = self._load(scope, job_id)
            if (type(p) is not PrivatePreview or p.scope != scope or p.job_id != job_id
                    or p.revision_id != revision_id):
                raise _failure()
            self._current(p)
            card = render_private(p, page=page)
            self._current(p)
            return card
        return self._safe(operation)

    def issue(self, preview, *, message_id, action='preview', page=0, ttl=300):
        """Bind a preview/refresh to an explicitly known existing private message."""
        def operation():
            if type(ttl) not in (int, float) or not math.isfinite(ttl) or not 0 < ttl <= 300:
                raise _failure()
            self._current(preview)
            render_private(preview, page=page)  # Validate page before consuming capacity.
            return self.ledger.issue(self._key(preview, action, page, message_id), ttl=ttl,
                                     clock=self._now, check=lambda: self._current(preview),
                                     final_check=(lambda: self._current(preview))
                                     if self._retention_enabled else None)
        return self._safe(operation)

    def handle_callback(self, callback_id, *, principal: Principal, destination: Destination,
                        tenant_id: str, app_id: str, message_id: str):
        """Identity/context MUST come from FeishuCardSecurity, never action values."""
        def operation():
            cap = self.ledger.lookup(callback_id, clock=self._now)
            key = json.loads(cap.key)
            if (type(principal) is not Principal or type(destination) is not Destination
                    or principal.platform != 'feishu' or destination.platform != 'feishu'
                    or destination.chat_type != 'private' or key['namespace'] != self.namespace
                    or principal.actor_id != key['actor'] or key['platform'] != principal.platform
                    or destination.destination_id != key['destination']
                    or tenant_id != key['binding']['tenant_id'] or app_id != key['binding']['app_id']
                    or message_id != key['message']):
                raise _failure()
            scope = RequesterContext(self.namespace, principal)
            p = self._load(scope, key['job'])
            if (type(p) is not PrivatePreview or p.scope != scope
                    or self._key(p, key['action'], key['page'], message_id) != cap.key):
                raise _failure()
            self._current(p)
            self.ledger.claim(callback_id, cap, clock=self._now, check=lambda: self._current(p),
                              final_check=(lambda: self._current(p))
                              if self._retention_enabled else None)
            # Durable claim precedes ANY private effect. Replays cannot render.
            card = render_private(p, page=key['page'])
            self._current(p)
            now = _stamp(self._now())
            if not cap.issued <= now < cap.expires:
                raise _failure()
            return card
        return self._safe(operation)
