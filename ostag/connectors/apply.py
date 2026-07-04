"""Apply executors: turn approved action receipts into real connector writes.

An executor receives a receipt whose ``apply_payload`` describes the exact
mutation (operation, base/table, record, patch or rows). The payload is built
at proposal time by the workflow, so what was approved is exactly what gets
applied — nothing is recomputed between approval and apply.
"""

from __future__ import annotations

from typing import Any

from ostag.connectors.requirements import RequirementTableConnector
from ostag.core.schemas import ActionReceipt, SchemaError


def target_system_for_platform(platform: str) -> str:
    """Name of the system-of-record a platform's table writes land in."""
    if platform == "dingtalk":
        return "dingtalk_ai_table"
    if platform == "slack":
        # Slack chats manage requirements in a Lark base via lark-cli.
        return "lark_base"
    return f"{platform}_base"


class RequirementTableApplyExecutor:
    """Applies approved record patches and row imports to a requirement table."""

    def __init__(self, connector: RequirementTableConnector) -> None:
        self.connector = connector

    async def apply(self, receipt: ActionReceipt) -> str:
        payload: dict[str, Any] = receipt.apply_payload
        operation = payload.get("operation")
        base_token = payload.get("base_token", "")
        table_id = payload.get("table_id", "")
        if not base_token or not table_id:
            raise SchemaError(
                code="invalid_apply_payload",
                message=f"Receipt {receipt.action_id} is missing base_token/table_id",
                recovery_hint="Re-propose the action so the payload is rebuilt.",
            )
        if operation == "batch_update":
            record_id = payload.get("record_id", "")
            patch = payload.get("patch", {})
            if not record_id or not patch:
                raise SchemaError(
                    code="invalid_apply_payload",
                    message=f"Receipt {receipt.action_id} has no record_id/patch to apply",
                    recovery_hint="Re-propose the action so the payload is rebuilt.",
                )
            await self.connector.batch_update(base_token, table_id, record_id, patch)
            return f"updated {record_id} fields: {', '.join(patch)}"
        if operation == "batch_create":
            rows_payload = payload.get("payload", {})
            rows = rows_payload.get("rows", [])
            if not rows:
                raise SchemaError(
                    code="invalid_apply_payload",
                    message=f"Receipt {receipt.action_id} has no rows to create",
                    recovery_hint="Re-propose the action so the payload is rebuilt.",
                )
            await self.connector.batch_create(base_token, table_id, rows_payload)
            return f"created {len(rows)} records in {table_id}"
        raise SchemaError(
            code="unsupported_operation",
            message=f"Receipt {receipt.action_id} has unsupported operation {operation!r}",
            recovery_hint="Supported operations: batch_update, batch_create.",
        )


def build_apply_executors(workflow: Any) -> dict[str, Any]:
    """Executor registry for an AutoBaseWorkflow's connectors, keyed by target system."""
    executor = RequirementTableApplyExecutor(workflow.requirements)
    return {
        "feishu_base": executor,
        "lark_base": executor,
        "dingtalk_ai_table": executor,
    }
