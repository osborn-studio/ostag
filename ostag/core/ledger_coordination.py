"""Explicit stable-lock coordination for a provisioned private ledger.

This is a locking primitive, NOT evidence that old/unmanaged writers stopped and
NOT permission to replace a ledger. Every writer in a future managed domain must
use the same installed lock; trusted supervisor exclusion is still mandatory.
The default RunLedger remains legacy until the host explicitly supplies this.
"""
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import os
from pathlib import Path
import stat
from time import monotonic, sleep

from ostag.core.schemas import SchemaError


def _denied():
    return SchemaError('ledger_coordination_unavailable', 'Ledger coordination is unavailable.',
                       'Verify the managed ledger domain; do not recreate or bypass its lock.')


def _path(value):
    if not isinstance(value, (str, Path)) or not str(value).strip():
        raise _denied()
    path = Path(value).absolute()
    if '..' in path.parts:
        raise _denied()
    # Never normalize symlinks away. Every existing component must be direct.
    for part in (path, *path.parents):
        if part.is_symlink():
            raise _denied()
    parent = path.parent.lstat()
    if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.geteuid() or parent.st_mode & 0o022:
        raise _denied()
    return path


def _private(info):
    return (stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid()
            and info.st_nlink == 1 and not info.st_mode & 0o077)


def _identity(info):
    return info.st_dev, info.st_ino


def _verify(path, descriptor):
    opened, current = os.fstat(descriptor), path.lstat()
    if not _private(opened) or not _private(current) or _identity(opened) != _identity(current):
        raise _denied()
    return _identity(opened)


def _acquire(descriptor, mode, deadline):
    if deadline is None:
        fcntl.flock(descriptor, mode)
        return
    while True:
        if monotonic() >= deadline:
            raise SchemaError('ledger_busy', 'Frozen proposal could not be recorded.',
                              'Verify current ledger custody before retrying.')
        try:
            fcntl.flock(descriptor, mode | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            if monotonic() >= deadline:
                raise SchemaError('ledger_busy', 'Frozen proposal could not be recorded.',
                                  'Verify current ledger custody before retrying.') from None
            sleep(.001)


@dataclass(frozen=True)
class LedgerCoordination:
    ledger_path: Path
    path: Path
    identity: tuple[int, int]

    def __post_init__(self):
        try:
            if (_path(self.ledger_path) != self.ledger_path
                    or self.path != self.ledger_path.with_name(self.ledger_path.name + '.coordination.lock')
                    or type(self.identity) is not tuple or len(self.identity) != 2
                    or any(type(v) is not int or v < 0 for v in self.identity)):
                raise _denied()
        except OSError:
            raise _denied() from None

    @classmethod
    def open(cls, ledger_path):
        """Read-only admission to a preinstalled domain; no fallback or creation."""
        try:
            ledger = _path(ledger_path)
            if not _private(ledger.lstat()):
                raise _denied()
            lock = _path(ledger.with_name(ledger.name + '.coordination.lock'))
            descriptor = os.open(lock, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            try:
                identity = _verify(lock, descriptor)
            finally:
                os.close(descriptor)
            return cls(ledger, lock, identity)
        except OSError:
            raise _denied() from None

    @classmethod
    def install(cls, ledger_path):
        """Provision once offline; does not establish old-worker exclusion.

        Existing locks are validated, never truncated/replaced. A failed install
        may leave a provisioned lock; preserve it for inspection, not blind retry.
        """
        try:
            ledger = _path(ledger_path)
            if not _private(ledger.lstat()):
                raise _denied()
            lock = _path(ledger.with_name(ledger.name + '.coordination.lock'))
            try:
                descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            except FileExistsError:
                descriptor = os.open(lock, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            try:
                _verify(lock, descriptor)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            directory = os.open(ledger.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            return cls.open(ledger)
        except OSError:
            raise _denied() from None

    def bind(self, ledger_path):
        try:
            if _path(ledger_path) != self.ledger_path:
                raise _denied()
        except OSError:
            raise _denied() from None

    @contextmanager
    def hold(self, *, exclusive=False, deadline=None):
        """Lock before opening the ledger; caller chooses shared or exclusive.

        Exclusive possession only excludes cooperating clients, not old binaries.
        It is never a maintenance authorization token.
        """
        descriptor = None
        try:
            if _path(self.path) != self.path:
                raise _denied()
            descriptor = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            if _verify(self.path, descriptor) != self.identity:
                raise _denied()
            _acquire(descriptor, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH, deadline)
            if _verify(self.path, descriptor) != self.identity:
                raise _denied()
            yield
        except OSError:
            raise _denied() from None
        finally:
            if descriptor is not None:
                os.close(descriptor)  # closes/releases even if validation/acquisition failed

    def verify_ledger(self, handle):
        try:
            _path(self.ledger_path)
            _verify(self.ledger_path, handle.fileno())
        except OSError:
            raise _denied() from None
