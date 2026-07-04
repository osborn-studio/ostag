"""Connector capability declarations.

Every platform adapter/connector declares what it can and cannot do, so
renderers can degrade gracefully and policy can refuse operations a platform
does not support. Declarations reflect the actual implementation state of
this repo: chat adapters currently render plain text and can send proactive
messages; requirement-table connectors can read/create/update records but are
never allowed to delete. GitHub/GitLab are declared as planned stubs.
"""

from __future__ import annotations

from ostag.core.schemas import ConnectorCapability, SchemaError

_CHAT_TEXT_CAPABILITIES: dict[str, bool | str] = {
    "can_read_thread": True,
    "can_reply_thread": True,
    "can_send_proactive": True,
    "can_render_buttons": False,
    "can_render_cards": False,
    "can_upload_artifact": False,
    "can_mention_users": "limited",
    "can_schedule_message": False,
    "can_apply_action": False,
}

_TABLE_CAPABILITIES: dict[str, bool | str] = {
    "can_read_records": True,
    "can_write_records": True,
    "can_create_records": True,
    "can_delete_records": False,
    "can_create_tables": True,
    "can_send_proactive": True,
}

CONNECTOR_CAPABILITIES: tuple[ConnectorCapability, ...] = (
    ConnectorCapability(
        platform="slack",
        connector="chat",
        capabilities=dict(_CHAT_TEXT_CAPABILITIES),
        limits={"max_message_chars": 40000},
    ),
    ConnectorCapability(
        platform="feishu",
        connector="chat",
        capabilities=dict(_CHAT_TEXT_CAPABILITIES),
        limits={"max_message_chars": 15000},
    ),
    ConnectorCapability(
        platform="feishu",
        connector="base",
        capabilities=dict(_TABLE_CAPABILITIES),
        limits={"batch_size": 30},
    ),
    ConnectorCapability(
        platform="lark",
        connector="chat",
        capabilities=dict(_CHAT_TEXT_CAPABILITIES),
        limits={"max_message_chars": 15000},
    ),
    ConnectorCapability(
        platform="lark",
        connector="base",
        capabilities=dict(_TABLE_CAPABILITIES),
        limits={"batch_size": 30},
    ),
    ConnectorCapability(
        platform="dingtalk",
        connector="chat",
        capabilities=dict(_CHAT_TEXT_CAPABILITIES),
        limits={"max_message_chars": 20000},
    ),
    ConnectorCapability(
        platform="dingtalk",
        connector="ai_table",
        capabilities=dict(_TABLE_CAPABILITIES),
        limits={"batch_size": 30},
    ),
    ConnectorCapability(
        platform="github",
        connector="issues",
        capabilities={
            "can_create_issues": True,
            "can_comment_issues": True,
            "can_close_issues": False,
            "can_delete_issues": False,
            "can_read_thread": False,
            "can_apply_action": True,
        },
        limits={"transport": "gh CLI (must be installed and authenticated)"},
    ),
    ConnectorCapability(
        platform="gitlab",
        connector="issues",
        capabilities={
            "can_create_issues": True,
            "can_comment_issues": True,
            "can_close_issues": False,
            "can_delete_issues": False,
            "can_read_thread": False,
            "can_apply_action": True,
        },
        limits={"transport": "glab CLI (must be installed and authenticated)"},
    ),
)


def list_capabilities(platform: str | None = None) -> tuple[ConnectorCapability, ...]:
    if platform is None:
        return CONNECTOR_CAPABILITIES
    return tuple(item for item in CONNECTOR_CAPABILITIES if item.platform == platform)


def get_capability(platform: str, connector: str) -> ConnectorCapability:
    for item in CONNECTOR_CAPABILITIES:
        if item.platform == platform and item.connector == connector:
            return item
    known = ", ".join(f"{item.platform}/{item.connector}" for item in CONNECTOR_CAPABILITIES)
    raise SchemaError(
        code="unknown_connector",
        message=f"No capability declaration for {platform}/{connector}",
        recovery_hint=f"Known connectors: {known}.",
    )
