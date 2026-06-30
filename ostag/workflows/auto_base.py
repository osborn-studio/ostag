from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse
from xml.etree import ElementTree

from ostag.config import Settings
from ostag.connectors.requirements import (
    LarkCLIRequirementTableConnector,
    RequirementTableConnector,
)
from ostag.connectors.tasks import LarkCLITaskConnector, TaskConnector
from ostag.core.channel_brain import ChannelBrainStore, ChannelPolicy, channel_policy_for_event
from ostag.core.event import NormalizedEvent
from ostag.core.stage_ledger import StageLedger


BASE_URL_RE = re.compile(r"https?://[^\s<>\"]+\.feishu\.cn/(?:app|base)/[^\s<>\"]+")
UNSUPPORTED_APPLINK_RE = re.compile(
    r"https?://applink\.feishu\.cn/client/message/link/open\?[^\s<>\"]+"
)
FILE_REF_RE = re.compile(r'<file\s+[^>]*key="(?P<key>[^"]+)"[^>]*name="(?P<name>[^"]+)"')
READONLY_FIELD_TYPES = {
    "formula",
    "lookup",
    "created_at",
    "updated_at",
    "created_by",
    "updated_by",
    "auto_number",
    "attachment",
}


@dataclass(frozen=True)
class BaseContext:
    base_token: str
    table_id: str | None
    url: str


@dataclass(frozen=True)
class ChatFile:
    message_id: str
    file_key: str
    name: str


@dataclass(frozen=True)
class WorkflowContext:
    base: BaseContext
    file: ChatFile


@dataclass(frozen=True)
class FieldInfo:
    name: str
    field_id: str | None
    field_type: str
    options: tuple[str, ...] = ()


@dataclass(frozen=True)
class TableInfo:
    table_id: str
    name: str


@dataclass(frozen=True)
class ResolvedTable:
    base_token: str
    table: TableInfo
    base_name: str | None = None
    base_url: str | None = None


@dataclass(frozen=True)
class MentionedUser:
    open_id: str
    name: str


@dataclass(frozen=True)
class RequirementManagementCommand:
    target: str
    status: str | None = None
    priority: str | None = None
    assignee: MentionedUser | None = None
    create_task: bool = False
    base: BaseContext | None = None


@dataclass(frozen=True)
class MatchingRecord:
    record_id: str
    fields: dict[str, Any]


class AutoBaseError(RuntimeError):
    pass


class LarkCLI:
    def __init__(self, config: Settings):
        self.config = config
        self.cwd = Path(config.auto_base_workdir)

    def subprocess_env(self, base_env: dict[str, str] | None = None) -> dict[str, str]:
        env = dict(base_env or os.environ)
        if Path(self.config.lark_cli_bin).is_absolute():
            bin_dir = str(Path(self.config.lark_cli_bin).parent)
            env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
        return env

    async def json(self, *args: str) -> dict[str, Any]:
        proc = await asyncio.create_subprocess_exec(
            self.config.lark_cli_bin,
            *args,
            cwd=self.cwd,
            env=self.subprocess_env(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=self.config.auto_base_cli_timeout_seconds
        )
        out = stdout.decode("utf-8", errors="replace").strip()
        err = stderr.decode("utf-8", errors="replace").strip()
        if proc.returncode != 0:
            raise AutoBaseError((err or out or f"lark-cli exited {proc.returncode}")[-3000:])
        try:
            data = json.loads(out or "{}")
        except json.JSONDecodeError as exc:
            raise AutoBaseError(f"lark-cli returned non-JSON output: {out[-1000:]}") from exc
        if data.get("ok") is False:
            raise AutoBaseError(json.dumps(data.get("error", data), ensure_ascii=False)[-3000:])
        return data


class AutoBaseWorkflow:
    def __init__(
        self,
        config: Settings,
        cli: LarkCLI | None = None,
        requirement_connector: RequirementTableConnector | None = None,
        task_connector: TaskConnector | None = None,
    ):
        self.config = config
        self.cli = cli or LarkCLI(config)
        self.requirements = requirement_connector or LarkCLIRequirementTableConnector(
            self.cli, config.auto_base_identity
        )
        self.tasks = task_connector or LarkCLITaskConnector(self.cli, config.auto_base_identity)
        self.channel_brain = ChannelBrainStore(config)

    async def handle(self, event: NormalizedEvent) -> str | None:
        if not self.config.auto_base_enabled:
            return None
        if event.platform not in {"feishu", "lark"}:
            return None
        if is_auto_base_status_request(event.text):
            return (
                "自动入表能力已经上线：我可以从群历史中定位最近的 docx 和 Base 链接，"
                "下载并解析需求文档，写入需求池；也可以维护默认需求底表，"
                "支持分配负责人、更新状态/优先级、创建飞书任务。"
            )
        management_command = parse_requirement_management_command(event, self.config)
        if management_command:
            ledger = StageLedger()
            try:
                return await self._handle_requirement_management(event, management_command, ledger)
            except TimeoutError as exc:
                return f"需求运营未完成：调用 lark-cli 超时。{exc}\n{ledger.format_user()}"
            except AutoBaseError as exc:
                return f"需求运营未完成：{friendly_lark_error(str(exc))}\n{ledger.format_user()}"
            except Exception as exc:  # noqa: BLE001
                return f"需求运营未完成：{type(exc).__name__}: {exc}\n{ledger.format_user()}"
        if not is_auto_base_request(event.text):
            return None

        try:
            context = await self._load_context(event)
            resolved = await self._resolve_or_create_table(context.base)
            fields = await self._list_fields(resolved.base_token, resolved.table.table_id)
            path = await self._download_file(context.file)
            text = extract_docx_text(path)
            records = extract_docx_records(text, source_name=context.file.name)
            payload = build_rows_for_fields(
                records,
                fields,
                source_file=context.file.name,
                source_message_id=context.file.message_id,
                now_text=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            )
            if not payload["rows"]:
                raise AutoBaseError("docx 中没有识别到可写入的需求内容。")
            result = await self._write_rows(resolved.base_token, resolved.table.table_id, payload)
        except TimeoutError as exc:
            return f"自动入表未完成：调用 lark-cli 超时。{exc}"
        except AutoBaseError as exc:
            return f"自动入表未完成：{friendly_lark_error(str(exc))}"
        except Exception as exc:  # noqa: BLE001
            return f"自动入表未完成：{type(exc).__name__}: {exc}"

        count = len(payload["rows"])
        base_name = resolved.base_name or result.get("base_name") or resolved.base_token
        suffix = f"（{resolved.base_url}）" if resolved.base_url else ""
        return (
            f"自动入表完成：已从《{context.file.name}》提取 {count} 条需求，"
            f"写入 Base「{base_name}」的表「{resolved.table.name}」{suffix}。"
        )

    async def _handle_requirement_management(
        self,
        event: NormalizedEvent,
        command: RequirementManagementCommand,
        ledger: StageLedger | None = None,
    ) -> str:
        ledger = ledger or StageLedger()
        ledger.complete("received", "已接收请求")
        base = command.base or default_base_context(self.config)
        if not base:
            raise AutoBaseError(
                "还没有配置默认需求底表。请在命令里附上 Base 链接，或设置 "
                "OSTAG_AUTO_BASE_DEFAULT_BASE_TOKEN 和 OSTAG_AUTO_BASE_DEFAULT_TABLE_ID。"
            )
        ledger.complete("policy_loaded", "已加载频道策略")
        resolved = await self._resolve_or_create_table(base)
        fields = await self._list_fields(resolved.base_token, resolved.table.table_id)
        ledger.complete("context_loaded", "已读取需求表结构")
        matches = await self._search_requirement_records(
            resolved.base_token, resolved.table.table_id, fields, command.target
        )
        if not matches:
            raise AutoBaseError(f"没有在需求表中找到匹配「{command.target}」的记录。")
        if len(matches) > 1:
            labels = "；".join(record_label(record) for record in matches[:5])
            raise AutoBaseError(f"找到多条匹配需求，请说得更具体：{labels}")

        record = matches[0]
        ledger.complete("target_found", "已定位需求")
        patch = build_requirement_patch(command, fields)
        policy = channel_policy_for_event(event, self.config)
        enforce_requirement_policy(command, patch, policy)
        self._enforce_action_budget(event, policy)
        ledger.complete("permission_checked", "已校验可执行动作")
        task_result: dict[str, Any] | None = None
        if command.create_task:
            task_result = await self._create_lark_task(command, record)
            task_url = extract_task_url(task_result)
            if task_url:
                patch.update(field_patch_if_exists(fields, "飞书任务", task_url))
                patch.update(field_patch_if_exists(fields, "任务链接", task_url))
        if patch:
            await self._update_record(resolved.base_token, resolved.table.table_id, record.record_id, patch)
        if patch or command.create_task:
            self.channel_brain.record_action(event.platform, event.chat_id)
        ledger.complete("executed", "已执行写入")
        if not patch and not command.create_task:
            raise AutoBaseError("没有识别到要更新的负责人、状态、优先级或任务动作。")

        actions: list[str] = []
        if command.assignee:
            actions.append(f"负责人：{command.assignee.name or command.assignee.open_id}")
        if command.status:
            actions.append(f"状态：{coerce_named_field_value(fields, '需求状态', command.status)}")
        if command.priority:
            actions.append(f"优先级：{coerce_named_field_value(fields, '优先级', command.priority)}")
        if command.create_task:
            task_url = extract_task_url(task_result or {})
            actions.append(f"飞书任务：已创建{f'（{task_url}）' if task_url else ''}")
        ledger.complete("replied", "已生成回执")
        return (
            f"需求运营完成：已更新「{record_label(record)}」。"
            + "；".join(actions)
            + "。\n"
            + ledger.format_user()
        )

    async def _load_context(self, event: NormalizedEvent) -> WorkflowContext:
        preferred_base = extract_base_context(event.text)
        if (
            preferred_base is None
            and UNSUPPORTED_APPLINK_RE.search(event.text)
            and is_auto_base_request(event.text)
        ):
            raise AutoBaseError(
                "你发的是飞书客户端消息跳转链接，不是多维表格真实地址。"
                "请复制多维表格的浏览器地址或分享地址，形如 "
                "https://...feishu.cn/app/{base_token} 或 https://...feishu.cn/base/{base_token}"
                "；我拿到真实 Base 链接后才能写入指定报表背后的数据表。"
            )

        messages: list[dict[str, Any]] = []
        page_token: str | None = None
        page_size = min(max(self.config.auto_base_history_page_size, 1), 50)
        for _ in range(5):
            args = [
                "im",
                "+chat-messages-list",
                "--as",
                self.config.auto_base_identity,
                "--chat-id",
                event.chat_id,
                "--page-size",
                str(page_size),
                "--no-reactions",
                "--json",
            ]
            if page_token:
                args.extend(["--page-token", page_token])
            history = await self.cli.json(*args)
            data = history.get("data", {})
            messages.extend(data.get("messages", []))
            if preferred_base:
                file = find_file_ref(messages)
                if file:
                    return WorkflowContext(base=preferred_base, file=file)
            context = find_workflow_context(messages, strict_explicit_target=True)
            if context:
                return context
            page_token = data.get("page_token")
            if not data.get("has_more") or not page_token:
                break
        raise AutoBaseError("没有在最近群消息中找到 docx 文件和 Base 链接。")

    async def _resolve_or_create_table(self, base: BaseContext) -> ResolvedTable:
        if base.table_id:
            tables = await self._list_tables(base.base_token)
            for table in tables:
                if table.table_id == base.table_id:
                    return ResolvedTable(base_token=base.base_token, table=table)
            return ResolvedTable(
                base_token=base.base_token, table=TableInfo(table_id=base.table_id, name=base.table_id)
            )

        tables = await self._list_tables(base.base_token)
        if tables:
            return ResolvedTable(base_token=base.base_token, table=choose_table(tables))
        if not self.config.auto_base_create_table_if_missing:
            raise AutoBaseError("Base 内没有可写入的数据表。")

        fields = json.dumps(default_requirement_fields(), ensure_ascii=False)
        try:
            created = await self.cli.json(
                "base",
                "+table-create",
                "--as",
                self.config.auto_base_identity,
                "--base-token",
                base.base_token,
                "--name",
                self.config.auto_base_default_table_name,
                "--fields",
                fields,
                "--json",
            )
            table_id = extract_table_id(created)
            if table_id:
                return ResolvedTable(
                    base_token=base.base_token,
                    table=TableInfo(table_id=table_id, name=self.config.auto_base_default_table_name),
                )
        except AutoBaseError as exc:
            if "800010808" in str(exc):
                raise AutoBaseError(
                    "目标是多维表格高级应用/页面，不是 OpenAPI 可写的数据表。"
                    "我不会另建需求表；请在页面中打开背后的真实数据表，复制带 "
                    "table=tbl... 的链接，或将 OS Tag 接入该应用的数据源/自动化后再写入。"
                ) from exc
            raise

        created_base = await self.cli.json(
            "base",
            "+base-create",
            "--as",
            self.config.auto_base_identity,
            "--name",
            f"OS Tag {self.config.auto_base_default_table_name}",
            "--table-name",
            self.config.auto_base_default_table_name,
            "--fields",
            fields,
            "--time-zone",
            "Asia/Shanghai",
            "--json",
        )
        base_token = extract_base_token(created_base)
        table_id = extract_table_id(created_base)
        base_name = extract_base_name(created_base) or f"OS Tag {self.config.auto_base_default_table_name}"
        base_url = extract_base_url(created_base)
        if not base_token:
            raise AutoBaseError("目标 Base 无法创建表，且创建伴随 Base 后未能确认 base_token。")
        if not table_id:
            tables = await self._list_tables(base_token)
            if not tables:
                raise AutoBaseError("已创建伴随 Base，但未能确认其中的需求表。")
            table = choose_table(tables)
        else:
            table = TableInfo(table_id=table_id, name=self.config.auto_base_default_table_name)
        return ResolvedTable(base_token=base_token, table=table, base_name=base_name, base_url=base_url)

    async def _list_tables(self, base_token: str) -> list[TableInfo]:
        data = await self.requirements.list_tables(base_token)
        tables = data.get("data", {}).get("tables", [])
        return [
            TableInfo(
                table_id=str(item.get("table_id") or item.get("id") or ""),
                name=str(item.get("name") or item.get("table_name") or item.get("id") or ""),
            )
            for item in tables
            if item.get("table_id") or item.get("id")
        ]

    async def _list_fields(self, base_token: str, table_id: str) -> list[FieldInfo]:
        data = await self.requirements.list_fields(base_token, table_id)
        raw_fields = data.get("data", {}).get("fields", [])
        fields = [field_info(item) for item in raw_fields]
        fields = [field for field in fields if field.name]
        if not fields:
            raise AutoBaseError("目标表没有可读取的字段结构，无法安全写入。")
        return fields

    async def _download_file(self, file: ChatFile) -> Path:
        download_root = Path(self.config.auto_base_download_dir)
        download_root.mkdir(parents=True, exist_ok=True)
        suffix = ".docx" if not file.name.lower().endswith(".docx") else ""
        output = download_root / f"{file.message_id}-{safe_filename(file.name)}{suffix}"
        data = await self.requirements.download_message_file(file.message_id, file.file_key, output)
        local = first_local_path(data) or output
        path = Path(self.config.auto_base_workdir) / local
        if not path.exists():
            raise AutoBaseError(f"文件下载后未找到本地路径：{local}")
        return path

    async def _write_rows(
        self, base_token: str, table_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        result = await self.requirements.batch_create(base_token, table_id, payload)
        return result.get("data", {}) if isinstance(result.get("data"), dict) else {}

    async def _search_requirement_records(
        self, base_token: str, table_id: str, fields: list[FieldInfo], keyword: str
    ) -> list[MatchingRecord]:
        search_fields = choose_search_fields(fields)
        if not search_fields:
            raise AutoBaseError("目标表缺少可搜索的需求文本字段。")
        projection_fields = list(search_fields)
        for name in ("需求状态", "优先级", "需求负责人"):
            if find_field(fields, name) and name not in projection_fields:
                projection_fields.append(name)
        data = await self.requirements.search(
            base_token,
            table_id,
            keyword,
            search_fields=search_fields,
            projection_fields=projection_fields,
            limit=5,
        )
        return extract_matching_records(data)

    async def _update_record(
        self, base_token: str, table_id: str, record_id: str, patch: dict[str, Any]
    ) -> None:
        await self.requirements.batch_update(base_token, table_id, record_id, patch)

    async def _create_lark_task(
        self, command: RequirementManagementCommand, record: MatchingRecord
    ) -> dict[str, Any]:
        summary = record_title(record) or command.target
        description = record_description(record)
        result = await self.tasks.create_task(
            summary=summary[:200],
            description=description[:4000],
            assignee_open_id=command.assignee.open_id if command.assignee else None,
            idempotency_key=f"ostag-{record.record_id}",
        )
        return result.get("data", {}) if isinstance(result.get("data"), dict) else result

    def _enforce_action_budget(self, event: NormalizedEvent, policy: ChannelPolicy) -> None:
        if policy.daily_action_budget <= 0:
            return
        used = self.channel_brain.actions_today(event.platform, event.chat_id)
        if used >= policy.daily_action_budget:
            raise AutoBaseError(
                f"这个群今天的 OS Tag 动作预算已用完：{used}/{policy.daily_action_budget}。"
            )


def is_auto_base_request(text: str) -> bool:
    value = text.lower()
    has_doc = any(key in value for key in ("docx", "word", "文件", "附件", "需求"))
    has_table = any(key in value for key in ("入表", "填入", "写入", "表格", "base", "多维表格"))
    return has_doc and has_table


def is_auto_base_status_request(text: str) -> bool:
    value = text.lower()
    has_feature = any(key in value for key in ("自动入表", "入表能力", "入表功能", "需求运营", "需求管理"))
    has_status = any(key in value for key in ("是否", "已经", "上线", "能不能", "可以", "状态"))
    return has_feature and has_status


def parse_requirement_management_command(
    event: NormalizedEvent, config: Settings
) -> RequirementManagementCommand | None:
    text = event.text.strip()
    value = text.lower()
    is_management = any(
        key in value
        for key in (
            "分配",
            "指派",
            "负责人",
            "状态",
            "优先级",
            "创建任务",
            "飞书任务",
            "维护需求",
            "管理需求",
        )
    ) and any(key in value for key in ("需求", "任务", "状态", "优先级", "负责人"))
    if not is_management:
        return None

    base = extract_base_context(text) or default_base_context(config, event)
    assignee = first_non_bot_mention(event, config.bot_name)
    status = extract_status_update(text)
    priority = extract_priority_update(text)
    create_task = any(key in value for key in ("创建任务", "飞书任务", "新建任务", "分配任务"))
    target = extract_requirement_target(text)
    if not target:
        return None
    return RequirementManagementCommand(
        target=target,
        status=status,
        priority=priority,
        assignee=assignee,
        create_task=create_task,
        base=base,
    )


def default_base_context(config: Settings, event: NormalizedEvent | None = None) -> BaseContext | None:
    base_token = config.auto_base_default_base_token
    table_id = config.auto_base_default_table_id
    if event is not None:
        policy = channel_policy_for_event(event, config)
        base_token = policy.default_base_token or base_token
        table_id = policy.default_table_id or table_id
    if not base_token:
        return None
    url = f"configured://base/{base_token}"
    return BaseContext(
        base_token=base_token,
        table_id=table_id or None,
        url=url,
    )


def first_non_bot_mention(event: NormalizedEvent, bot_name: str) -> MentionedUser | None:
    raw = event.raw or {}
    message = raw.get("event", raw).get("message", {}) if isinstance(raw, dict) else {}
    mentions = message.get("mentions") or []
    if not isinstance(mentions, list):
        return None
    for mention in mentions:
        if not isinstance(mention, dict):
            continue
        name = str(mention.get("name") or mention.get("user_name") or mention.get("key") or "")
        if bot_name and bot_name.lower() in name.lower():
            continue
        open_id = (
            mention.get("id")
            or mention.get("user_id")
            or mention.get("open_id")
            or mention.get("id", {})
        )
        if isinstance(open_id, dict):
            open_id = open_id.get("open_id") or open_id.get("user_id")
        if open_id:
            return MentionedUser(open_id=str(open_id), name=name)
    return None


def extract_status_update(text: str) -> str | None:
    patterns = (
        r"(?:状态|需求状态)\s*(?:改为|更新为|设为|设置为|变成|到)\s*([\u4e00-\u9fffA-Za-z0-9 _-]+)",
        r"(待评估|方案设计中|开发交付中|验收中|已完成|处理中|完成)",
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(1).strip(" 。；;，,")
    return None


def extract_priority_update(text: str) -> str | None:
    patterns = (
        r"(?:优先级|重要性)\s*(?:改为|更新为|设为|设置为|变成|到)?\s*([高高中低低Pp0-2 \-_]+|Need|Nice|Must|Should|Could)",
        r"\b(P[0-2]|Need|Nice|Must|Should|Could)\b",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return match.group(1).strip(" 。；;，,")
    return None


def extract_requirement_target(text: str) -> str:
    cleaned = BASE_URL_RE.sub("", text)
    cleaned = re.sub(r"<at[^>]*>.*?</at>", "", cleaned)
    cleaned = re.sub(r"@[^\s，,。；;]+", "", cleaned)
    code_match = re.search(r"[\u4e00-\u9fffA-Za-z]+-\d{3}", cleaned)
    if code_match:
        return code_match.group(0)
    quoted = re.search(r"[「《\"]([^」》\"]{2,80})[」》\"]", cleaned)
    if quoted:
        return quoted.group(1).strip()
    cleaned = re.sub(
        r"(请|帮我|把|将|需求|任务|分配|指派|负责人|状态|优先级|创建|飞书|新建|改为|更新为|设为|设置为|变成|到|不要新建表|不要新建 Base)",
        " ",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ：:，,。；;")
    for stop in (" 给 ", " 到 ", " 为 "):
        if stop in cleaned:
            cleaned = cleaned.split(stop, 1)[0].strip()
    return cleaned[:80]


def extract_base_context(text: str) -> BaseContext | None:
    match = BASE_URL_RE.search(text)
    if not match:
        return None
    url = match.group(0).rstrip("）。)，,，.;；、]")
    parsed = urlparse(url)
    parts = [part for part in parsed.path.split("/") if part]
    token = ""
    for idx, part in enumerate(parts):
        if part in {"app", "base"} and idx + 1 < len(parts):
            token = parts[idx + 1]
            break
    if not token or "{" in token or "}" in token or token == "...":
        return None
    query = parse_qs(parsed.query)
    table_id = (query.get("table") or [None])[0]
    if table_id and not table_id.startswith("tbl"):
        table_id = None
    return BaseContext(base_token=token, table_id=table_id, url=url)


def find_workflow_context(
    messages: list[dict[str, Any]], *, strict_explicit_target: bool = False
) -> WorkflowContext | None:
    base: BaseContext | None = None
    file: ChatFile | None = None
    for message in messages:
        content = str(message.get("content") or "")
        if (
            strict_explicit_target
            and base is None
            and UNSUPPORTED_APPLINK_RE.search(content)
            and is_auto_base_request(content)
            and extract_base_context(content) is None
        ):
            raise AutoBaseError(
                "你发的是飞书客户端消息跳转链接，不是多维表格真实地址。"
                "请复制多维表格的浏览器地址或分享地址，形如 "
                "https://...feishu.cn/app/{base_token} 或 https://...feishu.cn/base/{base_token}"
                "；我拿到真实 Base 链接后才能写入指定报表背后的数据表。"
            )
        base = base or extract_base_context(content)
        file = file or extract_file_ref(message)
        if base and file:
            return WorkflowContext(base=base, file=file)
    return None


def find_file_ref(messages: list[dict[str, Any]]) -> ChatFile | None:
    for message in messages:
        file = extract_file_ref(message)
        if file:
            return file
    return None


def extract_file_ref(message: dict[str, Any]) -> ChatFile | None:
    content = str(message.get("content") or "")
    match = FILE_REF_RE.search(content)
    if not match:
        return None
    name = match.group("name")
    if not name.lower().endswith(".docx"):
        return None
    return ChatFile(
        message_id=str(message.get("message_id") or ""),
        file_key=match.group("key"),
        name=name,
    )


def extract_docx_text(path: Path) -> str:
    try:
        with zipfile.ZipFile(path) as archive:
            xml = archive.read("word/document.xml")
    except (KeyError, zipfile.BadZipFile) as exc:
        raise AutoBaseError("文件不是可解析的 docx，或缺少 word/document.xml。") from exc

    root = ElementTree.fromstring(xml)
    namespace = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    lines: list[str] = []
    for paragraph in root.findall(".//w:p", namespace):
        text = "".join(node.text or "" for node in paragraph.findall(".//w:t", namespace)).strip()
        if text:
            lines.append(text)
    return "\n".join(lines)


def extract_docx_records(text: str, source_name: str) -> list[dict[str, str]]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    table_records = extract_requirement_table_records(lines)
    if table_records:
        enrich_records_with_supplements(table_records, lines)
        return table_records

    key_values: dict[str, str] = {}
    for line in lines:
        if "：" in line:
            key, value = line.split("：", 1)
        elif ":" in line:
            key, value = line.split(":", 1)
        else:
            continue
        key = normalize_doc_key(key)
        value = value.strip()
        if key and value:
            key_values[key] = value

    if key_values:
        record: dict[str, str] = {}
        for key, value in key_values.items():
            mapped = doc_key_alias(key)
            record[mapped] = value
        if "需求名称" not in record:
            record["需求名称"] = key_values.get("项目名称") or key_values.get("标题") or source_name
        return [record]

    body = "\n".join(lines[:80]).strip()
    title = next((line for line in lines if len(line) <= 80), source_name)
    return [{"需求名称": title or source_name, "需求描述": body}]


def extract_requirement_table_records(lines: list[str]) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    in_table = False
    idx = 0
    while idx < len(lines):
        line = lines[idx]
        if line.startswith("二、") and "需求清单" in line:
            in_table = True
            idx += 1
            continue
        if in_table and line.startswith("三、"):
            break
        if in_table and is_requirement_code(line):
            code = line
            idx += 1
            cells: list[str] = []
            while idx < len(lines):
                next_line = lines[idx]
                if next_line.startswith("三、") or is_requirement_code(next_line):
                    break
                cells.append(next_line)
                idx += 1
            record = requirement_cells_to_record(code, cells)
            if record:
                records.append(record)
            continue
        idx += 1
    return records


def is_requirement_code(text: str) -> bool:
    return bool(re.match(r"^[\u4e00-\u9fffA-Za-z]+-\d{3}$", text))


def requirement_cells_to_record(code: str, cells: list[str]) -> dict[str, str] | None:
    if len(cells) < 4:
        return None
    priority = cells[-1]
    expected = cells[-2]
    pain = cells[-3]
    desc = "\n".join(cells[:-3]).strip()
    if not desc:
        return None
    return {
        "需求编号": code,
        "需求名称": desc.split("：", 1)[0].strip()[:80] or code,
        "需求描述": desc,
        "当前做法 / 痛点": pain,
        "期望结果": expected,
        "优先级": priority,
    }


def enrich_records_with_supplements(records: list[dict[str, str]], lines: list[str]) -> None:
    by_code = {record["需求编号"]: record for record in records if record.get("需求编号")}
    idx = 0
    current: dict[str, str] | None = None
    while idx < len(lines):
        line = lines[idx]
        if line == "对应需求序号" and idx + 1 < len(lines):
            current = by_code.get(lines[idx + 1])
            idx += 2
            continue
        if current and idx + 1 < len(lines):
            key = line
            if key in {"使用场景", "输入材料 / 数据来源", "输出结果形式", "使用频率", "涉及系统 / 工具", "权限或保密要求", "补充说明"}:
                value = lines[idx + 1]
                if value not in {"对应需求序号"}:
                    current[key] = value
                idx += 2
                continue
        idx += 1


def build_rows_for_fields(
    records: list[dict[str, str]],
    fields: list[FieldInfo],
    *,
    source_file: str,
    source_message_id: str,
    now_text: str,
) -> dict[str, Any]:
    writable = [field for field in fields if field.field_type not in READONLY_FIELD_TYPES]
    if not writable:
        raise AutoBaseError("目标表没有可写字段。")

    selected_fields: list[FieldInfo] = []
    rows: list[list[Any]] = []
    field_names = [field.name for field in writable]

    for field in writable:
        if field_value_for_record(
            field,
            {},
            source_file=source_file,
            source_message_id=source_message_id,
            now_text=now_text,
            include_defaults=False,
        ) is not None or any(field.name in record for record in records):
            selected_fields.append(field)

    if not selected_fields:
        selected_fields = [writable[0]]

    for record in records[:200]:
        row = [
            field_value_for_record(
                selected_field,
                record,
                source_file=source_file,
                source_message_id=source_message_id,
                now_text=now_text,
                include_defaults=True,
            )
            for selected_field in selected_fields
        ]
        if any(value not in (None, "") for value in row):
            rows.append(row)

    return {"fields": [field.name for field in selected_fields], "rows": rows}


def field_value_for_record(
    field: FieldInfo | str,
    record: dict[str, str],
    *,
    source_file: str,
    source_message_id: str,
    now_text: str,
    include_defaults: bool,
) -> Any:
    field_name = field.name if isinstance(field, FieldInfo) else field
    if field_name in record:
        return coerce_field_value(field, record[field_name])
    normalized = normalize_doc_key(field_name)
    for key, value in record.items():
        if normalize_doc_key(key) == normalized:
            return coerce_field_value(field, value)
    if normalized in {"需求名称", "需求标题", "标题", "名称", "name", "title"}:
        return coerce_field_value(field, record.get("需求名称") or record.get("标题"))
    if normalized in {"需求描述", "需求内容", "描述", "内容", "说明", "背景", "description"}:
        return coerce_field_value(
            field, record.get("需求描述") or record.get("描述") or record.get("需求背景")
        )
    if normalized in {"需求详细描述", "需求详细描述可附文档", "详细描述", "详情"}:
        detail = requirement_detail_text(record)
        return coerce_field_value(field, detail)
    if normalized in {"优先级", "priority"}:
        return coerce_field_value(field, record.get("优先级"))
    if normalized in {"状态", "需求状态", "status"}:
        return coerce_field_value(field, "待处理" if include_defaults else "待处理")
    if normalized in {"来源", "来源文件", "文件名", "附件", "source"}:
        return source_file if include_defaults else source_file
    if normalized in {"来源消息", "消息id", "messageid", "message_id"}:
        return source_message_id if include_defaults else source_message_id
    if normalized in {"录入时间", "创建时间", "提交时间", "日期"}:
        return now_text if include_defaults else now_text
    return None


def requirement_detail_text(record: dict[str, str]) -> str:
    parts = []
    for key in (
        "需求编号",
        "需求描述",
        "当前做法 / 痛点",
        "期望结果",
        "使用场景",
        "输入材料 / 数据来源",
        "输出结果形式",
        "使用频率",
        "涉及系统 / 工具",
        "权限或保密要求",
        "补充说明",
    ):
        value = record.get(key)
        if value:
            parts.append(f"{key}：{value}")
    return "\n".join(parts)


def coerce_field_value(field: FieldInfo | str, value: Any) -> Any:
    if not isinstance(field, FieldInfo) or field.field_type != "select":
        return value
    if value in (None, ""):
        return value
    text = str(value).strip()
    if text in field.options:
        return text
    normalized = normalize_doc_key(text)
    priority_aliases = {
        "must": ("高p0", "p0"),
        "need": ("高p0", "p0"),
        "needed": ("高p0", "p0"),
        "高": ("高p0", "p0"),
        "高优先级": ("高p0", "p0"),
        "p0": ("高p0", "p0"),
        "should": ("中p1", "p1"),
        "want": ("中p1", "p1"),
        "中": ("中p1", "p1"),
        "中优先级": ("中p1", "p1"),
        "p1": ("中p1", "p1"),
        "could": ("低p2", "p2"),
        "nice": ("低p2", "p2"),
        "nicetohave": ("低p2", "p2"),
        "低": ("低p2", "p2"),
        "低优先级": ("低p2", "p2"),
        "p2": ("低p2", "p2"),
    }
    for candidate in field.options:
        candidate_norm = normalize_doc_key(candidate)
        if normalized and (candidate_norm.startswith(normalized) or normalized in candidate_norm):
            return candidate
    for marker in priority_aliases.get(normalized, ()):
        for candidate in field.options:
            if marker in normalize_doc_key(candidate):
                return candidate
    status_aliases = {
        "待处理": ("待评估", "方案设计中"),
        "处理中": ("开发交付中", "方案设计中"),
        "已完成": ("已完成",),
    }
    for alias in status_aliases.get(normalized, ()):
        if alias in field.options:
            return alias
    return text


def build_requirement_patch(
    command: RequirementManagementCommand, fields: list[FieldInfo]
) -> dict[str, Any]:
    patch: dict[str, Any] = {}
    if command.assignee:
        assignee_field = find_first_field(fields, ("需求负责人", "负责人", "Owner", "Assignee"))
        if assignee_field:
            patch[assignee_field.name] = [{"id": command.assignee.open_id}]
    if command.status:
        status_field = find_first_field(fields, ("需求状态", "状态", "Status"))
        if status_field:
            patch[status_field.name] = coerce_field_value(status_field, command.status)
    if command.priority:
        priority_field = find_first_field(fields, ("优先级", "重要性", "Priority"))
        if priority_field:
            patch[priority_field.name] = coerce_field_value(priority_field, command.priority)
    return patch


def enforce_requirement_policy(
    command: RequirementManagementCommand, patch: dict[str, Any], policy: ChannelPolicy
) -> None:
    if command.create_task and not policy.task_create_allowed:
        raise AutoBaseError("频道策略禁止 OS Tag 创建飞书任务。")
    if command.create_task and policy.task_create_requires_confirmation:
        raise AutoBaseError("频道策略要求创建任务前先人工确认。请明确回复：确认创建任务。")
    if policy.writable_fields:
        blocked = [field for field in patch if field not in policy.writable_fields]
        if blocked:
            allowed = "、".join(policy.writable_fields)
            blocked_text = "、".join(blocked)
            raise AutoBaseError(
                f"频道策略禁止写入字段：{blocked_text}。当前允许写入：{allowed}。"
            )


def field_patch_if_exists(fields: list[FieldInfo], field_name: str, value: Any) -> dict[str, Any]:
    field = find_field(fields, field_name)
    if not field:
        return {}
    return {field.name: coerce_field_value(field, value)}


def coerce_named_field_value(fields: list[FieldInfo], field_name: str, value: str) -> str:
    field = find_field(fields, field_name)
    return str(coerce_field_value(field or field_name, value))


def choose_search_fields(fields: list[FieldInfo]) -> list[str]:
    preferred = (
        "需求编号",
        "需求名称",
        "需求描述",
        "需求详细描述（可附文档）",
        "描述",
        "标题",
    )
    result: list[str] = []
    for name in preferred:
        field = find_field(fields, name)
        if field and field.field_type in {"text", "url", "phone"}:
            result.append(field.name)
    if result:
        return result
    return [field.name for field in fields if field.field_type == "text"][:3]


def find_field(fields: list[FieldInfo], name: str) -> FieldInfo | None:
    normalized = normalize_doc_key(name)
    for field in fields:
        if field.name == name or normalize_doc_key(field.name) == normalized:
            return field
    return None


def find_first_field(fields: list[FieldInfo], names: tuple[str, ...]) -> FieldInfo | None:
    for name in names:
        field = find_field(fields, name)
        if field:
            return field
    return None


def extract_matching_records(data: dict[str, Any]) -> list[MatchingRecord]:
    blob = data.get("data", {})
    shortcut_rows = blob.get("data") if isinstance(blob, dict) else None
    shortcut_fields = blob.get("fields") if isinstance(blob, dict) else None
    shortcut_record_ids = blob.get("record_id_list") if isinstance(blob, dict) else None
    if (
        isinstance(shortcut_rows, list)
        and isinstance(shortcut_fields, list)
        and isinstance(shortcut_record_ids, list)
    ):
        records: list[MatchingRecord] = []
        for idx, row in enumerate(shortcut_rows):
            if not isinstance(row, list) or idx >= len(shortcut_record_ids):
                continue
            fields = {
                str(field): row[field_idx]
                for field_idx, field in enumerate(shortcut_fields)
                if field_idx < len(row)
            }
            records.append(MatchingRecord(record_id=str(shortcut_record_ids[idx]), fields=fields))
        return records

    raw_records = blob.get("records") or blob.get("items") or []
    result: list[MatchingRecord] = []
    for item in raw_records:
        if not isinstance(item, dict):
            continue
        record_id = str(item.get("record_id") or item.get("id") or "")
        fields = item.get("fields") or item.get("record", {}).get("fields") or {}
        if record_id and isinstance(fields, dict):
            result.append(MatchingRecord(record_id=record_id, fields=fields))
    return result


def record_label(record: MatchingRecord) -> str:
    return record_title(record) or record.record_id


def record_title(record: MatchingRecord) -> str:
    for name in ("需求编号", "需求名称", "需求描述", "标题", "Name"):
        value = record.fields.get(name)
        text = cell_to_text(value)
        if text:
            return text[:80]
    return ""


def record_description(record: MatchingRecord) -> str:
    parts = []
    for name in ("需求编号", "需求名称", "需求描述", "需求详细描述（可附文档）", "优先级", "需求状态"):
        value = cell_to_text(record.fields.get(name))
        if value:
            parts.append(f"{name}: {value}")
    return "\n".join(parts) or record.record_id


def cell_to_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        return ", ".join(cell_to_text(item) for item in value if cell_to_text(item)).strip()
    if isinstance(value, dict):
        for key in ("text", "name", "title", "value", "id"):
            if value.get(key):
                return str(value[key]).strip()
    return str(value).strip()


def extract_task_url(data: dict[str, Any]) -> str | None:
    candidates = [data]
    if isinstance(data.get("task"), dict):
        candidates.append(data["task"])
    for item in candidates:
        for key in ("url", "app_link", "link"):
            value = item.get(key)
            if value:
                return str(value)
    return None


def friendly_lark_error(message: str) -> str:
    try:
        parsed = json.loads(message)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict):
        missing = parsed.get("missing_scopes") or parsed.get("permission_violations")
        if missing:
            scopes = ", ".join(str(scope) for scope in missing)
            return f"缺少飞书授权 scope：{scopes}。请完成授权后我会继续执行。"
        hint = parsed.get("hint")
        text = parsed.get("message") or parsed.get("error")
        if hint:
            return f"{text or '飞书接口返回错误'}；{hint}"
        if text:
            return str(text)
    if "base:record:read" in message:
        return "缺少飞书授权 scope：base:record:read。需求运营需要先读表定位记录。"
    return message


def field_info(item: dict[str, Any]) -> FieldInfo:
    options = item.get("options")
    option_names = ()
    if isinstance(options, list):
        option_names = tuple(str(option.get("name")) for option in options if option.get("name"))
    return FieldInfo(
        name=str(item.get("name") or item.get("field_name") or ""),
        field_id=item.get("field_id") or item.get("id"),
        field_type=str(item.get("type") or item.get("ui_type") or "").lower(),
        options=option_names,
    )


def choose_table(tables: list[TableInfo]) -> TableInfo:
    for table in tables:
        if "需求" in table.name:
            return table
    return tables[0]


def default_requirement_fields() -> list[dict[str, Any]]:
    return [
        {"type": "text", "name": "需求名称"},
        {"type": "text", "name": "需求编号"},
        {"type": "text", "name": "需求描述"},
        {"type": "text", "name": "当前做法 / 痛点"},
        {"type": "text", "name": "期望结果"},
        {
            "type": "select",
            "name": "状态",
            "options": [{"name": "待处理"}, {"name": "处理中"}, {"name": "已完成"}],
        },
        {"type": "text", "name": "优先级"},
        {"type": "text", "name": "来源文件"},
        {"type": "text", "name": "来源消息"},
        {"type": "datetime", "name": "录入时间"},
    ]


def extract_table_id(data: dict[str, Any]) -> str | None:
    blob = data.get("data", data)
    candidates = [
        blob.get("table_id") if isinstance(blob, dict) else None,
        blob.get("table", {}).get("table_id") if isinstance(blob, dict) else None,
        blob.get("base", {}).get("default_table_id") if isinstance(blob, dict) else None,
        blob.get("id") if isinstance(blob, dict) else None,
    ]
    return next((str(item) for item in candidates if item), None)


def extract_base_token(data: dict[str, Any]) -> str | None:
    blob = data.get("data", data)
    if not isinstance(blob, dict):
        return None
    candidates = [
        blob.get("base_token"),
        blob.get("app_token"),
        blob.get("base", {}).get("base_token"),
        blob.get("base", {}).get("app_token"),
    ]
    return next((str(item) for item in candidates if item), None)


def extract_base_name(data: dict[str, Any]) -> str | None:
    blob = data.get("data", data)
    if not isinstance(blob, dict):
        return None
    return blob.get("name") or blob.get("base", {}).get("name")


def extract_base_url(data: dict[str, Any]) -> str | None:
    blob = data.get("data", data)
    if not isinstance(blob, dict):
        return None
    return blob.get("url") or blob.get("base", {}).get("url")


def first_local_path(data: dict[str, Any]) -> Path | None:
    blob = data.get("data", data)
    if isinstance(blob, dict):
        for key in ("local_path", "path", "output"):
            if blob.get(key):
                return Path(str(blob[key]))
        resources = blob.get("resources")
        if isinstance(resources, list) and resources:
            local = resources[0].get("local_path")
            if local:
                return Path(str(local))
    return None


def safe_filename(name: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.\-\u4e00-\u9fff]+", "_", name).strip("._")
    return safe or next(tempfile._get_candidate_names())


def normalize_doc_key(key: str) -> str:
    return re.sub(r"[\s_：:（）()\[\]【】\-]+", "", key.strip()).lower()


def doc_key_alias(key: str) -> str:
    normalized = normalize_doc_key(key)
    aliases = {
        "项目名称": "需求名称",
        "需求标题": "需求名称",
        "标题": "需求名称",
        "名称": "需求名称",
        "需求背景": "需求描述",
        "背景": "需求描述",
        "需求内容": "需求描述",
        "内容": "需求描述",
        "描述": "需求描述",
    }
    for source, target in aliases.items():
        if normalize_doc_key(source) == normalized:
            return target
    return key
