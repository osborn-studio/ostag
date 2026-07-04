from __future__ import annotations

import pytest

from ostag.core.capabilities import (
    CONNECTOR_CAPABILITIES,
    get_capability,
    list_capabilities,
)
from ostag.core.schemas import SchemaError


def test_all_supported_platforms_declare_chat_capabilities() -> None:
    for platform in ("slack", "feishu", "lark", "dingtalk"):
        capability = get_capability(platform, "chat")
        assert capability.status == "available"
        assert capability.can("can_reply_thread")
        assert capability.can("can_send_proactive")
        assert capability.default_mode == "propose"
        assert capability.destructive_requires_approval is True


def test_table_connectors_never_allow_delete() -> None:
    for platform, connector in (("feishu", "base"), ("lark", "base"), ("dingtalk", "ai_table")):
        capability = get_capability(platform, connector)
        assert capability.can("can_read_records")
        assert capability.can("can_create_records")
        assert not capability.can("can_delete_records")


def test_github_issues_available_without_destructive_ops() -> None:
    capability = get_capability("github", "issues")
    assert capability.status == "available"
    assert capability.can("can_create_issues")
    assert capability.can("can_comment_issues")
    assert capability.can("can_apply_action")
    assert not capability.can("can_close_issues")
    assert not capability.can("can_delete_issues")
    assert capability.destructive_requires_approval is True


def test_gitlab_issues_available_without_destructive_ops() -> None:
    capability = get_capability("gitlab", "issues")
    assert capability.status == "available"
    assert capability.can("can_create_issues")
    assert capability.can("can_comment_issues")
    assert capability.can("can_apply_action")
    assert not capability.can("can_close_issues")
    assert not capability.can("can_delete_issues")


def test_list_capabilities_filters_by_platform() -> None:
    assert list_capabilities() == CONNECTOR_CAPABILITIES
    dingtalk = list_capabilities("dingtalk")
    assert {item.connector for item in dingtalk} == {"chat", "ai_table"}


def test_unknown_connector_error_lists_known_connectors() -> None:
    with pytest.raises(SchemaError) as excinfo:
        get_capability("slack", "canvas")
    assert excinfo.value.code == "unknown_connector"
    assert "slack/chat" in excinfo.value.recovery_hint


def test_capability_serialization_shape() -> None:
    payload = get_capability("dingtalk", "ai_table").to_dict()

    assert payload["platform"] == "dingtalk"
    assert payload["policy"]["default_mode"] == "propose"
    assert payload["policy"]["destructive_requires_approval"] is True
    assert payload["limits"]["batch_size"] == 30
