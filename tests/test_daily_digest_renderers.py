import json
from datetime import date

from ostag.core.ambient import RequirementSnapshot
from ostag.core.daily_digest import DailyDigestView
from ostag.core.daily_digest_renderers import (
    MAX_FEISHU_CARD_BYTES,
    render_daily_digest_text,
    render_daily_dingtalk_payload,
    render_daily_feishu_card,
)


def _view(kind: str = "morning") -> DailyDigestView:
    tasks = [
        RequirementSnapshot(
            "unsafe",
            "<script>*读取和展示*</script>" + "很长" * 100,
            "葛德彬",
            ("owner",),
            "方案设计中",
            "高 - P0",
            date(2026, 7, 10),
            5,
        ),
        RequirementSnapshot(
            "today", "交易盯盘提醒优化", "左士文", ("left",), "开发交付中",
            "中 - P1", date(2026, 7, 13), 0,
        ),
        RequirementSnapshot(
            "done", "组合分析导出优化", "葛德彬", ("owner",), "已完成",
            "中 - P1", date(2026, 7, 13), 0,
        ),
    ]
    return DailyDigestView.build(
        kind,  # type: ignore[arg-type]
        tasks,
        today=date(2026, 7, 13),
        table_url="https://example.test/tasks",
    )


def test_feishu_morning_board_has_approved_hierarchy_and_safe_bounds() -> None:
    card = render_daily_feishu_card(_view())
    encoded = json.dumps(card, ensure_ascii=False)

    assert card["header"]["template"] == "blue"
    assert card["header"]["title"]["content"] == "OS Tag · 今日作战看板"
    assert any(element["tag"] == "column_set" for element in card["elements"])
    assert "需要优先处理" in encoded
    assert "今日推进" in encoded
    assert "打开任务表" in encoded
    assert "callback_id" not in encoded
    assert "<script>" not in encoded and "&lt;script&gt;" in encoded
    assert len(card["elements"]) <= 20
    assert len(encoded.encode()) <= MAX_FEISHU_CARD_BYTES


def test_evening_renderers_include_results_and_tomorrow_attention() -> None:
    view = _view("evening")
    feishu = json.dumps(render_daily_feishu_card(view), ensure_ascii=False)
    dingtalk = json.dumps(
        render_daily_dingtalk_payload(view, manager_ids=("manager",)), ensure_ascii=False
    )

    for text in (feishu, dingtalk):
        assert "今日复盘看板" in text
        assert "今日完成" in text
        assert "仍未完成" in text
        assert "明日关注" in text
        assert "callback_id" not in text


def test_dingtalk_payload_mentions_unique_people_and_has_text_fallback() -> None:
    view = _view()
    payload = render_daily_dingtalk_payload(view, manager_ids=("manager", "owner"))
    fallback = render_daily_digest_text(view)

    assert payload["msgtype"] == "markdown"
    assert payload["at"]["atUserIds"] == ["manager", "owner", "left"]
    assert "今日作战看板" in payload["markdown"]["title"]
    assert "[打开任务表](https://example.test/tasks)" in payload["markdown"]["text"]
    assert "<script>" not in payload["markdown"]["text"]
    assert "今日重点" in fallback and "需要优先处理" in fallback


def test_renderers_reject_unsafe_table_urls() -> None:
    view = DailyDigestView.build(
        "morning", [], today=date(2026, 7, 13), table_url="javascript:alert(1)"
    )
    feishu = json.dumps(render_daily_feishu_card(view), ensure_ascii=False)
    dingtalk = json.dumps(render_daily_dingtalk_payload(view), ensure_ascii=False)

    assert "javascript:" not in feishu
    assert "javascript:" not in dingtalk
    assert "当前没有需要推进的重点任务" in feishu
