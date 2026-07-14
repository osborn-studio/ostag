from datetime import UTC, datetime

from ostag.copilot_relay.feishu_routes import CopilotFeishuController
from ostag.copilot_relay.models import RelayEnvelope
from ostag.copilot_relay.policy import RelayPolicy
from ostag.copilot_relay.service import CopilotRelayService
from ostag.copilot_relay.store import RelayStore
from ostag.core.event import NormalizedEvent
from ostag.core.identity import Principal
from ostag.work_monitor.identity import Destination
from ostag.work_monitor.store import MonitorStore


OWNER = Principal("feishu", "owner")
OTHER = Principal("feishu", "other")
PRIVATE = Destination("feishu", "owner-chat", "private")
NOW = datetime(2026, 7, 14, 1, 10, tzinfo=UTC)


def _envelope(
    message_id: str,
    *,
    session_id: str = "session-1",
    kind: str = "session.upsert",
    version: int = 1,
) -> RelayEnvelope:
    payloads = {
        "session.upsert": {
            "agent_id": "codex-cli", "title": f"任务 {session_id}", "state": "running",
            "resume_supported": True, "latest_summary": "实现中",
        },
        "decision.request": {
            "decision_id": "decision-1", "question": "选择方案？",
            "choices": [{"choice_id": "a", "label": "方案 A"}],
            "recommended_choice_id": "a", "blocking": True, "safe_work": [],
        },
    }
    return RelayEnvelope.from_dict(
        {
            "schema_version": 1, "message_id": message_id,
            "binding": {
                "instance_id": "macmini", "task_id": f"task-{session_id}",
                "run_id": f"run-{session_id}", "session_id": session_id,
                "turn_id": f"turn-{version}", "session_version": version,
            },
            "sequence": version, "created_at": "2026-07-14T01:00:00Z",
            "expires_at": "2026-07-14T02:00:00Z", "sender": OWNER.to_dict(),
            "kind": kind, "payload": payloads[kind], "correlation_id": None,
            "causation_id": None,
        }
    )


def _controller(tmp_path):
    store = RelayStore(MonitorStore(tmp_path / "monitor.db"))
    for session_id in ("session-1", "session-2"):
        store.upsert_session(_envelope(f"upsert-{session_id}", session_id=session_id))
    service = CopilotRelayService(store, RelayPolicy(OWNER))
    service.select_session(OWNER, "macmini", "session-1", selected_at="2026-07-14T01:00:00Z")
    return CopilotFeishuController(service, clock=lambda: NOW), store


def _event(
    text: str,
    *,
    event_id: str = "event-1",
    sender: str = "owner",
    destination: str = "owner-chat",
    chat_type: str = "private",
    quote_message_id: str | None = None,
) -> NormalizedEvent:
    return NormalizedEvent(
        platform="feishu", event_id=event_id, chat_id=destination,
        message_id=f"message-{event_id}", sender_id=sender, text=text, mention_bot=True,
        chat_type=chat_type, destination_id=destination,
        quote_message_id=quote_message_id,
    )


def test_private_text_and_commands_route_to_exact_current_session(tmp_path) -> None:
    controller, store = _controller(tmp_path)
    ordinary = controller.handle_event(_event("请先补充回归测试"))
    assert ordinary is not None and ordinary.text == "指令已排队，等待本地会话接收。"
    assert ordinary.relay_message_id == "relay_event-1"
    assert store.envelope_for_message("relay_event-1").binding.session_id == "session-1"

    latest = controller.handle_event(_event("/最新回复", event_id="latest"))
    assert latest.text.startswith("已请求")
    assert store.envelope_for_message("relay_latest").kind == "assistant.snapshot"
    current = controller.handle_event(_event("/任务", event_id="task"))
    assert current.card["header"]["title"]["content"].endswith("当前任务")


def test_quote_binding_survives_restart_and_stale_decision_is_rejected(tmp_path) -> None:
    controller, store = _controller(tmp_path)
    decision = _envelope("relay-decision", kind="decision.request")
    store.enqueue(decision, direction="inbound")
    store.record_decision(decision)
    controller.record_delivery(
        platform_message_id="feishu-decision", relay_message_id=decision.message_id,
        principal=OWNER, destination=PRIVATE,
    )

    restarted = CopilotFeishuController(controller.service, clock=lambda: NOW)
    answer = restarted.handle_event(
        _event("采用方案 A", event_id="answer", quote_message_id="feishu-decision")
    )
    assert answer.text.startswith("回答已排队")
    assert store.envelope_for_message("relay_answer").kind == "decision.answer"

    store.upsert_session(_envelope("upsert-v2", version=2))
    stale = restarted.handle_event(
        _event("再次回答", event_id="stale", quote_message_id="feishu-decision")
    )
    assert stale.text == "这条决策已过期，请查看当前任务的最新问题。"


def test_callbacks_are_one_use_ttl_actor_and_destination_bound(tmp_path) -> None:
    controller, store = _controller(tmp_path)
    callback_id = controller.issue_callback(
        OWNER, PRIVATE, action="switch", reference="macmini\0session-2", ttl_seconds=60
    )
    assert set(controller.callback_value(callback_id)) == {"callback_id"}
    assert controller.handle_callback(
        callback_id, principal=OTHER, destination=PRIVATE
    ).text == "没有权限或操作目标不匹配。"
    assert controller.handle_callback(
        callback_id, principal=OWNER, destination=Destination("feishu", "other-chat", "private")
    ).text == "没有权限或操作目标不匹配。"
    switched = controller.handle_callback(callback_id, principal=OWNER, destination=PRIVATE)
    assert "session-2" not in switched.text
    assert store.current_session("feishu", "owner").session_id == "session-2"
    assert controller.handle_callback(
        callback_id, principal=OWNER, destination=PRIVATE
    ).text == "操作已过期或已使用。"

    expired = controller.issue_callback(
        OWNER, PRIVATE, action="snapshot", reference="current", ttl_seconds=-1
    )
    assert controller.handle_callback(expired, principal=OWNER, destination=PRIVATE).text == (
        "操作已过期或已使用。"
    )


def test_switch_picker_is_unambiguous_and_group_writes_are_refused(tmp_path) -> None:
    controller, _ = _controller(tmp_path)
    picker = controller.handle_event(_event("/切换任务", event_id="switch"))
    encoded = str(picker.card)
    assert "任务 session-1" in encoded and "任务 session-2" in encoded
    assert "session-1" not in str(
        [action["value"] for element in picker.card["elements"] if element["tag"] == "action"
         for action in element["actions"]]
    )
    refused = controller.handle_event(
        _event("改变实现方向", event_id="group", destination="group-chat", chat_type="group")
    )
    assert refused.text == "Copilot 写操作仅支持已绑定的负责人私聊。"
