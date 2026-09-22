"""Bounded, trusted-local structural inventory of existing persistence domains.

Persisted facts never authorize work, recover jobs, resolve unknown effects, or
prove deployment drain. inspect_startup retains its Brief/Card-only contract;
inspect_runtime_startup covers all five stores, not runtime reconciliation.
Keep admission closed until future production reconciliation.
Each database has its own read transaction, NOT an
atomic global snapshot. Before/after file fingerprints detect observed changes;
they are neither freshness attestations nor a promise of ABA detection. SQLite
progress/deadline budgets bound query work, not arbitrary filesystem stalls.
Owners are trusted RequesterContexts, never endpoint/model-supplied authority.
Only hashes bind private boot/owner inputs; no identifiers or bodies are returned.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from functools import partial
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
from ostag.briefs.callbacks import CallbackLedger
from ostag.briefs import retention, revisions
from ostag.briefs.delivery import FeishuBinding
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


@dataclass(frozen=True)
class ScanBudget:
    row_limit: int = 1000
    budget_ms: int = 250


@dataclass(frozen=True)
class RuntimeInventoryConfig:
    """Explicit trusted configuration; defaults match the current ledger defaults.

    Brief schema4 is delivery-enabled; explicit schema5 adds effect custody.
    Neither is BriefStore's initial schema2.
    Versions other than these current schemas are invalid arguments, not migration
    requests. Each budget covers one store, including its metadata row.
    """
    brief_schema: int = 4
    card_schema: int = 2
    retention_schema: int = 1
    revision_schema: int = 1
    callback_schema: int = 1
    card_capacity: int = 1000
    retention_capacity: int = 10000
    revision_capacity: int = 1000
    callback_capacity: int = 1000
    revision_chain_limit: int = revisions.MAX_CHAIN
    revision_event_limit: int = revisions.MAX_EVENTS
    brief_budget: ScanBudget = ScanBudget()
    card_budget: ScanBudget = ScanBudget()
    retention_budget: ScanBudget = ScanBudget()
    revision_budget: ScanBudget = ScanBudget()
    callback_budget: ScanBudget = ScanBudget()


@dataclass(frozen=True)
class RuntimeStartupInventory:
    binding: str
    brief: StoreInventory
    card: StoreInventory
    retention: StoreInventory
    revision: StoreInventory
    callback: StoreInventory


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
    return _fetch(db, remaining, table, columns)


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
    if db.execute('PRAGMA user_version').fetchone()[0] == 5:
        meta = _rows(db, remaining, 'brief_effect_meta', 'epoch,ledger_epoch', 'id,highwater')
        _require(len(meta) == 1 and _digest_value(meta[0][0])
                 and (meta[0][1] == 'initial' or _digest_value(meta[0][1])) and meta[0][2] == 1
                 and type(meta[0][3]) is int and 0 <= meta[0][3] < 2**62)
        intents = _rows(db, remaining, 'brief_action_intents',
            'action_id,job_id,namespace,platform,actor_id,resource_key,kind,intent_digest,ledger_epoch',
            'created_us')
        _require(all(r[2:5] in owners for r in intents), 'owner')
        _require(all(jobs.get(r[1]) == r[2:5] and r[6] == 'brief.field.update'
                     and _digest_value(r[5]) and _digest_value(r[7])
                     and (r[8] == 'initial' or _digest_value(r[8])) and _time_value(r[9])
                     for r in intents))
        actions = {r[0] for r in intents}
        attempts = _rows(db, remaining, 'brief_effect_attempts',
            'attempt_id,action_id,owner_id,state,permit_digest', 'fence,issued_us,expires_us,begun_us')
        _require({r[1] for r in attempts} == actions and len(attempts) == len(actions))
        _require(all(_identifier_value(r[0]) and _identifier_value(r[2]) and _digest_value(r[4])
                     and type(r[5]) is int and 0 < r[5] <= meta[0][3]
                     and all(_time_value(t) for t in r[6:9]) and r[6] <= r[8] < r[7]
                     for r in attempts))
        _tally(counts, 'effects', 'begun unknown', (r[3] for r in attempts))
        counts['effects.intents'] = len(intents)
        attempt_ids = {r[0]: r[8] for r in attempts}
        evidence = _rows(db, remaining, 'brief_effect_observations',
                         'attempt_id,kind,evidence_sha256', 'ordinal,observed_us')
        _require(all(r[0] in attempt_ids and r[1] in
                     ('transport_response', 'transport_error', 'independent_readback')
                     and _digest_value(r[2]) and type(r[3]) is int and 1 <= r[3] <= 32
                     and _time_value(r[4]) and r[4] >= attempt_ids[r[0]] for r in evidence))
        counts['effects.observations'] = len(evidence)
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


def _scan(path, before, row_limit, budget_ms, *, version, verify, count):
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
        actual_version = db.execute('PRAGMA user_version').fetchone()[0]
        fingerprint = replace(before, schema_version=actual_version)
        _require(actual_version == version, 'schema')
        # Bound the existing verifiers' fetchall() and SQL normalization work.
        schema = db.execute("SELECT type IN ('table','index'), "
            "length(CAST(name AS BLOB))<=1024, "
            "sql IS NULL OR length(CAST(sql AS BLOB))<=16384 FROM sqlite_master LIMIT 65").fetchall()
        _require(len(schema) <= 64 and all(all(r) for r in schema), 'schema')
        try:
            verify(db)
        except sqlite3.Error as exc:
            if _reason(exc) in ('locked', 'budget'):
                raise
            raise _Incomplete('schema') from None
        except Exception:
            raise _Incomplete('schema') from None
        remaining = [row_limit]
        counts = count(db, remaining)
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


def _verify_card(db, path, capacity):
    meta = db.execute("SELECT typeof(id)='integer', typeof(highwater)='integer', "
                      "typeof(capacity)='integer' FROM card_meta LIMIT 2").fetchall()
    _require(len(meta) == 1 and all(meta[0]), 'schema')
    CardLedger(path, capacity=capacity)._verify(db)


def _probes(paths):
    probes = []
    for path in paths:
        try:
            probes.append(StoreInventory(False, 'ok', _fingerprint(path)))
        except Exception as exc:
            probes.append(StoreInventory(False, _reason(exc), None))
    for i, a in enumerate(probes):
        for j in range(i):
            b = probes[j]
            if (a.fingerprint and b.fingerprint and
                    (a.fingerprint.device, a.fingerprint.inode) ==
                    (b.fingerprint.device, b.fingerprint.inode)):
                probes[i] = replace(probes[i], reason='alias')
                probes[j] = replace(probes[j], reason='alias')
    return probes


def _absolute_paths(paths):
    # getcwd can fail (for example after directory removal). Never expose paths
    # through that OS exception, including its exception context.
    try:
        return [p.absolute() for p in paths]
    except Exception:
        pass
    raise ValueError('invalid startup inventory arguments')


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
    valid = False
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
            paths.append(Path(path))
        binding = hashlib.sha256(json.dumps(['startup-inventory-v1', boot_id, config_sha256,
            scopes, card_capacity, row_limit, budget_ms], separators=(',', ':')).encode()).hexdigest()
        valid = True
    except Exception:
        pass
    if not valid:
        raise ValueError('invalid startup inventory arguments')
    paths = _absolute_paths(paths)
    probes = _probes(paths)
    results = tuple(_scan(path, probe.fingerprint, row_limit, budget_ms,
                         version=2 if i else 4,
                         verify=(lambda db: _verify_card(db, path, card_capacity)) if i
                         else BriefStore._inspect,
                         count=(lambda db, remaining: _card_counts(
                             db, remaining, set(scopes), card_capacity)) if i else
                         (lambda db, remaining: _brief_counts(db, remaining, set(scopes))))
                    if probe.reason == 'ok' else probe
                    for i, (path, probe) in enumerate(zip(paths, probes)))
    return StartupInventory(binding, *results)


def _bounded_text(expression, maximum=1024):
    return (f"CASE WHEN typeof({expression})='text' AND "
            f"length(CAST({expression} AS BLOB)) BETWEEN 1 AND {maximum} "
            f"THEN {expression} END")


def _fetch(db, remaining, table, columns, nullable=()):
    rows = [tuple(r) for r in db.execute(
        f"SELECT {','.join(columns)} FROM {table} LIMIT ?", (remaining[0] + 1,))]
    remaining[0] -= len(rows)
    _require(remaining[0] >= 0, 'overflow')
    _require(all(v is not None or i in nullable for r in rows for i, v in enumerate(r)))
    return rows


def _digest_value(value):
    return type(value) is str and re.fullmatch('[0-9a-f]{64}', value) is not None


def _time_value(value):
    return type(value) is int and 0 <= value < 10**17


def _identifier_value(value):
    return (type(value) is str and
            re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/@-]{0,255}', value) is not None
            and '://' not in value)


def _canonical_object(raw):
    try:
        value = json.loads(raw)
        _require(type(value) is dict and retention._json(value) == raw)
        return value
    except Exception:
        raise _Incomplete('malformed') from None


def _added_meta(db, kind, config):
    columns = ['id', 'highwater', 'capacity']
    expected = [1, None, getattr(config, kind + '_capacity')]
    if kind == 'revision':
        columns += ['chain_limit', 'event_limit']
        expected += [config.revision_chain_limit, config.revision_event_limit]
    # BEFORE any verifier can fetch metadata, exclude oversized blobs/text and
    # duplicate rows. No potentially private value reaches Python unbounded.
    types = db.execute('SELECT ' + ','.join(f"typeof({c})='integer'" for c in columns)
                       + f' FROM {kind}_meta LIMIT 2').fetchall()
    _require(len(types) == 1 and all(types[0]), 'schema')
    row = tuple(db.execute('SELECT ' + ','.join(columns) + f' FROM {kind}_meta LIMIT 1')
                .fetchone())
    expected[1] = row[1]
    _require(row == tuple(expected) and _time_value(row[1]), 'schema')
    return row[1]


def _verify_added(db, kind, path, config):
    if kind == 'callback':
        # Same exact schema contract as the ledger; preflight in _scan bounds
        # sqlite_master, and metadata preflight bounds its capacity fetch.
        _added_meta(db, kind, config)
        CallbackLedger(path, capacity=config.callback_capacity)._verify(db)
        return
    module = retention if kind == 'retention' else revisions
    actual = [tuple(r) for r in db.execute(
        "SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_autoindex_%' "
        'ORDER BY name')]
    expected = sorted([('table', s.split()[2], s) for s in module._SCHEMA], key=lambda r: r[1])
    _require(actual == expected and db.execute('PRAGMA application_id').fetchone()[0]
             == module._APP and db.execute('PRAGMA journal_mode').fetchone()[0] == 'delete',
             'schema')
    _added_meta(db, kind, config)
    # Do not instantiate RetentionLedger (filesystem pinning) or invoke Revision's
    # quick_check: neither is needed for bounded structural inventory. Capacity
    # is verified against the bounded rows below, not an unbounded count scan.


def _retention_counts(db, remaining, owners, config, observed_us):
    observed_us = max(observed_us, _added_meta(db, 'retention', config))
    _rows(db, remaining, 'retention_meta', '', 'id')
    scopes = {retention._scope(owner) for owner in owners}
    jobs = _fetch(db, remaining, 'retention_jobs', [
        *(_bounded_text(c) for c in ('scope', 'job', 'binding', 'decision')),
        "CASE WHEN typeof(expires)='integer' THEN expires END",
        "CASE WHEN revision IS NULL THEN '' ELSE " + _bounded_text('revision') + ' END',
        "CASE WHEN delivery IS NULL THEN '' ELSE " + _bounded_text('delivery') + ' END'])
    job_map = {r[:2]: r for r in jobs}
    sources = _rows(db, remaining, 'retention_sources', 'scope,snapshot,resource,grant_key',
                    'expires,expired')
    links = _rows(db, remaining, 'retention_links', 'scope,job,snapshot,grant_key')
    revokes = _rows(db, remaining, 'retention_revokes', 'scope,kind,identity')
    targets = _rows(db, remaining, 'retention_targets', 'scope,job,kind,identity', 'done')
    all_rows = jobs + sources + links + revokes + targets
    _require(all(r[0] in scopes for r in all_rows), 'owner')
    _require(len(all_rows) <= config.retention_capacity)
    _require(all(_identifier_value(r[1]) and _digest_value(r[2]) and
                 r[3] in ('authorized', 'expired', 'revoked') and _time_value(r[4])
                 and (r[5] == '') == (r[6] == '')
                 and all(v == '' or _identifier_value(v) for v in r[5:]) for r in jobs))
    _require(all(all(_digest_value(v) for v in r[1:4]) and _time_value(r[4])
                 and r[5] in (0, 1) for r in sources))
    source_keys = {(r[0], r[1], r[3]) for r in sources}
    _require(all(r[:2] in job_map and (r[0], r[2], r[3]) in source_keys for r in links))
    _require({r[:2] for r in links} == job_map.keys())
    _require({(r[0], r[2], r[3]) for r in links} == source_keys)
    _require(all(r[1] in ('resource', 'grant') and _digest_value(r[2]) for r in revokes))
    _require(all(r[:2] in job_map and r[2] in ('primary', 'cards', 'callbacks', 'revisions')
                 and _digest_value(r[3]) and r[4] in (0, 1) for r in targets))
    _require({r[:2] for r in targets if r[2] == 'primary'} == job_map.keys())
    _require(all(not r[4] or job_map[r[:2]][3] != 'authorized' for r in targets))
    counts = {}
    _tally(counts, 'jobs', 'authorized expired revoked', (r[3] for r in jobs))
    counts['jobs.retained'] = sum(r[3] == 'authorized' and r[4] > observed_us for r in jobs)
    counts['jobs.elapsed'] = sum(r[4] <= observed_us for r in jobs)
    _tally(counts, 'sources', 'retained expired',
           ('expired' if r[5] or r[4] <= observed_us else 'retained' for r in sources))
    _tally(counts, 'revocations', 'resource grant', (r[1] for r in revokes))
    # done is a persisted receipt, not verification that target cleanup succeeded.
    _tally(counts, 'targets', 'pending done', ('done' if r[4] else 'pending' for r in targets))
    return tuple(sorted(counts.items()))


def _json_projection(column, maximum, fields, expected):
    # SQLite validates size BEFORE parsing JSON; only the selected, bounded
    # structural fields cross into Python. Drafts, excerpts and correction text
    # are never deserialized here. Unknown deep semantics remain a limitation.
    values = ','.join(f"'{key}',json_extract({column},'$.{key}')" for key in fields)
    types = ','.join(f"'{key}',json_type({column},'$.{key}')"
                     for key in sorted(set(fields) | set(expected)))
    keys = ','.join(f"'{key}'" for key in expected)
    shape = (f"(SELECT count(*)={len(expected)} AND count(DISTINCT key)={len(expected)} "
             f"AND sum(key NOT IN ({keys}))=0 FROM json_each({column}))")
    text_bytes = f"length(CAST(json_extract({column},'$.text') AS BLOB))"
    owner_field = 'scope' if column == 'snapshot' else 'actor'
    # Concatenation strips SQLite's JSON subtype, preserving the owner encoding
    # as a string. Compare canonical bytes so duplicate owner keys cannot hide
    # behind Python's last-key-wins object decoding.
    owner = f"json_extract({column},'$.{owner_field}') || ''"
    projection = (f"json_object({values},'__types',json_object({types}),'__shape',{shape},"
                  f"'__text_bytes',{text_bytes if column == 'record' else 'NULL'},"
                  f"'__owner',{owner})")
    return (f"CASE WHEN typeof({column})='text' AND "
            f"length(CAST({column} AS BLOB)) BETWEEN 1 AND {maximum} THEN "
            f"CASE WHEN json_valid({column}) THEN {_bounded_text(projection, 8192)} END END")


def _revision_counts(db, remaining, owners, config):
    highwater = _added_meta(db, 'revision', config)
    _rows(db, remaining, 'revision_meta', '', 'id')
    scopes = {revisions._scope(owner): owner for owner in owners}
    roots = _fetch(db, remaining, 'revision_roots', [
        *(_bounded_text(c) for c in ('scope', 'job', 'digest')),
        "CASE WHEN typeof(timestamp_us)='integer' THEN timestamp_us END",
        _json_projection('snapshot', revisions.MAX_ROOT_BYTES,
                         ('retired', 'scope', 'job', 'revision', 'state', 'binding', 'outbox'),
                         ('scope', 'job', 'revision', 'destination', 'binding', 'sources',
                          'draft', 'state', 'outbox')),
        "snapshot='" + retention._RETIRED + "'"])
    events = _fetch(db, remaining, 'revision_events', [
        *(_bounded_text(c) for c in ('scope', 'job', 'request', 'digest')),
        "CASE WHEN parent IS NULL THEN '' ELSE " + _bounded_text('parent') + ' END',
        _json_projection('record', 65536, ('retired', 'operation', 'actor', 'timestamp_us',
            'ordinal', 'request_id', 'parent_revision_id', 'event_id', 'revision_id',
            'expected_revision_id'), tuple(revisions.RevisionEvent.__dataclass_fields__) + ('ordinal',)),
        "record='" + retention._RETIRED + "'"])
    _require(len(roots) + len(events) <= config.revision_capacity)
    _require(all(r[0] in scopes for r in roots + events), 'owner')
    root_map = {}
    for scope, job, digest, stamp, raw, retired in roots:
        _require(revisions._ID.fullmatch(job) and _digest_value(digest)
                 and _time_value(stamp) and stamp <= highwater)
        value = json.loads(raw)
        if not retired:
            _require(value['retired'] is None and value['__owner'] == scope, 'owner')
            _require(value['__shape'] == 1 and value['job'] == job
                     and value['state'] == 'draft_ready'
                     and all(value['__types'][k] == 'object' for k in ('sources', 'draft'))
                     and all(type(value[k]) is str and revisions._ID.fullmatch(value[k])
                             for k in ('revision', 'outbox')))
            try:
                _require(type(value['binding']) is dict)
                FeishuBinding(**value['binding'])
            except Exception:
                raise _Incomplete('malformed') from None
        root_map[scope, job] = (stamp, retired, value.get('revision'))
    per_job, corrections, ordinals, identities, references = {}, {}, {}, {}, []
    revision_ids = {key: {root[2]} for key, root in root_map.items() if not root[1]}
    for scope, job, request, digest, parent, raw, retired in events:
        key = (scope, job)
        _require(key in root_map and revisions._ID.fullmatch(request) and _digest_value(digest))
        _require(parent == '' or revisions._ID.fullmatch(parent))
        per_job[key] = per_job.get(key, 0) + 1
        _require(per_job[key] <= config.revision_event_limit and retired == root_map[key][1])
        if retired:
            continue
        value = json.loads(raw)
        _require(value['__owner'] == retention._json(scopes[scope].principal.to_dict()), 'owner')
        _require(value['__shape'] == 1 and value['retired'] is None
                 and value['__types']['timestamp_us'] == 'integer'
                 and value['__types']['ordinal'] == 'integer'
                 and value['__types']['parent_revision_id'] in ('null', 'text')
                 and value['operation'] in ('correct', 'adopt', 'withdraw')
                 and value['request_id'] == request and (value['parent_revision_id'] or '') == parent
                 and _time_value(value['timestamp_us'])
                 and root_map[key][0] <= value['timestamp_us'] <= highwater)
        ordinal = value['ordinal']
        _require(type(ordinal) is int and 0 <= ordinal < config.revision_event_limit)
        seen = ordinals.setdefault(key, set())
        _require(ordinal not in seen)
        seen.add(ordinal)
        _require(all(type(value[k]) is str and revisions._ID.fullmatch(value[k])
                     for k in ('event_id', 'revision_id', 'expected_revision_id')))
        _require(re.fullmatch(r're_[0-9a-f]{48}', value['event_id']))
        seen_ids = identities.setdefault(key, set())
        _require(value['event_id'] not in seen_ids)
        seen_ids.add(value['event_id'])
        references.append((key, value['expected_revision_id']))
        if value['operation'] == 'correct':
            _require(parent == value['expected_revision_id'] and
                     parent != value['revision_id'] and
                     re.fullmatch(r'rv_[0-9a-f]{48}', value['revision_id']) and
                     value['__types']['text'] == 'text' and
                     1 <= value['__text_bytes'] <= revisions.MAX_CORRECTION_BYTES and
                     all(value['__types'][k] in ('null', 'text') for k in ('section', 'item_id')))
            corrections[key] = corrections.get(key, 0) + 1
            _require(corrections[key] <= config.revision_chain_limit)
            _require(value['revision_id'] not in revision_ids[key])
            revision_ids[key].add(value['revision_id'])
        else:
            _require(parent == '' and value['revision_id'] == value['expected_revision_id']
                     and all(value['__types'][k] == 'null' for k in ('text', 'section', 'item_id')))
    _require(all(v == set(range(len(v))) for v in ordinals.values()))
    _require(all(identifier in revision_ids[key] for key, identifier in references))
    return tuple(sorted({'roots': len(roots), 'events': len(events),
                         'roots.tombstones': sum(r[-1] for r in roots),
                         'events.tombstones': sum(r[-1] for r in events)}.items()))


def _callback_counts(db, remaining, owners, config, observed_us):
    highwater = _added_meta(db, 'callback', config)
    _rows(db, remaining, 'callback_meta', '', 'id')
    scopes = {_scope(owner) for owner in owners}
    rows = _fetch(db, remaining, 'brief_callbacks', [
        _bounded_text('digest'), _bounded_text('binding', 8192),
        *(f"CASE WHEN typeof({c})='integer' THEN {c} END" for c in ('issued', 'expires', 'spent'))])
    _require(len(rows) <= config.callback_capacity)
    counts = dict.fromkeys(('callbacks.live_unspent', 'callbacks.expired_unspent',
                            'callbacks.spent', 'callbacks.tombstones'), 0)
    for digest, raw, issued, expires, spent in rows:
        _require(_digest_value(digest) and _time_value(issued) and _time_value(expires)
                 and issued <= highwater and issued < expires and expires - issued <= 300000000
                 and spent in (0, 1))
        key = _canonical_object(raw)
        owner = tuple(key.get(k) for k in ('namespace', 'platform', 'actor'))
        _require(all(type(v) is str for v in owner) and owner in scopes, 'owner')
        _require(type(key.get('job')) is str and revisions._ID.fullmatch(key['job']))
        if 'retired' in key:
            _require(set(key) == {'namespace', 'platform', 'actor', 'job', 'retired'}
                     and key['retired'] is True and spent == 1)
            category = 'tombstones'
        else:
            _require(set(key) == {'namespace', 'platform', 'actor', 'job', 'revision',
                'destination', 'binding', 'purpose', 'action', 'page', 'message'})
            _require(type(key['revision']) is str and revisions._ID.fullmatch(key['revision'])
                     and type(key['destination']) is str
                     and re.fullmatch(r'oc_[A-Za-z0-9_-]{1,250}', key['destination'])
                     and _identifier_value(key['purpose'])
                     and key['action'] in ('preview', 'refresh') and type(key['page']) is int
                     and 0 <= key['page'] < 65536 and type(key['message']) is str
                     and re.fullmatch(r'[A-Za-z0-9_-]{8,256}', key['message']))
            try:
                _require(type(key['binding']) is dict)
                FeishuBinding(**key['binding'])
            except Exception:
                raise _Incomplete('malformed') from None
            category = ('spent' if spent else 'expired_unspent'
                        if expires <= max(observed_us, highwater) else 'live_unspent')
        counts['callbacks.' + category] += 1
    return tuple(sorted(counts.items()))


def inspect_runtime_startup(*, brief_path: str | Path, card_path: str | Path,
                            retention_path: str | Path, revision_path: str | Path,
                            callback_path: str | Path, config: RuntimeInventoryConfig,
                            owners: tuple[RequesterContext, ...] | list[RequesterContext],
                            boot_id: str, config_sha256: str,
                            observed_us: int) -> RuntimeStartupInventory:
    """Read-only structural facts, never reconciliation or permission to operate.

    All five paths, trusted configuration/owners, boot binding and observation time
    are required. The versioned hash binds all inputs, including paths (hash only).
    Unknown stores have counts=None; valid zero counts mean a completed bounded scan.

    Retention decisions/target receipts are persisted facts. Retained means only
    locally unexpired, not authorized: revocation propagation, source deadlines,
    target identities/cleanup receipts and cross-store joins are NOT reconciled.
    Callback live_unspent means unexpired relative to max(observed_us, highwater),
    never an in-flight operation. Revision bodies are projected to bounded metadata;
    content hashes, draft semantics, full history/parent chains and adoption validity
    are not verified. Counts cannot resolve historical effects or establish rights.
    Per-store read transactions are not an atomic cross-database snapshot. SQL work
    and returned rows/fields are bounded; filesystem latency is not guaranteed.
    """
    kinds = ('brief', 'card', 'retention', 'revision', 'callback')
    valid = False
    try:
        _require(type(config) is RuntimeInventoryConfig)
        for kind, version in zip(kinds, (4, 2, 1, 1, 1)):
            value = getattr(config, kind + '_schema')
            _require(type(value) is int and (value in (4, 5) if kind == 'brief' else value == version))
            budget = getattr(config, kind + '_budget')
            _require(type(budget) is ScanBudget)
            for value, maximum in ((budget.row_limit, 10000), (budget.budget_ms, 5000)):
                _require(type(value) is int and 1 <= value <= maximum)
        for value, maximum in ((config.card_capacity, 10000),
                (config.retention_capacity, 100000), (config.revision_capacity, revisions.MAX_ROWS),
                (config.callback_capacity, 10000), (config.revision_chain_limit, revisions.MAX_CHAIN),
                (config.revision_event_limit, revisions.MAX_EVENTS)):
            _require(type(value) is int and 1 <= value <= maximum)
        _require(_time_value(observed_us))
        _require(type(boot_id) is str and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,255}', boot_id))
        _require(type(config_sha256) is str and re.fullmatch('[0-9a-f]{64}', config_sha256))
        _require(type(owners) in (list, tuple) and 1 <= len(owners) <= 64)
        owners = tuple(owners)
        scopes = tuple(sorted(_scope(owner) for owner in owners))
        _require(len(set(scopes)) == len(scopes))
        # Validate every domain's scope contract before any filesystem operation.
        for owner in owners:
            retention._scope(owner)
            revisions._scope(owner)
        paths = []
        for path in (brief_path, card_path, retention_path, revision_path, callback_path):
            _require(isinstance(path, (str, Path)) and 0 < len(str(path)) <= 4096
                     and str(path).strip() and '\x00' not in str(path) and str(path) != ':memory:')
            paths.append(Path(path))
        # Even relative-path absolutization (getcwd) follows complete validation.
        valid = True
    except Exception:
        pass
    if not valid:
        raise ValueError('invalid startup inventory arguments')
    paths = _absolute_paths(paths)
    binding = hashlib.sha256(json.dumps(['runtime-startup-inventory-v1', boot_id, config_sha256,
        scopes, asdict(config), observed_us, [str(p) for p in paths]],
        sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    probes = _probes(paths)
    results = []
    for kind, path, probe in zip(kinds, paths, probes):
        if probe.reason != 'ok':
            results.append(probe)
            continue
        if kind == 'brief':
            verify = BriefStore._inspect
            count = partial(_brief_counts, owners=set(scopes))
        elif kind == 'card':
            verify = partial(_verify_card, path=path, capacity=config.card_capacity)
            count = partial(_card_counts, owners=set(scopes), capacity=config.card_capacity)
        else:
            verify = partial(_verify_added, kind=kind, path=path, config=config)
            if kind == 'retention':
                count = partial(_retention_counts, owners=owners, config=config, observed_us=observed_us)
            elif kind == 'revision':
                count = partial(_revision_counts, owners=owners, config=config)
            else:
                count = partial(_callback_counts, owners=owners, config=config, observed_us=observed_us)
        budget = getattr(config, kind + '_budget')
        results.append(_scan(path, probe.fingerprint, budget.row_limit, budget.budget_ms,
                             version=getattr(config, kind + '_schema'), verify=verify, count=count))
    return RuntimeStartupInventory(binding, *results)
