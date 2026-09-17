"""card-v1: owned permanent send tombstones and original-message navigation.

Explicit retention= adds the typed A10 gate from retention.py. Its bounded local
SQLite authority read is the documented exception to the no-I/O policy-callback
contract; it never acquires this ledger from inside a retention transaction.
Original current authorizers and final clocks remain required. Retirement is a
sanctioned opt-in exception to append-only content, never to send/event identity.


Explicit initialize and composition only. load(scope, job) MUST read the original
stored revision (PrivatePreview.from_outbox); authorize(preview) MUST check every
source/grant snapshot, original revision/outbox cancellation, private mapping and
installed generation. These and resolve_binding are bounded, synchronous local,
non-reentrant mirrors, with no network or access to this ledger. Fingerprints are
integrity checks, never authorization. No BriefStore/U01 schema or bytes change.

One loop-affine service owns capacity and stop; abandoned awaitables keep their
slot until they exit. SQLite lock retries are asynchronous for delivery, bounded
50ms for callbacks; synchronous SQLite/fsync and trusted local code cannot be
preempted. No scheduler, automatic polling, resend, search or recovery by callback.

Only a correlated CardCreateAck from the independent transport activates inert
initial tokens, in the SAME transaction as the owned send receipt. This attests
create acknowledgement, never stored full-content verification or human receipt.
Navigation consumes an epoch and freezes its replacement atomically. Lost HTTP
callback responses may leave the old displayed card unresponsive: no replay or
hidden resend. Local commit does not confirm a client update or its ordering.

Rows are never deleted: expiration cannot reopen a send. At most three current
navigation rows per permanent send row; pruning old epochs cannot revive tokens.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import dataclass, field
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import sqlite3
from threading import Event, Lock
import time

from ostag.briefs.delivery import FeishuBinding, _TransportEgressGate
from ostag.briefs.rendering import PrivatePreview, render_navigation
from ostag.briefs.store import _retirement_method, PrivateDestination, RequesterContext
from ostag.core.identity import Principal
from ostag.work_monitor.identity import Destination

_APP = 0x42435631
MAX_PREVIEW = 8 * 1048576
MAX_EPOCH = 1024
_ID = re.compile(r'[A-Za-z0-9_-]{1,256}\Z')
_TOKEN = re.compile(r'bc_[A-Za-z0-9_-]{32}\Z')
_MESSAGE = re.compile(r'[A-Za-z0-9_-]{8,256}\Z')
_SCHEMA = (
    'CREATE TABLE card_meta (id INTEGER PRIMARY KEY CHECK(id=1), '
    "highwater INTEGER NOT NULL CHECK(typeof(highwater)='integer' AND highwater>=0), "
    "capacity INTEGER NOT NULL CHECK(typeof(capacity)='integer' AND capacity BETWEEN 1 AND 10000))",
    "CREATE TABLE cards (logical TEXT PRIMARY KEY, delivery_id TEXT NOT NULL UNIQUE, "
    "scope TEXT NOT NULL, identity_json TEXT NOT NULL CHECK(length(identity_json)<=8192), "
    f"preview_json TEXT NOT NULL CHECK(length(preview_json)<={MAX_PREVIEW}), "
    "card_json TEXT NOT NULL CHECK(length(card_json)<=16384), "
    "wire_body BLOB NOT NULL CHECK(length(wire_body)<=32768), digest TEXT NOT NULL, "
    "issued INTEGER NOT NULL CHECK(typeof(issued)='integer' AND issued>=0), "
    "expires INTEGER NOT NULL CHECK(typeof(expires)='integer' AND expires<100000000000000000), "
    "state TEXT NOT NULL CHECK(state IN('pending','started','unknown','create_acknowledged')), "
    "cancelled INTEGER NOT NULL CHECK(typeof(cancelled)='integer' AND cancelled IN(0,1)), "
    "owner TEXT CHECK(length(owner)=48), token TEXT CHECK(length(token)=48), "
    "uncertainty TEXT NOT NULL CHECK(length(uncertainty)<=1024), "
    "first_failure TEXT CHECK(length(first_failure)<=1024), message_id TEXT CHECK(length(message_id)<=256), "
    "receipt TEXT CHECK(length(receipt)<=4096), epoch INTEGER NOT NULL CHECK(typeof(epoch)='integer' AND epoch BETWEEN 0 AND 1024), "
    "page INTEGER NOT NULL CHECK(typeof(page)='integer' AND page BETWEEN 0 AND 65535), "
    "response_card TEXT CHECK(length(response_card)<=16384), "
    "CHECK(expires>issued AND expires-issued<=300000000))",
    'CREATE TABLE card_tokens (digest TEXT PRIMARY KEY, delivery_id TEXT NOT NULL, '
    "epoch INTEGER NOT NULL CHECK(typeof(epoch)='integer' AND epoch BETWEEN 0 AND 1024), "
    "action TEXT NOT NULL CHECK(action IN('Next','Previous','Refresh')), "
    "page INTEGER NOT NULL CHECK(typeof(page)='integer' AND page BETWEEN 0 AND 65535))",
)
_WINDOW_SCHEMA = (
    'CREATE TABLE card_windows (delivery_id TEXT PRIMARY KEY, '
    "issued INTEGER NOT NULL CHECK(typeof(issued)='integer' AND issued>=0), "
    "expires INTEGER NOT NULL CHECK(typeof(expires)='integer' AND expires<100000000000000000), "
    'CHECK(expires>issued AND expires-issued<=300000000))',
)


class CardDeliveryError(RuntimeError):
    """Fixed diagnostic without private exception context."""


class _Busy(Exception):
    pass


def _fail():
    return CardDeliveryError('brief card unavailable')


def _json(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':'),
                      allow_nan=False)


def _digest(raw):
    return hashlib.sha256(raw).hexdigest()


def _stamp(value):
    if type(value) not in (int, float) or not 0 <= value < 10**11 or not math.isfinite(value):
        raise _fail()
    return int(value * 1000000)


def _id(value):
    if type(value) is not str or not _ID.fullmatch(value):
        raise _fail()
    return value


def _scope(scope):
    if type(scope) is not RequesterContext or scope.principal.platform != 'feishu':
        raise _fail()
    return _json([_id(scope.namespace), scope.principal.platform, _id(scope.principal.actor_id)])


def _preview(p):
    if type(p) is not PrivatePreview:
        raise _fail()
    _scope(p.scope)
    for item in (p.job_id, p.revision_id, p.outbox_id):
        _id(item)
    for item in (p.binding.tenant_id, p.binding.installation_id, p.binding.app_id, p.binding.bot_id):
        _id(item)
    value = _json(dict(sources=p.sources.to_dict(), draft=p.draft.to_dict(), state=p.state))
    if len(value.encode()) > MAX_PREVIEW:
        raise _fail()
    return value


def _identity(p, preview_json):
    return _json(dict(version='card-v1', scope=json.loads(_scope(p.scope)), job=p.job_id,
        revision=p.revision_id, outbox=p.outbox_id, destination=p.destination.address,
        binding=p.binding.to_dict(), purpose=p.sources.purpose,
        preview_digest=_digest(preview_json.encode())))


def _logical(scope, job, revision):
    return _digest(_json(['card-v1-original', _scope(scope), _id(job), _id(revision)]).encode())


def _controls(p, page):
    meta = render_navigation(p, page=page)
    targets = {'Refresh': page}
    if page:
        targets['Previous'] = page - 1
    if page + 1 < meta.pages:
        targets['Next'] = page + 1
    controls = {label: 'bc_' + secrets.token_urlsafe(24) for label in targets}
    card = _json(render_navigation(p, page=page, controls=controls).card)
    rows = [(_digest(token.encode()), action, targets[action]) for action, token in controls.items()]
    return card, rows


@dataclass(frozen=True)
class CardStatus:
    delivery_id: str
    state: str
    cancelled: bool
    content_verified: bool = False
    human_received: bool = False
    epoch: int = 0


@dataclass(frozen=True)
class CardIntent:
    """Independent exact frozen create request; never a U01 DeliveryIntent."""
    scope: RequesterContext = field(repr=False)
    delivery_id: str
    identity_json: str = field(repr=False)
    card_json: str = field(repr=False)
    wire_body: bytes = field(repr=False)
    digest: str = field(repr=False)
    issued: int
    expires: int

    def __post_init__(self):
        valid = False
        try:
            key = json.loads(self.identity_json)
            valid = (type(self.identity_json) is str and len(self.identity_json) <= 8192
                and _json(key) == self.identity_json and type(key) is dict
                and set(key) == {'version', 'scope', 'job', 'revision', 'outbox', 'destination',
                                 'binding', 'purpose', 'preview_digest'}
                and key['version'] == 'card-v1' and key['scope'] == json.loads(_scope(self.scope))
                and type(self.card_json) is str and 1 <= len(self.card_json.encode()) <= 16384
                and _json(json.loads(self.card_json)) == self.card_json
                and type(self.wire_body) is bytes and 1 <= len(self.wire_body) <= 32768
                and self.wire_body == _json(dict(receive_id=key['destination'],
                    msg_type='interactive', content=self.card_json)).encode()
                and self.digest == _digest(self.wire_body)
                and self.delivery_id == 'bcv1_' + _digest(self.identity_json.encode())
                and type(self.issued) is int and type(self.expires) is int
                and 0 <= self.issued < self.expires < 10**17
                and self.expires - self.issued <= 300000000
                and type(key['preview_digest']) is str
                and re.fullmatch(r'[0-9a-f]{64}', key['preview_digest']) is not None)
            for name in ('job', 'revision', 'outbox'):
                _id(key[name])
            binding = FeishuBinding(**key['binding'])
            for value in (binding.tenant_id, binding.installation_id, binding.app_id, binding.bot_id):
                _id(value)
            if (type(key['destination']) is not str
                    or not re.fullmatch(r'oc_[A-Za-z0-9_-]{1,250}', key['destination'])):
                valid = False
        except Exception:
            valid = False
        if not valid:
            raise _fail()

    @property
    def binding(self):
        return FeishuBinding(**json.loads(self.identity_json)['binding'])

    @property
    def destination(self):
        return PrivateDestination(self.scope.principal, json.loads(self.identity_json)['destination'])


@dataclass(frozen=True)
class CardAttempt:
    intent: CardIntent = field(repr=False)
    owner: str = field(repr=False)
    token: str = field(repr=False)

    def __post_init__(self):
        if (type(self.intent) is not CardIntent or any(type(v) is not str
                or not re.fullmatch(r'[0-9a-f]{48}', v) for v in (self.owner, self.token))):
            raise _fail()


@dataclass(frozen=True)
class CardCreateAck:
    """Trusted direct transport evidence, correlated to the exact owned request."""
    owner: str = field(repr=False)
    token: str = field(repr=False)
    request_digest: str = field(repr=False)
    message_id: str = field(repr=False)
    binding: FeishuBinding = field(repr=False)
    destination: str = field(repr=False)
    sender_id: str = field(repr=False)
    sender_id_type: str = field(repr=False)
    credential_digest: str = field(default='', repr=False)


class CardLedger:
    def __init__(self, path, *, capacity=1000):
        if type(capacity) is not int or not 1 <= capacity <= 10000:
            raise _fail()
        self.path, self.capacity = Path(path), capacity

    @_retirement_method
    def retire_local(self, scope, job_id):
        """Keep original logical/send custody; retire every body and live token."""
        from ostag.briefs.retention import _RETIRED, owned_retirement
        with owned_retirement(self, compact=True) as db:
            rows = db.execute('SELECT * FROM cards WHERE scope=?', (_scope(scope),)).fetchall()
            for row in rows:
                if json.loads(row['identity_json'])['job'] != job_id:
                    continue
                db.execute('UPDATE cards SET cancelled=1,preview_json=?,card_json=?,wire_body=?, '
                           'response_card=NULL WHERE delivery_id=?',
                           (_RETIRED, _RETIRED, b'', row['delivery_id']))
                db.execute('DELETE FROM card_tokens WHERE delivery_id=?', (row['delivery_id'],))
            self._verify_retired(db, scope, job_id)

    def _verify_retired(self, db, scope, job_id):
        """Verify all bodies/tokens; identity and numeric send facts stay inert."""
        from ostag.briefs.retention import _RETIRED
        for row in db.execute('SELECT * FROM cards WHERE scope=?', (_scope(scope),)).fetchall():
            if json.loads(row['identity_json'])['job'] != job_id:
                continue
            if (row['preview_json'] != _RETIRED or row['card_json'] != _RETIRED
                    or row['wire_body'] != b'' or row['response_card'] is not None
                    or row['cancelled'] != 1 or db.execute(
                        'SELECT 1 FROM card_tokens WHERE delivery_id=?',
                        (row['delivery_id'],)).fetchone()):
                raise _fail()

    @contextmanager
    def _connection(self, *, create=False, wait=0):
        if self.path.is_symlink():
            raise _fail()
        if create:
            # O_EXCL sets owner-only permissions before any private SQLite data;
            # reopening never changes an existing file's permissions.
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                pass
            else:
                os.close(fd)
        db = sqlite3.connect(self.path.resolve().as_uri() + '?mode=rw',
                             uri=True, timeout=wait, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            yield db
        except sqlite3.OperationalError as exc:
            if getattr(exc, 'sqlite_errorcode', 0) & 255 in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                raise _Busy() from None
            raise
        finally:
            db.close()

    def _verify(self, db, *, version=2):
        actual = [tuple(r) for r in db.execute(
            "SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_autoindex_%' "
            'ORDER BY name')]
        schema = _SCHEMA if version == 1 else _SCHEMA + _WINDOW_SCHEMA
        expected = sorted([('table', sql.split()[2], sql) for sql in schema], key=lambda r: r[1])
        if (actual != expected or db.execute('PRAGMA application_id').fetchone()[0] != _APP
                or db.execute('PRAGMA user_version').fetchone()[0] != version
                or db.execute('PRAGMA journal_mode').fetchone()[0] != 'delete'):
            raise _fail()
        meta = db.execute('SELECT id,highwater,capacity FROM card_meta').fetchall()
        if (len(meta) != 1 or tuple(meta[0])[::2] != (1, self.capacity)
                or type(meta[0]['highwater']) is not int or not 0 <= meta[0]['highwater'] < 10**17
                or type(meta[0]['capacity']) is not int):
            raise _fail()

    def initialize(self):
        failed = False
        try:
            with self._connection(create=True, wait=0.05) as db:
                # Inspect BEFORE any persistent pragma or schema write. Foreign,
                # future, empty-but-versioned, triggers and views are all refused.
                objects = db.execute('SELECT name FROM sqlite_master').fetchall()
                app = db.execute('PRAGMA application_id').fetchone()[0]
                version = db.execute('PRAGMA user_version').fetchone()[0]
                if objects or app or version:
                    self._verify(db)
                else:
                    if db.execute('PRAGMA journal_mode').fetchone()[0] != 'delete':
                        raise _fail()
                db.execute('PRAGMA synchronous=FULL')
                db.execute('BEGIN IMMEDIATE')
                if not objects and app == version == 0:
                    # Recheck under lock; another initializer may have won.
                    if not db.execute('SELECT name FROM sqlite_master').fetchall():
                        for sql in _SCHEMA + _WINDOW_SCHEMA:
                            db.execute(sql)
                        db.execute('INSERT INTO card_meta VALUES(1,0,?)', (self.capacity,))
                        db.execute(f'PRAGMA application_id={_APP}')
                        db.execute('PRAGMA user_version=2')
                self._verify(db)
                db.commit()
        except Exception:
            failed = True
        if failed:
            raise _fail()

    def migrate_v1(self):
        """Explicit atomic owned v1→v2 upgrade; all historical values stay exact.

        No automatic migration, downgrade, or adoption of an unknown layout.
        An absent window uses the frozen original TTL, including migrated rows.
        """
        failed = False
        try:
            with self._connection(wait=0.05) as db:
                self._verify(db, version=1)
                db.execute('PRAGMA synchronous=FULL')
                db.execute('BEGIN IMMEDIATE')
                self._verify(db, version=1)
                for sql in _WINDOW_SCHEMA:
                    db.execute(sql)
                db.execute('PRAGMA user_version=2')
                self._verify(db)
                db.commit()
        except Exception:
            failed = True
        if failed:
            raise _fail()

    @contextmanager
    def transaction(self, clock, *, wait=0):
        with self._connection(wait=wait) as db:
            self._verify(db)
            db.execute('PRAGMA synchronous=FULL')
            try:
                db.execute('BEGIN IMMEDIATE')
            except sqlite3.OperationalError as exc:
                if exc.sqlite_errorcode in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                    raise _Busy() from None
                raise
            self._verify(db)
            now = _stamp(clock())
            prior = db.execute('SELECT highwater FROM card_meta').fetchone()[0]
            if now < prior:
                raise _fail()
            db.execute('UPDATE card_meta SET highwater=?', (now,))
            yield db, now
            # Every private-effect caller performs all SQL first, then final
            # trusted authorization and only local time/stop checks before commit.
            db.commit()

    @staticmethod
    def _water(db, clock):
        now = _stamp(clock())
        prior = db.execute('SELECT highwater FROM card_meta').fetchone()[0]
        if now < prior:
            raise _fail()
        db.execute('UPDATE card_meta SET highwater=?', (now,))
        return now

    @staticmethod
    def _row(db, scope, delivery_id):
        _id(delivery_id)
        row = db.execute('SELECT * FROM cards WHERE scope=? AND delivery_id=?',
                         (_scope(scope), delivery_id)).fetchone()
        if row is None:
            raise _fail()
        return row

    @staticmethod
    def _status(row):
        return CardStatus(row['delivery_id'], row['state'], bool(row['cancelled']),
                          epoch=row['epoch'])

    @staticmethod
    def _intent(row, scope):
        if row['preview_json'] == '{"retired":true}':
            raise _fail()
        result = CardIntent(scope, row['delivery_id'], row['identity_json'], row['card_json'],
                            row['wire_body'], row['digest'], row['issued'], row['expires'])
        if (_digest(result.wire_body) != result.digest
                or _digest(row['preview_json'].encode()) != json.loads(result.identity_json)[
                    'preview_digest']):
            raise _fail()
        return result

    def _read(self, scope, delivery_id, *, body=False):
        with self._connection() as db:
            self._verify(db)
            row = self._row(db, scope, delivery_id)
            return self._intent(row, scope) if body else self._status(row)

    def frozen(self, scope, delivery_id):
        """Trusted local infrastructure only; not a status or HTTP requester API."""
        result = None
        try:
            result = self._read(scope, delivery_id, body=True)
        except Exception:
            pass
        if result is None:
            raise _fail()
        return result

    def status(self, scope, delivery_id):
        """Trusted metadata; lock contention uses the service's bounded retry loop."""
        result = None
        busy = False
        try:
            result = self._read(scope, delivery_id)
        except _Busy:
            busy = True
        except Exception:
            pass
        if busy:
            raise _Busy()
        if result is None:
            raise _fail()
        return result

    @staticmethod
    def _tokens(db, delivery_id, epoch, rows):
        if (type(epoch) is not int or not 0 <= epoch <= MAX_EPOCH or len(rows) > 3
                or any(type(page) is not int or not 0 <= page < 65536
                       for digest, action, page in rows)):
            raise _fail()
        db.execute('DELETE FROM card_tokens WHERE delivery_id=?', (delivery_id,))
        db.executemany('INSERT INTO card_tokens VALUES(?,?,?,?,?)',
                       [(digest, delivery_id, epoch, action, page) for digest, action, page in rows])

    def prepare(self, p, *, ttl, clock, check):
        preview_json = _preview(p)
        identity = _identity(p, preview_json)
        logical = _logical(p.scope, p.job_id, p.revision_id)
        with self.transaction(clock) as (db, now):
            row = db.execute('SELECT * FROM cards WHERE logical=?', (logical,)).fetchone()
            if row is not None:
                if row['identity_json'] != identity or row['preview_json'] != preview_json:
                    raise _fail()
                self._intent(row, p.scope)
                result = self._status(row)
            else:
                if db.execute('SELECT count(*) FROM cards').fetchone()[0] >= self.capacity:
                    raise _fail()
                card, tokens = _controls(p, 0)
                wire = _json(dict(receive_id=p.destination.address, msg_type='interactive',
                                  content=card)).encode()
                delivery_id = 'bcv1_' + _digest(identity.encode())
                if len(wire) > 32768:
                    raise _fail()
                marker = _json(dict(reason='send_unverified', at=now, request_digest=_digest(wire)))
                db.execute('INSERT INTO cards VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (logical, delivery_id, _scope(p.scope), identity, preview_json, card, wire,
                     _digest(wire), now, now + int(ttl * 1000000), 'pending', 0, None, None,
                     marker, None, None, None, 0, 0, None))
                self._tokens(db, delivery_id, 0, tokens)
                result = CardStatus(delivery_id, 'pending', False)
            self._water(db, clock)
            check()  # Last infrastructure operation before commit.
            if _stamp(clock()) < now:
                raise _fail()
        return result

    def begin(self, intent, *, clock, check):
        attempt = None
        expired = False
        with self.transaction(clock) as (db, now):
            row = self._row(db, intent.scope, intent.delivery_id)
            if row['state'] == 'pending' and now >= row['expires']:
                expired = True  # Commit highwater; never forget observed expiry.
            elif row['state'] == 'pending':
                self._live(row, now)
                if self._intent(row, intent.scope) != intent:
                    raise _fail()
                attempt = CardAttempt(intent, secrets.token_hex(24), secrets.token_hex(24))
                db.execute("UPDATE cards SET state='started',owner=?,token=? WHERE delivery_id=?",
                           (attempt.owner, attempt.token, intent.delivery_id))
                self._water(db, clock)
                check()
                try:
                    self._clock_live(clock, row)
                except Exception:
                    # Authority passed, but final expiry/deadline/clock failed.
                    # Commit only the inert send tombstone; no custody/IO escapes.
                    # Otherwise a restart could forget this observed expiration.
                    expired = True
        # A lost commit response never returns custody to the service.
        if expired:
            raise _fail()
        return attempt

    @staticmethod
    def _live(row, now):
        if row['cancelled'] or not row['issued'] <= now < row['expires']:
            raise _fail()

    @staticmethod
    def _clock_live(clock, row):
        now = _stamp(clock())
        if not row['issued'] <= now < row['expires']:
            raise _fail()

    @staticmethod
    def _window(db, row):
        """Navigation alone uses the explicit window; send TTL is immutable."""
        return db.execute('SELECT issued,expires FROM card_windows WHERE delivery_id=?',
                          (row['delivery_id'],)).fetchone() or row

    @staticmethod
    def _spend_only(db, delivery_id):
        db.execute('DELETE FROM card_tokens WHERE delivery_id=?', (delivery_id,))
        db.execute('UPDATE cards SET response_card=NULL WHERE delivery_id=?', (delivery_id,))

    def recovery_step(self, intent, expected_epoch, *, clock, check, ttl=None,
                      page=0, rendered=None):
        """Reserve once, publish, or validate release under current authority.

        Reservation spends the CAS epoch before unlocked rendering. Later failure
        never rolls that epoch back. Publish/release denial commits only a spend,
        and late commit waits are followed by another authority check by caller.
        """
        accepted = False
        with self.transaction(clock) as (db, now):
            row = self._row(db, intent.scope, intent.delivery_id)
            if (row['cancelled'] or row['state'] != 'create_acknowledged'
                    or row['epoch'] != expected_epoch):
                return False
            if self._intent(row, intent.scope) != intent:
                raise _fail()
            if ttl is not None:
                if expected_epoch >= MAX_EPOCH:
                    return False
                check(row)
                self._spend_only(db, intent.delivery_id)
                db.execute('UPDATE cards SET epoch=?,page=? WHERE delivery_id=?',
                           (expected_epoch + 1, page, intent.delivery_id))
                duration = int(ttl * 1000000)
                if not duration:
                    # A sub-microsecond TTL has no live representable interval;
                    # spend without rounding up or issuing any capabilities.
                    return False
                db.execute('INSERT OR REPLACE INTO card_windows VALUES(?,?,?)',
                           (intent.delivery_id, now, now + duration))
            elif rendered is not None:
                card, tokens = rendered
                self._tokens(db, intent.delivery_id, expected_epoch, tokens)
                db.execute('UPDATE cards SET response_card=? WHERE delivery_id=?',
                           (card, intent.delivery_id))
            window = self._window(db, row)
            try:
                self._water(db, clock)
                check(row)
                self._clock_live(clock, window)
                accepted = True
            except (Exception, asyncio.CancelledError):
                self._spend_only(db, intent.delivery_id)
        return accepted

    def discard_recovery(self, scope, delivery_id, epoch):
        """Best-effort spend after a failed release, even after stop/clock failure.

        No authority is created, and a competing later epoch is never touched.
        The reserved epoch itself was already committed before rendering.
        """
        with self._connection(wait=0.05) as db:
            self._verify(db)
            db.execute('BEGIN IMMEDIATE')
            self._verify(db)
            row = self._row(db, scope, delivery_id)
            if row['epoch'] == epoch:
                self._spend_only(db, delivery_id)
            db.commit()

    @staticmethod
    def _owned(row, attempt):
        if (row['state'] != 'started' or row['owner'] != attempt.owner
                or row['token'] != attempt.token or row['digest'] != attempt.intent.digest):
            raise _fail()

    def egress(self, attempt, *, clock, check):
        with self.transaction(clock) as (db, now):
            row = self._row(db, attempt.intent.scope, attempt.intent.delivery_id)
            self._owned(row, attempt)
            self._live(row, now)
            self._water(db, clock)
            check()
            self._clock_live(clock, row)

    def acknowledge(self, attempt, ack, *, clock, check):
        if (type(ack) is not CardCreateAck or ack.owner != attempt.owner
                or ack.token != attempt.token or ack.request_digest != attempt.intent.digest
                or ack.binding != attempt.intent.binding
                or ack.destination != attempt.intent.destination.address
                or type(ack.message_id) is not str or not _MESSAGE.fullmatch(ack.message_id)):
            raise _fail()
        _id(ack.sender_id)
        if ack.sender_id_type not in ('app_id', 'open_id', 'user_id', 'union_id'):
            raise _fail()
        with self.transaction(clock) as (db, now):
            row = self._row(db, attempt.intent.scope, attempt.intent.delivery_id)
            self._owned(row, attempt)
            self._live(row, now)
            receipt = _json(dict(kind='create_acknowledged', message_id=ack.message_id,
                request_digest=ack.request_digest, owner=ack.owner, token=ack.token,
                binding=ack.binding.to_dict(), destination=ack.destination,
                sender_id=ack.sender_id, sender_id_type=ack.sender_id_type,
                content_verified=False, human_received=False))
            db.execute("UPDATE cards SET state='create_acknowledged',message_id=?,receipt=? "
                       'WHERE delivery_id=?', (ack.message_id, receipt, attempt.intent.delivery_id))
            self._water(db, clock)
            check()
            self._clock_live(clock, row)
        # Tokens activate ONLY by joining this committed acknowledged row.

    def unknown(self, attempt, *, clock, reason='send_failed'):
        if reason not in ('send_failed', 'cancelled', 'abandoned'):
            raise _fail()
        with self.transaction(clock) as (db, now):
            row = self._row(db, attempt.intent.scope, attempt.intent.delivery_id)
            self._owned(row, attempt)
            db.execute("UPDATE cards SET state='unknown',first_failure=coalesce(first_failure,?) "
                       'WHERE delivery_id=?', (_json(dict(reason=reason, at=now)),
                                               attempt.intent.delivery_id))

    def cancel(self, scope, delivery_id, *, clock):
        with self.transaction(clock) as (db, now):
            self._row(db, scope, delivery_id)
            db.execute('UPDATE cards SET cancelled=1 WHERE delivery_id=?', (delivery_id,))


class _Operation:
    def __init__(self, service):
        self.service = service
        self.deadline = time.monotonic() + service.timeout
        self.owner = asyncio.current_task()
        self.abandoned = False

    def clock(self):
        if (self.abandoned or self.owner.cancelling() or self.service._stop.is_set()
                or time.monotonic() >= self.deadline):
            raise _fail()
        return self.service._now()


class CardDeliveryService:
    """Explicit first-delivery and callback lifecycle; no background polling."""
    def __init__(self, *, ledger, namespace, load, resolve_binding, authorize, transport,
                 clock=time.time, timeout=5, max_concurrency=1, retention=None):
        if (type(ledger) is not CardLedger or not all(callable(v) for v in
                (load, resolve_binding, authorize, clock, getattr(transport, 'send', None),
                 getattr(transport, 'check_ack', None)))
                or type(timeout) not in (float, int) or not 0 < timeout <= 30
                or not math.isfinite(timeout) or type(max_concurrency) is not int
                or not 1 <= max_concurrency <= 8):
            raise _fail()
        _id(namespace)
        from ostag.briefs.retention import configure_guard
        self._retention_enabled = configure_guard(retention, ledger)
        self.retention = retention
        self.ledger, self.namespace, self.transport = ledger, namespace, transport
        self._load, self._binding, self._authorize, self._clock = load, resolve_binding, authorize, clock
        self.timeout, self.max_concurrency = timeout, max_concurrency
        self._loop, self._active, self._last_time = None, 0, 0
        self._stop, self._lock = Event(), Lock()

    @property
    def active_operations(self):
        return self._active

    def stop(self):
        self._stop.set()

    def _now(self):
        value = self._clock()
        stamp = _stamp(value)
        if stamp < self._last_time or self._stop.is_set():
            raise _fail()
        self._last_time = stamp
        return value

    def _enter(self):
        loop = asyncio.get_running_loop()
        if (self._loop is not None and loop is not self._loop) or self._stop.is_set():
            raise _fail()
        if self._active >= self.max_concurrency:
            raise _fail()
        self._loop = loop
        self._active += 1
        return _Operation(self)

    def _current(self, p, identity=None, *, check_ack=None):
        if not self._lock.acquire(False):
            raise _fail()
        try:
            if (type(p) is not PrivatePreview or p.scope.namespace != self.namespace
                    or self._load(p.scope, p.job_id) != p
                    or (identity is not None and _identity(p, _preview(p)) != identity)
                    or type(p.binding) is not FeishuBinding):
                raise _fail()
            binding = self._binding()
            if type(binding) is not FeishuBinding or binding != p.binding:
                raise _fail()
            def mirrors():
                # All bounded/nonblocking mirrors follow retention IO. Token
                # rotation with an unchanged binding must invalidate a stale ACK.
                if (self._load(p.scope, p.job_id) != p or self._binding() != p.binding
                        or self._authorize(p) is not None):
                    raise _fail()
                if check_ack is not None and check_ack() is not None:
                    raise _fail()
            from ostag.briefs.retention import check_guard
            check_guard(self, p.scope, p.job_id, p.revision_id, authorize=mirrors)
        finally:
            self._lock.release()

    def _loaded(self, scope, job, revision):
        _scope(scope)
        if scope.namespace != self.namespace:
            raise _fail()
        _id(job)
        _id(revision)
        if not self._lock.acquire(False):
            raise _fail()
        try:
            p = self._load(scope, job)
        finally:
            self._lock.release()
        if (type(p) is not PrivatePreview or p.scope != scope or p.job_id != job
                or p.revision_id != revision):
            raise _fail()
        _preview(p)
        return p

    async def _db(self, op, method, *args, **kwargs):
        while True:
            op.clock()
            busy = False
            try:
                result = method(*args, **kwargs)
            except _Busy:
                busy = True
            if not busy:
                # Do not throw after begin returns: custody must reach caller even
                # if deadline expires at COMMIT, so only the owner may diagnose.
                return result
            await asyncio.sleep(min(0.01, max(0, op.deadline - time.monotonic())))

    async def _prepare(self, op, scope, job, revision, ttl):
        if (type(ttl) not in (int, float) or not 0.000001 <= ttl <= 300
                or not math.isfinite(ttl)):
            raise _fail()
        p = self._loaded(scope, job, revision)
        def policy():
            self._current(p)
            op.clock()
        return await self._db(op, self.ledger.prepare, p, ttl=ttl, clock=op.clock, check=policy)

    async def prepare(self, scope, job_id, revision_id, *, ttl=300):
        op = self._enter()
        result = None
        try:
            result = await self._prepare(op, scope, job_id, revision_id, ttl)
        except Exception:
            pass
        finally:
            self._active -= 1
        if result is None:
            raise _fail()
        return result

    async def deliver(self, scope, job_id, revision_id, *, ttl=300):
        """Callable first-delivery path, sharing one total deadline/capacity slot."""
        return await self._execute(scope, None, job_id, revision_id, ttl)

    async def send(self, scope, delivery_id):
        return await self._execute(scope, delivery_id, None, None, 300)

    async def recover_preview(self, scope, delivery_id, *, expected_epoch, message_id,
                              destination, tenant_id, app_id, page=0, ttl=300):
        """Trusted explicit local recovery of an ACK's exact original message.

        Infrastructure authenticates scope/destination/tenant/app/message; never
        derive these arguments or expected_epoch from existing button payloads.
        Scope-bound status exposes the CAS epoch. Each authorized reservation
        consumes it even when rendering, release, or response delivery fails.
        Only navigation gets a fresh bounded window: original send bytes/TTL,
        receipt, and lifetime source/grant/retention authority never renew.
        Returning a card proves neither client update nor full-content receipt.
        """
        op = self._enter()
        result = None
        epoch = None
        try:
            _scope(scope)
            if (scope.namespace != self.namespace
                    or type(expected_epoch) is not int or not 0 <= expected_epoch < MAX_EPOCH
                    or type(page) is not int or not 0 <= page < 65536
                    or type(ttl) not in (int, float) or not 0 < ttl <= 300
                    or not math.isfinite(ttl)
                    or type(destination) is not Destination or destination.platform != 'feishu'
                    or destination.chat_type != 'private' or type(message_id) is not str
                    or not _MESSAGE.fullmatch(message_id)):
                raise _fail()
            _id(tenant_id)
            _id(app_id)
            intent = await self._db(op, self.ledger._read, scope, delivery_id, body=True)
            key = json.loads(intent.identity_json)
            if (key['destination'] != destination.destination_id
                    or key['binding']['tenant_id'] != tenant_id
                    or key['binding']['app_id'] != app_id):
                raise _fail()
            p = self._loaded(scope, key['job'], key['revision'])

            def policy(row):
                if row['message_id'] != message_id:
                    raise _fail()
                self._current(p, intent.identity_json)

            if not await self._db(op, self.ledger.recovery_step, intent, expected_epoch,
                                  clock=op.clock, check=policy, ttl=ttl, page=page):
                raise _fail()
            epoch = expected_epoch + 1
            # No SQLite connection/write lock spans rendering. Reservation has
            # already pruned all older capabilities, including lost responses.
            rendered = _controls(p, page)
            await asyncio.sleep(0)
            if not await self._db(op, self.ledger.recovery_step, intent, epoch,
                                  clock=op.clock, check=policy, rendered=rendered):
                raise _fail()
            if not await self._db(op, self.ledger.recovery_step, intent, epoch,
                                  clock=op.clock, check=policy):
                raise _fail()
            # A final read snapshot follows commit waits and prevents another
            # card writer committing during the last retention/mirror gate.
            # Closing this read-only transaction needs no fsync/commit wait.
            with self.ledger._connection() as db:
                db.execute('BEGIN')
                self.ledger._verify(db)
                row = self.ledger._row(db, scope, delivery_id)
                if (row['cancelled'] or row['epoch'] != epoch
                        or row['state'] != 'create_acknowledged'
                        or row['message_id'] != message_id
                        or self.ledger._intent(row, scope) != intent):
                    raise _fail()
                window = self.ledger._window(db, row)
                if _stamp(op.clock()) < db.execute('SELECT highwater FROM card_meta').fetchone()[0]:
                    raise _fail()
                self._current(p, intent.identity_json)
                self.ledger._clock_live(op.clock, window)
            result = json.loads(rendered[0])
        except Exception:
            pass
        finally:
            if result is None and epoch is not None:
                try:
                    self.ledger.discard_recovery(scope, delivery_id, epoch)
                except Exception:
                    pass
            op.abandoned = True
            self._active -= 1
        if result is None:
            raise _fail()
        return result

    async def _execute(self, scope, delivery_id, job, revision, ttl):
        op = self._enter()
        task = attempt = result = None
        cancelled = False
        try:
            _scope(scope)
            if scope.namespace != self.namespace:
                raise _fail()
            if delivery_id is None:
                prepared = await self._prepare(op, scope, job, revision, ttl)
                delivery_id = prepared.delivery_id
            status = await self._db(op, self.ledger.status, scope, delivery_id)
            if status.state != 'pending' or status.cancelled:
                return status
            intent = await self._db(op, self.ledger._read, scope, delivery_id, body=True)
            key = json.loads(intent.identity_json)
            p = self._loaded(scope, key['job'], key['revision'])
            def policy():
                self._current(p, intent.identity_json)
            attempt = await self._db(op, self.ledger.begin, intent, clock=op.clock, check=policy)
            if attempt is None:
                # The pending observation may be stale. Without returned custody
                # we may only refresh status; a failed read must not diagnose or
                # mutate the winner, nor return the earlier pending observation.
                return await self._db(op, self.ledger.status, scope, delivery_id)
            async def egress():
                await self._db(op, self.ledger.egress, attempt, clock=op.clock, check=policy)
                policy()
                op.clock()
            gate = _TransportEgressGate(egress)
            async def exchange():
                return await self.transport.send(attempt, before_egress=gate)
            task = asyncio.create_task(exchange())
            while not task.done():
                op.clock()
                await asyncio.wait({task}, timeout=min(0.01, max(0, op.deadline - time.monotonic())))
            op.clock()
            ack = task.result()
            if not gate.valid:
                raise _fail()
            def acceptance_policy():
                self._current(p, intent.identity_json,
                              check_ack=lambda: self.transport.check_ack(attempt, ack))
                op.clock()
            await self._db(op, self.ledger.acknowledge, attempt, ack,
                           clock=op.clock, check=acceptance_policy)
            result = await self._db(op, self.ledger.status, scope, delivery_id)
        except asyncio.CancelledError:
            cancelled = True
        except Exception:
            pass
        finally:
            op.abandoned = True
            if task is not None and not task.done():
                task.cancel()
                def released(done):
                    if not done.cancelled():
                        done.exception()
                    self._active -= 1
                task.add_done_callback(released)
            else:
                self._active -= 1
        if result is None and attempt is not None:
            try:
                # Persist even after stop/deadline, with durable rollback checks.
                self.ledger.unknown(attempt, clock=self._clock,
                                    reason='cancelled' if cancelled else 'send_failed')
            except Exception:
                pass
            try:
                result = self.ledger.status(scope, delivery_id)
            except Exception:
                pass
        if cancelled:
            raise asyncio.CancelledError()
        if result is None:
            raise _fail()
        return result

    def cancel(self, scope, delivery_id):
        failed = False
        try:
            if scope.namespace != self.namespace:
                raise _fail()
            self.ledger.cancel(scope, delivery_id, clock=self._clock)
        except Exception:
            failed = True
        if failed:
            raise _fail()

    def handle_callback(self, callback_id, *, principal, destination, tenant_id, app_id, message_id):
        """Identity comes only from the authenticated router, never button values."""
        result = None
        try:
            result = self._navigate(callback_id, principal, destination, tenant_id, app_id, message_id)
        except (Exception, asyncio.CancelledError):
            pass
        if result is None:
            raise _fail()
        return result

    def _navigate(self, token, principal, destination, tenant, app, message):
        if (type(token) is not str or not _TOKEN.fullmatch(token)
                or type(principal) is not Principal or type(destination) is not Destination
                or principal.platform != 'feishu' or destination.platform != 'feishu'
                or destination.chat_type != 'private' or type(message) is not str
                or not _MESSAGE.fullmatch(message)):
            raise _fail()
        scope = RequesterContext(self.namespace, principal)
        failed_release = False
        with self.ledger.transaction(self._now, wait=0.05) as (db, now):
            cap = db.execute('SELECT * FROM card_tokens WHERE digest=?',
                             (_digest(token.encode()),)).fetchone()
            if cap is None:
                # Persist highwater even for expired/pruned/replayed capabilities.
                return None
            row = self.ledger._row(db, scope, cap['delivery_id'])
            if (row['state'] != 'create_acknowledged' or row['message_id'] != message
                    or row['epoch'] != cap['epoch'] or row['epoch'] >= MAX_EPOCH):
                return None
            window = self.ledger._window(db, row)
            if row['cancelled'] or not window['issued'] <= now < window['expires']:
                return None
            key = json.loads(row['identity_json'])
            if (key['destination'] != destination.destination_id
                    or key['binding']['tenant_id'] != tenant or key['binding']['app_id'] != app):
                return None
            p = self._loaded(scope, key['job'], key['revision'])
            self._current(p, row['identity_json'])  # Denial does not burn rightful buttons.
            epoch = row['epoch'] + 1
            card, new_tokens = _controls(p, cap['page'])
            self.ledger._tokens(db, row['delivery_id'], epoch, new_tokens)
            db.execute('UPDATE cards SET epoch=?,page=?,response_card=? WHERE delivery_id=?',
                       (epoch, cap['page'], card, row['delivery_id']))
            try:
                self.ledger._water(db, self._now)
                self._current(p, row['identity_json'])
                self.ledger._clock_live(self._now, window)
            except (Exception, asyncio.CancelledError):
                # Conservative spend-only commit on final denial: never restore
                # the old epoch; do not retain releasable fresh capabilities.
                db.execute('DELETE FROM card_tokens WHERE delivery_id=?', (row['delivery_id'],))
                db.execute('UPDATE cards SET response_card=NULL WHERE delivery_id=?',
                           (row['delivery_id'],))
                failed_release = True
        if failed_release:
            return None
        # Recheck durable cancellation/highwater AFTER ACK/epoch commit waits,
        # then all current authority before returning any private response.
        with self.ledger.transaction(self._now, wait=0.05) as (db, now):
            current = self.ledger._row(db, scope, row['delivery_id'])
            window = self.ledger._window(db, current)
            if current['cancelled'] or current['epoch'] != epoch:
                raise _fail()
            if not window['issued'] <= now < window['expires']:
                raise _fail()
            self.ledger._water(db, self._now)
            self._current(p, row['identity_json'])
            self.ledger._clock_live(self._now, window)
        self._current(p, row['identity_json'])
        self.ledger._clock_live(self._now, window)
        return json.loads(card)
