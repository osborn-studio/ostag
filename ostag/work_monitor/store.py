"""SQLite persistence for the OStag work monitor."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from .models import MonitorEvent, TASK_STATUSES


@dataclass(frozen=True)
class Migration:
    statements: tuple[str, ...]


_V1_STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS instances (
    instance_id TEXT PRIMARY KEY,
    last_sequence INTEGER NOT NULL DEFAULT 0,
    last_seen_at TEXT
)""",
    """CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    instance_id TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    kind TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    payload TEXT NOT NULL,
    UNIQUE(instance_id, sequence)
)""",
    """CREATE TABLE IF NOT EXISTS pending_events (
    instance_id TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    event_id TEXT NOT NULL UNIQUE,
    payload TEXT NOT NULL,
    PRIMARY KEY(instance_id, sequence)
)""",
    """CREATE TABLE IF NOT EXISTS tasks (
    instance_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    title TEXT NOT NULL,
    project TEXT NOT NULL,
    status TEXT NOT NULL,
    agent TEXT NOT NULL,
    progress INTEGER NOT NULL,
    duration_seconds INTEGER NOT NULL,
    summary TEXT NOT NULL,
    error_class TEXT,
    evidence_ref TEXT,
    occurred_at TEXT NOT NULL,
    PRIMARY KEY(instance_id, task_id)
)""",
    """CREATE TABLE IF NOT EXISTS subscriptions (
    platform TEXT NOT NULL,
    destination_id TEXT NOT NULL,
    instance_id TEXT NOT NULL DEFAULT '',
    task_id TEXT NOT NULL DEFAULT '',
    delivery_cursor INTEGER NOT NULL DEFAULT 0,
    UNIQUE(platform, destination_id, instance_id, task_id)
)""",
    """CREATE TABLE IF NOT EXISTS notification_outbox (
    notification_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL,
    destination_key TEXT NOT NULL,
    payload TEXT NOT NULL,
    delivered_at TEXT,
    UNIQUE(event_id, destination_key)
)""",
    """CREATE TABLE IF NOT EXISTS commands (
    command_id TEXT PRIMARY KEY,
    instance_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    status TEXT NOT NULL,
    payload TEXT NOT NULL,
    consumed_at TEXT
)""",
)

# V2 is deliberately self-healing for databases that declared v1 after creating
# only a subset of its tables. The version column is handled conditionally.
MIGRATIONS = (
    Migration(statements=_V1_STATEMENTS),
    Migration(
        statements=(
            *_V1_STATEMENTS,
            "ALTER TABLE tasks ADD COLUMN version INTEGER NOT NULL DEFAULT 1",
        )
    ),
    Migration(
        statements=(
            "ALTER TABLE commands ADD COLUMN action_id TEXT NOT NULL DEFAULT ''",
            "CREATE UNIQUE INDEX IF NOT EXISTS commands_action_id_unique "
            "ON commands(action_id) WHERE action_id <> ''",
        )
    ),
    Migration(
        statements=(
            "ALTER TABLE commands ADD COLUMN ack_payload TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE commands ADD COLUMN ack_hash TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE commands ADD COLUMN ledger_synced INTEGER NOT NULL DEFAULT 0",
        )
    ),
    Migration(
        statements=(
            "ALTER TABLE commands ADD COLUMN lease_until TEXT NOT NULL DEFAULT ''",
            "CREATE TABLE IF NOT EXISTS instance_nonces (instance_id TEXT NOT NULL, nonce TEXT NOT NULL, expires_at INTEGER NOT NULL, PRIMARY KEY(instance_id,nonce))",
        )
    ),
    Migration(
        statements=(
            "CREATE TABLE IF NOT EXISTS telegram_updates (update_id INTEGER PRIMARY KEY, consumed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)",
            "CREATE TABLE IF NOT EXISTS telegram_callbacks (callback_id TEXT PRIMARY KEY, action TEXT NOT NULL, task_id TEXT NOT NULL, destination_id TEXT NOT NULL, chat_type TEXT NOT NULL, consumed_at TEXT)",
        )
    ),
    Migration(
        statements=(
            "ALTER TABLE telegram_callbacks ADD COLUMN principal_platform TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE telegram_callbacks ADD COLUMN actor_id TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE telegram_callbacks ADD COLUMN expires_at INTEGER NOT NULL DEFAULT 0",
        )
    ),
    Migration(
        statements=(
            "ALTER TABLE telegram_callbacks ADD COLUMN control_action TEXT NOT NULL DEFAULT ''",
        )
    ),
    Migration(
        statements=(
            *_V1_STATEMENTS,
            "ALTER TABLE notification_outbox ADD COLUMN delivery_id TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE notification_outbox ADD COLUMN state TEXT NOT NULL DEFAULT 'pending'",
            "ALTER TABLE notification_outbox ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE notification_outbox ADD COLUMN next_attempt_at TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE notification_outbox ADD COLUMN lease_owner TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE notification_outbox ADD COLUMN lease_until TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE notification_outbox ADD COLUMN delayed INTEGER NOT NULL DEFAULT 0",
            "CREATE UNIQUE INDEX IF NOT EXISTS notification_delivery_unique ON notification_outbox(delivery_id) WHERE delivery_id <> ''",
            "CREATE TABLE IF NOT EXISTS monitor_schedule_windows (window_key TEXT PRIMARY KEY, created_at TEXT NOT NULL)",
        )
    ),
    Migration(
        statements=(
            "CREATE TABLE IF NOT EXISTS legacy_outbox_audit (notification_id TEXT PRIMARY KEY, audited INTEGER NOT NULL DEFAULT 0)",
            "INSERT OR IGNORE INTO legacy_outbox_audit(notification_id) SELECT notification_id FROM notification_outbox WHERE delivery_id=''",
            "UPDATE notification_outbox SET state='suppressed' WHERE delivery_id='' AND state='pending'",
        )
    ),
    Migration(
        statements=(
            "ALTER TABLE notification_outbox ADD COLUMN lease_generation INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE notification_outbox ADD COLUMN failure_transient INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE notification_outbox ADD COLUMN deadlettered_at TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE notification_outbox ADD COLUMN audit_pending INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE notification_outbox ADD COLUMN ledger_synced INTEGER NOT NULL DEFAULT 0",
        )
    ),
    Migration(
        statements=(
            "CREATE TABLE IF NOT EXISTS monitor_scheduler_owners (owner_id TEXT PRIMARY KEY, lease_until TEXT NOT NULL, heartbeat_at TEXT NOT NULL)",
        )
    ),
    Migration(
        statements=(
            "CREATE TABLE IF NOT EXISTS daily_digest_windows (window_key TEXT PRIMARY KEY, created_at TEXT NOT NULL, delivered_at TEXT NOT NULL DEFAULT '')",
            "CREATE TABLE IF NOT EXISTS daily_digest_outbox (delivery_id TEXT PRIMARY KEY, window_key TEXT NOT NULL, platform TEXT NOT NULL, destination_id TEXT NOT NULL, payload TEXT NOT NULL, fallback_text TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at TEXT NOT NULL, delivered_at TEXT NOT NULL DEFAULT '', last_error TEXT NOT NULL DEFAULT '')",
        )
    ),
    Migration(
        statements=(
            "CREATE TABLE IF NOT EXISTS relay_sessions (instance_id TEXT NOT NULL, session_id TEXT NOT NULL, task_id TEXT NOT NULL, run_id TEXT NOT NULL, agent_id TEXT NOT NULL, title TEXT NOT NULL, state TEXT NOT NULL, session_version INTEGER NOT NULL, last_turn_id TEXT NOT NULL, latest_summary TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY(instance_id,session_id))",
            "CREATE TABLE IF NOT EXISTS relay_messages (message_id TEXT PRIMARY KEY, direction TEXT NOT NULL, kind TEXT NOT NULL, binding_json TEXT NOT NULL, payload_json TEXT NOT NULL, state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at TEXT NOT NULL, lease_owner TEXT NOT NULL DEFAULT '', lease_until TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, delivered_at TEXT NOT NULL DEFAULT '', applied_at TEXT NOT NULL DEFAULT '', last_error TEXT NOT NULL DEFAULT '')",
            "CREATE INDEX IF NOT EXISTS relay_messages_delivery ON relay_messages(direction,state,next_attempt_at,lease_until)",
            "CREATE TABLE IF NOT EXISTS relay_decisions (decision_id TEXT PRIMARY KEY, instance_id TEXT NOT NULL, session_id TEXT NOT NULL, turn_id TEXT NOT NULL, session_version INTEGER NOT NULL, question TEXT NOT NULL, choices_json TEXT NOT NULL, state TEXT NOT NULL, expires_at TEXT NOT NULL, answered_at TEXT NOT NULL DEFAULT '')",
            "CREATE TABLE IF NOT EXISTS relay_current_tasks (platform TEXT NOT NULL, actor_id TEXT NOT NULL, instance_id TEXT NOT NULL, session_id TEXT NOT NULL, selected_at TEXT NOT NULL, PRIMARY KEY(platform,actor_id))",
            "CREATE TABLE IF NOT EXISTS relay_task_proposals (proposal_id TEXT PRIMARY KEY, platform TEXT NOT NULL, actor_id TEXT NOT NULL, payload_json TEXT NOT NULL, state TEXT NOT NULL, expires_at TEXT NOT NULL, confirmed_at TEXT NOT NULL DEFAULT '')",
        )
    ),
    Migration(
        statements=(
            "CREATE TABLE IF NOT EXISTS relay_instance_cursors (instance_id TEXT PRIMARY KEY,last_sequence INTEGER NOT NULL DEFAULT 0,updated_at TEXT NOT NULL DEFAULT '')",
        )
    ),
)


@dataclass(frozen=True)
class StoredTask:
    instance_id: str
    task_id: str
    status: str
    version: int


class MonitorStore:
    """Owns schema creation and small read-only monitor queries."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._migrate()

    def _migrate(self) -> None:
        with self.connect() as connection:
            row = connection.execute("PRAGMA user_version").fetchone()
            current_version = int(row[0])
            if current_version > len(MIGRATIONS):
                raise RuntimeError(
                    f"database schema version {current_version} is newer than supported "
                    f"version {len(MIGRATIONS)}"
                )
            for version, migration in enumerate(MIGRATIONS, start=1):
                if version <= current_version:
                    continue
                connection.execute("BEGIN IMMEDIATE")
                try:
                    for statement in migration.statements:
                        if statement.startswith("ALTER TABLE"):
                            table = statement.split()[2]
                            column_name = statement.split("ADD COLUMN", 1)[1].strip().split()[0]
                            columns = {
                                str(column[1])
                                for column in connection.execute(f"PRAGMA table_info({table})")
                            }
                            if column_name in columns:
                                continue
                        connection.execute(statement)
                    connection.execute(f"PRAGMA user_version = {version}")
                    connection.commit()
                except BaseException:
                    connection.rollback()
                    raise

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def last_sequence(self, instance_id: str) -> int:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT last_sequence FROM instances WHERE instance_id = ?", (instance_id,)
            ).fetchone()
        return 0 if row is None else int(row["last_sequence"])

    def last_seen_at(self, instance_id: str) -> str | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT last_seen_at FROM instances WHERE instance_id = ?", (instance_id,)
            ).fetchone()
        return None if row is None else row["last_seen_at"]

    def pending_sequences(self, instance_id: str) -> tuple[int, ...]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT sequence FROM pending_events WHERE instance_id = ? ORDER BY sequence",
                (instance_id,),
            ).fetchall()
        return tuple(int(row["sequence"]) for row in rows)

    def get_task(self, instance_id: str, task_id: str) -> StoredTask | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT instance_id, task_id, status, version FROM tasks "
                "WHERE instance_id = ? AND task_id = ?",
                (instance_id, task_id),
            ).fetchone()
        return None if row is None else StoredTask(**dict(row))

    def claim_pending_commands(
        self, instance_id: str, *, claimed_at: str, lease_until: str
    ) -> tuple[dict[str, Any], ...]:
        """Atomically return each pending command to one authenticated consumer."""
        with self.transaction() as connection:
            rows = connection.execute(
                "SELECT command_id,payload FROM commands WHERE instance_id=? AND status='pending' "
                "AND (lease_until='' OR lease_until<=?) "
                "AND json_extract(payload,'$.expires_at')>? ORDER BY rowid",
                (instance_id, claimed_at, claimed_at),
            ).fetchall()
            connection.executemany(
                "UPDATE commands SET consumed_at=?,lease_until=? WHERE command_id=?",
                [(claimed_at, lease_until, row["command_id"]) for row in rows],
            )
        return tuple(json.loads(str(row["payload"])) for row in rows)

    def consume_instance_nonce(
        self, instance_id: str, nonce: str, *, now: int, expires_at: int
    ) -> bool:
        try:
            with self.transaction() as connection:
                connection.execute("DELETE FROM instance_nonces WHERE expires_at<?", (now,))
                connection.execute(
                    "INSERT INTO instance_nonces(instance_id,nonce,expires_at) VALUES (?,?,?)",
                    (instance_id, nonce, expires_at),
                )
        except sqlite3.IntegrityError:
            return False
        return True

    def consume_telegram_update(self, update_id: int) -> bool:
        try:
            with self.transaction() as connection:
                connection.execute(
                    "INSERT INTO telegram_updates(update_id) VALUES (?)", (update_id,)
                )
        except sqlite3.IntegrityError:
            return False
        return True

    def create_telegram_callback(
        self,
        callback_id: str,
        *,
        action: str,
        task_id: str,
        destination_id: str,
        chat_type: str,
        principal_platform: str = "",
        actor_id: str = "",
        expires_at: int = 0,
        control_action: str = "",
    ) -> None:
        self.create_control_callback(
            callback_id, action=action, task_id=task_id, destination_id=destination_id,
            chat_type=chat_type, principal_platform=principal_platform, actor_id=actor_id,
            expires_at=expires_at, control_action=control_action,
        )

    def create_control_callback(
        self, callback_id: str, *, action: str, task_id: str, destination_id: str,
        chat_type: str, principal_platform: str, actor_id: str, expires_at: int,
        control_action: str = "",
    ) -> None:
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO telegram_callbacks(callback_id,action,task_id,destination_id,chat_type,"
                "principal_platform,actor_id,expires_at,control_action) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    callback_id,
                    action,
                    task_id,
                    destination_id,
                    chat_type,
                    principal_platform,
                    actor_id,
                    expires_at,
                    control_action,
                ),
            )

    def consume_telegram_callback(
        self,
        callback_id: str,
        *,
        principal_platform: str = "",
        actor_id: str = "",
        destination_id: str = "",
        chat_type: str = "",
        now: int = 0,
    ) -> dict[str, str] | None:
        return self.consume_control_callback(
            callback_id, principal_platform=principal_platform, actor_id=actor_id,
            destination_id=destination_id, chat_type=chat_type, now=now,
        )

    def consume_control_callback(
        self, callback_id: str, *, principal_platform: str, actor_id: str,
        destination_id: str, chat_type: str, now: int,
    ) -> dict[str, str] | None:
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT action,task_id,destination_id,chat_type,principal_platform,actor_id,"
                "expires_at,control_action,consumed_at "
                "FROM telegram_callbacks WHERE callback_id=?",
                (callback_id,),
            ).fetchone()
            if (
                row is None
                or row["consumed_at"] is not None
                or (principal_platform and row["principal_platform"] != principal_platform)
                or (actor_id and row["actor_id"] != actor_id)
                or (destination_id and row["destination_id"] != destination_id)
                or (chat_type and row["chat_type"] != chat_type)
                or (now and int(row["expires_at"]) <= now)
            ):
                return None
            connection.execute(
                "UPDATE telegram_callbacks SET consumed_at=CURRENT_TIMESTAMP WHERE callback_id=?",
                (callback_id,),
            )
        return {
            key: str(row[key])
            for key in ("action", "task_id", "destination_id", "chat_type", "control_action")
        }

    def stage_counts(self, instance_id: str | None = None) -> dict[str, int]:
        parameters: tuple[str, ...] = ()
        where = ""
        if instance_id is not None:
            where = " WHERE instance_id = ?"
            parameters = (instance_id,)
        with self.connect() as connection:
            rows = connection.execute(
                f"SELECT status, COUNT(*) AS count FROM tasks{where} GROUP BY status",  # noqa: S608
                parameters,
            ).fetchall()
        observed = {str(row["status"]): int(row["count"]) for row in rows}
        return {status: observed.get(status, 0) for status in sorted(TASK_STATUSES)}

    def event_count(self) -> int:
        with self.connect() as connection:
            row = connection.execute("SELECT COUNT(*) AS count FROM events").fetchone()
        assert row is not None
        return int(row["count"])


def encode_event(event: MonitorEvent) -> str:
    return json.dumps(event.to_dict(), separators=(",", ":"), sort_keys=True)


def decode_event(payload: str) -> MonitorEvent:
    return MonitorEvent.from_dict(json.loads(payload))
