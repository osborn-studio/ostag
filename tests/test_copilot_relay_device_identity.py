from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from ostag.config import load_settings
from ostag.copilot_relay.device_identity import DeviceIdentityError, DeviceIdentityStore


class FakeSecurityRunner:
    def __init__(self) -> None:
        self.items: dict[tuple[str, str], str] = {}
        self.reported_account: str | None = None

    def __call__(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        service = argv[argv.index("-s") + 1]
        account = argv[argv.index("-a") + 1]
        key = (service, account)
        if argv[1] == "add-generic-password":
            self.items[key] = argv[argv.index("-w") + 1]
            return subprocess.CompletedProcess(argv, 0, "", "")
        if key not in self.items:
            return subprocess.CompletedProcess(argv, 44, "", "not found")
        if "-w" in argv:
            return subprocess.CompletedProcess(argv, 0, self.items[key] + "\n", "")
        reported = self.reported_account or account
        return subprocess.CompletedProcess(argv, 0, f'    "acct"<blob>="{reported}"\n', "")


def test_load_or_create_persists_only_public_device_metadata(tmp_path: Path) -> None:
    path = tmp_path / "device-identity.json"
    runner = FakeSecurityRunner()
    store = DeviceIdentityStore(path, runner=runner)

    identity = store.load_or_create(display_name="MacBook Pro")

    assert identity.device_id.startswith("device_")
    assert identity.display_name == "MacBook Pro"
    assert identity.public_key.startswith("-----BEGIN PUBLIC KEY-----")
    assert store.load_or_create(display_name="Renamed").device_id == identity.device_id
    assert identity.sign(b"canonical-request") != identity.sign(b"other-request")
    assert path.stat().st_mode & 0o777 == 0o600
    payload = json.loads(path.read_text())
    assert set(payload) == {"device_id", "display_name", "public_key"}
    assert "PRIVATE" not in path.read_text()


def test_macos_keychain_hex_encoded_password_is_decoded_before_import(tmp_path) -> None:
    class HexSecurityRunner(FakeSecurityRunner):
        def __call__(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
            result = super().__call__(argv)
            if argv[1] == "find-generic-password" and "-w" in argv and result.returncode == 0:
                result = subprocess.CompletedProcess(
                    argv, 0, result.stdout.strip().encode().hex() + "\n", ""
                )
            return result

    store = DeviceIdentityStore(
        tmp_path / "device-identity.json", runner=HexSecurityRunner()
    )
    identity = store.load_or_create(display_name="MacBook Pro")

    assert identity.sign(b"signed-heartbeat")


def test_identity_file_rejects_symlinks(tmp_path: Path) -> None:
    target = tmp_path / "target.json"
    target.write_text("{}")
    path = tmp_path / "device-identity.json"
    path.symlink_to(target)

    with pytest.raises(DeviceIdentityError, match="symbolic link"):
        DeviceIdentityStore(path, runner=FakeSecurityRunner()).load_or_create(
            display_name="Mac mini"
        )


def test_keychain_account_must_match_persisted_device(tmp_path: Path) -> None:
    path = tmp_path / "device-identity.json"
    runner = FakeSecurityRunner()
    store = DeviceIdentityStore(path, runner=runner)
    identity = store.load_or_create(display_name="Mac mini")
    runner.reported_account = "device_someone_else"

    with pytest.raises(DeviceIdentityError, match="account mismatch"):
        identity.sign(b"request")


def test_key_rotation_does_not_replace_local_key_until_commit(tmp_path: Path) -> None:
    path = tmp_path / "device-identity.json"
    runner = FakeSecurityRunner()
    store = DeviceIdentityStore(path, runner=runner)
    identity = store.load_or_create(display_name="Mac mini")

    pending = store.prepare_rotation(identity)

    assert store.load_or_create(display_name="Mac mini").public_key == identity.public_key
    assert pending.public_key != identity.public_key
    rotated = pending.commit()
    assert rotated.device_id == identity.device_id
    assert rotated.public_key == pending.public_key
    assert rotated.sign(b"after-rotation")


def test_settings_expose_device_identity_path_and_display_name(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    identity_path = tmp_path / "identity.json"
    monkeypatch.setenv("OSTAG_DEVICE_IDENTITY_FILE", os.fspath(identity_path))
    monkeypatch.setenv("OSTAG_DEVICE_DISPLAY_NAME", "Studio Mac mini")

    settings = load_settings()

    assert settings.device_identity_file == identity_path
    assert settings.device_display_name == "Studio Mac mini"
