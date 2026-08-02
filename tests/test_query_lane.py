from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from ostag.core.query_lane import (
    QueryLaneError,
    QueryPolicy,
    QueryRequest,
    ReadOnlyQueryLane,
    RetryableQueryError,
)
from ostag.main import main_async


def _request(**overrides: object) -> QueryRequest:
    values: dict[str, object] = {
        "query_id": "query_123",
        "kind": "status",
        "platform": "feishu",
        "chat_id": "oc_allowed",
        "actor_id": "ou_reader",
        "payload": {"run_id": "run_123"},
    }
    values.update(overrides)
    return QueryRequest(**values)  # type: ignore[arg-type]


def _policy(**overrides: object) -> QueryPolicy:
    values: dict[str, object] = {
        "enabled": True,
        "authorized_channels": frozenset({"feishu:oc_allowed"}),
        "max_concurrency": 2,
        "timeout_seconds": 0.2,
        "retry_count": 1,
        "max_result_bytes": 1024,
    }
    values.update(overrides)
    return QueryPolicy(**values)  # type: ignore[arg-type]


async def test_authorized_query_returns_bounded_result_without_receipt_store(
    tmp_path: Path,
) -> None:
    receipts = tmp_path / "receipts.jsonl"

    async def handler(request: QueryRequest) -> dict[str, object]:
        return {"run_id": request.payload["run_id"], "status": "succeeded"}

    lane = ReadOnlyQueryLane(_policy(), {"status": handler})
    response = await lane.execute(_request())

    assert response.kind == "status"
    assert response.attempts == 1
    assert response.result == {"run_id": "run_123", "status": "succeeded"}
    assert receipts.exists() is False


async def test_remote_channel_must_be_explicitly_authorized() -> None:
    called = False

    async def handler(request: QueryRequest) -> dict[str, object]:
        nonlocal called
        called = True
        return {}

    lane = ReadOnlyQueryLane(_policy(), {"status": handler})

    with pytest.raises(QueryLaneError) as excinfo:
        await lane.execute(_request(chat_id="oc_denied"))

    assert excinfo.value.code == "query_channel_denied"
    assert called is False


@pytest.mark.parametrize("payload", [{"operation": "create_issue"}, {"body": "x"}])
async def test_mutation_shaped_or_unknown_payload_is_rejected_before_handler(
    payload: dict[str, object],
) -> None:
    called = False

    async def handler(request: QueryRequest) -> dict[str, object]:
        nonlocal called
        called = True
        return {}

    lane = ReadOnlyQueryLane(_policy(), {"status": handler})

    with pytest.raises(QueryLaneError) as excinfo:
        await lane.execute(_request(payload=payload))

    assert excinfo.value.code == "invalid_query_payload"
    assert called is False


async def test_only_explicit_retryable_failures_are_retried() -> None:
    attempts = 0

    async def handler(request: QueryRequest) -> dict[str, object]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RetryableQueryError("provider temporarily unavailable")
        return {"ok": True}

    lane = ReadOnlyQueryLane(_policy(retry_count=1), {"status": handler})
    response = await lane.execute(_request())

    assert response.attempts == 2
    assert attempts == 2


async def test_timeout_and_oversized_results_fail_closed() -> None:
    async def slow(request: QueryRequest) -> dict[str, object]:
        await asyncio.sleep(0.05)
        return {"ok": True}

    timeout_lane = ReadOnlyQueryLane(
        _policy(timeout_seconds=0.01, retry_count=0), {"status": slow}
    )
    with pytest.raises(QueryLaneError) as timeout_error:
        await timeout_lane.execute(_request())
    assert timeout_error.value.code == "query_timeout"

    async def large(request: QueryRequest) -> dict[str, object]:
        return {"value": "x" * 100}

    size_lane = ReadOnlyQueryLane(
        _policy(max_result_bytes=32, retry_count=0), {"status": large}
    )
    with pytest.raises(QueryLaneError) as size_error:
        await size_lane.execute(_request())
    assert size_error.value.code == "query_result_too_large"


async def test_concurrency_is_bounded() -> None:
    active = 0
    maximum = 0
    release = asyncio.Event()

    async def handler(request: QueryRequest) -> dict[str, object]:
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        await release.wait()
        active -= 1
        return {"id": request.query_id}

    lane = ReadOnlyQueryLane(
        _policy(max_concurrency=1, timeout_seconds=1), {"status": handler}
    )
    first = asyncio.create_task(lane.execute(_request(query_id="query_a")))
    second = asyncio.create_task(lane.execute(_request(query_id="query_b")))
    await asyncio.sleep(0.02)
    release.set()
    await asyncio.gather(first, second)

    assert maximum == 1


async def test_query_status_cli_uses_lane_and_does_not_create_receipts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ledger = tmp_path / "ledger.jsonl"
    receipts = tmp_path / "receipts.jsonl"
    ledger.write_text(
        json.dumps(
            {
                "event_id": "evt_1",
                "run_id": "run_123",
                "event_type": "run_created",
                "created_at": "2026-08-02T00:00:00+00:00",
                "payload": {
                    "run": {
                        "run_id": "run_123",
                        "status": "succeeded",
                        "source": {"platform": "feishu", "channel_id": "oc_allowed"},
                    }
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("OSTAG_RUN_LEDGER_PATH", str(ledger))
    monkeypatch.setenv("OSTAG_RECEIPT_STORE_PATH", str(receipts))
    monkeypatch.setenv("OSTAG_QUERY_LANE_ENABLED", "true")

    exit_code = await main_async(
        [
            "query",
            "status",
            "--run",
            "run_123",
            "--platform",
            "feishu",
            "--chat",
            "oc_allowed",
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["kind"] == "status"
    assert payload["result"]["run_id"] == "run_123"
    assert receipts.exists() is False
