from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError

from ostag.connectors.requirements import (
    DwsRequirementTableConnector,
    FeishuBaseReadConnector,
    LarkCLIRequirementTableConnector,
)
from ostag.connectors.tasks import LarkCLITaskConnector


class CapturingCLI:
    def __init__(self):
        self.calls = []

    async def json(self, *args):
        self.calls.append(args)
        return {"ok": True, "data": {}}


async def test_feishu_base_read_connector_uses_app_identity_and_read_only_search() -> None:
    requests = []

    def opener(request):
        requests.append(request)
        if request.full_url.endswith("/auth/v3/tenant_access_token/internal"):
            return {"code": 0, "tenant_access_token": "tenant-token"}
        return {
            "code": 0,
            "data": {
                "items": [
                    {"record_id": "rec1", "fields": {"需求描述": "交易盯盘"}}
                ]
            },
        }

    connector = FeishuBaseReadConnector(
        api_base_url="https://open.feishu.cn",
        app_id="cli_demo",
        app_secret="secret-demo",
        opener=opener,
    )

    result = await connector.list_records(
        "appToken", "tblDemo", ["需求描述", "需求状态"], limit=200
    )

    assert result["data"]["items"][0]["record_id"] == "rec1"
    assert len(requests) == 2
    auth, search = requests
    assert auth.get_method() == "POST"
    assert json.loads(auth.data) == {
        "app_id": "cli_demo",
        "app_secret": "secret-demo",
    }
    assert search.get_method() == "POST"
    assert search.full_url.endswith(
        "/open-apis/bitable/v1/apps/appToken/tables/tblDemo/records/search?page_size=200"
    )
    assert search.headers["Authorization"] == "Bearer tenant-token"
    assert json.loads(search.data) == {"field_names": ["需求描述", "需求状态"]}


async def test_feishu_base_read_connector_rejects_api_errors() -> None:
    responses = iter(
        [
            {"code": 0, "tenant_access_token": "tenant-token"},
            {"code": 99991672, "msg": "missing scope"},
        ]
    )
    connector = FeishuBaseReadConnector(
        api_base_url="https://open.feishu.cn",
        app_id="cli_demo",
        app_secret="secret-demo",
        opener=lambda request: next(responses),
    )

    try:
        await connector.list_records("appToken", "tblDemo", ["需求描述"])
    except RuntimeError as exc:
        assert "99991672" in str(exc)
        assert "secret-demo" not in str(exc)
    else:
        raise AssertionError("expected Feishu API error")


async def test_feishu_base_read_connector_preserves_json_http_errors(monkeypatch) -> None:
    def reject(request, timeout):
        raise HTTPError(
            request.full_url,
            400,
            "Bad Request",
            {},
            BytesIO(b'{"code":99991672,"msg":"missing scope"}'),
        )

    monkeypatch.setattr("urllib.request.urlopen", reject)
    connector = FeishuBaseReadConnector(
        api_base_url="https://open.feishu.cn",
        app_id="cli_demo",
        app_secret="secret-demo",
    )

    try:
        await connector.list_records("appToken", "tblDemo", ["需求描述"])
    except RuntimeError as exc:
        assert "99991672" in str(exc)
    else:
        raise AssertionError("expected Feishu HTTP error")


async def test_requirement_connector_search_builds_lark_cli_arguments() -> None:
    cli = CapturingCLI()
    connector = LarkCLIRequirementTableConnector(cli, "user")

    await connector.search(
        "base_token",
        "tblDemo",
        "交易盯盘",
        search_fields=["需求描述"],
        projection_fields=["需求描述", "需求状态"],
    )

    assert cli.calls == [
        (
            "base",
            "+record-search",
            "--as",
            "user",
            "--base-token",
            "base_token",
            "--table-id",
            "tblDemo",
            "--keyword",
            "交易盯盘",
            "--limit",
            "5",
            "--format",
            "json",
            "--search-field",
            "需求描述",
            "--field-id",
            "需求描述",
            "--field-id",
            "需求状态",
        )
    ]


async def test_requirement_connector_batch_update_uses_record_id_list_payload() -> None:
    cli = CapturingCLI()
    connector = LarkCLIRequirementTableConnector(cli, "user")

    await connector.batch_update("base_token", "tblDemo", "recDemo", {"需求状态": "开发交付中"})

    call = cli.calls[0]
    payload = json.loads(call[call.index("--json") + 1])
    assert call[:2] == ("base", "+record-batch-update")
    assert payload == {
        "record_id_list": ["recDemo"],
        "patch": {"需求状态": "开发交付中"},
    }


async def test_requirement_connector_list_records_builds_lark_cli_arguments() -> None:
    cli = CapturingCLI()
    connector = LarkCLIRequirementTableConnector(cli, "user")

    await connector.list_records(
        "base_token",
        "tblDemo",
        projection_fields=["需求名称", "负责人"],
        limit=20,
    )

    assert cli.calls == [
        (
            "base",
            "+record-list",
            "--as",
            "user",
            "--base-token",
            "base_token",
            "--table-id",
            "tblDemo",
            "--limit",
            "20",
            "--format",
            "json",
            "--field-id",
            "需求名称",
            "--field-id",
            "负责人",
        )
    ]


async def test_requirement_connector_download_uses_relative_output_path() -> None:
    cli = CapturingCLI()
    connector = LarkCLIRequirementTableConnector(cli, "user")

    await connector.download_message_file("omDemo", "fileKey", Path("tmp/file.docx"))

    assert "--output" in cli.calls[0]
    assert cli.calls[0][cli.calls[0].index("--output") + 1] == "tmp/file.docx"


async def test_dws_requirement_connector_creates_records_with_field_ids() -> None:
    class FakeDwsCLI(CapturingCLI):
        async def json(self, *args):
            self.calls.append(args)
            if args[:3] == ("aitable", "table", "get"):
                return {
                    "data": {
                        "tables": [
                            {
                                "tableId": "tblDing",
                                "fields": [
                                    {
                                        "fieldId": "fldStatus",
                                        "fieldName": "需求状态",
                                        "type": "singleSelect",
                                    },
                                    {
                                        "fieldId": "fldDesc",
                                        "fieldName": "需求描述",
                                        "type": "text",
                                    },
                                ],
                            }
                        ]
                    }
                }
            return {"status": "success", "data": {}}

    cli = FakeDwsCLI()
    connector = DwsRequirementTableConnector(cli)

    await connector.batch_create(
        "baseDing",
        "tblDing",
        {"fields": ["需求状态", "需求描述"], "rows": [["待评估", "客户需要日报汇总"]]},
    )

    create_call = cli.calls[-1]
    records = json.loads(create_call[create_call.index("--records") + 1])
    assert create_call[:3] == ("aitable", "record", "create")
    assert records == [
        {"cells": {"fldStatus": "待评估", "fldDesc": "客户需要日报汇总"}}
    ]


async def test_dws_requirement_connector_normalizes_record_field_names() -> None:
    class FakeDwsCLI(CapturingCLI):
        async def json(self, *args):
            self.calls.append(args)
            if args[:3] == ("aitable", "table", "get"):
                return {
                    "data": {
                        "tables": [
                            {
                                "tableId": "tblDing",
                                "fields": [
                                    {"fieldId": "fldDesc", "fieldName": "需求描述", "type": "text"}
                                ],
                            }
                        ]
                    }
                }
            return {
                "status": "success",
                "data": {
                    "records": [
                        {"recordId": "recDing", "cells": {"fldDesc": "日报自动汇总"}}
                    ]
                },
            }

    cli = FakeDwsCLI()
    connector = DwsRequirementTableConnector(cli)

    data = await connector.search(
        "baseDing",
        "tblDing",
        "日报",
        search_fields=["需求描述"],
        projection_fields=["需求描述"],
    )

    assert data == {
        "data": {"records": [{"record_id": "recDing", "fields": {"需求描述": "日报自动汇总"}}]}
    }


async def test_task_connector_create_task_uses_idempotency_key_and_assignee() -> None:
    cli = CapturingCLI()
    connector = LarkCLITaskConnector(cli, "user")

    await connector.create_task(
        summary="交易盯盘",
        description="需求描述",
        assignee_open_id="ou_demo",
        idempotency_key="ostag-recDemo",
    )

    call = cli.calls[0]
    assert call[:2] == ("task", "+create")
    assert call[call.index("--idempotency-key") + 1] == "ostag-recDemo"
    assert call[call.index("--assignee") + 1] == "ou_demo"
