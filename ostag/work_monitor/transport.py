"""Typed proactive-notification transport failures."""

from __future__ import annotations


class TransportError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int = 0,
        retry_after_seconds: float | None = None,
        transient: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds
        self.transient = transient
