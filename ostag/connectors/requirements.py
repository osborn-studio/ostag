from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Protocol


class RequirementTableConnector(Protocol):
    async def list_tables(self, base_token: str) -> dict[str, Any]: ...

    async def list_fields(self, base_token: str, table_id: str) -> dict[str, Any]: ...

    async def download_message_file(
        self, message_id: str, file_key: str, output: Path
    ) -> dict[str, Any]: ...

    async def batch_create(
        self, base_token: str, table_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]: ...

    async def search(
        self,
        base_token: str,
        table_id: str,
        keyword: str,
        search_fields: list[str],
        projection_fields: list[str],
        limit: int = 5,
    ) -> dict[str, Any]: ...

    async def batch_update(
        self, base_token: str, table_id: str, record_id: str, patch: dict[str, Any]
    ) -> dict[str, Any]: ...


class LarkCLIRequirementTableConnector:
    def __init__(self, cli: Any, identity: str):
        self.cli = cli
        self.identity = identity

    async def list_tables(self, base_token: str) -> dict[str, Any]:
        return await self.cli.json(
            "base",
            "+table-list",
            "--as",
            self.identity,
            "--base-token",
            base_token,
            "--json",
        )

    async def list_fields(self, base_token: str, table_id: str) -> dict[str, Any]:
        return await self.cli.json(
            "base",
            "+field-list",
            "--as",
            self.identity,
            "--base-token",
            base_token,
            "--table-id",
            table_id,
            "--json",
        )

    async def download_message_file(
        self, message_id: str, file_key: str, output: Path
    ) -> dict[str, Any]:
        return await self.cli.json(
            "im",
            "+messages-resources-download",
            "--as",
            self.identity,
            "--message-id",
            message_id,
            "--file-key",
            file_key,
            "--type",
            "file",
            "--output",
            str(output),
            "--json",
        )

    async def batch_create(
        self, base_token: str, table_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        return await self.cli.json(
            "base",
            "+record-batch-create",
            "--as",
            self.identity,
            "--base-token",
            base_token,
            "--table-id",
            table_id,
            "--json",
            json.dumps(payload, ensure_ascii=False),
        )

    async def search(
        self,
        base_token: str,
        table_id: str,
        keyword: str,
        search_fields: list[str],
        projection_fields: list[str],
        limit: int = 5,
    ) -> dict[str, Any]:
        args = [
            "base",
            "+record-search",
            "--as",
            self.identity,
            "--base-token",
            base_token,
            "--table-id",
            table_id,
            "--keyword",
            keyword,
            "--limit",
            str(limit),
            "--format",
            "json",
        ]
        for name in search_fields:
            args.extend(["--search-field", name])
        for name in projection_fields:
            args.extend(["--field-id", name])
        return await self.cli.json(*args)

    async def batch_update(
        self, base_token: str, table_id: str, record_id: str, patch: dict[str, Any]
    ) -> dict[str, Any]:
        payload = {"record_id_list": [record_id], "patch": patch}
        return await self.cli.json(
            "base",
            "+record-batch-update",
            "--as",
            self.identity,
            "--base-token",
            base_token,
            "--table-id",
            table_id,
            "--json",
            json.dumps(payload, ensure_ascii=False),
        )
