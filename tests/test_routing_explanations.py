"""Phase 5: read-only routing explanations (RoutingDecision)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from ostag.copilot_relay.models import RelayEnvelope
from ostag.copilot_relay.policy import RelayPolicy
from ostag.copilot_relay.route_explainer import RouteExplainer, RoutingDecisionStore
from ostag.copilot_relay.service import CopilotRelayService, RelayRoutingError
from ostag.copilot_relay.store import RelayStore
from ostag.core.identity import Principal
from ostag.core.routing_explanation import (
    RoutingCandidate,
    RoutingCandidateReason,
    RoutingCapacity,
    RoutingDecision,
    SelectedRoute,
)
from ostag.core.schemas import SchemaError
from ostag.work_monitor.identity import IdentityStore
from ostag.work_monitor.store import MonitorStore

NOW = "2026-07-26T10:00:00+00:00"
CLOCK = datetime(2026, 7, 14, 1, 0, tzinfo=timezone.utc)

OWNER = Principal("feishu", "owner")
MEMBER = Principal("feishu", "member")
STRANGER = Principal("feishu", "stranger")

REASON = (RoutingCandidateReason(code="current_session", message="Current session."),)


def decision(**overrides) -> RoutingDecision:
    base = dict(
        id="rd_1",
        run_id="run_1",
        reason_code="routed",
        reason="The message was routed to the selected session.",
        decided_at=NOW,
        candidates=(
            RoutingCandidate(
                runner_id="macmini/legacy",
                executor_id="sess_1",
                eligible=True,
                reasons=REASON,
            ),
        ),
        selected=SelectedRoute(runner_id="macmini/legacy", executor_id="sess_1"),
    )
    base.update(overrides)
    return RoutingDecision(**base)


# -- model invariants ---------------------------------------------------------


class TestRoutingDecisionModel:
    def test_round_trip(self):
        full = decision(
            candidates=(
                RoutingCandidate(
                    runner_id="macmini/legacy",
                    executor_id="sess_1",
                    eligible=True,
                    reasons=REASON,
                    locality="local",
                    readiness="ready",
                    capacity=RoutingCapacity(active=1, limit=4),
                ),
            ),
            policy_snapshot_id="ostag:macmini:unversioned",
            access_profile_snapshot_id="aps_task_1",
        )
        assert RoutingDecision.from_dict(full.to_dict()) == full

    def test_selected_must_be_an_eligible_candidate(self):
        with pytest.raises(SchemaError):  # selected not among candidates
            decision(selected=SelectedRoute(runner_id="other/legacy", executor_id="sess_9"))
        with pytest.raises(SchemaError):  # selected candidate ineligible
            decision(
                candidates=(
                    RoutingCandidate(
                        runner_id="macmini/legacy",
                        executor_id="sess_1",
                        eligible=False,
                        reasons=(
                            RoutingCandidateReason(code="session_terminal", message="Done."),
                        ),
                    ),
                ),
            )
        assert decision(selected=None).selected is None  # no selection is fine

    def test_candidate_requires_nonempty_reasons(self):
        with pytest.raises(SchemaError):
            RoutingCandidate(
                runner_id="macmini/legacy", executor_id="sess_1", eligible=True, reasons=()
            )

    def test_enums_and_capacity(self):
        with pytest.raises(SchemaError):
            decision(
                selected=None,
                candidates=(
                    RoutingCandidate(
                        runner_id="r/l", executor_id="e", eligible=True,
                        reasons=REASON, locality="orbital",
                    ),
                ),
            )
        with pytest.raises(SchemaError):
            RoutingCapacity(active=-1, limit=4)
        with pytest.raises(SchemaError):
            RoutingCapacity(active=0, limit=0)

    def test_strict_wire(self):
        data = decision().to_dict()
        data["surprise"] = 1
        with pytest.raises(SchemaError):
            RoutingDecision.from_dict(data)
        data = decision().to_dict()
        del data["runId"]
        with pytest.raises(SchemaError) as excinfo:
            RoutingDecision.from_dict(data)
        assert excinfo.value.code == "missing_field"
        with pytest.raises(SchemaError):
            decision(decided_at="2026-07-26 10:00:00+00:00")
        with pytest.raises(SchemaError):
            decision(id=42)


# -- explainer fixtures -------------------------------------------------------


def session_envelope(
    session_id: str = "sess_1", *, version: int = 3, state: str = "awaiting_decision"
) -> RelayEnvelope:
    return RelayEnvelope.from_dict(
        {
            "schema_version": 1,
            "message_id": f"upsert_{session_id}_{state}_{version}",
            "binding": {
                "instance_id": "macmini", "task_id": "task_1", "run_id": "run_1",
                "session_id": session_id, "turn_id": "turn_3", "session_version": version,
            },
            "sequence": version,
            "created_at": "2026-07-14T01:00:00Z",
            "expires_at": "2026-07-14T02:00:00Z",
            "sender": OWNER.to_dict(),
            "kind": "session.upsert",
            "payload": {
                "agent_id": "codex-cli", "title": "Relay", "state": state,
                "resume_supported": True, "latest_summary": "Waiting",
            },
            "correlation_id": None,
            "causation_id": None,
        }
    )


def build_service(tmp_path: Path, name: str) -> tuple[CopilotRelayService, MonitorStore]:
    monitor = MonitorStore(tmp_path / f"{name}.db")
    store = RelayStore(monitor)
    store.upsert_session(session_envelope())
    policy = RelayPolicy(OWNER, instance_scopes={MEMBER: frozenset()})
    service = CopilotRelayService(store, policy, clock=lambda: CLOCK)
    service.select_session(OWNER, "macmini", "sess_1", selected_at="1")
    return service, monitor


@pytest.fixture()
def env(tmp_path: Path):
    service, monitor = build_service(tmp_path, "explained")
    identities = IdentityStore(tmp_path / "identity.sqlite3")
    identities.bind_instance(OWNER, "macmini", display_name="O", bound_at=NOW)
    with identities._connect() as connection:
        connection.execute(
            "INSERT INTO monitor_bindings(platform,destination_id,chat_type,owner_platform,"
            "owner_actor_id,role,scope,revoked) VALUES('feishu','p2p_owner','private',"
            "'feishu','owner','owner','',0)"
        )
        connection.commit()
    decisions = RoutingDecisionStore(monitor, identities)
    return RouteExplainer(service, decisions), decisions, service


# -- corpus: byte-identical routing with and without explanation --------------


CORPUS = (
    ("plain", "继续完成测试", None, "evt_1"),
    ("duplicate", "继续完成测试", None, "evt_1"),
    ("approval", "请立即 deploy 到生产", None, "evt_2"),
    ("confirmation", "新任务：整理发布说明", None, "evt_3"),
    ("plain2", "补充一个用例", None, "evt_4"),
)


def run_corpus(service, explainer=None):
    outcomes = []
    for index, (label, text, quote, event_id) in enumerate(CORPUS):
        try:
            if explainer is None:
                result = service.handle_text(
                    OWNER, text, quote_message_id=quote, event_id=event_id
                )
            else:
                result, _ = explainer.handle_text(
                    OWNER, text, quote_message_id=quote, event_id=event_id,
                    decision_id=f"rd_{index}", decided_at=NOW,
                )
            outbound = result.outbound.to_dict() if result.outbound else None
            outcomes.append((label, result.state, outbound))
        except (RelayRoutingError, PermissionError) as exc:
            outcomes.append((label, "error", str(exc)))
    return outcomes


RELAY_TABLES = (
    "relay_messages",
    "relay_sessions",
    "relay_current_tasks",
    "relay_decisions",
    "relay_task_proposals",
    "relay_platform_messages",
)


def dump_tables(monitor: MonitorStore, tables=RELAY_TABLES) -> dict:
    out = {}
    with monitor.connect() as connection:
        for table in tables:
            out[table] = [
                tuple(row)
                for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid")
            ]
    return out


class TestByteIdenticalRouting:
    def test_corpus_outputs_identical_with_and_without_explainer(self, tmp_path):
        """The required proof: same corpus through the bare router and the
        explained router → identical RelayResults and identical relay-store
        state. The explanation layer cannot influence routing."""
        bare_service, bare_monitor = build_service(tmp_path, "bare")
        explained_service, explained_monitor = build_service(tmp_path, "explained")
        explainer = RouteExplainer(
            explained_service, RoutingDecisionStore(explained_monitor)
        )

        bare_outcomes = run_corpus(bare_service)
        explained_outcomes = run_corpus(explained_service, explainer)

        assert bare_outcomes == explained_outcomes
        assert dump_tables(bare_monitor) == dump_tables(explained_monitor)

    def test_explanations_are_deterministic(self, tmp_path):
        first_service, first_monitor = build_service(tmp_path, "first")
        second_service, second_monitor = build_service(tmp_path, "second")
        first = RouteExplainer(first_service, RoutingDecisionStore(first_monitor))
        second = RouteExplainer(second_service, RoutingDecisionStore(second_monitor))
        run_corpus(first_service, first)
        run_corpus(second_service, second)
        first_rows = dump_tables(first_monitor, ("routing_decisions",))
        second_rows = dump_tables(second_monitor, ("routing_decisions",))
        assert first_rows == second_rows
        assert first_rows["routing_decisions"]  # decisions were actually recorded


# -- explanation correctness --------------------------------------------------


class TestExplanations:
    def test_routed_selected_equals_actual_route(self, env):
        explainer, decisions, _ = env
        result, explanation = explainer.handle_text(
            OWNER, "继续", decision_id="rd_1", decided_at=NOW
        )
        binding = result.outbound.binding
        assert explanation.selected == SelectedRoute(
            runner_id=f"{binding.instance_id}/{binding.runtime_id}",
            executor_id=binding.session_id,
        )
        assert explanation.reason_code == "routed"
        assert explanation.run_id == "run_1"
        assert explanation.candidates[0].eligible is True
        assert decisions.get("rd_1") == explanation

    def test_requires_approval_has_no_selected_route(self, env):
        explainer, _, _ = env
        result, explanation = explainer.handle_text(
            OWNER, "请立即 deploy", decision_id="rd_2", decided_at=NOW
        )
        assert result.state == "requires_approval"
        assert result.outbound is None
        assert explanation.selected is None
        assert explanation.reason_code == "policy_requires_approval"
        assert explanation.candidates  # the candidate session is still explained

    def test_requires_confirmation_selects_target_session(self, env):
        explainer, _, _ = env
        result, explanation = explainer.handle_text(
            OWNER, "新任务：整理文档", event_id="evt_c", decision_id="rd_3", decided_at=NOW
        )
        assert result.state == "requires_confirmation"
        assert explanation.reason_code == "policy_requires_confirmation"
        assert explanation.selected.executor_id == "sess_1"

    def test_no_current_session_error_is_explained_and_reraised(self, env):
        explainer, decisions, _ = env
        with pytest.raises(RelayRoutingError):
            explainer.handle_text(
                STRANGER, "hello", decision_id="rd_4", decided_at=NOW
            )
        explanation = decisions.get("rd_4")
        assert explanation.reason_code == "no_current_session"
        assert explanation.selected is None
        assert explanation.candidates == ()
        assert explanation.run_id == "rd_4"  # attempt id fallback

    def test_terminal_session_is_ineligible_candidate(self, env):
        explainer, decisions, service = env
        service.store.upsert_session(session_envelope(state="completed", version=4))
        with pytest.raises(RelayRoutingError):
            explainer.handle_text(OWNER, "继续", decision_id="rd_5", decided_at=NOW)
        explanation = decisions.get("rd_5")
        assert explanation.reason_code == "terminal_session"
        candidate = explanation.candidates[0]
        assert candidate.eligible is False
        assert any(reason.code == "session_terminal" for reason in candidate.reasons)
        assert explanation.selected is None

    def test_access_denied_is_explained(self, env):
        explainer, decisions, service = env
        service.store.select_current_task(
            MEMBER.platform, MEMBER.actor_id, "macmini", "sess_1", selected_at="2"
        )
        with pytest.raises(PermissionError):
            explainer.handle_text(MEMBER, "hello", decision_id="rd_6", decided_at=NOW)
        explanation = decisions.get("rd_6")
        assert explanation.reason_code == "access_denied"
        candidate = explanation.candidates[0]
        assert candidate.eligible is False
        assert any(reason.code == "access_denied" for reason in candidate.reasons)

    def test_decisions_are_immutable(self, env):
        explainer, decisions, _ = env
        _, explanation = explainer.handle_text(
            OWNER, "继续", decision_id="rd_7", decided_at=NOW
        )
        assert decisions.record(OWNER, explanation) is False  # idempotent replay
        tampered = RoutingDecision.from_dict(
            {**explanation.to_dict(), "reason": "tampered explanation"}
        )
        with pytest.raises(SchemaError) as excinfo:
            decisions.record(OWNER, tampered)
        assert excinfo.value.code == "immutable_record"


# -- authorized views ---------------------------------------------------------


class TestAuthorizedViews:
    def test_personal_views_are_principal_scoped(self, env):
        explainer, decisions, _ = env
        explainer.handle_text(OWNER, "继续", decision_id="rd_8", decided_at=NOW)
        assert [view["id"] for view in decisions.personal_views(OWNER)] == ["rd_8"]
        assert decisions.personal_views(MEMBER) == ()

    def test_admin_views_require_global_owner(self, env):
        explainer, decisions, _ = env
        explainer.handle_text(OWNER, "继续", decision_id="rd_9", decided_at=NOW)
        with pytest.raises(SchemaError):
            decisions.admin_views(MEMBER)
        assert [view["id"] for view in decisions.admin_views(OWNER)] == ["rd_9"]

    def test_views_without_identity_store_are_denied(self, tmp_path):
        store = RoutingDecisionStore(MonitorStore(tmp_path / "m.db"))
        with pytest.raises(SchemaError):
            store.personal_views(OWNER)


# -- migration ----------------------------------------------------------------


def test_migration_v24_is_additive_from_v23_fixture(tmp_path: Path) -> None:
    import sqlite3

    from ostag.work_monitor.store import MIGRATIONS

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
        connection.execute(f"PRAGMA user_version = {previous_version}")
        connection.commit()
    finally:
        connection.close()

    store = MonitorStore(db_path)
    with store.connect() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='routing_decisions'"
        ).fetchone()
    assert RoutingDecisionStore(store).record(OWNER, decision()) is True
