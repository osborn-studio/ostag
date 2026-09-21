"""Persistent denial survives process exit; it is not rewrite authorization."""

import json
import os
import subprocess
import sys

import pytest

from test_production_deploy_lock import Host


@pytest.fixture
def host(tmp_path):
    value = Host(tmp_path)
    yield value
    value.close()


@pytest.mark.parametrize(
    "damage", ["sealed", "missing", "malformed", "permissions", "symlink", "duplicate"]
)
def test_deploy_checks_persistent_state_before_any_runtime_io(host, damage):
    path = host.maintenance
    if damage == "sealed":
        state = json.loads(path.read_text())
        state.update(state="sealed", generation=1, maintenance_id="a" * 64, request_sha256="b" * 64)
        path.write_text(json.dumps(state))
    elif damage == "missing":
        path.unlink()
    elif damage == "malformed":
        path.write_text("private invalid state")
    elif damage == "permissions":
        path.chmod(0o644)
    elif damage == "symlink":
        path.unlink()
        path.symlink_to(host.root / "other")
    else:
        path.write_text('{"state":"sealed","state":"open"}')
    code, output = host.finish(host.start())
    assert code != 0
    assert "maintenance" in output
    assert "private" not in output
    assert host.events() == ""


def test_sealed_context_blocks_other_deployer_then_persists_after_exit(host):
    from ostag.core.maintenance_seal import sealed_maintenance

    with sealed_maintenance(
        host.lock,
        host.maintenance,
        expected_generation=0,
        maintenance_id="a" * 64,
        request_sha256="b" * 64,
        authorize=lambda: None,
    ) as guard:
        guard.checkpoint()
        code, output = host.finish(host.start())
        assert code != 0 and "lock is busy" in output
        assert host.events() == ""
    assert json.loads(host.maintenance.read_text())["state"] == "sealed"
    code, output = host.finish(host.start())
    assert code != 0 and "maintenance" in output
    assert host.events() == ""
    from ostag.core.maintenance_seal import MaintenanceDenied

    with pytest.raises(MaintenanceDenied):
        guard.checkpoint()


@pytest.mark.skipif(
    sys.platform != "linux", reason="Full deploy preflight requires Linux stat and Bash mapfile"
)
def test_deployer_excludes_maintenance_before_state_change(host):
    from ostag.core.maintenance_seal import sealed_maintenance, MaintenanceDenied

    process = host.start(PAUSE_STAGE="first_read")
    host.entered(process)
    before = host.maintenance.read_bytes()
    with pytest.raises(MaintenanceDenied):
        with sealed_maintenance(
            host.lock,
            host.maintenance,
            expected_generation=0,
            maintenance_id="a" * 64,
            request_sha256="b" * 64,
            authorize=lambda: None,
        ):
            pytest.fail("concurrent maintenance admitted")
    assert host.maintenance.read_bytes() == before
    host.unblock()
    assert host.finish(process)[0] == 0


def test_launcher_rechecks_lock_identity_after_flock(host):
    host.lock.touch(mode=0o600)
    hook = host.root / "hooks"
    hook.mkdir()
    (hook / "sitecustomize.py").write_text("""
import fcntl, os
from pathlib import Path
original = fcntl.flock
def replace_after_lock(fd, flags):
    original(fd, flags)
    path = Path(os.environ["OSTAG_DEPLOY_LOCK_PATH"])
    replacement = path.with_name("replacement.lock")
    replacement.touch(mode=0o600)
    os.replace(replacement, path)
fcntl.flock = replace_after_lock
""")
    code, output = host.finish(host.start(PYTHONPATH=str(hook)))
    assert (
        code == 1
        and output == "production deploy refused: deployment lock is unavailable or unsafe\n"
    )
    assert host.events() == ""


def test_abrupt_exit_leaves_seal_and_prevents_new_deploy(host):
    script = """
import os,sys
from pathlib import Path
from ostag.core.maintenance_seal import sealed_maintenance
with sealed_maintenance(Path(sys.argv[1]),Path(sys.argv[2]),expected_generation=0,
 maintenance_id='a'*64,request_sha256='b'*64,authorize=lambda:None):
 os._exit(73)
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(host.lock), str(host.maintenance)], timeout=10
    )
    assert result.returncode == 73
    code, output = host.finish(host.start())
    assert code != 0 and "maintenance" in output
    assert host.events() == ""


def test_authorizer_denial_never_seals_or_enters(host):
    from ostag.core.maintenance_seal import sealed_maintenance, MaintenanceDenied

    before = host.maintenance.read_bytes()

    def deny():
        raise PermissionError("private denial")

    with pytest.raises(MaintenanceDenied):
        with sealed_maintenance(
            host.lock,
            host.maintenance,
            expected_generation=0,
            maintenance_id="a" * 64,
            request_sha256="b" * 64,
            authorize=deny,
        ):
            pytest.fail("denied operation entered")
    assert host.maintenance.read_bytes() == before


def test_same_request_reentry_syncs_without_advancing_generation(host, monkeypatch):
    import os
    from ostag.core.maintenance_seal import sealed_maintenance, MaintenanceDenied

    args = dict(
        expected_generation=0,
        maintenance_id="a" * 64,
        request_sha256="b" * 64,
        authorize=lambda: None,
    )
    with sealed_maintenance(host.lock, host.maintenance, **args):
        pass
    syncs = []
    original = os.fsync

    def sync(fd):
        syncs.append(os.fstat(fd).st_mode)
        original(fd)

    monkeypatch.setattr(os, "fsync", sync)
    with sealed_maintenance(host.lock, host.maintenance, **args):
        pass
    assert len(syncs) == 2
    assert json.loads(host.maintenance.read_text())["generation"] == 1
    with pytest.raises(MaintenanceDenied):
        with sealed_maintenance(
            host.lock, host.maintenance, **(args | {"maintenance_id": "c" * 64})
        ):
            pytest.fail("different request stole seal")


@pytest.mark.parametrize("sync_failure", [1, 2])
def test_sync_failure_never_enters_body_and_reentry_preserves_seal(host, monkeypatch, sync_failure):
    import os
    from ostag.core.maintenance_seal import sealed_maintenance, MaintenanceDenied

    args = dict(
        expected_generation=0,
        maintenance_id="a" * 64,
        request_sha256="b" * 64,
        authorize=lambda: None,
    )
    calls = []
    original = os.fsync

    def fail(fd):
        calls.append(fd)
        if len(calls) == sync_failure:
            raise OSError("synthetic sync failure")
        original(fd)

    with monkeypatch.context() as patch:
        patch.setattr(os, "fsync", fail)
        with pytest.raises(MaintenanceDenied):
            with sealed_maintenance(host.lock, host.maintenance, **args):
                pytest.fail("unconfirmed seal entered body")
    expected = "open" if sync_failure == 1 else "sealed"
    assert json.loads(host.maintenance.read_text())["state"] == expected
    with sealed_maintenance(host.lock, host.maintenance, **args) as guard:
        guard.checkpoint()
    assert json.loads(host.maintenance.read_text())["state"] == "sealed"


def test_container_stop_occurs_only_inside_durable_seal(host):
    from ostag.core.maintenance_seal import seal_and_stop_container
    from test_container_stop import setup

    request, cli, _, _ = setup()
    run = cli.run

    def sealed(args, **kwargs):
        value = json.loads(host.maintenance.read_text())
        assert value["state"] == "sealed"
        assert value["request_sha256"] == request.digest
        return run(args, **kwargs)

    cli.run = sealed
    result = seal_and_stop_container(
        host.lock,
        host.maintenance,
        expected_generation=0,
        request=request,
        cli=cli,
        authorize=lambda _: None,
    )
    assert result.maintenance_authorized is False
    assert json.loads(host.maintenance.read_text())["state"] == "sealed"
    code, output = host.finish(host.start())
    assert code != 0 and "maintenance" in output


def test_container_stop_failure_never_unseals(host):
    from ostag.core.maintenance_seal import seal_and_stop_container
    from ostag.core.container_stop import ContainerStopDenied
    from test_container_stop import setup

    request, cli, _, _ = setup()

    def fail(*args, **kwargs):
        raise OSError("synthetic unavailable daemon")

    cli.run = fail
    with pytest.raises(ContainerStopDenied):
        seal_and_stop_container(
            host.lock,
            host.maintenance,
            expected_generation=0,
            request=request,
            cli=cli,
            authorize=lambda _: None,
        )
    assert json.loads(host.maintenance.read_text())["state"] == "sealed"


def test_checkpoint_rejects_state_change_and_preserves_body_error(host):
    from ostag.core.maintenance_seal import sealed_maintenance, MaintenanceDenied

    failure = OSError("synthetic body failure")
    with pytest.raises(OSError) as raised:
        with sealed_maintenance(
            host.lock,
            host.maintenance,
            expected_generation=0,
            maintenance_id="a" * 64,
            request_sha256="b" * 64,
            authorize=lambda: None,
        ) as guard:
            host.maintenance.write_text("invalid")
            with pytest.raises(MaintenanceDenied):
                guard.checkpoint()
            raise failure
    assert raised.value is failure


@pytest.mark.parametrize(
    "case",
    [
        "open",
        "sealed",
        "unknown_schema",
        "boolean_generation",
        "extra",
        "missing_key",
        "invalid_id",
        "zero_sealed_generation",
        "open_with_binding",
        "duplicate",
        "malformed",
        "missing",
        "permissions",
        "hardlink",
        "symlink",
        "fifo",
        "directory",
    ],
)
def test_state_validator_conformance_between_inspector_and_launcher(host, case):
    from ostag.core.maintenance_seal import inspect_maintenance, MaintenanceDenied

    host.lock.touch(mode=0o600)
    path = host.maintenance
    state = json.loads(path.read_text())
    if case in ("sealed", "invalid_id", "zero_sealed_generation"):
        state.update(state="sealed", generation=1, maintenance_id="a" * 64, request_sha256="b" * 64)
    if case == "unknown_schema":
        state["schema_version"] = 2
    elif case == "boolean_generation":
        state["generation"] = True
    elif case == "extra":
        state["extra"] = "private"
    elif case == "missing_key":
        del state["generation"]
    elif case == "invalid_id":
        state["maintenance_id"] = "private"
    elif case == "zero_sealed_generation":
        state["generation"] = 0
    elif case == "open_with_binding":
        state["request_sha256"] = "b" * 64
    path.write_text(json.dumps(state))
    if case == "duplicate":
        path.write_text('{"state":"open","state":"sealed"}')
    elif case == "malformed":
        path.write_text("private")
    elif case == "permissions":
        path.chmod(0o644)
    elif case == "hardlink":
        os.link(path, host.root / "alias")
    elif case in ("missing", "symlink", "fifo", "directory"):
        path.unlink()
        if case == "symlink":
            path.symlink_to(host.root / "other")
        elif case == "fifo":
            os.mkfifo(path, 0o600)
        elif case == "directory":
            path.mkdir(mode=0o700)
    if case in ("open", "sealed"):
        assert inspect_maintenance(host.lock, path) == state
    else:
        with pytest.raises(MaintenanceDenied):
            inspect_maintenance(host.lock, path)
    code, output = host.finish(host.start(FAIL_STAGE="first_read"))
    if case == "open":
        assert code == 42 and host.events() == "first_read\n"
    else:
        expected = "maintenance sealed" if case == "sealed" else "maintenance state unavailable"
        assert code == 1 and output == f"production deploy refused: {expected}\n"
        assert host.events() == ""


@pytest.mark.skipif(os.geteuid() != 0, reason="Ownership conformance needs isolated Linux root")
@pytest.mark.parametrize("target", ["lock", "maintenance"])
def test_both_readers_require_same_effective_uid(host, target):
    from ostag.core.maintenance_seal import inspect_maintenance, MaintenanceDenied

    host.lock.touch(mode=0o600)
    path = getattr(host, target)
    os.chown(path, 12345, -1)
    try:
        with pytest.raises(MaintenanceDenied):
            inspect_maintenance(host.lock, host.maintenance)
        code, output = host.finish(host.start())
        assert code == 1 and "unavailable" in output
        assert host.events() == ""
    finally:
        os.chown(path, os.geteuid(), -1)


def test_inspector_does_not_create_missing_files_and_refuses_busy_lock(host):
    from ostag.core.maintenance_seal import (
        inspect_maintenance,
        sealed_maintenance,
        MaintenanceDenied,
    )

    with pytest.raises(MaintenanceDenied):
        inspect_maintenance(host.lock, host.maintenance)
    assert not host.lock.exists()
    with sealed_maintenance(
        host.lock,
        host.maintenance,
        expected_generation=0,
        maintenance_id="a" * 64,
        request_sha256="b" * 64,
        authorize=lambda: None,
    ):
        with pytest.raises(MaintenanceDenied):
            inspect_maintenance(host.lock, host.maintenance)
    before = host.maintenance.read_bytes()
    assert inspect_maintenance(host.lock, host.maintenance)["state"] == "sealed"
    assert host.maintenance.read_bytes() == before


@pytest.mark.parametrize("target", ["lock", "maintenance"])
@pytest.mark.parametrize(
    "kind",
    [
        "parent_writable",
        "parent_symlink",
        "parent_sticky",
        "relative",
        "dotdot",
        "empty",
        "missing_parent",
    ],
)
def test_directory_path_conformance_for_both_readers_and_sealing(host, target, kind):
    from ostag.core.maintenance_seal import (
        inspect_maintenance,
        sealed_maintenance,
        MaintenanceDenied,
    )

    host.lock.touch(mode=0o600)
    parent = host.root / "unsafe-parent"
    parent.mkdir()
    path = parent / "record"
    path.write_bytes(host.maintenance.read_bytes() if target == "maintenance" else b"")
    path.chmod(0o600)
    if kind == "parent_writable":
        parent.chmod(0o777)
    elif kind == "parent_sticky":
        parent.chmod(0o1777)
    elif kind == "parent_symlink":
        link = host.root / "alias-parent"
        link.symlink_to(parent, target_is_directory=True)
        path = link / "record"
    elif kind == "relative":
        path = "relative/record"
    elif kind == "dotdot":
        path = str(parent) + "/../unsafe-parent/record"
    elif kind == "empty":
        path = ""
    elif kind == "missing_parent":
        path = host.root / "missing-parent" / "record"
    lock, state = (path, host.maintenance) if target == "lock" else (host.lock, path)
    with pytest.raises(MaintenanceDenied):
        inspect_maintenance(lock, state)
    before = host.maintenance.read_bytes()
    with pytest.raises(MaintenanceDenied):
        with sealed_maintenance(
            lock,
            state,
            expected_generation=0,
            maintenance_id="a" * 64,
            request_sha256="b" * 64,
            authorize=lambda: None,
        ):
            pytest.fail("unsafe path admitted")
    assert host.maintenance.read_bytes() == before
    override = "OSTAG_DEPLOY_LOCK_PATH" if target == "lock" else "OSTAG_MAINTENANCE_STATE_PATH"
    code, output = host.finish(host.start(**{override: str(path)}))
    reason = (
        "deployment lock is unavailable or unsafe"
        if target == "lock"
        else "maintenance state unavailable"
    )
    assert code == 1 and output == f"production deploy refused: {reason}\n"
    assert host.events() == ""


def test_seal_and_new_lock_modes_are_independent_of_ambient_umask(host):
    from ostag.core.maintenance_seal import sealed_maintenance, inspect_maintenance

    prior = os.umask(0o777)
    try:
        with sealed_maintenance(
            host.lock,
            host.maintenance,
            expected_generation=0,
            maintenance_id="a" * 64,
            request_sha256="b" * 64,
            authorize=lambda: None,
        ):
            assert host.lock.stat().st_mode & 0o7777 == 0o600
            assert host.maintenance.stat().st_mode & 0o7777 == 0o600
    finally:
        os.umask(prior)
    assert inspect_maintenance(host.lock, host.maintenance)["state"] == "sealed"


def test_preflight_recreates_missing_boot_lock_without_clearing_seal(host):
    from ostag.core.maintenance_seal import (
        sealed_maintenance,
        inspect_maintenance,
        MaintenanceDenied,
    )

    with sealed_maintenance(
        host.lock,
        host.maintenance,
        expected_generation=0,
        maintenance_id="a" * 64,
        request_sha256="b" * 64,
        authorize=lambda: None,
    ):
        pass
    before = host.maintenance.read_bytes()
    host.lock.unlink()  # Simulate /run loss on reboot, after all owners exited.
    with pytest.raises(MaintenanceDenied):
        inspect_maintenance(host.lock, host.maintenance)
    hook = host.root / "boot-hooks"
    hook.mkdir()
    (hook / "sitecustomize.py").write_text("import os\nos.umask(0o777)\n")
    code, output = host.finish(host.start(PYTHONPATH=str(hook)))
    assert code == 1 and output == "production deploy refused: maintenance sealed\n"
    assert host.lock.stat().st_mode & 0o7777 == 0o600
    assert inspect_maintenance(host.lock, host.maintenance)["state"] == "sealed"
    assert host.maintenance.read_bytes() == before
    assert host.events() == ""
