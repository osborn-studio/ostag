from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import re
import shlex
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
        _debug_raw_event(data)
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
            chat_type="private" if str(data.get("conversationType")) == "1" else "group",
            destination_id=chat_id,
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
        text = dingtalk_text(text)
        if self.config.dry_run:
            print(text)
            return
        if not self.config.dingtalk_robot_webhook:
            await self._send_by_dws_bot(chat_id, text)
            return
        await self._post_json(
            signed_webhook_url(
                self.config.dingtalk_robot_webhook,
                self.config.dingtalk_robot_secret,
            ),
            _text_payload(text),
        )

    async def _send_by_dws_bot(self, chat_id: str, text: str) -> None:
        argv = shlex.split(self.config.dws_cli_bin)
        if not argv:
            raise RuntimeError("DWS_CLI_BIN is empty.")
        if not self.config.dingtalk_client_id:
            raise RuntimeError("DINGTALK_CLIENT_ID is required for DingTalk bot sends.")
        proc = await asyncio.create_subprocess_exec(
            *argv,
            "chat",
            "message",
            "send-by-bot",
            "--robot-code",
            self.config.dingtalk_client_id,
            "--group",
            chat_id,
            "--title",
            "OS Tag 主动巡检",
            "--text",
            text,
            "--format",
            "json",
            cwd=str(Path(self.config.auto_base_workdir)),
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
    for key in ("text", "content", "msgContent", "messageContent"):
        text = _extract_text(data.get(key))
        if text:
            return text
    return ""


def _mentions_bot(data: dict[str, Any], text: str, bot_name: str) -> bool:
    if data.get("isInAtList") is True or data.get("isAtAll") is True:
        return True
    at_users = data.get("atUsers")
    if isinstance(at_users, list) and at_users:
        return True
    if _is_direct_chat(data):
        return True
    return f"@{bot_name}" in text


def _extract_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        parsed = _parse_json_string(value)
        if parsed is not None:
            return _extract_text(parsed)
        return value.strip()
    if isinstance(value, dict):
        for key in ("content", "text", "richText", "title"):
            text = _extract_text(value.get(key))
            if text:
                return text
    if isinstance(value, list):
        return " ".join(text for item in value if (text := _extract_text(item))).strip()
    return ""


def _parse_json_string(value: str) -> Any:
    stripped = value.strip()
    if not stripped or stripped[0] not in "[{":
        return None
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return None


def _is_direct_chat(data: dict[str, Any]) -> bool:
    value = (
        str(
            data.get("conversationType")
            or data.get("conversation_type")
            or data.get("chatType")
            or data.get("chat_type")
            or ""
        )
        .strip()
        .lower()
    )
    return value in {"1", "single", "single_chat", "private", "private_chat", "p2p"}


def _text_payload(text: str) -> dict[str, Any]:
    return {"msgtype": "text", "text": {"content": text}}


def dingtalk_text(text: str) -> str:
    return re.sub(r'<at\s+user_id="[^"]+">([^<]+)</at>', r"@\1", text)


def _urlopen_json(req: urllib.request.Request) -> dict[str, Any]:
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
        raw = resp.read().decode("utf-8", errors="replace")
    if not raw:
        return {}
    return json.loads(raw)


def _debug_raw_event(data: dict[str, Any]) -> None:
    if os.getenv("OSTAG_DINGTALK_DEBUG_RAW", "").lower() not in {"1", "true", "yes", "on"}:
        return
    Path("logs/dingtalk-raw-debug.jsonl").parent.mkdir(parents=True, exist_ok=True)
    with Path("logs/dingtalk-raw-debug.jsonl").open("a", encoding="utf-8") as fp:
        fp.write(json.dumps(data, ensure_ascii=False, default=str) + "\n")
