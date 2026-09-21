"""Bounded, trusted-local inventory of Brief schema4 and Card schema2 only.

Persisted facts never authorize work, recover jobs, resolve unknown effects, or
prove deployment drain. Retention, revision and callback stores are NOT covered;
this is not total-runtime reconciliation. Keep admission closed until future
production reconciliation. Each database has its own read transaction, NOT an
atomic global snapshot. Before/after file fingerprints detect observed changes;
they are neither freshness attestations nor a promise of ABA detection. SQLite
progress/deadline budgets bound query work, not arbitrary filesystem stalls.
Owners are trusted RequesterContexts, never endpoint/model-supplied authority.
Only hashes bind private boot/owner inputs; no identifiers or bodies are returned.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
from time import monotonic
from typing import Literal

from ostag.briefs.card_delivery import CardLedger
from ostag.briefs.store import BriefStore, RequesterContext, _scope

Reason = Literal['ok', 'missing', 'file', 'alias', 'changed', 'locked', 'schema',
                 'malformed', 'owner', 'overflow', 'budget', 'unreadable']


@dataclass(frozen=True)
class Fingerprint:
    device: int
    inode: int
    size: int
    mtime_ns: int
    ctime_ns: int
    schema_version: int | None = None


@dataclass(frozen=True)
class StoreInventory:
    complete: bool
    reason: Reason
    fingerprint: Fingerprint | None
    counts: tuple[tuple[str, int], ...] | None = None


@dataclass(frozen=True)
class StartupInventory:
    binding: str
    brief: StoreInventory
    card: StoreInventory


class _Incomplete(Exception):
    def __init__(self, reason: Reason):
        self.reason = reason


def _require(condition, reason: Reason = 'malformed'):
    if not condition:
        raise _Incomplete(reason)


def _fingerprint(path):
    # Also reject symlinked ancestors; no resolve() hides an alias from inspection.
    for component in (path, *path.parents):
        _require(not stat.S_ISLNK(component.lstat().st_mode), 'file')
    value = path.lstat()
    _require(stat.S_ISREG(value.st_mode), 'file')
    return Fingerprint(value.st_dev, value.st_ino, value.st_size,
                       value.st_mtime_ns, value.st_ctime_ns)


def _reason(exc):
    if isinstance(exc, _Incomplete):
        return exc.reason
    if isinstance(exc, FileNotFoundError):
        return 'missing'
    code = getattr(exc, 'sqlite_errorcode', 0) & 255
    return {sqlite3.SQLITE_BUSY: 'locked', sqlite3.SQLITE_LOCKED: 'locked',
            sqlite3.SQLITE_INTERRUPT: 'budget'}.get(code, 'unreadable')


def _rows(db, remaining, table, text, scalars=''):
    # CASE bounds bytes in SQLite BEFORE Python fetches unconstrained identifiers.
    columns = [f"CASE WHEN typeof({c})='text' AND length(CAST({c} AS BLOB)) "
               f"BETWEEN 1 AND 1024 THEN {c} END" for c in text.split(',') if c]
    if scalars:
        columns.extend(f"CASE WHEN typeof({c})='integer' THEN {c} END"
                       for c in scalars.split(','))
    rows = [tuple(r) for r in db.execute(
        f"SELECT {','.join(columns)} FROM {table} LIMIT ?", (remaining[0] + 1,))]
    remaining[0] -= len(rows)
    _require(remaining[0] >= 0, 'overflow')
    _require(all(None not in row for row in rows))
    return rows


def _tally(counts, category, states, rows):
    allowed = states.split()
    for state in allowed:
        counts[f'{category}.{state}'] = 0
    for state in rows:
        key = f'{category}.{state}'
        _require(state in allowed)
        counts[key] += 1


def _brief_counts(db, remaining, owners):
    rows = _rows(db, remaining, 'brief_jobs', 'job_id,namespace,platform,actor_id,state',
                 '(owner_id IS NOT NULL OR lease_us IS NOT NULL)')
    jobs = {r[0]: r[1:4] for r in rows}
    _require(all(scope in owners for scope in jobs.values()), 'owner')
    counts = {'jobs.custody': sum(r[5] for r in rows)}
    _tally(counts, 'jobs', 'queued collecting generating draft_ready needs_input failed cancelled',
           (r[4] for r in rows))
    revisions = dict(_rows(db, remaining, 'brief_revisions', 'revision_id,job_id'))
    _require(all(job in jobs for job in revisions.values()))
    profiles = {r[0] for r in _rows(db, remaining, 'brief_cost_profiles', 'job_id')}
    _require(profiles <= jobs.keys())
    costs = _rows(db, remaining, 'brief_cost_calls', 'reservation_id,job_id,state')
    _require(all(r[1] in profiles for r in costs))
    _tally(counts, 'costs', 'reserved started unknown settled released', (r[2] for r in costs))
    for table in ('brief_intakes', 'brief_failures'):
        _require(all(r[0] in jobs for r in _rows(db, remaining, table, 'job_id')))
    boxes = _rows(db, remaining, 'brief_outbox', 'delivery_id,job_id,revision_id,state')
    _require(all(r[1] in jobs and revisions.get(r[2]) == r[1] for r in boxes))
    outbox = {r[0]: r[1:3] for r in boxes}
    _tally(counts, 'outbox', 'pending cancelled', (r[3] for r in boxes))
    deliveries = _rows(db, remaining, 'brief_deliveries',
                       'delivery_id,job_id,revision_id,namespace,platform,actor_id,state')
    _require(all(r[3:6] in owners for r in deliveries), 'owner')
    _require(all(outbox.get(r[0]) == r[1:3] and jobs.get(r[1]) == r[3:6]
                 for r in deliveries))
    delivery_ids = {r[0] for r in deliveries}
    _tally(counts, 'deliveries', 'pending started unknown provider_accepted',
           (r[6] for r in deliveries))
    reads = _rows(db, remaining, 'brief_delivery_reads', 'delivery_id', 'ordinal,settled')
    _require(all(r[0] in delivery_ids and type(r[1]) is int and 1 <= r[1] <= 32
                 and type(r[2]) is int and r[2] in (0, 1) for r in reads))
    _tally(counts, 'reads', 'unsettled settled',
           ('settled' if r[2] else 'unsettled' for r in reads))
    observations = _rows(db, remaining, 'brief_delivery_observations', 'delivery_id', 'ordinal')
    _require(all(r[0] in delivery_ids and type(r[1]) is int and 1 <= r[1] <= 32
                 for r in observations))
    return tuple(sorted(counts.items()))


def _card_counts(db, remaining, owners, capacity):
    _rows(db, remaining, 'card_meta', '', 'id')
    rows = _rows(db, remaining, 'cards', 'logical,delivery_id,scope,state', 'cancelled')
    _require(len(rows) <= capacity)
    scopes = {json.dumps(s, ensure_ascii=True, separators=(',', ':')) for s in owners}
    _require(all(r[2] in scopes for r in rows), 'owner')
    _require(all(type(r[4]) is int and r[4] in (0, 1) for r in rows))
    counts = {'cards.cancelled': sum(r[4] for r in rows)}
    _tally(counts, 'cards', 'pending started unknown create_acknowledged', (r[3] for r in rows))
    cards = {r[1] for r in rows}
    tokens = _rows(db, remaining, 'card_tokens', 'digest,delivery_id,action', 'epoch,page')
    _require(all(r[1] in cards and r[2] in ('Next', 'Previous', 'Refresh')
                 and type(r[3]) is int and 0 <= r[3] <= 1024
                 and type(r[4]) is int and 0 <= r[4] <= 65535 for r in tokens))
    windows = _rows(db, remaining, 'card_windows', 'delivery_id', 'issued,expires')
    _require(all(r[0] in cards and type(r[1]) is int and type(r[2]) is int
                 and 0 <= r[1] < r[2] < 10**17 and r[2] - r[1] <= 300000000
                 for r in windows))
    return tuple(sorted(counts.items()))


def _scan(path, before, owners, capacity, row_limit, budget_ms, *, card):
    db = None
    fingerprint, counts, reason = before, None, 'unreadable'
    deadline, steps = monotonic() + budget_ms / 1000, 0
    def progress():
        nonlocal steps
        steps += 1000
        return int(steps > 10_000_000 or monotonic() >= deadline)
    try:
        # Reject WAL before SQLite can open/create shared-memory sidecars.
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            _require(stat.S_ISREG(os.fstat(fd).st_mode), 'file')
            header = os.read(fd, 100)
        finally:
            os.close(fd)
        _require(header[:16] == b'SQLite format 3\x00' and header[18:20] == b'\x01\x01', 'schema')
        _require(_fingerprint(path) == before, 'changed')
        db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=0, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.set_progress_handler(progress, 1000)
        db.execute('PRAGMA query_only=ON')
        # Connection-local and before the first database read: if the path was
        # swapped to WAL after our header check, never create a shared wal-index.
        # Keep normal SQLite locking; immutable mode could ignore live journals.
        # https://sqlite.org/wal.html#use_of_wal_without_shared_memory
        db.execute('PRAGMA locking_mode=EXCLUSIVE')
        db.execute('BEGIN')
        version = db.execute('PRAGMA user_version').fetchone()[0]
        fingerprint = replace(before, schema_version=version)
        _require(version == (2 if card else 4), 'schema')
        # Bound the existing verifiers' fetchall() and SQL normalization work.
        schema = db.execute("SELECT type IN ('table','index'), "
            "length(CAST(name AS BLOB))<=1024, "
            "sql IS NULL OR length(CAST(sql AS BLOB))<=16384 FROM sqlite_master LIMIT 65").fetchall()
        _require(len(schema) <= 64 and all(all(r) for r in schema), 'schema')
        try:
            if card:
                meta = db.execute("SELECT typeof(id)='integer', typeof(highwater)='integer', "
                                  "typeof(capacity)='integer' FROM card_meta LIMIT 2").fetchall()
                _require(len(meta) == 1 and all(meta[0]), 'schema')
                CardLedger(path, capacity=capacity)._verify(db)
            else:
                BriefStore._inspect(db)
        except sqlite3.Error:
            raise
        except Exception:
            raise _Incomplete('schema') from None
        remaining = [row_limit]
        counts = (_card_counts(db, remaining, owners, capacity) if card
                  else _brief_counts(db, remaining, owners))
        _require(monotonic() < deadline, 'budget')
        reason = 'ok'
    except Exception as exc:
        reason = _reason(exc)
    finally:
        if db is not None:
            try:
                db.close()  # Ends the read transaction without any commit.
            except Exception:
                reason = 'unreadable'
    try:
        _require(_fingerprint(path) == before, 'changed')
    except Exception:
        reason = 'changed'
    return StoreInventory(reason == 'ok', reason, fingerprint, counts if reason == 'ok' else None)


def inspect_startup(*, brief_path: str | Path, card_path: str | Path, card_capacity: int,
                    owners: tuple[RequesterContext, ...] | list[RequesterContext],
                    boot_id: str, config_sha256: str, row_limit: int = 1000,
                    budget_ms: int = 250) -> StartupInventory:
    """Inspect existing stores; invalid arguments raise a fixed ValueError before IO.

    owners must be a nonempty list/tuple of up to 64 distinct RequesterContexts.
    The SHA256 binding covers boot, config, sorted owner scopes, capacity and scan
    limits. Each store counts at most row_limit total rows, including auxiliary
    relationship tables (and card_meta). Incomplete stores have counts=None.
    """
    try:
        for value, maximum in ((row_limit, 10000), (card_capacity, 10000), (budget_ms, 5000)):
            _require(type(value) is int and 1 <= value <= maximum)
        _require(type(boot_id) is str and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,255}', boot_id))
        _require(type(config_sha256) is str and re.fullmatch('[0-9a-f]{64}', config_sha256))
        _require(type(owners) in (list, tuple) and 1 <= len(owners) <= 64)
        scopes = tuple(sorted(_scope(owner) for owner in owners))
        _require(len(set(scopes)) == len(scopes))
        paths = []
        for path in (brief_path, card_path):
            _require(isinstance(path, (str, Path)) and 0 < len(str(path)) <= 4096
                     and str(path).strip() and '\x00' not in str(path) and str(path) != ':memory:')
            paths.append(Path(path).absolute())
        binding = hashlib.sha256(json.dumps(['startup-inventory-v1', boot_id, config_sha256,
            scopes, card_capacity, row_limit, budget_ms], separators=(',', ':')).encode()).hexdigest()
    except Exception:
        raise ValueError('invalid startup inventory arguments') from None
    probes = []
    for path in paths:
        try:
            probes.append(StoreInventory(False, 'ok', _fingerprint(path)))
        except Exception as exc:
            probes.append(StoreInventory(False, _reason(exc), None))
    a, b = (p.fingerprint for p in probes)
    if a and b and (a.device, a.inode) == (b.device, b.inode):
        probes = [replace(p, reason='alias') for p in probes]
    results = tuple(_scan(path, probe.fingerprint, set(scopes), card_capacity,
                         row_limit, budget_ms, card=bool(i)) if probe.reason == 'ok' else probe
                    for i, (path, probe) in enumerate(zip(paths, probes)))
    return StartupInventory(binding, *results)
