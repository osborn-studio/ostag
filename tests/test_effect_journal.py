"""U02 execution authority and central journal contracts; synthetic providers only."""
from dataclasses import replace
import asyncio
from datetime import timedelta

import pytest

from ostag.core.identity import Principal
from ostag.core.receipts import ReceiptCommand
from ostag.core.schemas import SchemaError
from ostag.core.effects import PendingEffect
from tests.test_approvals import _service, _receipt


def service(tmp_path, executor):
    result, ledger = _service(tmp_path, executors={'brief_field_change': executor},
                              apply_enabled=True)
    result.config = replace(result.config, actor_permissions=('feishu:dev:write',))
    receipt = replace(_receipt(), action_id='action_frozen' + 'a' * 64,
                      target_system='brief_field_change',
                      principal=Principal('feishu', 'dev'))
    ledger.path.parent.mkdir(parents=True, exist_ok=True)
    result.propose_once(receipt, chat_id='chat1', platform='feishu', proposer_id='dev')
    return result, ledger, receipt


async def authorize_and_apply(approvals, receipt):
    await approvals.execute(ReceiptCommand('approve', receipt.action_id),
        principal=Principal('feishu', 'dev'), chat_id='chat1')
    return await approvals.execute(ReceiptCommand('apply', receipt.action_id),
        principal=Principal('feishu', 'dev'), chat_id='chat1')


def test_stored_applying_receipt_is_not_an_execution_capability(tmp_path):
    from ostag.core.effects import claim_apply_invocation
    approvals, ledger, receipt = service(tmp_path, object())
    with pytest.raises(SchemaError) as error:
        claim_apply_invocation(ledger, receipt.with_state('applying'))
    assert error.value.code == 'effect_authority_denied'


async def test_real_approval_call_issues_exact_single_use_invocation(tmp_path):
    from ostag.core.effects import claim_apply_invocation
    seen = []
    class Executor:
        async def apply(self, receipt):
            permit = claim_apply_invocation(ledger, receipt)
            permit.checkpoint()
            seen.append(permit)
            assert permit.action_id == receipt.action_id
            assert permit.principal == receipt.principal
            assert permit.expires_at > permit.issued_at
            with pytest.raises(SchemaError):
                claim_apply_invocation(ledger, receipt)
            return PendingEffect(receipt.action_id, permit.attempt_id, '0' * 64)
    approvals, ledger, receipt = service(tmp_path, Executor())
    result = await authorize_and_apply(approvals, receipt)
    assert result['action']['state'] == 'applying'
    assert 'pending_effect' in result
    assert len(seen) == 1
    with pytest.raises(SchemaError):
        seen[0].checkpoint()  # no retained authority after the invocation returns


@pytest.mark.parametrize('change', ['payload', 'principal', 'ledger'])
async def test_invocation_cannot_be_rebound(tmp_path, change):
    from ostag.core.effects import claim_apply_invocation
    from ostag.core.run_ledger import RunLedger
    class Executor:
        async def apply(self, receipt):
            supplied = receipt
            supplied_ledger = ledger
            if change == 'payload':
                supplied = replace(receipt, apply_payload={'changed': True})
            elif change == 'principal':
                supplied = replace(receipt, principal=Principal('feishu', 'other'))
            else:
                supplied_ledger = RunLedger(ledger.path)
            with pytest.raises(SchemaError):
                claim_apply_invocation(supplied_ledger, supplied)
            # A wrong claim must not create another valid authority.
            actual = claim_apply_invocation(ledger, receipt)
            actual.checkpoint()
            return PendingEffect(receipt.action_id, actual.attempt_id, '0' * 64)
    approvals, ledger, receipt = service(tmp_path, Executor())
    await authorize_and_apply(approvals, receipt)


async def test_child_context_cannot_outlive_its_approval_invocation(tmp_path):
    from ostag.core.effects import claim_apply_invocation
    release = asyncio.Event()
    children = []
    class Executor:
        async def apply(self, receipt):
            async def delayed():
                await release.wait()
                with pytest.raises(SchemaError):
                    claim_apply_invocation(ledger, receipt)
            children.append(asyncio.create_task(delayed()))
            actual = claim_apply_invocation(ledger, receipt)
            return PendingEffect(receipt.action_id, actual.attempt_id, '0' * 64)
    approvals, ledger, receipt = service(tmp_path, Executor())
    await authorize_and_apply(approvals, receipt)
    release.set()
    await asyncio.wait_for(asyncio.gather(*children), timeout=2)


@pytest.mark.parametrize('clock_change', ['expired', 'rewound'])
async def test_invocation_expiry_and_clock_rewind_fail_closed(tmp_path, monkeypatch, clock_change):
    from ostag.core import effects
    class Executor:
        async def apply(self, receipt):
            permit = effects.claim_apply_invocation(ledger, receipt)
            now = permit.expires_at if clock_change == 'expired' else permit.issued_at - timedelta(seconds=1)
            monkeypatch.setattr(effects, '_now', lambda: now)
            with pytest.raises(SchemaError):
                permit.checkpoint()
            return PendingEffect(receipt.action_id, permit.attempt_id, '0' * 64)
    approvals, ledger, receipt = service(tmp_path, Executor())
    await authorize_and_apply(approvals, receipt)


def test_effect_schema_is_explicit_backed_up_and_preserves_old_rows(tmp_path):
    import sqlite3
    from ostag.briefs.store import BriefStore
    from tests.test_brief_store import admit, SCOPE
    store = BriefStore(tmp_path / 'briefs.db')
    store.enable_delivery()
    job = admit(store)
    with sqlite3.connect(store.path) as db:
        before = db.execute('SELECT * FROM brief_jobs').fetchall()
        old_schema = db.execute("SELECT name,sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY name").fetchall()
        assert db.execute('PRAGMA user_version').fetchone()[0] == 4
    backup = tmp_path / 'before-effects.db'
    store.enable_effects(backup_path=backup)
    with sqlite3.connect(backup) as db:
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert db.execute('PRAGMA user_version').fetchone()[0] == 4
        assert db.execute('SELECT * FROM brief_jobs').fetchall() == before
        assert db.execute("SELECT name,sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY name").fetchall() == old_schema
    with sqlite3.connect(store.path) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 5
        assert db.execute('SELECT * FROM brief_jobs').fetchall() == before
        for name, sql in old_schema:
            assert db.execute('SELECT sql FROM sqlite_master WHERE name=?', (name,)).fetchone()[0] == sql
        assert db.execute('SELECT highwater FROM brief_effect_meta').fetchone()[0] == 0
    assert BriefStore(store.path).get(SCOPE, job.job_id) == store.get(SCOPE, job.job_id)


def test_effect_enablement_refuses_existing_backup_without_touching_source(tmp_path):
    from ostag.briefs.store import BriefStore
    store = BriefStore(tmp_path / 'briefs.db')
    store.enable_delivery()
    before = store.path.read_bytes()
    backup = tmp_path / 'exists.db'
    backup.write_bytes(b'preserve existing artifact')
    with pytest.raises(Exception):
        store.enable_effects(backup_path=backup)
    assert store.path.read_bytes() == before
    assert backup.read_bytes() == b'preserve existing artifact'


def test_effect_migration_transaction_rolls_back_if_ddl_fails(tmp_path, monkeypatch):
    import sqlite3
    import ostag.briefs.store as module
    store = module.BriefStore(tmp_path / 'briefs.db')
    store.enable_delivery()
    original = module._EFFECT_SCHEMA
    monkeypatch.setattr(module, '_EFFECT_SCHEMA', original + ('INVALID DDL',))
    with pytest.raises(Exception):
        store.enable_effects(backup_path=tmp_path / 'backup.db')
    with sqlite3.connect(store.path) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 4
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='brief_effect_meta'").fetchall()


def journal_rig(tmp_path, operation, *, existing=None, key='job1'):
    from datetime import datetime, UTC
    from ostag.briefs.store import BriefStore, WorkerContext
    from ostag.core.effect_journal import EffectJournal, JournaledEffectExecutor
    from ostag.core.approvals import ApprovalService
    from ostag.core.run_ledger import RunLedger
    from ostag.config import Settings
    from ostag.briefs.actions import ActionProposalService
    from tests.test_brief_cards import SCOPE, preview
    from tests.test_brief_revisions import service as revision_service
    from tests.test_brief_action_proposals import observation, change
    store = existing or BriefStore(tmp_path / 'brief.db')
    if existing is None:
        store.enable_delivery()
        store.enable_effects(backup_path=tmp_path / 'before-effects.db')
    def now():
        return datetime.now(UTC).isoformat()
    original = preview()
    job = store.admit(SCOPE, idempotency_key=key, payload={'synthetic': True},
        grant_id='grant', grant_revision=1, destination=original.destination,
        execution_seconds=180, now=now())
    worker = WorkerContext('worker1')
    lease = store.claim_one(worker, clock=now).lease
    store.start_generating(lease, clock=now)
    revision = store.complete(lease, draft=original.draft.to_dict(), provenance={'synthetic': True},
                             validation_version='v1', clock=now)
    outbox = store.outbox(SCOPE, job.job_id)
    current = [replace(original, job_id=job.job_id, revision_id=revision.revision_id,
                       outbox_id=outbox.delivery_id)]
    revisions = revision_service(tmp_path / 'revisions.db', current=current)
    root = revisions.register(SCOPE, job.job_id)
    revisions.adopt(SCOPE, job.job_id, expected_revision_id=revision.revision_id, request_id='adopt1')
    ledger = RunLedger(tmp_path / 'run.jsonl')
    approvals = ApprovalService(Settings(apply_enabled=True,
        actor_permissions=(f'feishu:{SCOPE.principal.actor_id}:write',)), ledger)
    approvals.channel_brain.root = tmp_path / 'channels'
    observed = [observation()]
    proposals = ActionProposalService(revisions=revisions, approvals=approvals,
        observe=lambda c: observed[0], authorize=lambda c: True,
        destination=lambda scope: original.destination)
    frozen = change(job_id=job.job_id, revision_id=revision.revision_id, root_digest=root.root_digest)
    receipt = proposals.propose(SCOPE, frozen)
    journal = EffectJournal(store=store, proposals=proposals, worker=worker)
    approvals.executors['brief_field_change'] = JournaledEffectExecutor(journal, operation)
    return approvals, receipt, journal, SCOPE, original.destination.address


async def apply_journal(rig):
    approvals, receipt, journal, scope, chat = rig
    await approvals.execute(ReceiptCommand('approve', receipt.action_id),
                            principal=scope.principal, chat_id=chat)
    return await approvals.execute(ReceiptCommand('apply', receipt.action_id),
                                   principal=scope.principal, chat_id=chat)


async def test_central_intent_and_begin_are_durable_before_io(tmp_path):
    import sqlite3
    called = []
    async def operation(permit):
        p = permit.to_dict()
        with sqlite3.connect(rig[2].store.path) as db:
            rows = db.execute('SELECT i.action_id,a.attempt_id,a.state FROM brief_action_intents i '
                'JOIN brief_effect_attempts a ON a.action_id=i.action_id').fetchall()
            assert rows == [(p['action_id'], p['attempt_id'], 'begun')]
            assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        called.append(p)
        return {'provider_receipt': 'synthetic'}
    rig = journal_rig(tmp_path, operation)
    outcome = await apply_journal(rig)
    assert outcome['action']['state'] == 'applying'
    assert 'pending_effect' in outcome
    assert len(called) == 1
    with sqlite3.connect(rig[2].store.path) as db:
        assert db.execute('SELECT state FROM brief_effect_attempts').fetchone()[0] == 'unknown'
        assert db.execute('SELECT kind FROM brief_effect_observations').fetchone()[0] == 'transport_response'


async def test_failed_central_commit_never_calls_provider(tmp_path, monkeypatch):
    from contextlib import contextmanager
    import sqlite3
    calls = []
    async def operation(permit):
        calls.append(permit)
        return {}
    rig = journal_rig(tmp_path, operation)
    original = rig[2].store._delivery_transaction
    @contextmanager
    def fail_commit(*args, **kwargs):
        with original(*args, **kwargs) as db:
            yield db
            raise RuntimeError('synthetic precommit failure')
    monkeypatch.setattr(rig[2].store, '_delivery_transaction', fail_commit)
    with pytest.raises(SchemaError):
        await apply_journal(rig)
    assert calls == []
    with sqlite3.connect(rig[2].store.path) as db:
        assert db.execute('SELECT count(*) FROM brief_action_intents').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM brief_effect_attempts').fetchone()[0] == 0
        assert db.execute('SELECT highwater FROM brief_effect_meta').fetchone()[0] == 0


async def test_lost_provider_response_retains_unknown_custody(tmp_path):
    import sqlite3
    calls = []
    async def operation(permit):
        calls.append(permit)
        raise TimeoutError('synthetic response loss')
    rig = journal_rig(tmp_path, operation)
    with pytest.raises(SchemaError):
        await apply_journal(rig)
    assert len(calls) == 1
    with sqlite3.connect(rig[2].store.path) as db:
        assert db.execute('SELECT state FROM brief_effect_attempts').fetchone()[0] == 'unknown'
        assert db.execute('SELECT count(*) FROM brief_action_intents').fetchone()[0] == 1
    with pytest.raises(SchemaError):
        await apply_journal(rig)
    assert len(calls) == 1


async def test_retirement_removes_intent_content_but_preserves_unknown_custody(tmp_path):
    import sqlite3
    async def operation(permit):
        return {'provider_receipt': 'synthetic'}
    rig = journal_rig(tmp_path, operation)
    await apply_journal(rig)
    store = rig[2].store
    with sqlite3.connect(store.path) as db:
        job = db.execute('SELECT job_id FROM brief_action_intents').fetchone()[0]
    store.retire_local(rig[3], job)
    with sqlite3.connect(store.path) as db:
        assert db.execute('SELECT intent_json FROM brief_action_intents').fetchone()[0] == '{"retired":true}'
        assert db.execute('SELECT permit_json,state FROM brief_effect_attempts').fetchone() == ('{"retired":true}', 'unknown')
        assert db.execute('SELECT count(*) FROM brief_effect_observations').fetchone()[0] == 1
        assert db.execute('SELECT resource_key FROM brief_action_intents').fetchone()[0]


async def test_new_job_cannot_reset_same_resource_uncertainty(tmp_path):
    calls = []
    async def operation(permit):
        calls.append(permit.to_dict())
        return {'provider_receipt': 'synthetic'}
    first = journal_rig(tmp_path, operation)
    await apply_journal(first)
    second = journal_rig(tmp_path, operation, existing=first[2].store, key='job2')
    assert first[1].action_id != second[1].action_id
    with pytest.raises(SchemaError):
        await apply_journal(second)
    assert len(calls) == 1


async def test_sqlite_snapshot_cannot_hide_later_jsonl_effect_custody(tmp_path):
    import sqlite3
    calls = []
    async def operation(permit):
        calls.append(permit.to_dict())
        return {'provider_receipt': 'synthetic'}
    first = journal_rig(tmp_path, operation)
    store = first[2].store
    backup = tmp_path / 'before-dispatch.db'
    source = sqlite3.connect(store.path)
    target = sqlite3.connect(backup)
    try:
        source.backup(target)
    finally:
        source.close()
        target.close()
    await apply_journal(first)
    # Controlled offline restore into the same test DB; approval JSONL retains
    # the later applying receipt. This is a failure probe, not a restore API.
    source = sqlite3.connect(backup)
    target = sqlite3.connect(store.path)
    try:
        source.backup(target)
    finally:
        source.close()
        target.close()
    second = journal_rig(tmp_path, operation, existing=store, key='job2')
    with pytest.raises(SchemaError):
        await apply_journal(second)
    assert len(calls) == 1


async def test_unverified_string_response_cannot_mark_frozen_action_applied(tmp_path):
    class Executor:
        async def apply(self, receipt):
            return 'success'
    approvals, ledger, receipt = service(tmp_path, Executor())
    with pytest.raises(SchemaError):
        await authorize_and_apply(approvals, receipt)
    assert ledger.get_receipt(receipt.action_id).state == 'failed'


async def test_authority_revoked_after_durable_begin_never_calls_provider(tmp_path, monkeypatch):
    import sqlite3
    calls = []
    async def operation(permit):
        calls.append(permit)
        return {}
    rig = journal_rig(tmp_path, operation)
    journal = rig[2]
    original = journal._current
    checks = []
    def revoked(receipt):
        checks.append(receipt.action_id)
        if len(checks) == 2:
            raise SchemaError('effect_authority_denied', 'Synthetic revocation', 'No retry')
        return original(receipt)
    monkeypatch.setattr(journal, '_current', revoked)
    with pytest.raises(SchemaError):
        await apply_journal(rig)
    assert calls == []
    with sqlite3.connect(journal.store.path) as db:
        assert db.execute('SELECT state FROM brief_effect_attempts').fetchone()[0] == 'begun'
        assert db.execute('SELECT count(*) FROM brief_effect_observations').fetchone()[0] == 0


async def test_final_lifetime_check_rolls_back_intent_and_begin(tmp_path, monkeypatch):
    import sqlite3
    from ostag.core import effects
    calls = []
    async def operation(permit):
        calls.append(permit)
        return {}
    rig = journal_rig(tmp_path, operation)
    original = effects._Invocation.checkpoint
    def expires_after_insert(invocation):
        # Inspect the active connection by hooking the transaction below; this
        # models expiry at the last checkpoint before commit, not before claim.
        if active and active[0].execute('SELECT count(*) FROM brief_effect_attempts').fetchone()[0]:
            raise SchemaError('effect_authority_denied', 'Synthetic expiry', 'No retry')
        return original(invocation)
    from contextlib import contextmanager
    transaction = rig[2].store._delivery_transaction
    active = []
    @contextmanager
    def captured(*args, **kwargs):
        with transaction(*args, **kwargs) as db:
            active.append(db)
            try:
                yield db
            finally:
                active.pop()
    monkeypatch.setattr(rig[2].store, '_delivery_transaction', captured)
    monkeypatch.setattr(effects._Invocation, 'checkpoint', expires_after_insert)
    with pytest.raises(SchemaError):
        await apply_journal(rig)
    assert calls == []
    with sqlite3.connect(rig[2].store.path) as db:
        assert db.execute('SELECT count(*) FROM brief_action_intents').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM brief_effect_attempts').fetchone()[0] == 0
        assert db.execute('SELECT highwater FROM brief_effect_meta').fetchone()[0] == 0


@pytest.mark.parametrize('damage', [None, 'fence', 'owner', 'orphan'])
async def test_schema5_inventory_and_reconciliation_preserve_unknown_effects(tmp_path, damage):
    import sqlite3
    from test_brief_startup_inventory_all import stores, inspect
    from test_brief_startup_reconciliation import reconcile
    from ostag.briefs.startup_inventory import RuntimeInventoryConfig
    async def operation(permit):
        return {'synthetic': True}
    rig = journal_rig(tmp_path, operation)
    await apply_journal(rig)
    others = tmp_path / 'other-stores'
    others.mkdir()
    domains = stores.__wrapped__(others)
    domains['brief'] = rig[2].store
    import json
    from test_brief_service import Authority
    with sqlite3.connect(domains['brief'].path) as db:
        db.execute('UPDATE brief_jobs SET payload_json=?',
                   (json.dumps({'config': Authority().config.to_dict()}),))
    config = RuntimeInventoryConfig(brief_schema=5)
    # Old/default startup explicitly rejects the new schema instead of ignoring
    # its custody tables. Only explicit schema5 inventory can examine them.
    assert not inspect(domains, owners=(rig[3],)).brief.complete
    if damage:
        with sqlite3.connect(domains['brief'].path) as db:
            if damage == 'fence':
                db.execute('UPDATE brief_effect_meta SET highwater=0')
            elif damage == 'owner':
                db.execute("UPDATE brief_action_intents SET actor_id='foreign'")
            else:
                db.execute('DELETE FROM brief_effect_attempts')
    report = inspect(domains, config=config, owners=(rig[3],))
    if damage:
        assert not report.brief.complete
        assert report.brief.counts is None
    else:
        assert report.brief.complete, report.brief
        counts = dict(report.brief.counts)
        assert counts['effects.unknown'] == counts['effects.intents'] == 1
        assert counts['effects.begun'] == 0
        result = reconcile(domains, config=config, owners=(rig[3],))
        assert result.unknown_effects >= 1
        assert not result.ready


def test_migration_refuses_real_intervening_commit_after_snapshot(tmp_path, monkeypatch):
    import sqlite3
    from contextlib import contextmanager
    from ostag.briefs.store import BriefStore, Conflict
    from test_brief_store import admit
    store = BriefStore(tmp_path / 'brief.db')
    store.enable_delivery()
    admit(store)
    connection = store._connection
    writes = []
    class Connection:
        def __init__(self, db):
            self.db = db
        def __getattr__(self, name):
            return getattr(self.db, name)
        def rollback(self):
            self.db.rollback()
            with sqlite3.connect(store.path) as concurrent:
                concurrent.execute("UPDATE brief_jobs SET state='cancelled'")
            writes.append(True)
    @contextmanager
    def interleaved():
        with connection() as db:
            yield Connection(db)
    monkeypatch.setattr(store, '_connection', interleaved)
    backup = tmp_path / 'snapshot.db'
    with pytest.raises(Conflict):
        store.enable_effects(backup_path=backup)
    assert writes == [True]
    with sqlite3.connect(store.path) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 4
        assert db.execute('SELECT state FROM brief_jobs').fetchone()[0] == 'cancelled'
        assert not db.execute("SELECT name FROM sqlite_master WHERE name='brief_effect_meta'").fetchall()
    with sqlite3.connect(backup) as db:
        assert db.execute('SELECT state FROM brief_jobs').fetchone()[0] == 'queued'


async def test_provider_error_body_never_escapes_frozen_effect_surface(tmp_path):
    import traceback
    secret = 'PRIVATE_PROVIDER_RECORD_salary_920000'
    async def operation(permit):
        raise RuntimeError(secret)
    rig = journal_rig(tmp_path, operation)
    with pytest.raises(SchemaError) as error:
        await apply_journal(rig)
    assert secret not in str(error.value)
    assert secret not in ''.join(traceback.format_exception(error.value))
    assert secret not in rig[2].ledger.path.read_text()


async def test_provider_error_body_never_escapes_chat_reply(tmp_path):
    secret = 'PRIVATE_PROVIDER_RECORD_salary_920000'
    async def operation(permit):
        raise RuntimeError(secret)
    rig = journal_rig(tmp_path, operation)
    approvals, receipt, journal, scope, chat = rig
    await approvals.execute(ReceiptCommand('approve', receipt.action_id),
                            principal=scope.principal, chat_id=chat)
    reply = await approvals.handle_text_command(ReceiptCommand('apply', receipt.action_id),
                                               principal=scope.principal, chat_id=chat)
    assert secret not in reply
    assert secret not in journal.ledger.path.read_text()


async def test_inflight_retirement_preserves_response_hash_without_reviving_payload(tmp_path):
    import sqlite3
    entered, release = asyncio.Event(), asyncio.Event()
    seen = []
    async def operation(permit):
        seen.append(permit)
        entered.set()
        await release.wait()
        return {'private_response': 'synthetic only'}
    rig = journal_rig(tmp_path, operation)
    task = asyncio.create_task(apply_journal(rig))
    await asyncio.wait_for(entered.wait(), 2)
    permit = seen[0]
    rig[2].store.retire_local(rig[3], permit.to_dict()['job_id'])
    release.set()
    outcome = await task
    assert outcome['action']['state'] == 'applying'
    with sqlite3.connect(rig[2].store.path) as db:
        assert db.execute('SELECT intent_json FROM brief_action_intents').fetchone()[0] == '{"retired":true}'
        assert db.execute('SELECT permit_json,state FROM brief_effect_attempts').fetchone() == ('{"retired":true}', 'unknown')
        assert db.execute('SELECT kind,evidence_sha256 FROM brief_effect_observations').fetchone() == (
            'transport_response', outcome['pending_effect']['evidence_sha256'])
    with rig[2].store._delivery_transaction(write=False) as db:
        with pytest.raises(SchemaError):
            rig[2]._row(db, permit)  # observation permission is not dispatch permission
    with pytest.raises(SchemaError):
        await apply_journal(rig)
    assert len(seen) == 1


@pytest.mark.parametrize('damage', ['permit_digest', 'intent_digest', 'resource_key',
                                   'owner_id', 'epoch', 'job_state', 'partial_scrub'])
async def test_retired_observation_requires_original_bound_metadata(tmp_path, damage):
    import sqlite3
    async def operation(permit):
        rig[2].store.retire_local(rig[3], permit.to_dict()['job_id'])
        with sqlite3.connect(rig[2].store.path) as db:
            if damage in ('permit_digest', 'owner_id'):
                db.execute(f'UPDATE brief_effect_attempts SET {damage}=?', ('b' * 64,))
            elif damage in ('intent_digest', 'resource_key'):
                db.execute(f'UPDATE brief_action_intents SET {damage}=?', ('b' * 64,))
            elif damage == 'epoch':
                db.execute('UPDATE brief_effect_meta SET epoch=?', ('b' * 64,))
            elif damage == 'job_state':
                db.execute("UPDATE brief_jobs SET state='draft_ready'")
            else:
                db.execute('UPDATE brief_effect_attempts SET permit_json=?', (permit._json,))
        return {'synthetic': True}
    rig = journal_rig(tmp_path, operation)
    with pytest.raises(SchemaError):
        await apply_journal(rig)
    with sqlite3.connect(rig[2].store.path) as db:
        assert db.execute('SELECT count(*) FROM brief_effect_observations').fetchone()[0] == 0
        assert db.execute('SELECT state FROM brief_effect_attempts').fetchone()[0] == 'unknown'
        assert db.execute('SELECT intent_json FROM brief_action_intents').fetchone()[0] == '{"retired":true}'


@pytest.mark.parametrize('policy', ['read_only', 'four_eyes', 'apply_disabled'])
async def test_frozen_effect_respects_existing_approval_policy(tmp_path, policy):
    import sqlite3
    calls = []
    async def operation(permit):
        calls.append(permit)
        return {}
    rig = journal_rig(tmp_path, operation)
    approvals, receipt, journal, scope, chat = rig
    if policy == 'read_only':
        approvals.config = replace(approvals.config,
            actor_permissions=(f'{scope.principal.platform}:{scope.principal.actor_id}:read',))
    elif policy == 'four_eyes':
        approvals.config = replace(approvals.config, four_eyes_enabled=True)
    else:
        approvals.config = replace(approvals.config, apply_enabled=False)
    with pytest.raises(SchemaError):
        await apply_journal(rig)
    assert calls == []
    with sqlite3.connect(journal.store.path) as db:
        assert db.execute('SELECT count(*) FROM brief_action_intents').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM brief_effect_attempts').fetchone()[0] == 0
