"""Dedicated A02 SQLite persistence, not a runtime or a delivery service.

Trusted service code must authorize admission and every later IO/commit using
current grants. Principal, pinned grant metadata and payload flags (including
accepted/authorized/valid) are not authority. A04 must require meeting intake
state == accepted, supply its frozen snapshot and pinned execution duration,
and bind ExecutionTiming to the exact persisted first_claim_at/duration/scope.

Requester APIs require namespace + platform + actor. WorkerContext is an
explicit trusted in-process boundary, not a credential: never expose worker
methods or construct contexts directly from untrusted endpoint/model input.
Recovery checks grant revision, snapshot TTL and source availability outside
SQLite; the opt-in A04 precommit callback is the only authorization callback
inside the lock, must be bounded/non-reentrant, and cannot access this store.
No model/user callback is executed and no new source data replaces a snapshot.
A03/A04 own semantic validation and current authorization. Trusted nonblocking clocks are sampled under locks.

One job is one queue item; one initial attempt plus two expired-attempt retries,
one live lease globally. A completion only queues one requester-private outbox
intent. It does not send, acknowledge, or promise exactly-once delivery. Opt-in U01 schema4
adds the separate send fence/receipt/uncertain-result protocol; M06 owns collection
progress. No Monitor schema, server lifecycle or provider integration lives here.

Timing contract: DELETE journaling with BEGIN EXCLUSIVE puts both reader and
writer lock waits BEFORE the first clock sample. The exclusive lock is retained
through COMMIT, so a normal reader cannot delay COMMIT past the final fresh
lease/deadline check. Fenced operations check again after transaction work,
including result materialization; expiry or clock rewind rolls back all effects.
For a successful transaction this final check is its logical linearization point
under the exclusive lock; durability/visibility occurs at SQLite COMMIT. This
is not a realtime guarantee that the physical commit or function return precedes
the deadline: arbitrary process scheduling, filesystem or fsync stalls between
the final check and commit/return cannot be bounded here. A lost response must
be reconciled through the scoped durable job/outbox, never blindly republished.

Only the default DELETE journal mode is supported; it is checked without setting
or converting journal mode, including on reused handles under the transaction
lock. FULL synchronous durability is explicit. Owned schema1 DELETE databases
migrate additively to schema2; foreign/future/partial schemas are refused before writes.
The tradeoff is that writes also exclude readers; lock waits use busy_timeout,
but transaction execution and fsync duration are not bounded by that timeout.
Use a filesystem with working SQLite locks and durability. No WAL is enabled:
WAL would require same-host local storage and backups retaining the live WAL or
using SQLite's backup API, plus a separately reviewed owned-database migration.
SQLite transaction semantics: https://www.sqlite.org/lang_transaction.html
WAL operational limits: https://www.sqlite.org/wal.html
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hashlib
import json
import math
from pathlib import Path
import sqlite3
from threading import Event, Lock, Thread
from time import monotonic
from types import MappingProxyType
from typing import Literal, TypeAlias
from uuid import uuid4

from ostag.briefs.models import (
    _identifier, _invalid, _positive_int, _principal, _utc,
)
from ostag.core.identity import Principal

# Preserve the A02/A03 construction contract (including actual schema1 -> 2).
# A04 explicitly calls enable_manual() for the additive schema3 migration.
CURRENT_SCHEMA_VERSION = 2
READABLE_SCHEMA_MIN = 1
READABLE_SCHEMA_MAX = 4
MANUAL_SCHEMA_VERSION = 3
DELIVERY_SCHEMA_VERSION = 4
APPLICATION_ID = 0x42524653  # BRFS; deliberately different from unmarked Monitor SQLite.
MAX_JSON_BYTES = 1024 * 1024
MAX_QUEUED = 20
MAX_ATTEMPTS = 3
LEASE_SECONDS = 30
MAX_EXECUTION_SECONDS = 180

JsonValue: TypeAlias = (
    None | bool | int | float | str | tuple["JsonValue", ...] | Mapping[str, "JsonValue"]
)
JobState: TypeAlias = Literal[
    "queued", "collecting", "generating", "draft_ready", "needs_input", "failed", "cancelled"
]
_ACTIVE = ("collecting", "generating")
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class StoreError(RuntimeError):
    """Brief-domain persistence failure; never a successful no-op."""


class BriefSchemaError(StoreError):
    """Foreign, corrupt, partial or incompatible DB; A11 may disable briefs only."""


class StoreBusy(StoreError):
    """Retryable SQLite contention, with no partially committed operation."""


class Conflict(StoreError):
    """Identity, idempotency or explicit state conflict."""


class QueueFull(StoreError):
    """Admission rejected at the global queued-job capacity."""


class LeaseLost(Conflict):
    """Stop new IO and discard results from this attempt."""


class ClockError(StoreError):
    """Trusted clock preceded a relevant persisted observation; refuse the operation."""


def _seconds(value: object, field: str, maximum: float, *, zero: bool = False) -> float:
    if (type(value) not in (int, float)
            or not (0 <= value <= maximum if zero else 0 < value <= maximum)
            or not math.isfinite(value)):
        raise _invalid(f"{field} must be finite and {'nonnegative' if zero else 'positive'}"
                       f", at most {maximum}")
    return float(value)


def _integer(value: object, field: str, maximum: int = 2**63 - 1) -> int:
    value = _positive_int(value, field)
    if value > maximum:
        raise _invalid(f"{field} exceeds {maximum}")
    return value


def _micros(value: str) -> int:
    delta = _utc(value, "now") - _EPOCH
    return (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds


def _stamp(value: int | None) -> str | None:
    return None if value is None else (_EPOCH + timedelta(microseconds=value)).isoformat().replace(
        "+00:00", "Z"
    )


def utc_now() -> str:
    """Default trusted wall clock; callers may inject a strict UTC clock for tests."""
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _clock_micros(clock: Callable[[], str]) -> int:
    if not callable(clock):
        raise _invalid("clock must be a trusted nonblocking UTC callable")
    return _micros(clock())


def _canonical(payload: object) -> str:
    """Exact JSON types, depth <=64, <=1MiB UTF-8; reject rather than truncate.

    Iterative encoder emission bounds serialized allocation. Traversal has a
    node/byte bound too, including cycles; caller objects are never retained.
    Only plain dict/list containers are accepted, avoiding custom serializers.
    """
    if type(payload) is not dict:
        raise _invalid("snapshot must be a plain JSON object")
    nodes = 0
    budget = 0

    def check(value: object, depth: int) -> None:
        nonlocal nodes, budget
        nodes += 1
        if depth > 64 or nodes > MAX_JSON_BYTES or budget > MAX_JSON_BYTES:
            raise _invalid("JSON snapshot exceeds depth or size bound")
        if type(value) is dict:
            budget += 2 + len(value)
            for key, child in value.items():
                if type(key) is not str:
                    raise _invalid("JSON keys must be strings")
                check(key, depth + 1)
                check(child, depth + 1)
        elif type(value) is list:
            budget += 2 + len(value)
            for child in value:
                check(child, depth + 1)
        elif type(value) is str:
            if len(value) > MAX_JSON_BYTES:
                raise _invalid("JSON string exceeds size bound")
            budget += len(value.encode("utf-8"))
        elif type(value) is float:
            if not math.isfinite(value):
                raise _invalid("JSON numbers must be finite")
        elif value is not None and type(value) not in (bool, int):
            raise _invalid("snapshot contains a non-JSON value")

    try:
        check(payload, 0)
        chunks: list[str] = []
        size = 0
        encoder = json.JSONEncoder(ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                                   allow_nan=False)
        for chunk in encoder.iterencode(payload):
            size += len(chunk.encode("utf-8"))
            if size > MAX_JSON_BYTES:
                raise _invalid("JSON snapshot exceeds 1MiB")
            chunks.append(chunk)
        return "".join(chunks)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise _invalid("snapshot is not bounded valid JSON") from exc


def _frozen(encoded: str) -> Mapping[str, JsonValue]:
    def freeze(value: object) -> JsonValue:
        if isinstance(value, dict):
            return MappingProxyType({key: freeze(child) for key, child in value.items()})
        if isinstance(value, list):
            return tuple(freeze(child) for child in value)
        return value

    return freeze(json.loads(encoded))


@dataclass(frozen=True)
class RequesterContext:
    """Authenticated requester scope, supplied by trusted service code."""

    namespace: str
    principal: Principal

    def __post_init__(self) -> None:
        _identifier(self.namespace, "namespace")
        object.__setattr__(self, "principal", _principal(self.principal))


@dataclass(frozen=True)
class WorkerContext:
    """Trusted worker identity; this object itself does not establish authority."""

    owner_id: str

    def __post_init__(self) -> None:
        _identifier(self.owner_id, "owner_id")


@dataclass(frozen=True)
class PrivateDestination:
    """Trusted adapter-resolved private address for exactly this requesting principal.

    Store checks the principal binding; A04 must verify address ownership/private
    chat type with the adapter. A user/model-provided address is not authorized.
    """

    principal: Principal
    address: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "principal", _principal(self.principal))
        _identifier(self.address, "private destination address")


def _scope(value: RequesterContext) -> tuple[str, str, str]:
    if type(value) is not RequesterContext:
        raise _invalid("explicit RequesterContext required")
    _identifier(value.namespace, "namespace")
    principal = _principal(value.principal)
    return value.namespace, principal.platform, principal.actor_id


def _worker(value: WorkerContext) -> str:
    if type(value) is not WorkerContext:
        raise _invalid("explicit trusted WorkerContext required")
    return _identifier(value.owner_id, "owner_id")


@dataclass(frozen=True)
class Lease:
    """Fenced attempt token, always checked against persisted scope and current time."""

    scope: RequesterContext
    job_id: str
    worker: WorkerContext
    fence: int

    def __post_init__(self) -> None:
        _scope(self.scope)
        _identifier(self.job_id, "job_id")
        _worker(self.worker)
        _integer(self.fence, "fence")


@dataclass(frozen=True)
class Job:
    """One detached bounded snapshot; JSON views are recursively immutable."""

    job_id: str
    scope: RequesterContext
    idempotency_key: str
    payload_json: str
    content_hash: str
    grant_id: str
    grant_revision: int
    destination: PrivateDestination
    execution_seconds: int
    state: JobState
    attempt: int
    fence: int
    owner_id: str | None
    created_at: str
    updated_at: str
    first_claim_at: str | None
    deadline: str | None
    lease_until: str | None

    @property
    def payload(self) -> Mapping[str, JsonValue]:
        return _frozen(self.payload_json)

    @property
    def lease(self) -> Lease:
        if self.state not in _ACTIVE or self.owner_id is None:
            raise Conflict("job has no active attempt")
        return Lease(self.scope, self.job_id, WorkerContext(self.owner_id), self.fence)


@dataclass(frozen=True)
class Revision:
    """Frozen structural draft, not a claim of semantic validation or approval."""

    revision_id: str
    job_id: str
    payload_json: str
    provenance_json: str
    validation_version: str
    created_at: str

    @property
    def payload(self) -> Mapping[str, JsonValue]:
        return _frozen(self.payload_json)

    @property
    def provenance(self) -> Mapping[str, JsonValue]:
        return _frozen(self.provenance_json)


@dataclass(frozen=True)
class OutboxSnapshot:
    """One pending/cancelled local intent; no provider send or receipt state."""

    delivery_id: str
    destination: PrivateDestination
    state: Literal["pending", "cancelled"]
    revision: Revision


@dataclass(frozen=True)
class RecoveryValidity:
    """Current trusted checks on the supplied job, never parsed from its payload.

    grant_current includes current authorization for the pinned grant ID/revision
    and scope. snapshot_fresh and sources_available refer to this exact original
    snapshot, without fetching replacement content. A04 supplies these checks.
    """

    grant_current: bool
    snapshot_fresh: bool
    sources_available: bool

    def __post_init__(self) -> None:
        for value in (self.grant_current, self.snapshot_fresh, self.sources_available):
            if type(value) is not bool:
                raise _invalid("recovery checks require explicit booleans")

    @property
    def valid(self) -> bool:
        return self.grant_current and self.snapshot_fresh and self.sources_available


# A03's fifth file is this A02-owned durable extension, intentionally additive.
# Money is exact micro-USD, bounded per evidence item; totals use Python integers
# to avoid SQLite SUM overflow. Admission is an estimate, never a billing limit.
MAX_COST_INTEGER = 10**12


class BudgetDenied(Conflict):
    """No new provider IO: job/day/call capacity or charged-day check failed."""


def _cost_int(value: object, field: str, maximum: int = MAX_COST_INTEGER,
              minimum: int = 0) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise _invalid(f"{field} must be an exact integer in [{minimum}, {maximum}]")
    return value


def _hash(value: object, field: str) -> str:
    if (type(value) is not str or len(value) != 64
            or any(c not in "0123456789abcdef" for c in value)):
        raise _invalid(f"{field} must be lowercase SHA256")
    return value


@dataclass(frozen=True)
class Usage:
    """Trusted transport/reconciliation evidence; never model-authored JSON.

    cost_micros is observed billable cost, including any provider-side overhead.
    Missing evidence is represented by absent Usage, never synthetic zero usage.
    """

    input_tokens: int
    output_tokens: int
    cost_micros: int

    def __post_init__(self):
        for field in ("input_tokens", "output_tokens", "cost_micros"):
            _cost_int(getattr(self, field), field)

    def to_dict(self):
        return {"input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
                "cost_micros": self.cost_micros}

    @classmethod
    def from_dict(cls, payload):
        from ostag.briefs.models import _keys
        return cls(**_keys(payload, ("input_tokens", "output_tokens", "cost_micros")))


@dataclass(frozen=True)
class CostProfile:
    """Trusted first-use job pin. A03 derives the reservation from pricing bounds.

    parameters_json freezes the full versioned evaluation profile, not a config
    alias. This store primitive cannot audit provider tokenization/pricing; A04
    must supply an adapter whose bound includes every provider-billed input token.
    """

    parameters_json: str
    evaluation_key: str
    max_job_micros: int
    max_calls: int
    reservation_micros: int

    def __post_init__(self):
        _hash(self.evaluation_key, "evaluation_key")
        _cost_int(self.max_job_micros, "max_job_micros", minimum=1)
        _cost_int(self.max_calls, "max_calls", 3, 1)
        _cost_int(self.reservation_micros, "reservation_micros", minimum=1)
        if self.reservation_micros > self.max_job_micros:
            raise _invalid("maximum call reservation exceeds job cap")
        if type(self.parameters_json) is not str or len(self.parameters_json) > 16384:
            raise _invalid("bounded canonical parameters required")
        try:
            if _canonical(json.loads(self.parameters_json)) != self.parameters_json:
                raise _invalid("parameters must be canonical JSON")
        except (ValueError, RecursionError) as exc:
            raise _invalid("parameters must be canonical JSON") from exc

    @property
    def profile_json(self):
        return _canonical({"parameters_json": self.parameters_json,
                           "evaluation_key": self.evaluation_key,
                           "max_job_micros": self.max_job_micros, "max_calls": self.max_calls,
                           "reservation_micros": self.reservation_micros})


@dataclass(frozen=True)
class CostReservation:
    reservation_id: str
    job_id: str
    call_id: str
    charged_day: str
    state: str
    reserved_micros: int
    charged_micros: int
    usage: Usage | None
    diagnostic: str
    config_hash: str

    def __post_init__(self):
        for field in ("reservation_id", "job_id", "call_id", "diagnostic"):
            _identifier(getattr(self, field), field)
        _hash(self.config_hash, "config_hash")
        if type(self.charged_day) is not str or len(self.charged_day) != 10:
            raise _invalid("charged_day must be a UTC calendar date")
        _utc(self.charged_day + "T00:00:00Z", "charged_day")
        _cost_int(self.reserved_micros, "reserved_micros", minimum=1)
        _cost_int(self.charged_micros, "charged_micros")
        if type(self.state) is not str or self.state not in (
            "reserved", "started", "unknown", "settled", "released"
        ):
            raise _invalid("unknown cost reservation state")
        if (self.state == "settled") != (type(self.usage) is Usage):
            raise _invalid("only settled cost records contain exact Usage")
        if self.usage is not None:
            if type(self.usage) is not Usage:
                raise _invalid("usage must be an explicit Usage")
            self.usage.__post_init__()
        expected = (0 if self.state == "released" else
                    self.usage.cost_micros if self.usage else self.reserved_micros)
        if self.charged_micros != expected:
            raise _invalid("charge must retain the maximum until settled or proven unstarted")

    @property
    def overrun(self):
        return self.usage is not None and self.usage.cost_micros > self.reserved_micros


def _reservation(row: sqlite3.Row) -> CostReservation:
    usage = None if row["usage_json"] is None else Usage.from_dict(json.loads(row["usage_json"]))
    charge = (0 if row["state"] == "released" else
              usage.cost_micros if usage is not None else row["reserved_micros"])
    return CostReservation(row["reservation_id"], row["job_id"], row["call_id"],
                           row["charged_day"], row["state"], row["reserved_micros"],
                           charge, usage, row["diagnostic"], row["config_hash"])


# Exact persistent schema definitions (including rejection of every unknown
# trigger) let partially initialized or unexpectedly modified owned files fail
# closed. Future migrations must add their prior readable schemas explicitly;
# version numbers alone are never sufficient proof of ownership/compatibility.
_SCHEMA = (
    """CREATE TABLE brief_jobs (
        job_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, platform TEXT NOT NULL,
        actor_id TEXT NOT NULL, idempotency_key TEXT NOT NULL, payload_json TEXT NOT NULL,
        content_hash TEXT NOT NULL, grant_id TEXT NOT NULL, grant_revision INTEGER NOT NULL,
        destination TEXT NOT NULL, execution_seconds INTEGER NOT NULL
            CHECK(execution_seconds BETWEEN 1 AND 180),
        state TEXT NOT NULL CHECK(state IN
            ('queued','collecting','generating','draft_ready','needs_input','failed','cancelled')),
        attempt INTEGER NOT NULL DEFAULT 0 CHECK(attempt BETWEEN 0 AND 3),
        fence INTEGER NOT NULL DEFAULT 0 CHECK(fence >= 0), owner_id TEXT,
        created_us INTEGER NOT NULL, updated_us INTEGER NOT NULL,
        first_claim_us INTEGER, deadline_us INTEGER, lease_us INTEGER,
        UNIQUE(namespace, platform, actor_id, idempotency_key)
    )""",
    "CREATE INDEX brief_jobs_state ON brief_jobs(state, lease_us)",
    """CREATE TABLE brief_revisions (
        revision_id TEXT PRIMARY KEY, job_id TEXT NOT NULL UNIQUE REFERENCES brief_jobs(job_id),
        payload_json TEXT NOT NULL, provenance_json TEXT NOT NULL,
        validation_version TEXT NOT NULL, created_us INTEGER NOT NULL
    )""",
    """CREATE TABLE brief_outbox (
        delivery_id TEXT PRIMARY KEY, job_id TEXT NOT NULL UNIQUE REFERENCES brief_jobs(job_id),
        revision_id TEXT NOT NULL UNIQUE REFERENCES brief_revisions(revision_id),
        destination TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('pending','cancelled'))
    )""",
)


_COST_SCHEMA = (
    """CREATE TABLE brief_cost_profiles (
        job_id TEXT PRIMARY KEY REFERENCES brief_jobs(job_id), profile_json TEXT NOT NULL
    )""",
    """CREATE TABLE brief_cost_calls (
        reservation_id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES brief_cost_profiles(job_id),
        call_id TEXT NOT NULL, fence INTEGER NOT NULL, owner_id TEXT NOT NULL,
        charged_day TEXT NOT NULL, reserved_micros INTEGER NOT NULL CHECK(reserved_micros > 0),
        state TEXT NOT NULL CHECK(state IN ('reserved','started','unknown','settled','released')),
        usage_json TEXT, diagnostic TEXT NOT NULL, config_hash TEXT NOT NULL,
        daily_limit_micros INTEGER NOT NULL, created_us INTEGER NOT NULL, updated_us INTEGER NOT NULL,
        UNIQUE(job_id, call_id)
    )""",
    "CREATE INDEX brief_cost_day ON brief_cost_calls(charged_day, job_id)",
)
_MANUAL_SCHEMA = (
    """CREATE TABLE brief_intakes (
        job_id TEXT PRIMARY KEY REFERENCES brief_jobs(job_id), request_hash TEXT NOT NULL
    )""",
    """CREATE TABLE brief_failures (
        job_id TEXT PRIMARY KEY REFERENCES brief_jobs(job_id), cause TEXT NOT NULL,
        created_us INTEGER NOT NULL
    )""",
)
_DELIVERY_SCHEMA = (
    """CREATE TABLE brief_deliveries (
        delivery_id TEXT PRIMARY KEY REFERENCES brief_outbox(delivery_id),
        job_id TEXT NOT NULL UNIQUE REFERENCES brief_jobs(job_id),
        revision_id TEXT NOT NULL UNIQUE REFERENCES brief_revisions(revision_id),
        namespace TEXT NOT NULL, platform TEXT NOT NULL, actor_id TEXT NOT NULL,
        intent_json TEXT NOT NULL CHECK(length(CAST(intent_json AS BLOB)) <= 65536),
        intent_digest TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('pending','started','unknown','provider_accepted')),
        token TEXT UNIQUE, owner_id TEXT, candidate_id TEXT,
        created_us INTEGER NOT NULL, updated_us INTEGER NOT NULL, started_us INTEGER,
        uncertainty_json TEXT CHECK(length(CAST(uncertainty_json AS BLOB)) <= 1024),
        first_failure_json TEXT CHECK(length(CAST(first_failure_json AS BLOB)) <= 1024),
        receipt_json TEXT CHECK(length(CAST(receipt_json AS BLOB)) <= 65536),
        read_attempts INTEGER NOT NULL DEFAULT 0 CHECK(read_attempts BETWEEN 0 AND 32)
    )""",
    """CREATE TABLE brief_delivery_reads (
        delivery_id TEXT NOT NULL REFERENCES brief_deliveries(delivery_id),
        ordinal INTEGER NOT NULL CHECK(ordinal BETWEEN 1 AND 32),
        owner_id TEXT NOT NULL, token TEXT NOT NULL UNIQUE, candidate_id TEXT NOT NULL,
        settled INTEGER NOT NULL DEFAULT 0 CHECK(settled IN (0,1)),
        PRIMARY KEY(delivery_id, ordinal)
    )""",
    """CREATE TABLE brief_delivery_observations (
        delivery_id TEXT NOT NULL REFERENCES brief_deliveries(delivery_id),
        ordinal INTEGER NOT NULL CHECK(ordinal BETWEEN 1 AND 32),
        evidence_json TEXT NOT NULL CHECK(length(CAST(evidence_json AS BLOB)) <= 65536),
        PRIMARY KEY(delivery_id, ordinal)
    )""",
)
_SCHEMAS = {1: _SCHEMA, 2: _SCHEMA + _COST_SCHEMA,
            3: _SCHEMA + _COST_SCHEMA + _MANUAL_SCHEMA,
            4: _SCHEMA + _COST_SCHEMA + _MANUAL_SCHEMA + _DELIVERY_SCHEMA}

# Fixed public-domain causes only: never exception strings, excerpts or provider errors.
FAILURE_CAUSES = frozenset({
    "cancelled", "stopped", "timeout", "lease_lost", "authorization_denied",
    "invalid_request", "config_changed", "recovery_required", "store_error",
    "commit_failed", "needs_input", "input_too_large", "checkpoint_denied",
    "call_denied", "budget_denied", "transport_error", "invalid_transport_response",
    "cost_overrun", "missing_usage", "missing_identity", "model_mismatch",
    "usage_bound_exceeded", "invalid_output",
})


def _failure_cause(cause: str) -> None:
    if type(cause) is not str or cause not in FAILURE_CAUSES:
        raise _invalid("unknown sanitized failure cause")


def _normalized(sql: str) -> str:
    return " ".join(sql.split())


def _database_error(exc: sqlite3.Error, *, opening: bool = False) -> StoreError:
    code = getattr(exc, "sqlite_errorcode", 0) & 0xFF
    if code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
        return StoreBusy("brief database busy/locked; retry the operation later")
    if opening:
        return BriefSchemaError("brief database is unreadable or has an invalid schema")
    return StoreError(f"brief database operation failed ({type(exc).__name__})")


def _job(row: sqlite3.Row) -> Job:
    principal = Principal(row["platform"], row["actor_id"])
    return Job(
        job_id=row["job_id"], scope=RequesterContext(row["namespace"], principal),
        idempotency_key=row["idempotency_key"], payload_json=row["payload_json"],
        content_hash=row["content_hash"], grant_id=row["grant_id"],
        grant_revision=row["grant_revision"], destination=PrivateDestination(
            principal, row["destination"]), execution_seconds=row["execution_seconds"],
        state=row["state"], attempt=row["attempt"], fence=row["fence"], owner_id=row["owner_id"],
        created_at=_stamp(row["created_us"]), updated_at=_stamp(row["updated_us"]),
        first_claim_at=_stamp(row["first_claim_us"]), deadline=_stamp(row["deadline_us"]),
        lease_until=_stamp(row["lease_us"]),
    )


def _revision(row: sqlite3.Row) -> Revision:
    return Revision(row["revision_id"], row["job_id"], row["payload_json"],
                    row["provenance_json"], row["validation_version"], _stamp(row["created_us"]))


class BriefStore:
    """Owns a dedicated on-disk SQLite file and short per-operation connections.

    busy_timeout is finite 0..5 seconds (default 1); no connection is retained.
    All mutations serialize with BEGIN EXCLUSIVE. Lease operations take a trusted,
    nonblocking, thread-safe UTC clock callable (default utc_now), sampled AFTER
    acquiring the write lock, never a timestamp captured before lock contention.
    Test clocks must advance consistently across all handles/guard threads.
    The operational clock and optional trusted bounded precommit policy gate
    run under the lock; policy gates must never be supplied as clocks and must
    not reenter this store/guard. No model/user callback may run here. Admission's now is an intake timestamp,
    independent of execution timing; exact replay does not change persisted time.
    No generic connection or SQL API is exposed. Methods accepting worker tokens
    are trusted maintenance;
    get/outbox/cancel are requester scoped. SQLite errors always roll back.
    """

    def __init__(self, path: str | Path, *, busy_timeout: float = 1.0) -> None:
        self.busy_timeout = _seconds(busy_timeout, "busy_timeout", 5, zero=True)
        if not isinstance(path, (str, Path)) or not str(path).strip() or str(path) == ":memory:":
            raise _invalid("a dedicated on-disk SQLite path is required")
        self.path = Path(path)
        # Parent directory creation is an operational responsibility, avoiding
        # unrelated filesystem effects when a wrong DB path is supplied.
        with self._connection(opening=True) as db:
            # Header and schema must come from one snapshot: another initializer
            # can commit between the separate PRAGMA/SELECT inspection queries.
            db.execute("BEGIN")
            self._inspect(db, allow_empty=True)
            db.rollback()
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN EXCLUSIVE")
            fresh = self._inspect(db, allow_empty=True)  # serialize competing initializers
            if fresh:
                for statement in _SCHEMAS[CURRENT_SCHEMA_VERSION]:
                    db.execute(statement)
                db.execute(f"PRAGMA application_id={APPLICATION_ID}")
                db.execute(f"PRAGMA user_version={CURRENT_SCHEMA_VERSION}")
            elif db.execute("PRAGMA user_version").fetchone()[0] == 1:
                for statement in _COST_SCHEMA:
                    db.execute(statement)
                db.execute(f"PRAGMA user_version={CURRENT_SCHEMA_VERSION}")
            db.commit()

    @contextmanager
    def _connection(self, *, opening: bool = False) -> Iterator[sqlite3.Connection]:
        db = None
        try:
            db = sqlite3.connect(self.path, timeout=self.busy_timeout, isolation_level=None)
            db.row_factory = sqlite3.Row
            yield db
        except sqlite3.Error as exc:
            raise _database_error(exc, opening=opening) from exc
        finally:
            if db is not None:
                try:
                    if db.in_transaction:
                        db.rollback()
                finally:
                    db.close()

    @staticmethod
    def _inspect(db: sqlite3.Connection, *, allow_empty: bool = False) -> bool:
        app = db.execute("PRAGMA application_id").fetchone()[0]
        version = db.execute("PRAGMA user_version").fetchone()[0]
        rows = db.execute("SELECT type,name,sql FROM sqlite_master").fetchall()
        if db.execute("PRAGMA journal_mode").fetchone()[0] != "delete":
            raise BriefSchemaError("brief database requires DELETE journal mode; refusing conversion")
        if app == 0 and version == 0 and not rows and allow_empty:
            return True
        if app != APPLICATION_ID:
            raise BriefSchemaError("not an owned brief database; refusing mutation")
        if not READABLE_SCHEMA_MIN <= version <= READABLE_SCHEMA_MAX:
            raise BriefSchemaError(f"brief schema {version} outside readable interval "
                                   f"[{READABLE_SCHEMA_MIN}, {READABLE_SCHEMA_MAX}]")
        actual = {_normalized(row["sql"]) for row in rows
                  if row["sql"] is not None}
        if actual != {_normalized(sql) for sql in _SCHEMAS[version]}:
            raise BriefSchemaError("partial or unexpected brief schema; refusing mutation")
        return False

    @contextmanager
    def _transaction(self, *, write: bool = True) -> Iterator[sqlite3.Connection]:
        with self._connection() as db:
            db.execute("BEGIN")
            self._inspect(db)
            db.rollback()
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN EXCLUSIVE" if write else "BEGIN")
            self._inspect(db)
            yield db
            db.commit()

    def enable_manual(self) -> None:
        """Opt in to additive schema3; legacy construction still upgrades 1 -> 2.

        Actual schema1/2 rows are unchanged. Reopened schema3 is readable by
        this implementation; old binaries must fail closed on its newer schema.
        """
        with self._transaction() as db:
            if db.execute("PRAGMA user_version").fetchone()[0] == 2:
                for statement in _MANUAL_SCHEMA:
                    db.execute(statement)
                db.execute(f"PRAGMA user_version={MANUAL_SCHEMA_VERSION}")

    def enable_delivery(self) -> None:
        """Explicit additive schema4 opt-in through schema3; never constructor-driven.

        Exact old rows/DDL are retained. Schema1 construction still performs the
        existing 1->2 migration; enable_manual still stops at 3. Old binaries
        reject 4. Delivery methods use nonwaiting transactions so their async
        caller owns contention/cancellation bounds without SQLite worker threads.
        """
        with self._transaction() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version == 2:
                for statement in _MANUAL_SCHEMA:
                    db.execute(statement)
            if version < DELIVERY_SCHEMA_VERSION:
                for statement in _DELIVERY_SCHEMA:
                    db.execute(statement)
                db.execute(f"PRAGMA user_version={DELIVERY_SCHEMA_VERSION}")

    @contextmanager
    def _delivery_transaction(self, *, write=True):
        """Nonwaiting U01 transactions; legacy timing/error paths stay unchanged.

        Match the same exact-schema/DELETE/FULL/exclusive protocol. Contention
        is retried only by the delivery operation's bounded async caller. New
        private paths discard SQLite exception contexts as well as their text.
        """
        db = None
        failure = None
        try:
            db = sqlite3.connect(self.path, timeout=0, isolation_level=None)
            db.row_factory = sqlite3.Row
            db.execute("BEGIN")
            self._inspect(db)
            db.rollback()
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN EXCLUSIVE" if write else "BEGIN")
            self._inspect(db)
            yield db
            db.commit()
        except sqlite3.Error as exc:
            failure = _database_error(exc)
        finally:
            if db is not None:
                try:
                    if db.in_transaction:
                        db.rollback()
                finally:
                    db.close()
        if failure is not None:
            raise failure

    @staticmethod
    def _delivery_row(db, worker, scope, delivery_id):
        _worker(worker)
        params = _scope(scope)
        _identifier(delivery_id, "delivery_id")
        if db.execute("PRAGMA user_version").fetchone()[0] != DELIVERY_SCHEMA_VERSION:
            raise BriefSchemaError("delivery requires explicit schema4 enablement")
        return db.execute("SELECT * FROM brief_deliveries WHERE namespace=? AND platform=? "
                          "AND actor_id=? AND delivery_id=?", (*params, delivery_id)).fetchone()

    def _delivery_view(self, db, worker, scope, delivery_id):
        from ostag.briefs.delivery import _snapshot
        row = self._delivery_row(db, worker, scope, delivery_id)
        if row is None:
            return None
        job = self._find(db, scope, row["job_id"])
        box = db.execute("SELECT * FROM brief_outbox WHERE delivery_id=?",
                         (delivery_id,)).fetchone()
        observations = tuple(r[0] for r in db.execute(
            "SELECT evidence_json FROM brief_delivery_observations WHERE delivery_id=? "
            "ORDER BY ordinal", (delivery_id,)))
        return _snapshot(row, scope, job["state"], box["state"], observations)

    def delivery_snapshot(self, worker: WorkerContext, scope: RequesterContext, delivery_id: str):
        """Detached private history, internal + authenticated scope; never an approval."""
        with self._delivery_transaction(write=False) as db:
            return self._delivery_view(db, worker, scope, delivery_id)

    @staticmethod
    def _delivery_clock(clock):
        import asyncio
        from ostag.briefs.delivery import DeliveryError, _reason
        reason = None
        try:
            stamp = _clock_micros(clock)
        except DeliveryError as exc:
            reason = _reason(exc, "clock_error")
        except (Exception, asyncio.CancelledError):
            reason = "clock_error"
        if reason:
            raise DeliveryError(reason)
        return stamp

    def _delivery_final(self, clock, stamp, precommit):
        # All SQL/result construction precedes this bounded trusted local policy.
        # The enclosing transaction performs only COMMIT after this clock check.
        import asyncio
        from ostag.briefs.delivery import DeliveryError, _reason
        reason = None
        try:
            if precommit() is not None:
                reason = "authorization_denied"
        except DeliveryError as exc:
            reason = _reason(exc, "authorization_denied")
        except (Exception, asyncio.CancelledError):
            reason = "authorization_denied"
        if reason:
            raise DeliveryError(reason)
        if self._delivery_clock(clock) < stamp:
            raise ClockError("delivery clock moved backwards")

    def prepare_delivery(self, worker, intent, *, clock, precommit):
        """Admit exact persisted revision only; no replacement or cross-job coalescing."""
        from ostag.briefs.delivery import DeliveryIntent, wire_text
        if type(intent) is not DeliveryIntent or not callable(precommit):
            raise Conflict("trusted delivery intent and policy required")
        encoded, digest = intent.encoded, intent.digest
        with self._delivery_transaction() as db:
            old = self._delivery_row(db, worker, intent.scope, intent.delivery_id)
            job = self._find(db, intent.scope, intent.job_id)
            box = db.execute("SELECT * FROM brief_outbox WHERE delivery_id=?",
                             (intent.delivery_id,)).fetchone()
            revision = db.execute("SELECT * FROM brief_revisions WHERE revision_id=?",
                                  (intent.revision_id,)).fetchone()
            stamp = self._delivery_clock(clock)
            if (job is None or box is None or revision is None
                    or box["job_id"] != intent.job_id or revision["job_id"] != intent.job_id
                    or box["revision_id"] != intent.revision_id
                    or box["destination"] != intent.destination.address
                    or job["destination"] != intent.destination.address
                    or intent.wire_body != wire_text(revision["payload_json"])):
                raise Conflict("delivery does not match persisted private revision")
            self._forward(job, stamp)
            if old is not None:
                self._forward(old, stamp)
                if old["intent_json"] != encoded or old["intent_digest"] != digest:
                    raise Conflict("delivery replay changed frozen intent")
            else:
                if box["state"] != "pending" or job["state"] != "draft_ready":
                    raise Conflict("local delivery intent cancelled")
                db.execute("INSERT INTO brief_deliveries (delivery_id,job_id,revision_id,"
                           "namespace,platform,actor_id,intent_json,intent_digest,state,"
                           "created_us,updated_us) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                           (intent.delivery_id, intent.job_id, intent.revision_id,
                            *_scope(intent.scope), encoded, digest, "pending", stamp, stamp))
            result = self._delivery_view(db, worker, intent.scope, intent.delivery_id)
            self._delivery_final(clock, stamp, precommit)
            return result

    def _delivery_current(self, db, worker, intent, clock):
        from ostag.briefs.delivery import DeliveryIntent
        if type(intent) is not DeliveryIntent:
            raise Conflict("trusted delivery intent required")
        row = self._delivery_row(db, worker, intent.scope, intent.delivery_id)
        if row is None or row["intent_json"] != intent.encoded or row["intent_digest"] != intent.digest:
            raise Conflict("delivery identity mismatch")
        stamp = self._delivery_clock(clock)
        self._forward(row, stamp)
        job = self._find(db, intent.scope, intent.job_id)
        self._forward(job, stamp)
        box = db.execute("SELECT state FROM brief_outbox WHERE delivery_id=?",
                         (intent.delivery_id,)).fetchone()
        if box[0] != "pending" or job["state"] != "draft_ready":
            raise Conflict("local delivery intent cancelled")
        return row, stamp

    def begin_delivery(self, worker, intent, *, clock, precommit):
        """Atomic one-send claim. No lease, expiry, reset or resend operation exists."""
        from ostag.briefs.delivery import SendAttempt, _uncertainty
        with self._delivery_transaction() as db:
            row, stamp = self._delivery_current(db, worker, intent, clock)
            result = None
            if row["state"] == "pending":
                token = uuid4().hex + uuid4().hex
                db.execute("UPDATE brief_deliveries SET state='started',token=?,owner_id=?,"
                           "started_us=?,updated_us=?,uncertainty_json=? WHERE delivery_id=? AND state='pending'",
                           (token, _worker(worker), stamp, stamp,
                            _uncertainty("send_unverified", _stamp(stamp), row["intent_digest"]),
                            intent.delivery_id))
                result = SendAttempt(intent, token)
            self._delivery_final(clock, stamp, precommit)
            return result

    @staticmethod
    def _delivery_send_owner(row, worker, token):
        if (type(token) is not str or not token or row["token"] != token
                or row["owner_id"] != _worker(worker)):
            raise Conflict("send claim owner mismatch")

    @staticmethod
    def _delivery_observation_owner(db, row, worker, observation):
        from ostag.briefs.delivery import ObservationAttempt
        if (type(observation) is not ObservationAttempt
                or type(observation.ordinal) is not int
                or not 1 <= observation.ordinal <= row["read_attempts"]
                or type(observation.token) is not str
                or observation.intent.encoded != row["intent_json"]
                or observation.intent.digest != row["intent_digest"]
                or observation.message_id != row["candidate_id"]):
            raise Conflict("observation claim mismatch")
        claim = db.execute("SELECT * FROM brief_delivery_reads WHERE delivery_id=? AND ordinal=?",
                           (row["delivery_id"], observation.ordinal)).fetchone()
        if (claim is None or claim["settled"] or claim["owner_id"] != _worker(worker)
                or claim["token"] != observation.token
                or claim["candidate_id"] != observation.message_id):
            raise Conflict("observation claim is no longer current")

    @staticmethod
    def _settle_delivery_observation(db, observation):
        db.execute("UPDATE brief_delivery_reads SET settled=1 WHERE delivery_id=? AND ordinal=?",
                   (observation.intent.delivery_id, observation.ordinal))

    def check_delivery_egress(self, worker, intent, *, token, clock, precommit,
                              observation=None):
        """Recheck durable cancellation before local policy at scheduled/actual IO."""
        with self._delivery_transaction() as db:
            row, stamp = self._delivery_current(db, worker, intent, clock)
            if token is not None:
                self._delivery_send_owner(row, worker, token)
                if row["state"] != "started" or observation is not None:
                    raise Conflict("send claim is no longer current")
            else:
                self._delivery_observation_owner(db, row, worker, observation)
                if row["state"] not in ("started", "unknown"):
                    raise Conflict("exact observation identity unavailable")
            self._delivery_final(clock, stamp, precommit)

    @staticmethod
    def _delivery_note(db, row, stamp, reason):
        from ostag.briefs.delivery import REASONS, _json, _uncertainty
        if type(reason) is not str or reason not in REASONS:
            raise Conflict("invalid sanitized delivery reason")
        evidence = _json(dict(reason=reason, at=_stamp(stamp),
                              intent_digest=row["intent_digest"]))
        uncertainty = _uncertainty(reason, _stamp(stamp), row["intent_digest"])
        db.execute("UPDATE brief_deliveries SET state='unknown',updated_us=?,"
                   "first_failure_json=COALESCE(first_failure_json,?) WHERE delivery_id=? "
                   "AND state IN ('started','unknown')", (stamp, uncertainty, row["delivery_id"]))
        ordinal = db.execute("SELECT count(*) FROM brief_delivery_observations "
                             "WHERE delivery_id=?", (row["delivery_id"],)).fetchone()[0] + 1
        if ordinal <= 32:
            db.execute("INSERT INTO brief_delivery_observations VALUES (?,?,?)",
                       (row["delivery_id"], ordinal, evidence))

    def delivery_unknown(self, worker, scope, delivery_id, reason, *, clock,
                         token=None, observation=None):
        """Fixed diagnostic by actual custody only; never grants or reopens send.

        Send requires matching owner + opaque token. Observation requires its
        separate unconsumed read budget capability, consumed atomically here.
        Lost begin responses retain initial uncertainty without this mutation.
        """
        with self._delivery_transaction() as db:
            row = self._delivery_row(db, worker, scope, delivery_id)
            if row is None:
                raise Conflict("delivery identity mismatch")
            if observation is None:
                self._delivery_send_owner(row, worker, token)
            else:
                if token is not None:
                    raise Conflict("ambiguous delivery custody")
                self._delivery_observation_owner(db, row, worker, observation)
            if row["state"] not in ("started", "unknown"):
                return
            stamp = self._delivery_clock(clock)
            self._forward(row, stamp)
            self._delivery_note(db, row, stamp, reason)
            if observation is not None:
                self._settle_delivery_observation(db, observation)
            if self._delivery_clock(clock) < stamp:
                raise ClockError("delivery clock moved backwards")

    def begin_delivery_observation(self, worker, intent, *, clock, precommit):
        """One owned read budget unit, spent even if its COMMIT response is lost."""
        from ostag.briefs.delivery import ObservationAttempt
        with self._delivery_transaction() as db:
            row, stamp = self._delivery_current(db, worker, intent, clock)
            allowed = (row["state"] in ("started", "unknown")
                       and row["candidate_id"] is not None and row["read_attempts"] < 32)
            result = None
            if allowed:
                result = ObservationAttempt(intent, uuid4().hex + uuid4().hex,
                                            row["candidate_id"], row["read_attempts"] + 1)
                db.execute("INSERT INTO brief_delivery_reads "
                           "(delivery_id,ordinal,owner_id,token,candidate_id) VALUES (?,?,?,?,?)",
                           (intent.delivery_id, result.ordinal, _worker(worker), result.token,
                            result.message_id))
                db.execute("UPDATE brief_deliveries SET read_attempts=read_attempts+1,"
                           "updated_us=? WHERE delivery_id=?", (stamp, intent.delivery_id))
            self._delivery_final(clock, stamp, precommit)
            return result

    def _accept_delivery(self, worker, intent, candidate, messages, *, token, clock, precommit,
                         observation=None):
        from ostag.briefs.delivery import DeliveryIntent, _message_id, receipt
        if type(intent) is not DeliveryIntent:
            raise Conflict("trusted delivery intent required")
        # This private path receives only trusted correlated transport results.
        if candidate is not None:
            _message_id(candidate)
        evidence = receipt(intent, candidate, messages)
        with self._delivery_transaction() as db:
            row, stamp = self._delivery_current(db, worker, intent, clock)
            if row["state"] not in ("started", "unknown"):
                raise Conflict("delivery evidence state changed")
            if token is not None:
                self._delivery_send_owner(row, worker, token)
                if row["candidate_id"] is not None and row["candidate_id"] != candidate:
                    raise Conflict("candidate identity changed")
                if candidate is not None:
                    db.execute("UPDATE brief_deliveries SET candidate_id=? WHERE delivery_id=?",
                               (candidate, intent.delivery_id))
            else:
                self._delivery_observation_owner(db, row, worker, observation)
                if candidate is None or row["candidate_id"] != candidate:
                    raise Conflict("observation identity mismatch")
                self._settle_delivery_observation(db, observation)
            if evidence is None:
                reason = "missing_proof" if type(messages) is tuple and not messages else "invalid_proof"
                self._delivery_note(db, row, stamp, reason)
            else:
                db.execute("UPDATE brief_deliveries SET state='provider_accepted',receipt_json=?,"
                           "updated_us=? WHERE delivery_id=?", (evidence, stamp, intent.delivery_id))
            result = self._delivery_view(db, worker, intent.scope, intent.delivery_id)
            self._delivery_final(clock, stamp, precommit)
            return result

    def accept_delivery_response(self, worker, attempt, candidate, messages, *, clock, precommit):
        """Internal exact-attempt response only; candidate IDs are never requester arguments."""
        from ostag.briefs.delivery import SendAttempt
        if type(attempt) is not SendAttempt or type(attempt.token) is not str or not attempt.token:
            raise Conflict("trusted send claim required")
        return self._accept_delivery(worker, attempt.intent, candidate, messages,
                                     token=attempt.token, clock=clock, precommit=precommit)

    def accept_delivery_observation(self, worker, observation, messages, *, clock, precommit):
        """Owned read result only; exact identity AND full content required."""
        from ostag.briefs.delivery import ObservationAttempt
        if type(observation) is not ObservationAttempt:
            raise Conflict("trusted observation claim required")
        return self._accept_delivery(worker, observation.intent, observation.message_id, messages,
                                     token=None, observation=observation,
                                     clock=clock, precommit=precommit)

    def get_by_event(self, scope: RequesterContext, idempotency_key: str) -> Job | None:
        """Durable scoped lookup BEFORE constructing a new intake snapshot/clock."""
        params = _scope(scope)
        _identifier(idempotency_key, "idempotency_key")
        with self._transaction(write=False) as db:
            row = db.execute("SELECT * FROM brief_jobs WHERE namespace=? AND platform=? "
                             "AND actor_id=? AND idempotency_key=?",
                             (*params, idempotency_key)).fetchone()
            return None if row is None else _job(row)

    def matches_request(self, scope: RequesterContext, job_id: str, request_hash: str) -> bool:
        _hash(request_hash, "request_hash")
        with self._transaction(write=False) as db:
            if self._find(db, scope, job_id) is None:
                return False
            row = db.execute("SELECT request_hash FROM brief_intakes WHERE job_id=?",
                             (job_id,)).fetchone()
            return row is not None and row[0] == request_hash

    def failure_cause(self, scope: RequesterContext, job_id: str) -> str | None:
        with self._transaction(write=False) as db:
            if self._find(db, scope, job_id) is None:
                return None
            row = db.execute("SELECT cause FROM brief_failures WHERE job_id=?",
                             (job_id,)).fetchone()
            return None if row is None else row[0]

    @staticmethod
    def _failure(db, row, cause, stamp):
        if cause is not None:
            _failure_cause(cause)
            db.execute("INSERT OR IGNORE INTO brief_failures VALUES (?,?,?)",
                       (row["job_id"], cause, stamp))

    def record_expired_failure(self, worker: WorkerContext, expected: Job, *, cause: str,
                               clock: Callable[[], str] = utc_now,
                               retire_current: bool = False) -> bool:
        """Maintenance only: one exact detached-row CAS, never a stale owner write.

        May terminalize an expired attempt or annotate a failure terminal state.
        Concurrent renewal/recovery/cancellation abandons the CAS; never touches
        a ready revision or outbox. No private content is accepted here.
        retire_current additionally allows this trusted worker to retire its own
        exact active attempt after a local guard failure, even before lease expiry.
        A renewed/recovered row fails CAS; another owner's live row is forbidden.
        """
        owner = _worker(worker)
        if type(retire_current) is not bool:
            raise _invalid("retire_current is an internal explicit boolean")
        _failure_cause(cause)
        with self._transaction() as db:
            row = self._find(db, expected.scope, expected.job_id)
            stamp = _clock_micros(clock)
            if row is None or _job(row) != expected:
                return False
            self._forward(row, stamp)
            if row["state"] in _ACTIVE:
                if stamp < min(row["lease_us"], row["deadline_us"]) and not (
                    retire_current and row["owner_id"] == owner
                ):
                    return False
                self._terminal(db, row, "needs_input", stamp)
            elif row["state"] not in ("failed", "needs_input", "cancelled"):
                return False
            self._failure(db, row, cause, stamp)
            if _clock_micros(clock) < stamp:
                raise ClockError("maintenance clock moved backwards")
            return True

    @staticmethod
    def _find(db: sqlite3.Connection, scope: RequesterContext, job_id: str) -> sqlite3.Row | None:
        params = _scope(scope)
        _identifier(job_id, "job_id")
        return db.execute("SELECT * FROM brief_jobs WHERE namespace=? AND platform=? "
                          "AND actor_id=? AND job_id=?", (*params, job_id)).fetchone()

    def get(self, scope: RequesterContext, job_id: str) -> Job | None:
        """Requester-scoped, detached single-job read; identity mismatch is absent."""
        with self._transaction(write=False) as db:
            row = self._find(db, scope, job_id)
            return None if row is None else _job(row)

    def outbox(self, scope: RequesterContext, job_id: str) -> OutboxSnapshot | None:
        """Requester-scoped read of at most one revision and pending/cancelled intent."""
        with self._transaction(write=False) as db:
            job = self._find(db, scope, job_id)
            if job is None:
                return None
            row = db.execute("SELECT * FROM brief_outbox WHERE job_id=?", (job_id,)).fetchone()
            if row is None:
                return None
            revision = db.execute("SELECT * FROM brief_revisions WHERE revision_id=?",
                                  (row["revision_id"],)).fetchone()
            return OutboxSnapshot(row["delivery_id"], _job(job).destination,
                                  row["state"], _revision(revision))

    def admit(
        self, scope: RequesterContext, *, idempotency_key: str, payload: object,
        grant_id: str, grant_revision: int, destination: PrivateDestination,
        now: str, execution_seconds: int = MAX_EXECUTION_SECONDS,
        request_hash: str | None = None, precommit: Callable[[], None] | None = None,
    ) -> Job:
        """Atomically admit a trusted frozen snapshot as one queued job.

        Caller verifies current grant, accepted intake, and private destination.
        Equal canonical content replays even at capacity; admission time is not
        part of the content hash. Arbitrary payload flags convey no authority.
        """
        if request_hash is not None:
            _hash(request_hash, "request_hash")
        if precommit is not None and not callable(precommit):
            raise _invalid("precommit must be a trusted bounded callable")
        namespace, platform, actor = _scope(scope)
        _identifier(idempotency_key, "idempotency_key")
        _identifier(grant_id, "grant_id")
        _integer(grant_revision, "grant_revision")
        _integer(execution_seconds, "execution_seconds", MAX_EXECUTION_SECONDS)
        if type(destination) is not PrivateDestination or destination.principal != scope.principal:
            raise _invalid("destination must be explicitly requester-private")
        _identifier(destination.address, "destination")
        encoded = _canonical(payload)
        # Encode content separately from its bounded payload, allowing an exact
        # 1MiB payload without accidentally charging envelope bytes against it.
        content = json.dumps([namespace, platform, actor, encoded, grant_id, grant_revision,
                              destination.address, "requester_private", execution_seconds],
                             ensure_ascii=False, separators=(",", ":"))
        digest = hashlib.sha256(content.encode()).hexdigest()
        stamp = _micros(now)
        with self._transaction() as db:
            existing = db.execute("SELECT * FROM brief_jobs WHERE namespace=? AND platform=? "
                                  "AND actor_id=? AND idempotency_key=?",
                                  (namespace, platform, actor, idempotency_key)).fetchone()
            if existing is not None:
                if request_hash is not None:
                    binding = db.execute("SELECT request_hash FROM brief_intakes WHERE job_id=?",
                                         (existing["job_id"],)).fetchone()
                    matches = binding is not None and binding[0] == request_hash
                else:
                    matches = existing["content_hash"] == digest
                if not matches:
                    raise Conflict("idempotency key already binds different admission content")
                return _job(existing)
            if db.execute("SELECT count(*) FROM brief_jobs WHERE state='queued'").fetchone()[0] >= MAX_QUEUED:
                raise QueueFull("brief queue has 20 pending jobs; retry later")
            job_id = uuid4().hex
            db.execute("INSERT INTO brief_jobs(job_id,namespace,platform,actor_id,idempotency_key,"
                       "payload_json,content_hash,grant_id,grant_revision,destination,execution_seconds,"
                       "state,created_us,updated_us) VALUES (?,?,?,?,?,?,?,?,?,?,?,'queued',?,?)",
                       (job_id, namespace, platform, actor, idempotency_key, encoded, digest,
                        grant_id, grant_revision, destination.address, execution_seconds, stamp, stamp))
            if request_hash is not None:
                db.execute("INSERT INTO brief_intakes VALUES (?,?)", (job_id, request_hash))
            result = _job(self._find(db, scope, job_id))
            if precommit is not None and precommit() is not None:
                raise _invalid("precommit must raise on denial, not return approval flags")
            return result

    @staticmethod
    def _forward(row: sqlite3.Row, now: int) -> None:
        if now < row["updated_us"]:
            raise ClockError("clock precedes persisted job claim/renewal/update")

    @staticmethod
    def _live(db: sqlite3.Connection, now: int) -> bool:
        # Also fail on a clock rewind relative to any active attempt. It must
        # not make a future heartbeat look like permission for another lease.
        if db.execute("SELECT 1 FROM brief_jobs WHERE state IN ('collecting','generating') "
                      "AND updated_us>? LIMIT 1", (now,)).fetchone():
            raise ClockError("clock precedes an active attempt's last observation")
        return db.execute("SELECT 1 FROM brief_jobs WHERE state IN ('collecting','generating') "
                          "AND lease_us>? AND deadline_us>? LIMIT 1", (now, now)).fetchone() is not None

    @staticmethod
    def _claim(
        db: sqlite3.Connection, row: sqlite3.Row, owner: str, now: int, clock: Callable[[], str],
    ) -> Job:
        first = row["first_claim_us"] if row["first_claim_us"] is not None else now
        deadline = row["deadline_us"] if row["deadline_us"] is not None else (
            first + row["execution_seconds"] * 1_000_000)
        lease = min(now + LEASE_SECONDS * 1_000_000, deadline)
        # Validate conversion before persistence (e.g. datetime year overflow).
        try:
            _stamp(deadline)
        except OverflowError as exc:
            raise _invalid("execution deadline is outside supported UTC range") from exc
        db.execute("UPDATE brief_jobs SET state='collecting',attempt=attempt+1,fence=fence+1,"
                   "owner_id=?,first_claim_us=?,deadline_us=?,lease_us=?,updated_us=? WHERE job_id=?",
                   (owner, first, deadline, lease, now, row["job_id"]))
        claimed = db.execute("SELECT * FROM brief_jobs WHERE job_id=?",
                             (row["job_id"],)).fetchone()
        result = _job(claimed)
        BriefStore._fresh_attempt(claimed, clock, now)
        return result

    def claim_one(self, worker: WorkerContext, *, clock: Callable[[], str] = utc_now,
                  require_known_costs: bool = False) -> Job | None:
        """Trusted worker: claim at most one queued job, with one global live lease."""
        owner = _worker(worker)
        if type(require_known_costs) is not bool:
            raise _invalid("require_known_costs must be an internal explicit boolean")
        with self._transaction() as db:
            stamp = _clock_micros(clock)
            if require_known_costs and db.execute(
                "SELECT 1 FROM brief_cost_calls WHERE state IN ('started','unknown') LIMIT 1"
            ).fetchone() is not None:
                return None
            if self._live(db, stamp):
                return None
            row = db.execute("SELECT * FROM brief_jobs WHERE state='queued' ORDER BY rowid LIMIT 1").fetchone()
            if row is None:
                return None
            self._forward(row, stamp)
            return self._claim(db, row, owner, stamp, clock)

    @staticmethod
    def _fresh_attempt(row: sqlite3.Row, clock: Callable[[], str], observed: int) -> None:
        """Final acceptance check against the pre-mutation lease, even after terminalization.

        In particular, renew cannot use its own tentative extension to conceal
        expiry of the original lease while the transaction was doing work.
        """
        fresh = _clock_micros(clock)
        if fresh < observed:
            raise ClockError("attempt clock moved backwards during transaction")
        if fresh >= row["lease_us"] or fresh >= row["deadline_us"]:
            raise LeaseLost("attempt lease or hard deadline expired during transaction")

    @contextmanager
    def _attempt(
        self, lease: Lease, clock: Callable[[], str], *, states: tuple[str, ...] = _ACTIVE,
    ) -> Iterator[tuple[sqlite3.Connection, sqlite3.Row, int]]:
        with self._transaction() as db:
            stamp = _clock_micros(clock)
            row = self._current(db, lease, stamp, states=states)
            yield db, row, stamp
            self._fresh_attempt(row, clock, stamp)

    def _current(
        self, db: sqlite3.Connection, lease: Lease, now: int, *, states: tuple[str, ...] = _ACTIVE,
    ) -> sqlite3.Row:
        if type(lease) is not Lease:
            raise _invalid("explicit current Lease required")
        lease.__post_init__()
        row = self._find(db, lease.scope, lease.job_id)
        if (row is None or row["owner_id"] != lease.worker.owner_id
                or row["fence"] != lease.fence or row["state"] not in states):
            raise LeaseLost("attempt owner, scope, fence or phase is no longer current")
        self._forward(row, now)
        if now >= row["lease_us"] or now >= row["deadline_us"]:
            raise LeaseLost("attempt lease or hard deadline expired")
        return row

    def assert_current(self, lease: Lease, *, clock: Callable[[], str] = utc_now) -> Job:
        """Trusted worker checkpoint only; A04 must additionally reauthorize IO."""
        with self._attempt(lease, clock) as (db, row, stamp):
            return _job(row)

    def renew(self, lease: Lease, *, clock: Callable[[], str] = utc_now) -> Job:
        """Renew only a current unexpired attempt, never beyond its pinned deadline."""
        with self._attempt(lease, clock) as (db, row, stamp):
            until = min(stamp + LEASE_SECONDS * 1_000_000, row["deadline_us"])
            db.execute("UPDATE brief_jobs SET lease_us=?,updated_us=? WHERE job_id=?",
                       (until, stamp, lease.job_id))
            return _job(self._find(db, lease.scope, lease.job_id))

    def start_generating(self, lease: Lease, *, clock: Callable[[], str] = utc_now) -> Job:
        """Only collecting -> generating; source semantics belong to A03/A04."""
        with self._attempt(lease, clock, states=("collecting",)) as (db, row, stamp):
            db.execute("UPDATE brief_jobs SET state='generating',updated_us=? WHERE job_id=?",
                       (stamp, lease.job_id))
            return _job(self._find(db, lease.scope, lease.job_id))

    @staticmethod
    def _terminal(db: sqlite3.Connection, row: sqlite3.Row, state: str, now: int) -> None:
        db.execute("UPDATE brief_jobs SET state=?,fence=fence+1,lease_us=NULL,owner_id=NULL,"
                   "updated_us=? WHERE job_id=?", (state, now, row["job_id"]))

    def finish(
        self, lease: Lease, *, outcome: Literal["failed", "needs_input"],
        cause: str | None = None, clock: Callable[[], str] = utc_now,
    ) -> Job:
        """Trusted worker explicitly terminates a current attempt without a draft."""
        if type(outcome) is not str or outcome not in ("failed", "needs_input"):
            raise _invalid("worker outcome must be failed or needs_input")
        with self._attempt(lease, clock) as (db, row, stamp):
            self._terminal(db, row, outcome, stamp)
            self._failure(db, row, cause, stamp)
            return _job(self._find(db, lease.scope, lease.job_id))

    def cancel(
        self, scope: RequesterContext, job_id: str, *, clock: Callable[[], str] = utc_now,
    ) -> Job:
        """Cancel queued/active work or a ready draft's pending delivery; never unsend.

        Ready -> cancelled invalidates the pending intent while retaining the
        immutable historical revision. Failed/needs_input stay terminal.
        """
        with self._transaction() as db:
            stamp = _clock_micros(clock)
            row = self._find(db, scope, job_id)
            if row is None:
                raise Conflict("job not found in requester scope")
            self._forward(row, stamp)
            if row["state"] not in ("queued", *_ACTIVE, "draft_ready", "cancelled"):
                raise Conflict("cannot change this terminal job")
            if row["state"] != "cancelled":
                self._terminal(db, row, "cancelled", stamp)
                if (row["state"] != "draft_ready"
                        and db.execute("PRAGMA user_version").fetchone()[0] >= 3):
                    self._failure(db, row, "cancelled", stamp)
                db.execute("UPDATE brief_outbox SET state='cancelled' WHERE job_id=? "
                           "AND state='pending'", (job_id,))
            return _job(self._find(db, scope, job_id))

    def complete(
        self, lease: Lease, *, draft: object, provenance: object,
        validation_version: str, clock: Callable[[], str] = utc_now,
        precommit: Callable[[], None] | None = None,
    ) -> Revision:
        """Atomically store one frozen draft + pending outbox + draft_ready state.

        A04 must reauthorize the commit and A03/A04 must validate source/claim
        semantics first. Metadata is a trusted validation version and a bounded
        nonempty provenance object, not an approval flag. Duplicate completion
        deterministically raises LeaseLost (ready is terminal); read outbox in
        requester scope to reconcile a commit whose response was lost.
        """
        if precommit is not None and not callable(precommit):
            raise _invalid("precommit must be a trusted bounded callable")
        encoded, metadata = _canonical(draft), _canonical(provenance)
        if metadata == "{}":
            raise _invalid("nonempty provenance metadata required")
        _identifier(validation_version, "validation_version")
        with self._attempt(lease, clock, states=("generating",)) as (db, row, stamp):
            revision_id, delivery_id = uuid4().hex, uuid4().hex
            db.execute("INSERT INTO brief_revisions VALUES (?,?,?,?,?,?)",
                       (revision_id, lease.job_id, encoded, metadata, validation_version, stamp))
            db.execute("INSERT INTO brief_outbox VALUES (?,?,?,?,?)",
                       (delivery_id, lease.job_id, revision_id, row["destination"], "pending"))
            self._terminal(db, row, "draft_ready", stamp)
            # Assert the required relational effect, including silent SQL ignores.
            effect = db.execute(
                "SELECT 1 FROM brief_jobs j JOIN brief_revisions r ON r.job_id=j.job_id "
                "JOIN brief_outbox o ON o.job_id=j.job_id AND o.revision_id=r.revision_id "
                "WHERE j.job_id=? AND j.state='draft_ready' AND j.fence=? "
                "AND j.owner_id IS NULL AND j.lease_us IS NULL "
                "AND r.revision_id=? AND o.delivery_id=? AND o.state='pending' "
                "AND o.destination=j.destination",
                (lease.job_id, lease.fence + 1, revision_id, delivery_id),
            ).fetchone()
            if effect is None:
                raise StoreError("completion did not persist draft, pending outbox and terminal job")
            result = _revision(db.execute("SELECT * FROM brief_revisions WHERE revision_id=?",
                                          (revision_id,)).fetchone())
            # All relational writes/checks/materialization precede this logical
            # authorization acceptance point. Callback is trusted, bounded and
            # NON-REENTRANT: no store/guard access or arbitrary network IO here.
            # Only _attempt's fresh clock/lease check and COMMIT follow. This is
            # not cross-system atomicity with an external ACL/grant database.
            if precommit is not None and precommit() is not None:
                raise _invalid("precommit must raise on denial, not return approval flags")
            return result

    def recover_one(
        self, worker: WorkerContext, *, validate: Callable[[Job], RecoveryValidity],
        cause: str | None = None, clock: Callable[[], str] = utc_now,
        require_known_costs: bool = False,
    ) -> Job | None:
        """Trusted maintenance: inspect at most one expired attempt per call.

        Required validator runs outside all DB locks against a detached job.
        Re-read the entire row (snapshot/hash/grant/version/fence/timing) under
        the write lock before any effect; a concurrent change abandons this
        candidate. Recheck clock after validation, global concurrency, deadline
        and total attempts. Invalid checks -> needs_input; expired deadline or
        exhausted attempts -> failed. Validator errors propagate without claim.
        No validation default, no payload approval flags, no implicit new data.
        """
        owner = _worker(worker)
        if type(require_known_costs) is not bool:
            raise _invalid("require_known_costs must be an internal explicit boolean")
        if not callable(validate) or not callable(clock):
            raise _invalid("trusted recovery validator and clock must be callable")
        with self._transaction(write=False) as db:
            if require_known_costs and db.execute(
                "SELECT 1 FROM brief_cost_calls WHERE state IN ('started','unknown') LIMIT 1"
            ).fetchone() is not None:
                return None
            observed = _clock_micros(clock)
            if self._live(db, observed):
                return None
            row = db.execute("SELECT * FROM brief_jobs WHERE state IN ('collecting','generating') "
                             "AND (lease_us<=? OR deadline_us<=?) ORDER BY rowid LIMIT 1",
                             (observed, observed)).fetchone()
            if row is None:
                return None
            self._forward(row, observed)
            candidate = tuple(row)
            job = _job(row)
        validity = None
        if observed < row["deadline_us"] and row["attempt"] < MAX_ATTEMPTS:
            validity = validate(job)
            if type(validity) is not RecoveryValidity:
                raise _invalid("validator must return explicit RecoveryValidity")
            validity.__post_init__()
        with self._transaction() as db:
            if require_known_costs and db.execute(
                "SELECT 1 FROM brief_cost_calls WHERE state IN ('started','unknown') LIMIT 1"
            ).fetchone() is not None:
                return None
            stamp = _clock_micros(clock)
            if stamp < observed:
                raise ClockError("recovery clock moved backwards during validation")
            current = self._find(db, job.scope, job.job_id)
            if current is None or tuple(current) != candidate:
                return None
            self._forward(current, stamp)
            if stamp >= current["deadline_us"] or current["attempt"] >= MAX_ATTEMPTS:
                self._terminal(db, current, "failed", stamp)
                self._failure(db, current, cause, stamp)
                return None
            if validity is None or not validity.valid:
                self._terminal(db, current, "needs_input", stamp)
                self._failure(db, current, cause, stamp)
                return None
            if self._live(db, stamp):
                return None
            return self._claim(db, current, owner, stamp, clock)

    @staticmethod
    def _cost_schema(db: sqlite3.Connection) -> None:
        if db.execute("PRAGMA user_version").fetchone()[0] not in (2, 3, 4):
            raise BriefSchemaError("cost operations require schema2; reopen for forward migration")

    @staticmethod
    def _cost_row(db: sqlite3.Connection, reservation_id: str) -> sqlite3.Row:
        _identifier(reservation_id, "reservation_id")
        row = db.execute("SELECT * FROM brief_cost_calls WHERE reservation_id=?",
                         (reservation_id,)).fetchone()
        if row is None:
            raise Conflict("unknown cost reservation")
        return row

    @staticmethod
    def _cost_clock(db: sqlite3.Connection, namespace: str, stamp: int) -> None:
        if db.execute("SELECT 1 FROM brief_cost_calls c JOIN brief_jobs j ON j.job_id=c.job_id "
                      "WHERE j.namespace=? AND c.updated_us>? LIMIT 1",
                      (namespace, stamp)).fetchone():
            raise ClockError("clock precedes namespace cost accounting observation")

    @staticmethod
    def _cost_capacity(db: sqlite3.Connection, job: sqlite3.Row, day: str,
                       profile: CostProfile, daily_limit: int, additional: int,
                       new_call: bool) -> None:
        job_rows = db.execute("SELECT * FROM brief_cost_calls WHERE job_id=?",
                              (job["job_id"],)).fetchall()
        day_rows = db.execute(
            "SELECT c.* FROM brief_cost_calls c JOIN brief_jobs j ON j.job_id=c.job_id "
            "WHERE j.namespace=? AND c.charged_day=?", (job["namespace"], day)).fetchall()
        # Python integer sums cannot overflow even across arbitrarily many jobs.
        for rows, cap in ((job_rows, profile.max_job_micros), (day_rows, daily_limit)):
            records = [_reservation(row) for row in rows]
            if any(r.overrun for r in records) or sum(r.charged_micros for r in records) + additional > cap:
                raise BudgetDenied("job or namespace UTC-day cost cap exhausted/overrun")
        calls = sum(row["state"] != "released" for row in job_rows)
        if calls + int(new_call) > profile.max_calls:
            raise BudgetDenied("cumulative job model-call limit exhausted")

    @staticmethod
    def _cost_day_fence(clock: Callable[[], str], observed: int, day: str) -> None:
        fresh = _clock_micros(clock)
        if fresh < observed:
            raise ClockError("cost clock moved backwards")
        if _stamp(fresh)[:10] != day:
            raise BudgetDenied("UTC day changed before cost admission/start committed")

    @contextmanager
    def _cost_attempt(self, lease: Lease, clock: Callable[[], str]):
        # Apply the day fence to A02's LAST clock sample as well, rather than
        # checking the day just before a later lease-only acceptance sample.
        charged_day = None
        def same_day_clock():
            nonlocal charged_day
            value = _stamp(_clock_micros(clock))
            day = value[:10]
            if charged_day is not None and charged_day != day:
                raise BudgetDenied("UTC day changed at the final cost commit fence")
            charged_day = day
            return value
        with self._attempt(lease, same_day_clock, states=("generating",)) as current:
            yield current

    def reserve_cost(self, lease: Lease, *, call_id: str, profile: CostProfile,
                     daily_limit_micros: int, config_hash: str,
                     clock: Callable[[], str] = utc_now) -> CostReservation:
        """Reserve a conservative maximum before IO, bound to the stored namespace.

        First use pins the entire profile for this JOB, across fences/configs and
        restarts. The daily cap is a current trusted admission value, not a ledger
        partition. A repeated call ID is a tombstone, including after release;
        only start_cost's single successful transition permits a provider call.
        """
        _identifier(call_id, "call_id")
        _hash(config_hash, "config_hash")
        _cost_int(daily_limit_micros, "daily_limit_micros", minimum=1)
        if type(profile) is not CostProfile:
            raise _invalid("explicit immutable CostProfile required")
        profile.__post_init__()
        with self._cost_attempt(lease, clock) as (db, job, stamp):
            self._cost_schema(db)
            self._cost_clock(db, job["namespace"], stamp)
            pinned = db.execute("SELECT profile_json FROM brief_cost_profiles WHERE job_id=?",
                                (job["job_id"],)).fetchone()
            if pinned is not None and pinned[0] != profile.profile_json:
                raise Conflict("job already pins a different budget/evaluation profile")
            day = _stamp(stamp)[:10]
            existing = db.execute("SELECT * FROM brief_cost_calls WHERE job_id=? AND call_id=?",
                                  (job["job_id"], call_id)).fetchone()
            if existing is not None:
                if existing["config_hash"] != config_hash:
                    raise Conflict("logical call already pins a different config audit hash")
                return _reservation(existing)
            self._cost_capacity(db, job, day, profile, daily_limit_micros,
                                profile.reservation_micros, True)
            if pinned is None:
                db.execute("INSERT INTO brief_cost_profiles VALUES (?,?)",
                           (job["job_id"], profile.profile_json))
            reservation_id = uuid4().hex
            db.execute("INSERT INTO brief_cost_calls VALUES (?,?,?,?,?,?,?,'reserved',NULL,"
                       "'reserved',?,?,?,?)", (reservation_id, job["job_id"], call_id,
                       lease.fence, lease.worker.owner_id, day, profile.reservation_micros,
                       config_hash, daily_limit_micros, stamp, stamp))
            result = _reservation(self._cost_row(db, reservation_id))
            self._cost_day_fence(clock, stamp, day)
            return result

    def start_cost(self, lease: Lease, reservation_id: str, *, daily_limit_micros: int,
                   clock: Callable[[], str] = utc_now) -> CostReservation:
        """At most one caller starts a logical call. Never start a prior-day reserve.

        Admission is linearized under the EXCLUSIVE lock, with a final fresh day
        and lease check. A04 must recheck current authorization AFTER this and any
        other blocking admission/lease work, at the transport invocation boundary.
        Arbitrary scheduling after return cannot be made atomic with remote IO;
        adapters must also honor remaining time/cancellation and the charged day.
        """
        _cost_int(daily_limit_micros, "daily_limit_micros", minimum=1)
        with self._cost_attempt(lease, clock) as (db, job, stamp):
            self._cost_schema(db)
            self._cost_clock(db, job["namespace"], stamp)
            row = self._cost_row(db, reservation_id)
            if (row["job_id"] != lease.job_id or row["fence"] != lease.fence
                    or row["owner_id"] != lease.worker.owner_id or row["state"] != "reserved"):
                raise Conflict("logical call already consumed or belongs to a different lease")
            self._forward(row, stamp)
            day = _stamp(stamp)[:10]
            if row["charged_day"] != day:
                raise BudgetDenied("reservation belongs to a previous UTC day; no IO")
            profile = CostProfile(**json.loads(db.execute(
                "SELECT profile_json FROM brief_cost_profiles WHERE job_id=?",
                (lease.job_id,)).fetchone()[0]))
            self._cost_capacity(db, job, day, profile, daily_limit_micros, 0, False)
            db.execute("UPDATE brief_cost_calls SET state='started',diagnostic='started',"
                       "daily_limit_micros=?,updated_us=? WHERE reservation_id=?",
                       (daily_limit_micros, stamp, reservation_id))
            result = _reservation(self._cost_row(db, reservation_id))
            self._cost_day_fence(clock, stamp, day)
            return result

    def has_uncertain_cost(self, worker: WorkerContext) -> bool:
        """Trusted custody gate, global across namespaces and worker restarts."""
        _worker(worker)
        with self._transaction(write=False) as db:
            self._cost_schema(db)
            return db.execute(
                "SELECT 1 FROM brief_cost_calls WHERE state IN ('started','unknown') LIMIT 1"
            ).fetchone() is not None

    def cost_record(self, scope: RequesterContext, job_id: str, call_id: str) -> CostReservation | None:
        _identifier(call_id, "call_id")
        with self._transaction(write=False) as db:
            self._cost_schema(db)
            if self._find(db, scope, job_id) is None:
                return None
            row = db.execute("SELECT * FROM brief_cost_calls WHERE job_id=? AND call_id=?",
                             (job_id, call_id)).fetchone()
            return None if row is None else _reservation(row)

    def _cost_evidence(self, worker: WorkerContext, reservation_id: str, *, action: str,
                       usage: Usage | None, diagnostic: str,
                       clock: Callable[[], str]) -> CostReservation:
        _worker(worker)
        _identifier(diagnostic, "cost diagnostic")
        with self._transaction() as db:
            self._cost_schema(db)
            stamp = _clock_micros(clock)
            row = self._cost_row(db, reservation_id)
            self._forward(row, stamp)
            state, encoded = row["state"], row["usage_json"]
            if action == "released":
                if state not in ("reserved", "released"):
                    raise Conflict("cannot release a possibly started call")
            elif action == "settled":
                if state not in ("started", "unknown", "settled"):
                    raise Conflict("only a started call can have billable usage evidence")
                encoded = _canonical(usage.to_dict())
                if state == "settled" and encoded != row["usage_json"]:
                    raise Conflict("contradictory repeated usage settlement")
            elif state not in ("started", "unknown", "settled"):
                raise Conflict("unknown outcome requires a started call")
            # Reconciliation never refunds settled evidence or rewrites its first
            # diagnostic on an identical settlement. Additional rejection reasons
            # can be attached without changing known money/usage.
            if action == state and (action != "unknown" or diagnostic == row["diagnostic"]):
                return _reservation(row)
            target = "settled" if state == "settled" else action
            db.execute("UPDATE brief_cost_calls SET state=?,usage_json=?,diagnostic=?,updated_us=? "
                       "WHERE reservation_id=?", (target, encoded, diagnostic, stamp, reservation_id))
            result = _reservation(self._cost_row(db, reservation_id))
            if _clock_micros(clock) < stamp:
                raise ClockError("reconciliation clock moved backwards")
            return result

    def settle_cost(self, worker: WorkerContext, reservation_id: str, usage: Usage, *,
                    clock: Callable[[], str] = utc_now) -> CostReservation:
        """Bounded trusted reconciliation: one reservation, exact usage, once.

        May record late provider evidence after cancel/lease loss, in its ORIGINAL
        charged bucket. WorkerContext is an internal capability, not an endpoint
        credential. A04 must verify evidence belongs to this reservation/provider
        call. This never changes a job, authorizes IO or permits draft completion.
        Contradictory settlement is rejected; an overrun is recorded without clamp
        and blocks new calls for that job and charged namespace/day.
        """
        if type(usage) is not Usage:
            raise _invalid("explicit trusted Usage required")
        usage.__post_init__()
        return self._cost_evidence(worker, reservation_id, action="settled", usage=usage,
                                   diagnostic="settled", clock=clock)

    def mark_cost_unknown(self, worker: WorkerContext, reservation_id: str, diagnostic: str, *,
                          clock: Callable[[], str] = utc_now) -> CostReservation:
        """Retain the maximum after timeout/cancel/missing usage; never auto-refund."""
        return self._cost_evidence(worker, reservation_id, action="unknown", usage=None,
                                   diagnostic=diagnostic, clock=clock)

    def release_cost(self, worker: WorkerContext, reservation_id: str, *,
                     clock: Callable[[], str] = utc_now) -> CostReservation:
        """Release only proven unstarted IO, retaining the call ID tombstone."""
        return self._cost_evidence(worker, reservation_id, action="released", usage=None,
                                   diagnostic="precall_cancelled", clock=clock)


class LeaseGuard:
    """Independent heartbeat context for a trusted worker's one active attempt.

    Default 10s interval vs 30s lease; each renewal opens its own connection.
    FIRST renewal/checkpoint/commit error latches loss and signals stop_requested,
    including malformed completion input: prepare/validate drafts before commit.
    Caller must use before_io/assert_current before EVERY new external IO and
    this guard's complete method for results, plus A04 current grant checks.
    Direct store.complete still enforces persisted lease/fence, but cannot know
    about a process-local heartbeat error that did not alter the database.

    Optional retry_busy=True is A04-only database contention tolerance. Only
    StoreBusy can retry, and only until the last CONFIRMED lease/original deadline
    in both trusted wall and elapsed monotonic time. Checkpoints/completion wait
    for a successful fenced transaction; no unavailable check authorizes IO.
    A retry samples the bounded clock again inside the transaction before renewal
    and precommit, so lock waits cannot silently renew an expired lease.
    A04 supplies confirmed_job directly from the committed claim, seeding bounds
    BEFORE the first checkpoint. Without that snapshot the first busy checkpoint
    fails closed. The seed limits waiting only; it never authorizes IO by itself.

    A04 must observe stop_requested and stop new IO on loss. This layer cannot
    retract an already-running call. The clock must
    be a nonblocking thread-safe trusted callable; no model/user callback is run
    here. Exit signals stop and joins with a bounded wait; a stuck heartbeat is
    reported as an error rather than silently declaring successful shutdown.
    """

    def __init__(
        self, store: BriefStore, lease: Lease, *, clock: Callable[[], str] = utc_now,
        interval: float = 10.0, retry_busy: bool = False,
        confirmed_job: Job | None = None, stop_signal: Event | None = None,
    ) -> None:
        self.interval = _seconds(interval, "heartbeat interval", 10)
        if type(retry_busy) is not bool:
            raise _invalid("retry_busy must be an explicit internal boolean")
        self.retry_busy = retry_busy
        self._confirmed_until: int | None = None
        self._lease_monotonic_until: float | None = None
        self._deadline_monotonic_until: float | None = None
        self._observed: int | None = None
        if type(lease) is not Lease or not callable(clock):
            raise _invalid("guard requires a Lease and callable trusted clock")
        lease.__post_init__()
        self.store, self.lease, self.clock = store, lease, clock
        self._stop_signal = stop_signal
        if confirmed_job is not None:
            if not retry_busy or type(confirmed_job) is not Job or confirmed_job.lease != lease:
                raise _invalid("busy bounds require this attempt's committed job snapshot")
            self._observed = _micros(confirmed_job.updated_at)
            self._confirm(confirmed_job)
        self.stop_requested = Event()
        self._failure: Exception | None = None
        self._lock = Lock()
        self._thread: Thread | None = None
        self._closed = False
        self._completed = False

    @property
    def lost(self) -> bool:
        return self._failure is not None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _lose(self, exc: Exception) -> None:
        if self._failure is None:
            self._failure = exc
        self.stop_requested.set()

    def _check_open(self) -> None:
        if (self.lost or self._closed or self._completed or self._thread is None
                or self.stop_requested.is_set()
                or (self._stop_signal is not None and self._stop_signal.is_set())):
            raise LeaseLost("lease guard stopped or lost; discard result and stop IO") from self._failure

    def __enter__(self) -> LeaseGuard:
        if self._thread is not None or self._closed:
            raise Conflict("lease guard is single-use")
        self._thread = Thread(target=self._heartbeat, name="brief-lease-heartbeat", daemon=True)
        try:
            self.assert_current()
            self._thread.start()
        except Exception:
            self._closed = True
            raise
        return self

    def _bounded_clock(self) -> str:
        """Clock-only bound, also used inside SQLite after waits and precommit.

        A04 opt-in only. Busy never extends the last confirmed lease. Monotonic
        time prevents a stalled wall clock from extending grace indefinitely;
        invalid/backward clocks and actual expiry always latch loss.
        """
        value = self.clock()
        now = _micros(value)
        if self._observed is not None and now < self._observed:
            raise ClockError("guard clock moved backwards")
        self._observed = now
        if self._confirmed_until is not None and (
            now >= self._confirmed_until
            or monotonic() >= self._lease_monotonic_until
            or monotonic() >= self._deadline_monotonic_until
        ):
            raise LeaseLost("last confirmed lease or original deadline expired")
        return value

    def _confirm(self, job: Job) -> None:
        if not self.retry_busy:
            return
        # Confirm only after a successful fenced DB transaction. Never refresh
        # the monotonic bound merely because a read or failed renewal returned.
        now = _micros(self._bounded_clock())
        until = min(_micros(job.lease_until), _micros(job.deadline))
        tick = monotonic()
        if now >= until:
            raise LeaseLost("confirmed lease expired")
        if self._deadline_monotonic_until is None:
            self._deadline_monotonic_until = tick + (_micros(job.deadline) - now) / 1_000_000
        if self._confirmed_until is None or until > self._confirmed_until:
            bound = tick + (until - now) / 1_000_000
            if self._confirmed_until is not None:
                # A partially stalled wall clock cannot rebase elapsed time;
                # renewal buys only the extension actually persisted in SQLite.
                bound = min(bound, self._lease_monotonic_until
                            + (until - self._confirmed_until) / 1_000_000)
            self._lease_monotonic_until = min(bound, self._deadline_monotonic_until)
        self._confirmed_until = until

    def _heartbeat(self) -> None:
        while not self.stop_requested.wait(self.interval):
            with self._lock:
                if self.stop_requested.is_set():
                    return
                try:
                    clock = self._bounded_clock if self.retry_busy else self.clock
                    if self.retry_busy:
                        clock()
                    job = self.store.renew(self.lease, clock=clock)
                    self._confirm(job)
                except StoreBusy as exc:
                    if self.retry_busy:
                        try:
                            self._bounded_clock()
                        except Exception as expired:
                            self._lose(expired)
                            return
                        # Retry only heartbeat, on its configured cadence. No
                        # checkpoint succeeds and no new IO is authorized here.
                        continue
                    self._lose(exc)
                    return
                except Exception as exc:
                    self._lose(exc)
                    return

    def assert_current(self) -> Job:
        """Mandatory new-IO checkpoint; failure latches, never resets to success."""
        with self._lock:
            self._check_open()
            try:
                job = self._database(self.store.assert_current, self.lease)
                self._confirm(job)
                return job
            except Exception as exc:
                self._lose(exc)
                raise LeaseLost("lease guard checkpoint failed; stop IO") from exc

    def before_io(self) -> Job:
        return self.assert_current()

    def _database(self, operation, *args, evidence=False, clocked=True, **kwargs):
        """Caller owns _lock. Retry rolled-back StoreBusy only, never provider IO.

        Evidence gets one ordinary reconciliation attempt even after lease loss;
        it cannot authorize a draft. Further attempts require live confirmed
        bounds. Fenced work uses the bounded clock inside every transaction,
        including after lock acquisition and immediately before COMMIT.
        """
        clock = self.clock if evidence or not self.retry_busy else self._bounded_clock
        while True:
            if not evidence:
                self._check_open()
                if self.retry_busy:
                    self._bounded_clock()
            try:
                if clocked:
                    return operation(*args, clock=clock, **kwargs)
                return operation(*args, **kwargs)
            except StoreBusy:
                if not self.retry_busy or self._confirmed_until is None:
                    raise
                self._check_open()
                self._bounded_clock()
                self.stop_requested.wait(min(self.interval, 0.05))
                self._check_open()
                self._bounded_clock()

    def database(self, operation, *args, evidence=False, clocked=True, **kwargs):
        """Internal A04 generation persistence only; never policy or transport.

        A03 still owns reservation, response validation and usage/provenance.
        This does not retry generation. No result is accepted on a failed read.
        Evidence may settle money after loss, but never renews the lease.
        """
        with self._lock:
            try:
                return self._database(operation, *args, evidence=evidence, clocked=clocked, **kwargs)
            except (LeaseLost, ClockError) as exc:
                self._lose(exc)
                raise

    def complete(
        self, *, draft: object, provenance: object, validation_version: str,
        precommit: Callable[[], None] | None = None,
    ) -> Revision:
        """Commit only while guard and DB lease are current; then stop heartbeat."""
        with self._lock:
            self._check_open()
            try:
                revision = self._database(self.store.complete, self.lease,
                                               draft=draft, provenance=provenance,
                                               validation_version=validation_version,
                                               precommit=precommit)
            except Exception as exc:
                self._lose(exc)
                raise
            self._completed = True
            self.stop_requested.set()
            return revision

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self._closed = True
        self.stop_requested.set()
        if self._thread is not None and self._thread.ident is not None:
            self._thread.join(timeout=self.store.busy_timeout + 1.0)
            if self._thread.is_alive():
                failure = StoreError("heartbeat did not stop within bounded join timeout")
                self._lose(failure)
                raise failure from exc if isinstance(exc, BaseException) else None
