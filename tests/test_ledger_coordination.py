"""Opt-in stable lock foundation; not old-worker exclusion or migration authority."""
import os
from pathlib import Path

import pytest

from ostag.core.run_ledger import RunLedger
from ostag.core.schemas import SchemaError


def prepare(tmp_path):
    from ostag.core.ledger_coordination import LedgerCoordination
    path = tmp_path/'run.jsonl'
    path.write_bytes(b'')
    path.chmod(0o600)
    return path, LedgerCoordination.install(path)


def test_lock_install_is_explicit_private_and_does_not_change_ledger(tmp_path):
    from ostag.core.ledger_coordination import LedgerCoordination
    path, coordination = prepare(tmp_path)
    before = path.read_bytes()
    assert coordination.path.stat().st_mode & 0o777 == 0o600
    assert LedgerCoordination.open(path).identity == coordination.identity
    assert path.read_bytes() == before
    with RunLedger(path, coordination=coordination)._exclusive(read_only=True):
        pass
    assert path.read_bytes() == before


@pytest.mark.parametrize('damage', ['symlink', 'hardlink', 'permissions', 'removed', 'replacement'])
def test_lock_identity_or_filesystem_changes_fail_closed(tmp_path, damage):
    path, coordination = prepare(tmp_path)
    if damage == 'symlink':
        coordination.path.unlink()
        coordination.path.symlink_to(path)
    elif damage == 'hardlink':
        os.link(coordination.path, tmp_path/'alias')
    elif damage == 'permissions':
        coordination.path.chmod(0o666)
    elif damage == 'removed':
        coordination.path.unlink()
    else:
        candidate = tmp_path/'new-lock'
        candidate.write_bytes(b'')
        candidate.chmod(0o600)
        os.replace(candidate, coordination.path)
    with pytest.raises(SchemaError):
        with RunLedger(path, coordination=coordination)._exclusive():
            pass
    assert path.read_bytes() == b''


def test_coordination_cannot_bind_another_ledger(tmp_path):
    path, coordination = prepare(tmp_path)
    with pytest.raises(SchemaError):
        RunLedger(tmp_path/'other.jsonl', coordination=coordination)


def test_missing_coordination_open_is_read_only(tmp_path):
    from ostag.core.ledger_coordination import LedgerCoordination
    with pytest.raises(SchemaError):
        LedgerCoordination.open(tmp_path/'missing.jsonl')
    assert list(tmp_path.iterdir()) == []


def test_stable_and_inode_waits_share_one_bounded_deadline(tmp_path):
    import fcntl
    import time
    path, coordination = prepare(tmp_path)
    descriptor = os.open(coordination.path, os.O_RDONLY)
    fcntl.flock(descriptor, fcntl.LOCK_EX)
    try:
        started = time.monotonic()
        with pytest.raises(SchemaError) as error:
            with RunLedger(path, coordination=coordination)._exclusive():
                pass
        elapsed = time.monotonic()-started
        assert error.value.code == 'ledger_busy'
        assert .4 <= elapsed < 2
    finally:
        os.close(descriptor)


@pytest.mark.parametrize('damage', ['symlink', 'hardlink', 'removed', 'permissions'])
def test_coordinated_data_file_is_not_recreated_or_aliased(tmp_path, damage):
    path, coordination = prepare(tmp_path)
    if damage == 'symlink':
        path.unlink()
        path.symlink_to(coordination.path)
    elif damage == 'hardlink':
        os.link(path, tmp_path/'data-alias')
    elif damage == 'removed':
        path.unlink()
    else:
        path.chmod(0o666)
    with pytest.raises(SchemaError):
        with RunLedger(path, coordination=coordination)._exclusive():
            pytest.fail('invalid data file entered')
    if damage == 'removed':
        assert not path.exists()
    assert coordination.path.read_bytes() == b''


def test_bound_absolute_ledger_does_not_follow_later_cwd(tmp_path, monkeypatch):
    path, coordination = prepare(tmp_path)
    monkeypatch.chdir(tmp_path)
    ledger = RunLedger(Path('run.jsonl'), coordination=coordination)
    other = tmp_path/'other'
    other.mkdir()
    monkeypatch.chdir(other)
    with ledger._exclusive():
        pass
    assert not (other/'run.jsonl').exists()


@pytest.mark.parametrize('replacement', ['ledger', 'lock'])
def test_waiting_process_opens_new_generation_only_after_stable_lock(tmp_path, replacement):
    import json
    import select
    import subprocess
    import sys
    from ostag.core.schemas import LedgerEvent
    path, coordination = prepare(tmp_path)
    before = (json.dumps(LedgerEvent('evt_before', 'run_test', 'security_event',
                                     {'kind': 'synthetic'}, '2026-09-21T00:00:00Z').to_dict())+'\n').encode()
    path.write_bytes(before)
    old_descriptor = os.open(path, os.O_RDONLY)
    script = '''
import sys
from pathlib import Path
from ostag.core.ledger_coordination import LedgerCoordination
from ostag.core.run_ledger import RunLedger
from ostag.core.schemas import LedgerEvent, SchemaError
path=Path(sys.argv[1])
coordination=LedgerCoordination.open(path)
ledger=RunLedger(path, coordination=coordination)
print('READY', flush=True)
try:
 ledger.append(LedgerEvent('evt_child', 'run_test', 'security_event',
                         {'kind':'synthetic'}, '2026-09-21T00:00:01Z'))
except SchemaError:
 sys.exit(20)
'''
    child = None
    try:
        with coordination.hold(exclusive=True):
            child = subprocess.Popen([sys.executable, '-c', script, str(path)],
                cwd=Path(__file__).resolve().parents[1], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            assert select.select([child.stdout], [], [], 10)[0], 'child did not initialize'
            assert child.stdout.readline() == b'READY\n'
            candidate = tmp_path/'candidate.jsonl'
            candidate.write_bytes(b'')
            candidate.chmod(0o600)
            os.replace(candidate, path if replacement == 'ledger' else coordination.path)
            # Synthetic replacement only; no production maintenance API.
        _, errors = child.communicate(timeout=10)
        assert child.returncode == (0 if replacement == 'ledger' else 20), errors.decode()
        assert os.read(old_descriptor, 65536) == before
        records = [json.loads(line) for line in path.read_text().splitlines()]
        assert len(records) == 1
        assert records[0]['event_id'] == ('evt_child' if replacement == 'ledger' else 'evt_before')
    finally:
        os.close(old_descriptor)
        if child is not None and child.poll() is None:
            child.kill()
            child.communicate()


def test_coordinated_listing_never_treats_lost_domain_as_empty(tmp_path):
    path, coordination = prepare(tmp_path)
    ledger = RunLedger(path, coordination=coordination)
    assert ledger.events() == []
    path.unlink()
    with pytest.raises(SchemaError):
        ledger.events()
    assert not path.exists()


def test_coordinated_listing_waits_on_stable_lock(tmp_path):
    path, coordination = prepare(tmp_path)
    with coordination.hold(exclusive=True):
        with pytest.raises(SchemaError) as error:
            RunLedger(path, coordination=coordination).events()
        assert error.value.code == 'ledger_busy'
