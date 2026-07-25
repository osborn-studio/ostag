from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from ostag.core.event import NormalizedEvent, Platform


@runtime_checkable
class Adapter(Protocol):
    platform: Platform

    async def dry_run(self, event_path: Path, runner: Any) -> str:
        """Process a local event fixture through the same path used by live events."""

    async def start(self, runner: Any) -> None:
        """Start the live platform transport."""

    async def normalize_event(self, raw_event: dict[str, Any]) -> NormalizedEvent:
        """Convert a platform event payload into the core event contract."""

    async def reply(self, event: NormalizedEvent, text: str) -> None:
        """Send a reply through the platform transport."""

    async def send_message(
        self, chat_id: str, text: str, *, delivery_id: str, chat_type: str = "group"
    ) -> None:
        """Send a proactive message through the platform transport."""


@runtime_checkable
class StructuredCardAdapter(Protocol):
    async def send_card(
        self,
        destination_id: str,
        card: dict[str, Any],
        fallback_text: str,
        *,
        delivery_id: str,
    ) -> str | None:
        """Send a native structured card to a chat."""
