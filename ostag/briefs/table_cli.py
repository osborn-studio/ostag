"""Explicit read-only Feishu CLI capability; no route or live A08 acceptance.

Trusted composition: TableReadPort(FeishuTableCLI(...).snapshot). The required
authorize(key) callback checks CURRENT authority, returns None/True, or denies
by returning anything else/raising. Configuration, executable resolution and
callbacks belong to trusted application code, never table content. Credentials
remain in the CLI's existing configuration; no credential argv/env is supplied.
Construction does no IO. Each snapshot/readback independently checks version,
identity and all pages; neither a prior snapshot nor a cache is accepted.

Contract source: supplied larksuite/cli v1.0.69 shortcuts/base/record_list.go,
record_ops.go, record_markdown.go and record_markdown_test.go. JSON record-list
uses a success envelope containing a row-major matrix, NOT bitable items.
Supported envelope: ok=true, identity='user', data=object. Matrix keys are
fields (nonempty unique strings), record_id_list, data, strict bool has_more;
optional ignored_fields/record_not_found must be null or empty lists. Optional
count is supported only as an exact integer page row count. Other matrix keys
(including undocumented scope/count/revision metadata) fail closed. This is a
deliberately conservative fixture-tested subset, not remote contract acceptance.
The version probe accepts only 'lark-cli version 1.0.69' (optional final LF).
The supplied record sources do not establish the version probe's wire format;
deployment must validate this supported Cobra-style output. Host 1.0.56 is NOT
contract proof and is rejected. No host CLI or real table is needed for tests.

Columns come exclusively from returned fields, with no status/name inference.
Resource binding comes from our exact request, NOT an independent provider echo.
v3 matrix paging has no supported transactional revision: revision is 'unknown'.
Mutable offset paging cannot prove absence of all concurrent changes, even after
exact-ID duplicate detection, stable columns and a final identity check. A
returned snapshot means bounded traversal exhausted, never atomicity or write
proof. Cells/IDs and returned snapshots are private; do not log or publish them.

The shared run_process owns bounded pipes, 15s timeout, process-group kill/reap.
Injected runners must honor that protocol; synchronous trusted callbacks own
their latency. Late/rollback/nonfinite clock observations never release data.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
import json
import math
import re
import time

from ostag.briefs.meeting_io import RESPONSE_BYTES, TOTAL_BYTES, decode_json, run_process
from ostag.briefs.table_quality import (
    MAX_ROWS, MAX_SNAPSHOT_BYTES, ResourceKey, TableRecord, TableSnapshot,
)

CLI_VERSION = "1.0.69"
PAGE_SIZE = 100
MAX_PAGES = 10
OPERATION_SECONDS = 120
COMMAND_SECONDS = 15


class TableReadError(Exception):
    """Fixed redacted failure; never includes provider output or chained errors."""

    def __init__(self):
        super().__init__("table_read_denied")


def _require(condition):
    if not condition:
        raise TableReadError()


def _matches(value, pattern):
    return type(value) is str and re.fullmatch(pattern, value) is not None


def _wall_now():
    return datetime.now(UTC)


@dataclass(frozen=True, repr=False)
class FeishuTableCLI:
    """Trusted configuration; pass snapshot as the existing TableReadPort.reader.

    profile is an exact CLI profile, not an alias to discover. namespace and
    expected_open_id bind every request to the configured application/user.
    runner(argv, *, deadline, clock) returns (stdout bytes, total pipe bytes).
    clock is monotonic seconds; wall returns an aware datetime.
    """

    profile: str
    expected_open_id: str
    namespace: str
    authorize: Callable[[ResourceKey], object] = field(repr=False)
    runner: Callable = field(default=run_process, repr=False)
    clock: Callable = field(default=time.monotonic, repr=False)
    wall: Callable = field(default=_wall_now, repr=False)

    def __post_init__(self):
        _require(_matches(self.profile, r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}"))
        _require(_matches(self.expected_open_id, r"ou_[A-Za-z0-9_-]{1,97}"))
        _require(_matches(self.namespace, r"[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,255}")
                 and "://" not in self.namespace)
        _require(all(callable(c) for c in (self.authorize, self.runner, self.clock, self.wall)))

    def snapshot(self, key: ResourceKey) -> TableSnapshot:
        # Raise OUTSIDE the handler: 'from None' alone retains sensitive
        # __context__, even though Python's default traceback hides it.
        try:
            return _Observation(self, key).collect()
        except Exception:
            pass
        raise TableReadError() from None


class _Observation:
    def __init__(self, config, key):
        _require(type(key) is ResourceKey and key.platform == "feishu"
                 and key.namespace == config.namespace)
        # IDs only, never names/aliases/URLs or option-looking arguments.
        _require(_matches(key.base_id, r"[A-Za-z0-9][A-Za-z0-9_]{0,255}"))
        _require(_matches(key.table_id, r"tbl[A-Za-z0-9_]{1,100}"))
        self.config, self.key = config, key
        self.last_tick = None
        self.deadline = None
        self.started = self.tick()
        self.deadline = self.started + OPERATION_SECONDS
        _require(math.isfinite(self.deadline) and self.deadline > self.started)
        self.started_wall = self.wall()
        self.accepted_bytes = 0

    def tick(self):
        value = self.config.clock()
        _require(type(value) in (int, float) and math.isfinite(value) and value >= 0)
        _require(self.last_tick is None or value >= self.last_tick)
        _require(self.deadline is None or value < self.deadline)
        self.last_tick = value
        return value

    def wall(self):
        value = self.config.wall()
        _require(type(value) is datetime and value.tzinfo is not None
                 and value.utcoffset() is not None)
        return value.astimezone(UTC)

    def guard(self):
        self.tick()
        allowed = self.config.authorize(self.key)
        _require(allowed is None or allowed is True)
        self.tick()

    def invoke(self, args, *, version=False):
        self.guard()
        _require(self.accepted_bytes < TOTAL_BYTES)
        end = min(self.deadline, self.tick() + COMMAND_SECONDS)
        result = self.config.runner(
            ["lark-cli", *args, "--profile", self.config.profile],
            deadline=end, clock=self.tick,
        )
        self.guard()
        _require(self.tick() < end)
        _require(type(result) is tuple and len(result) == 2)
        raw, size = result
        _require(type(raw) is bytes and type(size) is int
                 and len(raw) <= size <= RESPONSE_BYTES)
        _require(self.accepted_bytes + size <= TOTAL_BYTES)
        self.accepted_bytes += size
        if version:
            expected = f"lark-cli version {CLI_VERSION}".encode("ascii")
            _require(raw in (expected, expected + b"\n"))
            return None
        value = decode_json(raw)
        # decode_json rejects NaN/Infinity literals; finite JSON exponents can
        # also overflow Python floats (1e400), even in envelope metadata.
        json.dumps(value, allow_nan=False)
        _require(type(value) is dict and value.get("ok") is True
                 and value.get("identity") == "user" and type(value.get("data")) is dict)
        _require(value.get("error") is None)
        if "code" in value:
            _require(type(value["code"]) is int and value["code"] == 0)
        return value["data"]

    def identity(self):
        value = self.invoke(["api", "GET", "/open-apis/authen/v1/user_info",
                             "--as", "user", "--params", "{}", "--format", "json"])
        _require(type(value.get("open_id")) is str
                 and value["open_id"] == self.config.expected_open_id)

    def collect(self):
        self.invoke(["--version"], version=True)
        self.identity()
        columns, seen, records, canonical_bytes = None, set(), [], 0
        for _ in range(MAX_PAGES):
            page = self.invoke([
                "base", "+record-list", "--base-token", self.key.base_id,
                "--table-id", self.key.table_id, "--as", "user", "--format", "json",
                "--offset", str(len(records)), "--limit", str(PAGE_SIZE),
            ])
            _require(not set(page) - {"fields", "record_id_list", "data", "has_more",
                                      "ignored_fields", "record_not_found", "count"})
            for marker in ("ignored_fields", "record_not_found"):
                value = page.get(marker)
                _require(value is None or (type(value) is list and not value))
            fields, ids, rows = page.get("fields"), page.get("record_id_list"), page.get("data")
            _require(type(fields) is list and bool(fields)
                     and all(type(f) is str and bool(f.strip()) for f in fields))
            _require(len(set(fields)) == len(fields))
            _require(columns is None or fields == columns)
            columns = fields
            _require(type(ids) is list and type(rows) is list
                     and len(ids) == len(rows) <= PAGE_SIZE)
            _require(type(page.get("has_more")) is bool)
            if "count" in page:
                _require(type(page["count"]) is int and page["count"] == len(rows))
            _require(len(records) + len(rows) <= MAX_ROWS)
            for rid, cells in zip(ids, rows):
                _require(_matches(rid, r"rec[A-Za-z0-9_]{1,100}") and rid not in seen)
                _require(type(cells) is list and len(cells) == len(columns))
                record = TableRecord(rid, dict(zip(columns, cells)))
                canonical_bytes += len(record._json) + 1
                _require(canonical_bytes <= MAX_SNAPSHOT_BYTES)
                seen.add(rid)
                records.append(record)
            self.tick()
            if not page["has_more"]:
                break
            _require(bool(rows) and len(records) < MAX_ROWS)
        else:
            raise TableReadError()
        self.identity()
        completed = self.wall()
        _require(completed >= self.started_wall)
        snapshot = TableSnapshot(self.key, tuple(records),
                                 completed.isoformat().replace("+00:00", "Z"), "unknown")
        self.guard()
        return snapshot
