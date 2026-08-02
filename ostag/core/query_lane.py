"""Bounded, read-only execution lane for status and reporting queries."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final

from ostag.core.permissions import LOCAL_ADMIN_ACTOR


QueryHandler = Callable[["QueryRequest"], Awaitable[Mapping[str, Any]]]

_PAYLOAD_FIELDS: Final[dict[str, frozenset[str]]] = {
    "status": frozenset({"run_id"}),
    "audit": frozenset({"since", "until", "platform", "chat_id"}),
    "weekly_review": frozenset({"scope", "limit"}),
    "backlog": frozenset({"team", "project", "limit"}),
}
_REQUIRED_FIELDS: Final[dict[str, frozenset[str]]] = {
    "status": frozenset({"run_id"}),
    "audit": frozenset(),
    "weekly_review": frozenset(),
    "backlog": frozenset(),
}


class QueryLaneError(RuntimeError):
    def __init__(self, code: str, message: str, recovery_hint: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.recovery_hint = recovery_hint

    def to_dict(self) -> dict[str, str]:
        result = {"code": self.code, "message": self.message}
        if self.recovery_hint:
            result["recovery_hint"] = self.recovery_hint
        return result


class RetryableQueryError(RuntimeError):
    """A temporary provider failure that the bounded lane may retry."""


def _non_empty_string(value: object, field: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise QueryLaneError("invalid_query", f"{field} must be a non-empty string")


@dataclass(frozen=True)
class QueryRequest:
    query_id: str
    kind: str
    platform: str
    chat_id: str
    actor_id: str
    payload: Mapping[str, Any]

    def __post_init__(self) -> None:
        for field in ("query_id", "kind", "platform", "chat_id", "actor_id"):
            _non_empty_string(getattr(self, field), field)
        if self.kind not in _PAYLOAD_FIELDS:
            raise QueryLaneError("unsupported_query_kind", f"Unsupported query kind: {self.kind}")
        if not isinstance(self.payload, Mapping):
            raise QueryLaneError("invalid_query_payload", "query payload must be an object")


@dataclass(frozen=True)
class QueryPolicy:
    enabled: bool = False
    authorized_channels: frozenset[str] = frozenset()
    max_concurrency: int = 4
    timeout_seconds: float = 10
    retry_count: int = 1
    max_result_bytes: int = 65_536

    def __post_init__(self) -> None:
        if self.max_concurrency < 1:
            raise ValueError("query max concurrency must be positive")
        if self.timeout_seconds <= 0:
            raise ValueError("query timeout must be positive")
        if self.retry_count < 0:
            raise ValueError("query retry count cannot be negative")
        if self.max_result_bytes < 1:
            raise ValueError("query result byte limit must be positive")
        if any(not item or ":" not in item for item in self.authorized_channels):
            raise ValueError("authorized query channels must use platform:chat_id")


@dataclass(frozen=True)
class QueryResponse:
    query_id: str
    kind: str
    attempts: int
    result: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "query_id": self.query_id,
            "kind": self.kind,
            "attempts": self.attempts,
            "result": self.result,
        }


class ReadOnlyQueryLane:
    def __init__(self, policy: QueryPolicy, handlers: Mapping[str, QueryHandler]) -> None:
        self.policy = policy
        self.handlers = dict(handlers)
        self._semaphore = asyncio.Semaphore(policy.max_concurrency)

    async def execute(self, request: QueryRequest) -> QueryResponse:
        self._authorize(request)
        self._validate_payload(request)
        handler = self.handlers.get(request.kind)
        if handler is None:
            raise QueryLaneError(
                "query_handler_unavailable",
                f"No read-only handler is configured for {request.kind}",
            )

        async with self._semaphore:
            result, attempts = await self._run_bounded(handler, request)
        normalized = self._validate_result(result)
        return QueryResponse(request.query_id, request.kind, attempts, normalized)

    def _authorize(self, request: QueryRequest) -> None:
        if not self.policy.enabled:
            raise QueryLaneError(
                "query_lane_disabled",
                "The read-only query lane is disabled",
                "Set OSTAG_QUERY_LANE_ENABLED=true after configuring authorized channels.",
            )
        if request.actor_id == LOCAL_ADMIN_ACTOR:
            return
        channel_key = f"{request.platform}:{request.chat_id}"
        if channel_key not in self.policy.authorized_channels:
            raise QueryLaneError(
                "query_channel_denied",
                "This channel is not authorized for read-only queries",
            )

    @staticmethod
    def _validate_payload(request: QueryRequest) -> None:
        keys = set(request.payload)
        if not all(isinstance(key, str) for key in keys):
            raise QueryLaneError("invalid_query_payload", "query payload keys must be strings")
        allowed = _PAYLOAD_FIELDS[request.kind]
        required = _REQUIRED_FIELDS[request.kind]
        if not required.issubset(keys) or not keys.issubset(allowed):
            raise QueryLaneError(
                "invalid_query_payload",
                f"Invalid fields for {request.kind} query",
            )
        for key, value in request.payload.items():
            if key == "limit":
                if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 500:
                    raise QueryLaneError(
                        "invalid_query_payload", "query limit must be an integer in 1..500"
                    )
            elif value is not None and (not isinstance(value, str) or not value.strip()):
                raise QueryLaneError(
                    "invalid_query_payload", f"query field {key} must be a non-empty string"
                )

    async def _run_bounded(
        self, handler: QueryHandler, request: QueryRequest
    ) -> tuple[Mapping[str, Any], int]:
        attempts = 0
        while True:
            attempts += 1
            try:
                result = await asyncio.wait_for(
                    handler(request), timeout=self.policy.timeout_seconds
                )
                return result, attempts
            except TimeoutError as exc:
                raise QueryLaneError(
                    "query_timeout", "The read-only query exceeded its time limit"
                ) from exc
            except RetryableQueryError as exc:
                if attempts > self.policy.retry_count:
                    raise QueryLaneError(
                        "query_retry_exhausted",
                        "The read-only provider remained unavailable after bounded retries",
                    ) from exc
            except QueryLaneError:
                raise
            except Exception as exc:
                raise QueryLaneError(
                    "query_failed", "The read-only query failed without changing remote state"
                ) from exc

    def _validate_result(self, result: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(result, Mapping) or not all(isinstance(key, str) for key in result):
            raise QueryLaneError("invalid_query_result", "query result must be an object")
        normalized = dict(result)
        try:
            encoded = json.dumps(
                normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise QueryLaneError(
                "invalid_query_result", "query result must be JSON serializable"
            ) from exc
        if len(encoded) > self.policy.max_result_bytes:
            raise QueryLaneError(
                "query_result_too_large", "The read-only query result exceeded its byte limit"
            )
        return normalized
