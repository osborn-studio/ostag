"""Read-only startup inventory contracts using actual owned SQLite databases."""
from dataclasses import FrozenInstanceError, asdict
import json
import os
from pathlib import Path
import sqlite3

import pytest

from ostag.briefs import startup_inventory as inventory
from ostag.briefs.card_delivery import CardLedger
from ostag.briefs.store import BriefStore, RequesterContext
from ostag.core.identity import Principal
from test_brief_store import SCOPE, admit, at

SECRET = 'PRIVATE_SENTINEL_9f37'


@pytest.fixture
def stores(tmp_path):
    brief = BriefStore(tmp_path / 'brief.db')
    brief.enable_delivery()
    card = CardLedger(tmp_path / 'card.db', capacity=20)
    card.initialize()
    return brief, card


def inspect(stores, **changes):
    brief, card = stores
    return inventory.inspect_startup(**(dict(brief_path=brief.path, card_path=card.path,
        card_capacity=20, owners=(SCOPE,), boot_id='boot-1', config_sha256='a' * 64)
        | changes))


def execute(path, sql, args=()):
    with sqlite3.connect(path) as db:
        db.execute(sql, args)


def snapshot(directory):
    return {p.name: (p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_ctime_ns)
            for p in directory.iterdir() if p.is_file()}


def assert_unknown(result, reason=None):
    assert result.complete is False and result.counts is None
    if reason:
        assert result.reason == reason


def test_current_empty_immutable_bound_and_byte_identical(stores):
    before = snapshot(stores[0].path.parent)
    report = inspect(stores)
    assert report.brief.complete and report.card.complete
    assert report.brief.reason == report.card.reason == 'ok'
    assert not any(dict(report.brief.counts).values())
    assert not any(dict(report.card.counts).values())
    assert report.brief.fingerprint.schema_version == 4
    assert report.card.fingerprint.schema_version == 2
    for field, value in [('device', 'st_dev'), ('inode', 'st_ino'), ('size', 'st_size'),
                         ('mtime_ns', 'st_mtime_ns'), ('ctime_ns', 'st_ctime_ns')]:
        assert getattr(report.brief.fingerprint, field) == getattr(stores[0].path.stat(), value)
    assert snapshot(stores[0].path.parent) == before
    assert inspect(stores) == report
    assert inspect(stores, boot_id='boot-2').binding != report.binding
    assert inspect(stores, config_sha256='b' * 64).binding != report.binding
    assert inspect(stores, owners=(RequesterContext('other', SCOPE.principal),)).binding != report.binding
    with pytest.raises(FrozenInstanceError):
        report.brief.complete = False
    assert not hasattr(report, 'drained')


def populate(brief, card):
    jobs = []
    for i, state in enumerate(('queued', 'collecting', 'generating', 'draft_ready',
                                'needs_input', 'failed', 'cancelled')):
        job = admit(brief, key=f'key{i}', payload={'private': SECRET})
        jobs.append(job.job_id)
        execute(brief.path, 'UPDATE brief_jobs SET state=?,owner_id=?,lease_us=1 WHERE job_id=?',
                (state, SECRET, job.job_id))
    with sqlite3.connect(brief.path) as db:
        for i, state in enumerate(('reserved', 'started', 'unknown', 'settled', 'released')):
            db.execute('INSERT INTO brief_cost_profiles VALUES(?,?)', (jobs[i], SECRET))
            db.execute('INSERT INTO brief_cost_calls VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (f'reservation{i}', jobs[i], SECRET, 1, SECRET, '2026-09-20', 1,
                 state, SECRET, SECRET, 'a'*64, 100, 1, 1))
        for i, state in enumerate(('pending', 'started', 'unknown', 'provider_accepted')):
            db.execute('INSERT INTO brief_revisions VALUES(?,?,?,?,?,?)',
                       (f'r{i}', jobs[i], SECRET, SECRET, 'v1', 1))
            db.execute('INSERT INTO brief_outbox VALUES(?,?,?,?,?)',
                       (f'd{i}', jobs[i], f'r{i}', SECRET, 'pending'))
            db.execute('INSERT INTO brief_deliveries '
                '(delivery_id,job_id,revision_id,namespace,platform,actor_id,intent_json,'
                'intent_digest,state,created_us,updated_us) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                (f'd{i}', jobs[i], f'r{i}', SCOPE.namespace, SCOPE.principal.platform,
                 SCOPE.principal.actor_id, SECRET, SECRET, state, 1, 1))
        db.execute('INSERT INTO brief_delivery_reads VALUES(?,?,?,?,?,?)',
                   ('d2', 1, SECRET, SECRET, SECRET, 0))
        db.execute('INSERT INTO brief_delivery_reads VALUES(?,?,?,?,?,?)',
                   ('d2', 2, SECRET, SECRET+'2', SECRET, 1))
        db.execute('INSERT INTO brief_delivery_observations VALUES(?,?,?)', ('d2', 2, SECRET))
    with sqlite3.connect(card.path) as db:
        for i, state in enumerate(('pending', 'started', 'unknown', 'create_acknowledged')):
            db.execute('INSERT INTO cards VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (f'logical{i}', f'card{i}', json.dumps([SCOPE.namespace, 'feishu',
                  SCOPE.principal.actor_id], separators=(',', ':')), SECRET, SECRET, SECRET,
                 SECRET.encode(), SECRET, 0, 1, state, 1, None, None, SECRET,
                 None, None, None, 0, 0, None))
        db.execute('INSERT INTO card_tokens VALUES(?,?,?,?,?)', (SECRET, 'card2', 0, 'Next', 1))
        db.execute('INSERT INTO card_windows VALUES(?,?,?)', ('card2', 0, 1))
    return jobs


def test_all_states_custody_cancellation_and_private_bodies(stores):
    populate(*stores)
    before = snapshot(stores[0].path.parent)
    report = inspect(stores, boot_id=SECRET)
    assert report.brief.complete and report.card.complete
    brief, card = dict(report.brief.counts), dict(report.card.counts)
    for state in ('queued', 'collecting', 'generating', 'draft_ready', 'needs_input', 'failed', 'cancelled'):
        assert brief['jobs.' + state] == 1
    assert brief['jobs.custody'] == 7
    for state in ('reserved', 'started', 'unknown', 'settled', 'released'):
        assert brief['costs.' + state] == 1
    assert brief['outbox.pending'] == 4  # Includes the acknowledged delivery.
    for state in ('pending', 'started', 'unknown', 'provider_accepted'):
        assert brief['deliveries.' + state] == 1
    assert brief['reads.unsettled'] == brief['reads.settled'] == 1
    for state in ('pending', 'started', 'unknown', 'create_acknowledged'):
        assert card['cards.' + state] == 1
    assert card['cards.cancelled'] == 4
    encoded = repr(report) + json.dumps(asdict(report))
    for secret in (SECRET, str(stores[0].path), SCOPE.principal.actor_id, 'logical0', 'reservation0'):
        assert secret not in encoded
    assert snapshot(stores[0].path.parent) == before


@pytest.mark.parametrize('which', [0, 1])
@pytest.mark.parametrize('version', [0, 1, 3, 99])
def test_wrong_schema_untouched(stores, which, version):
    execute(stores[which].path, f'PRAGMA user_version={version}')
    before = snapshot(stores[0].path.parent)
    result = inspect(stores)
    assert_unknown((result.brief, result.card)[which], 'schema')
    assert snapshot(stores[0].path.parent) == before


def test_missing_files_stay_absent(tmp_path):
    report = inventory.inspect_startup(brief_path=tmp_path/'absent1', card_path=tmp_path/'absent2',
        card_capacity=20, owners=(SCOPE,), boot_id='boot', config_sha256='a'*64)
    assert_unknown(report.brief, 'missing')
    assert_unknown(report.card, 'missing')
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize('which', [0, 1])
def test_exclusive_lock_is_unknown(stores, which):
    with sqlite3.connect(stores[which].path) as db:
        db.execute('BEGIN EXCLUSIVE')
        report = inspect(stores)
        assert_unknown((report.brief, report.card)[which], 'locked')


def test_total_row_budget_not_per_table(stores):
    populate(*stores)
    report = inspect(stores, row_limit=10)
    assert_unknown(report.brief, 'overflow')
    assert report.card.complete  # 4 cards + meta + token + window.
    assert_unknown(inspect(stores, row_limit=6).card, 'overflow')


@pytest.mark.parametrize('which,sql', [
    (0, "UPDATE brief_jobs SET actor_id='foreign'"),
    (0, "UPDATE brief_deliveries SET actor_id='foreign'"),
    (1, "UPDATE cards SET scope='[\"foreign\",\"feishu\",\"actor\"]'"),
])
def test_foreign_owner(stores, which, sql):
    populate(*stores)
    execute(stores[which].path, sql)
    report = inspect(stores)
    assert_unknown((report.brief, report.card)[which], 'owner')


@pytest.mark.parametrize('which', [0, 1])
@pytest.mark.parametrize('sql', [
    'CREATE VIEW foreign_view AS SELECT 1',
    'CREATE TRIGGER foreign_trigger AFTER INSERT ON {table} BEGIN SELECT 1; END',
    'PRAGMA application_id=1',
])
def test_schema_tampering(stores, which, sql):
    execute(stores[which].path, sql.format(table=('brief_jobs', 'cards')[which]))
    result = inspect(stores)
    assert_unknown((result.brief, result.card)[which], 'schema')


@pytest.mark.parametrize('which,sql', [
    (0, "UPDATE brief_cost_calls SET job_id='orphan'"),
    (0, "UPDATE brief_revisions SET job_id='orphan'"),
    (0, "UPDATE brief_outbox SET revision_id='orphan'"),
    (0, "UPDATE brief_deliveries SET job_id='orphan'"),
    (0, "UPDATE brief_delivery_reads SET delivery_id='orphan'"),
    (0, "UPDATE brief_delivery_observations SET delivery_id='orphan'"),
    (1, "UPDATE card_tokens SET delivery_id='orphan'"),
    (1, "UPDATE card_windows SET delivery_id='orphan'"),
])
def test_orphans(stores, which, sql):
    populate(*stores)
    table = sql.split()[1]
    execute(stores[which].path, sql + f' WHERE rowid=(SELECT min(rowid) FROM {table})')
    result = inspect(stores)
    assert_unknown((result.brief, result.card)[which], 'malformed')


@pytest.mark.parametrize('which,table', [(0, 'brief_jobs'), (0, 'brief_cost_calls'),
                                         (0, 'brief_outbox'), (0, 'brief_deliveries'), (1, 'cards')])
def test_malformed_state(stores, which, table):
    populate(*stores)
    with sqlite3.connect(stores[which].path) as db:
        db.execute('PRAGMA ignore_check_constraints=ON')
        db.execute(f'UPDATE {table} SET state=?', (SECRET,))
    result = inspect(stores)
    assert_unknown((result.brief, result.card)[which], 'malformed')
    assert SECRET not in repr(result)


@pytest.mark.parametrize('kind', ['same', 'hardlink', 'symlink', 'directory'])
def test_alias_and_nonregular_paths(stores, tmp_path, kind):
    path = tmp_path / 'alias'
    if kind == 'hardlink':
        os.link(stores[0].path, path)
    elif kind == 'symlink':
        path.symlink_to(stores[1].path)
    elif kind == 'directory':
        path.mkdir()
    else:
        path = stores[0].path
    result = inspect(stores, card_path=path)
    assert_unknown(result.card, 'alias' if kind in ('same', 'hardlink') else 'file')
    if kind in ('same', 'hardlink'):
        assert_unknown(result.brief, 'alias')


def test_replacement_during_scan(stores, monkeypatch, tmp_path):
    replacement = tmp_path / 'replacement'
    replacement.write_bytes(stores[0].path.read_bytes())
    original = BriefStore._inspect
    def replace_file(db, **kwargs):
        result = original(db, **kwargs)
        replacement.replace(stores[0].path)
        return result
    monkeypatch.setattr(BriefStore, '_inspect', replace_file)
    assert_unknown(inspect(stores).brief, 'changed')


@pytest.mark.parametrize('changes', [
    {'row_limit': 0}, {'row_limit': 10001}, {'row_limit': True}, {'row_limit': 1.5},
    {'budget_ms': 0}, {'budget_ms': 5001}, {'budget_ms': True},
    {'boot_id': ''}, {'boot_id': 'x'*257}, {'boot_id': object()},
    {'config_sha256': SECRET}, {'config_sha256': 'z'*64},
    {'owners': ()}, {'owners': (SCOPE,)*65}, {'owners': (object(),)},
    {'owners': iter((SCOPE,))}, {'owners': (SCOPE, SCOPE)},
    {'card_capacity': 0}, {'card_capacity': True}, {'brief_path': ''},
    {'card_path': ':memory:'}, {'brief_path': 'bad\x00path'},
])
def test_invalid_arguments_before_io(stores, monkeypatch, changes):
    def forbidden(*args, **kwargs):
        pytest.fail('I/O before argument validation')
    monkeypatch.setattr(sqlite3, 'connect', forbidden)
    monkeypatch.setattr(os, 'open', forbidden)
    monkeypatch.setattr(Path, 'lstat', forbidden)
    with pytest.raises(ValueError, match='^invalid startup inventory arguments$') as exc:
        inspect(stores, **changes)
    assert SECRET not in repr(exc.value)


def test_errors_are_sanitized(stores, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError(SECRET)
    monkeypatch.setattr(sqlite3, 'connect', fail)
    result = inspect(stores)
    assert_unknown(result.brief)
    assert_unknown(result.card)
    assert SECRET not in repr(result)


def test_real_cancellation_preserves_unknown_effects(stores):
    jobs = populate(*stores)
    execute(stores[1].path, 'UPDATE cards SET cancelled=0')
    stores[0].cancel(SCOPE, jobs[2], clock=lambda: at(20))
    stores[1].cancel(SCOPE, 'card2', clock=lambda: 20)
    report = inspect(stores)
    assert report.brief.complete and report.card.complete
    assert dict(report.brief.counts)['costs.unknown'] == 1
    assert dict(report.brief.counts)['deliveries.unknown'] == 1
    assert dict(report.brief.counts)['reads.unsettled'] == 1
    assert dict(report.card.counts)['cards.unknown'] == 1
    assert dict(report.card.counts)['cards.cancelled'] == 1


def test_actual_older_schemas_remain_untouched(tmp_path):
    brief = BriefStore(tmp_path / 'old-brief.db')  # Normal default initializes schema2.
    card = CardLedger(tmp_path / 'old-card.db', capacity=20)
    card.initialize()
    with sqlite3.connect(card.path) as db:
        db.execute('DROP TABLE card_windows')
        db.execute('PRAGMA user_version=1')
        db.row_factory = sqlite3.Row
        card._verify(db, version=1)
    before = snapshot(tmp_path)
    result = inspect((brief, card))
    assert_unknown(result.brief, 'schema')
    assert_unknown(result.card, 'schema')
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize('which', [0, 1])
def test_corrupt_and_unreadable_files(stores, monkeypatch, which):
    stores[which].path.write_bytes(SECRET.encode())
    before = snapshot(stores[0].path.parent)
    result = inspect(stores)
    assert_unknown((result.brief, result.card)[which])
    assert SECRET not in repr(result)
    assert snapshot(stores[0].path.parent) == before
    original = os.open
    def denied(path, *args, **kwargs):
        if path == stores[which].path:
            raise PermissionError(SECRET)
        return original(path, *args, **kwargs)
    monkeypatch.setattr(os, 'open', denied)
    result = inspect(stores)
    assert_unknown((result.brief, result.card)[which], 'unreadable')


def test_same_inode_change_detected(stores, monkeypatch):
    original = BriefStore._inspect
    def changed(db, **kwargs):
        original(db, **kwargs)
        path = stores[0].path
        value = path.stat()
        os.utime(path, ns=(value.st_atime_ns, value.st_mtime_ns + 1_000_000))
    monkeypatch.setattr(BriefStore, '_inspect', changed)
    assert_unknown(inspect(stores).brief, 'changed')


@pytest.mark.parametrize('which', [0, 1])
@pytest.mark.parametrize('mode', ['many', 'large'])
def test_schema_enumeration_bounded_before_verifier(stores, monkeypatch, which, mode):
    with sqlite3.connect(stores[which].path) as db:
        if mode == 'many':
            for i in range(70):
                db.execute(f'CREATE TABLE extra{i}(x)')
        else:
            db.execute('CREATE TABLE extra(x ' + ' ' * 17000 + ')')
    def forbidden(*args, **kwargs):
        pytest.fail('Unbounded verifier entered')
    monkeypatch.setattr(BriefStore if which == 0 else CardLedger,
                        '_inspect' if which == 0 else '_verify', forbidden)
    result = inspect(stores)
    assert_unknown((result.brief, result.card)[which], 'schema')


def test_no_private_columns_or_mutating_entrypoints(stores, monkeypatch):
    populate(*stores)
    connect = sqlite3.connect
    seen = []
    forbidden_columns = {'payload_json', 'intent_json', 'identity_json', 'preview_json',
                         'card_json', 'wire_body', 'provenance_json', 'profile_json',
                         'usage_json', 'receipt_json', 'evidence_json', 'destination',
                         'token', 'candidate_id', 'uncertainty_json', 'first_failure_json',
                         'receipt', 'message_id', 'uncertainty', 'first_failure', 'response_card'}
    def authorizer(action, table, column, *args):
        if action == sqlite3.SQLITE_READ:
            seen.append((table, column))
            assert column not in forbidden_columns
        return sqlite3.SQLITE_OK
    def readonly(filename, **kwargs):
        assert filename.endswith('?mode=ro') and kwargs['uri'] and kwargs['timeout'] == 0
        db = connect(filename, **kwargs)
        db.set_authorizer(authorizer)
        return db
    def forbidden(*args, **kwargs):
        pytest.fail('Mutating entrypoint called')
    monkeypatch.setattr(sqlite3, 'connect', readonly)
    monkeypatch.setattr(BriefStore, '__init__', forbidden)
    monkeypatch.setattr(CardLedger, 'initialize', forbidden)
    report = inspect(stores)
    assert report.brief.complete and report.card.complete, seen
    assert seen


def test_sql_query_only_and_read_transaction(stores, monkeypatch):
    original = BriefStore._inspect
    def verify(db, **kwargs):
        assert db.in_transaction
        assert db.execute('PRAGMA query_only').fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError):
            db.execute('DELETE FROM brief_jobs')
        return original(db, **kwargs)
    monkeypatch.setattr(BriefStore, '_inspect', verify)
    assert inspect(stores).brief.complete


def test_deterministic_progress_budget(stores, monkeypatch):
    populate(*stores)
    now = [0.0]
    original = BriefStore._inspect
    def expire(db, **kwargs):
        original(db, **kwargs)
        now[0] += 1
        # A query exceeds one progress interval after the deterministic deadline.
        db.execute('WITH RECURSIVE n(x) AS (VALUES(0) UNION ALL SELECT x+1 FROM n '
                   'WHERE x<10000) SELECT sum(x) FROM n').fetchone()
        pytest.fail('SQLite progress handler did not interrupt')
    monkeypatch.setattr(inventory, 'monotonic', lambda: now[0])
    monkeypatch.setattr(BriefStore, '_inspect', expire)
    result = inspect(stores)
    assert_unknown(result.brief, 'budget')
    assert result.card.complete


@pytest.mark.parametrize('which,sql', [
    (0, "UPDATE brief_jobs SET job_id=? WHERE rowid=1"),
    (1, "UPDATE cards SET scope=? WHERE rowid=1"),
])
def test_oversized_identifiers_fail_closed(stores, which, sql):
    populate(*stores)
    execute(stores[which].path, sql, (SECRET * 10000,))
    result = inspect(stores)
    assert_unknown((result.brief, result.card)[which], 'malformed')
    assert SECRET not in repr(result)


@pytest.mark.parametrize('which,table,column,value', [
    (0, 'brief_jobs', 'state', 'custody'),
    (0, 'brief_delivery_reads', 'settled', 2),
    (0, 'brief_delivery_reads', 'ordinal', 0),
    (1, 'cards', 'state', 'cancelled'),
    (1, 'cards', 'cancelled', 2),
    (1, 'card_tokens', 'epoch', -1),
    (1, 'card_tokens', 'action', 'secret'),
    (1, 'card_windows', 'expires', 0),
])
def test_malformed_custody_metadata(stores, which, table, column, value):
    populate(*stores)
    with sqlite3.connect(stores[which].path) as db:
        db.execute('PRAGMA ignore_check_constraints=ON')
        db.execute(f'UPDATE {table} SET {column}=? WHERE rowid=1', (value,))
    result = inspect(stores)
    assert_unknown((result.brief, result.card)[which], 'malformed')


@pytest.mark.parametrize('table', ['brief_cost_profiles', 'brief_intakes', 'brief_failures'])
def test_auxiliary_job_orphans(stores, table):
    with sqlite3.connect(stores[0].path) as db:
        columns = 'job_id,profile_json' if table == 'brief_cost_profiles' else (
            'job_id,request_hash' if table == 'brief_intakes' else 'job_id,cause,created_us')
        args = ('orphan', SECRET) if table != 'brief_failures' else ('orphan', SECRET, 1)
        db.execute(f'INSERT INTO {table}({columns}) VALUES({",".join("?" for _ in args)})', args)
    assert_unknown(inspect(stores).brief, 'malformed')


def test_capacity_binding_and_multiple_owner_scopes(stores):
    assert_unknown(inspect(stores, card_capacity=21).card, 'schema')
    owners = tuple(RequesterContext(f'owner{i}', Principal('feishu', f'actor{i}')) for i in range(64))
    result = inspect(stores, owners=owners, row_limit=10000, budget_ms=5000)
    assert result.brief.complete and result.card.complete
    assert inspect(stores, owners=tuple(reversed(owners)), row_limit=10000, budget_ms=5000) == result


def test_wal_refused_without_sidecar_mutation(stores):
    with sqlite3.connect(stores[1].path) as db:
        assert db.execute('PRAGMA journal_mode=WAL').fetchone()[0] == 'wal'
        before = snapshot(stores[1].path.parent)
        assert_unknown(inspect(stores).card, 'schema')
        assert snapshot(stores[1].path.parent) == before


@pytest.mark.parametrize('which,table,column,reason', [
    (0, 'brief_delivery_reads', 'ordinal', 'malformed'),
    (1, 'cards', 'cancelled', 'malformed'),
    (1, 'card_meta', 'highwater', 'schema'),
])
def test_oversized_blob_in_numeric_metadata(stores, which, table, column, reason):
    populate(*stores)
    with sqlite3.connect(stores[which].path) as db:
        db.execute('PRAGMA ignore_check_constraints=ON')
        db.execute(f'UPDATE {table} SET {column}=? WHERE rowid=1', (SECRET.encode()*10000,))
    report = inspect(stores)
    assert_unknown((report.brief, report.card)[which], reason)
    assert SECRET not in repr(report)


@pytest.mark.parametrize('which', [0, 1])
def test_wal_swap_before_connect_never_creates_sidecars(stores, monkeypatch, which):
    target = stores[which].path
    replacement = target.parent / 'replacement.db'
    if which == 0:
        BriefStore(replacement).enable_delivery()
    else:
        CardLedger(replacement, capacity=20).initialize()
    real_connect = sqlite3.connect
    db = real_connect(replacement)
    try:
        assert db.execute('PRAGMA journal_mode=WAL').fetchone()[0] == 'wal'
    finally:
        db.close()
    assert not Path(str(replacement) + '-wal').exists()
    assert not Path(str(replacement) + '-shm').exists()
    replacement_bytes = replacement.read_bytes()
    touched, swaps = [], []

    class Observe(sqlite3.Connection):
        def execute(self, *args, **kwargs):
            try:
                return super().execute(*args, **kwargs)
            finally:
                # A reader may remove sidecars on close; inspect during SQL too.
                touched.extend(suffix for suffix in ('-wal', '-shm')
                               if Path(str(target) + suffix).exists())

    def swap_then_connect(path, *args, **kwargs):
        if path == target.as_uri() + '?mode=ro':
            os.replace(replacement, target)
            swaps.append(True)
            kwargs['factory'] = Observe
        return real_connect(path, *args, **kwargs)

    monkeypatch.setattr(inventory.sqlite3, 'connect', swap_then_connect)
    report = inspect(stores)
    assert swaps == [True]
    assert_unknown((report.brief, report.card)[which], 'changed')
    assert touched == []
    assert target.read_bytes() == replacement_bytes
    assert not Path(str(target) + '-wal').exists()
    assert not Path(str(target) + '-shm').exists()
