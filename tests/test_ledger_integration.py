"""Cross-module regression probes from the U02 integration review."""
import pytest

from ostag.core.ledger_factory import configured_ledger
from ostag.core.run_ledger import RunLedger
from ostag.core.schemas import SchemaError
from test_ledger_coordination import prepare
from test_brief_generation import reserve, SCOPE, WORKER, Usage, admit, at


@pytest.mark.parametrize('injected', [False, True])
def test_managed_policy_rejects_omitted_runtime_domain(tmp_path, injected):
    path, _ = prepare(tmp_path)
    other = tmp_path / 'omitted.jsonl'
    with pytest.raises(SchemaError) as raised:
        configured_ledger(other, managed_paths=(path,),
                          existing=RunLedger(other) if injected else None)
    assert raised.value.code == 'ledger_coordination_unavailable'
    assert not other.exists()


@pytest.mark.parametrize('missing', ['monitor', 'runner'])
def test_server_checks_all_reachable_domains_before_opening_database(tmp_path, missing):
    from fastapi.testclient import TestClient
    from ostag.config import Settings
    from ostag.work_monitor.server import create_app
    from test_managed_ledgers import provision
    monitor = tmp_path / 'monitor' / 'run_ledger.jsonl'
    runner = tmp_path / 'runner.jsonl'
    listed = runner if missing == 'monitor' else monitor
    provision(listed)
    database = monitor.with_name('monitor.sqlite3')
    app = create_app(settings=Settings(
        managed_ledger_paths=(listed,), monitor_server_db=database,
        run_ledger_path=runner, monitor_scheduler_enabled=False,
        feishu_use_websocket=True, feishu_app_id='synthetic-app',
        feishu_app_secret='synthetic-secret'))
    with pytest.raises(SchemaError):
        with TestClient(app):
            pytest.fail('partially configured domains admitted')
    assert not database.exists()


def test_managed_readers_share_an_inode_snapshot(tmp_path):
    path, coordination = prepare(tmp_path)
    first = RunLedger(path, coordination=coordination)
    second = RunLedger(path, coordination=coordination)
    with first._exclusive(read_only=True):
        assert second.events() == []


def test_managed_append_excludes_an_active_reader(tmp_path, monkeypatch):
    import fcntl
    from ostag.core.schemas import LedgerEvent
    path, coordination = prepare(tmp_path)
    reader = RunLedger(path, coordination=coordination)
    writer = RunLedger(path, coordination=coordination)
    event = LedgerEvent('evt_probe', 'run_probe', 'security_event',
                        {'kind': 'synthetic'}, '2026-09-21T00:00:01Z')
    flock = fcntl.flock
    with reader._exclusive(read_only=True):
        # Probe the real kernel lock without waiting for this test's own reader.
        with monkeypatch.context() as patch:
            patch.setattr(fcntl, 'flock', lambda fd, mode: flock(fd, mode | fcntl.LOCK_NB))
            with pytest.raises(BlockingIOError):
                writer.append(event)
        assert path.read_bytes() == b''
    writer.append(event)
    assert reader.events()[0]['event_id'] == 'evt_probe'


def test_effect_migration_preserves_cost_lifecycle(tmp_path):
    from ostag.briefs.store import BriefStore
    store = BriefStore(tmp_path / 'briefs.db')
    admit(store)
    clock = at
    job = store.claim_one(WORKER, clock=clock)
    store.start_generating(job.lease, clock=clock)
    generation_setup = (store, job, [0], clock)
    store.enable_delivery()
    reservation = reserve(generation_setup)
    store.enable_effects(backup_path=tmp_path / 'schema4-backup.db')
    assert store.cost_record(SCOPE, job.job_id, 'call-1') == reservation
    assert not store.has_uncertain_cost(WORKER)
    store.start_cost(job.lease, reservation.reservation_id,
                     daily_limit_micros=100000, clock=clock)
    assert store.has_uncertain_cost(WORKER)
    store.mark_cost_unknown(WORKER, reservation.reservation_id, 'timeout', clock=clock)
    assert store.has_uncertain_cost(WORKER)
    settled = store.settle_cost(WORKER, reservation.reservation_id,
                                Usage(100, 50, 200), clock=clock)
    assert settled.charged_micros == 200
    assert not store.has_uncertain_cost(WORKER)
    later = reserve(generation_setup, 'call-2')
    assert store.release_cost(WORKER, later.reservation_id, clock=clock).state == 'released'
