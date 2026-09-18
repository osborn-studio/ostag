"""Disabled v1 status and explicit v2 local custody bridge; no deploy preflight."""

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

from ostag.briefs.lifecycle import (
    _KINDS, BriefLifecycle, LifecycleDenied, _clock, check_binding,
)

_LIMIT = 4096
_IO_TIMEOUT = 0.5
_HEARTBEAT = 0.25
_PROTOCOL = "brief-local-custody/1"
_ATTACHED_FIELDS = {
    "version", "protocol", "request_id", "boot_id", "control_generation", "control_valid",
    "admission", "maintenance", "custody", "uncertain", "local_drained", "shutdown",
    "updated_at", "lifecycle", "component", "drain_ack",
}
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
    return type(value) is int and 1 <= value < 2**63


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
    if type(value) is dict and type(value.get("version")) is int and value["version"] == 2:
        return _attached_shape(value)
    return _disabled_shape(value)


def _attached_shape(value):
    """Exact local observation, with no durable queue/remote/startup assertions."""
    if type(value) is not dict or set(value) != _ATTACHED_FIELDS:
        return False
    counts, component = value["custody"], value["component"]
    if (type(counts) is not dict or set(counts) != set(_KINDS)
            or not all(type(n) is int and 0 <= n <= 256 for n in counts.values())
            or sum(counts.values()) > 256
            or type(component) is not dict or set(component) != {"status", "reason"}):
        return False
    valid = value["control_valid"]
    running = value["lifecycle"] == "running"
    return bool(
        type(value["version"]) is int and value["version"] == 2
        and value["protocol"] == _PROTOCOL and _positive_int(value["request_id"])
        and _uuid(value["boot_id"])
        and (value["control_generation"] is None or _positive_int(value["control_generation"]))
        and all(type(value[k]) is bool for k in (
            "control_valid", "maintenance", "uncertain", "local_drained", "shutdown", "drain_ack"))
        and (not valid or value["control_generation"] is not None)
        and value["admission"] in ("off", "on")
        and (valid or (value["admission"] == "off" and not value["maintenance"]))
        and (not value["maintenance"] or (value["admission"] == "off" and not sum(counts.values())))
        and (not value["uncertain"] or sum(counts.values()) > 0)
        and value["local_drained"] == (
            valid and value["admission"] == "off" and not value["shutdown"]
            and not sum(counts.values()))
        and value["drain_ack"] is False
        and _finite(value["updated_at"]) and value["updated_at"] >= 0
        and value["lifecycle"] in ("running", "stopped")
        and (not value["shutdown"] or not valid)
        and (running or value["shutdown"])
        and component == (
            {"status": "stopped", "reason": "stopped"} if not running else
            {"status": "ok", "reason": "closed" if value["admission"] == "off" else "open"}
            if valid else {"status": "degraded", "reason": component["reason"]})
        and type(component["reason"]) is str
        and component["reason"] in (_REASONS | {"open", "control_denied"})
        and (valid or not running or component["reason"] in {
            "control_missing", "control_invalid", "control_wrong_boot", "control_stale",
            "control_denied"})
    )


def _disabled_shape(value):
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
    Attached v2 ALWAYS fails: a sealed local zero cannot prove durable startup
    reconciliation or absence of remote orphans from an earlier process. A future
    production owner must combine that evidence with the local observation.
    """
    return bool(
        _status_shape(value)
        and value["version"] == 1
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


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_nlink,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _read_json(directory, name, *, with_identity=False):
    before = _entry(directory, name)
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=directory)
    try:
        info = _regular(fd)
        if info.st_size > _LIMIT or _identity(info) != _identity(before):
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
        if (_identity(info) != _identity(os.fstat(fd))
                or _identity(info) != _identity(_entry(directory, name))):
            raise _Fault("invalid_json")
        identity = (os.fstat(directory).st_dev, os.fstat(directory).st_ino, *_identity(info))
        return (value, identity) if with_identity else value
    finally:
        os.close(fd)


class _StatusWriter:
    """Synchronous filesystem owner, used only in the isolated child process."""

    def __init__(self, status_dir, control_path, boot_id):
        self.status_dir, self.control_path, self.boot_id = status_dir, control_path, boot_id
        self.directory = self.lock = None
        self.accepted = None
        self.control_identity = None

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
        self.control_identity = None
        try:
            directory = _directory(self.control_path.parent)
            try:
                value, identity = _read_json(directory, self.control_path.name, with_identity=True)
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
            self.control_identity = identity
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
        return self._write(value)

    def _write(self, value, *, check_current=None):
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
            if check_current is not None:
                check_current()
            if _identity(_entry(self.directory, name)) != _identity(_regular(fd)):
                raise _Fault("unsafe_file")
            os.replace(name, "status.json", src_dir_fd=self.directory, dst_dir_fd=self.directory)
        finally:
            os.close(fd)
            try:
                os.unlink(name, dir_fd=self.directory)
            except FileNotFoundError:
                pass
        return value


def _message(boot, request, phase, data):
    return dict(protocol=_PROTOCOL, boot_id=boot, request_id=request, phase=phase, data=data)


def _send(connection, value):
    raw = json.dumps(value, allow_nan=False, separators=(",", ":")).encode()
    if len(raw) > _LIMIT:
        raise _Fault("ipc_invalid")
    connection.send_bytes(raw)


def _receive(connection, boot):
    value = json.loads(connection.recv_bytes(_LIMIT).decode("utf-8"), object_pairs_hook=_pairs,
                       parse_constant=lambda _: (_ for _ in ()).throw(_Fault("ipc_invalid")))
    if (type(value) is not dict
            or set(value) != {"protocol", "boot_id", "request_id", "phase", "data"}
            or value["protocol"] != _PROTOCOL or value["boot_id"] != boot
            or not _positive_int(value["request_id"])
            or value["phase"] not in ("sample", "publish", "stopped", "error")):
        raise _Fault("ipc_invalid")
    return value


def _observation(snapshot, boot, request, lifecycle, reason):
    """A v2 watermark is the last locally applied generation, even when invalid.

    Counts are overlapping receipts, including permanently uncertain operations;
    they are neither durable row counts nor estimates of remote activity.
    """
    valid = snapshot.control_valid and not snapshot.shutdown
    return {
        "version": 2, "protocol": _PROTOCOL, "request_id": request, "boot_id": boot,
        "control_generation": snapshot.control_generation, "control_valid": valid,
        "admission": snapshot.admission if valid else "off",
        "maintenance": snapshot.maintenance if valid else False,
        "custody": dict(snapshot.custody), "uncertain": snapshot.uncertain,
        "local_drained": snapshot.drained, "shutdown": snapshot.shutdown,
        "updated_at": 0, "lifecycle": lifecycle, "drain_ack": False,
        "component": {"status": "stopped", "reason": "stopped"}
        if lifecycle == "stopped" else {
            "status": "ok" if valid else "degraded",
            "reason": ("closed" if snapshot.admission == "off" else "open") if valid else reason,
        },
    }


def _serve_attached(connection, status_dir, control_path, boot_id):
    """Bounded two-phase child protocol; no caller-provided custody API.

    Only the parent reads the concrete lifecycle. Child rechecks the sampled
    file's content AND identity after writing the temporary status, immediately
    before rename, and again before ACK. Filesystem edits after that observation
    are detected on the next bounded sample/lease, not instantaneously.
    """
    writer = _StatusWriter(status_dir, control_path, boot_id)
    request, sampled, identity, reason = 0, None, None, "control_missing"
    phase = None
    try:
        writer.open()
        while True:
            message = _receive(connection, boot_id)
            current, data = message["request_id"], message["data"]
            if message["phase"] == "sample":
                if current <= request or data is not None:
                    raise _Fault("ipc_invalid")
                request, phase = current, "sample"
                sampled, reason = writer._control()
                identity = writer.control_identity
                _send(connection, _message(boot_id, request, phase,
                                           {"control": sampled, "reason": reason}))
                continue
            terminal = message["phase"] == "stopped"
            if ((terminal and current <= request)
                    or (not terminal and (message["phase"] != "publish"
                        or phase != "sample" or current != request))
                    or not _attached_shape(data) or data["boot_id"] != boot_id
                    or data["request_id"] != current
                    or data["lifecycle"] != ("stopped" if terminal else "running")
                    or data["updated_at"] != 0):
                raise _Fault("ipc_invalid")
            if not terminal and data["control_valid"] and (
                sampled is None or data["control_generation"] != sampled["generation"]
                or data["admission"] != sampled["admission"]
                or data["maintenance"] != sampled["maintenance"]
            ):
                raise _Fault("ipc_invalid")

            def check_current():
                directory = _directory(status_dir)
                try:
                    if (_identity(os.fstat(directory))[:2]
                            != _identity(os.fstat(writer.directory))[:2]):
                        raise _Fault("status_unavailable")
                finally:
                    os.close(directory)
                held = _regular(writer.lock)
                if (_identity(_entry(writer.directory, "owner.lock")) != _identity(held)
                        or held.st_uid != os.geteuid() or stat.S_IMODE(held.st_mode) != 0o600):
                    raise _Fault("ownership_conflict")
                try:
                    if not _status_shape(_read_json(writer.directory, "status.json")):
                        raise _Fault("status_invalid")
                except FileNotFoundError:
                    pass
                if not terminal:
                    latest, latest_reason = writer._control()
                    if latest != sampled or writer.control_identity != identity or latest_reason != reason:
                        raise _Fault("control_stale")

            status = data | {"updated_at": time.time()}
            if not _attached_shape(status):
                raise _Fault("status_invalid")
            writer._write(status, check_current=check_current)
            check_current()
            _send(connection, _message(boot_id, current, message["phase"], status))
            phase = None
            if terminal:
                break
    except Exception as exc:
        try:
            _send(connection, _message(boot_id, max(1, request), "error", {
                "reason": "ownership_conflict" if str(exc) == "ownership_conflict"
                else "status_unavailable"}))
        except (OSError, ValueError):
            pass
    finally:
        writer.close()
        connection.close()


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
    """One file owner per lifespan, with a fresh boot and a lifetime flock.

    The default disabled owner composes no stores, artifacts, retention, runners
    or workers. Its v1 zero counters describe this absence, never database rows.
    Without lifecycle, legacy disabled v1 behavior is unchanged. Explicit
    lifecycle= binds the SAME concrete object injected into services, never a
    new business thread/store. Attachment seals even cleanup/cancel on off:
    operators must perform tracked cleanup under on before requesting a drain.

    Attached control is sampled every 250 ms plus bounded child IO (500 ms per
    exchange). Admission/checks expire 750 ms after the sample REQUEST, even if
    the parent loop stalls. Only an exact publication ACK accepted within that
    lifetime may open/renew. Disk edits are not observed instantaneously.
    Admission is sealed from custody capture through ACK, so that observation
    can only overcount completions during publication. After on acceptance new
    custody may begin; on observations ALWAYS have local_drained=false.
    v2 is exclusively a local observation, NEVER production drain evidence:
    durable startup reconciliation and prior-process remote orphans are unknown.

    One spawned process isolates all filesystem IO. There is only one outstanding
    request, with a deadline; a stuck child is killed and reaped before any final
    publication is attempted. No detached thread can later overwrite shutdown
    with a ready status. No retries run indefinitely during shutdown.
    """

    def __init__(self, status_dir: Path, control_path: Path, *, lifecycle: BriefLifecycle | None = None):
        check_binding(lifecycle)
        self._lifecycle = lifecycle
        if lifecycle is not None:
            lifecycle._bind_file_owner(self)
        self.status_dir, self.control_path = status_dir, control_path
        self.boot_id = None
        self.health, self.reason = "degraded", "starting"
        self.last_status = None
        self._process = self._connection = self._task = None
        self._pending = False
        self._needs_reap = False
        self._request = 0
        self._stop = asyncio.Event()
        self._initial = asyncio.Event()

    def _spawn(self):
        context = multiprocessing.get_context("spawn")
        self._connection, child = context.Pipe()
        self._process = context.Process(
            target=_serve if self._lifecycle is None else _serve_attached,
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
        if self._lifecycle is not None:
            return await self._publish_attached(lifecycle)
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

    async def _exchange(self, request, phase, data):
        if self._pending:
            raise _Fault("ipc_pending")
        self._pending = True
        _send(self._connection, _message(self.boot_id, request, phase, data))
        deadline = asyncio.get_running_loop().time() + _IO_TIMEOUT
        while not self._connection.poll():
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError
            await asyncio.sleep(0.01)
        # Check even a queued reply: a stalled event loop must not accept it late.
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError
        result = _receive(self._connection, self.boot_id)
        if result["phase"] == "error":
            if result["data"] == {"reason": "ownership_conflict"}:
                self.health, self.reason = "degraded", "ownership_conflict"
            raise _Fault("status_unavailable")
        if result["request_id"] != request or result["phase"] != phase:
            raise _Fault("ipc_stale")
        self._pending = False
        return result["data"]

    async def _publish_attached(self, lifecycle):
        life = self._lifecycle
        self._request += 1
        request = self._request
        try:
            if lifecycle == "stopped":
                snapshot = life.snapshot()
                reason, phase = "stopped", "stopped"
            else:
                sampled_at = _clock()
                if sampled_at is None:
                    raise _Fault("clock_failed")
                sample = await self._exchange(request, "sample", None)
                if (type(sample) is not dict or set(sample) != {"control", "reason"}
                        or type(sample["reason"]) is not str or sample["reason"] not in _REASONS):
                    raise _Fault("ipc_invalid")
                control, reason = sample["control"], sample["reason"]
                if control is not None and (
                    type(control) is not dict or set(control) != _CONTROL_FIELDS
                    or type(control["version"]) is not int or control["version"] != 1
                    or control["boot_id"] != self.boot_id or not _positive_int(control["generation"])
                    or type(control["admission"]) is not str or control["admission"] not in ("on", "off")
                    or type(control["maintenance"]) is not bool
                    or (control["maintenance"] and control["admission"] == "on")
                ):
                    raise _Fault("ipc_invalid")
                if self._stop.is_set():
                    raise _Fault("stopped")
                try:
                    epoch, snapshot = life._file_prepare(self, None if control is None else (
                        control["generation"], control["admission"], control["maintenance"]), sampled_at)
                except LifecycleDenied:
                    life._file_close(self)
                    epoch, snapshot = life._file_prepare(self, None, sampled_at)
                    reason = "control_denied"
                phase = "publish"
            expected = _observation(snapshot, self.boot_id, request, lifecycle, reason)
            if not _attached_shape(expected):
                raise _Fault("status_invalid")
            result = await self._exchange(request, phase, expected)
            if (not _attached_shape(result)
                    or result != expected | {"updated_at": result["updated_at"]}):
                raise _Fault("ipc_invalid")
            # Replace any prior off/zero BEFORE opening to concurrent threads.
            # The disk observation already changed before this exact ACK arrived.
            self.last_status = result
            if lifecycle == "running":
                if self._stop.is_set():
                    raise _Fault("stopped")
                life._file_ack(self, epoch, sampled_at)
            self.health = "stopped" if lifecycle == "stopped" else "ok"
            self.reason = result["component"]["reason"]
            return True
        except BaseException:
            life._file_close(self)
            # An earlier off/zero is not the result of this failed request.
            self.last_status = None
            raise

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
        if self._task is not None or self._stop.is_set():
            raise RuntimeError("BriefStatusOwner is single-use")
        self.boot_id = str(uuid4())
        self._task = asyncio.create_task(self._run())
        try:
            await self._initial.wait()
        except asyncio.CancelledError:
            self._close_attached()
            self._stop.set()
            await asyncio.shield(self._task)
            raise

    def _close_attached(self):
        if self._lifecycle is not None:
            self._needs_reap |= self._pending
            self._lifecycle.shutdown()

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
            if self.reason != "ownership_conflict":
                self.health, self.reason = "degraded", "worker_failed"
        finally:
            self._close_attached()
            self._initial.set()
            await self._finish()

    async def _finish(self):
        # A contender must never publish over the winner, including at shutdown.
        conflict = self.reason == "ownership_conflict"
        recover = self._pending or self._needs_reap
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
        self._close_attached()
        self._stop.set()
        if self._task is not None:
            await asyncio.shield(self._task)
