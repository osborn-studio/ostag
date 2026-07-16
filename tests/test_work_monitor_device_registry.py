from __future__ import annotations

import base64
from pathlib import Path

import pytest
from Crypto.PublicKey import ECC
from Crypto.Signature import eddsa

from ostag.core.identity import Principal
from ostag.work_monitor.device_registry import DeviceRegistry, DeviceRegistryError
from ostag.work_monitor.store import MonitorStore


def _keys() -> tuple[object, str]:
    private = ECC.generate(curve="Ed25519")
    return private, private.public_key().export_key(format="PEM")


def _signature(private: object, canonical: bytes) -> str:
    raw = eddsa.new(private, mode="rfc8032").sign(canonical)
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def test_pairing_code_is_one_time_expiring_and_stores_no_private_key(tmp_path: Path) -> None:
    registry = DeviceRegistry(MonitorStore(tmp_path / "monitor.sqlite3"), clock=lambda: 100)
    owner = Principal("feishu", "ou_owner")
    _, public_key = _keys()
    code = registry.create_pairing_code(owner)

    paired = registry.pair_device(
        pairing_code=code,
        device_id="device_macbook",
        display_name="MacBook Pro",
        public_key=public_key,
    )

    assert paired.device_id == "device_macbook"
    assert registry.list_devices(owner) == (paired,)
    assert "PRIVATE" not in registry._store.path.read_text(errors="ignore")
    with pytest.raises(DeviceRegistryError, match="invalid or expired"):
        registry.pair_device(
            pairing_code=code,
            device_id="device_replay",
            display_name="Replay",
            public_key=public_key,
        )

    expired = registry.create_pairing_code(owner, ttl_seconds=10)
    registry._clock = lambda: 111
    with pytest.raises(DeviceRegistryError, match="invalid or expired"):
        registry.pair_device(
            pairing_code=expired,
            device_id="device_expired",
            display_name="Expired",
            public_key=public_key,
        )


def test_signature_authentication_revocation_and_old_key_rotation(tmp_path: Path) -> None:
    now = 1_000
    registry = DeviceRegistry(MonitorStore(tmp_path / "monitor.sqlite3"), clock=lambda: now)
    owner = Principal("feishu", "ou_owner")
    old_private, old_public = _keys()
    code = registry.create_pairing_code(owner)
    registry.pair_device(
        pairing_code=code,
        device_id="device_macmini",
        display_name="Mac mini",
        public_key=old_public,
    )
    body = b'{"ok":true}'
    canonical = registry.canonical_request(
        method="POST",
        path="/v1/copilot/events",
        device_id="device_macmini",
        timestamp=str(now),
        nonce="nonce-1",
        body=body,
    )
    authenticated = registry.authenticate(
        device_id="device_macmini",
        timestamp=str(now),
        nonce="nonce-1",
        signature=_signature(old_private, canonical),
        method="POST",
        path="/v1/copilot/events",
        body=body,
    )
    assert authenticated.device_id == "device_macmini"
    with pytest.raises(DeviceRegistryError, match="invalid device credentials"):
        registry.authenticate(
            device_id="device_macmini",
            timestamp=str(now),
            nonce="nonce-1",
            signature=_signature(old_private, canonical),
            method="POST",
            path="/v1/copilot/events",
            body=body,
        )

    new_private, new_public = _keys()
    registry.rotate_key("device_macmini", new_public_key=new_public)
    new_canonical = registry.canonical_request(
        method="POST",
        path="/v1/copilot/events",
        device_id="device_macmini",
        timestamp=str(now),
        nonce="nonce-2",
        body=body,
    )
    assert registry.authenticate(
        device_id="device_macmini",
        timestamp=str(now),
        nonce="nonce-2",
        signature=_signature(new_private, new_canonical),
        method="POST",
        path="/v1/copilot/events",
        body=body,
    ).public_key == new_public

    registry.revoke_device(owner, "device_macmini")
    with pytest.raises(DeviceRegistryError, match="invalid device credentials"):
        registry.authenticate(
            device_id="device_macmini",
            timestamp=str(now),
            nonce="nonce-3",
            signature="invalid",
            method="POST",
            path="/v1/copilot/events",
            body=body,
        )


@pytest.mark.parametrize("name", ["", " ", "x" * 201])
def test_pairing_rejects_invalid_display_names(tmp_path: Path, name: str) -> None:
    registry = DeviceRegistry(MonitorStore(tmp_path / "monitor.sqlite3"), clock=lambda: 1)
    owner = Principal("feishu", "owner")
    code = registry.create_pairing_code(owner)
    _, public_key = _keys()

    with pytest.raises(DeviceRegistryError, match="display name"):
        registry.pair_device(
            pairing_code=code,
            device_id="device_invalid",
            display_name=name,
            public_key=public_key,
        )
