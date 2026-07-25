"""Shadow persistence + capture for agent access-profile snapshots (Phase 3).

Snapshots are captured after an intake event has been projected (a
post-projection, best-effort hook — never on the routing path), stored
immutably with a content digest, and read back only through the same
instance-scoped authorization boundary the completion boards use. Nothing in
this module influences admission, candidate filtering, route selection, or
fallback.
"""

from __future__ import annotations

import json

from ostag.core.access_snapshot import (
    AgentAccessProfileSnapshot,
    AgentPrincipal,
    OstagBinding,
    PrincipalRef,
)
from ostag.core.identity import Principal
from ostag.core.schemas import SchemaError
from ostag.work_monitor.identity import IdentityStore
from ostag.work_monitor.models import MonitorEvent
from ostag.work_monitor.store import MonitorStore

# Adapters shipped by OStag itself map to upstream "provider_agent"; anything
# else declared on a task is "custom" (declared in the Phase 3 mapping note).
_PROVIDER_AGENTS = frozenset({"codex", "claude", "claude-code", "gemini", "cursor"})


def snapshot_id_for(task_id: str) -> str:
    return f"aps_{task_id}"


class AccessSnapshotStore:
    """Immutable, instance-scoped storage for access-profile snapshots."""

    def __init__(self, store: MonitorStore, identities: IdentityStore | None = None) -> None:
        self.store = store
        self.identities = identities

    def record(
        self,
        instance_id: str,
        snapshot: AgentAccessProfileSnapshot,
        *,
        work_thread_key: str,
    ) -> bool:
        """Insert-or-verify an immutable snapshot row. Returns True when created."""
        payload = json.dumps(snapshot.to_dict(), sort_keys=True, separators=(",", ":"))
        with self.store.transaction() as connection:
            row = connection.execute(
                "SELECT snapshot_json FROM access_profile_snapshots "
                "WHERE instance_id=? AND snapshot_id=?",
                (instance_id, snapshot.id),
            ).fetchone()
            if row is not None:
                if row["snapshot_json"] != payload:
                    raise SchemaError(
                        code="immutable_record",
                        message=f"AgentAccessProfileSnapshot {snapshot.id} is immutable",
                        recovery_hint="Record a snapshot under a new id instead of editing.",
                    )
                return False
            connection.execute(
                "INSERT INTO access_profile_snapshots(instance_id,snapshot_id,"
                "work_thread_key,content_digest,snapshot_json,captured_at) "
                "VALUES(?,?,?,?,?,?)",
                (
                    instance_id,
                    snapshot.id,
                    work_thread_key,
                    snapshot.content_digest(),
                    payload,
                    snapshot.captured_at,
                ),
            )
            return True

    def get(self, instance_id: str, snapshot_id: str) -> AgentAccessProfileSnapshot:
        with self.store.connect() as connection:
            row = connection.execute(
                "SELECT snapshot_json FROM access_profile_snapshots "
                "WHERE instance_id=? AND snapshot_id=?",
                (instance_id, snapshot_id),
            ).fetchone()
        if row is None:
            raise SchemaError(
                code="not_found",
                message=f"AgentAccessProfileSnapshot {snapshot_id} not found for {instance_id}",
                recovery_hint="Snapshots exist only for tasks admitted after Phase 3.",
            )
        return AgentAccessProfileSnapshot.from_dict(json.loads(row["snapshot_json"]))

    # -- authorized read surfaces ---------------------------------------------

    def personal_views(self, principal: Principal) -> tuple[dict, ...]:
        """Redacted views for the principal's own instances only."""
        identities = self._require_identities()
        views: list[dict] = []
        for instance_id in sorted(identities.personal_instances(principal)):
            views.extend(self._redacted_views(instance_id))
        return tuple(views)

    def admin_views(self, principal: Principal) -> tuple[dict, ...]:
        """Redacted views across all instances; requires a bound global owner."""
        identities = self._require_identities()
        if not identities.is_global_owner(principal):
            raise SchemaError(
                code="permission_denied",
                message="Team access-snapshot views require a bound global owner",
                recovery_hint="Use the personal view, or bind an owner/admin principal.",
            )
        views: list[dict] = []
        for instance_id in sorted(identities.team_instances()):
            views.extend(self._redacted_views(instance_id))
        return tuple(views)

    def _require_identities(self) -> IdentityStore:
        if self.identities is None:
            raise SchemaError(
                code="permission_denied",
                message="Access-snapshot views require an identity store",
                recovery_hint="Construct AccessSnapshotStore with an IdentityStore.",
            )
        return self.identities

    def _redacted_views(self, instance_id: str) -> list[dict]:
        with self.store.connect() as connection:
            rows = connection.execute(
                "SELECT snapshot_json FROM access_profile_snapshots WHERE instance_id=? "
                "ORDER BY snapshot_id",
                (instance_id,),
            ).fetchall()
        return [
            AgentAccessProfileSnapshot.from_dict(
                json.loads(row["snapshot_json"])
            ).redacted_view()
            for row in rows
        ]

    # -- shadow capture -------------------------------------------------------

    def capture_from_task_event(self, event: MonitorEvent) -> bool:
        """Shadow capture on ``task.created``; deterministic snapshot id.

        Everything recorded is real event data: the agent and project come
        from the projected ``TaskSnapshot``. Permission grants are left empty
        at capture — per-task capabilities travel on ``instance.heartbeat``
        events, not task events, and the shadow phase records only what the
        intake event itself proves (declared in the mapping note). Requester
        attribution is the instance's system principal (OStag intake events
        do not carry a human requester), and the policy snapshot id is the
        declared-unversioned native placeholder.
        """
        if event.kind != "task.created" or event.task is None:
            return False
        task = event.task
        snapshot = AgentAccessProfileSnapshot(
            id=snapshot_id_for(task.task_id),
            agent_principal=AgentPrincipal(
                id=task.agent,
                kind="provider_agent" if task.agent in _PROVIDER_AGENTS else "custom",
            ),
            requested_by=PrincipalRef(platform="ostag", actor_id=f"instance:{event.instance_id}"),
            policy_snapshot_id=f"ostag:{event.instance_id}:unversioned",
            captured_at=event.occurred_at,
            ostag_binding=OstagBinding(instance_id=event.instance_id),
            project_targets=(task.project,) if task.project else (),
        )
        return self.record(
            event.instance_id,
            snapshot,
            work_thread_key=f"{event.instance_id}/{task.task_id}",
        )
