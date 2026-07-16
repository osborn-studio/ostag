from __future__ import annotations

import base64
import json
import os
import re
import stat
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from Crypto.PublicKey import ECC
from Crypto.Signature import eddsa


KEYCHAIN_SERVICE = "ai.ostag.device-gateway"
_DEVICE_ID = re.compile(r"device_[0-9a-f]{32}\Z")
_ACCOUNT_LINE = re.compile(r'^\s*"acct"<blob>="([^"]+)"\s*$', re.MULTILINE)
Runner = Callable[[list[str]], subprocess.CompletedProcess[str]]


class DeviceIdentityError(RuntimeError):
    """Raised when device identity storage cannot be trusted."""


def _default_runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        argv,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


@dataclass(frozen=True)
class DeviceIdentity:
    device_id: str
    display_name: str
    public_key: str
    _signer: Callable[[bytes], str] = field(repr=False, compare=False)

    def canonical_sign(self, body: bytes) -> str:
        if not isinstance(body, bytes):
            raise TypeError("body must be bytes")
        return self._signer(body)

    def sign(self, body: bytes) -> str:
        return self.canonical_sign(body)


@dataclass(frozen=True)
class PendingDeviceKeyRotation:
    device_id: str
    public_key: str
    _commit: Callable[[], DeviceIdentity] = field(repr=False, compare=False)

    def commit(self) -> DeviceIdentity:
        return self._commit()


class DeviceIdentityStore:
    def __init__(self, path: Path, *, runner: Runner | None = None) -> None:
        self._path = path.expanduser()
        self._runner = runner or _default_runner

    def load_or_create(self, *, display_name: str) -> DeviceIdentity:
        if self._path.is_symlink():
            raise DeviceIdentityError("device identity file must not be a symbolic link")
        if self._path.exists():
            payload = self._read_metadata()
        else:
            payload = self._create_metadata(display_name)
        device_id, persisted_name, public_key = self._validate_metadata(payload)
        return DeviceIdentity(
            device_id=device_id,
            display_name=persisted_name,
            public_key=public_key,
            _signer=lambda body: self._sign(device_id, public_key, body),
        )

    def prepare_rotation(self, identity: DeviceIdentity) -> PendingDeviceKeyRotation:
        if identity.device_id != self.load_or_create(
            display_name=identity.display_name
        ).device_id:
            raise DeviceIdentityError("rotation identity does not match persisted device")
        old_private = self._load_private_pem(identity.device_id, identity.public_key)
        new_private = ECC.generate(curve="Ed25519")
        new_private_pem = new_private.export_key(format="PEM")
        new_public_pem = new_private.public_key().export_key(format="PEM")

        def commit() -> DeviceIdentity:
            self._store_private_pem(identity.device_id, new_private_pem)
            payload = {
                "device_id": identity.device_id,
                "display_name": identity.display_name,
                "public_key": new_public_pem,
            }
            try:
                self._write_metadata(payload)
            except BaseException:
                self._store_private_pem(identity.device_id, old_private)
                raise
            return self.load_or_create(display_name=identity.display_name)

        return PendingDeviceKeyRotation(
            device_id=identity.device_id,
            public_key=new_public_pem,
            _commit=commit,
        )

    def _create_metadata(self, display_name: str) -> dict[str, str]:
        name = self._validate_display_name(display_name)
        device_id = f"device_{uuid.uuid4().hex}"
        private_key = ECC.generate(curve="Ed25519")
        private_pem = private_key.export_key(format="PEM")
        public_pem = private_key.public_key().export_key(format="PEM")
        self._store_private_pem(device_id, private_pem)
        payload = {
            "device_id": device_id,
            "display_name": name,
            "public_key": public_pem,
        }
        self._write_metadata(payload)
        return payload

    def _store_private_pem(self, device_id: str, private_pem: str) -> None:
        result = self._runner([
            "/usr/bin/security",
            "add-generic-password",
            "-U",
            "-s",
            KEYCHAIN_SERVICE,
            "-a",
            device_id,
            "-w",
            private_pem,
        ])
        if result.returncode != 0:
            raise DeviceIdentityError("could not store device key in macOS Keychain")

    def _read_metadata(self) -> object:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(self._path, flags)
        except OSError as exc:
            raise DeviceIdentityError("could not safely open device identity file") from exc
        try:
            details = os.fstat(descriptor)
            if not stat.S_ISREG(details.st_mode):
                raise DeviceIdentityError("device identity path must be a regular file")
            if stat.S_IMODE(details.st_mode) != 0o600:
                raise DeviceIdentityError("device identity file permissions must be 0600")
            with os.fdopen(descriptor, encoding="utf-8") as stream:
                descriptor = -1
                return json.load(stream)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise DeviceIdentityError("device identity metadata is invalid") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    def _write_metadata(self, payload: dict[str, str]) -> None:
        parent = self._path.parent
        if parent.is_symlink():
            raise DeviceIdentityError("device identity directory must not be a symbolic link")
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = parent / f".{self._path.name}.{uuid.uuid4().hex}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(temporary, flags, 0o600)
        try:
            encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
            os.write(descriptor, encoded)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        try:
            os.replace(temporary, self._path)
            os.chmod(self._path, 0o600, follow_symlinks=False)
        finally:
            temporary.unlink(missing_ok=True)

    def _validate_metadata(self, payload: object) -> tuple[str, str, str]:
        if not isinstance(payload, dict) or set(payload) != {
            "device_id",
            "display_name",
            "public_key",
        }:
            raise DeviceIdentityError("device identity metadata has an invalid shape")
        device_id = payload["device_id"]
        public_key = payload["public_key"]
        if not isinstance(device_id, str) or _DEVICE_ID.fullmatch(device_id) is None:
            raise DeviceIdentityError("device identity has an invalid device_id")
        display_name = self._validate_display_name(payload["display_name"])
        if not isinstance(public_key, str) or len(public_key) > 2_000:
            raise DeviceIdentityError("device identity has an invalid public key")
        try:
            parsed = ECC.import_key(public_key)
        except (ValueError, IndexError, TypeError) as exc:
            raise DeviceIdentityError("device identity has an invalid public key") from exc
        if parsed.has_private() or parsed.curve != "Ed25519":
            raise DeviceIdentityError("device identity public key must be Ed25519 public material")
        return device_id, display_name, public_key

    @staticmethod
    def _validate_display_name(value: object) -> str:
        if not isinstance(value, str) or not value.strip() or len(value.strip()) > 200:
            raise DeviceIdentityError("device display name must be between 1 and 200 characters")
        return value.strip()

    def _sign(self, device_id: str, public_pem: str, body: bytes) -> str:
        private_pem = self._load_private_pem(device_id, public_pem)
        private_key = ECC.import_key(private_pem)
        signature = eddsa.new(private_key, mode="rfc8032").sign(body)
        return base64.urlsafe_b64encode(signature).decode("ascii").rstrip("=")

    def _load_private_pem(self, device_id: str, public_pem: str) -> str:
        metadata = self._runner([
            "/usr/bin/security",
            "find-generic-password",
            "-s",
            KEYCHAIN_SERVICE,
            "-a",
            device_id,
        ])
        if metadata.returncode != 0:
            raise DeviceIdentityError("device key is missing from macOS Keychain")
        account = _ACCOUNT_LINE.search(metadata.stdout)
        if account is None or account.group(1) != device_id:
            raise DeviceIdentityError("macOS Keychain account mismatch")
        secret = self._runner([
            "/usr/bin/security",
            "find-generic-password",
            "-s",
            KEYCHAIN_SERVICE,
            "-a",
            device_id,
            "-w",
        ])
        if secret.returncode != 0:
            raise DeviceIdentityError("device key is missing from macOS Keychain")
        try:
            private_key = ECC.import_key(secret.stdout.strip())
        except (ValueError, IndexError, TypeError) as exc:
            raise DeviceIdentityError("macOS Keychain contains an invalid device key") from exc
        if not private_key.has_private() or private_key.curve != "Ed25519":
            raise DeviceIdentityError("macOS Keychain device key must be private Ed25519 material")
        if private_key.public_key().export_key(format="PEM") != public_pem:
            raise DeviceIdentityError("macOS Keychain key does not match device metadata")
        return secret.stdout.strip()
