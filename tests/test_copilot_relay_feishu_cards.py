import json
from dataclasses import replace

from ostag.copilot_relay.feishu_cards import (
    MAX_CARD_BYTES,
    render_current_task_card,
    render_decision_card,
    render_osstack_status_card,
    render_task_proposal_card,
)
from ostag.copilot_relay.models import RelayEnvelope
from ostag.copilot_relay.store import RelaySession
from ostag.copilot_relay.status_board import (
    OsstackStatusBoardView,
    StatusSessionItem,
)


def _session() -> RelaySession:
    return RelaySession(
        "macmini", "sess-secret-native", "task-1", "run-secret", "codex-cli",
        "修复 <登录> **回归**", "awaiting_decision", 3, "turn-secret",
        "已完成分析，等待选择", "2026-07-14T01:00:00Z",
    )


def _envelope(kind: str) -> RelayEnvelope:
    payload = {
        "decision.request": {
            "decision_id": "decision-1",
            "question": "采用 <A> 还是 **B**？",
            "choices": [
                {"choice_id": "a", "label": "方案 A"},
                {"choice_id": "b", "label": "方案 B"},
            ],
            "recommended_choice_id": "a",
            "blocking": True,
            "safe_work": ["继续静态检查"],
        },
        "task.create.proposal": {
            "proposal_id": "proposal-1",
            "objective": "检查登录 <回归>",
            "repository": "ostag",
            "workspace": "/Users/private/worktree",
            "suggested_agent": "codex-cli",
            "acceptance_criteria": ["全部测试通过"],
            "sensitive_operations": ["生产部署"],
        },
    }[kind]
    return RelayEnvelope.from_dict(
        {
            "schema_version": 1,
            "message_id": f"message-{kind}",
            "binding": {
                "instance_id": "macmini", "task_id": "task-1", "run_id": "run-secret",
                "session_id": "sess-secret-native", "turn_id": "turn-secret",
                "session_version": 3,
            },
            "sequence": 1,
            "created_at": "2026-07-14T01:00:00Z",
            "expires_at": "2026-07-14T02:00:00Z",
            "sender": {"platform": "osstack", "actor_id": "reporter"},
            "kind": kind,
            "payload": payload,
            "correlation_id": None,
            "causation_id": None,
        }
    )


def _encoded(card: dict[str, object]) -> str:
    data = json.dumps(card, ensure_ascii=False, separators=(",", ":"))
    assert len(data.encode()) < MAX_CARD_BYTES
    return data


def test_current_task_card_is_mobile_first_bounded_and_hides_native_ids() -> None:
    card = render_current_task_card(
        _session(), can_act=True, callback_factory=lambda action, ref: f"opaque-{action}"
    )
    encoded = _encoded(card)
    assert card["config"]["wide_screen_mode"] is False
    assert "查看最新回复" in encoded and "切换任务" in encoded
    assert "sess-secret-native" not in encoded
    assert "run-secret" not in encoded and "turn-secret" not in encoded
    task_text = card["elements"][0]["content"]
    assert "&lt;登录&gt;" in task_text and "\\*\\*回归\\*\\*" in task_text
    values = [
        action["value"]
        for element in card["elements"] if element["tag"] == "action"
        for action in element["actions"]
    ]
    assert values == [
        {"callback_id": "opaque-snapshot"}, {"callback_id": "opaque-switch"}
    ]


def test_decision_and_proposal_cards_show_context_but_never_source_paths() -> None:
    decision = _encoded(
        render_decision_card(
            _session(), _envelope("decision.request"), can_act=True,
            callback_factory=lambda action, ref: f"opaque-{action}-{ref[-1]}",
        )
    )
    proposal = _encoded(
        render_task_proposal_card(
            _session(), _envelope("task.create.proposal"), can_act=True,
            callback_factory=lambda action, ref: f"opaque-{action}",
        )
    )
    assert "AI 推荐" in decision and "回复此消息也可作答" in decision
    assert "方案 A" in decision and "方案 B" in decision
    assert "确认创建" in proposal and "取消" in proposal
    assert "/Users/private/worktree" not in proposal
    assert "sess-secret-native" not in decision + proposal


def test_card_timestamps_are_rendered_in_beijing_time() -> None:
    current = _encoded(render_current_task_card(_session(), can_act=False))
    decision = _encoded(
        render_decision_card(
            _session(), _envelope("decision.request"), can_act=False
        )
    )

    assert "2026-07-14 09:00 北京时间" in current
    assert "2026-07-14 10:00 北京时间" in decision
    assert " UTC" not in current + decision


def test_non_owner_cards_have_no_action_elements() -> None:
    def callback(action: str, ref: str) -> str:
        return "must-not-be-called"
    cards = (
        render_current_task_card(_session(), can_act=False, callback_factory=callback),
        render_decision_card(
            _session(), _envelope("decision.request"), can_act=False,
            callback_factory=callback,
        ),
        render_task_proposal_card(
            _session(), _envelope("task.create.proposal"), can_act=False,
            callback_factory=callback,
        ),
    )
    assert all(not any(item["tag"] == "action" for item in card["elements"]) for card in cards)


def _status_view() -> OsstackStatusBoardView:
    session = StatusSessionItem(
        instance_id="ostag-local",
        session_id="session-secret-native",
        device_name="Mac mini",
        agent_id="codex-cli",
        title="OStag 状态看板",
        state="idle",
        latest_summary="上一轮发布验证已完成，当前等待新任务。",
        updated_at="2026-07-14T23:17:00Z",
        online=True,
        waiting_decision=False,
    )
    return OsstackStatusBoardView(
        scope="personal",
        generated_at="2026-07-14T23:20:00Z",
        device_count=1,
        online_count=1,
        running_count=0,
        waiting_decision_count=0,
        blocked_count=0,
        completed_today_count=2,
        current_session=session,
        sessions=(session,),
        attention=(),
        stale=False,
    )


def test_osstack_status_card_is_mobile_first_and_shows_device_agent() -> None:
    card = render_osstack_status_card(
        _status_view(),
        can_view_team=True,
        callback_factory=lambda action, ref: f"opaque-{action}",
    )
    encoded = _encoded(card)

    assert card["header"]["title"]["content"] == "OStag · osstack 实时进度看板"
    assert card["config"]["wide_screen_mode"] is False
    assert "我的设备" in encoded
    visible = encoded.replace("\\", "")
    assert "Mac mini" in visible and "ostag-local" in visible
    assert "codex-cli" in visible
    assert "运行中" in encoded and "等你决策" in encoded and "今日完成" in encoded
    assert "北京时间" in encoded and "只读状态查询" in encoded
    assert "团队总览" in encoded
    assert "session-secret-native" not in encoded


def test_member_status_card_has_no_team_scope_or_other_instance() -> None:
    view = _status_view()
    card = render_osstack_status_card(
        view,
        can_view_team=False,
        callback_factory=lambda action, ref: f"opaque-{action}",
    )
    encoded = _encoded(card)

    assert "团队总览" not in encoded
    assert "employee-other" not in encoded
    assert "刷新看板" in encoded and "切换任务" in encoded


def test_status_card_handles_unbound_offline_and_untrusted_text() -> None:
    base = _status_view()
    empty = replace(
        base,
        device_count=0,
        online_count=0,
        current_session=None,
        sessions=(),
    )
    empty_encoded = _encoded(render_osstack_status_card(empty, can_view_team=False))
    assert "尚未绑定 osstack" in empty_encoded

    unsafe = replace(
        base.current_session,
        title="<script>*boom*</script>",
        latest_summary="[bad](javascript:alert(1))",
        online=False,
    )
    offline = replace(
        base,
        online_count=0,
        current_session=unsafe,
        sessions=(unsafe,),
        stale=True,
    )
    offline_card = render_osstack_status_card(offline, can_view_team=False)
    offline_encoded = _encoded(offline_card)

    assert "数据可能已过期" in offline_encoded
    assert "<script>" not in offline_encoded and "&lt;script&gt;" in offline_encoded
    assert "javascript:alert" in offline_encoded
    assert len(offline_card["elements"]) <= 20
    assert len(offline_encoded.encode()) < MAX_CARD_BYTES
