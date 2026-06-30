from __future__ import annotations

from typing import Any, Protocol


class TaskConnector(Protocol):
    async def create_task(
        self,
        *,
        summary: str,
        description: str,
        assignee_open_id: str | None,
        idempotency_key: str,
    ) -> dict[str, Any]: ...


class LarkCLITaskConnector:
    def __init__(self, cli: Any, identity: str):
        self.cli = cli
        self.identity = identity

    async def create_task(
        self,
        *,
        summary: str,
        description: str,
        assignee_open_id: str | None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        args = [
            "task",
            "+create",
            "--as",
            self.identity,
            "--summary",
            summary,
            "--description",
            description,
            "--idempotency-key",
            idempotency_key,
            "--json",
        ]
        if assignee_open_id:
            args.extend(["--assignee", assignee_open_id])
        return await self.cli.json(*args)
