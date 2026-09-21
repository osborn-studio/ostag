"""Physical five-store reconciliation; hashes are not global atomicity proof."""
import sqlite3

import pytest

from ostag.briefs.retention import _identity
from ostag.briefs.startup_inventory import RuntimeInventoryConfig
from ostag.briefs.startup_reconciliation import KINDS, reconcile_startup
from test_brief_service import SCOPE, Authority
from test_brief_startup_inventory import snapshot
from test_brief_startup_inventory_all import stores as inventory_stores


@pytest.fixture
def stores(tmp_path):
    return inventory_stores.__wrapped__(tmp_path)


def reconcile(stores, **changes):
    targets = {target: _identity(stores[kind]) for kind, target in (
        ('brief', 'primary'), ('card', 'cards'), ('revision', 'revisions'), ('callback', 'callbacks'))}
    args = dict(config=RuntimeInventoryConfig(), owners=(SCOPE,), boot_id='boot-1',
                config_sha256='a' * 64, observed_us=0, target_identities=targets,
                generation_config=Authority().config)
    args.update({kind + '_path': stores[kind].path for kind in KINDS})
    return reconcile_startup(**(args | changes))


def test_fresh_empty_and_clean_reopen_read_only(stores, tmp_path):
    before = snapshot(tmp_path)
    r = reconcile(stores)
    assert r.ready, r
    assert r.complete and r.active_leases == r.unknown_effects == r.queued == 0
    assert reconcile(stores) == r
    assert snapshot(tmp_path) == before
    assert reconcile(stores, boot_id='boot-2').binding != r.binding
    assert reconcile(stores, config_sha256='b' * 64).binding != r.binding


@pytest.mark.parametrize('kind', KINDS)
def test_missing_foreign_and_schema_fail_closed(stores, tmp_path, kind):
    before = snapshot(tmp_path)
    path = tmp_path / 'missing.db'
    result = reconcile(stores, **{kind + '_path': path})
    assert not result.ready and not result.complete and kind + ':missing' in result.blockers
    assert snapshot(tmp_path) == before
    with sqlite3.connect(stores[kind].path) as db:
        db.execute('PRAGMA user_version=99')
    assert not reconcile(stores).ready


def test_alias_wal_and_busy_are_not_empty(stores):
    assert not reconcile(stores, card_path=stores['brief'].path).ready
    with sqlite3.connect(stores['card'].path) as db:
        db.execute('PRAGMA journal_mode=WAL')
    assert not reconcile(stores).ready


def test_exclusive_database_lock_is_not_unknown_zero(stores):
    with sqlite3.connect(stores['brief'].path) as db:
        db.execute('BEGIN EXCLUSIVE')
        report = reconcile(stores)
        assert not report.complete and not report.ready
        assert 'brief:locked' in report.blockers


def test_unregistered_legacy_job_is_explicit_blocker(stores):
    from test_brief_service import Authority, event
    service = Authority().service(stores['brief'])
    try:
        assert service.submit(event()).status == 'queued'
        result = reconcile(stores)
        assert result.complete and not result.ready
        assert result.queued == 1 and 'missing_retention' in result.blockers
    finally:
        service.close()


def test_stored_generation_pins_must_match_current_composition(stores):
    from dataclasses import replace
    from test_brief_service import event
    service = Authority().service(stores['brief'])
    try:
        assert service.submit(event()).status == 'queued'
        pinned = Authority().config
        changed = replace(pinned, daily_limit_micros=pinned.daily_limit_micros + 1)
        result = reconcile(stores, generation_config=changed)
        assert result.complete and not result.ready
        assert 'generation_config_mismatch' in result.blockers
    finally:
        service.close()


@pytest.fixture
def populated(tmp_path):
    from test_brief_retention import full_stack
    _, service, coordinator, preview, _, card, callback, revision, _ = full_stack(tmp_path)
    result = dict(brief=service.store, card=card.ledger, retention=coordinator.ledger,
                  revision=revision.ledger, callback=callback.ledger)
    try:
        yield result, coordinator, preview, card, callback, revision
    finally:
        service.close()


async def test_completed_card_callback_revision_cross_references(populated, tmp_path):
    ledgers, _, preview, card, callback, revision = populated
    await card.deliver(SCOPE, preview.job_id, preview.revision_id)
    callback.issue(preview, message_id='om_message_123')
    revision.register(SCOPE, preview.job_id)
    before = snapshot(tmp_path)
    report = reconcile(ledgers)
    assert report.ready, report
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize('damage,expected', [
    ('target', 'retention_target_identity'), ('binding', 'retention_binding'),
    ('revision', 'retention_revision'), ('lease', 'previous_ownership'),
    ('cost', 'unknown_remote_effect'), ('orphan', 'orphan_card'),
    ('callback', 'orphan_callback'), ('revision_root', 'revision_binding'),
])
async def test_cross_store_mismatches_and_unknown_are_quarantined(populated, damage, expected):
    import json
    ledgers, _, preview, card, callback, revision = populated
    await card.deliver(SCOPE, preview.job_id, preview.revision_id)
    callback.issue(preview, message_id='om_message_123')
    revision.register(SCOPE, preview.job_id)
    if damage in ('target', 'binding', 'revision'):
        with sqlite3.connect(ledgers['retention'].path) as db:
            if damage == 'target':
                db.execute("UPDATE retention_targets SET identity=?", ('f' * 64,))
            elif damage == 'binding':
                db.execute("UPDATE retention_jobs SET binding=?", ('f' * 64,))
            else:
                db.execute("UPDATE retention_jobs SET revision='different'")
    elif damage == 'lease':
        with sqlite3.connect(ledgers['brief'].path) as db:
            db.execute("UPDATE brief_jobs SET owner_id='missing-pid',lease_us=1")
    elif damage == 'cost':
        with sqlite3.connect(ledgers['brief'].path) as db:
            db.execute("UPDATE brief_cost_calls SET state='unknown'")
    elif damage == 'orphan':
        with sqlite3.connect(ledgers['card'].path) as db:
            value = json.loads(db.execute('SELECT identity_json FROM cards').fetchone()[0])
            value['job'] = 'absent'
            db.execute('UPDATE cards SET identity_json=?', (json.dumps(value, sort_keys=True, separators=(',', ':')),))
    elif damage == 'callback':
        with sqlite3.connect(ledgers['callback'].path) as db:
            value = json.loads(db.execute('SELECT binding FROM brief_callbacks').fetchone()[0])
            value['revision'] = 'absent'
            db.execute('UPDATE brief_callbacks SET binding=?', (json.dumps(value, sort_keys=True, separators=(',', ':')),))
    else:
        with sqlite3.connect(ledgers['revision'].path) as db:
            value = json.loads(db.execute('SELECT snapshot FROM revision_roots').fetchone()[0])
            value['revision'] = 'absent'
            db.execute('UPDATE revision_roots SET snapshot=?', (json.dumps(value, sort_keys=True, separators=(',', ':')),))
    report = reconcile(ledgers)
    assert report.complete and not report.ready, report
    assert expected in report.blockers
