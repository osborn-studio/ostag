"""Telegram Bot API transport and update normalization."""

from __future__ import annotations

import asyncio
import json
import urllib.request
from pathlib import Path
from typing import Any, Callable

from ostag.config import Settings
from ostag.core.event import NormalizedEvent


Transport = Callable[[str, dict[str, object]], dict[str, object]]


class TelegramBotClient:
    """Small injectable stdlib Bot API client; its token is never printable."""

    def __init__(self, token: str, *, transport: Transport | None = None, timeout: float = 10) -> None:
        if not token:
            raise ValueError("Telegram bot token is required")
        self._token = token
        self._timeout = timeout
        self._transport = transport

    def __repr__(self) -> str:
        return "TelegramBotClient(token=<redacted>)"

    def call(self, method: str, payload: dict[str, object]) -> dict[str, object]:
        if self._transport is not None:
            response = self._transport(method, payload)
        else:
            try:
                body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
                request = urllib.request.Request(
                    f"https://api.telegram.org/bot{self._token}/{method}", body,
                    {"Content-Type": "application/json"}, method="POST",
                )
                with urllib.request.urlopen(request, timeout=self._timeout) as result:  # noqa: S310
                    response = json.loads(result.read())
            except Exception:
                # urllib exceptions can contain the requested URL, which embeds the token.
                raise RuntimeError(f"Telegram Bot API {method} transport failed") from None
        if not isinstance(response, dict) or response.get("ok") is not True:
            raise RuntimeError(f"Telegram Bot API {method} failed")
        return response

    def send_message(
        self, chat_id: str, text: str, *, reply_markup: dict[str, object] | None = None
    ) -> dict[str, object]:
        payload: dict[str, object] = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        return self.call("sendMessage", payload)

    def answer_callback(self, callback_query_id: str, text: str) -> dict[str, object]:
        return self.call("answerCallbackQuery", {
            "callback_query_id": callback_query_id, "text": text[:200]
        })


class TelegramAdapter:
    platform = "telegram"

    def __init__(self, config: Settings, *, client: TelegramBotClient | None = None) -> None:
        self.config = config
        self.client = client or (
            TelegramBotClient(config.telegram_bot_token) if config.telegram_bot_token else None
        )

    async def normalize_event(self, raw_event: dict[str, Any]) -> NormalizedEvent:
        update_id = raw_event.get("update_id")
        if not isinstance(update_id, int) or isinstance(update_id, bool) or update_id < 0:
            raise ValueError("invalid Telegram update_id")
        callback = raw_event.get("callback_query")
        if isinstance(callback, dict):
            message = callback.get("message")
            if not isinstance(message, dict):
                raise ValueError("callback message is required")
            sender = callback.get("from")
            text = callback.get("data")
            callback_id = callback.get("id")
            if not isinstance(callback_id, str) or not 1 <= len(callback_id) <= 128:
                raise ValueError("invalid Telegram callback id")
            if not isinstance(text, str) or not 1 <= len(text.encode("utf-8")) <= 64:
                raise ValueError("invalid Telegram callback data")
            event_id = callback_id
        else:
            message = raw_event.get("message")
            if not isinstance(message, dict):
                raise ValueError("unsupported Telegram update")
            sender = message.get("from")
            text = message.get("text", "")
            event_id = str(update_id)
        chat = message.get("chat")
        if not isinstance(sender, dict) or not isinstance(chat, dict):
            raise ValueError("Telegram sender and chat are required")
        sender_id, chat_id = sender.get("id"), chat.get("id")
        chat_type = chat.get("type")
        if type(sender_id) is not int or type(chat_id) is not int:
            raise ValueError("invalid Telegram identity")
        if chat_type == "private":
            normalized_chat_type = "private"
        elif chat_type in {"group", "supergroup"}:
            normalized_chat_type = "group"
        else:
            raise ValueError("unsupported Telegram chat type")
        if not isinstance(text, str) or not event_id:
            raise ValueError("invalid Telegram message")
        return NormalizedEvent(
            platform="telegram", event_id=event_id, chat_id=str(chat_id),
            message_id=str(message.get("message_id", "")), sender_id=str(sender_id), text=text,
            mention_bot=True, chat_type=normalized_chat_type, destination_id=str(chat_id),
            raw=raw_event,
        )

    async def dry_run(self, event_path: Path, runner: Any) -> str:
        event = await self.normalize_event(json.loads(event_path.read_text(encoding="utf-8")))
        reply = await runner.handle_event(event)
        await self.reply(event, reply)
        return reply

    async def start(self, runner: Any) -> None:
        raise RuntimeError("Telegram live mode is served by the HTTPS webhook monitor server")

    async def reply(self, event: NormalizedEvent, text: str) -> None:
        await self.send_message(event.destination_id, text, delivery_id=event.event_id,
                                chat_type=event.chat_type)

    async def send_message(
        self, chat_id: str, text: str, *, delivery_id: str, chat_type: str = "group"
    ) -> None:
        if not text:
            return
        if self.config.dry_run:
            print(text)
            return
        if self.client is None:
            raise RuntimeError("Telegram Bot API client is not configured")
        await asyncio.to_thread(self.client.send_message, chat_id, text)
