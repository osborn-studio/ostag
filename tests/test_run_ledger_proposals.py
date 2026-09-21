"""Unique proposal creation shares the existing authoritative approval ledger."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json

import pytest

from ostag.core.identity import Principal
from ostag.core.run_ledger import RunLedger
from ostag.core.schemas import ActionReceipt, SchemaError


def receipt(**changes):
    return replace(ActionReceipt(
        action_id='action_frozen' + 'a' * 64, run_id='run_brief',
        title='Review one field', target_system='feishu_base',
        target_object='base/table/record/field', mutation_type='update',
        principal=Principal('feishu', 'requester'), state='needs_approval',
        apply_payload={'kind': 'brief-field-change-v1', 'intent_json': '{"new":null}'},
    ), **changes)


def once(ledger, value=None, **changes):
    args = dict(chat_id='private_chat', platform='feishu', proposer_id='requester')
    args.update(changes)
    return ledger.record_receipt_once(value or receipt(), **args)


def test_same_proposal_preserves_approval_and_unique_creation(tmp_path):
    ledger = RunLedger(tmp_path / 'run.jsonl')
    first = once(ledger)
    ledger.record_receipt_state(first.with_state('ready_to_apply'),
                               previous_state='needs_approval', actor_id='reviewer')
    replay = once(RunLedger(ledger.path), receipt(created_at='2026-09-21T00:00:00+00:00'))
    assert replay.state == 'ready_to_apply'
    assert len([e for e in ledger.events() if e['event_type'] == 'action_proposed']) == 1


def test_concurrent_independent_ledger_instances_create_one_receipt(tmp_path):
    path = tmp_path / 'run.jsonl'
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: once(RunLedger(path)), range(24)))
    assert len({r.action_id for r in results}) == 1
    assert len(RunLedger(path).events()) == 1


@pytest.mark.parametrize('changes', [
    {'apply_payload': {'kind': 'brief-field-change-v1', 'intent_json': '{"new":1}'}},
    {'target_object': 'base/table/another/field'},
    {'run_id': 'run_other'},
    {'principal': Principal('feishu', 'other')},
])
def test_same_id_cannot_rebind_approved_intent(tmp_path, changes):
    ledger = RunLedger(tmp_path / 'run.jsonl')
    once(ledger)
    before = ledger.path.read_bytes()
    with pytest.raises(SchemaError):
        once(ledger, receipt(**changes))
    assert ledger.path.read_bytes() == before


def test_source_channel_is_part_of_unique_binding(tmp_path):
    ledger = RunLedger(tmp_path / 'run.jsonl')
    once(ledger)
    with pytest.raises(SchemaError):
        once(ledger, chat_id='another_chat')


def test_legacy_propose_cannot_reset_reserved_receipt(tmp_path):
    ledger = RunLedger(tmp_path / 'run.jsonl')
    first = once(ledger)
    ledger.record_receipt_state(first.with_state('ready_to_apply'),
                               previous_state=first.state, actor_id='reviewer')
    before = ledger.path.read_bytes()
    with pytest.raises(SchemaError):
        ledger.record_receipt(receipt(), platform='feishu', proposer_id='requester')
    assert ledger.path.read_bytes() == before
    assert ledger.get_receipt(first.action_id).state == 'ready_to_apply'


def test_redaction_cannot_silently_change_frozen_proposal(tmp_path):
    ledger = RunLedger(tmp_path / 'run.jsonl')
    with pytest.raises(SchemaError):
        once(ledger, receipt(apply_payload={'secret': 'sk-abcdefghijklmnopqrstuv'}))
    assert not ledger.path.exists()


@pytest.mark.parametrize('corruption', ['truncated', 'duplicate', 'binding'])
def test_uncertain_ledger_refuses_unique_creation(tmp_path, corruption):
    ledger = RunLedger(tmp_path / 'run.jsonl')
    once(ledger)
    content = ledger.path.read_text()
    if corruption == 'truncated':
        content += '{"unterminated":'
    elif corruption == 'duplicate':
        content += content
    else:
        record = json.loads(content)
        record['payload']['receipt']['target_object'] = 'tampered'
        content = json.dumps(record) + '\n'
    ledger.path.write_text(content)
    with pytest.raises(SchemaError):
        once(ledger)
    assert ledger.path.read_text() == content


def _process_once(path, connection):
    connection.send('ready')
    assert connection.recv() == 'go'
    connection.send(once(RunLedger(path)).action_id)
    connection.close()


def test_independent_processes_share_unique_creation(tmp_path):
    import multiprocessing
    ctx = multiprocessing.get_context('spawn')
    pairs = [ctx.Pipe() for _ in range(3)]
    children = [ctx.Process(target=_process_once, args=(tmp_path / 'run.jsonl', child))
                for _, child in pairs]
    try:
        for process in children:
            process.start()
        for parent, child in pairs:
            child.close()
            assert parent.poll(20) and parent.recv() == 'ready'
        for parent, _ in pairs:
            parent.send('go')
        results = []
        for parent, _ in pairs:
            assert parent.poll(10)
            results.append(parent.recv())
        for process in children:
            process.join(10)
            assert process.exitcode == 0
        assert len(set(results)) == 1
        assert len(RunLedger(tmp_path / 'run.jsonl').events()) == 1
    finally:
        for process in children:
            if process.is_alive():
                process.kill()
                process.join(5)
            process.close()
        for parent, _ in pairs:
            parent.close()


def test_failed_sync_is_not_assumed_durable_on_replay(tmp_path, monkeypatch):
    import ostag.core.run_ledger as module
    real = module.os.fsync
    calls = []
    def sync(fd):
        calls.append(fd)
        if len(calls) == 1:
            raise OSError('synthetic sync failure')
        return real(fd)
    monkeypatch.setattr(module.os, 'fsync', sync)
    ledger = RunLedger(tmp_path / 'run.jsonl')
    with pytest.raises(OSError):
        once(ledger)
    assert once(ledger).state == 'needs_approval'
    assert len(calls) == 3  # failed file sync, successful file+directory retry
    assert len(ledger.events()) == 1


def test_lock_contention_has_bounded_refusal(tmp_path, monkeypatch):
    import fcntl
    import ostag.core.run_ledger as module
    path = tmp_path / 'run.jsonl'
    with path.open('a') as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        clock = iter([0, 1])
        monkeypatch.setattr(module, 'monotonic', lambda: next(clock))
        with pytest.raises(SchemaError, match='could not be recorded') as error:
            once(RunLedger(path))
        assert error.value.code == 'ledger_busy'
    assert path.read_bytes() == b''


def test_competing_replacements_cannot_overwrite_each_other(tmp_path):
    path = tmp_path / 'run.jsonl'
    group = 'd' * 64
    first = once(RunLedger(path), proposal_scope=group)
    def replace_once(letter):
        value = receipt(action_id='action_frozen' + letter * 64,
                        apply_payload={'new': letter})
        try:
            return once(RunLedger(path), value, proposal_scope=group, replaces=first.action_id)
        except SchemaError as exc:
            return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(replace_once, ['b', 'c']))
    assert sum(type(r) is ActionReceipt for r in results) == 1
    assert results.count('frozen_proposal_conflict') == 1
    ledger = RunLedger(path)
    assert len(ledger.events()) == 2  # new proposal + supersession share ONE event
    assert ledger.get_receipt(first.action_id).state == 'stale'


def test_legacy_append_waits_for_frozen_writer_without_busy_failure(tmp_path, monkeypatch):
    import fcntl
    import threading
    import ostag.core.run_ledger as module
    from ostag.core.schemas import LedgerEvent
    path = tmp_path / 'run.jsonl'
    attempted = threading.Event()
    real_flock = fcntl.flock
    def flock(fd, operation):
        if operation & fcntl.LOCK_NB:
            try:
                return real_flock(fd, operation)
            finally:
                attempted.set()
        if operation != fcntl.LOCK_UN:
            attempted.set()
        return real_flock(fd, operation)
    # Force any bounded lock acquisition to exhaust immediately. Legacy writes
    # must wait for the owner instead of adopting the new frozen busy failure.
    clock = iter([0, 1])
    monkeypatch.setattr(module, 'monotonic', lambda: next(clock))
    with path.open('a') as owner:
        real_flock(owner.fileno(), fcntl.LOCK_EX)
        monkeypatch.setattr(module.fcntl, 'flock', flock)
        with ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(RunLedger(path).append,
                                 LedgerEvent('evt_legacy', 'run_old', 'security_event', {}))
            try:
                assert attempted.wait(2)
            finally:
                real_flock(owner.fileno(), fcntl.LOCK_UN)
            assert result.result(timeout=2).event_id == 'evt_legacy'
    assert len(RunLedger(path).events()) == 1


@pytest.mark.parametrize('failure', [OSError('synthetic disk failure'),
    SchemaError('ledger_busy', 'synthetic busy', 'retry')])
async def test_audit_failure_does_not_mask_permission_denial(tmp_path, monkeypatch, caplog, failure):
    from tests.test_approvals import _service
    from ostag.core.receipts import ReceiptCommand
    service, ledger = _service(tmp_path)
    def fail(*args, **kwargs):
        raise failure
    monkeypatch.setattr(ledger, 'record_security_event', fail)
    with pytest.raises(SchemaError) as error:
        await service.execute(ReceiptCommand('approve', '1'), actor_id='viewer', chat_id='chat1')
    assert error.value.code == 'permission_denied'
    assert 'security_audit_write_failed' in caplog.text
    assert 'synthetic' not in caplog.text


def test_streaming_history_crosses_old_capacity_limit(tmp_path):
    from ostag.core.schemas import LedgerEvent
    ledger = RunLedger(tmp_path / 'run.jsonl')
    first = once(ledger)
    line = json.dumps(LedgerEvent('evt_padding', 'run_old', 'security_event',
        {'detail': 'x' * (1024 * 1024)}).to_dict()) + '\n'
    with ledger.path.open('a') as handle:
        for _ in range(65):
            handle.write(line)
    assert ledger.path.stat().st_size > 64 * 1024 * 1024
    assert once(ledger).action_id == first.action_id


def recover(ledger, **changes):
    import hashlib
    args = dict(expected_sha256=hashlib.sha256(ledger.path.read_bytes()).hexdigest(),
                operator=Principal('osstack', 'operator'), reason='verified maintenance incident')
    args.update(changes)
    return ledger.recover_frozen_history(**args)


@pytest.mark.parametrize('tail', [b'{"event_id":', b'\xff\xfe', b'{"bad":true}\n'])
def test_recovery_preserves_bytes_and_quarantines_previous_approvals(tmp_path, tail):
    ledger = RunLedger(tmp_path / 'run.jsonl')
    first = once(ledger)
    ledger.record_receipt_state(first.with_state('ready_to_apply'),
        previous_state=first.state, actor_id='reviewer')
    original = ledger.path.read_bytes() + tail
    ledger.path.write_bytes(original)
    with pytest.raises(SchemaError):
        once(ledger)
    epoch = recover(ledger)
    assert ledger.path.read_bytes().startswith(original)
    assert ledger.frozen_epoch() == epoch != 'initial'
    assert once(ledger).state == 'blocked'
    with pytest.raises(SchemaError):
        ledger.record_receipt_state(first.with_state('applying'),
            previous_state='ready_to_apply', actor_id='reviewer')
    # An unknown proposal might have been hidden in the damaged tail. Old epoch
    # cannot create it, even with a new unique id.
    unseen = receipt(action_id='action_frozen' + 'b' * 64)
    with pytest.raises(SchemaError) as error:
        once(ledger, unseen)
    assert error.value.code == 'frozen_epoch_mismatch'
    current = replace(unseen, apply_payload={**unseen.apply_payload, 'ledger_epoch': epoch})
    assert once(ledger, current).state == 'needs_approval'


def test_recovery_requires_exact_operator_snapshot(tmp_path):
    ledger = RunLedger(tmp_path / 'run.jsonl')
    once(ledger)
    before = ledger.path.read_bytes()
    with pytest.raises(SchemaError):
        recover(ledger, expected_sha256='0' * 64)
    assert ledger.path.read_bytes() == before


def test_partial_recovery_append_never_reenables_old_epoch(tmp_path, monkeypatch):
    ledger = RunLedger(tmp_path / 'run.jsonl')
    once(ledger)
    with ledger.path.open('ab') as h:
        h.write(b'{"unfinished":')
    real = ledger._write_locked
    def torn(handle, record, **kwargs):
        handle.write(b'{"recovery_torn":')
        handle.flush()
        raise OSError('synthetic crash during recovery')
    monkeypatch.setattr(ledger, '_write_locked', torn)
    with pytest.raises(OSError):
        recover(ledger)
    with pytest.raises(SchemaError):
        once(ledger)
    monkeypatch.setattr(ledger, '_write_locked', real)
    epoch = recover(ledger)
    assert ledger.frozen_epoch() == epoch
    assert once(ledger).state == 'blocked'


def test_recovery_marker_cannot_be_forged_or_rebound(tmp_path):
    from ostag.core.schemas import LedgerEvent
    ledger = RunLedger(tmp_path / 'run.jsonl')
    once(ledger)
    with pytest.raises(SchemaError):
        ledger.append(LedgerEvent('evt_fake', 'run_recovery', 'security_event',
                                 {'kind': 'frozen_history_recovered'}))
    recover(ledger)
    lines = ledger.path.read_bytes().splitlines(keepends=True)
    marker = json.loads(lines[-1])
    marker['payload']['prefix_sha256'] = '0' * 64
    ledger.path.write_bytes(b''.join(lines[:-1]) + json.dumps(marker).encode() + b'\n')
    with pytest.raises(SchemaError):
        ledger.frozen_epoch()


def test_recovery_does_not_reauthorize_same_uncertain_logical_slot(tmp_path):
    ledger = RunLedger(tmp_path / 'run.jsonl')
    group = 'e' * 64
    old = once(ledger, proposal_scope=group)
    epoch = recover(ledger)
    new = receipt(action_id='action_frozen' + 'c' * 64, apply_payload={'ledger_epoch': epoch})
    with pytest.raises(SchemaError):
        once(ledger, new, proposal_scope=group, replaces=old.action_id)
    assert ledger.get_frozen_receipt(old.action_id, chat_id='private_chat',
                                    platform='feishu').state == 'blocked'


def test_frozen_state_transition_requires_sync_before_return(tmp_path, monkeypatch):
    import ostag.core.run_ledger as module
    ledger = RunLedger(tmp_path / 'run.jsonl')
    first = once(ledger)
    def fail(fd):
        raise OSError('synthetic state sync failure')
    monkeypatch.setattr(module.os, 'fsync', fail)
    with pytest.raises(OSError):
        ledger.record_receipt_state(first.with_state('applying'),
                                   previous_state=first.state, actor_id='reviewer')
    # A caller must not execute after this exception. The uncertain applying
    # record is conservatively retained, never rolled back to an approved state.
    assert ledger.get_receipt(first.action_id).state == 'applying'


@pytest.mark.parametrize('payload', [{}, {'latency': float('inf')}, {'latency': float('nan')}])
def test_valid_legacy_audit_payload_does_not_quarantine_frozen_history(tmp_path, payload):
    from ostag.core.schemas import LedgerEvent
    ledger = RunLedger(tmp_path / 'run.jsonl')
    first = once(ledger)
    ledger.append(LedgerEvent('evt_legacy', 'run_old', 'security_event', payload))
    ledger.record_receipt_state(first.with_state('ready_to_apply'),
                               previous_state=first.state, actor_id='reviewer')
    assert ledger.frozen_epoch() == 'initial'
    assert once(ledger).state == 'ready_to_apply'
    assert ledger.get_frozen_receipt(first.action_id, chat_id='private_chat',
                                    platform='feishu').state == 'ready_to_apply'


def test_broken_supersession_does_not_break_unrelated_legacy_listing(tmp_path):
    ledger = RunLedger(tmp_path / 'run.jsonl')
    first = once(ledger, proposal_scope='c' * 64)
    once(ledger, receipt(action_id='action_frozen' + 'b' * 64),
         proposal_scope='c' * 64, replaces=first.action_id)
    legacy = receipt(action_id='action_legacy')
    ledger.record_receipt(legacy, platform='feishu', proposer_id='requester')
    lines = ledger.path.read_bytes().splitlines(keepends=True)
    bad = json.loads(lines[1])
    bad['payload']['supersedes'] = 'action_frozenmissing'
    lines[1] = json.dumps(bad).encode() + b'\n'
    ledger.path.write_bytes(b''.join(lines))
    assert ledger.get_receipt(legacy.action_id).state == 'needs_approval'
    assert ledger.get_receipt(first.action_id).state == 'blocked'
    with pytest.raises(SchemaError):
        ledger.get_frozen_receipt(first.action_id, chat_id='private_chat', platform='feishu')
    recover(ledger)
    assert ledger.get_receipt(legacy.action_id).state == 'needs_approval'
    assert once(ledger, proposal_scope='c' * 64).state == 'blocked'


def test_unrelated_legacy_receipt_can_retain_nonfinite_payload(tmp_path):
    ledger = RunLedger(tmp_path / 'run.jsonl')
    first = once(ledger)
    legacy = receipt(action_id='action_legacy', apply_payload={'latency': float('inf')})
    ledger.record_receipt(legacy, platform='feishu', proposer_id='requester')
    assert once(ledger).action_id == first.action_id
    assert ledger.get_receipt(legacy.action_id).apply_payload['latency'] == float('inf')


def test_legacy_wire_event_with_pruned_payload_remains_readable(tmp_path):
    from ostag.core.schemas import LedgerEvent
    ledger = RunLedger(tmp_path / 'run.jsonl')
    first = once(ledger)
    wire = LedgerEvent('evt_pruned', 'run_old', 'security_event', {}).to_dict()
    assert 'payload' not in wire  # valid public wire, even though append restores {}
    with ledger.path.open('ab') as handle:
        handle.write(json.dumps(wire).encode() + b'\n')
    assert once(ledger).action_id == first.action_id
