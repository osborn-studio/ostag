"""Five-domain structural inventory: real owned ledgers, no production wiring."""
from dataclasses import FrozenInstanceError, asdict, fields, replace
from itertools import combinations
import json
import os
from pathlib import Path
import sqlite3

import pytest

from ostag.briefs import startup_inventory as inv
from ostag.briefs.callbacks import CallbackLedger
from ostag.briefs.card_delivery import CardLedger
from ostag.briefs.retention import RetentionLedger
from ostag.briefs.revisions import RevisionLedger
from ostag.briefs.store import BriefStore, RequesterContext
from test_brief_retention import full_stack, MARKER
from test_brief_service import SCOPE
from test_brief_startup_inventory import snapshot, assert_unknown, execute

KINDS = ('brief', 'card', 'retention', 'revision', 'callback')
ADDED = KINDS[2:]
SECRET = 'PRIVATE_STARTUP_SENTINEL_7419'


@pytest.fixture
def stores(tmp_path):
    result = dict(brief=BriefStore(tmp_path/'brief.db'), card=CardLedger(tmp_path/'card.db'),
                  retention=RetentionLedger(tmp_path/'retention.db'),
                  revision=RevisionLedger(tmp_path/'revision.db'),
                  callback=CallbackLedger(tmp_path/'callback.db'))
    result['brief'].enable_delivery()
    for kind in KINDS[1:]:
        result[kind].initialize()
    return result


def inspect(stores, **changes):
    args = {kind + '_path': stores[kind].path for kind in KINDS}
    args.update(config=inv.RuntimeInventoryConfig(), owners=(SCOPE,), boot_id='boot-1',
                config_sha256='a'*64, observed_us=0)
    return inv.inspect_runtime_startup(**(args | changes))


@pytest.fixture
def populated(tmp_path):
    a, briefs, c, p, text, card, callback, revision, sends = full_stack(tmp_path)
    token = callback.issue(p, message_id='om_private_message')
    revision.register(SCOPE, p.job_id)
    revision.correct(SCOPE, p.job_id, expected_revision_id=p.revision_id,
                     request_id='private_request', text=SECRET)
    c.revoke_job(SCOPE, p.job_id)
    stores = dict(brief=briefs.store, card=card.ledger, retention=c.ledger,
                  revision=revision.ledger, callback=callback.ledger)
    try:
        yield stores, p, token
    finally:
        briefs.close()


def test_defaults_empty_and_immutable(stores, tmp_path):
    config = inv.RuntimeInventoryConfig()
    for kind in KINDS[1:]:
        assert getattr(config, kind + '_capacity') == stores[kind].capacity
    assert config.revision_chain_limit == stores['revision'].chain_limit
    assert config.revision_event_limit == stores['revision'].event_limit
    before = snapshot(tmp_path)
    report = inspect(stores)
    for kind in KINDS:
        store = getattr(report, kind)
        assert store.complete and store.reason == 'ok', store
        assert not any(dict(store.counts).values())
        assert store.fingerprint.schema_version == getattr(config, kind + '_schema')
    assert inspect(stores) == report
    assert snapshot(tmp_path) == before
    with pytest.raises(FrozenInstanceError):
        report.callback = report.card
    with pytest.raises(FrozenInstanceError):
        report.retention.counts = ()
    for name in ('ready', 'drained', 'reconciled'):
        assert not hasattr(report, name)


def test_real_pending_and_redaction(populated, tmp_path):
    stores, p, token = populated
    before = snapshot(tmp_path)
    report = inspect(stores)
    for kind in KINDS:
        assert getattr(report, kind).complete, (kind, getattr(report, kind))
    assert dict(report.retention.counts)['targets.pending'] == 4
    assert dict(report.retention.counts)['jobs.revoked'] == 1
    assert dict(report.revision.counts)['events'] == 1
    assert dict(report.callback.counts)['callbacks.live_unspent'] == 1
    encoded = repr(report) + json.dumps(asdict(report))
    for private in (SECRET, MARKER, token, str(tmp_path), SCOPE.principal.actor_id,
                    p.job_id, p.revision_id, 'om_private_message', 'private_request'):
        assert private not in encoded
    assert snapshot(tmp_path) == before
    later = inspect(stores, observed_us=10**16)
    assert dict(later.callback.counts)['callbacks.live_unspent'] == 0
    assert dict(later.callback.counts)['callbacks.expired_unspent'] == 1
    assert dict(later.retention.counts)['sources.expired'] > 0
    assert dict(later.retention.counts)['jobs.elapsed'] == 1
    assert snapshot(tmp_path) == before


def test_actual_retirement_tombstones(populated):
    stores, p, token = populated
    stores['callback'].retire_local(SCOPE, p.job_id)
    stores['revision'].retire_local(SCOPE, p.job_id)
    report = inspect(stores)
    assert report.callback.complete and report.revision.complete
    assert dict(report.callback.counts)['callbacks.tombstones'] == 1
    assert dict(report.revision.counts)['roots.tombstones'] == 1
    assert dict(report.revision.counts)['events.tombstones'] == 1


@pytest.mark.parametrize('kind', ADDED)
@pytest.mark.parametrize('failure', ['missing', 'foreign', 'future', 'locked', 'wal'])
def test_unavailable_never_zero(stores, tmp_path, kind, failure):
    path = stores[kind].path
    db = None
    if failure == 'missing':
        path.unlink()
    elif failure == 'foreign':
        execute(path, 'PRAGMA application_id=1')
    elif failure == 'future':
        execute(path, 'PRAGMA user_version=99')
    elif failure == 'locked':
        db = sqlite3.connect(path, timeout=0)
        db.execute('BEGIN EXCLUSIVE')
    else:
        db = sqlite3.connect(path, timeout=0)
        db.execute('PRAGMA journal_mode=WAL')
    try:
        before = snapshot(tmp_path)
        reason = dict(missing='missing', foreign='schema', future='schema', locked='locked', wal='schema')
        assert_unknown(getattr(inspect(stores), kind), reason[failure])
        assert snapshot(tmp_path) == before
    finally:
        if db:
            db.close()


@pytest.mark.parametrize('kind', ADDED)
def test_row_and_sql_budgets(populated, monkeypatch, kind):
    stores, _, _ = populated
    config = replace(inv.RuntimeInventoryConfig(), **{kind + '_budget': inv.ScanBudget(row_limit=1)})
    assert_unknown(getattr(inspect(stores, config=config), kind), 'overflow')
    now = [0]
    original = inv._verify_added
    def expire(db, **kwargs):
        original(db, **kwargs)
        if kwargs['kind'] == kind:
            now[0] += 1
            db.execute('WITH RECURSIVE n(x) AS (VALUES(0) UNION ALL SELECT x+1 FROM n '
                       'WHERE x<10000) SELECT sum(x) FROM n').fetchone()
            pytest.fail('progress deadline did not interrupt')
    monkeypatch.setattr(inv, 'monotonic', lambda: now[0])
    monkeypatch.setattr(inv, '_verify_added', expire)
    report = inspect(stores)
    assert_unknown(getattr(report, kind), 'budget')
    assert all(getattr(report, k).complete for k in KINDS if k != kind)


@pytest.mark.parametrize('kind,sql,reason', [
    ('retention', "UPDATE retention_jobs SET scope='foreign'", 'owner'),
    ('retention', "UPDATE retention_sources SET scope='foreign'", 'owner'),
    ('retention', "UPDATE retention_jobs SET decision='ready'", 'malformed'),
    ('retention', "UPDATE retention_jobs SET binding='bad'", 'malformed'),
    ('retention', "UPDATE retention_jobs SET delivery=NULL", 'malformed'),
    ('retention', "UPDATE retention_sources SET expired=2", 'malformed'),
    ('retention', "UPDATE retention_links SET snapshot='orphan' WHERE rowid=1", 'malformed'),
    ('retention', "UPDATE retention_targets SET job='orphan'", 'malformed'),
    ('retention', "UPDATE retention_targets SET done=2", 'malformed'),
    ('retention', "UPDATE retention_targets SET identity='bad'", 'malformed'),
    ('retention', "DELETE FROM retention_links", 'malformed'),
    ('retention', "DELETE FROM retention_targets WHERE kind='primary'", 'malformed'),
    ('revision', "UPDATE revision_roots SET scope='foreign'", 'owner'),
    ('revision', "UPDATE revision_events SET scope='foreign'", 'owner'),
    ('revision', "UPDATE revision_events SET job='orphan'", 'malformed'),
    ('revision', "UPDATE revision_events SET record='bad json'", 'malformed'),
    ('revision', "UPDATE revision_events SET record=json_set(record,'$.operation','ready')", 'malformed'),
    ('revision', "UPDATE revision_events SET record=json_set(record,'$.ordinal',9)", 'malformed'),
    ('revision', "UPDATE revision_roots SET snapshot=json_set(snapshot,'$.job','orphan')", 'malformed'),
    ('revision', "UPDATE revision_events SET record=json_set(record,'$.actor.actor_id','foreign')", 'owner'),
    ('callback', "UPDATE brief_callbacks SET binding='bad json'", 'malformed'),
    ('callback', "UPDATE brief_callbacks SET spent=2", 'malformed'),
    ('callback', "UPDATE brief_callbacks SET expires=issued", 'malformed'),
    ('callback', "UPDATE brief_callbacks SET issued=-1", 'malformed'),
])
def test_malformed_rows_are_unknown(populated, kind, sql, reason):
    stores, _, _ = populated
    with sqlite3.connect(stores[kind].path) as db:
        db.execute('PRAGMA ignore_check_constraints=ON')
        db.execute(sql)
    before = snapshot(stores[kind].path.parent)
    assert_unknown(getattr(inspect(stores), kind), reason)
    assert snapshot(stores[kind].path.parent) == before


@pytest.mark.parametrize('field,value,reason', [
    ('actor', 'foreign', 'owner'), ('platform', [], 'owner'),
    ('retired', True, 'malformed'), ('page', True, 'malformed'),
    ('binding', {'generation': 1}, 'malformed'), ('binding', SECRET, 'malformed'),
    ('action', 'send', 'malformed'), ('job', '', 'malformed'),
])
def test_callback_binding_contract(populated, field, value, reason):
    stores, _, _ = populated
    with sqlite3.connect(stores['callback'].path) as db:
        binding = json.loads(db.execute('SELECT binding FROM brief_callbacks').fetchone()[0])
        binding[field] = value
        db.execute('UPDATE brief_callbacks SET binding=?',
                   (json.dumps(binding, sort_keys=True, separators=(',', ':')),))
    report = inspect(stores)
    assert_unknown(report.callback, reason)
    assert SECRET not in repr(report)


@pytest.mark.parametrize('kind', ADDED)
@pytest.mark.parametrize('column,value', [('highwater', -1), ('highwater', SECRET.encode()*10000),
                                          ('capacity', SECRET), ('capacity', 1)])
def test_metadata_types_and_configuration(stores, kind, column, value):
    execute(stores[kind].path, f'UPDATE {kind}_meta SET {column}=?', (value,))
    assert_unknown(getattr(inspect(stores), kind), 'schema')


@pytest.mark.parametrize('pair', list(combinations(KINDS, 2)))
@pytest.mark.parametrize('hardlink', [False, True])
def test_every_alias_pair(stores, tmp_path, pair, hardlink):
    left, right = pair
    path = stores[left].path
    if hardlink:
        path = tmp_path / 'alias'
        os.link(stores[left].path, path)
    report = inspect(stores, **{right + '_path': path})
    assert_unknown(getattr(report, left), 'alias')
    assert_unknown(getattr(report, right), 'alias')
    assert all(getattr(report, k).complete for k in KINDS if k not in pair)


@pytest.mark.parametrize('kind', ADDED)
@pytest.mark.parametrize('ancestor', [False, True])
def test_symlinks(stores, tmp_path, kind, ancestor):
    path = tmp_path / 'alias'
    path.symlink_to(tmp_path if ancestor else stores[kind].path, target_is_directory=ancestor)
    report = inspect(stores, **{kind + '_path': path / stores[kind].path.name if ancestor else path})
    assert_unknown(getattr(report, kind), 'file')


@pytest.mark.parametrize('kind', ADDED)
def test_wal_swap_never_creates_sidecars(stores, monkeypatch, tmp_path, kind):
    target = stores[kind].path
    replacement = tmp_path / 'replacement'
    replacement.write_bytes(target.read_bytes())
    connect = sqlite3.connect
    db = connect(replacement, timeout=0)
    db.execute('PRAGMA journal_mode=WAL')
    db.close()
    expected = replacement.read_bytes()
    touched, swaps = [], []
    class Observe(sqlite3.Connection):
        def execute(self, *args, **kwargs):
            try:
                return super().execute(*args, **kwargs)
            finally:
                touched.extend(s for s in ('-wal', '-shm') if Path(str(target) + s).exists())
    def swap(path, *args, **kwargs):
        if path == target.as_uri() + '?mode=ro':
            os.replace(replacement, target)
            swaps.append(True)
            kwargs['factory'] = Observe
        return connect(path, *args, **kwargs)
    monkeypatch.setattr(inv.sqlite3, 'connect', swap)
    assert_unknown(getattr(inspect(stores), kind), 'changed')
    assert swaps == [True] and touched == []
    assert target.read_bytes() == expected
    assert not any(Path(str(target) + s).exists() for s in ('-wal', '-shm'))


@pytest.mark.parametrize('kind', ADDED)
@pytest.mark.parametrize('mode', ['many', 'large'])
def test_schema_preflight(stores, monkeypatch, kind, mode):
    with sqlite3.connect(stores[kind].path) as db:
        if mode == 'many':
            for i in range(70):
                db.execute(f'CREATE TABLE extra{i}(x)')
        else:
            db.execute('CREATE TABLE extra(x ' + ' '*17000 + ')')
    original = inv._verify_added
    def verify(db, **kwargs):
        assert kwargs['kind'] != kind, 'unbounded schema reached verifier'
        return original(db, **kwargs)
    monkeypatch.setattr(inv, '_verify_added', verify)
    assert_unknown(getattr(inspect(stores), kind), 'schema')


@pytest.mark.parametrize('change', [
    {'owners': []}, {'owners': [SCOPE, SCOPE]}, {'owners': [object()]},
    {'boot_id': SECRET + '\n'}, {'config_sha256': SECRET}, {'observed_us': True},
    {'observed_us': -1}, {'observed_us': 10**17}, {'callback_path': ':memory:'},
    {'retention_path': ''}, {'revision_path': 'bad\x00path'}, {'config': None},
] + [{'config': replace(inv.RuntimeInventoryConfig(), **{f.name: True})}
     for f in fields(inv.RuntimeInventoryConfig())]
  + [{'config': replace(inv.RuntimeInventoryConfig(), **{k+'_budget': inv.ScanBudget(row_limit=10001)})}
     for k in KINDS])
def test_invalid_before_io(stores, monkeypatch, change):
    def forbidden(*args, **kwargs):
        pytest.fail('I/O before validation')
    monkeypatch.setattr(Path, 'lstat', forbidden)
    monkeypatch.setattr(os, 'open', forbidden)
    monkeypatch.setattr(os, 'getcwd', forbidden)
    monkeypatch.setattr(sqlite3, 'connect', forbidden)
    with pytest.raises(ValueError, match='^invalid startup inventory arguments$') as exc:
        inspect(stores, **change)
    assert SECRET not in repr(exc.value)


def test_all_config_and_context_bound(stores):
    base = inspect(stores).binding
    config = inv.RuntimeInventoryConfig()
    for field in fields(config):
        if field.name.endswith('_schema'):
            continue  # Only supported versions are valid; versions appear in explicit hash below.
        value = getattr(config, field.name)
        value = replace(value, row_limit=999) if isinstance(value, inv.ScanBudget) else value - 1
        assert inspect(stores, config=replace(config, **{field.name: value})).binding != base
    for changes in ({'boot_id': SECRET}, {'config_sha256': 'b'*64}, {'observed_us': 1},
                    {'owners': (RequesterContext('other', SCOPE.principal),)},
                    {'callback_path': stores['callback'].path.parent/'missing'}):
        assert inspect(stores, **changes).binding != base
    import hashlib
    from ostag.briefs.store import _scope
    expected = ['runtime-startup-inventory-v1', 'boot-1', 'a'*64, ( _scope(SCOPE), ),
                asdict(config), 0, [str(stores[k].path) for k in KINDS]]
    assert base == hashlib.sha256(json.dumps(expected, sort_keys=True,
                                            separators=(',', ':')).encode()).hexdigest()


def test_readonly_connection_order_and_no_business_calls(populated, monkeypatch):
    stores, _, _ = populated
    connect = sqlite3.connect
    seen = []
    class Observe(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            seen.append(sql)
            return super().execute(sql, *args, **kwargs)
    def readonly(path, **kwargs):
        assert path.endswith('?mode=ro') and kwargs['timeout'] == 0
        kwargs['factory'] = Observe
        return connect(path, **kwargs)
    def forbidden(*args, **kwargs):
        pytest.fail('business or initialization call during scan')
    monkeypatch.setattr(sqlite3, 'connect', readonly)
    monkeypatch.setattr(RetentionLedger, '__init__', forbidden)
    for cls in (CardLedger, CallbackLedger, RevisionLedger):
        monkeypatch.setattr(cls, 'initialize', forbidden)
        monkeypatch.setattr(cls, 'retire_local', forbidden)
    report = inspect(stores)
    assert all(getattr(report, k).complete for k in KINDS)
    begins = [i for i, sql in enumerate(seen) if sql == 'BEGIN']
    assert len(begins) == 5
    for i in begins:
        assert seen[i-2:i] == ['PRAGMA query_only=ON', 'PRAGMA locking_mode=EXCLUSIVE']


def test_actual_cleanup_receipts_and_revocations(populated):
    from ostag.briefs.retention import RetentionCoordinator
    from test_brief_generation import at
    stores, p, _ = populated
    c = RetentionCoordinator(ledger=stores['retention'], store=stores['brief'],
        cards=stores['card'], callbacks=stores['callback'], revisions=stores['revision'],
        clock=lambda: at(10), maintenance=lambda *args: None)
    c.revoke_grant(SCOPE, SCOPE, 'untracked-grant')
    c.revoke_source(SCOPE, SCOPE, 'untracked-resource')
    assert c.cleanup(SCOPE, p.job_id).pending_targets == 0
    before = snapshot(stores['brief'].path.parent)
    report = inspect(stores)
    assert report.retention.complete
    counts = dict(report.retention.counts)
    assert counts['targets.done'] == 4 and counts['targets.pending'] == 0
    assert counts['revocations.resource'] == counts['revocations.grant'] == 1
    assert snapshot(stores['brief'].path.parent) == before


def test_spent_callback_is_a_fact(populated):
    stores, _, token = populated
    ledger = stores['callback']
    cap = ledger.lookup(token, clock=lambda: 1000)
    ledger.claim(token, cap, clock=lambda: 1000, check=lambda: None)
    report = inspect(stores)
    assert report.callback.complete
    assert dict(report.callback.counts)['callbacks.spent'] == 1
    assert dict(report.callback.counts)['callbacks.live_unspent'] == 0


def test_existing_unknown_effects_remain_facts(stores):
    from test_brief_startup_inventory import populate
    from test_brief_store import SCOPE as OTHER_SCOPE
    populate(stores['brief'], stores['card'])
    report = inspect(stores, owners=(SCOPE, OTHER_SCOPE))
    assert report.brief.complete and report.card.complete
    assert dict(report.brief.counts)['costs.unknown'] == 1
    assert dict(report.brief.counts)['deliveries.unknown'] == 1
    assert dict(report.card.counts)['cards.unknown'] == 1


@pytest.mark.parametrize('kind,table,column', [
    ('retention', 'retention_jobs', 'binding'),
    ('retention', 'retention_targets', 'identity'),
    ('retention', 'retention_sources', 'scope'),
    ('revision', 'revision_roots', 'snapshot'),
    ('revision', 'revision_events', 'record'),
    ('callback', 'brief_callbacks', 'binding'),
])
def test_large_fields_bounded_before_python_decode(populated, monkeypatch, kind, table, column):
    stores, _, _ = populated
    # Invalid UTF-8 declared as TEXT must never cross SQLite's Python decoder.
    # The oversized field is rejected by CASE before fetch, with a fixed reason.
    execute(stores[kind].path, f'UPDATE {table} SET {column}=CAST(? AS TEXT) WHERE rowid=1',
            (b'\xff' * (2 * 1048576),))
    original = inv.json.loads
    def decode(raw, *args, **kwargs):
        assert len(raw) <= 8192
        return original(raw, *args, **kwargs)
    monkeypatch.setattr(inv.json, 'loads', decode)
    report = inspect(stores)
    assert_unknown(getattr(report, kind), 'malformed')
    assert SECRET not in repr(report)


def test_private_revision_bodies_never_deserialized(populated, monkeypatch):
    stores, _, _ = populated
    original = inv.json.loads
    seen = []
    def decode(raw, *args, **kwargs):
        assert SECRET not in raw and MARKER not in raw
        seen.append(raw)
        return original(raw, *args, **kwargs)
    monkeypatch.setattr(inv.json, 'loads', decode)
    report = inspect(stores)
    assert report.revision.complete and seen


@pytest.mark.parametrize('kind', ADDED)
def test_errors_and_same_inode_changes_are_sanitized(stores, monkeypatch, kind):
    original = inv._verify_added
    def change(db, **kwargs):
        original(db, **kwargs)
        if kwargs['kind'] == kind:
            path = stores[kind].path
            stat = path.stat()
            os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1000000))
    monkeypatch.setattr(inv, '_verify_added', change)
    assert_unknown(getattr(inspect(stores), kind), 'changed')
    def fail(*args, **kwargs):
        raise OSError(SECRET)
    monkeypatch.setattr(inv.sqlite3, 'connect', fail)
    report = inspect(stores)
    assert_unknown(getattr(report, kind), 'unreadable')
    assert SECRET not in repr(report)


@pytest.mark.parametrize('field,value', [
    ('operation', 'unknown'), ('timestamp_us', True), ('ordinal', False),
    ('text', None), ('text', SECRET * 1000), ('parent_revision_id', 0),
])
def test_revision_bounded_value_types(populated, field, value):
    stores, _, _ = populated
    with sqlite3.connect(stores['revision'].path) as db:
        row = json.loads(db.execute('SELECT record FROM revision_events').fetchone()[0])
        row[field] = value
        db.execute('UPDATE revision_events SET record=?',
                   (json.dumps(row, sort_keys=True, separators=(',', ':')),))
    assert_unknown(inspect(stores).revision, 'malformed')


def test_revision_orphan_parent_and_root_binding(populated):
    stores, _, _ = populated
    with sqlite3.connect(stores['revision'].path) as db:
        row = json.loads(db.execute('SELECT record FROM revision_events').fetchone()[0])
        row['parent_revision_id'] = row['expected_revision_id'] = 'orphan'
        db.execute('UPDATE revision_events SET parent=?,record=?',
                   ('orphan', json.dumps(row, sort_keys=True, separators=(',', ':'))))
    assert_unknown(inspect(stores).revision, 'malformed')
    execute(stores['revision'].path, 'DELETE FROM revision_events')
    execute(stores['revision'].path,
            "UPDATE revision_roots SET snapshot=json_set(snapshot,'$.binding.generation',0)")
    assert_unknown(inspect(stores).revision, 'malformed')


@pytest.mark.parametrize('kind', ADDED)
def test_actual_foreign_schema_and_duplicate_metadata(stores, kind):
    path = stores[kind].path
    execute(path, f'ALTER TABLE {kind}_meta RENAME TO foreign_meta')
    assert_unknown(getattr(inspect(stores), kind), 'schema')
    execute(path, f'ALTER TABLE foreign_meta RENAME TO {kind}_meta')
    # Restoring with ALTER changes canonical DDL; use a fresh owned store instead.
    path = path.parent / 'fresh.db'
    ledger = type(stores[kind])(path)
    ledger.initialize()
    with sqlite3.connect(path) as db:
        db.execute('PRAGMA ignore_check_constraints=ON')
        columns = 'id,highwater,capacity' + (',chain_limit,event_limit' if kind == 'revision' else '')
        db.execute(f'INSERT INTO {kind}_meta({columns}) SELECT 2,highwater,capacity'
                   + (',chain_limit,event_limit' if kind == 'revision' else '')
                   + f' FROM {kind}_meta')
    assert_unknown(getattr(inspect(stores, **{kind+'_path': path}), kind), 'schema')


def test_revision_limits_and_capacity_are_checked(populated):
    stores, _, _ = populated
    for kind in ADDED:
        config = replace(inv.RuntimeInventoryConfig(), **{kind + '_capacity': 1})
        execute(stores[kind].path, f'UPDATE {kind}_meta SET capacity=1')
        # Callback has exactly one row; add a second valid row to exceed capacity.
        if kind == 'callback':
            execute(stores[kind].path, "INSERT INTO brief_callbacks SELECT ?,binding,issued,expires,spent "
                    'FROM brief_callbacks', ('a'*64,))
        assert_unknown(getattr(inspect(stores, config=config), kind), 'malformed')
        execute(stores[kind].path, f'UPDATE {kind}_meta SET capacity=?',
                (getattr(inv.RuntimeInventoryConfig(), kind + '_capacity'),))
    execute(stores['revision'].path, 'UPDATE revision_meta SET chain_limit=1,event_limit=1')
    config = replace(inv.RuntimeInventoryConfig(), revision_chain_limit=1, revision_event_limit=1)
    assert inspect(stores, config=config).revision.complete
    with sqlite3.connect(stores['revision'].path) as db:
        row = list(db.execute('SELECT * FROM revision_events').fetchone())
        record = json.loads(row[4])
        record.update(request_id='second', ordinal=1, event_id='re_'+'b'*48,
                      revision_id='rv_'+'c'*48, parent_revision_id=record['revision_id'],
                      expected_revision_id=record['revision_id'])
        row[2:5] = ['second', record['parent_revision_id'],
                    json.dumps(record, sort_keys=True, separators=(',', ':'))]
        db.execute('INSERT INTO revision_events VALUES(?,?,?,?,?,?)', row)
    assert_unknown(inspect(stores, config=config).revision, 'malformed')
    config = replace(config, revision_event_limit=2)
    execute(stores['revision'].path, 'UPDATE revision_meta SET event_limit=2')
    assert_unknown(inspect(stores, config=config).revision, 'malformed')


def test_legacy_also_validates_all_paths_before_getcwd(monkeypatch):
    def forbidden():
        pytest.fail('getcwd before final argument validation')
    monkeypatch.setattr(os, 'getcwd', forbidden)
    with pytest.raises(ValueError, match='^invalid startup inventory arguments$'):
        inv.inspect_startup(brief_path='relative.db', card_path='', card_capacity=1000,
                            owners=(SCOPE,), boot_id='boot', config_sha256='a'*64)


def test_validation_errors_do_not_retain_private_exception_context(stores, monkeypatch):
    def fail(*args):
        raise ValueError(SECRET)
    monkeypatch.setattr(inv, '_scope', fail)
    with pytest.raises(ValueError) as exc:
        inspect(stores)
    assert exc.value.__context__ is None and exc.value.__cause__ is None
    assert SECRET not in repr(exc.value)


@pytest.mark.parametrize('table,column', [('revision_roots', 'snapshot'), ('revision_events', 'record')])
def test_duplicate_revision_owner_keys_are_unknown(populated, table, column):
    stores, _, _ = populated
    with sqlite3.connect(stores['revision'].path) as db:
        raw = db.execute(f'SELECT {column} FROM {table}').fetchone()[0]
        key = '"actor_id":' + json.dumps(SCOPE.principal.actor_id)
        assert key in raw
        raw = raw.replace(key, key + ',' + key, 1)
        db.execute(f'UPDATE {table} SET {column}=?', (raw,))
    assert_unknown(inspect(stores).revision, 'owner')
