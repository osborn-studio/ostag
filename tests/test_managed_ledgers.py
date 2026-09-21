"""Managed host admission, not proof that old binaries have stopped."""
import json

import pytest

from ostag.config import Settings, load_settings
from ostag.core.ledger_coordination import LedgerCoordination
from ostag.core.run_ledger import RunLedger
from ostag.core.schemas import SchemaError


def provision(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'')
    path.chmod(0o600)
    return LedgerCoordination.install(path)


def test_managed_factory_requires_explicit_existing_domain(tmp_path):
    from ostag.core.ledger_factory import configured_ledger
    path = tmp_path / 'run.jsonl'
    with pytest.raises(SchemaError):
        configured_ledger(path, managed_paths=(path,))
    assert not path.exists()
    coord = provision(path)
    ledger = configured_ledger(path, managed_paths=(path,))
    assert ledger.coordination == coord
    coord.path.unlink()
    with pytest.raises(SchemaError):
        configured_ledger(path, managed_paths=(path,))
    assert not coord.path.exists()


def test_default_factory_retains_legacy_without_creation(tmp_path):
    from ostag.core.ledger_factory import configured_ledger
    path = tmp_path / 'missing' / 'run.jsonl'
    ledger = configured_ledger(path)
    assert ledger.coordination is None
    assert not path.parent.exists()


@pytest.mark.parametrize('bad', ['relative.jsonl', '/tmp/../ledger', '', 1, None])
def test_settings_reject_invalid_managed_paths(bad):
    with pytest.raises(ValueError):
        Settings(managed_ledger_paths=(bad,)).validate()


def test_env_policy_is_json_array_and_duplicate_paths_rejected(tmp_path, monkeypatch):
    path = tmp_path / 'run.jsonl'
    monkeypatch.setenv('OSTAG_MANAGED_LEDGER_PATHS', json.dumps([str(path)]))
    assert load_settings().managed_ledger_paths == (path,)
    for raw in ('null', '"/tmp/ledger"', '{}', '[1]', 'invalid', json.dumps([str(path)] * 2)):
        monkeypatch.setenv('OSTAG_MANAGED_LEDGER_PATHS', raw)
        with pytest.raises(ValueError):
            load_settings()


def test_injected_legacy_ledger_cannot_bypass_policy(tmp_path):
    from ostag.core.ledger_factory import configured_ledger
    path = tmp_path / 'run.jsonl'
    coord = provision(path)
    legacy = RunLedger(path)
    with pytest.raises(SchemaError):
        configured_ledger(path, managed_paths=(path,), existing=legacy)
    supplied = RunLedger(path, coordination=coord)
    assert configured_ledger(path, managed_paths=(path,), existing=supplied) is supplied
    with pytest.raises(SchemaError):
        configured_ledger(tmp_path / 'different.jsonl', managed_paths=(path,), existing=supplied)


def test_multiple_domains_and_missing_other_domain_are_checked(tmp_path):
    from ostag.core.ledger_factory import configured_ledger
    main, monitor = tmp_path/'main.jsonl', tmp_path/'monitor.jsonl'
    provision(main)
    with pytest.raises(SchemaError):
        configured_ledger(main, managed_paths=(main, monitor))
    provision(monitor)
    for path in (main, monitor):
        assert configured_ledger(path, managed_paths=(main, monitor)).coordination.ledger_path == path


@pytest.mark.parametrize('alias', ['symlink', 'hardlink'])
def test_alias_cannot_select_unmanaged_fallback(tmp_path, alias):
    import os
    from ostag.core.ledger_factory import configured_ledger
    path = tmp_path/'run.jsonl'
    provision(path)
    other = tmp_path/'alias.jsonl'
    if alias == 'symlink':
        other.symlink_to(path)
    else:
        os.link(path, other)
    with pytest.raises(SchemaError):
        configured_ledger(other, managed_paths=(path,))


def test_runner_and_auto_base_use_managed_ledger(tmp_path):
    from ostag.core.runner import OSTagRunner
    path = tmp_path/'run.jsonl'
    coord = provision(path)
    config = Settings(run_ledger_path=path, managed_ledger_paths=(path,),
                      audit_log=tmp_path/'audit.log')
    runner = OSTagRunner(config)
    assert runner.run_ledger.coordination == coord
    assert runner.auto_base.run_ledger.coordination == coord
    with pytest.raises(SchemaError):
        OSTagRunner(config, run_ledger=RunLedger(path))


def test_monitor_service_and_scheduler_use_store_domain(tmp_path):
    from ostag.work_monitor.store import MonitorStore
    from ostag.work_monitor.identity import IdentityStore
    from ostag.work_monitor.service import WorkMonitorService
    from ostag.work_monitor.scheduler import WorkMonitorScheduler
    path = tmp_path/'run_ledger.jsonl'
    coord = provision(path)
    store = MonitorStore(tmp_path/'monitor.sqlite3')
    identities = IdentityStore(store.path)
    service = WorkMonitorService(store, identities, managed_ledger_paths=(path,))
    scheduler = WorkMonitorScheduler(store, identities, {}, managed_ledger_paths=(path,))
    assert service.ledger.coordination == coord
    assert scheduler.ledger.coordination == coord
    with pytest.raises(SchemaError):
        WorkMonitorService(store, identities, managed_ledger_paths=(path,), ledger=RunLedger(path))


def test_injected_approvals_cannot_hide_an_uncoordinated_writer(tmp_path):
    from ostag.core.runner import OSTagRunner
    from ostag.core.approvals import ApprovalService
    path = tmp_path/'run.jsonl'
    provision(path)
    config = Settings(run_ledger_path=path, managed_ledger_paths=(path,), audit_log=tmp_path/'audit.log')
    with pytest.raises(SchemaError):
        OSTagRunner(config, approvals=ApprovalService(config, RunLedger(path)))


def test_ambient_followups_use_configured_domain(tmp_path):
    from ostag.core.ambient import AmbientScheduler
    path = tmp_path/'run.jsonl'
    coord = provision(path)
    config = Settings(run_ledger_path=path, managed_ledger_paths=(path,), audit_log=tmp_path/'audit.log')
    scheduler = AmbientScheduler(config, adapter=object())
    assert scheduler.followups.ledger.coordination == coord


def test_missing_managed_domain_refuses_lifespan_before_database_creation(tmp_path):
    from fastapi.testclient import TestClient
    from ostag.work_monitor.server import create_app
    path = tmp_path/'run_ledger.jsonl'
    database = tmp_path/'monitor.sqlite3'
    app = create_app(settings=Settings(managed_ledger_paths=(path,), monitor_server_db=database,
                                      monitor_scheduler_enabled=False, feishu_use_websocket=False))
    assert not database.exists()
    with pytest.raises(SchemaError):
        with TestClient(app):
            pytest.fail('missing domain admitted')
    assert not database.exists()
    assert not path.exists()


def test_injected_legacy_monitor_refuses_lifespan(tmp_path):
    from fastapi.testclient import TestClient
    from ostag.work_monitor.server import create_app
    from ostag.work_monitor.store import MonitorStore
    from ostag.work_monitor.identity import IdentityStore
    from ostag.work_monitor.service import WorkMonitorService
    from ostag.work_monitor.projection import Projector
    path = tmp_path/'run_ledger.jsonl'
    provision(path)
    store = MonitorStore(tmp_path/'monitor.sqlite3')
    service = WorkMonitorService(store, IdentityStore(store.path))
    app = create_app(settings=Settings(managed_ledger_paths=(path,), monitor_server_db=store.path,
                                      monitor_scheduler_enabled=False, feishu_use_websocket=False),
                     service=service, projector=Projector(store))
    with pytest.raises(SchemaError):
        with TestClient(app):
            pytest.fail('legacy injected service admitted')


def test_server_constructs_coordinated_monitor_and_scheduler(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from ostag.work_monitor import server
    from ostag.work_monitor.scheduler import WorkMonitorScheduler
    path = tmp_path/'run_ledger.jsonl'
    coord = provision(path)
    captured = []
    original = WorkMonitorScheduler.__init__

    def capture(self, *args, **kwargs):
        original(self, *args, **kwargs)
        captured.append(self)

    monkeypatch.setattr(WorkMonitorScheduler, '__init__', capture)
    app = server.create_app(settings=Settings(managed_ledger_paths=(path,),
        monitor_server_db=tmp_path/'monitor.sqlite3', monitor_scheduler_enabled=True,
        feishu_use_websocket=False, brief_status_dir=tmp_path/'status',
        brief_control_path=tmp_path/'control.json'))
    with TestClient(app) as client:
        assert client.get('/healthz').status_code == 200
        assert len(captured) == 1
        assert captured[0].ledger.coordination == coord


async def test_cli_read_respects_configured_domain_and_never_recreates_lock(tmp_path, monkeypatch, capsys):
    from ostag.main import main_async
    path = tmp_path/'run.jsonl'
    coord = provision(path)
    monkeypatch.setenv('OSTAG_RUN_LEDGER_PATH', str(path))
    monkeypatch.setenv('OSTAG_MANAGED_LEDGER_PATHS', json.dumps([str(path)]))
    assert await main_async(['receipts', 'list', '--json']) == 0
    assert json.loads(capsys.readouterr().out) == []
    coord.path.unlink()
    assert await main_async(['receipts', 'list', '--json']) == 1
    assert json.loads(capsys.readouterr().out)['error']['code'] == 'ledger_coordination_unavailable'
    assert not coord.path.exists()


@pytest.mark.parametrize('owner', ['feishu_ws', 'daily_scheduler', 'scheduler'])
def test_unvalidated_injected_owners_are_not_started_with_managed_policy(tmp_path, owner):
    from fastapi.testclient import TestClient
    from ostag.work_monitor.server import create_app
    path = tmp_path/'run_ledger.jsonl'
    provision(path)
    with pytest.raises(ValueError, match='Managed ledger policy'):
        app = create_app(settings=Settings(managed_ledger_paths=(path,)), **{owner: object()})
        with TestClient(app):
            pytest.fail('unknown injected owner admitted')
