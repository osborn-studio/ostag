"""Phase 5: read-only routing explanations (RoutingDecision)."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from ostag.copilot_relay.feishu_routes import CopilotFeishuController
from ostag.copilot_relay.models import RelayEnvelope
from ostag.copilot_relay.policy import RelayPolicy
from ostag.copilot_relay.route_explainer import RouteExplainer, RoutingDecisionStore
from ostag.copilot_relay.service import CopilotRelayService, RelayRoutingError
from ostag.copilot_relay.store import RelayStore
from ostag.core.event import NormalizedEvent
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
from ostag.work_monitor.store import MIGRATIONS, MonitorStore

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
            RoutingCandidate(
                runner_id="r/l", executor_id="e", eligible=True,
                reasons=REASON, locality="orbital",
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

    def test_optional_fields_are_not_nullable_on_wire(self):
        # Upstream .optional() fields reject explicit null / empty values.
        data = decision().to_dict()
        data["selected"] = None
        with pytest.raises(SchemaError) as excinfo:
            RoutingDecision.from_dict(data)
        assert excinfo.value.code == "invalid_type"
        candidate = decision().to_dict()["candidates"][0]
        for key, bad in (("capacity", None), ("capacity", False), ("locality", "")):
            payload = dict(candidate)
            payload[key] = bad
            with pytest.raises(SchemaError):
                RoutingCandidate.from_dict(payload)
        data = decision().to_dict()
        data["policySnapshotId"] = ""
        with pytest.raises(SchemaError):
            RoutingDecision.from_dict(data)

    def test_malformed_roots_raise_schema_error_not_type_error(self):
        for payload in (None, False, ["x"], "text"):
            with pytest.raises(SchemaError):
                RoutingDecision.from_dict(payload)
            with pytest.raises(SchemaError):
                RoutingCandidate.from_dict(payload)
            with pytest.raises(SchemaError):
                SelectedRoute.from_dict(payload)


# -- explainer fixtures -------------------------------------------------------


def session_envelope(
    session_id: str = "sess_1",
    *,
    version: int = 3,
    state: str = "awaiting_decision",
    kind: str = "session.upsert",
    message_id: str | None = None,
    payload: dict | None = None,
) -> RelayEnvelope:
    return RelayEnvelope.from_dict(
        {
            "schema_version": 1,
            "message_id": message_id or f"upsert_{session_id}_{state}_{version}",
            "binding": {
                "instance_id": "macmini", "task_id": "task_1", "run_id": "run_1",
                "session_id": session_id, "turn_id": "turn_3", "session_version": version,
            },
            "sequence": version,
            "created_at": "2026-07-14T01:00:00Z",
            "expires_at": "2026-07-14T02:00:00Z",
            "sender": OWNER.to_dict(),
            "kind": kind,
            "payload": payload
            or {
                "agent_id": "codex-cli", "title": "Relay", "state": state,
                "resume_supported": True, "latest_summary": "Waiting",
            },
            "correlation_id": None,
            "causation_id": None,
        }
    )


def decision_request_envelope(message_id: str = "om_decision_7") -> RelayEnvelope:
    return session_envelope(
        message_id=message_id,
        kind="decision.request",
        payload={
            "decision_id": "decision_7", "question": "选择？",
            "choices": [{"choice_id": "b", "label": "B"}],
            "recommended_choice_id": "b", "blocking": True, "safe_work": [],
        },
    )


class CountingPolicy(RelayPolicy):
    """A stateful access provider: every can_access call is observable."""

    def __init__(self, owner: Principal) -> None:
        super().__init__(owner)
        self.calls = 0

    def can_access(self, principal: Principal, instance_id: str) -> bool:
        self.calls += 1
        return super().can_access(principal, instance_id)


def build_service(
    tmp_path: Path, name: str, *, policy: RelayPolicy | None = None
) -> tuple[CopilotRelayService, MonitorStore]:
    monitor = MonitorStore(tmp_path / f"{name}.db")
    store = RelayStore(monitor)
    store.upsert_session(session_envelope())
    store.upsert_session(session_envelope("sess_2", version=1, state="running"))
    service = CopilotRelayService(
        store,
        policy or RelayPolicy(OWNER, instance_scopes={MEMBER: frozenset()}),
        clock=lambda: CLOCK,
    )
    service.select_session(OWNER, "macmini", "sess_1", selected_at="1")
    decision_env = decision_request_envelope()
    store.enqueue(decision_env, direction="inbound")
    store.record_decision(decision_env)
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

# Scripted steps: ("route", text, quote, event_id) | ("select", session_id).
# Covers queued, immediate duplicate, approval, confirmation, quoted decision
# answer, duplicate replay AFTER a session switch (the duplicate returns the
# ORIGINAL envelope bound to sess_1), terminal errors, and no-session errors.
CORPUS = (
    ("route", OWNER, "继续完成测试", None, "evt_1"),
    ("route", OWNER, "继续完成测试", None, "evt_1"),          # immediate duplicate
    ("route", OWNER, "请立即 deploy 到生产", None, "evt_2"),   # requires_approval
    ("route", OWNER, "新任务：整理发布说明", None, "evt_3"),   # requires_confirmation
    ("route", OWNER, "采用 B", "om_decision_7", "evt_4"),      # quoted decision answer
    ("select", OWNER, "sess_2"),
    ("route", OWNER, "继续完成测试", None, "evt_1"),          # duplicate after switch
    ("route", OWNER, "补充一个用例", None, "evt_5"),          # routed to sess_2
    ("route", STRANGER, "hello", None, "evt_6"),              # no current session
    ("route", OWNER, "   ", None, "evt_7"),                   # empty text
)


def run_corpus(service, explainer=None):
    outcomes = []
    for index, step in enumerate(CORPUS):
        if step[0] == "select":
            _, principal, session_id = step
            service.select_session(
                principal, "macmini", session_id, selected_at=f"sel_{index}"
            )
            outcomes.append(("select", session_id))
            continue
        _, principal, text, quote, event_id = step
        try:
            if explainer is None:
                result = service.handle_text(
                    principal, text, quote_message_id=quote, event_id=event_id
                )
            else:
                result, _ = explainer.handle_text(
                    principal, text, quote_message_id=quote, event_id=event_id,
                    decision_id=f"rd_{index}", decided_at=NOW,
                )
            outcomes.append(
                (
                    result.state,
                    result.outbound.to_dict() if result.outbound else None,
                    result.proposal_id,
                    (result.policy.outcome, result.policy.reason) if result.policy else None,
                )
            )
        except (RelayRoutingError, PermissionError) as exc:
            outcomes.append(("error", type(exc).__name__, str(exc)))
    return outcomes


RELAY_TABLES = (
    "relay_messages",
    "relay_sessions",
    "relay_current_tasks",
    "relay_decisions",
    "relay_task_proposals",
    "relay_platform_messages",
    "relay_callbacks",
    "relay_instance_cursors",
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
        """The required proof: the full corpus (incl. duplicate-after-switch
        and quoted decision answers) through the bare and explained routers →
        identical complete RelayResults and identical relay-store state."""
        bare_service, bare_monitor = build_service(tmp_path, "bare")
        explained_service, explained_monitor = build_service(tmp_path, "explained")
        explainer = RouteExplainer(
            explained_service, RoutingDecisionStore(explained_monitor)
        )

        bare_outcomes = run_corpus(bare_service)
        explained_outcomes = run_corpus(explained_service, explainer)

        assert bare_outcomes == explained_outcomes
        assert dump_tables(bare_monitor) == dump_tables(explained_monitor)

    def test_explainer_makes_no_extra_policy_calls(self, tmp_path):
        """A stateful can_access provider must observe the SAME number of
        lookups with and without the explanation layer."""
        bare_policy = CountingPolicy(OWNER)
        explained_policy = CountingPolicy(OWNER)
        bare_service, _ = build_service(tmp_path, "bare", policy=bare_policy)
        explained_service, explained_monitor = build_service(
            tmp_path, "explained", policy=explained_policy
        )
        explainer = RouteExplainer(
            explained_service, RoutingDecisionStore(explained_monitor)
        )
        run_corpus(bare_service)
        run_corpus(explained_service, explainer)
        assert bare_policy.calls == explained_policy.calls

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

    def test_recording_failure_never_changes_routing(self, env, monkeypatch):
        explainer, decisions, service = env
        monkeypatch.setattr(
            decisions, "record", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError())
        )
        result, explanation = explainer.handle_text(
            OWNER, "继续", event_id="evt_x", decision_id="rd_x", decided_at=NOW
        )
        assert result.state == "queued"  # routing unaffected
        assert explanation.reason_code == "routed"

    def test_recording_conflict_never_masks_router_outcome(self, env):
        explainer, decisions, _ = env
        conflicting = decision(id="rd_pre", run_id="other")
        decisions.record(OWNER, conflicting)
        # Same id will conflict on content — routing must still succeed.
        result, _ = explainer.handle_text(
            OWNER, "继续", event_id="evt_y", decision_id="rd_pre", decided_at=NOW
        )
        assert result.state == "queued"
        # And a router ERROR must re-raise the router's exception, not the
        # recording conflict.
        with pytest.raises(RelayRoutingError):
            explainer.handle_text(
                STRANGER, "hi", decision_id="rd_pre", decided_at=NOW
            )


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

    def test_duplicate_after_session_switch_explains_original_route(self, env):
        """Round-1 blocker regression: the duplicate returns the ORIGINAL
        envelope (sess_1) although the current session is now sess_2 —
        selected must equal that original route, not raise."""
        explainer, decisions, service = env
        explainer.handle_text(
            OWNER, "继续完成测试", event_id="evt_1", decision_id="rd_a", decided_at=NOW
        )
        service.select_session(OWNER, "macmini", "sess_2", selected_at="2")
        result, explanation = explainer.handle_text(
            OWNER, "继续完成测试", event_id="evt_1", decision_id="rd_b", decided_at=NOW
        )
        assert result.state == "duplicate"
        assert result.outbound.binding.session_id == "sess_1"
        assert explanation.reason_code == "duplicate_event"
        assert explanation.selected.executor_id == "sess_1"  # the actual route
        assert explanation.candidates[0].reasons[0].code == "duplicate_original_route"
        assert decisions.get("rd_b") == explanation

    def test_quoted_decision_answer_selects_quoted_session(self, env):
        explainer, _, _ = env
        result, explanation = explainer.handle_text(
            OWNER, "采用 B", quote_message_id="om_decision_7",
            decision_id="rd_q", decided_at=NOW,
        )
        assert result.outbound.kind == "decision.answer"
        assert explanation.selected.executor_id == "sess_1"
        assert explanation.candidates[0].reasons[0].code == "quoted_session"

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

    def test_snapshot_and_confirm_are_explained(self, env):
        explainer, decisions, service = env
        result, explanation = explainer.request_snapshot(
            OWNER, decision_id="rd_snap", decided_at=NOW
        )
        assert result.outbound.kind == "assistant.snapshot"
        assert explanation.reason_code == "snapshot_requested"
        assert explanation.selected.executor_id == "sess_1"
        proposal_result, _ = explainer.handle_text(
            OWNER, "新任务：写文档", event_id="evt_p", decision_id="rd_p", decided_at=NOW
        )
        confirm_result, confirm_explanation = explainer.confirm_task_proposal(
            OWNER, proposal_result.outbound,
            confirmed_at="2026-07-14T01:01:00Z",
            decision_id="rd_conf", decided_at=NOW,
        )
        assert confirm_result.state == "queued"
        assert confirm_explanation.reason_code == "proposal_confirmed"
        assert confirm_explanation.selected.executor_id == "sess_1"
        assert decisions.get("rd_conf") == confirm_explanation

    def test_decisions_are_immutable_and_principal_attributed(self, env):
        explainer, decisions, _ = env
        _, explanation = explainer.handle_text(
            OWNER, "继续", decision_id="rd_7", decided_at=NOW
        )
        assert decisions.record(OWNER, explanation) is False  # identical replay
        with pytest.raises(SchemaError) as excinfo:  # cross-principal collision
            decisions.record(MEMBER, explanation)
        assert excinfo.value.code == "conflict"
        tampered = RoutingDecision.from_dict(
            {**explanation.to_dict(), "reason": "tampered explanation"}
        )
        with pytest.raises(SchemaError) as excinfo:
            decisions.record(OWNER, tampered)
        assert excinfo.value.code == "immutable_record"


# -- production wiring --------------------------------------------------------


class TestControllerWiring:
    def _controller(self, tmp_path: Path, name: str, *, explain: bool):
        service, monitor = build_service(tmp_path, name)
        decisions = RoutingDecisionStore(monitor)
        explainer = RouteExplainer(service, decisions) if explain else None
        controller = CopilotFeishuController(
            service, clock=lambda: CLOCK, route_explainer=explainer
        )
        return controller, decisions, monitor

    @staticmethod
    def _event(text: str, event_id: str) -> NormalizedEvent:
        return NormalizedEvent(
            platform="feishu",
            event_id=event_id,
            chat_id="p2p_owner",
            message_id=f"om_{event_id}",
            sender_id=OWNER.actor_id,
            text=text,
            mention_bot=True,
            chat_type="private",
            destination_id="p2p_owner",
        )

    def test_controller_records_decisions_and_behavior_is_identical(self, tmp_path):
        bare, _, bare_monitor = self._controller(tmp_path, "bare", explain=False)
        wired, decisions, wired_monitor = self._controller(
            tmp_path, "wired", explain=True
        )
        for event_id, text in (("evt_1", "继续完成测试"), ("evt_2", "补充一个用例")):
            event = self._event(text, event_id)
            assert bare.handle_event(event) == wired.handle_event(event)
        assert dump_tables(bare_monitor) == dump_tables(wired_monitor)
        recorded = decisions.get("rd_evt_1")
        assert recorded.reason_code == "routed"
        assert recorded.selected.executor_id == "sess_1"


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

    def test_rendered_views_sanitize_every_string_including_codes(self, env):
        _, decisions, _ = env
        hostile = decision(
            id="rd_h",
            reason_code="routed‮\nhidden",
            candidates=(
                RoutingCandidate(
                    runner_id="macmini/legacy",
                    executor_id="sess_1",
                    eligible=True,
                    reasons=(
                        RoutingCandidateReason(
                            code="current‮_session", message="line1\nline2"
                        ),
                    ),
                ),
            ),
            selected=None,
        )
        decisions.record(OWNER, hostile)
        view = next(v for v in decisions.personal_views(OWNER) if v["id"] == "rd_h")
        rendered = str(view)
        assert "‮" not in rendered
        assert "\nhidden" not in rendered
        assert "\n" not in view["candidates"][0]["reasons"][0]["message"]


# -- migration ----------------------------------------------------------------


def test_migration_v24_is_additive_from_v23_fixture(tmp_path: Path) -> None:
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
        # Seed representative v23 data: a completion thread + an escalation.
        connection.execute(
            "INSERT INTO completion_threads(instance_id,work_thread_key,created_at,"
            "updated_at) VALUES('macbook','macbook/task-1',?,?)",
            (NOW, NOW),
        )
        connection.execute(
            "INSERT INTO human_escalations(instance_id,escalation_id,work_thread_key,"
            "class,state,dedupe_key,active_dedupe_key,escalation_json,created_at,"
            "updated_at) VALUES('macbook','esc_1','macbook/task-1','approval','open',"
            "'','thread:','{}',?,?)",
            (NOW, NOW),
        )
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
        # v23 data survives untouched.
        row = conn.execute(
            "SELECT state FROM human_escalations WHERE escalation_id='esc_1'"
        ).fetchone()
        assert row["state"] == "open"
        thread = conn.execute(
            "SELECT work_thread_key FROM completion_threads WHERE instance_id='macbook'"
        ).fetchone()
        assert thread["work_thread_key"] == "macbook/task-1"
    assert RoutingDecisionStore(store).record(OWNER, decision()) is True
