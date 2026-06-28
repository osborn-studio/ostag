from __future__ import annotations

from ostag.adapters.feishu import FeishuAdapter
from ostag.core.event import NormalizedEvent


class LarkAdapter(FeishuAdapter):
    platform = "lark"

    @property
    def app_id(self) -> str:
        return self.config.lark_app_id

    @property
    def app_secret(self) -> str:
        return self.config.lark_app_secret

    @property
    def verification_token(self) -> str:
        return self.config.lark_verification_token

    @property
    def api_base_url(self) -> str:
        return self.config.lark_api_base_url

    async def normalize_event(self, raw_event: dict) -> NormalizedEvent:
        event = await super().normalize_event(raw_event)
        return NormalizedEvent(
            platform="lark",
            event_id=event.event_id,
            chat_id=event.chat_id,
            message_id=event.message_id,
            sender_id=event.sender_id,
            text=event.text,
            mention_bot=event.mention_bot,
            thread_id=event.thread_id,
            raw=event.raw,
        )
