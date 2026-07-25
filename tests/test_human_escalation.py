"""Phase 4: structured, time-bounded human escalation."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from ostag.core.completion import ActorRef
from ostag.core.escalation import (
    ActionHint,
    EscalationAcknowledgement,
    EscalationResolution,
    HumanEscalation,
    HumanEscalationOption,
)
from ostag.core.identity import Principal
from ostag.core.schemas import SchemaError
from ostag.work_monitor.escalation_store import EscalationStore
from ostag.work_monitor.identity import IdentityStore
from ostag.work_monitor.store import MIGRATIONS, MonitorStore

NOW = "2026-07-26T10:00:00+00:00"
LATER = "2026-07-26T11:00:00+00:00"
DEADLINE = "2026-07-26T12:00:00+00:00"
AFTER_DEADLINE = "2026-07-26T12:00:01+00:00"

ALICE = Principal(platform="feishu", actor_id="alice")
OWNER = Principal(platform="feishu", actor_id="owner")
HUMAN = ActorRef(actor_id="alice", handle="@alice")
OTHER_HUMAN = ActorRef(actor_id="bob")

THREAD = "macbook/task-1"


def escalation(**overrides) -> HumanEscalation:
    base = dict(
        id="esc_1",
        work_thread_key=THREAD,
        escalation_class="approval",
        audience="requester",
        subject_ref="contract-1@1",
        state="open",
        blocking=True,
        summary="Deploy gate needs approval",
        reason="deploy_evidence_missing",
        opened_at=NOW,
    )
    base.update(overrides)
    return HumanEscalation(**base)


OPTIONS = (
    HumanEscalationOption(id="approve", label="Approve", consequence="Deploy proceeds"),
    HumanEscalationOption(id="reject", label="Reject", consequence="Deploy is blocked"),
)


# -- model invariants ---------------------------------------------------------


class TestHumanEscalationModel:
    def test_round_trip(self):
        esc = escalation(
            options=OPTIONS,
            next_action=ActionHint(kind="request_human_decision", target_id="esc_1"),
            dedupe_key="completion:contract-1:1:1:blocked",
            expires_at=DEADLINE,
            run_id="run-1",
        )
        assert HumanEscalation.from_dict(esc.to_dict()) == esc

    def test_resolved_requires_resolution_and_vice_versa(self):
        with pytest.raises(SchemaError):
            escalation(state="resolved")
        with pytest.raises(SchemaError):
            escalation(
                state="open",
                resolution=EscalationResolution(actor=HUMAN, resolved_at=LATER),
            )

    def test_acknowledged_requires_attribution_and_open_forbids_it(self):
        with pytest.raises(SchemaError):
            escalation(state="acknowledged")
        with pytest.raises(SchemaError):
            escalation(
                state="open",
                acknowledgement=EscalationAcknowledgement(actor=HUMAN, acknowledged_at=LATER),
            )
        # resolved may carry the earlier acknowledgement
        esc = escalation(
            state="resolved",
            acknowledgement=EscalationAcknowledgement(actor=HUMAN, acknowledged_at=LATER),
            resolution=EscalationResolution(actor=HUMAN, resolved_at=DEADLINE),
        )
        assert esc.state == "resolved"

    def test_expires_at_must_be_after_opened_at_parsed(self):
        with pytest.raises(SchemaError):
            escalation(expires_at=NOW)
        with pytest.raises(SchemaError):
            escalation(opened_at="2026-07-26T10:00:00+00:00", expires_at="2026-07-26T10:00:00Z")

    def test_expired_requires_expires_at(self):
        with pytest.raises(SchemaError):
            escalation(state="expired", terminal_reason="deadline")
        assert escalation(state="expired", expires_at=DEADLINE).state == "expired"

    def test_superseded_requires_successor_id(self):
        with pytest.raises(SchemaError):
            escalation(state="superseded")
        assert escalation(state="superseded", superseded_by_id="esc_2").superseded_by_id

    def test_option_ids_unique_and_resolution_option_must_be_offered(self):
        dup = (
            HumanEscalationOption(id="a", label="A", consequence="c"),
            HumanEscalationOption(id="a", label="B", consequence="c"),
        )
        with pytest.raises(SchemaError):
            escalation(options=dup)
        with pytest.raises(SchemaError):
            escalation(
                state="resolved",
                options=OPTIONS,
                resolution=EscalationResolution(
                    actor=HUMAN, resolved_at=LATER, option_id="not-offered"
                ),
            )

    def test_time_orderings(self):
        early = "2026-07-26T09:59:59+00:00"
        with pytest.raises(SchemaError):
            escalation(
                state="resolved",
                resolution=EscalationResolution(actor=HUMAN, resolved_at=early),
            )
        with pytest.raises(SchemaError):
            escalation(
                state="acknowledged",
                acknowledgement=EscalationAcknowledgement(actor=HUMAN, acknowledged_at=early),
            )
        with pytest.raises(SchemaError):  # resolvedAt < acknowledgedAt
            escalation(
                state="resolved",
                acknowledgement=EscalationAcknowledgement(actor=HUMAN, acknowledged_at=DEADLINE),
                resolution=EscalationResolution(actor=HUMAN, resolved_at=LATER),
            )

    def test_strict_types(self):
        with pytest.raises(SchemaError):
            escalation(blocking="yes")
        with pytest.raises(SchemaError):
            escalation(summary=42)
        with pytest.raises(SchemaError):
            escalation(acknowledgement={"actor": {"actorId": "a"}, "acknowledgedAt": LATER})
        with pytest.raises(SchemaError):
            escalation(escalation_class="urgent")
        with pytest.raises(SchemaError):
            escalation(audience="everyone")
        with pytest.raises(SchemaError):
            escalation(opened_at="2026-07-26 10:00:00+00:00")

    def test_unknown_and_missing_wire_keys(self):
        data = escalation().to_dict()
        data["surprise"] = 1
        with pytest.raises(SchemaError):
            HumanEscalation.from_dict(data)
        data = escalation().to_dict()
        del data["subjectRef"]
        with pytest.raises(SchemaError) as excinfo:
            HumanEscalation.from_dict(data)
        assert excinfo.value.code == "missing_field"

    def test_superseded_by_id_only_valid_for_superseded_state(self):
        with pytest.raises(SchemaError):
            escalation(superseded_by_id="esc_2")  # state=open

    def test_terminal_reason_only_valid_for_terminal_states(self):
        with pytest.raises(SchemaError):
            escalation(terminal_reason="why")  # state=open
        with pytest.raises(SchemaError):
            escalation(
                state="resolved",
                terminal_reason="why",
                resolution=EscalationResolution(actor=HUMAN, resolved_at=LATER),
            )

    def test_attribution_at_or_after_deadline_is_invalid_at_model_layer(self):
        """Closes the open() bypass: a pre-attributed escalation cannot carry
        human attribution at or past its own deadline."""
        with pytest.raises(SchemaError):
            escalation(
                state="acknowledged",
                expires_at=DEADLINE,
                acknowledgement=EscalationAcknowledgement(actor=HUMAN, acknowledged_at=DEADLINE),
            )
        with pytest.raises(SchemaError):
            escalation(
                state="resolved",
                expires_at=DEADLINE,
                resolution=EscalationResolution(actor=HUMAN, resolved_at=AFTER_DEADLINE),
            )
        ok = escalation(
            state="acknowledged",
            expires_at=DEADLINE,
            acknowledgement=EscalationAcknowledgement(actor=HUMAN, acknowledged_at=LATER),
        )
        assert ok.state == "acknowledged"

    def test_metadata_false_is_rejected_not_coerced(self):
        hint = ActionHint(kind="none").to_dict()
        hint["metadata"] = False
        with pytest.raises(SchemaError):
            ActionHint.from_dict(hint)

    def test_explicit_empty_options_list_is_rejected(self):
        data = escalation().to_dict()
        data["options"] = []
        with pytest.raises(SchemaError):
            HumanEscalation.from_dict(data)

    def test_actor_typing_at_escalation_boundary(self):
        with pytest.raises(SchemaError):  # a raw dict is not an ActorRef
            EscalationAcknowledgement(actor={"actorId": "a"}, acknowledged_at=LATER)
        with pytest.raises(SchemaError):  # non-string actor fields rejected
            EscalationResolution(actor=ActorRef(actor_id=True), resolved_at=LATER)
        bad = escalation(
            state="acknowledged",
            acknowledgement=EscalationAcknowledgement(actor=HUMAN, acknowledged_at=LATER),
        ).to_dict()
        bad["acknowledgement"]["actor"] = ["not", "an", "object"]
        with pytest.raises(SchemaError) as excinfo:
            HumanEscalation.from_dict(bad)
        assert excinfo.value.code == "invalid_type"

    def test_escalation_input_feed_active_semantics(self):
        assert escalation().to_escalation_input().active is True
        expired = escalation(state="expired", expires_at=DEADLINE, terminal_reason="deadline")
        assert expired.to_escalation_input().active is False  # expired never authorizes
        resolved = escalation(
            state="resolved",
            resolution=EscalationResolution(actor=HUMAN, resolved_at=LATER),
        )
        feed = resolved.to_escalation_input()
        assert feed.active is False
        assert feed.resolved_at == LATER
        non_blocking = escalation(blocking=False)
        assert non_blocking.to_escalation_input().active is False


# -- lifecycle store ----------------------------------------------------------


@pytest.fixture()
def env(tmp_path: Path):
    monitor = MonitorStore(tmp_path / "monitor.db")
    identities = IdentityStore(tmp_path / "identity.sqlite3")
    identities.bind_instance(ALICE, "macbook", display_name="A", bound_at=NOW)
    with identities._connect() as connection:  # seed a private owner binding
        connection.execute(
            "INSERT INTO monitor_bindings(platform,destination_id,chat_type,owner_platform,"
            "owner_actor_id,role,scope,revoked) VALUES('feishu','p2p_owner','private',"
            "'feishu','owner','owner','',0)"
        )
        connection.commit()
    with monitor.transaction() as connection:
        for thread in (THREAD, "macbook/task-2"):
            connection.execute(
                "INSERT INTO completion_threads(instance_id,work_thread_key,created_at,"
                "updated_at) VALUES('macbook',?,?,?)",
                (thread, NOW, NOW),
            )
    return monitor, EscalationStore(monitor, identities)


class TestOpenAndDedupe:
    def test_open_and_get(self, env):
        _, store = env
        created, was_created = store.open("macbook", escalation(dedupe_key="k1"))
        assert was_created is True
        assert store.get("macbook", "esc_1") == created

    def test_open_requires_existing_thread(self, env):
        _, store = env
        with pytest.raises(SchemaError) as excinfo:
            store.open("macbook", escalation(work_thread_key="macbook/ghost"))
        assert excinfo.value.code == "not_found"

    def test_open_requires_open_or_acknowledged(self, env):
        _, store = env
        with pytest.raises(SchemaError):
            store.open(
                "macbook", escalation(state="expired", expires_at=DEADLINE, terminal_reason="x")
            )

    def test_dedupe_returns_live_escalation(self, env):
        _, store = env
        first, _ = store.open("macbook", escalation(dedupe_key="k1"))
        second, created = store.open("macbook", escalation(id="esc_2", dedupe_key="k1"))
        assert created is False
        assert second == first  # the live holder wins; no duplicate opened

    def test_dedupe_is_run_scoped(self, env):
        _, store = env
        store.open("macbook", escalation(dedupe_key="k1", run_id="run-1"))
        other, created = store.open(
            "macbook", escalation(id="esc_2", dedupe_key="k1", run_id="run-2")
        )
        assert created is True
        assert other.id == "esc_2"

    def test_same_id_identical_identity_is_idempotent(self, env):
        _, store = env
        store.open("macbook", escalation())
        replay, created = store.open("macbook", escalation())
        assert created is False
        assert replay.id == "esc_1"

    def test_same_id_different_identity_conflicts(self, env):
        _, store = env
        store.open("macbook", escalation())
        with pytest.raises(SchemaError) as excinfo:
            store.open("macbook", escalation(work_thread_key="macbook/task-2"))
        assert excinfo.value.code == "conflict"

    def test_dedupe_slot_released_after_terminal(self, env):
        _, store = env
        store.open("macbook", escalation(dedupe_key="k1"))
        store.transition("macbook", "esc_1", "resolved", at=LATER, actor=HUMAN)
        successor, created = store.open("macbook", escalation(id="esc_2", dedupe_key="k1"))
        assert created is True
        assert successor.id == "esc_2"

    def test_acknowledged_keeps_dedupe_slot(self, env):
        _, store = env
        store.open("macbook", escalation(dedupe_key="k1"))
        store.transition("macbook", "esc_1", "acknowledged", at=LATER, actor=HUMAN)
        held, created = store.open("macbook", escalation(id="esc_2", dedupe_key="k1"))
        assert created is False
        assert held.id == "esc_1"


class TestTransitions:
    def test_acknowledge_then_resolve(self, env):
        monitor, store = env
        store.open("macbook", escalation(options=OPTIONS))
        acked, changed = store.transition(
            "macbook", "esc_1", "acknowledged", at=LATER, actor=HUMAN
        )
        assert changed is True and acked.state == "acknowledged"
        resolved, changed = store.transition(
            "macbook", "esc_1", "resolved", at=DEADLINE, actor=HUMAN, option_id="approve"
        )
        assert changed is True
        assert resolved.resolution.option_id == "approve"
        with monitor.connect() as connection:
            events = [
                row["event_type"]
                for row in connection.execute(
                    "SELECT event_type FROM completion_governance_events "
                    "WHERE instance_id='macbook' ORDER BY rowid"
                )
            ]
        assert events == [
            "human_escalation.opened",
            "human_escalation.acknowledged",
            "human_escalation.resolved",
        ]

    def test_resolve_requires_option_when_offered(self, env):
        _, store = env
        store.open("macbook", escalation(options=OPTIONS))
        with pytest.raises(SchemaError) as excinfo:
            store.transition("macbook", "esc_1", "resolved", at=LATER, actor=HUMAN)
        assert excinfo.value.code == "missing_field"

    def test_attributed_actor_required(self, env):
        _, store = env
        store.open("macbook", escalation())
        with pytest.raises(SchemaError):
            store.transition("macbook", "esc_1", "acknowledged", at=LATER)
        with pytest.raises(SchemaError):
            store.transition("macbook", "esc_1", "resolved", at=LATER)

    def test_idempotent_replay_matches_attribution(self, env):
        _, store = env
        store.open("macbook", escalation())
        store.transition("macbook", "esc_1", "resolved", at=LATER, actor=HUMAN)
        replay, changed = store.transition(
            "macbook", "esc_1", "resolved", at=DEADLINE, actor=HUMAN
        )
        assert changed is False and replay.resolution.resolved_at == LATER
        with pytest.raises(SchemaError) as excinfo:
            store.transition("macbook", "esc_1", "resolved", at=DEADLINE, actor=OTHER_HUMAN)
        assert excinfo.value.code == "conflict"

    def test_terminal_states_are_immutable(self, env):
        _, store = env
        store.open("macbook", escalation())
        store.transition("macbook", "esc_1", "resolved", at=LATER, actor=HUMAN)
        with pytest.raises(SchemaError) as excinfo:
            store.transition("macbook", "esc_1", "acknowledged", at=DEADLINE, actor=HUMAN)
        assert excinfo.value.code == "conflict"

    def test_late_resolution_is_coerced_to_expired(self, env):
        """The core Phase 4 rule: an expired escalation never authorizes."""
        monitor, store = env
        store.open("macbook", escalation(expires_at=DEADLINE))
        result, changed = store.transition(
            "macbook", "esc_1", "resolved", at=AFTER_DEADLINE, actor=HUMAN
        )
        assert changed is True
        assert result.state == "expired"
        assert result.resolution is None  # the late approval was NOT recorded
        assert result.terminal_reason == "Escalation expired without implicit approval."
        assert result.to_escalation_input().active is False
        with monitor.connect() as connection:
            payloads = [
                json.loads(row["payload_json"])
                for row in connection.execute(
                    "SELECT payload_json FROM completion_governance_events "
                    "WHERE instance_id='macbook' AND event_type='human_escalation.expired'"
                )
            ]
        assert payloads and payloads[0]["rejectedLateTransition"] == "resolved"

    def test_late_acknowledgement_is_coerced_to_expired(self, env):
        _, store = env
        store.open("macbook", escalation(expires_at=DEADLINE))
        result, _ = store.transition(
            "macbook", "esc_1", "acknowledged", at=DEADLINE, actor=HUMAN
        )
        assert result.state == "expired"  # at == expiresAt is already too late

    def test_explicit_expiry_requires_deadline_reached(self, env):
        _, store = env
        store.open("macbook", escalation(expires_at=DEADLINE))
        with pytest.raises(SchemaError):
            store.transition("macbook", "esc_1", "expired", at=LATER)
        result, changed = store.transition("macbook", "esc_1", "expired", at=DEADLINE)
        assert changed is True and result.state == "expired"

    def test_supersede_requires_existing_successor_in_thread(self, env):
        _, store = env
        store.open("macbook", escalation())
        with pytest.raises(SchemaError):
            store.transition(
                "macbook", "esc_1", "superseded", at=LATER, superseded_by_id="esc_missing"
            )
        with pytest.raises(SchemaError):
            store.transition("macbook", "esc_1", "superseded", at=LATER, superseded_by_id="esc_1")
        store.open(
            "macbook", escalation(id="esc_other", work_thread_key="macbook/task-2")
        )
        with pytest.raises(SchemaError):  # different thread
            store.transition(
                "macbook", "esc_1", "superseded", at=LATER, superseded_by_id="esc_other"
            )
        store.open("macbook", escalation(id="esc_2"))
        result, changed = store.transition(
            "macbook", "esc_1", "superseded", at=LATER, superseded_by_id="esc_2"
        )
        assert changed is True
        assert result.superseded_by_id == "esc_2"


class TestExpireSweep:
    def test_transition_and_sweep_validate_at_upfront(self, env):
        _, store = env
        store.open("macbook", escalation())
        with pytest.raises(SchemaError):
            store.transition(
                "macbook", "esc_1", "superseded", at="not-a-timestamp", superseded_by_id="x"
            )
        with pytest.raises(SchemaError):
            store.expire_sweep("macbook", at="not-a-timestamp")

    def test_sweep_survives_arbitrary_per_candidate_failures(self, env, monkeypatch):
        _, store = env
        store.open("macbook", escalation(expires_at=DEADLINE))
        store.open(
            "macbook", escalation(id="esc_2", expires_at=DEADLINE, dedupe_key="k2")
        )
        original = store.transition
        calls = {"n": 0}

        def flaky(instance_id, escalation_id, to_state, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("simulated non-schema failure")
            return original(instance_id, escalation_id, to_state, **kwargs)

        monkeypatch.setattr(store, "transition", flaky)
        scanned, expired = store.expire_sweep("macbook", at=AFTER_DEADLINE)
        assert scanned == 2
        assert expired == 1  # the second candidate still expired

    def test_sweep_expires_only_overdue(self, env):
        _, store = env
        store.open("macbook", escalation(expires_at=DEADLINE))
        store.open("macbook", escalation(id="esc_2", expires_at="2026-07-27T00:00:00+00:00"))
        store.open("macbook", escalation(id="esc_3"))  # no deadline
        scanned, expired = store.expire_sweep("macbook", at=AFTER_DEADLINE)
        assert (scanned, expired) == (3, 1)
        assert store.get("macbook", "esc_1").state == "expired"
        assert store.get("macbook", "esc_2").state == "open"
        assert store.get("macbook", "esc_3").state == "open"

    def test_sweep_is_idempotent_and_run_scoped(self, env):
        _, store = env
        store.open("macbook", escalation(expires_at=DEADLINE, run_id="run-1"))
        store.open(
            "macbook",
            escalation(id="esc_2", expires_at=DEADLINE, run_id="run-2", dedupe_key="k2"),
        )
        scanned, expired = store.expire_sweep("macbook", at=AFTER_DEADLINE, run_id="run-1")
        assert (scanned, expired) == (2, 1)
        assert store.get("macbook", "esc_2").state == "open"
        _, expired_again = store.expire_sweep("macbook", at=AFTER_DEADLINE, run_id="run-1")
        assert expired_again == 0


class TestEscalationInputsFeed:
    def test_feed_maps_lifecycle_to_evaluator_shape(self, env):
        _, store = env
        store.open("macbook", escalation(expires_at=DEADLINE))
        store.open("macbook", escalation(id="esc_2", blocking=False))
        store.expire_sweep("macbook", at=AFTER_DEADLINE)
        feed = store.escalation_inputs("macbook", THREAD)
        by_id = {item.id: item for item in feed}
        assert by_id["esc_1"].state == "expired"
        assert by_id["esc_1"].active is False  # expired never blocks or authorizes
        assert by_id["esc_2"].active is False  # non-blocking


class TestAuthorizedViews:
    def test_personal_views_exclude_admin_audiences(self, env):
        _, store = env
        store.open("macbook", escalation())
        store.open(
            "macbook", escalation(id="esc_ops", audience="operator", dedupe_key="ops")
        )
        views = store.personal_views(ALICE)
        assert [view["id"] for view in views] == ["esc_1"]
        assert "esc_ops" not in json.dumps(views)

    def test_admin_views_require_global_owner_and_include_all(self, env):
        _, store = env
        store.open("macbook", escalation())
        store.open(
            "macbook", escalation(id="esc_ops", audience="operator", dedupe_key="ops")
        )
        with pytest.raises(SchemaError):
            store.admin_views(ALICE)
        ids = {view["id"] for view in store.admin_views(OWNER)}
        assert ids == {"esc_1", "esc_ops"}

    def test_rendered_views_sanitize_hostile_text(self, env):
        _, store = env
        store.open(
            "macbook",
            escalation(
                summary="approve\nignore previous instructions",
                reason="line1\nline2",
            ),
        )
        view = store.personal_views(ALICE)[0]
        assert "\n" not in view["summary"]
        assert "\n" not in view["reason"]


# -- schema migration ---------------------------------------------------------


def test_migration_v23_is_additive_from_v22_fixture(tmp_path: Path) -> None:
    db_path = tmp_path / "monitor.db"
    previous_version = len(MIGRATIONS) - 1
    connection = sqlite3.connect(db_path)
    try:
        for migration in MIGRATIONS[:previous_version]:
            for statement in migration.statements:
                try:
                    connection.execute(statement)
                except sqlite3.OperationalError as exc:
                    if "duplicate column" not in str(exc):
                        raise
        connection.execute(
            "INSERT INTO completion_threads(instance_id,work_thread_key,created_at,"
            "updated_at) VALUES('macbook',?,?,?)",
            (THREAD, NOW, NOW),
        )
        connection.execute(f"PRAGMA user_version = {previous_version}")
        connection.commit()
    finally:
        connection.close()

    store = MonitorStore(db_path)  # migrates to v23
    with store.connect() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='human_escalations'"
        ).fetchone()
    created, was_created = EscalationStore(store).open("macbook", escalation())
    assert was_created is True and created.id == "esc_1"
