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


def task_system_for_platform(platform: str) -> str:
    """Name of the task system a platform's task creations land in."""
    if platform in ("slack", "lark"):
        return "lark_task"
    return f"{platform}_task"


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


def _task_url(data: dict[str, Any]) -> str:
    candidates = [data]
    if isinstance(data.get("task"), dict):
        candidates.append(data["task"])
    for item in candidates:
        for key in ("url", "app_link", "link"):
            value = item.get(key)
            if value:
                return str(value)
    return ""


class TaskCreateApplyExecutor:
    """Creates the approved task and backfills its link into declared record fields."""

    def __init__(self, tasks: Any, requirements: RequirementTableConnector) -> None:
        self.tasks = tasks
        self.requirements = requirements

    async def apply(self, receipt: ActionReceipt) -> str:
        payload: dict[str, Any] = receipt.apply_payload
        if payload.get("operation") != "create_task":
            raise SchemaError(
                code="unsupported_operation",
                message=f"Receipt {receipt.action_id} has unsupported operation "
                f"{payload.get('operation')!r} for the task executor",
                recovery_hint="Task receipts must carry operation=create_task.",
            )
        summary = payload.get("summary", "")
        if not summary:
            raise SchemaError(
                code="invalid_apply_payload",
                message=f"Receipt {receipt.action_id} has no task summary",
                recovery_hint="Re-propose the action so the payload is rebuilt.",
            )
        result = await self.tasks.create_task(
            summary=summary,
            description=payload.get("description", ""),
            assignee_open_id=payload.get("assignee_open_id") or None,
            idempotency_key=payload.get("idempotency_key", f"ostag-{receipt.action_id}"),
        )
        data = result.get("data", {}) if isinstance(result.get("data"), dict) else result
        url = _task_url(data)
        backfill = [name for name in payload.get("backfill_fields", []) if name]
        evidence = f"created task {url or '(no url returned)'}"
        if url and backfill and payload.get("record_id"):
            await self.requirements.batch_update(
                payload.get("base_token", ""),
                payload.get("table_id", ""),
                payload["record_id"],
                {name: url for name in backfill},
            )
            evidence += f"; backfilled {', '.join(backfill)} on {payload['record_id']}"
        return evidence


def build_apply_executors(workflow: Any) -> dict[str, Any]:
    """Executor registry for an AutoBaseWorkflow's connectors, keyed by target system."""
    executor = RequirementTableApplyExecutor(workflow.requirements)
    registry: dict[str, Any] = {
        "feishu_base": executor,
        "lark_base": executor,
        "dingtalk_ai_table": executor,
    }
    if hasattr(workflow, "tasks"):
        task_executor = TaskCreateApplyExecutor(workflow.tasks, workflow.requirements)
        registry["feishu_task"] = task_executor
        registry["lark_task"] = task_executor
    return registry
