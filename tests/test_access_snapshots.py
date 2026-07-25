"""Phase 3: agent access-profile snapshots (shadow mode)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from ostag.core.access_snapshot import (
    AccessConstraints,
    AgentAccessProfileSnapshot,
    AgentPrincipal,
    ConnectionRef,
    OstagBinding,
    PermissionGrantRef,
    PrincipalRef,
)
from ostag.core.identity import Principal
from ostag.core.schemas import SchemaError
from ostag.work_monitor.access_snapshots import AccessSnapshotStore
from ostag.work_monitor.identity import IdentityStore
from ostag.work_monitor.models import MonitorEvent, TaskSnapshot
from ostag.work_monitor.projection import Projector
from ostag.work_monitor.store import MIGRATIONS, MonitorStore

NOW = "2026-07-26T10:00:00+00:00"
LATER = "2026-07-27T00:00:00+00:00"

ALICE = Principal(platform="feishu", actor_id="alice")
BOB = Principal(platform="feishu", actor_id="bob")
OWNER = Principal(platform="feishu", actor_id="owner")


def snapshot(**overrides) -> AgentAccessProfileSnapshot:
    base = dict(
        id="aps_task-1",
        agent_principal=AgentPrincipal(id="codex", kind="provider_agent"),
        requested_by=PrincipalRef(platform="ostag", actor_id="instance:macbook"),
        policy_snapshot_id="ostag:macbook:unversioned",
        captured_at=NOW,
        ostag_binding=OstagBinding(instance_id="macbook"),
        project_targets=("ostag",),
        permissions=(PermissionGrantRef(scope="code.fix", reason="declared per-task capability"),),
    )
    base.update(overrides)
    return AgentAccessProfileSnapshot(**base)


def event(
    instance: str,
    sequence: int,
    task_id: str,
    *,
    kind: str = "task.created",
    agent: str = "codex",
) -> MonitorEvent:
    return MonitorEvent(
        event_id=f"{instance}-{sequence}",
        instance_id=instance,
        sequence=sequence,
        kind=kind,
        occurred_at=f"2026-07-26T10:00:0{sequence}+00:00",
        task=TaskSnapshot(
            task_id=task_id,
            title=f"Task {task_id}",
            project="ostag",
            status="todo",
            agent=agent,
            progress=0,
            duration_seconds=0,
            summary="safe summary",
            error_class=None,
            evidence_ref=None,
            version=sequence,
        ),
    )


# -- model invariants ---------------------------------------------------------


class TestSnapshotModel:
    def test_round_trip_preserves_content(self):
        snap = snapshot(
            connection_refs=(
                ConnectionRef(
                    id="conn-1",
                    provider="github",
                    custody="operator",
                    broker_ref="broker/secret-ref",
                    declared_capabilities=("repo.read",),
                ),
            ),
            constraints=AccessConstraints(locality="local_required", maximum_risk_tier="medium"),
            expires_at=LATER,
        )
        restored = AgentAccessProfileSnapshot.from_dict(snap.to_dict())
        assert restored == snap
        assert restored.content_digest() == snap.content_digest()

    def test_expires_at_must_be_after_captured_at(self):
        with pytest.raises(SchemaError) as excinfo:
            snapshot(expires_at=NOW)
        assert "expiresAt" in str(excinfo.value)

    def test_expiry_comparison_is_parsed_not_lexical(self):
        # "...Z" sorts after "...+00:00" lexically but is the same instant.
        with pytest.raises(SchemaError):
            snapshot(captured_at="2026-07-26T10:00:00+00:00", expires_at="2026-07-26T10:00:00Z")

    def test_revoked_at_cannot_precede_captured_at(self):
        with pytest.raises(SchemaError) as excinfo:
            snapshot(revoked_at="2026-07-26T09:59:59+00:00")
        assert "revokedAt" in str(excinfo.value)

    def test_revoked_at_equal_to_captured_at_is_allowed(self):
        assert snapshot(revoked_at=NOW).revoked_at == NOW

    def test_unknown_wire_key_is_rejected(self):
        data = snapshot().to_dict()
        data["surprise"] = True
        with pytest.raises(SchemaError):
            AgentAccessProfileSnapshot.from_dict(data)

    def test_invalid_enums_are_rejected(self):
        with pytest.raises(SchemaError):
            AgentPrincipal(id="x", kind="robot")
        with pytest.raises(SchemaError):
            ConnectionRef(id="c", provider="p", custody="stolen", broker_ref="b")
        with pytest.raises(SchemaError):
            AccessConstraints(locality="anywhere")
        with pytest.raises(SchemaError):
            AccessConstraints(maximum_risk_tier="extreme")

    def test_non_utc_timestamp_is_rejected(self):
        with pytest.raises(SchemaError):
            snapshot(captured_at="2026-07-26T10:00:00+08:00")

    def test_digest_is_stable_and_content_sensitive(self):
        a = snapshot()
        assert a.content_digest() == snapshot().content_digest()
        assert a.content_digest() != snapshot(policy_snapshot_id="ostag:other:unversioned").content_digest()
        assert a.content_digest().startswith("sha256:")

    def test_redacted_view_leaks_no_broker_refs_or_reasons(self):
        snap = snapshot(
            connection_refs=(
                ConnectionRef(
                    id="conn-1",
                    provider="github",
                    custody="operator",
                    broker_ref="broker/SECRET-REF",
                ),
            ),
            permissions=(
                PermissionGrantRef(scope="code.fix", reason="INTERNAL sensitive rationale"),
            ),
        )
        rendered = json.dumps(snap.redacted_view())
        assert "SECRET-REF" not in rendered
        assert "brokerRef" not in rendered
        assert "INTERNAL sensitive rationale" not in rendered
        assert snap.redacted_view()["permissionScopes"] == ["code.fix"]
        assert snap.redacted_view()["contentDigest"] == snap.content_digest()


# -- persistence --------------------------------------------------------------


@pytest.fixture()
def env(tmp_path: Path):
    monitor = MonitorStore(tmp_path / "monitor.db")
    identities = IdentityStore(tmp_path / "identity.sqlite3")
    identities.bind_instance(ALICE, "macbook", display_name="A", bound_at=NOW)
    identities.bind_instance(BOB, "macmini", display_name="B", bound_at=NOW)
    with identities._connect() as connection:  # seed a private owner binding
        connection.execute(
            "INSERT INTO monitor_bindings(platform,destination_id,chat_type,owner_platform,"
            "owner_actor_id,role,scope,revoked) VALUES('feishu','p2p_owner','private',"
            "'feishu','owner','owner','',0)"
        )
        connection.commit()
    return monitor, AccessSnapshotStore(monitor, identities)


class TestAccessSnapshotStore:
    def test_record_and_get_round_trip(self, env):
        _, store = env
        assert store.record("macbook", snapshot(), work_thread_key="macbook/task-1") is True
        assert store.get("macbook", "aps_task-1") == snapshot()

    def test_duplicate_identical_record_is_idempotent(self, env):
        _, store = env
        store.record("macbook", snapshot(), work_thread_key="macbook/task-1")
        assert store.record("macbook", snapshot(), work_thread_key="macbook/task-1") is False

    def test_changed_content_is_rejected_immutable(self, env):
        _, store = env
        store.record("macbook", snapshot(), work_thread_key="macbook/task-1")
        with pytest.raises(SchemaError) as excinfo:
            store.record(
                "macbook",
                snapshot(project_targets=("other",)),
                work_thread_key="macbook/task-1",
            )
        assert excinfo.value.code == "immutable_record"

    def test_instances_are_isolated(self, env):
        _, store = env
        store.record("macbook", snapshot(), work_thread_key="macbook/task-1")
        with pytest.raises(SchemaError) as excinfo:
            store.get("macmini", "aps_task-1")
        assert excinfo.value.code == "not_found"

    def test_personal_views_are_scoped_to_own_instances(self, env):
        _, store = env
        store.record("macbook", snapshot(), work_thread_key="macbook/task-1")
        store.record(
            "macmini",
            snapshot(
                id="aps_task-9",
                ostag_binding=OstagBinding(instance_id="macmini"),
                requested_by=PrincipalRef(platform="ostag", actor_id="instance:macmini"),
            ),
            work_thread_key="macmini/task-9",
        )
        alice_views = store.personal_views(ALICE)
        assert [view["id"] for view in alice_views] == ["aps_task-1"]
        assert "aps_task-9" not in json.dumps(alice_views)

    def test_admin_views_require_global_owner(self, env):
        _, store = env
        store.record("macbook", snapshot(), work_thread_key="macbook/task-1")
        with pytest.raises(SchemaError) as excinfo:
            store.admin_views(ALICE)
        assert excinfo.value.code == "permission_denied"
        assert [view["id"] for view in store.admin_views(OWNER)] == ["aps_task-1"]

    def test_views_without_identity_store_are_denied(self, tmp_path):
        store = AccessSnapshotStore(MonitorStore(tmp_path / "m.db"))
        with pytest.raises(SchemaError) as excinfo:
            store.personal_views(ALICE)
        assert excinfo.value.code == "permission_denied"


# -- shadow capture through the projector -------------------------------------


class TestProjectorShadowCapture:
    def test_task_created_event_captures_snapshot(self, env):
        monitor, store = env
        projector = Projector(monitor, access_snapshots=store)
        result = projector.apply(event("macbook", 1, "task-1"))
        assert result.applied_sequences == (1,)
        snap = store.get("macbook", "aps_task-1")
        assert snap.agent_principal == AgentPrincipal(id="codex", kind="provider_agent")
        assert snap.project_targets == ("ostag",)
        # per-task capabilities travel on heartbeats, not task events — the
        # shadow snapshot records only what the intake event proves.
        assert snap.permissions == ()
        assert snap.ostag_binding.instance_id == "macbook"
        assert snap.captured_at == "2026-07-26T10:00:01+00:00"

    def test_unknown_agent_maps_to_custom_kind(self, env):
        monitor, store = env
        projector = Projector(monitor, access_snapshots=store)
        projector.apply(event("macbook", 1, "task-1", agent="in-house-bot"))
        assert store.get("macbook", "aps_task-1").agent_principal.kind == "custom"

    def test_non_created_events_do_not_capture(self, env):
        monitor, store = env
        projector = Projector(monitor, access_snapshots=store)
        projector.apply(event("macbook", 1, "task-1", kind="task.progress"))
        with pytest.raises(SchemaError):
            store.get("macbook", "aps_task-1")

    def test_duplicate_event_does_not_recapture(self, env):
        monitor, store = env
        projector = Projector(monitor, access_snapshots=store)
        projector.apply(event("macbook", 1, "task-1"))
        assert projector.apply(event("macbook", 1, "task-1")).duplicate is True
        assert store.get("macbook", "aps_task-1") is not None

    def test_buffered_created_event_is_captured_when_drained(self, env):
        monitor, store = env
        projector = Projector(monitor, access_snapshots=store)
        buffered = projector.apply(event("macbook", 2, "task-2"))
        assert buffered.buffered is True
        with pytest.raises(SchemaError):
            store.get("macbook", "aps_task-2")
        drained = projector.apply(event("macbook", 1, "task-1"))
        assert drained.applied_sequences == (1, 2)
        assert store.get("macbook", "aps_task-1") is not None
        assert store.get("macbook", "aps_task-2") is not None

    def test_capture_failure_never_breaks_projection(self, env, monkeypatch):
        monitor, store = env

        def boom(_event):
            raise RuntimeError("shadow capture exploded")

        monkeypatch.setattr(store, "capture_from_task_event", boom)
        projector = Projector(monitor, access_snapshots=store)
        result = projector.apply(event("macbook", 1, "task-1"))
        assert result.stored is True
        assert result.applied_sequences == (1,)
        assert monitor.get_task("macbook", "task-1").status == "todo"

    def test_projector_without_hook_is_unchanged(self, env):
        monitor, _ = env
        projector = Projector(monitor)
        result = projector.apply(event("macbook", 1, "task-1"))
        assert result.applied_sequences == (1,)

    def test_hooked_projection_is_byte_identical_to_unhooked(self, tmp_path):
        """Shadow capture must not perturb projection: identical event corpus →
        identical ApplyResults and identical events/tasks/pending tables."""
        corpus = [
            event("macbook", 1, "task-1"),
            event("macbook", 3, "task-3"),  # gap → buffered
            event("macbook", 2, "task-2"),  # drains 2 and 3
            event("macbook", 2, "task-2"),  # duplicate
            event("macmini", 1, "task-9", kind="task.progress", agent="in-house-bot"),
        ]
        plain_store = MonitorStore(tmp_path / "plain.db")
        hooked_store = MonitorStore(tmp_path / "hooked.db")
        plain = Projector(plain_store)
        hooked = Projector(hooked_store, access_snapshots=AccessSnapshotStore(hooked_store))

        for item in corpus:
            assert plain.apply(item) == hooked.apply(item)

        def table_dump(store: MonitorStore, table: str) -> list[tuple]:
            with store.connect() as connection:
                return [
                    tuple(row)
                    for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid")
                ]

        for table in ("events", "tasks", "pending_events", "instances"):
            assert table_dump(plain_store, table) == table_dump(hooked_store, table)


# -- schema migration ---------------------------------------------------------


def test_migration_v22_is_additive_from_v21_fixture(tmp_path: Path) -> None:
    """A database created at v21 opens, keeps its data, and gains the snapshot table."""
    db_path = tmp_path / "monitor.db"
    previous_version = len(MIGRATIONS) - 1
    connection = sqlite3.connect(db_path)
    try:
        for migration in MIGRATIONS[:previous_version]:
            for statement in migration.statements:
                try:
                    connection.execute(statement)
                except sqlite3.OperationalError:
                    pass  # duplicate-column idempotency mirrors MonitorStore._migrate
        connection.execute(
            "INSERT INTO tasks(instance_id,task_id,title,project,status,agent,progress,"
            "duration_seconds,summary,occurred_at) VALUES('macmini','t1','旧任务','p',"
            "'done','codex',100,5,'legacy row','2026-07-01T00:00:00+00:00')"
        )
        connection.execute(f"PRAGMA user_version = {previous_version}")
        connection.commit()
    finally:
        connection.close()

    store = MonitorStore(db_path)  # triggers migration to v22
    with store.connect() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)
        row = conn.execute(
            "SELECT title FROM tasks WHERE instance_id='macmini' AND task_id='t1'"
        ).fetchone()
        assert row["title"] == "旧任务"
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='access_profile_snapshots'"
            )
        }
        assert tables == {"access_profile_snapshots"}
    # the migrated database accepts snapshot writes
    AccessSnapshotStore(store).record(
        "macmini", snapshot(), work_thread_key="macmini/task-1"
    )
