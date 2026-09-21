"""Frozen proposals use real revision/adoption and authoritative receipt storage."""
from dataclasses import replace
import json

import pytest

from ostag.briefs.actions import FieldObservation, FrozenFieldChange, ActionProposalService
from ostag.briefs.store import PrivateDestination
from ostag.briefs.table_quality import ResourceKey
from ostag.config import Settings
from ostag.core.approvals import ApprovalService
from ostag.core.run_ledger import RunLedger
from ostag.core.schemas import SchemaError, ActionReceipt
from test_brief_revisions import SCOPE, service, adopt, correct, withdraw


def observation(**changes):
    args = dict(scope=SCOPE, resource=ResourceKey(SCOPE.namespace, 'feishu', 'base', 'table'),
                record_id='record', field_id='客户状态', value=None,
                provider_version='provider-revision-1', snapshot_sha256='a' * 64,
                grant_id='grant', grant_revision=1)
    args.update(changes)
    return FieldObservation(**args)


def change(obs=None, **changes):
    args = dict(observation=obs or observation(), new_value={'状态': ['done', None]},
                job_id='job', revision_id='revision', root_digest='b' * 64, run_id='run_brief',
                proposal_id='action1')
    args.update(changes)
    return FrozenFieldChange(**args)


def rig(tmp_path):
    revisions = service(tmp_path / 'revision.db')
    view = revisions.register(SCOPE, 'job')
    current = [observation()]
    allowed = [True]
    ledger = RunLedger(tmp_path / 'run.jsonl')
    approvals = ApprovalService(Settings(), ledger)
    proposals = ActionProposalService(revisions=revisions, approvals=approvals,
        observe=lambda frozen: current[0], authorize=lambda frozen: allowed[0],
        destination=lambda scope: PrivateDestination(scope.principal, 'private_chat'))
    frozen = change(current[0], root_digest=view.root_digest)
    return proposals, revisions, ledger, frozen, current, allowed


def test_immutable_nested_values_and_null_survive_legacy_wire(tmp_path):
    value = {'items': [None, False, 0, '', [], {}]}
    frozen = change(new_value=value)
    value['items'].clear()
    result = frozen.to_dict()
    result['new_value']['items'].clear()
    receipt = frozen.to_receipt()
    wire = ActionReceipt.from_dict(json.loads(json.dumps(receipt.to_dict())))
    assert json.loads(wire.apply_payload['intent_json'])['new_value'] == {
        'items': [None, False, 0, '', [], {}]}
    receipt.apply_payload.clear()
    assert frozen.to_receipt().apply_payload
    assert frozen.to_receipt().state == 'needs_approval'


def test_adoption_and_action_approval_are_separate(tmp_path):
    proposals, revisions, ledger, frozen, _, _ = rig(tmp_path)
    with pytest.raises(SchemaError):
        proposals.propose(SCOPE, frozen)
    accepted = adopt(revisions, 'revision')
    assert accepted.current.business_approved is False
    receipt = proposals.propose(SCOPE, frozen)
    assert receipt.state == 'needs_approval'
    assert receipt.target_system == 'brief_field_change'
    ledger.record_receipt_state(receipt.with_state('ready_to_apply'),
                               previous_state=receipt.state, actor_id='reviewer')
    assert proposals.propose(SCOPE, frozen).state == 'ready_to_apply'
    assert len(ledger.events()) == 2


@pytest.mark.parametrize('modify', ['target', 'value', 'version', 'grant', 'snapshot', 'revocation'])
def test_current_observation_or_grant_changes_refuse_proposal(tmp_path, modify):
    proposals, revisions, ledger, frozen, current, allowed = rig(tmp_path)
    adopt(revisions, 'revision')
    kwargs = {'target': {'record_id': 'other'}, 'value': {'value': 123},
              'version': {'provider_version': 'new'}, 'grant': {'grant_revision': 2},
              'snapshot': {'snapshot_sha256': 'c' * 64}, 'revocation': {}}[modify]
    current[0] = observation(**kwargs)
    if modify == 'revocation':
        allowed[0] = False
    with pytest.raises(SchemaError):
        proposals.propose(SCOPE, frozen)
    assert not ledger.path.exists()


@pytest.mark.parametrize('operation', ['correct', 'withdraw'])
def test_old_adopted_revision_cannot_replay_after_change(tmp_path, operation):
    proposals, revisions, ledger, frozen, _, _ = rig(tmp_path)
    adopt(revisions, 'revision')
    proposals.propose(SCOPE, frozen)
    if operation == 'correct':
        correct(revisions)
    else:
        withdraw(revisions, 'revision')
    before = ledger.path.read_bytes()
    with pytest.raises(SchemaError):
        proposals.propose(SCOPE, frozen)
    assert ledger.path.read_bytes() == before


def test_changed_payload_gets_new_unapproved_receipt(tmp_path):
    proposals, revisions, ledger, frozen, _, _ = rig(tmp_path)
    adopt(revisions, 'revision')
    first = proposals.propose(SCOPE, frozen)
    ledger.record_receipt_state(first.with_state('ready_to_apply'),
                               previous_state=first.state, actor_id='reviewer')
    second = proposals.propose(SCOPE, change(root_digest=frozen.to_dict()['root_digest'],
                                           new_value='different'), replaces=first.action_id)
    assert first.action_id != second.action_id
    assert second.state == 'needs_approval'
    assert ledger.get_receipt(first.action_id).state == 'stale'
    assert proposals.propose(SCOPE, frozen).state == 'stale'
    with pytest.raises(SchemaError):
        ledger.record_receipt_state(first.with_state('applying'),
                                   previous_state='ready_to_apply', actor_id='reviewer')


def test_replacement_requires_current_receipt_and_cannot_replace_running_effect(tmp_path):
    proposals, revisions, ledger, frozen, _, _ = rig(tmp_path)
    adopt(revisions, 'revision')
    first = proposals.propose(SCOPE, frozen)
    revised = change(root_digest=frozen.to_dict()['root_digest'], new_value='different')
    with pytest.raises(SchemaError):
        proposals.propose(SCOPE, revised)
    ledger.record_receipt_state(first.with_state('applying'),
                               previous_state=first.state, actor_id='executor')
    before = ledger.path.read_bytes()
    with pytest.raises(SchemaError):
        proposals.propose(SCOPE, revised, replaces=first.action_id)
    assert ledger.path.read_bytes() == before


@pytest.mark.parametrize('bad', [float('nan'), object(), {'secret': 'sk-abcdefghijklmnopqrstuv'}])
def test_invalid_or_secret_values_never_become_frozen_change(bad):
    with pytest.raises(SchemaError):
        change(new_value=bad)


def test_provider_version_is_not_optional_or_inferred_from_digest():
    with pytest.raises(SchemaError):
        observation(provider_version=None)


def test_foreign_requester_cannot_use_an_adopted_change(tmp_path):
    from ostag.core.identity import Principal
    proposals, revisions, _, frozen, _, _ = rig(tmp_path)
    adopt(revisions, 'revision')
    with pytest.raises(SchemaError):
        proposals.propose(replace(SCOPE, principal=Principal('feishu', 'other')), frozen)


def test_resolve_rechecks_current_revision_and_real_approval(tmp_path):
    proposals, revisions, ledger, frozen, _, _ = rig(tmp_path)
    adopt(revisions, 'revision')
    receipt = proposals.propose(SCOPE, frozen)
    receipt.apply_payload['intent_json'] = '{}'
    stored, decoded = proposals.resolve(SCOPE, receipt.action_id)
    assert decoded.digest == frozen.digest and stored.state == 'needs_approval'
    ledger.record_receipt_state(stored.with_state('ready_to_apply'),
                               previous_state=stored.state, actor_id='reviewer')
    assert proposals.resolve(SCOPE, stored.action_id)[0].state == 'ready_to_apply'
    correct(revisions)
    with pytest.raises(SchemaError):
        proposals.resolve(SCOPE, stored.action_id)


def test_resolve_refuses_damaged_creation_history(tmp_path):
    proposals, revisions, ledger, frozen, _, _ = rig(tmp_path)
    adopt(revisions, 'revision')
    receipt = proposals.propose(SCOPE, frozen)
    record = json.loads(ledger.path.read_text())
    record['payload'].pop('proposal_binding')
    ledger.path.write_text(json.dumps(record) + '\n')
    with pytest.raises(SchemaError):
        proposals.resolve(SCOPE, receipt.action_id)


def test_json_boolean_and_integer_observations_are_distinct(tmp_path):
    proposals, revisions, _, frozen, current, _ = rig(tmp_path)
    adopt(revisions, 'revision')
    frozen = change(observation(value=True), root_digest=frozen.to_dict()['root_digest'])
    current[0] = observation(value=1)
    with pytest.raises(SchemaError):
        proposals.propose(SCOPE, frozen)


@pytest.mark.parametrize('cached_receipt', [False, True])
async def test_existing_chat_approval_works_but_superseded_action_cannot_apply(tmp_path, cached_receipt):
    from ostag.core.receipts import parse_receipt_command
    proposals, revisions, ledger, frozen, _, _ = rig(tmp_path)
    adopt(revisions, 'revision')
    approvals = proposals.approvals
    approvals.config = replace(approvals.config, apply_enabled=True,
        actor_permissions=(f'feishu:{SCOPE.principal.actor_id}:write',))
    approvals.channel_brain.root = tmp_path / 'channel_brain'
    effects = []
    class Executor:
        async def apply(self, receipt):
            effects.append(receipt)
            return 'unexpected effect'
    approvals.executors['brief_field_change'] = Executor()
    receipt = proposals.propose(SCOPE, frozen)
    approve = parse_receipt_command(receipt.commands['approve'])
    assert approve is not None
    result = await approvals.execute(approve, principal=SCOPE.principal,
                                     chat_id='private_chat')
    assert result['action']['state'] == 'ready_to_apply'
    def replacement():
        result = proposals.propose(SCOPE,
            change(root_digest=frozen.to_dict()['root_digest'], new_value='changed'),
            replaces=receipt.action_id)
        assert result.state == 'needs_approval'
    if cached_receipt:
        original = approvals._resolve_receipt
        def stale_read(*args):
            cached = original(*args)
            replacement()
            return cached
        approvals._resolve_receipt = stale_read
    else:
        replacement()
    apply = parse_receipt_command(receipt.commands['apply'])
    assert apply is not None
    with pytest.raises(SchemaError):
        await approvals.execute(apply, principal=SCOPE.principal, chat_id='private_chat')
    assert effects == []
    assert ledger.get_receipt(receipt.action_id).state == 'stale'
