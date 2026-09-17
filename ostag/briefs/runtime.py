"""A11a status/control foundation. No Brief business composition or deploy preflight."""

from __future__ import annotations

import asyncio
import fcntl
import json
import math
import multiprocessing
import os
import stat
import time
from pathlib import Path
from uuid import UUID, uuid4

_LIMIT = 4096
_IO_TIMEOUT = 0.5
_HEARTBEAT = 0.25
_CONTROL_FIELDS = {"version", "boot_id", "generation", "admission", "maintenance"}
_STATUS_FIELDS = {
    "version",
    "boot_id",
    "control_generation",
    "control_valid",
    "admission",
    "maintenance",
    "enabled",
    "active_leases",
    "inflight",
    "updated_at",
    "lifecycle",
    "component",
    "drain_ack",
}
_REASONS = {
    "closed",
    "unsupported",
    "control_missing",
    "control_invalid",
    "control_wrong_boot",
    "control_stale",
    "stopped",
}


class _Fault(ValueError):
    """Only bounded public reason codes cross the process boundary."""


def _positive_int(value):
    return type(value) is int and value >= 1


def _finite(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def _uuid(value):
    try:
        return isinstance(value, str) and str(UUID(value)) == value
    except ValueError:
        return False


def _status_shape(value):
    if not isinstance(value, dict) or set(value) != _STATUS_FIELDS:
        return False
    component = value["component"]
    inflight = value["inflight"]
    return (
        type(value["version"]) is int
        and value["version"] == 1
        and _uuid(value["boot_id"])
        and (value["control_generation"] is None or _positive_int(value["control_generation"]))
        and all(
            type(value[k]) is bool for k in ("control_valid", "maintenance", "enabled", "drain_ack")
        )
        and value["control_valid"] == (value["control_generation"] is not None)
        and value["admission"] == "off"
        and value["enabled"] is False
        and type(value["active_leases"]) is int
        and value["active_leases"] == 0
        and isinstance(inflight, dict)
        and set(inflight) == {"model", "cli", "send"}
        and all(type(n) is int and n == 0 for n in inflight.values())
        and _finite(value["updated_at"])
        and value["updated_at"] >= 0
        and value["lifecycle"] in ("running", "stopped")
        and isinstance(component, dict)
        and set(component) == {"status", "reason"}
        and component["status"] in ("ok", "degraded", "stopped")
        and isinstance(component["reason"], str)
        and component["reason"] in _REASONS
        and (
            not value["drain_ack"]
            or (
                value["control_valid"]
                and value["lifecycle"] == "running"
                and component == {"status": "ok", "reason": "closed"}
            )
        )
    )


def validate_drain_status(value, expected_boot_id, expected_generation, *, now):
    """Validate supplied status only; callers must read it from a trusted source.

    This reusable check is not an installed deployment preflight. A missing,
    future, stale, restarted, malformed or nonzero status never proves drainage.
    """
    return bool(
        _status_shape(value)
        and _uuid(expected_boot_id)
        and _positive_int(expected_generation)
        and _finite(now)
        and value["boot_id"] == expected_boot_id
        and value["control_generation"] == expected_generation
        and 0 <= now - value["updated_at"] <= 5
        and value["drain_ack"] is True
    )


def _directory(path):
    # Traverse with dir_fds: O_NOFOLLOW on just the final component is insufficient.
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for part in Path(path).absolute().parts[1:]:
            if part == "..":
                raise _Fault("unsafe_file")
            child = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd
            )
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def _regular(fd):
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise _Fault("unsafe_file")
    return info


def _entry(directory, name):
    info = os.stat(name, dir_fd=directory, follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise _Fault("unsafe_file")
    return info


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise _Fault("invalid_json")
        key.encode("utf-8", errors="strict")
        if isinstance(value, str):
            value.encode("utf-8", errors="strict")
        result[key] = value
    return result


def _read_json(directory, name):
    before = _entry(directory, name)
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=directory)
    try:
        info = _regular(fd)
        if info.st_size > _LIMIT or (info.st_dev, info.st_ino) != (before.st_dev, before.st_ino):
            raise _Fault("invalid_json")
        raw = os.read(fd, _LIMIT)
        if len(raw) != info.st_size or os.fstat(fd).st_size != len(raw):
            raise _Fault("invalid_json")
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(_Fault("invalid_json")),
        )
        if not isinstance(value, dict):
            raise _Fault("invalid_json")
        return value
    finally:
        os.close(fd)


class _StatusWriter:
    """Synchronous filesystem owner, used only in the isolated child process."""

    def __init__(self, status_dir, control_path, boot_id):
        self.status_dir, self.control_path, self.boot_id = status_dir, control_path, boot_id
        self.directory = self.lock = None
        self.accepted = None

    def open(self):
        try:
            self.directory = _directory(self.status_dir)
            try:
                _entry(self.directory, "owner.lock")
            except FileNotFoundError:
                pass
            self.lock = os.open(
                "owner.lock",
                os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                0o600,
                dir_fd=self.directory,
            )
            info = _regular(self.lock)
            if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
                raise _Fault("status_unavailable")
            try:
                fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise _Fault("ownership_conflict") from None
        except BaseException:
            self.close()
            raise

    def close(self):
        # Never unlink the lock inode: contenders must lock the same file.
        for name in ("lock", "directory"):
            fd = getattr(self, name)
            if fd is not None:
                os.close(fd)
                setattr(self, name, None)

    def _control(self):
        try:
            directory = _directory(self.control_path.parent)
            try:
                value = _read_json(directory, self.control_path.name)
            finally:
                os.close(directory)
            if (
                set(value) != _CONTROL_FIELDS
                or type(value["version"]) is not int
                or value["version"] != 1
                or not _positive_int(value["generation"])
                or value["admission"] not in ("off", "on")
                or type(value["maintenance"]) is not bool
                or (value["maintenance"] and value["admission"] == "on")
            ):
                raise _Fault("control_invalid")
            if value["boot_id"] != self.boot_id:
                raise _Fault("control_wrong_boot")
            if self.accepted is not None and (
                value["generation"] < self.accepted["generation"]
                or (value["generation"] == self.accepted["generation"] and value != self.accepted)
            ):
                raise _Fault("control_stale")
            self.accepted = value
            return value, "closed" if value["admission"] == "off" else "unsupported"
        except FileNotFoundError:
            return None, "control_missing"
        except _Fault as exc:
            code = str(exc)
            return None, code if code in _REASONS else "control_invalid"
        except (OSError, ValueError, RecursionError):
            return None, "control_invalid"

    def publish(self, lifecycle):
        control, reason = self._control() if lifecycle == "running" else (None, "stopped")
        value = {
            "version": 1,
            "boot_id": self.boot_id,
            "control_generation": control["generation"] if control else None,
            "control_valid": control is not None,
            "admission": "off",
            "maintenance": control["maintenance"] if control else False,
            "enabled": False,
            "active_leases": 0,
            "inflight": {"model": 0, "cli": 0, "send": 0},
            "updated_at": time.time(),
            "lifecycle": lifecycle,
            "component": {
                "status": "stopped"
                if lifecycle == "stopped"
                else ("ok" if reason == "closed" else "degraded"),
                "reason": reason,
            },
            "drain_ack": lifecycle == "running" and control is not None and reason == "closed",
        }
        if not _status_shape(value):
            raise _Fault("status_invalid")
        # Reject special files, aliases and corrupt/unknown existing status schemas.
        try:
            if not _status_shape(_read_json(self.directory, "status.json")):
                raise _Fault("status_invalid")
        except FileNotFoundError:
            pass
        # Detect replacement of our locked inode before publishing.
        current = os.stat("owner.lock", dir_fd=self.directory, follow_symlinks=False)
        held = os.fstat(self.lock)
        if (current.st_dev, current.st_ino) != (held.st_dev, held.st_ino) or held.st_nlink != 1:
            raise _Fault("ownership_conflict")
        name = f".status-{uuid4().hex}.tmp"
        fd = os.open(
            name,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=self.directory,
        )
        try:
            os.fchmod(fd, 0o600)
            raw = json.dumps(value, allow_nan=False, separators=(",", ":")).encode()
            if len(raw) > _LIMIT or os.write(fd, raw) != len(raw):
                raise _Fault("status_unavailable")
            os.replace(name, "status.json", src_dir_fd=self.directory, dst_dir_fd=self.directory)
        finally:
            os.close(fd)
            try:
                os.unlink(name, dir_fd=self.directory)
            except FileNotFoundError:
                pass
        return value


def _serve(connection, status_dir, control_path, boot_id):
    writer = _StatusWriter(status_dir, control_path, boot_id)
    try:
        writer.open()
        while True:
            lifecycle = connection.recv()
            try:
                status = writer.publish(lifecycle)
                connection.send({"status": status})
            except Exception:
                connection.send({"error": "status_unavailable"})
            if lifecycle == "stopped":
                break
    except _Fault as exc:
        connection.send(
            {
                "error": "ownership_conflict"
                if str(exc) == "ownership_conflict"
                else "status_unavailable"
            }
        )
    except (OSError, EOFError):
        try:
            connection.send({"error": "status_unavailable"})
        except (OSError, EOFError):
            pass
    finally:
        writer.close()
        connection.close()


class BriefStatusOwner:
    """One status-only owner per lifespan, with a fresh boot and a lifetime flock.

    No stores, artifacts, retention, runners or workers are composed. Zero
    counters describe this absence; they are never inferred from a database.
    Control is sampled each heartbeat, NOT at operation boundaries. Real
    admission/claim/IO gates, trusted mirrors, enabled services, signed recovery
    routes, protected mounts and deployed preflight remain future A11 work.

    One spawned process isolates all filesystem IO. There is only one outstanding
    request, with a deadline; a stuck child is killed and reaped before any final
    publication is attempted. No detached thread can later overwrite shutdown
    with a ready status. No retries run indefinitely during shutdown.
    """

    def __init__(self, status_dir: Path, control_path: Path):
        self.status_dir, self.control_path = status_dir, control_path
        self.boot_id = None
        self.health, self.reason = "degraded", "starting"
        self.last_status = None
        self._process = self._connection = self._task = None
        self._pending = False
        self._stop = asyncio.Event()
        self._initial = asyncio.Event()

    def _spawn(self):
        context = multiprocessing.get_context("spawn")
        self._connection, child = context.Pipe()
        self._process = context.Process(
            target=_serve,
            args=(child, self.status_dir, self.control_path, self.boot_id),
            daemon=True,
        )
        try:
            self._process.start()
        except Exception:
            self._connection.close()
            raise
        finally:
            child.close()

    async def _publish(self, lifecycle):
        if self._pending:
            raise RuntimeError("publication already pending")
        self._connection.send(lifecycle)
        self._pending = True
        deadline = asyncio.get_running_loop().time() + _IO_TIMEOUT
        while not self._connection.poll():
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError
            await asyncio.sleep(0.01)
        result = self._connection.recv()
        self._pending = False
        if "error" in result:
            self.health, self.reason = "degraded", result["error"]
            return False
        self.last_status = result["status"]
        # Health reports publication outcome, not business readiness/control validity.
        self.health = "stopped" if lifecycle == "stopped" else "ok"
        self.reason = self.last_status["component"]["reason"]
        return True

    async def _reap(self):
        process = self._process
        if process is None or process.pid is None:
            return True
        for action in (process.terminate, process.kill):
            if not process.is_alive():
                break
            action()
            deadline = asyncio.get_running_loop().time() + _IO_TIMEOUT
            while process.is_alive() and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.01)
        if process.is_alive():
            self.health, self.reason = "degraded", "worker_unreaped"
            return False
        process.join(timeout=0)
        process.close()
        self._process = None
        self._connection.close()
        self._pending = False
        return True

    async def start(self):
        if self._task is not None:
            raise RuntimeError("BriefStatusOwner is single-use")
        self.boot_id = str(uuid4())
        self._task = asyncio.create_task(self._run())
        await self._initial.wait()

    async def _run(self):
        try:
            self._spawn()
            while not self._stop.is_set():
                await self._publish("running")
                self._initial.set()
                if self.reason == "ownership_conflict":
                    break
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=_HEARTBEAT)
                except TimeoutError:
                    pass
        except TimeoutError:
            self.health, self.reason = "degraded", "io_timeout"
        except Exception:
            self.health, self.reason = "degraded", "worker_failed"
        finally:
            self._initial.set()
            await self._finish()

    async def _finish(self):
        # A contender must never publish over the winner, including at shutdown.
        conflict = self.reason == "ownership_conflict"
        recover = self._pending
        try:
            if self._process is not None and self._process.is_alive() and not conflict:
                if not recover:
                    await self._publish("stopped")
                    return
            if await self._reap() and not conflict and recover:
                # One bounded terminal attempt after the timed-out writer is dead.
                self._spawn()
                await self._publish("stopped")
        except Exception:
            self.health, self.reason = "degraded", "status_unavailable"
        finally:
            await self._reap()

    async def stop(self):
        self._stop.set()
        if self._task is not None:
            await asyncio.shield(self._task)
