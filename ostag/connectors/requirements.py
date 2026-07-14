from __future__ import annotations

import asyncio
import json
import os
import shlex
import urllib.request
from pathlib import Path
from typing import Any, Callable, Protocol
from urllib.parse import quote, urlencode


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

    async def list_records(
        self,
        base_token: str,
        table_id: str,
        projection_fields: list[str],
        limit: int = 50,
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

    async def list_records(
        self,
        base_token: str,
        table_id: str,
        projection_fields: list[str],
        limit: int = 50,
    ) -> dict[str, Any]:
        args = [
            "base",
            "+record-list",
            "--as",
            self.identity,
            "--base-token",
            base_token,
            "--table-id",
            table_id,
            "--limit",
            str(limit),
            "--format",
            "json",
        ]
        for name in projection_fields:
            args.extend(["--field-id", name])
        return await self.cli.json(*args)


def _open_feishu_json(request: urllib.request.Request) -> dict[str, Any]:
    with urllib.request.urlopen(request, timeout=15) as response:  # noqa: S310
        value = json.loads(response.read().decode("utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError("Feishu returned a non-object response")
    return value


class FeishuBaseReadConnector:
    """Read Base records with the server app identity, without user CLI state."""

    def __init__(
        self,
        *,
        api_base_url: str,
        app_id: str,
        app_secret: str,
        opener: Callable[[urllib.request.Request], dict[str, Any]] = _open_feishu_json,
    ) -> None:
        self.api_base_url = api_base_url.rstrip("/")
        self.app_id = app_id
        self.app_secret = app_secret
        self.opener = opener

    async def _tenant_access_token(self) -> str:
        request = urllib.request.Request(
            f"{self.api_base_url}/open-apis/auth/v3/tenant_access_token/internal",
            data=json.dumps(
                {"app_id": self.app_id, "app_secret": self.app_secret}
            ).encode(),
            method="POST",
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        response = await asyncio.to_thread(self.opener, request)
        token = response.get("tenant_access_token")
        if response.get("code", 0) != 0 or not isinstance(token, str) or not token:
            code = response.get("code", "unknown")
            raise RuntimeError(f"Feishu tenant token request failed: code={code}")
        return token

    async def list_records(
        self,
        base_token: str,
        table_id: str,
        projection_fields: list[str],
        limit: int = 50,
    ) -> dict[str, Any]:
        if not 1 <= limit <= 500:
            raise ValueError("Feishu Base record limit must be in 1..500")
        token = await self._tenant_access_token()
        path = (
            f"/open-apis/bitable/v1/apps/{quote(base_token, safe='')}"
            f"/tables/{quote(table_id, safe='')}/records/search"
        )
        request = urllib.request.Request(
            f"{self.api_base_url}{path}?{urlencode({'page_size': limit})}",
            data=json.dumps({"field_names": projection_fields}, ensure_ascii=False).encode(),
            method="POST",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=utf-8",
            },
        )
        response = await asyncio.to_thread(self.opener, request)
        if response.get("code", 0) != 0:
            code = response.get("code", "unknown")
            message = str(response.get("msg") or "request rejected")[:200]
            raise RuntimeError(f"Feishu Base read failed: code={code} msg={message}")
        data = response.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("items", []), list):
            raise RuntimeError("Feishu Base read returned an invalid payload")
        return response


class DwsCLI:
    def __init__(self, config: Any):
        self.config = config
        self.cwd = Path(config.auto_base_workdir)
        self.argv = shlex.split(config.dws_cli_bin)

    async def json(self, *args: str) -> dict[str, Any]:
        if not self.argv:
            raise RuntimeError("DWS_CLI_BIN is empty.")
        proc = await asyncio.create_subprocess_exec(
            *self.argv,
            *args,
            cwd=self.cwd,
            env=dict(os.environ),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=self.config.auto_base_cli_timeout_seconds
        )
        out = stdout.decode("utf-8", errors="replace").strip()
        err = stderr.decode("utf-8", errors="replace").strip()
        if proc.returncode != 0:
            raise RuntimeError((err or out or f"dws exited {proc.returncode}")[-3000:])
        try:
            data = json.loads(out or "{}")
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"dws returned non-JSON output: {out[-1000:]}") from exc
        if data.get("success") is False or data.get("status") == "error":
            raise RuntimeError(json.dumps(data.get("error", data), ensure_ascii=False)[-3000:])
        return data


class DwsRequirementTableConnector:
    def __init__(self, cli: DwsCLI):
        self.cli = cli

    async def list_tables(self, base_token: str) -> dict[str, Any]:
        return await self.cli.json(
            "aitable",
            "table",
            "get",
            "--base-id",
            base_token,
            "--format",
            "json",
        )

    async def list_fields(self, base_token: str, table_id: str) -> dict[str, Any]:
        return await self.cli.json(
            "aitable",
            "table",
            "get",
            "--base-id",
            base_token,
            "--table-ids",
            table_id,
            "--format",
            "json",
        )

    async def download_message_file(
        self, message_id: str, file_key: str, output: Path
    ) -> dict[str, Any]:
        raise RuntimeError("DingTalk message file download is not implemented yet.")

    async def batch_create(
        self, base_token: str, table_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        field_map = await self._field_map(base_token, table_id)
        records = rows_payload_to_dws_records(payload, field_map)
        return await self.cli.json(
            "aitable",
            "record",
            "create",
            "--base-id",
            base_token,
            "--table-id",
            table_id,
            "--records",
            json.dumps(records, ensure_ascii=False),
            "--format",
            "json",
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
        field_map = await self._field_map(base_token, table_id)
        field_ids = [
            field_map[name]["fieldId"]
            for name in projection_fields
            if name in field_map and field_map[name].get("fieldId")
        ]
        args = [
            "aitable",
            "record",
            "query",
            "--base-id",
            base_token,
            "--table-id",
            table_id,
            "--query",
            keyword,
            "--limit",
            str(limit),
            "--format",
            "json",
        ]
        if field_ids:
            args.extend(["--field-ids", ",".join(field_ids)])
        data = await self.cli.json(*args)
        return normalize_dws_records(data, field_map)

    async def batch_update(
        self, base_token: str, table_id: str, record_id: str, patch: dict[str, Any]
    ) -> dict[str, Any]:
        field_map = await self._field_map(base_token, table_id)
        cells = named_cells_to_dws_cells(patch, field_map)
        return await self.cli.json(
            "aitable",
            "record",
            "update",
            "--base-id",
            base_token,
            "--table-id",
            table_id,
            "--records",
            json.dumps([{"recordId": record_id, "cells": cells}], ensure_ascii=False),
            "--format",
            "json",
        )

    async def list_records(
        self,
        base_token: str,
        table_id: str,
        projection_fields: list[str],
        limit: int = 50,
    ) -> dict[str, Any]:
        field_map = await self._field_map(base_token, table_id)
        field_ids = [
            field_map[name]["fieldId"]
            for name in projection_fields
            if name in field_map and field_map[name].get("fieldId")
        ]
        args = [
            "aitable",
            "record",
            "query",
            "--base-id",
            base_token,
            "--table-id",
            table_id,
            "--limit",
            str(limit),
            "--format",
            "json",
        ]
        if field_ids:
            args.extend(["--field-ids", ",".join(field_ids)])
        data = await self.cli.json(*args)
        return normalize_dws_records(data, field_map)

    async def _field_map(self, base_token: str, table_id: str) -> dict[str, dict[str, Any]]:
        data = await self.list_fields(base_token, table_id)
        fields: list[dict[str, Any]] = []
        for table in data.get("data", {}).get("tables", []):
            if table.get("tableId") == table_id or table.get("table_id") == table_id:
                fields = table.get("fields", [])
                break
        return {
            str(field.get("fieldName") or field.get("name")): field
            for field in fields
            if field.get("fieldName") or field.get("name")
        }


def rows_payload_to_dws_records(
    payload: dict[str, Any], field_map: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    fields = [str(field) for field in payload.get("fields", [])]
    records = []
    for row in payload.get("rows", []):
        if not isinstance(row, list):
            continue
        cells = {}
        for idx, value in enumerate(row):
            if idx >= len(fields):
                continue
            field = field_map.get(fields[idx])
            if not field:
                continue
            cells[str(field["fieldId"])] = dws_cell_value(value, str(field.get("type") or ""))
        if cells:
            records.append({"cells": cells})
    return records


def named_cells_to_dws_cells(
    patch: dict[str, Any], field_map: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    cells = {}
    for name, value in patch.items():
        field = field_map.get(name)
        if not field:
            continue
        cells[str(field["fieldId"])] = dws_cell_value(value, str(field.get("type") or ""))
    return cells


def dws_cell_value(value: Any, field_type: str) -> Any:
    normalized = field_type.lower()
    if normalized == "richtext" and isinstance(value, str):
        return {"markdown": value}
    if normalized == "user" and isinstance(value, list):
        converted = []
        for item in value:
            if isinstance(item, dict) and item.get("userId"):
                converted.append(item)
            elif isinstance(item, dict) and item.get("id"):
                converted.append({"userId": item["id"]})
        return converted or value
    return value


def normalize_dws_records(
    data: dict[str, Any], field_map: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    id_to_name = {
        str(field.get("fieldId")): name
        for name, field in field_map.items()
        if field.get("fieldId")
    }
    records = data.get("data", {}).get("records") or data.get("data", {}).get("items") or []
    normalized = []
    for record in records:
        if not isinstance(record, dict):
            continue
        raw_cells = record.get("cells") or record.get("fields") or {}
        fields = {
            id_to_name.get(str(field_id), str(field_id)): value
            for field_id, value in raw_cells.items()
        }
        normalized.append(
            {
                "record_id": record.get("recordId") or record.get("record_id") or record.get("id"),
                "fields": fields,
            }
        )
    return {"data": {"records": normalized}}
