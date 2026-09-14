"""Opt-in read-only private previews with a separate owned SQLite capability ledger.

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
from ostag.briefs.store import RequesterContext
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

    def initialize(self):
        """Create only an empty owned database, or verify its exact version/capacity."""
        failed = False
        try:
            with self._connect(create=True) as db:
                db.execute('BEGIN IMMEDIATE')
                app = db.execute('PRAGMA application_id').fetchone()[0]
                tables = {r[0] for r in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
                if app == 0 and not tables:
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
        if (db.execute('PRAGMA application_id').fetchone()[0] != _APP_ID
                or db.execute('PRAGMA user_version').fetchone()[0] != 1
                or db.execute('SELECT capacity FROM callback_meta WHERE id=1').fetchone()
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

    def issue(self, key, *, ttl, clock, check):
        token = 'br_' + secrets.token_urlsafe(24)
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            now = self._time(db, clock)
            db.execute('DELETE FROM brief_callbacks WHERE expires<=?', (now,))
            if db.execute('SELECT count(*) FROM brief_callbacks').fetchone()[0] >= self.capacity:
                db.commit()
                raise _failure()
            check()
            now = self._time(db, clock)
            db.execute('INSERT INTO brief_callbacks VALUES(?,?,?,?,0)',
                       (_hash(token), key, now, now + int(ttl * 1000000)))
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

    def claim(self, token, cap, *, clock, check):
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
            db.commit()


class BriefCallbackController:
    """Explicit composition only; all public failures discard exception contexts."""
    def __init__(self, *, ledger: CallbackLedger, namespace: str, resolve_binding,
                 load, authorize, clock=time.time):
        if (type(ledger) is not CallbackLedger or type(namespace) is not str
                or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', namespace)
                or not all(callable(c) for c in (resolve_binding, load, authorize, clock))):
            raise _failure()
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
        # Load and binding resolution may have waited. ALL-source authorization
        # must be the final infrastructure call immediately before release.
        if self._authorize(p) is not None:
            raise _failure()

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
                                     clock=self._now, check=lambda: self._current(preview))
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
            self.ledger.claim(callback_id, cap, clock=self._now, check=lambda: self._current(p))
            # Durable claim precedes ANY private effect. Replays cannot render.
            card = render_private(p, page=key['page'])
            self._current(p)
            now = _stamp(self._now())
            if not cap.issued <= now < cap.expires:
                raise _failure()
            return card
        return self._safe(operation)
