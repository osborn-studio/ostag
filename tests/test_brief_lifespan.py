from __future__ import annotations

import asyncio
import json
import os
import time
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from ostag.briefs.runtime import BriefStatusOwner, _StatusWriter, validate_drain_status
from ostag.config import Settings
from ostag.work_monitor.server import create_app
from brief_process_helpers import _delayed_serve, _failing_serve


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch):
    monkeypatch.setattr("os.environ", {})


async def wait_for_generation(owner, generation):
    async with asyncio.timeout(3):
        while not owner.last_status or owner.last_status["control_generation"] != generation:
            await asyncio.sleep(0.01)


def control(path, boot, generation=1, admission="off", maintenance=False):
    value = dict(
        version=1, boot_id=boot, generation=generation, admission=admission, maintenance=maintenance
    )
    path.write_text(json.dumps(value))
    return value


@pytest.fixture
def writer(tmp_path):
    status = tmp_path / "status"
    status.mkdir()
    owner = _StatusWriter(status, tmp_path / "control.json", str(uuid4()))
    owner.open()
    try:
        yield owner
    finally:
        owner.close()


def test_control_generations_and_invalid_reads_preserve_watermark(writer):
    first = writer.publish("running")
    assert first["control_valid"] is False and first["drain_ack"] is False
    control(writer.control_path, writer.boot_id, 3)
    accepted = writer.publish("running")
    assert accepted["control_generation"] == 3 and accepted["drain_ack"] is True
    assert validate_drain_status(accepted, writer.boot_id, 3, now=accepted["updated_at"])
    assert writer.publish("running")["drain_ack"] is True
    control(writer.control_path, writer.boot_id, 3, maintenance=True)
    assert writer.publish("running")["control_valid"] is False
    writer.control_path.write_text("{}")
    assert writer.publish("running")["control_generation"] is None
    control(writer.control_path, writer.boot_id, 2)
    assert writer.publish("running")["control_valid"] is False
    control(writer.control_path, writer.boot_id, 3)
    assert writer.publish("running")["drain_ack"] is True
    control(writer.control_path, writer.boot_id, 4, admission="on")
    on = writer.publish("running")
    assert on["control_valid"] is True and on["drain_ack"] is False
    assert on["admission"] == "off" and on["enabled"] is False
    control(writer.control_path, writer.boot_id, 5, maintenance=True)
    assert writer.publish("running")["drain_ack"] is True
    stopped = writer.publish("stopped")
    assert stopped["drain_ack"] is False and stopped["lifecycle"] == "stopped"


@pytest.mark.parametrize(
    "raw",
    [
        "{}",
        "[]",
        "null",
        '{"version":1,"version":1}',
        "NaN",
        "x" * 4097,
        b"\xff",
        '{"boot_id":"\\ud800"}',
    ],
)
def test_malformed_control_fails_closed(writer, raw):
    writer.control_path.write_bytes(raw if isinstance(raw, bytes) else raw.encode())
    status = writer.publish("running")
    assert status["control_valid"] is False
    assert status["control_generation"] is None
    assert status["drain_ack"] is False


@pytest.mark.parametrize(
    "change",
    [
        {"version": True},
        {"version": 2},
        {"generation": True},
        {"generation": 0},
        {"generation": 1.0},
        {"generation": "1"},
        {"maintenance": 1},
        {"admission": "ON"},
        {"maintenance": True, "admission": "on"},
        {"extra": False},
        {"boot_id": str(uuid4())},
    ],
)
def test_control_exact_schema(writer, change):
    value = control(writer.control_path, writer.boot_id)
    writer.control_path.write_text(json.dumps(value | change))
    assert writer.publish("running")["drain_ack"] is False


@pytest.mark.parametrize("kind", ["fifo", "symlink", "directory"])
def test_control_special_files_never_block_or_follow(writer, tmp_path, kind):
    target = tmp_path / "target"
    control(target, writer.boot_id)
    if kind == "fifo":
        os.mkfifo(writer.control_path)
    elif kind == "symlink":
        writer.control_path.symlink_to(target)
    else:
        writer.control_path.mkdir()
    assert writer.publish("running")["control_valid"] is False
    assert json.loads(target.read_text())["generation"] == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("updated_at", float("nan")),
        ("updated_at", float("inf")),
        ("updated_at", True),
        ("updated_at", "1"),
        ("updated_at", 10**400),
        ("version", True),
        ("active_leases", True),
        ("active_leases", 1),
        ("enabled", 0),
        ("control_generation", True),
        ("control_valid", 1),
        ("drain_ack", 1),
        ("inflight", {"model": 1, "cli": 0, "send": 0}),
        ("inflight", {"model": False, "cli": 0, "send": 0}),
        ("component", {"status": "ok", "reason": "secret"}),
        ("extra", 1),
    ],
)
def test_drain_validation_rejects_malformed_status(writer, field, value):
    control(writer.control_path, writer.boot_id)
    status = writer.publish("running")
    assert not validate_drain_status(status | {field: value}, writer.boot_id, 1, now=time.time())


def test_drain_validation_requires_boot_generation_and_freshness(writer):
    control(writer.control_path, writer.boot_id)
    status = writer.publish("running")
    at = status["updated_at"]
    assert validate_drain_status(status, writer.boot_id, 1, now=at + 5)
    for boot, gen, now in [
        (str(uuid4()), 1, at),
        (writer.boot_id, 2, at),
        (writer.boot_id, True, at),
        (writer.boot_id, 1, at + 5.001),
        (writer.boot_id, 1, at - 0.001),
        (writer.boot_id, 1, float("nan")),
        (writer.boot_id, 1, True),
    ]:
        assert not validate_drain_status(status, boot, gen, now=now)
    assert not validate_drain_status({}, writer.boot_id, 1, now=at)
    assert not validate_drain_status(None, writer.boot_id, 1, now=at)


def test_private_atomic_status_and_lock_contention(writer):
    control(writer.control_path, writer.boot_id)
    first = writer.publish("running")
    path = writer.status_dir / "status.json"
    assert path.stat().st_mode & 0o777 == 0o600
    other = _StatusWriter(writer.status_dir, writer.control_path, str(uuid4()))
    with pytest.raises(ValueError, match="ownership_conflict"):
        other.open()
    other.close()
    assert json.loads(path.read_text()) == first
    writer.publish("stopped")
    writer.close()
    other.open()
    try:
        assert other.publish("running")["control_valid"] is False
    finally:
        other.close()


def settings(tmp_path, **kwargs):
    return Settings(
        monitor_server_db=tmp_path / "monitor.db",
        brief_status_dir=tmp_path / "status",
        brief_control_path=tmp_path / "control.json",
        feishu_use_websocket=False,
        monitor_scheduler_enabled=False,
        **kwargs,
    )


def test_inert_app_and_reentry_with_no_business_files(tmp_path):
    configured = settings(tmp_path)
    app = create_app(settings=configured)
    assert list(tmp_path.iterdir()) == []
    configured.brief_status_dir.mkdir()
    with TestClient(app) as client:
        first = app.state.brief_status_owner
        response = client.get("/healthz")
        assert response.status_code == 200 and response.json() == {"status": "ok"}
        assert response.headers["X-OStag-Brief-Status"] == "ok", first.reason
        control(configured.brief_control_path, first.boot_id)
        # Observe actual heartbeat publication with a bounded deadline, no race-hiding sleep.
        client.portal.call(wait_for_generation, first, 1)
        assert first.last_status["drain_ack"] is True
    stopped = json.loads((configured.brief_status_dir / "status.json").read_text())
    assert stopped["lifecycle"] == "stopped" and stopped["drain_ack"] is False
    with TestClient(app):
        second = app.state.brief_status_owner
        assert second is not first and second.boot_id != first.boot_id
        assert second.last_status["control_valid"] is False
    assert {p.name for p in configured.brief_status_dir.iterdir()} == {"status.json", "owner.lock"}
    assert not any("brief" in p.name or "artifact" in p.name for p in tmp_path.iterdir())


async def test_actual_owner_contention_and_release(tmp_path):
    status = tmp_path / "status"
    status.mkdir()
    a = BriefStatusOwner(status, tmp_path / "control.json")
    b = BriefStatusOwner(status, tmp_path / "control.json")
    await a.start()
    try:
        await b.start()
        assert b.health == "degraded" and b.reason == "ownership_conflict"
        assert json.loads((status / "status.json").read_text())["boot_id"] == a.boot_id
    finally:
        await b.stop()
        await a.stop()
    c = BriefStatusOwner(status, tmp_path / "control.json")
    await c.start()
    try:
        assert c.health == "ok"
    finally:
        await c.stop()


def test_missing_status_does_not_break_schedulers(tmp_path):
    events = []

    class Scheduler:
        async def run(self, stop):
            events.append("started")
            await stop.wait()
            events.append("stopped")

    app = create_app(
        settings=settings(tmp_path), scheduler=Scheduler(), daily_scheduler=Scheduler()
    )
    with TestClient(app) as client:
        assert client.get("/healthz").json() == {"status": "ok"}
        assert client.get("/healthz").headers["X-OStag-Brief-Status"] == "degraded"
        assert events.count("started") == 2
        assert not (tmp_path / "status").exists()
    assert events.count("stopped") == 2


@pytest.mark.parametrize("entry", ["status.json", "owner.lock"])
@pytest.mark.parametrize("kind", ["fifo", "symlink", "directory", "hardlink"])
def test_status_and_lock_reject_untrusted_files(tmp_path, entry, kind):
    directory = tmp_path / "status"
    directory.mkdir()
    target = tmp_path / "target"
    target.write_text("untouched")
    path = directory / entry
    if kind == "fifo":
        os.mkfifo(path)
    elif kind == "symlink":
        path.symlink_to(target)
    elif kind == "hardlink":
        os.link(target, path)
    else:
        path.mkdir()
    owner = _StatusWriter(directory, tmp_path / "control.json", str(uuid4()))
    try:
        with pytest.raises((ValueError, OSError)):
            owner.open()
            owner.publish("running")
    finally:
        owner.close()
    assert target.read_text() == "untouched"


@pytest.mark.parametrize("raw", ["{}", "[]", '{"version":1,"version":1}', "x" * 4097, "\ud800"])
def test_corrupt_existing_status_not_overwritten(writer, raw):
    path = writer.status_dir / "status.json"
    data = raw.encode("utf-8", errors="surrogatepass")
    path.write_bytes(data)
    with pytest.raises((ValueError, UnicodeError)):
        writer.publish("running")
    assert path.read_bytes() == data


@pytest.mark.parametrize("parent_link", [False, True])
def test_status_directory_symlink_is_rejected(tmp_path, parent_link):
    real = tmp_path / "real"
    real.mkdir()
    (real / "nested").mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    directory = link / "nested" if parent_link else link
    owner = _StatusWriter(directory, tmp_path / "control.json", str(uuid4()))
    with pytest.raises(OSError):
        owner.open()
    assert list((real / "nested").iterdir()) == []
    assert not (real / "owner.lock").exists()


def test_unwritable_status_mock_preserves_previous_status(writer, monkeypatch):
    from ostag.briefs import runtime

    control(writer.control_path, writer.boot_id)
    writer.publish("running")
    path = writer.status_dir / "status.json"
    previous = path.read_bytes()

    def denied(*args, **kwargs):
        raise PermissionError("/private/secret/must/not/escape")

    monkeypatch.setattr(runtime.os, "replace", denied)
    with pytest.raises(PermissionError):
        writer.publish("running")
    assert path.read_bytes() == previous
    assert {p.name for p in writer.status_dir.iterdir()} == {"owner.lock", "status.json"}


def test_delayed_writer_keeps_health_responsive_and_cannot_overwrite_shutdown(
    tmp_path, monkeypatch
):
    import functools
    import multiprocessing
    from ostag.briefs import runtime

    context = multiprocessing.get_context("spawn")
    entered, release = context.Event(), context.Semaphore(0)
    monkeypatch.setattr(
        runtime, "_serve", functools.partial(_delayed_serve, entered=entered, release=release)
    )
    (tmp_path / "status").mkdir()
    app = create_app(settings=settings(tmp_path))
    with TestClient(app) as client:
        owner = app.state.brief_status_owner
        control(tmp_path / "control.json", owner.boot_id)
        assert entered.wait(timeout=3), (owner.health, owner.reason, owner._child_ready, owner.last_status)
        begin = time.monotonic()
        response = client.get("/healthz")
        assert response.status_code == 200 and response.json() == {"status": "ok"}
        assert time.monotonic() - begin < 0.3
    assert owner._process is None
    path = tmp_path / "status" / "status.json"
    terminal = path.read_bytes()
    assert json.loads(terminal)["lifecycle"] == "stopped"
    assert json.loads(terminal)["drain_ack"] is False
    release.release()
    # Process termination/reaping, not a sleep, proves there can be no late rename.
    assert path.read_bytes() == terminal


def test_failed_publication_is_bounded_and_does_not_break_monitor(tmp_path, monkeypatch):
    from ostag.briefs import runtime

    monkeypatch.setattr(runtime, "_serve", _failing_serve)
    (tmp_path / "status").mkdir()
    app = create_app(settings=settings(tmp_path))
    with TestClient(app) as client:
        response = client.get("/healthz")
        assert response.status_code == 200 and response.json() == {"status": "ok"}
        assert response.headers["X-OStag-Brief-Status"] == "degraded"
        owner = app.state.brief_status_owner
        assert owner.reason == "status_unavailable"
        assert owner.last_status is None
    assert owner._process is None


async def test_cancelled_owner_propagates_and_releases(tmp_path):
    directory = tmp_path / "status"
    directory.mkdir()
    owner = BriefStatusOwner(directory, tmp_path / "control.json")
    await owner.start()
    control(owner.control_path, owner.boot_id)
    await wait_for_generation(owner, 1)
    owner._task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await owner._task
    assert owner._process is None
    terminal = json.loads((directory / "status.json").read_text())
    assert terminal["lifecycle"] == "stopped" and terminal["drain_ack"] is False


async def test_cancellation_during_delayed_publication_never_misreads_running_as_stopped(
    tmp_path, monkeypatch
):
    import functools
    import multiprocessing
    from ostag.briefs import runtime

    context = multiprocessing.get_context("spawn")
    entered, release = context.Event(), context.Semaphore(0)
    monkeypatch.setattr(
        runtime, "_serve", functools.partial(_delayed_serve, entered=entered, release=release)
    )
    directory = tmp_path / "status"
    directory.mkdir()
    owner = BriefStatusOwner(directory, tmp_path / "control.json")
    await owner.start()
    control(owner.control_path, owner.boot_id)
    async with asyncio.timeout(3):
        while not entered.is_set():
            await asyncio.sleep(0.01)
    owner._task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await owner._task
    release.release()
    assert owner._process is None
    terminal = json.loads((directory / "status.json").read_text())
    assert terminal["lifecycle"] == "stopped" and terminal["drain_ack"] is False


@pytest.mark.parametrize("admission", ["off", "on"])
def test_new_boot_rejects_old_control(writer, admission):
    control(writer.control_path, writer.boot_id, 9, admission=admission)
    writer.publish("running")
    writer.publish("stopped")
    writer.close()
    new = _StatusWriter(writer.status_dir, writer.control_path, str(uuid4()))
    new.open()
    try:
        status = new.publish("running")
        assert status["control_valid"] is False and status["drain_ack"] is False
        assert status["component"]["reason"] == "control_wrong_boot"
        control(new.control_path, new.boot_id)
        assert new.publish("running")["drain_ack"] is True
    finally:
        new.close()


def test_invalid_higher_generation_is_not_accepted(writer):
    control(writer.control_path, writer.boot_id, 4)
    assert writer.publish("running")["drain_ack"] is True
    control(writer.control_path, writer.boot_id, 20, admission="on", maintenance=True)
    assert writer.publish("running")["control_valid"] is False
    control(writer.control_path, writer.boot_id, 5)
    assert writer.publish("running")["drain_ack"] is True


def test_control_is_read_only_and_environment_cannot_open_admission(writer, monkeypatch):
    monkeypatch.setenv("OSTAG_BRIEF_ADMISSION", "on")
    control(writer.control_path, writer.boot_id, maintenance=True)
    before = writer.control_path.read_bytes()
    status = writer.publish("running")
    assert status["maintenance"] is True and status["admission"] == "off"
    assert writer.control_path.read_bytes() == before
    writer.control_path.unlink()
    status = writer.publish("running")
    assert status["admission"] == "off" and status["drain_ack"] is False


def test_all_reads_are_at_most_4096_bytes(writer, monkeypatch):
    from ostag.briefs import runtime

    original = runtime.os.read
    sizes = []

    def read(fd, size):
        sizes.append(size)
        assert size <= 4096
        return original(fd, size)

    monkeypatch.setattr(runtime.os, "read", read)
    control(writer.control_path, writer.boot_id)
    writer.publish("running")
    writer.publish("running")
    assert sizes


async def test_process_start_failure_is_degraded_not_monitor_failure(tmp_path, monkeypatch):
    def unavailable(self):
        raise OSError("private process error")

    monkeypatch.setattr(BriefStatusOwner, "_spawn", unavailable)
    owner = BriefStatusOwner(tmp_path, tmp_path / "control.json")
    await owner.start()
    assert owner.health == "degraded" and owner.reason == "worker_failed"
    await owner.stop()


@pytest.mark.parametrize("entry", ["status.json", "owner.lock", "control.json"])
def test_special_files_are_rejected_before_open(tmp_path, monkeypatch, entry):
    from ostag.briefs import runtime

    directory = tmp_path / "status"
    directory.mkdir()
    path = (tmp_path if entry == "control.json" else directory) / entry
    os.mkfifo(path)
    original = runtime.os.open

    def guarded(name, *args, **kwargs):
        assert str(name) != entry, "must reject known special files before opening them"
        return original(name, *args, **kwargs)

    monkeypatch.setattr(runtime.os, "open", guarded)
    writer = _StatusWriter(directory, tmp_path / "control.json", str(uuid4()))
    try:
        if entry == "control.json":
            writer.open()
            assert writer.publish("running")["control_valid"] is False
        else:
            with pytest.raises(ValueError):
                writer.open()
                writer.publish("running")
    finally:
        writer.close()
