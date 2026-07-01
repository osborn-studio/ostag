from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ostag.config import Settings
from ostag.core.event import NormalizedEvent

MENTION_RE = re.compile(r"<@[^>]+>")


class SlackAdapter:
    platform = "slack"

    def __init__(self, config: Settings):
        self.config = config
        self._client: Any | None = None

    async def dry_run(self, event_path: Path, runner: Any) -> str:
        raw = json.loads(event_path.read_text(encoding="utf-8"))
        event = await self.normalize_event(raw)
        reply = await runner.handle_event(event)
        await self.reply(event, reply)
        return reply

    async def start(self, runner: Any) -> None:
        try:
            from slack_bolt import App
            from slack_bolt.adapter.socket_mode import SocketModeHandler
        except ImportError as exc:
            raise RuntimeError(
                "slack live mode requires `uv sync --extra slack` and valid Slack tokens."
            ) from exc

        if not self.config.slack_bot_token or not self.config.slack_app_token:
            raise RuntimeError("SLACK_BOT_TOKEN and SLACK_APP_TOKEN are required.")

        app = App(token=self.config.slack_bot_token)

        @app.event("app_mention")
        def handle_mention(event: dict[str, Any], client: Any, logger: Any) -> None:
            self._client = client
            normalized = _normalize_slack_event(event, self.config.bot_name)
            status = client.chat_postMessage(
                channel=normalized.chat_id,
                thread_ts=normalized.thread_id or normalized.message_id,
                text="OS Tag is working on this.",
            )
            try:
                reply = _run_async(runner.handle_event(normalized))
                if reply:
                    client.chat_update(
                        channel=normalized.chat_id,
                        ts=status["ts"],
                        text=reply,
                    )
            except Exception as exc:  # noqa: BLE001
                logger.exception("OS Tag Slack handler failed")
                client.chat_update(
                    channel=normalized.chat_id,
                    ts=status["ts"],
                    text=f"OS Tag failed: `{type(exc).__name__}: {exc}`",
                )

        SocketModeHandler(app, self.config.slack_app_token).start()

    async def normalize_event(self, raw_event: dict[str, Any]) -> NormalizedEvent:
        event = raw_event.get("event", raw_event)
        return _normalize_slack_event(event, self.config.bot_name)

    async def reply(self, event: NormalizedEvent, text: str) -> None:
        if not text:
            return
        if self.config.dry_run:
            print(text)
            return
        if self._client is None:
            raise RuntimeError("Slack client is not initialized.")
        self._client.chat_postMessage(
            channel=event.chat_id,
            thread_ts=event.thread_id or event.message_id,
            text=text,
        )

    async def send_message(self, chat_id: str, text: str) -> None:
        if not text:
            return
        if self.config.dry_run:
            print(text)
            return
        if self._client is None:
            raise RuntimeError("Slack client is not initialized.")
        self._client.chat_postMessage(channel=chat_id, text=text)


def _normalize_slack_event(event: dict[str, Any], bot_name: str) -> NormalizedEvent:
    text = event.get("text", "")
    cleaned_text = strip_mentions(text, bot_name)
    return NormalizedEvent(
        platform="slack",
        event_id=event.get("event_ts") or event.get("ts", ""),
        chat_id=event.get("channel", ""),
        message_id=event.get("ts", ""),
        sender_id=event.get("user") or event.get("bot_id", ""),
        text=cleaned_text,
        mention_bot=_mentions_bot(text, bot_name) or event.get("type") == "app_mention",
        thread_id=event.get("thread_ts") or event.get("ts"),
        raw=event,
    )


def strip_mentions(text: str, bot_name: str) -> str:
    stripped = MENTION_RE.sub("", text)
    stripped = re.sub(rf"@{re.escape(bot_name)}\b", "", stripped, flags=re.IGNORECASE).strip()
    return stripped or "Find the most relevant context for this thread and summarize it."


def _mentions_bot(text: str, bot_name: str) -> bool:
    return bool(MENTION_RE.search(text)) or f"@{bot_name}" in text


def _run_async(awaitable: Any) -> Any:
    import asyncio

    return asyncio.run(awaitable)
