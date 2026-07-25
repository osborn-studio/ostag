from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Callable

from Crypto.PublicKey import ECC
from Crypto.Signature import eddsa

from ostag.core.identity import Principal

from .store import MonitorStore


_DEVICE_ID = re.compile(r"device_[A-Za-z0-9_-]{1,120}\Z")


class DeviceRegistryError(RuntimeError):
    pass


@dataclass(frozen=True)
class RegisteredDevice:
    device_id: str
    principal_platform: str
    actor_id: str
    display_name: str
    public_key: str
    gateway_version: str
    capabilities: tuple[str, ...]
    paired_at: str
    last_seen_at: str
    revoked_at: str

    def public_dict(self) -> dict[str, object]:
        return {
            "device_id": self.device_id,
            "display_name": self.display_name,
            "gateway_version": self.gateway_version,
            "capabilities": list(self.capabilities),
            "paired_at": self.paired_at,
            "last_seen_at": self.last_seen_at,
            "revoked": bool(self.revoked_at),
        }


class DeviceRegistry:
    def __init__(
        self,
        store: MonitorStore,
        *,
        clock: Callable[[], float] = time.time,
        entropy: Callable[[int], bytes] = secrets.token_bytes,
        max_clock_skew_seconds: int = 300,
    ) -> None:
        self._store = store
        self._clock = clock
        self._entropy = entropy
        self._max_clock_skew_seconds = max_clock_skew_seconds

    def create_pairing_code(self, owner: Principal, *, ttl_seconds: int = 600) -> str:
        if ttl_seconds < 1 or ttl_seconds > 600:
            raise DeviceRegistryError("pairing code TTL must be between 1 and 600 seconds")
        encoded = base64.b32encode(self._entropy(12)).decode("ascii").rstrip("=")
        code = "-".join((encoded[:8], encoded[8:16], encoded[16:]))
        digest = self._code_digest(code)
        with self._store.transaction() as connection:
            connection.execute(
                "INSERT INTO monitor_device_pairing_codes(code_digest,principal_platform,actor_id,expires_at,consumed_at) VALUES (?,?,?,?,NULL)",
                (digest, owner.platform, owner.actor_id, int(self._clock()) + ttl_seconds),
            )
        return code

    def pair_device(
        self,
        *,
        pairing_code: str,
        device_id: str,
        display_name: str,
        public_key: str,
    ) -> RegisteredDevice:
        self._validate_device_id(device_id)
        name = self._validate_display_name(display_name)
        canonical_public = self._validate_public_key(public_key)
        now = int(self._clock())
        paired_at = self._timestamp(now)
        digest = self._code_digest(pairing_code)
        try:
            with self._store.transaction() as connection:
                code = connection.execute(
                    "SELECT principal_platform,actor_id FROM monitor_device_pairing_codes WHERE code_digest=? AND consumed_at IS NULL AND expires_at>=?",
                    (digest, now),
                ).fetchone()
                if code is None:
                    raise DeviceRegistryError("pairing code is invalid or expired")
                consumed = connection.execute(
                    "UPDATE monitor_device_pairing_codes SET consumed_at=? WHERE code_digest=? AND consumed_at IS NULL",
                    (now, digest),
                )
                if consumed.rowcount != 1:
                    raise DeviceRegistryError("pairing code is invalid or expired")
                connection.execute(
                    "INSERT INTO monitor_devices(device_id,principal_platform,actor_id,display_name,public_key,paired_at) VALUES (?,?,?,?,?,?)",
                    (
                        device_id,
                        str(code["principal_platform"]),
                        str(code["actor_id"]),
                        name,
                        canonical_public,
                        paired_at,
                    ),
                )
        except DeviceRegistryError:
            raise
        except Exception as exc:
            raise DeviceRegistryError("device is already paired") from exc
        device = self.get_device(device_id)
        assert device is not None
        return device

    def get_device(self, device_id: str) -> RegisteredDevice | None:
        with self._store.connect() as connection:
            row = connection.execute(
                "SELECT * FROM monitor_devices WHERE device_id=?", (device_id,)
            ).fetchone()
        return None if row is None else self._device(row)

    def list_devices(self, owner: Principal) -> tuple[RegisteredDevice, ...]:
        with self._store.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM monitor_devices WHERE principal_platform=? AND actor_id=? ORDER BY paired_at,device_id",
                (owner.platform, owner.actor_id),
            ).fetchall()
        return tuple(self._device(row) for row in rows)

    def revoke_device(self, owner: Principal, device_id: str) -> None:
        with self._store.transaction() as connection:
            result = connection.execute(
                "UPDATE monitor_devices SET revoked_at=? WHERE device_id=? AND principal_platform=? AND actor_id=? AND revoked_at=''",
                (self._timestamp(int(self._clock())), device_id, owner.platform, owner.actor_id),
            )
            if result.rowcount != 1:
                raise DeviceRegistryError("active device not found")

    def rotate_key(self, device_id: str, *, new_public_key: str) -> RegisteredDevice:
        canonical = self._validate_public_key(new_public_key)
        with self._store.transaction() as connection:
            result = connection.execute(
                "UPDATE monitor_devices SET public_key=? WHERE device_id=? AND revoked_at=''",
                (canonical, device_id),
            )
            if result.rowcount != 1:
                raise DeviceRegistryError("active device not found")
        device = self.get_device(device_id)
        assert device is not None
        return device

    def record_heartbeat(
        self,
        device_id: str,
        *,
        gateway_version: str,
        runtimes: tuple[dict[str, object], ...],
    ) -> None:
        if not isinstance(gateway_version, str) or len(gateway_version) > 100:
            raise DeviceRegistryError("invalid gateway version")
        if not isinstance(runtimes, tuple) or len(runtimes) > 100:
            raise DeviceRegistryError("runtime inventory must be a bounded tuple")
        parsed: list[tuple[str, str, str, int]] = []
        seen: set[str] = set()
        for runtime in runtimes:
            if not isinstance(runtime, dict) or set(runtime) != {
                "runtime_id", "agent_type", "state", "session_count"
            }:
                raise DeviceRegistryError("invalid runtime inventory item")
            runtime_id = runtime["runtime_id"]
            agent_type = runtime["agent_type"]
            state = runtime["state"]
            session_count = runtime["session_count"]
            if (
                not isinstance(runtime_id, str)
                or not runtime_id
                or len(runtime_id) > 128
                or runtime_id in seen
                or agent_type not in {"codex", "claude-code", "other"}
                or state not in {"online", "degraded", "offline"}
                or isinstance(session_count, bool)
                or not isinstance(session_count, int)
                or not 0 <= session_count <= 10_000
            ):
                raise DeviceRegistryError("invalid runtime inventory item")
            seen.add(runtime_id)
            parsed.append((runtime_id, str(agent_type), str(state), session_count))
        now = self._timestamp(int(self._clock()))
        with self._store.transaction() as connection:
            active = connection.execute(
                "SELECT 1 FROM monitor_devices WHERE device_id=? AND revoked_at=''", (device_id,)
            ).fetchone()
            if active is None:
                raise DeviceRegistryError("active device not found")
            connection.execute(
                "UPDATE monitor_devices SET gateway_version=?,last_seen_at=? WHERE device_id=?",
                (gateway_version, now, device_id),
            )
            connection.execute("DELETE FROM monitor_device_runtimes WHERE device_id=?", (device_id,))
            connection.executemany(
                "INSERT INTO monitor_device_runtimes(device_id,runtime_id,agent_type,state,session_count,updated_at) VALUES (?,?,?,?,?,?)",
                [(device_id, *runtime, now) for runtime in parsed],
            )

    def list_runtimes(self, device_id: str) -> tuple[dict[str, object], ...]:
        with self._store.connect() as connection:
            rows = connection.execute(
                "SELECT runtime_id,agent_type,state,session_count FROM monitor_device_runtimes WHERE device_id=? ORDER BY runtime_id",
                (device_id,),
            ).fetchall()
        return tuple(dict(row) for row in rows)

    def connection_state(self, device_id: str) -> str:
        device = self.get_device(device_id)
        if device is None or device.revoked_at or not device.last_seen_at:
            return "offline"
        seen = datetime.fromisoformat(device.last_seen_at.replace("Z", "+00:00")).timestamp()
        age = max(0.0, self._clock() - seen)
        if age < 30:
            return "online"
        if age < 120:
            return "degraded"
        return "offline"

    @staticmethod
    def canonical_request(
        *, method: str, path: str, device_id: str, timestamp: str, nonce: str, body: bytes
    ) -> bytes:
        return "\n".join(
            (
                "v2",
                method.upper(),
                path,
                device_id,
                timestamp,
                nonce,
                hashlib.sha256(body).hexdigest(),
            )
        ).encode()

    def authenticate(
        self,
        *,
        device_id: str,
        timestamp: str,
        nonce: str,
        signature: str,
        method: str,
        path: str,
        body: bytes,
    ) -> RegisteredDevice:
        device = self.get_device(device_id)
        try:
            signed_at = int(timestamp)
            now = int(self._clock())
            fresh = abs(now - signed_at) <= self._max_clock_skew_seconds
            padded = signature + "=" * (-len(signature) % 4)
            raw_signature = base64.urlsafe_b64decode(padded.encode("ascii"))
            public_key = ECC.import_key(device.public_key if device else "")
            eddsa.new(public_key, mode="rfc8032").verify(
                self.canonical_request(
                    method=method,
                    path=path,
                    device_id=device_id,
                    timestamp=timestamp,
                    nonce=nonce,
                    body=body,
                ),
                raw_signature,
            )
            valid = bool(device and not device.revoked_at and nonce and fresh)
        except (ValueError, TypeError, UnicodeError):
            valid = False
            now = int(self._clock())
            signed_at = 0
        if not valid or not self._store.consume_instance_nonce(
            device_id,
            nonce,
            now=now,
            expires_at=signed_at + self._max_clock_skew_seconds,
        ):
            raise DeviceRegistryError("invalid device credentials")
        assert device is not None
        return device

    @staticmethod
    def _code_digest(code: str) -> str:
        normalized = code.strip().upper()
        if not normalized or len(normalized) > 64:
            raise DeviceRegistryError("pairing code is invalid or expired")
        return hashlib.sha256(normalized.encode()).hexdigest()

    @staticmethod
    def _validate_device_id(device_id: str) -> None:
        if not isinstance(device_id, str) or _DEVICE_ID.fullmatch(device_id) is None:
            raise DeviceRegistryError("invalid device id")

    @staticmethod
    def _validate_display_name(display_name: str) -> str:
        if not isinstance(display_name, str) or not display_name.strip() or len(display_name.strip()) > 200:
            raise DeviceRegistryError("display name must be between 1 and 200 characters")
        return display_name.strip()

    @staticmethod
    def _validate_public_key(public_key: str) -> str:
        if not isinstance(public_key, str) or len(public_key) > 2_000:
            raise DeviceRegistryError("invalid Ed25519 public key")
        try:
            parsed = ECC.import_key(public_key)
        except (ValueError, TypeError, IndexError) as exc:
            raise DeviceRegistryError("invalid Ed25519 public key") from exc
        if parsed.has_private() or parsed.curve != "Ed25519":
            raise DeviceRegistryError("invalid Ed25519 public key")
        return parsed.export_key(format="PEM")

    @staticmethod
    def _timestamp(value: int) -> str:
        return datetime.fromtimestamp(value, UTC).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _device(row: object) -> RegisteredDevice:
        record = dict(row)  # type: ignore[arg-type]
        return RegisteredDevice(
            device_id=str(record["device_id"]),
            principal_platform=str(record["principal_platform"]),
            actor_id=str(record["actor_id"]),
            display_name=str(record["display_name"]),
            public_key=str(record["public_key"]),
            gateway_version=str(record["gateway_version"]),
            capabilities=tuple(json.loads(str(record["capabilities_json"]))),
            paired_at=str(record["paired_at"]),
            last_seen_at=str(record["last_seen_at"]),
            revoked_at=str(record["revoked_at"]),
        )
