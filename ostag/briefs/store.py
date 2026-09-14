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
SQLite; no authorization/model/user callback runs under a database lock and no
new source data replaces an admitted snapshot. A03/A04 own semantic validation
and current authorization. Trusted nonblocking clocks are sampled under locks.

One job is one queue item; one initial attempt plus two expired-attempt retries,
one live lease globally. A completion only queues one requester-private outbox
intent. It does not send, acknowledge, or promise exactly-once delivery. U01
must add the send fence/receipt/uncertain-result protocol; M06 owns collection
progress. No Monitor schema, server lifecycle or provider integration lives here.
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
from types import MappingProxyType
from typing import Literal, TypeAlias
from uuid import uuid4

from ostag.briefs.models import (
    _identifier, _invalid, _positive_int, _principal, _utc,
)
from ostag.core.identity import Principal

CURRENT_SCHEMA_VERSION = 1
READABLE_SCHEMA_MIN = 1
READABLE_SCHEMA_MAX = 1
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
    All mutations serialize with BEGIN IMMEDIATE. Lease operations take a trusted,
    nonblocking, thread-safe UTC clock callable (default utc_now), sampled AFTER
    acquiring the write lock, never a timestamp captured before lock contention.
    Test clocks must advance consistently across all handles/guard threads.
    Only this operational clock runs under the lock; grant/model/user callbacks
    must never be supplied as clocks. Admission's now is an intake timestamp,
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
            self._inspect(db, allow_empty=True)
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("BEGIN IMMEDIATE")
            fresh = self._inspect(db, allow_empty=True)  # serialize competing initializers
            if fresh:
                for statement in _SCHEMA:
                    db.execute(statement)
                db.execute(f"PRAGMA application_id={APPLICATION_ID}")
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
        if app == 0 and version == 0 and not rows and allow_empty:
            return True
        if app != APPLICATION_ID:
            raise BriefSchemaError("not an owned brief database; refusing mutation")
        if not READABLE_SCHEMA_MIN <= version <= READABLE_SCHEMA_MAX:
            raise BriefSchemaError(f"brief schema {version} outside readable interval "
                                   f"[{READABLE_SCHEMA_MIN}, {READABLE_SCHEMA_MAX}]")
        actual = {_normalized(row["sql"]) for row in rows
                  if row["sql"] is not None}
        if actual != {_normalized(sql) for sql in _SCHEMA}:
            raise BriefSchemaError("partial or unexpected brief schema; refusing mutation")
        return False

    @contextmanager
    def _transaction(self, *, write: bool = True) -> Iterator[sqlite3.Connection]:
        with self._connection() as db:
            self._inspect(db)
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            self._inspect(db)
            yield db
            db.commit()

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
    ) -> Job:
        """Atomically admit a trusted frozen snapshot as one queued job.

        Caller verifies current grant, accepted intake, and private destination.
        Equal canonical content replays even at capacity; admission time is not
        part of the content hash. Arbitrary payload flags convey no authority.
        """
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
                if existing["content_hash"] != digest:
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
            return _job(self._find(db, scope, job_id))

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
    def _claim(db: sqlite3.Connection, row: sqlite3.Row, owner: str, now: int) -> Job:
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
        return _job(db.execute("SELECT * FROM brief_jobs WHERE job_id=?",
                               (row["job_id"],)).fetchone())

    def claim_one(self, worker: WorkerContext, *, clock: Callable[[], str] = utc_now) -> Job | None:
        """Trusted worker: claim at most one queued job, with one global live lease."""
        owner = _worker(worker)
        with self._transaction() as db:
            stamp = _clock_micros(clock)
            if self._live(db, stamp):
                return None
            row = db.execute("SELECT * FROM brief_jobs WHERE state='queued' ORDER BY rowid LIMIT 1").fetchone()
            if row is None:
                return None
            self._forward(row, stamp)
            return self._claim(db, row, owner, stamp)

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
        with self._transaction() as db:
            stamp = _clock_micros(clock)
            return _job(self._current(db, lease, stamp))

    def renew(self, lease: Lease, *, clock: Callable[[], str] = utc_now) -> Job:
        """Renew only a current unexpired attempt, never beyond its pinned deadline."""
        with self._transaction() as db:
            stamp = _clock_micros(clock)
            row = self._current(db, lease, stamp)
            until = min(stamp + LEASE_SECONDS * 1_000_000, row["deadline_us"])
            db.execute("UPDATE brief_jobs SET lease_us=?,updated_us=? WHERE job_id=?",
                       (until, stamp, lease.job_id))
            return _job(self._find(db, lease.scope, lease.job_id))

    def start_generating(self, lease: Lease, *, clock: Callable[[], str] = utc_now) -> Job:
        """Only collecting -> generating; source semantics belong to A03/A04."""
        with self._transaction() as db:
            stamp = _clock_micros(clock)
            self._current(db, lease, stamp, states=("collecting",))
            db.execute("UPDATE brief_jobs SET state='generating',updated_us=? WHERE job_id=?",
                       (stamp, lease.job_id))
            return _job(self._find(db, lease.scope, lease.job_id))

    @staticmethod
    def _terminal(db: sqlite3.Connection, row: sqlite3.Row, state: str, now: int) -> None:
        db.execute("UPDATE brief_jobs SET state=?,fence=fence+1,lease_us=NULL,owner_id=NULL,"
                   "updated_us=? WHERE job_id=?", (state, now, row["job_id"]))

    def finish(
        self, lease: Lease, *, outcome: Literal["failed", "needs_input"],
        clock: Callable[[], str] = utc_now,
    ) -> Job:
        """Trusted worker explicitly terminates a current attempt without a draft."""
        if type(outcome) is not str or outcome not in ("failed", "needs_input"):
            raise _invalid("worker outcome must be failed or needs_input")
        with self._transaction() as db:
            stamp = _clock_micros(clock)
            row = self._current(db, lease, stamp)
            self._terminal(db, row, outcome, stamp)
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
                db.execute("UPDATE brief_outbox SET state='cancelled' WHERE job_id=? "
                           "AND state='pending'", (job_id,))
            return _job(self._find(db, scope, job_id))

    def complete(
        self, lease: Lease, *, draft: object, provenance: object,
        validation_version: str, clock: Callable[[], str] = utc_now,
    ) -> Revision:
        """Atomically store one frozen draft + pending outbox + draft_ready state.

        A04 must reauthorize the commit and A03/A04 must validate source/claim
        semantics first. Metadata is a trusted validation version and a bounded
        nonempty provenance object, not an approval flag. Duplicate completion
        deterministically raises LeaseLost (ready is terminal); read outbox in
        requester scope to reconcile a commit whose response was lost.
        """
        encoded, metadata = _canonical(draft), _canonical(provenance)
        if metadata == "{}":
            raise _invalid("nonempty provenance metadata required")
        _identifier(validation_version, "validation_version")
        with self._transaction() as db:
            stamp = _clock_micros(clock)
            row = self._current(db, lease, stamp, states=("generating",))
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
            return _revision(db.execute("SELECT * FROM brief_revisions WHERE revision_id=?",
                                        (revision_id,)).fetchone())

    def recover_one(
        self, worker: WorkerContext, *, validate: Callable[[Job], RecoveryValidity],
        clock: Callable[[], str] = utc_now,
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
        if not callable(validate) or not callable(clock):
            raise _invalid("trusted recovery validator and clock must be callable")
        with self._transaction(write=False) as db:
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
            stamp = _clock_micros(clock)
            if stamp < observed:
                raise ClockError("recovery clock moved backwards during validation")
            current = self._find(db, job.scope, job.job_id)
            if current is None or tuple(current) != candidate:
                return None
            self._forward(current, stamp)
            if stamp >= current["deadline_us"] or current["attempt"] >= MAX_ATTEMPTS:
                self._terminal(db, current, "failed", stamp)
                return None
            if validity is None or not validity.valid:
                self._terminal(db, current, "needs_input", stamp)
                return None
            if self._live(db, stamp):
                return None
            return self._claim(db, current, owner, stamp)


class LeaseGuard:
    """Independent heartbeat context for a trusted worker's one active attempt.

    Default 10s interval vs 30s lease; each renewal opens its own connection.
    FIRST renewal/checkpoint/commit error latches loss and signals stop_requested,
    including malformed completion input: prepare/validate drafts before commit.
    Caller must use before_io/assert_current before EVERY new external IO and
    this guard's complete method for results, plus A04 current grant checks.
    Direct store.complete still enforces persisted lease/fence, but cannot know
    about a process-local heartbeat error that did not alter the database.

    A04 must observe stop_requested, stop new IO and kill/reap a provider process
    on loss. This layer cannot retract an already-running call. The clock must
    be a nonblocking thread-safe trusted callable; no model/user callback is run
    here. Exit signals stop and joins with a bounded wait; a stuck heartbeat is
    reported as an error rather than silently declaring successful shutdown.
    """

    def __init__(
        self, store: BriefStore, lease: Lease, *, clock: Callable[[], str] = utc_now,
        interval: float = 10.0,
    ) -> None:
        self.interval = _seconds(interval, "heartbeat interval", 10)
        if type(lease) is not Lease or not callable(clock):
            raise _invalid("guard requires a Lease and callable trusted clock")
        lease.__post_init__()
        self.store, self.lease, self.clock = store, lease, clock
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
        if self.lost or self._closed or self._completed or self._thread is None:
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

    def _heartbeat(self) -> None:
        while not self.stop_requested.wait(self.interval):
            with self._lock:
                if self.stop_requested.is_set():
                    return
                try:
                    self.store.renew(self.lease, clock=self.clock)
                except Exception as exc:
                    self._lose(exc)
                    return

    def assert_current(self) -> Job:
        """Mandatory new-IO checkpoint; failure latches, never resets to success."""
        with self._lock:
            self._check_open()
            try:
                return self.store.assert_current(self.lease, clock=self.clock)
            except Exception as exc:
                self._lose(exc)
                raise LeaseLost("lease guard checkpoint failed; stop IO") from exc

    def before_io(self) -> Job:
        return self.assert_current()

    def complete(
        self, *, draft: object, provenance: object, validation_version: str,
    ) -> Revision:
        """Commit only while guard and DB lease are current; then stop heartbeat."""
        with self._lock:
            self._check_open()
            try:
                revision = self.store.complete(self.lease, draft=draft, provenance=provenance,
                                               validation_version=validation_version,
                                               clock=self.clock)
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
