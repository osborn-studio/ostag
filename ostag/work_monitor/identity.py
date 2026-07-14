"""Platform-scoped identities and monitor destination enrollment."""

from __future__ import annotations

import hmac
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from ostag.core.identity import Principal


class IdentityError(PermissionError):
    pass


@dataclass(frozen=True)
class Destination:
    platform: str
    destination_id: str
    chat_type: str

    def __post_init__(self) -> None:
        if not self.platform or not self.destination_id:
            raise ValueError("destination platform and destination_id are required")
        if self.chat_type not in {"private", "group"}:
            raise ValueError("chat_type must be private or group")

    def to_dict(self) -> dict[str, str]:
        return {
            "platform": self.platform,
            "destination_id": self.destination_id,
            "chat_type": self.chat_type,
        }


@dataclass(frozen=True)
class Binding:
    owner: Principal
    destination: Destination
    role: str = "owner"
    scope: str = ""

    def __post_init__(self) -> None:
        if self.role not in {"owner", "admin"}:
            raise ValueError("binding role must be owner or admin")
        if self.destination.chat_type == "group" and not self.scope:
            raise ValueError("group binding scope is required")
        if self.destination.chat_type == "private" and self.scope:
            raise ValueError("private binding scope must be empty")


class IdentityStore:
    """Stores owner bindings, grants, and subscriptions with platform in every key."""

    def __init__(self, path: str | Path, *, enrollment_secret: str = "") -> None:
        self.path = Path(path)
        self.enrollment_secret = enrollment_secret
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS monitor_bindings (
                    platform TEXT NOT NULL, destination_id TEXT NOT NULL,
                    chat_type TEXT NOT NULL, owner_platform TEXT NOT NULL,
                    owner_actor_id TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'owner',
                    scope TEXT NOT NULL DEFAULT '',
                    revoked INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(platform, destination_id)
                );
                CREATE TABLE IF NOT EXISTS monitor_grants (
                    platform TEXT NOT NULL, destination_id TEXT NOT NULL,
                    principal_platform TEXT NOT NULL, actor_id TEXT NOT NULL,
                    capability TEXT NOT NULL, revoked INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(platform, destination_id, principal_platform, actor_id, capability)
                );
                CREATE TABLE IF NOT EXISTS monitor_subscriptions (
                    principal_platform TEXT NOT NULL, actor_id TEXT NOT NULL,
                    destination_platform TEXT NOT NULL, destination_id TEXT NOT NULL,
                    instance_id TEXT NOT NULL DEFAULT '', task_id TEXT NOT NULL DEFAULT '',
                    delivery_cursor INTEGER NOT NULL DEFAULT 0,
                    revoked INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(principal_platform, actor_id, destination_platform,
                                destination_id, instance_id, task_id)
                );
                CREATE TABLE IF NOT EXISTS monitor_consumed_enrollment_secrets (
                    secret_digest TEXT PRIMARY KEY, consumed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                """
            )
            binding_columns = {row[1] for row in connection.execute("PRAGMA table_info(monitor_bindings)")}
            if "role" not in binding_columns:
                connection.execute("ALTER TABLE monitor_bindings ADD COLUMN role TEXT NOT NULL DEFAULT 'owner'")
            if "scope" not in binding_columns:
                connection.execute("ALTER TABLE monitor_bindings ADD COLUMN scope TEXT NOT NULL DEFAULT ''")
            subscription_columns = {row[1] for row in connection.execute("PRAGMA table_info(monitor_subscriptions)")}
            if "delivery_cursor" not in subscription_columns:
                connection.execute("ALTER TABLE monitor_subscriptions ADD COLUMN delivery_cursor INTEGER NOT NULL DEFAULT 0")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _same_platform(principal: Principal, destination: Destination) -> None:
        if principal.platform != destination.platform:
            raise IdentityError("principal and destination platforms do not match")

    def bind_private(
        self,
        *,
        owner: Principal,
        destination: Destination,
        enrollment_secret: str = "",
        local_admin: bool = False,
    ) -> None:
        self._same_platform(owner, destination)
        if destination.chat_type != "private":
            raise IdentityError("private owner binding requires a private destination")
        valid_secret = bool(self.enrollment_secret) and hmac.compare_digest(
            enrollment_secret, self.enrollment_secret
        )
        if not local_admin and not valid_secret:
            raise IdentityError("local-admin or valid enrollment secret required")
        digest = hmac.digest(b"ostag-monitor-enrollment", enrollment_secret.encode(), "sha256").hex()
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                if valid_secret and not local_admin:
                    connection.execute(
                        "INSERT INTO monitor_consumed_enrollment_secrets(secret_digest) VALUES (?)",
                        (digest,),
                    )
                self._upsert_binding(
                    owner,
                    destination,
                    role="admin" if local_admin else "owner",
                    connection=connection,
                    local_admin=local_admin,
                )
                connection.commit()
        except sqlite3.IntegrityError as exc:
            raise IdentityError("enrollment secret already consumed or destination already owned") from exc

    def enroll_group(
        self,
        owner: Principal,
        destination: Destination,
        *,
        via: Destination | None = None,
        scope: str,
        local_admin: bool = False,
    ) -> None:
        self._same_platform(owner, destination)
        if destination.chat_type != "group":
            raise IdentityError("group enrollment requires a group destination")
        if via is None and not local_admin:
            raise IdentityError("group enrollment requires an explicit authorization scope")
        if not local_admin and (via is None or via.chat_type != "private" or not self.is_owner(owner, via)):
            raise IdentityError("only the destination owner may enroll a group")
        if not scope.strip():
            raise IdentityError("group enrollment scope is required")
        self._upsert_binding(
            owner, destination, role="admin" if local_admin else "owner",
            scope=scope.strip(), local_admin=local_admin
        )

    def _upsert_binding(
        self,
        owner: Principal,
        destination: Destination,
        *,
        role: str = "owner",
        scope: str = "",
        connection: sqlite3.Connection | None = None,
        local_admin: bool = False,
    ) -> None:
        owns_connection = connection is None
        connection = connection or self._connect()
        try:
            cursor = connection.execute(
                "INSERT INTO monitor_bindings(platform,destination_id,chat_type,owner_platform,owner_actor_id,role,scope,revoked) VALUES (?,?,?,?,?,?,?,0) "
                "ON CONFLICT(platform,destination_id) DO UPDATE SET "
                "chat_type=excluded.chat_type,owner_platform=excluded.owner_platform,"
                "owner_actor_id=excluded.owner_actor_id,role=excluded.role,scope=excluded.scope,revoked=0 "
                "WHERE monitor_bindings.owner_platform=excluded.owner_platform "
                "AND monitor_bindings.owner_actor_id=excluded.owner_actor_id OR ?=1",
                (
                    destination.platform,
                    destination.destination_id,
                    destination.chat_type,
                    owner.platform,
                    owner.actor_id,
                    role,
                    scope,
                    int(local_admin),
                ),
            )
            if cursor.rowcount == 0:
                raise IdentityError("destination is already bound to another owner")
            if owns_connection:
                connection.commit()
        finally:
            if owns_connection:
                connection.close()

    def binding(self, destination: Destination) -> Binding | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT owner_platform,owner_actor_id,role,scope FROM monitor_bindings "
                "WHERE platform=? AND destination_id=? AND revoked=0",
                (destination.platform, destination.destination_id),
            ).fetchone()
        return None if row is None else Binding(
            Principal(str(row["owner_platform"]), str(row["owner_actor_id"])),
            destination,
            str(row["role"]),
            str(row["scope"]),
        )

    def is_owner(self, principal: Principal, destination: Destination) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM monitor_bindings WHERE platform=? AND destination_id=? "
                "AND owner_platform=? AND owner_actor_id=? AND revoked=0",
                (
                    destination.platform,
                    destination.destination_id,
                    principal.platform,
                    principal.actor_id,
                ),
            ).fetchone()
        return row is not None

    def is_global_owner(self, principal: Principal) -> bool:
        """A bound private owner/admin may operate across registered employee instances."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM monitor_bindings WHERE chat_type='private' "
                "AND owner_platform=? AND owner_actor_id=? AND role IN ('owner','admin') "
                "AND revoked=0 LIMIT 1",
                (principal.platform, principal.actor_id),
            ).fetchone()
        return row is not None

    def allowed_instances(self, principal: Principal) -> frozenset[str] | None:
        """Return None for global owners, otherwise exact instance scopes granted to an actor."""
        if self.is_global_owner(principal):
            return None
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT DISTINCT b.scope FROM monitor_grants g JOIN monitor_bindings b "
                "ON b.platform=g.platform AND b.destination_id=g.destination_id "
                "WHERE g.principal_platform=? AND g.actor_id=? AND g.revoked=0 AND b.revoked=0 "
                "AND g.capability IN ('read','propose','write','admin')",
                (principal.platform, principal.actor_id),
            ).fetchall()
        scopes = {str(row["scope"]) for row in rows}
        return frozenset(scope.removeprefix("instance:") for scope in scopes if scope.startswith("instance:"))

    def list_bindings(self, owner: Principal) -> tuple[Destination, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT platform,destination_id,chat_type FROM monitor_bindings "
                "WHERE owner_platform=? AND owner_actor_id=? AND revoked=0 "
                "ORDER BY platform,destination_id",
                (owner.platform, owner.actor_id),
            ).fetchall()
        return tuple(Destination(**dict(row)) for row in rows)

    def bindings(self, owner: Principal) -> tuple[Binding, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT platform,destination_id,chat_type,role,scope FROM monitor_bindings "
                "WHERE owner_platform=? AND owner_actor_id=? AND revoked=0 "
                "ORDER BY platform,destination_id",
                (owner.platform, owner.actor_id),
            ).fetchall()
        return tuple(
            Binding(
                owner=owner,
                destination=Destination(
                    str(row["platform"]), str(row["destination_id"]), str(row["chat_type"])
                ),
                role=str(row["role"]),
                scope=str(row["scope"]),
            )
            for row in rows
        )

    def revoke(
        self, owner: Principal, destination: Destination, *, local_admin: bool = False
    ) -> None:
        if not local_admin and not self.is_owner(owner, destination):
            raise IdentityError("only the destination owner may revoke it")
        with self._connect() as connection:
            connection.execute(
                "UPDATE monitor_bindings SET revoked=1 WHERE platform=? AND destination_id=?",
                (destination.platform, destination.destination_id),
            )
            connection.execute(
                "UPDATE monitor_grants SET revoked=1 WHERE platform=? AND destination_id=?",
                (destination.platform, destination.destination_id),
            )
            connection.execute(
                "UPDATE monitor_subscriptions SET revoked=1 WHERE destination_platform=? AND destination_id=?",
                (destination.platform, destination.destination_id),
            )

    def grant(
        self,
        owner: Principal,
        destination: Destination,
        principal: Principal,
        capability: str,
    ) -> None:
        if not self.is_owner(owner, destination):
            raise IdentityError("only the destination owner may grant access")
        self._same_platform(principal, destination)
        if capability not in {"read", "propose", "write", "admin"}:
            raise ValueError("unsupported monitor capability")
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO monitor_grants VALUES (?,?,?,?,?,0) "
                "ON CONFLICT DO UPDATE SET revoked=0",
                (
                    destination.platform,
                    destination.destination_id,
                    principal.platform,
                    principal.actor_id,
                    capability,
                ),
            )

    def allows(self, principal: Principal, destination: Destination, capability: str) -> bool:
        if self.is_owner(principal, destination):
            return True
        accepted = {
            "read": ("read", "propose", "write", "admin"),
            "propose": ("propose", "write", "admin"),
            "write": ("write", "admin"),
            "admin": ("admin",),
        }.get(capability, ())
        if not accepted:
            return False
        placeholders = ",".join("?" for _ in accepted)
        with self._connect() as connection:
            row = connection.execute(
                f"SELECT 1 FROM monitor_grants WHERE platform=? AND destination_id=? "  # noqa: S608
                f"AND principal_platform=? AND actor_id=? AND capability IN ({placeholders}) "
                "AND revoked=0",
                (
                    destination.platform,
                    destination.destination_id,
                    principal.platform,
                    principal.actor_id,
                    *accepted,
                ),
            ).fetchone()
        return row is not None

    def subscribe(
        self,
        principal: Principal,
        destination: Destination,
        *,
        instance_id: str = "",
        task_id: str = "",
        delivery_cursor: int = 0,
    ) -> None:
        if delivery_cursor < 0:
            raise ValueError("delivery_cursor must be non-negative")
        if not self.allows(principal, destination, "propose"):
            raise IdentityError("propose permission required")
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO monitor_subscriptions(principal_platform,actor_id,destination_platform,destination_id,instance_id,task_id,delivery_cursor,revoked) VALUES (?,?,?,?,?,?,?,0) "
                "ON CONFLICT DO UPDATE SET delivery_cursor=MAX(delivery_cursor,excluded.delivery_cursor),revoked=0",
                (
                    principal.platform,
                    principal.actor_id,
                    destination.platform,
                    destination.destination_id,
                    instance_id,
                    task_id,
                    delivery_cursor,
                ),
            )

    def advance_subscription_cursor(
        self, principal: Principal, destination: Destination, *, instance_id: str = "",
        task_id: str = "", delivery_cursor: int,
    ) -> int:
        if delivery_cursor < 0:
            raise ValueError("delivery_cursor must be non-negative")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "UPDATE monitor_subscriptions SET delivery_cursor=MAX(delivery_cursor,?) "
                "WHERE principal_platform=? AND actor_id=? AND destination_platform=? "
                "AND destination_id=? AND instance_id=? AND task_id=? AND revoked=0 RETURNING delivery_cursor",
                (delivery_cursor, principal.platform, principal.actor_id, destination.platform,
                 destination.destination_id, instance_id, task_id),
            ).fetchone()
            if row is None:
                raise IdentityError("active subscription not found")
        return int(row["delivery_cursor"])

    def subscriptions(
        self, principal: Principal, destination: Destination
    ) -> tuple[tuple[str, str, int], ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT s.instance_id,s.task_id,s.delivery_cursor FROM monitor_subscriptions s "
                "JOIN monitor_bindings b ON b.platform=s.destination_platform "
                "AND b.destination_id=s.destination_id AND b.revoked=0 WHERE "
                "s.principal_platform=? AND s.actor_id=? AND s.destination_platform=? "
                "AND s.destination_id=? AND s.revoked=0 ORDER BY s.instance_id,s.task_id",
                (
                    principal.platform,
                    principal.actor_id,
                    destination.platform,
                    destination.destination_id,
                ),
            ).fetchall()
        return tuple((str(row["instance_id"]), str(row["task_id"]), int(row["delivery_cursor"])) for row in rows)
