"""A06 trusted local HUMAN annotations and reversible CONTENT adoption.

Explicit retention= adds the typed A10 gate from retention.py. Its bounded local
SQLite authority read is the documented exception to the no-I/O policy-callback
contract; it never acquires this ledger from inside a retention transaction.
Original current authorizers and final clocks remain required. Retirement is a
sanctioned opt-in exception to append-only content, never to send/event identity.


Explicitly initialize a separate owned ledger; never pass a BriefStore database.
The ONLY root supplier is load(authenticated RequesterContext, job_id), returning
an original stored PrivatePreview (normally constructed from the A04 outbox).
load and authorize are mandatory current bounded local mirrors. authorize must
raise on ANY changed source/grant/owner/private destination/binding generation,
original revision or outbox cancellation. Neither callback may do IO under this
ledger's lock, reenter this service, or access this ledger. Composition owns their
latency/freshness bounds; Python cannot preempt synchronous callbacks or fsync.
No user authentication flags, editor names, replacement previews, network/model
calls, business approvals, sends or original-card updates are accepted here.
A11 must supply authenticated context and bind signed card revision IDs on the
server; browser-provided 'latest' is never a confirmation capability.

Roots and events are append-only through this API. A correction annotates the
whole draft or an exact original section/item; it never changes a claim, source,
URL, coverage, provenance or excerpt. New corrections invalidate CONTENT adoption.
Withdrawal requires current access and exact adopted HEAD; revoked access fails
closed, including withdrawal, and releases no private history. Replays return an
immutable historical event plus separately calculated CURRENT state, never apply
an event again. Request IDs are permanent per requester/job across operation kinds.

Trusted local disk is required: digests/strict schema checks detect inconsistency,
not filesystem tampering. There is no cross-system atomicity with policy mirrors.
BEGIN EXCLUSIVE bounds lock waiting to 50ms and removes reader lock upgrades at
COMMIT; DELETE/FULL durability still cannot bound OS/fsync latency. All SQL,
materialization and rendering precede final current-root/policy/clock checks at
the logical precommit/release point. A commit error/crash can have an ambiguous
outcome: recover using the SAME request ID and exact payload, under current access;
never infer success from an exception or retry with a fresh ID. Root registration
is inherently idempotent by scoped job plus exact frozen snapshot. No automatic
pruning/migration: permanent row, per-job event and chain limits survive restart.
Admission conservatively leaves one global row per existing root for withdrawal;
each adoption also leaves one per-job event slot. Withdrawal can use that reserve.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from functools import wraps
import hashlib
import json
import math
from pathlib import Path
import re
import secrets
import sqlite3
import time

from ostag.briefs.delivery import FeishuBinding
from ostag.briefs.models import SourceSet
from ostag.briefs.rendering import PrivatePreview, _bytes, _element, _parts, defang
from ostag.briefs.store import _retirement_method, PrivateDestination, RequesterContext
from ostag.briefs.validation import (
    SECTIONS, Coverage, SourceLink, strict_json, validate_output,
)
from ostag.core.identity import Principal

MAX_CORRECTION_BYTES = 8192
MAX_ROOT_BYTES = 1048576
MAX_ROWS = 10000
MAX_CHAIN = 32
MAX_EVENTS = 256
MAX_PAGES = 4096
_APP = 0x42525631
_ID = re.compile(r'[A-Za-z0-9_-]{1,128}\Z')
_NEW_ID = re.compile(r'(?:rv|re)_[0-9a-f]{48}\Z')
_NOTICE = ('HUMAN corrections are unverified human input, not source facts. '
           'CONTENT adoption is not business approval or permission to act. '
           'Original model claims require semantic review; review every coverage gap.')
_SCHEMA = (
    'CREATE TABLE revision_meta (id INTEGER PRIMARY KEY CHECK(id=1), '
    'highwater INTEGER NOT NULL, capacity INTEGER NOT NULL, '
    'chain_limit INTEGER NOT NULL, event_limit INTEGER NOT NULL)',
    'CREATE TABLE revision_roots (scope TEXT NOT NULL, job TEXT NOT NULL UNIQUE, '
    'snapshot TEXT NOT NULL, digest TEXT NOT NULL, timestamp_us INTEGER NOT NULL, '
    'PRIMARY KEY(scope,job))',
    'CREATE TABLE revision_events (scope TEXT NOT NULL, job TEXT NOT NULL, '
    'request TEXT NOT NULL, parent TEXT, record TEXT NOT NULL, digest TEXT NOT NULL, '
    'PRIMARY KEY(scope,job,request), UNIQUE(scope,job,parent))',
)


class RevisionError(RuntimeError):
    """Public fixed diagnostics; no payload, path or chained exception."""
    message = 'brief revision unavailable'

    def __init__(self):
        super().__init__(self.message)


class RevisionInvalid(RevisionError):
    message = 'invalid revision request'


class RevisionDenied(RevisionError):
    message = 'revision access denied'


class RevisionConflict(RevisionError):
    message = 'revision intent conflict'


class RevisionStorageError(RevisionError):
    message = 'revision storage unavailable'


class RevisionClockError(RevisionError):
    message = 'revision clock unavailable'


class RevisionLimit(RevisionError):
    message = 'revision capacity exhausted'


_ERRORS = (RevisionInvalid, RevisionDenied, RevisionConflict, RevisionStorageError,
           RevisionClockError, RevisionLimit)


def _public(fn):
    @wraps(fn)
    def call(*args, **kwargs):
        failure = None
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            failure = type(exc) if type(exc) in _ERRORS else RevisionStorageError
        # Outside the handler: even __context__ cannot retain a secret exception.
        raise failure()
    return call


def _json(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':'),
                      allow_nan=False)


def _digest(value):
    return hashlib.sha256(value.encode('ascii')).hexdigest()


def _id(value):
    if type(value) is not str or not _ID.fullmatch(value):
        raise RevisionInvalid()
    return value


def _integer(value, maximum, minimum=0, error=RevisionInvalid):
    if type(value) is not int or not minimum <= value <= maximum:
        raise error()
    return value


def _scope(scope):
    if type(scope) is not RequesterContext or type(scope.principal) is not Principal:
        raise RevisionDenied()
    # Revalidate even forged frozen instances; namespace/actor use the existing
    # domain's identifier grammar, while externally supplied operation IDs are narrow.
    scope.__post_init__()
    if scope.principal.platform != 'feishu':
        raise RevisionDenied()
    return _json([scope.namespace, scope.principal.to_dict()])


def _clock(clock):
    stamp = None
    try:
        value = clock()
        if type(value) in (int, float) and math.isfinite(value) and 0 <= value < 10**11:
            stamp = int(value * 1000000)
    except Exception:
        pass
    if stamp is None:
        raise RevisionClockError()
    return stamp


def _decode(raw, maximum):
    if type(raw) is not str or len(raw) > maximum:
        raise RevisionStorageError()
    value = strict_json(raw, maximum)
    if _json(value) != raw:
        raise RevisionStorageError()
    return value


def _snapshot(p):
    """Validate/detach the trusted loader result, including nested scalar types."""
    if type(p) is not PrivatePreview:
        raise RevisionDenied()
    p.__post_init__()
    _scope(p.scope)
    for value in (p.job_id, p.revision_id, p.outbox_id):
        _id(value)
    if p.state != 'draft_ready':
        raise RevisionDenied()
    sources = SourceSet.from_dict(p.sources.to_dict())
    binding = FeishuBinding(**p.binding.to_dict())
    draft_dict = p.draft.to_dict()
    draft = validate_output(_json({'version': 'brief-output-v1'} |
        {s: draft_dict[s] for s in SECTIONS}), sources=sources,
        coverage=Coverage.from_dict(draft_dict['coverage']), max_output_bytes=MAX_ROOT_BYTES,
        links=tuple(SourceLink(**v) for v in draft_dict['links']))
    if _json(draft.to_dict()) != _json(draft_dict):
        raise RevisionDenied()
    detached = PrivatePreview(RequesterContext(p.scope.namespace, p.scope.principal),
        p.job_id, p.revision_id, PrivateDestination(p.scope.principal, p.destination.address),
        binding, sources, draft, p.state, p.outbox_id)
    raw = _json(dict(scope=json.loads(_scope(detached.scope)), job=detached.job_id,
        revision=detached.revision_id, destination=detached.destination.address,
        binding=detached.binding.to_dict(), sources=detached.sources.to_dict(),
        draft=detached.draft.to_dict(), state=detached.state, outbox=detached.outbox_id))
    if len(raw) > MAX_ROOT_BYTES:
        raise RevisionLimit()
    return detached, raw


def _target(p, section, item_id):
    if section is None:
        if item_id is not None:
            raise RevisionInvalid()
    elif type(section) is not str or section not in SECTIONS:
        raise RevisionInvalid()
    elif item_id is not None:
        if (type(item_id) is not str or len(item_id) > 256
                or item_id not in {i.id for i in getattr(p.draft, section)}):
            raise RevisionInvalid()


def _text(value):
    if (type(value) is not str or not value.strip() or len(value) > MAX_CORRECTION_BYTES
            or any(0xD800 <= ord(c) <= 0xDFFF for c in value)
            or len(value.encode('utf-8')) > MAX_CORRECTION_BYTES):
        raise RevisionInvalid()


@dataclass(frozen=True, repr=False)
class RevisionEvent:
    event_id: str
    operation: str
    revision_id: str
    parent_revision_id: str | None
    actor: Principal
    timestamp_us: int
    request_id: str
    expected_revision_id: str
    text: str | None
    section: str | None
    item_id: str | None


@dataclass(frozen=True, repr=False)
class RevisionState:
    head_revision_id: str
    adopted_revision_id: str | None

    @property
    def business_approved(self):
        return False


@dataclass(frozen=True, repr=False)
class RevisionReceipt:
    """event is historical evidence; ONLY current describes current adoption."""
    event: RevisionEvent
    replayed: bool
    current: RevisionState


@dataclass(frozen=True, repr=False)
class RevisionView:
    original: PrivatePreview
    root_digest: str
    corrections: tuple[RevisionEvent, ...]
    events: tuple[RevisionEvent, ...]
    state: RevisionState


@dataclass(frozen=True, repr=False)
class RenderedRevision:
    """Detached immutable JSON pages; decode for a local transport if authorized."""
    revision_id: str
    pages: tuple[str, ...]


class RevisionLedger:
    @_public
    def __init__(self, path, *, capacity=1000, chain_limit=MAX_CHAIN, event_limit=MAX_EVENTS):
        self.path = Path(path)
        self.capacity = _integer(capacity, MAX_ROWS, 1)
        self.chain_limit = _integer(chain_limit, MAX_CHAIN, 1)
        self.event_limit = _integer(event_limit, MAX_EVENTS, 1)

    @_retirement_method
    def retire_local(self, scope, job_id):
        """Permanent root/event identity rows; denied hash-only history, never adoption."""
        from ostag.briefs.retention import _RETIRED, owned_retirement
        with owned_retirement(self, compact=True) as db:
            key = _scope(scope)
            db.execute('UPDATE revision_roots SET snapshot=? WHERE scope=? AND job=?',
                       (_RETIRED, key, job_id))
            db.execute('UPDATE revision_events SET record=? WHERE scope=? AND job=?',
                       (_RETIRED, key, job_id))
            self._verify_retired(db, scope, job_id)

    def _verify_retired(self, db, scope, job_id):
        """Both original snapshots and every event/history body must be gone."""
        from ostag.briefs.retention import _RETIRED
        key = _scope(scope)
        if (db.execute('SELECT 1 FROM revision_roots WHERE scope=? AND job=? AND snapshot!=?',
                       (key, job_id, _RETIRED)).fetchone()
                or db.execute('SELECT 1 FROM revision_events WHERE scope=? AND job=? AND record!=?',
                              (key, job_id, _RETIRED)).fetchone()):
            raise RevisionStorageError()

    @contextmanager
    def _connection(self, *, create=False):
        db = sqlite3.connect(self.path.resolve().as_uri() +
            ('?mode=rwc' if create else '?mode=rw'), uri=True, timeout=0.05, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            yield db
        finally:
            # close rolls back an uncommitted transaction on every failure path.
            db.close()

    def _verify(self, db):
        actual = [tuple(row) for row in db.execute(
            "SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_autoindex_%' "
            'ORDER BY name')]
        expected = sorted([('table', ddl.split()[2], ddl) for ddl in _SCHEMA], key=lambda r: r[1])
        if (actual != expected or db.execute('PRAGMA application_id').fetchone()[0] != _APP
                or db.execute('PRAGMA user_version').fetchone()[0] != 1
                or db.execute('PRAGMA journal_mode').fetchone()[0] != 'delete'
                or db.execute('PRAGMA quick_check(1)').fetchone()[0] != 'ok'):
            raise RevisionStorageError()
        rows = db.execute('SELECT * FROM revision_meta').fetchall()
        if len(rows) != 1 or tuple(rows[0]) != (
                1, rows[0]['highwater'], self.capacity, self.chain_limit, self.event_limit):
            raise RevisionStorageError()
        for key in ('id', 'highwater', 'capacity', 'chain_limit', 'event_limit'):
            _integer(rows[0][key], 10**17-1, error=RevisionStorageError)
        count = db.execute('SELECT (SELECT count(*) FROM revision_roots) + '
                           '(SELECT count(*) FROM revision_events)').fetchone()[0]
        if count > self.capacity:
            raise RevisionStorageError()
        return rows[0]['highwater'], count

    @_public
    def initialize(self):
        with self._connection(create=True) as db:
            def empty():
                return (not db.execute('SELECT name FROM sqlite_master').fetchall()
                        and db.execute('PRAGMA application_id').fetchone()[0] == 0
                        and db.execute('PRAGMA user_version').fetchone()[0] == 0
                        and db.execute('PRAGMA journal_mode').fetchone()[0] == 'delete')
            if not empty():
                self._verify(db)  # No persistent write to foreign/future/malformed files.
            db.execute('PRAGMA synchronous=FULL')
            db.execute('BEGIN EXCLUSIVE')
            if empty():  # Concurrent initializer may already have registered the schema.
                for ddl in _SCHEMA:
                    db.execute(ddl)
                db.execute('INSERT INTO revision_meta VALUES(1,0,?,?,?)',
                           (self.capacity, self.chain_limit, self.event_limit))
                db.execute(f'PRAGMA application_id={_APP}')
                db.execute('PRAGMA user_version=1')
            self._verify(db)
            db.commit()

    @contextmanager
    def _transaction(self):
        with self._connection() as db:
            self._verify(db)
            db.execute('PRAGMA synchronous=FULL')
            db.execute('BEGIN EXCLUSIVE')
            self._verify(db)
            yield db
            db.commit()


def _event_dict(event):
    return {name: getattr(event, name) for name in event.__dataclass_fields__} | {
        'actor': event.actor.to_dict()}


def _history(db, p, ledger, highwater, registered):
    rows = db.execute('SELECT * FROM revision_events WHERE scope=? AND job=?',
                      (_scope(p.scope), p.job_id)).fetchall()
    if len(rows) > ledger.event_limit:
        raise RevisionStorageError()
    events = []
    for row in rows:
        value = _decode(row['record'], 65536)
        if (set(value) != set(RevisionEvent.__dataclass_fields__) | {'ordinal'}
                or row['digest'] != _digest(row['record'])
                or row['request'] != value['request_id']
                or row['parent'] != value['parent_revision_id']):
            raise RevisionStorageError()
        ordinal = _integer(value.pop('ordinal'), ledger.event_limit-1,
                           error=RevisionStorageError)
        value['actor'] = Principal(**value['actor'])
        events.append((ordinal, RevisionEvent(**value)))
    events.sort(key=lambda pair: pair[0])
    head, adopted, stamp = p.revision_id, None, registered
    ids, corrections, ordered = {head}, [], []
    for ordinal, (stored_ordinal, e) in enumerate(events):
        if (ordinal != stored_ordinal or e.actor != p.scope.principal
                or e.expected_revision_id != head):
            raise RevisionStorageError()
        for value in (e.event_id, e.revision_id, e.expected_revision_id, e.request_id):
            _id(value)
        if (not _NEW_ID.fullmatch(e.event_id) or not e.event_id.startswith('re_')
                or e.event_id in ids):
            raise RevisionStorageError()
        ids.add(e.event_id)
        _integer(e.timestamp_us, highwater, stamp, RevisionStorageError)
        stamp = e.timestamp_us
        if e.operation == 'correct':
            _text(e.text)
            _target(p, e.section, e.item_id)
            if (e.parent_revision_id != head or not _NEW_ID.fullmatch(e.revision_id)
                    or not e.revision_id.startswith('rv_') or e.revision_id in ids):
                raise RevisionStorageError()
            ids.add(e.revision_id)
            head, adopted = e.revision_id, None
            corrections.append(e)
        elif (e.operation in ('adopt', 'withdraw') and e.revision_id == head
                and e.parent_revision_id is e.text is e.section is e.item_id is None):
            if e.operation == 'withdraw' and adopted != head:
                raise RevisionStorageError()
            adopted = head if e.operation == 'adopt' else None
        else:
            raise RevisionStorageError()
        ordered.append(e)
    if len(corrections) > ledger.chain_limit:
        raise RevisionStorageError()
    return tuple(ordered), tuple(corrections), RevisionState(head, adopted)


class RevisionService:
    @_public
    def __init__(self, *, ledger, namespace, load, authorize, clock=time.time, retention=None):
        if (type(ledger) is not RevisionLedger or type(namespace) is not str
                or not namespace or len(namespace) > 256
                or not all(callable(fn) for fn in (load, authorize, clock))):
            raise RevisionInvalid()
        from ostag.briefs.retention import configure_guard
        self._retention_enabled = configure_guard(retention, ledger)
        self.retention = retention
        self.ledger, self.namespace = ledger, namespace
        self._load, self._authorize, self._clock = load, authorize, clock

    def _fresh(self, scope, job, expected=None):
        result, failure = None, RevisionDenied
        try:
            p, raw = _snapshot(self._load(scope, job))
            if (p.scope != scope or p.job_id != job or
                    (expected is not None and raw != expected)):
                raise RevisionDenied()
            from ostag.briefs.retention import check_guard
            check_guard(self, p.scope, p.job_id, p.revision_id,
                        authorize=lambda: self._authorize(p))
            result = p, raw
        except Exception as exc:
            if type(exc) is RevisionLimit:
                failure = RevisionLimit
        if result is None:
            raise failure()
        return result

    def _run(self, scope, job, operation, expected=None, request=None,
             text=None, section=None, item_id=None):
        scope_key = _scope(scope)
        if scope.namespace != self.namespace:
            raise RevisionDenied()
        _id(job)
        if operation in ('correct', 'adopt', 'withdraw'):
            _id(expected)
            _id(request)
        if operation == 'correct':
            _text(text)
        p, raw = self._fresh(scope, job)
        if operation == 'correct':
            _target(p, section, item_id)
        with self.ledger._transaction() as db:
            highwater, count = self.ledger._verify(db)
            roots = db.execute('SELECT count(*) FROM revision_roots').fetchone()[0]
            stamp = _clock(self._clock)
            if stamp < highwater:
                raise RevisionClockError()
            row = db.execute('SELECT * FROM revision_roots WHERE scope=? AND job=?',
                             (scope_key, job)).fetchone()
            if row is None:
                if operation != 'register':
                    raise RevisionDenied()
                if count + 1 + roots > self.ledger.capacity:
                    raise RevisionLimit()
                db.execute('INSERT INTO revision_roots VALUES(?,?,?,?,?)',
                           (scope_key, job, raw, _digest(raw), stamp))
                persisted = db.execute('SELECT * FROM revision_roots WHERE scope=? AND job=?',
                                       (scope_key, job)).fetchone()
                if persisted is None or tuple(persisted) != (
                        scope_key, job, raw, _digest(raw), stamp):
                    raise RevisionStorageError()
                count += 1
                registered = stamp
            else:
                if row['snapshot'] == '{"retired":true}':
                    raise RevisionDenied()
                # Compare canonical bytes, not IDs or Python bool/int equality.
                _decode(row['snapshot'], MAX_ROOT_BYTES)
                if row['digest'] != _digest(row['snapshot']):
                    raise RevisionStorageError()
                if row['snapshot'] != raw:
                    raise RevisionDenied()
                registered = _integer(row['timestamp_us'], highwater,
                                      error=RevisionStorageError)
            events, corrections, state = _history(
                db, p, self.ledger, max(highwater, registered), registered)
            replayed, event = False, None
            if operation in ('correct', 'adopt', 'withdraw'):
                event = next((e for e in events if e.request_id == request), None)
                if event is not None:
                    if (event.operation, event.expected_revision_id, event.text,
                        event.section, event.item_id) != (operation, expected, text, section, item_id):
                        raise RevisionConflict()
                    replayed = True
                else:
                    if expected != state.head_revision_id:
                        raise RevisionConflict()
                    if operation == 'withdraw' and state.adopted_revision_id != expected:
                        raise RevisionConflict()
                    reserve = 0 if operation == 'withdraw' else roots
                    event_reserve = 1 if operation == 'adopt' else 0
                    if (count + 1 + reserve > self.ledger.capacity
                            or len(events) + 1 + event_reserve > self.ledger.event_limit
                            or (operation == 'correct' and
                                len(corrections) >= self.ledger.chain_limit)):
                        raise RevisionLimit()
                    event = RevisionEvent('re_' + secrets.token_hex(24), operation,
                        'rv_' + secrets.token_hex(24) if operation == 'correct' else expected,
                        expected if operation == 'correct' else None, scope.principal, stamp,
                        request, expected, text, section, item_id)
                    record = _json(_event_dict(event) | {'ordinal': len(events)})
                    db.execute('INSERT INTO revision_events VALUES(?,?,?,?,?,?)',
                        (scope_key, job, request, event.parent_revision_id, record, _digest(record)))
                    events, corrections, state = _history(db, p, self.ledger, stamp, registered)
                    if event not in events:
                        raise RevisionStorageError()
            view = RevisionView(p, _digest(raw), corrections, events, state)
            if operation == 'render':
                result = _render(view)
            elif event is not None:
                result = RevisionReceipt(event, replayed, state)
            else:
                result = view
            fence = _clock(self._clock)
            if fence < stamp:
                raise RevisionClockError()
            db.execute('UPDATE revision_meta SET highwater=? WHERE id=1', (fence,))
            if self.ledger._verify(db)[0] != fence:
                raise RevisionStorageError()
            # ALL SQL/waits/rendering have completed. Only COMMIT follows these
            # current local policy checks and nonblocking trusted clock sample.
            self._fresh(scope, job, raw)
            if _clock(self._clock) < fence:
                raise RevisionClockError()
        return result

    @_public
    def register(self, scope, job_id):
        return self._run(scope, job_id, 'register')

    @_public
    def read(self, scope, job_id):
        return self._run(scope, job_id, 'read')

    @_public
    def render(self, scope, job_id):
        """Authorize AFTER rendering as well as before any private work."""
        return self._run(scope, job_id, 'render')

    @_public
    def correct(self, scope, job_id, *, expected_revision_id, request_id, text,
                section=None, item_id=None):
        return self._run(scope, job_id, 'correct', expected_revision_id, request_id,
                         text, section, item_id)

    @_public
    def adopt(self, scope, job_id, *, expected_revision_id, request_id):
        return self._run(scope, job_id, 'adopt', expected_revision_id, request_id)

    @_public
    def withdraw(self, scope, job_id, *, expected_revision_id, request_id):
        return self._run(scope, job_id, 'withdraw', expected_revision_id, request_id)


def _render(view):
    """Linear bounded work; no repeated rendering of all pages per selected page."""
    p, pages = view.original, []
    header = 'Revision ' + defang(view.state.head_revision_id)
    coverage = ('Collection reports complete coverage.' if p.draft.coverage.complete else
                f'Partial collection: {len(p.draft.coverage.gaps)} coverage gaps.')

    def prose(label, value):
        chunks = _parts(value)
        return [_element(f'{label} part {i}/{len(chunks)}: {part}')
                for i, part in enumerate(chunks, 1)]

    def group(label, units):
        current, size = [], 0
        base = [_element(header), _element(_NOTICE), _element(coverage), _element(label)]
        budget = min(12000, 16384 - _bytes({'elements': base}) - 256)
        def flush():
            if len(pages) >= MAX_PAGES:
                raise RevisionLimit()
            pages.append([*base, *current])
        for unit in units:
            cost = _bytes(unit)
            if current and (size + cost > budget or len(current) >= 7):
                flush()
                current, size = [], 0
            current.append(unit)
            size += cost
        if current:
            flush()

    units = prose('Original task ID', p.job_id) + prose('Original revision ID', p.revision_id)
    for i, gap in enumerate(p.draft.coverage.gaps, 1):
        units.extend(prose(f'Coverage gap {i}', gap))
    for section in SECTIONS:
        for item in getattr(p.draft, section):
            units.extend(prose('Original ' + section + ' / ' + item.claim.kind,
                               item.claim.text))
            units.extend(prose('Original item / cited source IDs',
                               item.id + ' / ' + ' '.join(item.claim.source_ids)))
    for source in p.sources.sources:
        units.extend(prose('Source ID / resource / version',
                           f'{source.source_id} / {source.resource_id} / {source.resource_version}'))
    for link in p.draft.links:
        units.append({'tag': 'action', 'actions': [{'tag': 'button', 'type': 'default',
            'text': {'tag': 'plain_text', 'content': 'Open trusted source'}, 'url': link.url}]})
    group('ORIGINAL SOURCE-BACKED DRAFT — unchanged', units)
    for e in view.corrections:
        label = ('HUMAN annotation / revision ' + defang(e.revision_id) + ' / parent ' +
                 defang(e.parent_revision_id))
        metadata = prose('Authenticated HUMAN editor', e.actor.platform + ' ' + e.actor.actor_id)
        metadata += prose('Request ID', e.request_id)
        metadata += prose('Timestamp UTC microseconds', str(e.timestamp_us))
        metadata += prose('Original annotation target',
                          (e.section or 'whole draft') + (' / ' + e.item_id if e.item_id else ''))
        group(label, metadata + prose('Exact correction (visibly escaped)', e.text))
    encoded = []
    for i, elements in enumerate(pages, 1):
        card = _json({'elements': [*elements, _element(f'Page {i} of {len(pages)}')]})
        if len(card.encode('utf-8')) > 16384:
            raise RevisionLimit()
        encoded.append(card)
    return RenderedRevision(view.state.head_revision_id, tuple(encoded))
