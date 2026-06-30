from __future__ import annotations

from typing import Protocol


class BackendError(RuntimeError):
    """Internal backend failure detail that should not be sent to chat verbatim."""


class Backend(Protocol):
    async def run(self, prompt: str) -> str:
        """Return a reply for a fully prepared OS Tag prompt."""
