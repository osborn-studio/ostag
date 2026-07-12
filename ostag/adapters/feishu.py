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
from ostag.work_monitor.transport import TransportError

AT_TAG_RE = re.compile(r"<at[^>]*>.*?</at>")


class FeishuAdapter:
    platform = "feishu"

    def __init__(self, config: Settings, *, runner: Any | None = None):
        self.config = config
        self.runner = runner
        self._loop: asyncio.AbstractEventLoop | None = None
        self._client: Any | None = None
        self._inflight: set[Any] = set()
        self._ws_stop = threading.Event()
        self._sdk_loop: asyncio.AbstractEventLoop | None = None
        self._bridge_ready = threading.Event()
        self._stop_requested = threading.Event()

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

        self.runner = runner
        self._loop = asyncio.get_running_loop()
        self._bridge_ready.clear()
        self._stop_requested.clear()
        await asyncio.to_thread(self._start_blocking, runner)

    async def run(self, stop: asyncio.Event) -> None:
        """Run one supervised WS bridge with one event loop and no per-event threads."""
        if self.runner is None:
            raise RuntimeError("FeishuAdapter.run requires a bound runner")
        if not self.app_id or not self.app_secret:
            raise RuntimeError(f"{self.platform.upper()}_APP_ID and APP_SECRET are required.")
        self._loop = asyncio.get_running_loop()
        failure: list[BaseException] = []

        def bridge() -> None:
            try:
                self._start_blocking(self.runner)
            except BaseException as exc:
                failure.append(exc)

        thread = threading.Thread(target=bridge, name="ostag-feishu-ws-bridge", daemon=False)
        thread.start()
        while thread.is_alive() and not self._bridge_ready.is_set() and not stop.is_set():
            await asyncio.sleep(0.01)
        while thread.is_alive() and not stop.is_set():
            await asyncio.sleep(0.05)
        if stop.is_set():
            await self.stop()
        await asyncio.to_thread(thread.join, 5)
        if thread.is_alive():
            raise RuntimeError("Feishu WebSocket bridge did not stop")
        if failure and not stop.is_set():
            raise failure[0]

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
        log_level = getattr(lark.LogLevel, "ERROR", None)
        if log_level is None:
            raise RuntimeError(
                "Feishu SDK must provide ERROR log level; refusing ticket-bearing logs"
            )
        cli = lark.ws.Client(
            self.app_id,
            self.app_secret,
            event_handler=event_handler,
            # INFO logs include the short-lived WebSocket endpoint ticket.
            log_level=log_level,
            domain=self.api_base_url,
        )
        self._client = cli
        self._ws_stop.clear()
        try:
            from lark_oapi.ws.client import loop as sdk_loop
        except ImportError:
            sdk_loop = None
        self._sdk_loop = sdk_loop
        self._bridge_ready.set()
        if self._stop_requested.is_set():
            return
        cli.start()

    def _run_live_event_handler(self, data: Any, runner: Any) -> None:
        if self._loop is None or self._loop.is_closed():
            raise RuntimeError("Feishu event loop is not running")
        future = asyncio.run_coroutine_threadsafe(self._handle_live_event(data, runner), self._loop)
        self._inflight.add(future)
        future.add_done_callback(self._inflight.discard)

    async def stop(self) -> None:
        self._ws_stop.set()
        self._stop_requested.set()
        client = self._client
        if client is not None:
            closer = getattr(client, "stop", None) or getattr(client, "close", None)
            if callable(closer):
                result = closer()
                if asyncio.iscoroutine(result):
                    await result
            elif self._sdk_loop is not None and self._sdk_loop.is_running():
                disconnect = getattr(client, "_disconnect", None)

                async def shutdown_sdk() -> None:
                    try:
                        if callable(disconnect):
                            await disconnect()
                    finally:
                        self._sdk_loop.stop()

                self._sdk_loop.call_soon_threadsafe(
                    lambda: self._sdk_loop.create_task(shutdown_sdk())
                )
        if self._inflight:
            await asyncio.gather(
                *(asyncio.wrap_future(item) for item in tuple(self._inflight)),
                return_exceptions=True,
            )

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
        text = _content_text(content) or message.get("text") or event.get("text") or ""
        mentions = message.get("mentions") or content.get("mentions") or []
        mention_bot = bool(mentions) or _mentions_bot(text, self.config.bot_name)
        cleaned_text = strip_mentions(text, self.config.bot_name)
        chat_id = message.get("chat_id") or event.get("chat_id", "")
        raw_chat_type = str(message.get("chat_type") or event.get("chat_type") or "")
        chat_type = "private" if raw_chat_type in {"p2p", "private"} else "group"

        return NormalizedEvent(
            platform="feishu",
            event_id=header.get("event_id")
            or event.get("event_id")
            or message.get("message_id", ""),
            chat_id=chat_id,
            message_id=message.get("message_id") or event.get("message_id", ""),
            sender_id=(
                sender.get("sender_id", {}).get("open_id")
                or sender.get("sender_id", {}).get("user_id")
                or sender.get("open_id")
                or event.get("sender_id", "")
            ),
            text=cleaned_text,
            mention_bot=mention_bot,
            chat_type=chat_type,
            destination_id=chat_id,
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
        response = await asyncio.to_thread(_urlopen_json, req)
        if response.get("code", 0) != 0:
            code = int(response.get("code", -1))
            raise TransportError(
                f"Feishu rejected message: code={code}",
                status_code=400,
                transient=code in {99991663, 99991664},
            )

    async def send_message(
        self, chat_id: str, text: str, *, delivery_id: str, chat_type: str = "group"
    ) -> None:
        if not delivery_id:
            raise ValueError("stable delivery_id is required for proactive messages")
        if not text:
            return
        if self.config.dry_run:
            print(text)
            return
        token = await self._tenant_access_token()
        receive_id_type = "open_id" if chat_type == "private" else "chat_id"
        payload = json.dumps(
            {
                "receive_id": chat_id,
                "msg_type": "text",
                "content": json.dumps({"text": text}, ensure_ascii=False),
            }
        ).encode()
        url = (
            f"{self.api_base_url}/open-apis/im/v1/messages?receive_id_type={receive_id_type}"
            f"&uuid={delivery_id}"
        )
        req = urllib.request.Request(
            url,
            data=payload,
            method="POST",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=utf-8",
            },
        )
        response = await asyncio.to_thread(_urlopen_json, req)
        if response.get("code", 0) != 0:
            code = int(response.get("code", -1))
            raise TransportError(
                f"Feishu rejected message: code={code}",
                status_code=400,
                transient=code in {99991663, 99991664},
            )

    async def send_card(
        self,
        destination_id: str,
        card: dict[str, Any],
        fallback_text: str,
        *,
        chat_type: str = "group",
        delivery_id: str,
    ) -> None:
        """Proactively deliver an interactive card, falling back to plain text."""
        if self.config.dry_run:
            print(fallback_text)
            return
        receive_id_type = "open_id" if chat_type == "private" else "chat_id"
        token = await self._tenant_access_token()
        payload = json.dumps(
            {
                "receive_id": destination_id,
                "msg_type": "interactive",
                "content": json.dumps(card, ensure_ascii=False),
            }
        ).encode()
        if not delivery_id:
            raise ValueError("stable delivery_id is required for proactive cards")
        req = urllib.request.Request(
            f"{self.api_base_url}/open-apis/im/v1/messages?receive_id_type={receive_id_type}"
            f"&uuid={delivery_id}",
            data=payload,
            method="POST",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=utf-8",
            },
        )
        try:
            response = await asyncio.to_thread(_urlopen_json, req)
            if response.get("code", 0) != 0:
                raise RuntimeError("Feishu rejected interactive card")
        except (RuntimeError, TimeoutError):
            await self.send_message(
                destination_id, fallback_text, chat_type=chat_type, delivery_id=delivery_id
            )

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


def _content_text(content: dict[str, Any]) -> str:
    if content.get("text"):
        return str(content["text"])
    post_content = content.get("content")
    if isinstance(post_content, list):
        return _post_nodes_text(post_content)
    return ""


def _post_nodes_text(nodes: list[Any]) -> str:
    parts: list[str] = []
    for node in nodes:
        if isinstance(node, list):
            parts.append(_post_nodes_text(node))
        elif isinstance(node, dict):
            tag = node.get("tag")
            if tag == "a":
                parts.append(str(node.get("href") or node.get("text") or ""))
            elif tag == "at":
                name = node.get("user_name") or node.get("name") or ""
                parts.append(f"@{name}" if name else "<at>")
            else:
                parts.append(str(node.get("text") or node.get("href") or ""))
    return "".join(parts).strip()


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
    return f"@{bot_name}" in text or "<at " in text or text.lstrip().startswith("@")


def strip_mentions(text: str, bot_name: str) -> str:
    stripped = AT_TAG_RE.sub("", text)
    stripped = re.sub(rf"@{re.escape(bot_name)}\b", "", stripped, flags=re.IGNORECASE).strip()
    stripped = re.sub(r"^@[A-Za-z0-9_. -]+\s+", "", stripped).strip()
    return stripped or "请根据当前上下文给出下一步建议。"


def _urlopen_json(req: urllib.request.Request) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(req, timeout=20) as response:  # noqa: S310
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        retry_after = exc.headers.get("Retry-After") if exc.headers else None
        raise TransportError(
            f"Feishu API HTTP {exc.code}",
            status_code=exc.code,
            retry_after_seconds=float(retry_after) if retry_after else None,
            transient=exc.code == 429 or exc.code >= 500,
        ) from None
