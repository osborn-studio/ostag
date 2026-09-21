"""A02 persistence contracts; synthetic data, no service or delivery wiring."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
import sqlite3
import subprocess
import sys
from threading import Barrier
from time import monotonic, sleep

import pytest

from ostag.briefs.store import (
    APPLICATION_ID, CURRENT_SCHEMA_VERSION, READABLE_SCHEMA_MIN, READABLE_SCHEMA_MAX,
    BriefStore, BriefSchemaError, StoreError, StoreBusy, Conflict, QueueFull,
    LeaseLost, RequesterContext, WorkerContext, PrivateDestination,
)
from ostag.core.identity import Principal
from ostag.core.schemas import SchemaError
from ostag.work_monitor.store import MonitorStore


SCOPE = RequesterContext("company", Principal("feishu", "alice"))
WORKER = WorkerContext("worker-1")
DEST = PrivateDestination(SCOPE.principal, "dm-alice")


def at(seconds=0):
    return (datetime(2026, 9, 13, tzinfo=UTC) + timedelta(seconds=seconds)).isoformat()


def admit(store, key="key", **kwargs):
    args = dict(payload={"sources": [{"id": "source-1", "text": "synthetic"}]},
                grant_id="pilot", grant_revision=1, destination=DEST,
                execution_seconds=180, now=at())
    args.update(kwargs)
    return store.admit(SCOPE, idempotency_key=key, **args)


def admit_with_busy_retry(store, key="key", *, timeout=15, **kwargs):
    # Retry only transient contention; each SQLite wait still uses the fixture's 0.1s.
    deadline = monotonic() + timeout
    while True:
        try:
            return admit(store, key, **kwargs)
        except StoreBusy:
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise
            sleep(min(0.01, remaining))
            if monotonic() >= deadline:
                raise


def publish(store, lease, now=None, **kwargs):
    args = dict(draft={"text": "Draft", "recipient": "ignored-model-recipient"},
                provenance={"model": "fake", "prompt_version": "v1", "snapshot": "v1"},
                validation_version="v1", clock=lambda: now or at(2))
    args.update(kwargs)
    return store.complete(lease, **args)


@pytest.fixture
def store(tmp_path):
    return BriefStore(tmp_path / "briefs.sqlite3", busy_timeout=0.1)


def test_persistence_immutable_admission_and_private_outbox(store):
    payload = {"sources": [{"id": "x", "values": [1, True, None]}]}
    queued = admit(store, payload=payload)
    payload["sources"][0]["values"].append(99)
    reopened = BriefStore(store.path)
    job = reopened.get(SCOPE, queued.job_id)
    assert job.state == "queued" and job.attempt == 0
    assert job.payload["sources"][0]["values"] == (1, True, None)
    with pytest.raises(TypeError):
        job.payload["sources"][0]["id"] = "mutate"
    with pytest.raises(FrozenInstanceError):
        job.state = "generating"
    claim = reopened.claim_one(WORKER, clock=lambda: at(1))
    reopened.start_generating(claim.lease, clock=lambda: at(2))
    draft = {"text": ["frozen"]}
    revision = publish(reopened, claim.lease, draft=draft)
    draft["text"].append("later")
    delivery = BriefStore(store.path).outbox(SCOPE, job.job_id)
    assert delivery.state == "pending"
    assert delivery.destination == DEST
    assert delivery.revision == revision
    assert delivery.revision.payload["text"] == ("frozen",)
    assert delivery.revision.provenance["model"] == "fake"
    assert delivery.revision.validation_version == "v1"
    assert reopened.get(SCOPE, job.job_id).state == "draft_ready"


def database_image(path):
    with sqlite3.connect(path) as db:
        return (db.execute("PRAGMA application_id").fetchone(),
                db.execute("PRAGMA user_version").fetchone(),
                db.execute("SELECT type,name,sql FROM sqlite_master ORDER BY name").fetchall())


def test_monitor_foreign_future_and_partial_schema_are_unchanged(tmp_path):
    monitor = tmp_path / "monitor.db"
    MonitorStore(monitor)
    foreign = tmp_path / "foreign.db"
    with sqlite3.connect(foreign) as db:
        db.execute("CREATE TABLE foreign_data(value)")
    future = tmp_path / "future.db"
    BriefStore(future)
    with sqlite3.connect(future) as db:
        db.execute(f"PRAGMA user_version={CURRENT_SCHEMA_VERSION + 1}")
    partial = tmp_path / "partial.db"
    with sqlite3.connect(partial) as db:
        db.execute(f"PRAGMA application_id={APPLICATION_ID}")
        db.execute(f"PRAGMA user_version={CURRENT_SCHEMA_VERSION}")
        db.execute("CREATE TABLE brief_jobs(id)")
    for path in (monitor, foreign, future, partial):
        before = database_image(path)
        raw = path.read_bytes()
        with pytest.raises(BriefSchemaError):
            BriefStore(path)
        assert database_image(path) == before
        assert path.read_bytes() == raw
    assert READABLE_SCHEMA_MIN <= CURRENT_SCHEMA_VERSION <= READABLE_SCHEMA_MAX


def test_corrupt_and_partial_headers_fail_closed(tmp_path):
    path = tmp_path / "broken.db"
    path.write_bytes(b"not a sqlite database" * 100)
    before = path.read_bytes()
    with pytest.raises(BriefSchemaError):
        BriefStore(path)
    assert path.read_bytes() == before
    for app_id, version in ((APPLICATION_ID, 0), (0, CURRENT_SCHEMA_VERSION), (1234, 0)):
        path = tmp_path / f"header-{app_id}-{version}.db"
        with sqlite3.connect(path) as db:
            db.execute(f"PRAGMA application_id={app_id}")
            db.execute(f"PRAGMA user_version={version}")
        before = path.read_bytes()
        with pytest.raises(BriefSchemaError):
            BriefStore(path)
        assert path.read_bytes() == before


def test_concurrent_capacity_and_idempotency(store):
    barrier = Barrier(24)

    def submit(i):
        barrier.wait(timeout=10)
        try:
            return admit_with_busy_retry(store, str(i))
        except QueueFull:
            return None

    with ThreadPoolExecutor(max_workers=24) as pool:
        results = list(pool.map(submit, range(24)))
    accepted = [job for job in results if job]
    assert len(accepted) == 20
    assert results.count(None) == 4
    original = accepted[0]
    assert admit(store, original.idempotency_key).job_id == original.job_id
    with pytest.raises(Conflict):
        admit(store, original.idempotency_key, payload={"changed": True})
    for change in ({"grant_revision": 2}, {"grant_id": "other"},
                   {"destination": PrivateDestination(SCOPE.principal, "dm-other")},
                   {"execution_seconds": 90}):
        with pytest.raises(Conflict):
            admit(store, original.idempotency_key, **change)


def test_concurrent_exact_replay_has_one_row(store):
    barrier = Barrier(8)

    def submit(_):
        barrier.wait(timeout=10)
        return admit_with_busy_retry(store)

    with ThreadPoolExecutor(max_workers=8) as pool:
        jobs = list(pool.map(submit, range(8)))
    assert len({job.job_id for job in jobs}) == 1
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT count(*) FROM brief_jobs").fetchone()[0] == 1
    assert admit(store, payload={"sources": [{"text": "synthetic", "id": "source-1"}]}).job_id == jobs[0].job_id


@pytest.mark.parametrize("release_on_busy", [True, False], ids=["retry", "exhausted"])
def test_admission_busy_retry_with_real_lock(store, monkeypatch, release_on_busy):
    original = store.admit
    busy_errors = []
    with sqlite3.connect(store.path) as holder:
        holder.execute("BEGIN EXCLUSIVE")
        def attempt(*args, **kwargs):
            try:
                return original(*args, **kwargs)
            except StoreBusy as error:
                busy_errors.append(error)
                if release_on_busy:
                    holder.rollback()
                raise
        monkeypatch.setattr(store, "admit", attempt)
        if release_on_busy:
            assert admit_with_busy_retry(store).state == "queued"
            assert len(busy_errors) == 1
        else:
            started = monotonic()
            with pytest.raises(StoreBusy) as error:
                admit_with_busy_retry(store, timeout=0.25)
            assert monotonic() - started >= 0.25
            assert error.value is busy_errors[-1]
            assert holder.execute("SELECT count(*) FROM brief_jobs").fetchone()[0] == 0


@pytest.mark.parametrize("error_type", [StoreError, Conflict, QueueFull, RuntimeError])
def test_admission_busy_retry_propagates_other_errors(store, monkeypatch, error_type):
    error = error_type("unexpected admission failure")
    calls = []
    def fail(*args, **kwargs):
        calls.append(True)
        raise error
    monkeypatch.setattr(store, "admit", fail)
    with pytest.raises(error_type) as caught:
        admit_with_busy_retry(store)
    assert caught.value is error and calls == [True]


@pytest.mark.parametrize("scope", [
    RequesterContext("other", SCOPE.principal),
    RequesterContext("company", Principal("slack", "alice")),
    RequesterContext("company", Principal("feishu", "bob")),
])
def test_requester_isolation(store, scope):
    job = admit(store)
    assert store.get(scope, job.job_id) is None
    assert store.outbox(scope, job.job_id) is None
    with pytest.raises(Conflict):
        store.cancel(scope, job.job_id, clock=lambda: at(1))
    other = store.admit(scope, idempotency_key="key", payload={}, grant_id="pilot",
                        grant_revision=1, destination=PrivateDestination(scope.principal, "dm"),
                        now=at())
    assert other.job_id != job.job_id


def test_destination_must_explicitly_match_requester(store):
    with pytest.raises(SchemaError):
        admit(store, destination=PrivateDestination(Principal("slack", "alice"), "dm"))
    with pytest.raises(SchemaError):
        admit(store, destination={"recipient": "alice", "private": True})


@pytest.mark.parametrize("payload", [
    {1: "bad"}, {"x": {False: 3}}, {"x": float("nan")}, {"x": float("inf")},
    {"x": float("-inf")}, {"x": b"bytes"}, {"x": {1, 2}}, {"x": (1, 2)},
    {"x": object()}, {"x": "\ud800"}, [], "json", {"x": "x" * 1048576},
])
def test_rejects_malformed_and_oversize_json(store, payload):
    with pytest.raises(SchemaError):
        admit(store, payload=payload)
    assert store.claim_one(WORKER, clock=lambda: at()) is None


def test_cyclic_deep_and_exact_size_json(store):
    cycle = {}
    cycle["self"] = cycle
    deep = {}
    for _ in range(70):
        deep = {"x": deep}
    for payload in (cycle, deep):
        with pytest.raises(SchemaError):
            admit(store, payload=payload)
    exact = admit(store, payload={"x": "x" * (1048576 - 8)})
    assert len(exact.payload_json.encode()) == 1048576


@pytest.mark.parametrize("seconds", [181, True, False, -1, 0, 1.5, "30"])
def test_execution_duration_validation(store, seconds):
    with pytest.raises(SchemaError):
        admit(store, execution_seconds=seconds)


@pytest.mark.parametrize("timeout", [True, float("nan"), float("inf"), -1, 6, "1"])
def test_busy_timeout_validation(tmp_path, timeout):
    with pytest.raises(SchemaError):
        BriefStore(tmp_path / "briefs.db", busy_timeout=timeout)


def test_strict_operational_values(store):
    for now in ("2026-09-13", "2026-09-13T00:00:00", "2026-09-13T01:00:00+01:00", True):
        with pytest.raises(SchemaError):
            admit(store, now=now)
    for change in ({"grant_revision": True}, {"grant_revision": 0},
                   {"grant_id": ""}, {"grant_id": "x" * 257}):
        with pytest.raises(SchemaError):
            admit(store, **change)
    for value in ("", "x" * 257, True):
        with pytest.raises(SchemaError):
            WorkerContext(value)
        with pytest.raises(SchemaError):
            RequesterContext(value, SCOPE.principal)
    with pytest.raises(SchemaError):
        store.get(SCOPE, "")


def test_busy_is_retryable_and_other_db_errors_are_not_success(store):
    with sqlite3.connect(store.path) as db:
        db.execute("BEGIN IMMEDIATE")
        with pytest.raises(StoreBusy, match="retry"):
            admit(store)
        with pytest.raises(StoreBusy):
            BriefStore(store.path, busy_timeout=0.01)
    assert admit(store).state == "queued"


def test_outbox_insert_failure_rolls_back_whole_completion(store, monkeypatch):
    job = admit(store)
    claimed = store.claim_one(WORKER, clock=lambda: at())
    store.start_generating(claimed.lease, clock=lambda: at(1))
    original = store._transaction
    reached_insert = []

    @contextmanager
    def inject_failure(*args, **kwargs):
        with original(*args, **kwargs) as db:
            # After schema inspection, on the actual completion connection.
            db.create_function("record_insert", 0, lambda: reached_insert.append(True))
            db.execute("CREATE TEMP TRIGGER reject_outbox BEFORE INSERT ON main.brief_outbox "
                       "BEGIN SELECT record_insert(); "
                       "SELECT RAISE(ABORT, 'test outbox failure'); END")
            yield db

    with monkeypatch.context() as patch:
        patch.setattr(store, "_transaction", inject_failure)
        with pytest.raises(StoreError) as error:
            publish(store, claimed.lease)
        assert not isinstance(error.value, BriefSchemaError)
    assert reached_insert == [True]
    assert store.get(SCOPE, job.job_id).state == "generating"
    assert store.outbox(SCOPE, job.job_id) is None
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT count(*) FROM brief_revisions").fetchone()[0] == 0
    publish(store, claimed.lease)
    with pytest.raises(LeaseLost):
        publish(store, claimed.lease)
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT count(*) FROM brief_revisions").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM brief_outbox").fetchone()[0] == 1


def test_crash_with_uncommitted_change_reopens_original_job(store):
    job = admit(store)
    code = """
import os, sqlite3, sys
connection = sqlite3.connect(sys.argv[1])
connection.execute('BEGIN IMMEDIATE')
connection.execute("UPDATE brief_jobs SET state='cancelled'")
os._exit(17)
"""
    result = subprocess.run([sys.executable, "-c", code, str(store.path)], timeout=5)
    assert result.returncode == 17
    assert BriefStore(store.path).get(SCOPE, job.job_id).state == "queued"


def test_cancel_invalidates_active_and_pending_delivery(store):
    job = admit(store)
    claim = store.claim_one(WORKER, clock=lambda: at())
    cancelled = store.cancel(SCOPE, job.job_id, clock=lambda: at(1))
    assert cancelled.state == "cancelled" and cancelled.fence > claim.fence
    with pytest.raises(LeaseLost):
        store.renew(claim.lease, clock=lambda: at(2))
    with pytest.raises(LeaseLost):
        publish(store, claim.lease)
    other = admit(store, "other")
    current = store.claim_one(WORKER, clock=lambda: at(2))
    store.start_generating(current.lease, clock=lambda: at(2))
    publish(store, current.lease)
    store.cancel(SCOPE, other.job_id, clock=lambda: at(3))
    assert store.outbox(SCOPE, other.job_id).state == "cancelled"
    assert store.cancel(SCOPE, other.job_id, clock=lambda: at(4)).state == "cancelled"
    assert store.claim_one(WORKER, clock=lambda: at(4)) is None


def test_huge_operational_number_is_a_typed_validation_error(tmp_path):
    with pytest.raises(SchemaError):
        BriefStore(tmp_path / "invalid.db", busy_timeout=10**1000)
    assert not (tmp_path / "invalid.db").exists()


@pytest.mark.parametrize("kwargs", [
    {"draft": {"number": float("nan")}},
    {"draft": {"text": "x" * 1048576}},
    {"provenance": {}},
    {"provenance": {"bad": {1: "key"}}},
    {"provenance": {"text": "x" * 1048576}},
    {"validation_version": True},
    {"validation_version": ""},
    {"validation_version": "\ud800"},
])
def test_invalid_completion_keeps_current_attempt_without_partial_output(store, kwargs):
    job = admit(store)
    claim = store.claim_one(WORKER, clock=lambda: at())
    store.start_generating(claim.lease, clock=lambda: at(1))
    with pytest.raises(SchemaError):
        publish(store, claim.lease, **kwargs)
    assert store.get(SCOPE, job.job_id).state == "generating"
    assert store.outbox(SCOPE, job.job_id) is None


def test_reused_handle_rejects_future_schema_without_mutation(store):
    with sqlite3.connect(store.path) as db:
        db.execute(f"PRAGMA user_version={CURRENT_SCHEMA_VERSION + 1}")
    before = store.path.read_bytes()
    with pytest.raises(BriefSchemaError):
        admit(store)
    assert store.path.read_bytes() == before


def test_concurrent_completion_is_one_effect(store):
    store = BriefStore(store.path, busy_timeout=3)
    job = admit(store)
    first = store.claim_one(WORKER, clock=lambda: at())
    store.start_generating(first.lease, clock=lambda: at(1))
    barrier = Barrier(2)

    def finish(_):
        barrier.wait()
        try:
            return publish(store, first.lease)
        except LeaseLost:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(finish, range(2)))
    revisions = [revision for revision in results if revision is not None]
    assert len(revisions) == 1
    assert BriefStore(store.path).outbox(SCOPE, job.job_id).revision == revisions[0]


@pytest.mark.parametrize("body", ["SELECT RAISE(IGNORE)", "SELECT 1"])
def test_unknown_persistent_trigger_is_rejected_without_mutation(store, body):
    job = admit(store)
    claimed = store.claim_one(WORKER, clock=lambda: at())
    store.start_generating(claimed.lease, clock=lambda: at(1))
    with sqlite3.connect(store.path) as db:
        db.execute("CREATE TRIGGER silently_skip_outbox BEFORE INSERT ON brief_outbox "
                   f"BEGIN {body}; END")
    before = store.path.read_bytes()
    with pytest.raises(BriefSchemaError):
        BriefStore(store.path)
    with pytest.raises(BriefSchemaError):
        publish(store, claimed.lease)
    assert store.path.read_bytes() == before
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT state FROM brief_jobs WHERE job_id=?",
                          (job.job_id,)).fetchone()[0] == "generating"
        assert db.execute("SELECT count(*) FROM brief_revisions").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM brief_outbox").fetchone()[0] == 0


def test_silently_ignored_outbox_write_cannot_report_success(store, monkeypatch):
    job = admit(store)
    claimed = store.claim_one(WORKER, clock=lambda: at())
    store.start_generating(claimed.lease, clock=lambda: at(1))
    original = store._transaction

    @contextmanager
    def inject_ignore(*args, **kwargs):
        with original(*args, **kwargs) as db:
            db.execute("CREATE TEMP TRIGGER skip_outbox BEFORE INSERT ON main.brief_outbox "
                       "BEGIN SELECT RAISE(IGNORE); END")
            yield db

    with monkeypatch.context() as patch:
        patch.setattr(store, "_transaction", inject_ignore)
        with pytest.raises(StoreError) as error:
            publish(store, claimed.lease)
        assert not isinstance(error.value, BriefSchemaError)
    assert store.get(SCOPE, job.job_id).state == "generating"
    assert store.outbox(SCOPE, job.job_id) is None
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT count(*) FROM brief_revisions").fetchone()[0] == 0


def test_exact_admission_replay_does_not_rewind_active_job(store):
    job = admit(store)
    claim = store.claim_one(WORKER, clock=lambda: at(1))
    renewed = store.renew(claim.lease, clock=lambda: at(10))
    replay = admit(store)  # Original admission timestamp, no new time-dependent effect.
    assert replay == renewed and replay.job_id == job.job_id


@pytest.mark.parametrize("initialized", [False, True])
def test_concurrent_setup_preserves_owned_delete_database(tmp_path, initialized):
    path = tmp_path / "setup.db"
    existing = None
    if initialized:
        existing = admit(BriefStore(path))
        before = path.read_bytes(), database_image(path)
    barrier = Barrier(8)

    def reopen(_):
        barrier.wait(timeout=5)
        return BriefStore(path, busy_timeout=3)

    with ThreadPoolExecutor(max_workers=8) as pool:
        stores = list(pool.map(reopen, range(8)))
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    if initialized:
        assert (path.read_bytes(), database_image(path)) == before
        assert all(store.get(SCOPE, existing.job_id) == existing for store in stores)
    else:
        assert admit(stores[0]).state == "queued"


def test_changed_journal_mode_is_refused_without_conversion(store):
    job = admit(store)
    with sqlite3.connect(store.path) as db:
        assert db.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    before = store.path.read_bytes()
    with pytest.raises(BriefSchemaError, match="journal"):
        BriefStore(store.path)
    with pytest.raises(BriefSchemaError, match="journal"):
        store.cancel(SCOPE, job.job_id, clock=lambda: at(1))
    assert store.path.read_bytes() == before
    with sqlite3.connect(store.path) as db:
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert db.execute("SELECT state FROM brief_jobs").fetchone()[0] == "queued"


@pytest.mark.parametrize('request_hash', [None, 'a' * 64])
def test_meeting_kind_requires_scope_binding_even_through_raw_store(store, request_hash):
    store.enable_manual()
    payload = {'kind': 'meeting-brief-v1', 'request': {'confirmed_scope_hash': 'a' * 64}}
    with pytest.raises(SchemaError):
        admit(store, payload=payload, request_hash=request_hash)
    assert store.get_by_event(SCOPE, 'key') is None


@pytest.mark.parametrize('request_value', [None, [], 'untrusted', 1])
def test_meeting_scope_binding_rejects_nonobject_request(store, request_value):
    store.enable_manual()
    with pytest.raises(SchemaError):
        admit(store, payload={'kind': 'meeting-brief-v1', 'request': request_value},
              request_hash='a' * 64, meeting_scope_hash='a' * 64)
    assert store.get_by_event(SCOPE, 'key') is None


def test_raw_meeting_admission_preserves_replay_and_cross_event_tombstone(store):
    store.enable_manual()
    args = dict(payload={'kind': 'meeting-brief-v1',
                         'request': {'confirmed_scope_hash': 'a' * 64, 'origin': at()}},
                request_hash='a' * 64, meeting_scope_hash='a' * 64)
    original = admit(store, **args)
    assert admit(store, **args).job_id == original.job_id
    with pytest.raises(Conflict):
        admit(store, 'another-event', **args)
    with pytest.raises(Conflict):
        admit(store, **(args | {'payload': {'kind': 'meeting-brief-v1',
              'request': {'confirmed_scope_hash': 'a' * 64, 'origin': at(30)}}}))
    assert store.get_by_event(SCOPE, 'another-event') is None
