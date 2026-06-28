from __future__ import annotations

from typing import Protocol


class Backend(Protocol):
    async def run(self, prompt: str) -> str:
        """Return a reply for a fully prepared OS Tag prompt."""
