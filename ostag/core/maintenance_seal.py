"""Persistent maintenance denial under the same trusted-host deployment flock.

No automatic initialization/unseal, data rewrite or production entrypoint.
The host must provision initial state offline and bind both paths in every
trusted deployment/maintenance invocation. A seal is denial, never permission
to rewrite ledgers or proof that arbitrary host writers have stopped.
"""

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import uuid


class MaintenanceDenied(RuntimeError):
    def __init__(self):
        super().__init__("Maintenance state or exclusive custody is unavailable.")


def _parent(path):
    text = str(path)
    parts = text.split("/")
    if not text.startswith("/") or any(p in ("", ".", "..") for p in parts[1:]):
        raise MaintenanceDenied()
    directory = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for index, part in enumerate(parts[1:]):
            metadata = os.fstat(directory)
            sticky = (
                index < len(parts) - 2 and metadata.st_uid == 0 and metadata.st_mode & stat.S_ISVTX
            )
            if metadata.st_uid not in (0, os.geteuid()) or (
                metadata.st_mode & 0o022 and not sticky
            ):
                raise MaintenanceDenied()
            if index == len(parts) - 2:
                return directory, part
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
    except BaseException:
        os.close(directory)
        raise


def _private(info):
    return (
        stat.S_ISREG(info.st_mode)
        and info.st_uid == os.geteuid()
        and info.st_nlink == 1
        and stat.S_IMODE(info.st_mode) == 0o600
    )


def _identity(info):
    return info.st_dev, info.st_ino


def _state(directory, name):
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    try:
        info = os.fstat(descriptor)
        if not _private(info) or not 0 < info.st_size <= 4096:
            raise MaintenanceDenied()
        raw = os.read(descriptor, 4097)
        if len(raw) != info.st_size or _identity(info) != _identity(
            os.stat(name, dir_fd=directory, follow_symlinks=False)
        ):
            raise MaintenanceDenied()
    finally:
        os.close(descriptor)

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise MaintenanceDenied()
            result[key] = value
        return result

    value = json.loads(raw, object_pairs_hook=unique)
    if (
        type(value) is not dict
        or set(value)
        != {"schema_version", "generation", "state", "maintenance_id", "request_sha256"}
        or type(value["schema_version"]) is not int
        or value["schema_version"] != 1
        or type(value["generation"]) is not int
        or value["generation"] < 0
    ):
        raise MaintenanceDenied()
    if value["state"] == "open":
        if value["maintenance_id"] is not None or value["request_sha256"] is not None:
            raise MaintenanceDenied()
    elif value["state"] == "sealed":
        if value["generation"] < 1 or any(
            type(value[k]) is not str or not re.fullmatch("[0-9a-f]{64}", value[k])
            for k in ("maintenance_id", "request_sha256")
        ):
            raise MaintenanceDenied()
    else:
        raise MaintenanceDenied()
    return value, info


class MaintenanceGuard:
    def __init__(self, lock_fd, lock_parent, lock_name, state_parent, state_name, state, authorize):
        self._active = True
        self._lock_fd, self._lock_parent, self._lock_name = lock_fd, lock_parent, lock_name
        self._state_parent, self._state_name, self._state = state_parent, state_name, state
        self._authorize = authorize

    def checkpoint(self):
        try:
            if not self._active or self._authorize() is not None:
                raise MaintenanceDenied()
            info = os.fstat(self._lock_fd)
            if not _private(info) or _identity(info) != _identity(
                os.stat(self._lock_name, dir_fd=self._lock_parent, follow_symlinks=False)
            ):
                raise MaintenanceDenied()
            if _state(self._state_parent, self._state_name)[0] != self._state:
                raise MaintenanceDenied()
        except Exception:
            raise MaintenanceDenied() from None


def inspect_maintenance(lock_path, state_path):
    """Read a validated snapshot under the existing host lock; never authorize work."""
    descriptors = []
    try:
        parent, name = _parent(lock_path)
        descriptors.append(parent)
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        descriptors.append(fd)
        if not _private(os.fstat(fd)):
            raise MaintenanceDenied()
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        if _identity(os.fstat(fd)) != _identity(
            os.stat(name, dir_fd=parent, follow_symlinks=False)
        ):
            raise MaintenanceDenied()
        state_parent, state_name = _parent(state_path)
        descriptors.append(state_parent)
        return _state(state_parent, state_name)[0]
    except Exception:
        raise MaintenanceDenied() from None
    finally:
        for fd in reversed(descriptors):
            os.close(fd)


@contextmanager
def sealed_maintenance(
    lock_path, state_path, *, expected_generation, maintenance_id, request_sha256, authorize
):
    descriptors = []
    guard = None
    try:
        try:
            if (
                type(expected_generation) is not int
                or expected_generation < 0
                or not callable(authorize)
                or any(
                    type(v) is not str or not re.fullmatch("[0-9a-f]{64}", v)
                    for v in (maintenance_id, request_sha256)
                )
                or Path(lock_path) == Path(state_path)
            ):
                raise MaintenanceDenied()
            if authorize() is not None:
                raise MaintenanceDenied()
            lock_parent, lock_name = _parent(lock_path)
            descriptors.append(lock_parent)
            created = False
            try:
                descriptor = os.open(
                    lock_name,
                    os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_NONBLOCK,
                    0o600,
                    dir_fd=lock_parent,
                )
                created = True
            except FileExistsError:
                descriptor = os.open(
                    lock_name,
                    os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK,
                    dir_fd=lock_parent,
                )
            descriptors.append(descriptor)
            if created:
                os.fchmod(descriptor, 0o600)
            if not _private(os.fstat(descriptor)):
                raise MaintenanceDenied()
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if _identity(os.fstat(descriptor)) != _identity(
                os.stat(lock_name, dir_fd=lock_parent, follow_symlinks=False)
            ):
                raise MaintenanceDenied()
            state_parent, state_name = _parent(state_path)
            descriptors.append(state_parent)
            original, info = _state(state_parent, state_name)
            sealed = dict(
                schema_version=1,
                generation=expected_generation + 1,
                state="sealed",
                maintenance_id=maintenance_id,
                request_sha256=request_sha256,
            )
            if original != sealed:
                if original["state"] != "open" or original["generation"] != expected_generation:
                    raise MaintenanceDenied()
                if authorize() is not None:
                    raise MaintenanceDenied()
                temporary = ".maintenance-" + uuid.uuid4().hex
                fd = os.open(
                    temporary,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=state_parent,
                )
                try:
                    with os.fdopen(fd, "w") as output:
                        os.fchmod(output.fileno(), 0o600)
                        if not _private(os.fstat(output.fileno())):
                            raise MaintenanceDenied()
                        output.write(json.dumps(sealed, sort_keys=True) + "\n")
                        output.flush()
                        os.fsync(output.fileno())
                    if _identity(
                        os.stat(state_name, dir_fd=state_parent, follow_symlinks=False)
                    ) != _identity(info):
                        raise MaintenanceDenied()
                    os.replace(
                        temporary, state_name, src_dir_fd=state_parent, dst_dir_fd=state_parent
                    )
                    os.fsync(state_parent)
                finally:
                    try:
                        os.unlink(temporary, dir_fd=state_parent)
                    except FileNotFoundError:
                        pass
            else:
                # A prior attempt may have renamed the state but failed its
                # directory sync. Re-entry cannot assert durability from bytes
                # alone; synchronize again before exposing the guarded body.
                fd = os.open(
                    state_name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=state_parent
                )
                try:
                    if not _private(os.fstat(fd)) or _identity(os.fstat(fd)) != _identity(info):
                        raise MaintenanceDenied()
                    os.fsync(fd)
                    os.fsync(state_parent)
                finally:
                    os.close(fd)
            guard = MaintenanceGuard(
                descriptor, lock_parent, lock_name, state_parent, state_name, sealed, authorize
            )
            guard.checkpoint()
        except Exception:
            raise MaintenanceDenied() from None
        yield guard
    finally:
        if guard is not None:
            guard._active = False
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def seal_and_stop_container(lock_path, state_path, *, expected_generation, request, cli, authorize):
    """Stop one bound container while sealed; never restart or unseal on failure."""
    from ostag.core.container_stop import ContainerStopper, StopRequest

    if type(request) is not StopRequest:
        raise MaintenanceDenied()

    def check():
        return authorize(request)

    with sealed_maintenance(
        lock_path,
        state_path,
        expected_generation=expected_generation,
        maintenance_id=request.request_id,
        request_sha256=request.digest,
        authorize=check,
    ) as guard:

        def admitted(value):
            if value != request:
                raise MaintenanceDenied()
            guard.checkpoint()

        observation = ContainerStopper(cli, authorize=admitted).stop(request)
        guard.checkpoint()
        return observation
