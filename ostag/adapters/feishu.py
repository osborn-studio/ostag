from __future__ import annotations

import asyncio
import json
import re
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from ostag.config import Settings
from ostag.core.event import NormalizedEvent

AT_TAG_RE = re.compile(r"<at[^>]*>.*?</at>")


class FeishuAdapter:
    platform = "feishu"

    def __init__(self, config: Settings):
        self.config = config

    async def dry_run(self, event_path: Path, runner: Any) -> str:
        raw = json.loads(event_path.read_text(encoding="utf-8"))
        event = await self.normalize_event(raw)
        reply = await runner.handle_event(event)
        await self.reply(event, reply)
        return reply

    @property
    def app_id(self) -> str:
        return self.config.feishu_app_id

    @property
    def app_secret(self) -> str:
        return self.config.feishu_app_secret

    @property
    def verification_token(self) -> str:
        return self.config.feishu_verification_token

    @property
    def api_base_url(self) -> str:
        return self.config.feishu_api_base_url

    async def start(self, runner: Any) -> None:
        if not self.app_id or not self.app_secret:
            raise RuntimeError(f"{self.platform.upper()}_APP_ID and APP_SECRET are required.")

        await asyncio.to_thread(self._start_blocking, runner)

    def _start_blocking(self, runner: Any) -> None:
        try:
            import lark_oapi as lark
        except ImportError as exc:
            raise RuntimeError(
                f"{self.platform} live mode requires `uv sync --extra feishu` and valid app credentials."
            ) from exc

        event_handler = (
            lark.EventDispatcherHandler.builder("", self.verification_token)
            .register_p2_im_message_receive_v1(
                lambda data: self._run_live_event_handler(data, runner)
            )
            .build()
        )
        cli = (
            lark.ws.Client(
                self.app_id,
                self.app_secret,
                event_handler=event_handler,
                log_level=lark.LogLevel.INFO,
                domain=self.api_base_url,
            )
        )
        cli.start()

    def _run_live_event_handler(self, data: Any, runner: Any) -> None:
        thread = threading.Thread(
            target=lambda: asyncio.run(self._handle_live_event(data, runner)),
            name=f"ostag-{self.platform}-event",
            daemon=True,
        )
        thread.start()

    async def _handle_live_event(self, data: Any, runner: Any) -> None:
        raw = data.raw if hasattr(data, "raw") else data
        if isinstance(raw, str):
            raw = json.loads(raw)
        elif not isinstance(raw, dict):
            raw = _to_plain_data(raw)
        event = await self.normalize_event(raw)
        reply = await runner.handle_event(event)
        if reply:
            await self.reply(event, reply)

    async def normalize_event(self, raw_event: dict[str, Any]) -> NormalizedEvent:
        event = raw_event.get("event", raw_event)
        header = raw_event.get("header", {})
        message = event.get("message", {})
        sender = event.get("sender", {})
        content = _parse_content(message.get("content"))
        text = content.get("text") or message.get("text") or event.get("text") or ""
        mentions = message.get("mentions") or content.get("mentions") or []
        mention_bot = bool(mentions) or _mentions_bot(text, self.config.bot_name)
        cleaned_text = strip_mentions(text, self.config.bot_name)

        return NormalizedEvent(
            platform="feishu",
            event_id=header.get("event_id") or event.get("event_id") or message.get("message_id", ""),
            chat_id=message.get("chat_id") or event.get("chat_id", ""),
            message_id=message.get("message_id") or event.get("message_id", ""),
            sender_id=(
                sender.get("sender_id", {}).get("open_id")
                or sender.get("sender_id", {}).get("user_id")
                or sender.get("open_id")
                or event.get("sender_id", "")
            ),
            text=cleaned_text,
            mention_bot=mention_bot,
            thread_id=message.get("thread_id") or message.get("root_id"),
            raw=raw_event,
        )

    async def reply(self, event: NormalizedEvent, text: str) -> None:
        if not text:
            return
        if self.config.dry_run:
            print(text)
            return
        token = await self._tenant_access_token()
        payload = json.dumps(
            {
                "msg_type": "text",
                "content": json.dumps({"text": text}, ensure_ascii=False),
            }
        ).encode()
        url = f"{self.api_base_url}/open-apis/im/v1/messages/{event.message_id}/reply"
        req = urllib.request.Request(
            url,
            data=payload,
            method="POST",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=utf-8",
            },
        )
        await asyncio.to_thread(_urlopen_json, req)

    async def _tenant_access_token(self) -> str:
        body = json.dumps(
            {
                "app_id": self.app_id,
                "app_secret": self.app_secret,
            }
        ).encode()
        req = urllib.request.Request(
            f"{self.api_base_url}/open-apis/auth/v3/tenant_access_token/internal",
            data=body,
            method="POST",
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        data = await asyncio.to_thread(_urlopen_json, req)
        token = data.get("tenant_access_token")
        if not token:
            raise RuntimeError(f"Failed to get tenant_access_token: {data}")
        return token


def _parse_content(content: Any) -> dict[str, Any]:
    if isinstance(content, dict):
        return content
    if isinstance(content, str) and content:
        try:
            parsed = json.loads(content)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {"text": content}
    return {}


def _to_plain_data(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        return [_to_plain_data(item) for item in value]
    if isinstance(value, tuple):
        return [_to_plain_data(item) for item in value]
    if isinstance(value, dict):
        return {key: _to_plain_data(item) for key, item in value.items() if item is not None}
    if hasattr(value, "__dict__"):
        return {
            key: _to_plain_data(item)
            for key, item in vars(value).items()
            if not key.startswith("_") and item is not None
        }
    return value


def _mentions_bot(text: str, bot_name: str) -> bool:
    return f"@{bot_name}" in text or "<at " in text


def strip_mentions(text: str, bot_name: str) -> str:
    stripped = AT_TAG_RE.sub("", text)
    stripped = re.sub(rf"@{re.escape(bot_name)}\b", "", stripped, flags=re.IGNORECASE).strip()
    return stripped or "请根据当前上下文给出下一步建议。"


def _urlopen_json(req: urllib.request.Request) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(req, timeout=20) as response:  # noqa: S310
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Feishu API HTTP {exc.code}: {body}") from exc
