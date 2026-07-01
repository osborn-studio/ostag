from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from ostag.config import Settings
from ostag.core.event import NormalizedEvent


AT_RE = re.compile(r"@\S+")


class DingTalkAdapter:
    platform = "dingtalk"

    def __init__(self, config: Settings):
        self.config = config

    async def dry_run(self, event_path: Path, runner: Any) -> str:
        raw = json.loads(event_path.read_text(encoding="utf-8"))
        event = await self.normalize_event(raw)
        reply = await runner.handle_event(event)
        await self.reply(event, reply)
        return reply

    async def start(self, runner: Any) -> None:
        if not self.config.dingtalk_client_id or not self.config.dingtalk_client_secret:
            raise RuntimeError("DINGTALK_CLIENT_ID and DINGTALK_CLIENT_SECRET are required.")
        try:
            import dingtalk_stream
        except ImportError as exc:
            raise RuntimeError(
                "dingtalk live mode requires `uv sync --extra dingtalk` and valid robot credentials."
            ) from exc

        adapter = self

        class OSChatbotHandler(dingtalk_stream.ChatbotHandler):
            async def process(self, callback):  # type: ignore[no-untyped-def]
                raw = getattr(callback, "data", callback)
                event = await adapter.normalize_event(raw)
                reply = await runner.handle_event(event)
                if reply:
                    message = dingtalk_stream.ChatbotMessage.from_dict(raw)
                    self.reply_text(reply, message)
                return dingtalk_stream.AckMessage.STATUS_OK, "OK"

        async def run_client() -> None:
            credential = dingtalk_stream.Credential(
                self.config.dingtalk_client_id,
                self.config.dingtalk_client_secret,
            )
            client = dingtalk_stream.DingTalkStreamClient(credential)
            client.register_callback_handler(
                dingtalk_stream.ChatbotMessage.TOPIC,
                OSChatbotHandler(),
            )
            await asyncio.to_thread(client.start_forever)

        await run_client()

    async def normalize_event(self, raw_event: dict[str, Any]) -> NormalizedEvent:
        data = raw_event.get("data", raw_event)
        text = _message_text(data)
        cleaned_text = strip_mentions(text, self.config.bot_name)
        mention_bot = _mentions_bot(data, text, self.config.bot_name)
        message_id = str(data.get("msgId") or data.get("msg_id") or data.get("messageId") or "")
        chat_id = str(
            data.get("conversationId")
            or data.get("conversation_id")
            or data.get("openConversationId")
            or ""
        )
        sender_id = str(
            data.get("senderStaffId")
            or data.get("senderId")
            or data.get("senderNick")
            or data.get("userId")
            or ""
        )
        return NormalizedEvent(
            platform="dingtalk",
            event_id=message_id or str(raw_event.get("eventId") or ""),
            chat_id=chat_id,
            message_id=message_id,
            sender_id=sender_id,
            text=cleaned_text,
            mention_bot=mention_bot,
            thread_id=str(data.get("conversationId") or ""),
            raw=raw_event,
        )

    async def reply(self, event: NormalizedEvent, text: str) -> None:
        if not text:
            return
        if self.config.dry_run:
            print(text)
            return
        session_webhook = ""
        if isinstance(event.raw, dict):
            data = event.raw.get("data", event.raw)
            if isinstance(data, dict):
                session_webhook = str(data.get("sessionWebhook") or "")
        if session_webhook:
            await self._post_json(session_webhook, _text_payload(text))
            return
        await self.send_message(event.chat_id, text)

    async def send_message(self, chat_id: str, text: str) -> None:
        if not text:
            return
        if self.config.dry_run:
            print(text)
            return
        if not self.config.dingtalk_robot_webhook:
            raise RuntimeError("DINGTALK_ROBOT_WEBHOOK is required for proactive DingTalk messages.")
        await self._post_json(
            signed_webhook_url(
                self.config.dingtalk_robot_webhook,
                self.config.dingtalk_robot_secret,
            ),
            _text_payload(text),
        )

    async def _post_json(self, url: str, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode()
        req = urllib.request.Request(
            url,
            data=data,
            method="POST",
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        await asyncio.to_thread(_urlopen_json, req)


def strip_mentions(text: str, bot_name: str) -> str:
    cleaned = re.sub(rf"@{re.escape(bot_name)}\b", "", text, flags=re.IGNORECASE)
    cleaned = AT_RE.sub("", cleaned).strip()
    return cleaned or "Find the most relevant context for this thread and summarize it."


def signed_webhook_url(webhook: str, secret: str, timestamp_ms: int | None = None) -> str:
    if not secret:
        return webhook
    timestamp = str(timestamp_ms or int(time.time() * 1000))
    string_to_sign = f"{timestamp}\n{secret}".encode()
    digest = hmac.new(secret.encode(), string_to_sign, hashlib.sha256).digest()
    sign = urllib.parse.quote_plus(base64.b64encode(digest).decode())
    separator = "&" if "?" in webhook else "?"
    return f"{webhook}{separator}timestamp={timestamp}&sign={sign}"


def _message_text(data: dict[str, Any]) -> str:
    text = data.get("text")
    if isinstance(text, dict):
        return str(text.get("content") or text.get("text") or "").strip()
    if isinstance(text, str):
        return text.strip()
    content = data.get("content")
    if isinstance(content, str):
        return content.strip()
    return ""


def _mentions_bot(data: dict[str, Any], text: str, bot_name: str) -> bool:
    if data.get("isInAtList") is True or data.get("isAtAll") is True:
        return True
    at_users = data.get("atUsers")
    if isinstance(at_users, list) and at_users:
        return True
    return f"@{bot_name}" in text


def _text_payload(text: str) -> dict[str, Any]:
    return {"msgtype": "text", "text": {"content": text}}


def _urlopen_json(req: urllib.request.Request) -> dict[str, Any]:
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
        raw = resp.read().decode("utf-8", errors="replace")
    if not raw:
        return {}
    return json.loads(raw)
