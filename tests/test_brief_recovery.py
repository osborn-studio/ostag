"""A02 lease/recovery boundaries, with fake clocks and blocked fake IO only."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
import sqlite3
from threading import Barrier, Event, Thread

import pytest

from ostag.briefs.store import (
    BriefStore, RequesterContext, WorkerContext, PrivateDestination,
    RecoveryValidity, LeaseGuard, LeaseLost, ClockError, Conflict,
)
from ostag.core.identity import Principal
from ostag.core.schemas import SchemaError


SCOPE = RequesterContext("company", Principal("feishu", "alice"))
WORKER = WorkerContext("worker")
VALID = RecoveryValidity(grant_current=True, snapshot_fresh=True, sources_available=True)


def at(seconds=0):
    return (datetime(2026, 9, 13, tzinfo=UTC) + timedelta(seconds=seconds)).isoformat()


@pytest.fixture
def store(tmp_path):
    return BriefStore(tmp_path / "briefs.db", busy_timeout=1)


def admit(store, key="key", seconds=180):
    return store.admit(SCOPE, idempotency_key=key, payload={"authorized": True, "valid": True},
                       grant_id="grant", grant_revision=3,
                       destination=PrivateDestination(SCOPE.principal, "dm"),
                       execution_seconds=seconds, now=at())


def recover(store, seconds, validity=VALID, worker=WORKER):
    return store.recover_one(worker, validate=lambda job: validity, clock=lambda: at(seconds))


def complete(store, lease, seconds):
    return store.complete(lease, draft={"claims": []}, provenance={"model": "fake"},
                          validation_version="v1", clock=lambda: at(seconds))


def test_queue_wait_excluded_and_shorter_duration_pinned(store):
    original = admit(store, seconds=12)
    first = store.claim_one(WORKER, clock=lambda: at(1000))
    assert first.job_id == original.job_id
    assert first.first_claim_at == at(1000).replace("+00:00", "Z")
    assert first.deadline == at(1012).replace("+00:00", "Z")
    assert first.lease_until == first.deadline
    assert first.execution_seconds == 12
    with pytest.raises(LeaseLost):
        store.renew(first.lease, clock=lambda: at(1012))
    assert recover(store, 1012) is None
    assert store.get(SCOPE, first.job_id).state == "failed"


@pytest.mark.parametrize("count", [1, 2])
def test_concurrent_claim_has_one_global_live_lease(store, count):
    for i in range(count):
        admit(store, str(i))
    barrier = Barrier(2)

    def claim(i):
        barrier.wait()
        return store.claim_one(WorkerContext(f"worker-{i}"), clock=lambda: at())

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, range(2)))
    winners = [job for job in results if job]
    assert len(winners) == 1
    winner = winners[0]
    assert winner.attempt == winner.fence == 1
    assert store.claim_one(WORKER, clock=lambda: at(29)) is None
    store.cancel(SCOPE, winner.job_id, clock=lambda: at(29))
    next_job = store.claim_one(WORKER, clock=lambda: at(29))
    assert (next_job is not None) == (count == 2)


def test_expired_lease_does_not_block_next_queued_job_or_double_recovery(store):
    admit(store)
    admit(store, "second")
    first = store.claim_one(WORKER, clock=lambda: at())
    second = store.claim_one(WORKER, clock=lambda: at(30))
    assert second and second.job_id != first.job_id
    assert recover(store, 30) is None
    store.start_generating(second.lease, clock=lambda: at(31))
    complete(store, second.lease, 31)
    assert recover(store, 31).job_id == first.job_id


def test_lease_expiry_equality_new_fence_same_owner_and_retry_budget(store):
    admit(store)
    first = store.claim_one(WORKER, clock=lambda: at())
    store.start_generating(first.lease, clock=lambda: at(1))
    with pytest.raises(LeaseLost):
        store.renew(first.lease, clock=lambda: at(30))
    with pytest.raises(LeaseLost):
        complete(store, first.lease, 30)
    second = recover(store, 30)
    assert second.attempt == 2 and second.fence > first.fence
    assert second.deadline == first.deadline and second.payload_json == first.payload_json
    assert second.first_claim_at == first.first_claim_at
    for operation in (store.renew, store.start_generating, store.assert_current):
        with pytest.raises(LeaseLost):
            operation(first.lease, clock=lambda: at(31))
    with pytest.raises(LeaseLost):
        complete(store, first.lease, 31)
    third = recover(store, 60)
    assert third.attempt == 3 and third.deadline == first.deadline
    assert recover(store, 90) is None
    final = store.get(SCOPE, first.job_id)
    assert final.state == "failed" and final.attempt == 3
    assert recover(store, 100) is None


def test_renew_cap_deadline_equality_and_backward_time(store):
    admit(store, seconds=45)
    first = store.claim_one(WORKER, clock=lambda: at())
    renewed = store.renew(first.lease, clock=lambda: at(20))
    assert renewed.lease_until == renewed.deadline == at(45).replace("+00:00", "Z")
    for operation in (store.renew, store.start_generating, store.assert_current):
        with pytest.raises(ClockError):
            operation(first.lease, clock=lambda: at(19))
    with pytest.raises(ClockError):
        store.cancel(SCOPE, first.job_id, clock=lambda: at(19))
    with pytest.raises(ClockError):
        store.claim_one(WORKER, clock=lambda: at(19))
    store.start_generating(first.lease, clock=lambda: at(20))
    with pytest.raises(ClockError):
        complete(store, first.lease, 19)
    with pytest.raises(LeaseLost):
        complete(store, first.lease, 45)
    assert recover(store, 45) is None
    assert store.get(SCOPE, first.job_id).state == "failed"


def test_backwards_first_claim_and_recovery_clock(store):
    admit(store)
    with pytest.raises(ClockError):
        store.claim_one(WORKER, clock=lambda: at(-1))
    first = store.claim_one(WORKER, clock=lambda: at())
    times = iter([at(30), at(29)])
    with pytest.raises(ClockError):
        store.recover_one(WORKER, validate=lambda job: VALID, clock=lambda: next(times))
    assert store.get(SCOPE, first.job_id).attempt == 1


def test_illegal_transitions_and_tampered_lease_context(store):
    queued = admit(store)
    with pytest.raises(Conflict):
        _ = queued.lease
    first = store.claim_one(WORKER, clock=lambda: at())
    with pytest.raises(LeaseLost):
        complete(store, first.lease, 1)
    for lease in (
        replace(first.lease, scope=RequesterContext("other", SCOPE.principal)),
        replace(first.lease, scope=RequesterContext("company", Principal("slack", "alice"))),
        replace(first.lease, worker=WorkerContext("wrong-owner")),
    ):
        with pytest.raises(LeaseLost):
            store.start_generating(lease, clock=lambda: at(1))
    with pytest.raises(SchemaError):
        replace(first.lease, fence=True)
    with pytest.raises(SchemaError):
        store.finish(first.lease, outcome="draft_ready", clock=lambda: at(1))
    store.start_generating(first.lease, clock=lambda: at(1))
    with pytest.raises(LeaseLost):
        store.start_generating(first.lease, clock=lambda: at(1))
    complete(store, first.lease, 2)
    with pytest.raises(LeaseLost):
        store.start_generating(first.lease, clock=lambda: at(3))
    assert recover(store, 31) is None


@pytest.mark.parametrize("outcome", ["needs_input", "failed"])
def test_explicit_worker_failure_is_terminal(store, outcome):
    admit(store)
    first = store.claim_one(WORKER, clock=lambda: at())
    final = store.finish(first.lease, outcome=outcome, clock=lambda: at(1))
    assert final.state == outcome and final.fence > first.fence
    assert recover(store, 30) is None


@pytest.mark.parametrize("phase", ["collecting", "generating"])
@pytest.mark.parametrize("field", ["grant_current", "snapshot_fresh", "sources_available"])
def test_recovery_requires_all_current_trusted_checks(store, phase, field):
    admit(store)
    first = store.claim_one(WORKER, clock=lambda: at())
    if phase == "generating":
        store.start_generating(first.lease, clock=lambda: at(1))
    validity = replace(VALID, **{field: False})
    assert recover(store, 30, validity) is None
    assert store.get(SCOPE, first.job_id).state == "needs_input"
    assert recover(store, 40) is None


def test_missing_malformed_or_failed_validator_cannot_approve_payload_flags(store):
    admit(store)
    first = store.claim_one(WORKER, clock=lambda: at())
    store.start_generating(first.lease, clock=lambda: at(1))
    with pytest.raises(TypeError):
        store.recover_one(WORKER, clock=lambda: at(30))
    for validity in (True, {"valid": True}, None):
        with pytest.raises(SchemaError):
            recover(store, 30, validity)
    with pytest.raises(SchemaError):
        RecoveryValidity(grant_current=1, snapshot_fresh=True, sources_available=True)

    def broken(job):
        raise RuntimeError("authority unavailable")

    with pytest.raises(RuntimeError):
        store.recover_one(WORKER, validate=broken, clock=lambda: at(30))
    assert store.get(SCOPE, first.job_id).attempt == 1


def test_validator_has_no_sqlite_lock_and_cas_rejects_cancellation(store):
    first_job = admit(store)
    store.claim_one(WORKER, clock=lambda: at())

    def check(job):
        assert job.grant_revision == 3 and job.payload["authorized"] is True
        # This write from a separate connection succeeds only outside the callback lock.
        store.cancel(SCOPE, first_job.job_id, clock=lambda: at(30))
        return VALID

    assert store.recover_one(WORKER, validate=check, clock=lambda: at(30)) is None
    assert store.get(SCOPE, first_job.job_id).state == "cancelled"


def test_simultaneous_recovery_has_one_winner(store):
    admit(store)
    store.claim_one(WORKER, clock=lambda: at())
    barrier = Barrier(2)

    def check(job):
        barrier.wait(timeout=3)
        return VALID

    def recovery(i):
        return store.recover_one(WorkerContext(f"w-{i}"), validate=check, clock=lambda: at(30))

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(recovery, range(2)))
    assert len([job for job in results if job]) == 1
    assert next(job for job in results if job).attempt == 2


def test_recovery_rechecks_snapshot_exactly_and_deadline_after_callback(store):
    admit(store)
    first = store.claim_one(WORKER, clock=lambda: at())

    def tamper(job):
        with sqlite3.connect(store.path) as db:
            db.execute("UPDATE brief_jobs SET payload_json='{}' WHERE job_id=?", (job.job_id,))
        return VALID

    assert store.recover_one(WORKER, validate=tamper, clock=lambda: at(30)) is None
    assert store.get(SCOPE, first.job_id).attempt == 1
    times = iter([at(30), at(180)])
    assert store.recover_one(WORKER, validate=lambda job: VALID,
                             clock=lambda: next(times)) is None
    assert store.get(SCOPE, first.job_id).state == "failed"


def test_heartbeat_progresses_while_fake_model_blocks_and_stops_cleanly(store, monkeypatch):
    admit(store)
    first = store.claim_one(WORKER, clock=lambda: at())
    store.start_generating(first.lease, clock=lambda: at())
    started, release, heartbeat = Event(), Event(), Event()
    original = store.renew

    def observe_renewal(*args, **kwargs):
        result = original(*args, **kwargs)
        heartbeat.set()  # Evidence of a committed heartbeat, not clock call count.
        return result

    monkeypatch.setattr(store, "renew", observe_renewal)

    def fake_model():
        started.set()
        assert release.wait(3)

    model = Thread(target=fake_model)
    with LeaseGuard(store, first.lease, clock=lambda: at(1), interval=0.005) as guard:
        guard.before_io()
        model.start()
        try:
            assert started.wait(1) and heartbeat.wait(2)
            assert model.is_alive()
            assert store.get(SCOPE, first.job_id).lease_until > first.lease_until
            guard.assert_current()
        finally:
            release.set()
            model.join(2)
    assert not guard.running and not model.is_alive()
    with pytest.raises(LeaseLost):
        guard.before_io()


def test_failed_heartbeat_latches_prevents_io_and_commit(store, monkeypatch):
    admit(store)
    first = store.claim_one(WORKER, clock=lambda: at())
    store.start_generating(first.lease, clock=lambda: at())
    calls = []
    original = store.renew

    def fail_once(*args, **kwargs):
        calls.append("renew")
        raise RuntimeError("simulated heartbeat failure")

    monkeypatch.setattr(store, "renew", fail_once)
    with LeaseGuard(store, first.lease, clock=lambda: at(1), interval=0.005) as guard:
        assert guard.stop_requested.wait(2)
        monkeypatch.setattr(store, "renew", original)
        for _ in range(2):
            with pytest.raises(LeaseLost):
                guard.before_io()
                calls.append("forbidden IO")
        with pytest.raises(LeaseLost):
            guard.complete(draft={}, provenance={"model": "fake"}, validation_version="v1")
        assert guard.lost and calls == ["renew"]
    assert not guard.running
    assert store.get(SCOPE, first.job_id).state == "generating"


def test_cancellation_latches_guard_and_exception_exit_joins(store):
    job = admit(store)
    first = store.claim_one(WORKER, clock=lambda: at())
    guard = LeaseGuard(store, first.lease, clock=lambda: at(2), interval=0.005)
    with pytest.raises(RuntimeError, match="caller failure"):
        with guard:
            store.cancel(SCOPE, job.job_id, clock=lambda: at(2))
            assert guard.stop_requested.wait(2)
            assert guard.lost
            with pytest.raises(LeaseLost):
                guard.assert_current()
            raise RuntimeError("caller failure")
    assert not guard.running


@pytest.mark.parametrize("interval", [True, 0, -1, 30, float("nan"), float("inf"), "10"])
def test_guard_interval_is_strict_and_bounded(store, interval):
    admit(store)
    first = store.claim_one(WORKER, clock=lambda: at())
    with pytest.raises(SchemaError):
        LeaseGuard(store, first.lease, interval=interval)


def test_guard_successful_completion_stops_renewing_without_latching_loss(store):
    job = admit(store)
    first = store.claim_one(WORKER, clock=lambda: at())
    store.start_generating(first.lease, clock=lambda: at())
    with LeaseGuard(store, first.lease, clock=lambda: at(1), interval=0.005) as guard:
        revision = guard.complete(draft={}, provenance={"model": "fake"}, validation_version="v1")
        assert guard.stop_requested.is_set() and not guard.lost
        with pytest.raises(LeaseLost):
            guard.before_io()
    assert not guard.running and not guard.lost
    assert store.outbox(SCOPE, job.job_id).revision == revision


def test_guard_first_checkpoint_failure_and_expiry_shutdown(store):
    admit(store)
    first = store.claim_one(WORKER, clock=lambda: at())
    guard = LeaseGuard(store, first.lease, clock=lambda: at(30), interval=0.005)
    with pytest.raises(LeaseLost):
        with guard:
            pytest.fail("expired lease must not start the worker")
    assert guard.lost and guard.stop_requested.is_set() and not guard.running


def test_recovery_callback_allows_competing_claim_but_cannot_create_second_lease(store):
    admit(store)
    admit(store, "next")
    first = store.claim_one(WORKER, clock=lambda: at())

    def check(job):
        second = store.claim_one(WorkerContext("second-worker"), clock=lambda: at(30))
        assert second.job_id != job.job_id
        return VALID

    assert store.recover_one(WORKER, validate=check, clock=lambda: at(30)) is None
    assert store.get(SCOPE, first.job_id).attempt == 1


def test_recovery_short_duration_preserves_remaining_time(store):
    admit(store, seconds=40)
    first = store.claim_one(WORKER, clock=lambda: at())
    second = recover(store, 30)
    assert second.execution_seconds == 40
    assert second.deadline == first.deadline == second.lease_until
    assert second.first_claim_at == first.first_claim_at
    assert recover(store, 40) is None
    assert store.get(SCOPE, first.job_id).state == "failed"


@pytest.mark.parametrize("reader", [False, True])
def test_recovery_samples_time_after_contended_write_lock(store, monkeypatch, reader):
    admit(store)
    first = store.claim_one(WORKER, clock=lambda: at())
    # Existing clock API: reproduces the stale pre-lock observation directly.
    result = contend(store, monkeypatch, 30, 180,
                     lambda clock: store.recover_one(WORKER, validate=lambda job: VALID,
                                                      clock=clock), reader=reader)
    assert result is None
    final = store.get(SCOPE, first.job_id)
    assert final.state == "failed" and final.attempt == 1


def contend(store, monkeypatch, before, after, operation, *, reader=False):
    """Real second connection owns the lock; events advance a synthetic UTC clock.

    Writer case: trace fires before SQLite acquires the write lock. Reader
    case: DELETE mode lets IMMEDIATE reach COMMIT while the reader still holds
    its snapshot; EXCLUSIVE must wait at BEGIN instead. In either case release
    the real blocking connection only after advancing past the deadline.
    No sleeps, wall-clock expiry races, or model/provider calls are involved.
    """
    from contextlib import contextmanager

    entering = Event()
    current = [at(before)]
    original = store._connection

    @contextmanager
    def traced_connection(**kwargs):
        with original(**kwargs) as db:
            boundaries = ("BEGIN EXCLUSIVE", "COMMIT") if reader else (
                "BEGIN IMMEDIATE", "BEGIN EXCLUSIVE")
            db.set_trace_callback(lambda sql: entering.set() if sql in boundaries else None)
            yield db

    with sqlite3.connect(store.path) as holder, monkeypatch.context() as patch:
        if reader:
            assert holder.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
            holder.execute("BEGIN")
            holder.execute("SELECT * FROM brief_jobs").fetchall()
        else:
            holder.execute("BEGIN IMMEDIATE")
        patch.setattr(store, "_connection", traced_connection)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(operation, lambda: current[0])
            try:
                assert entering.wait(2), "operation never attempted its write transaction"
                current[0] = at(after)
            finally:
                holder.rollback()
            return future.result(timeout=2)


@pytest.mark.parametrize("expiry", [30, 180])
@pytest.mark.parametrize("reader", [False, True], ids=["writer-lock", "reader-commit-lock"])
@pytest.mark.parametrize("operation", ["complete", "renew", "assert_current", "start_generating", "finish"])
def test_fenced_operation_observes_expiry_after_lock_wait(
    store, monkeypatch, expiry, operation, reader,
):
    job = admit(store)
    first = store.claim_one(WORKER, clock=lambda: at())
    if operation != "start_generating":
        store.start_generating(first.lease, clock=lambda: at())
    if expiry == 180:
        for seconds in range(20, 180, 20):
            store.renew(first.lease, clock=lambda: at(seconds))

    def execute(clock):
        args = {"clock": clock}
        if operation == "complete":
            args.update(draft={}, provenance={"model": "fake"}, validation_version="v1")
        elif operation == "finish":
            args["outcome"] = "failed"
        return getattr(store, operation)(first.lease, **args)

    with pytest.raises(LeaseLost):
        contend(store, monkeypatch, expiry - 1, expiry, execute, reader=reader)
    assert store.get(SCOPE, job.job_id).state == (
        "collecting" if operation == "start_generating" else "generating")
    assert store.outbox(SCOPE, job.job_id) is None
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT count(*) FROM brief_revisions").fetchone()[0] == 0


@pytest.mark.parametrize("reader", [False, True])
def test_claim_and_cancel_use_post_lock_clock(store, monkeypatch, reader):
    job = admit(store)
    first = contend(store, monkeypatch, 0, 10,
                    lambda clock: store.claim_one(WORKER, clock=clock), reader=reader)
    assert first.first_claim_at == at(10).replace("+00:00", "Z")
    assert first.deadline == at(190).replace("+00:00", "Z")
    cancelled = contend(store, monkeypatch, 10, 15,
                        lambda clock: store.cancel(SCOPE, job.job_id, clock=clock), reader=reader)
    assert cancelled.updated_at == at(15).replace("+00:00", "Z")


@pytest.mark.parametrize("operation", [
    "complete", "renew", "start_generating", "finish", "assert_current", "claim_one", "recover_one",
])
@pytest.mark.parametrize("after", [40, -1], ids=["expired", "clock-rewind"])
def test_elapsed_transaction_work_rolls_back_attempt(store, monkeypatch, operation, after):
    from contextlib import contextmanager

    job = admit(store, seconds=40)
    lease = None
    if operation != "claim_one":
        first = store.claim_one(WORKER, clock=lambda: at())
        lease = first.lease
        if operation == "complete":
            store.start_generating(lease, clock=lambda: at())
    before = store.get(SCOPE, job.job_id)
    current = [at(30 if operation == "recover_one" else 1)]
    reached = []
    original = store._connection

    @contextmanager
    def delayed_connection(**kwargs):
        with original(**kwargs) as db:
            def trace(sql):
                # Advance during real SQL work, after the initial time check.
                if sql.startswith("UPDATE brief_jobs") or (
                    operation == "assert_current" and sql.startswith("SELECT * FROM brief_jobs")
                ):
                    reached.append(sql)
                    current[0] = at(after)

            db.set_trace_callback(trace)
            yield db

    args = {"clock": lambda: current[0]}
    if operation == "complete":
        args.update(draft={}, provenance={"model": "fake"}, validation_version="v1")
    elif operation == "finish":
        args["outcome"] = "failed"
    elif operation == "recover_one":
        args["validate"] = lambda job: VALID
    with monkeypatch.context() as patch:
        patch.setattr(store, "_connection", delayed_connection)
        with pytest.raises(LeaseLost if after == 40 else ClockError):
            getattr(store, operation)(WORKER if operation.endswith("_one") else lease, **args)
    assert reached, "expiry must occur during actual transaction work"
    reopened = BriefStore(store.path)
    assert reopened.get(SCOPE, job.job_id) == before
    assert reopened.outbox(SCOPE, job.job_id) is None
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT count(*) FROM brief_revisions").fetchone()[0] == 0


def test_contended_claim_does_not_treat_committed_heartbeat_as_clock_rewind(store, monkeypatch):
    admit(store)
    first = store.claim_one(WORKER, clock=lambda: at())
    store.renew(first.lease, clock=lambda: at(10))
    assert contend(store, monkeypatch, 9, 11,
                   lambda clock: store.claim_one(WORKER, clock=clock)) is None


@pytest.mark.parametrize("reader", [False, True])
def test_guard_commit_observes_deadline_after_lock_wait(store, monkeypatch, reader):
    job = admit(store, seconds=2)
    first = store.claim_one(WORKER, clock=lambda: at())
    store.start_generating(first.lease, clock=lambda: at())

    # The completion must use the clock advanced by contend.
    with LeaseGuard(store, first.lease, clock=lambda: at(1), interval=10) as guard:
        def finish(clock):
            guard.clock = clock
            return guard.complete(draft={}, provenance={"model": "fake"}, validation_version="v1")

        with pytest.raises(LeaseLost):
            contend(store, monkeypatch, 1, 2, finish, reader=reader)
        assert guard.lost and guard.stop_requested.is_set()
    assert not guard.running
    assert store.get(SCOPE, job.job_id).state == "generating"
    assert store.outbox(SCOPE, job.job_id) is None


@pytest.mark.parametrize("value", [None, True, "2026-09-13T00:00:00Z", lambda: True,
                                       lambda: "2026-09-13T00:00:00", lambda: "invalid"])
def test_operation_clock_requires_callable_strict_utc(store, value):
    admit(store)
    with pytest.raises(SchemaError):
        store.claim_one(WORKER, clock=value)


def test_real_reader_held_past_wall_deadline_cannot_publish(store):
    """Parent reproduction without clock injection or transaction monkeypatches."""
    store = BriefStore(store.path, busy_timeout=3)
    job = admit(store, seconds=2)
    first = store.claim_one(WORKER)
    store.start_generating(first.lease)
    deadline = datetime.fromisoformat(first.deadline.replace("Z", "+00:00"))
    started = Event()

    def publish():
        started.set()
        return store.complete(first.lease, draft={}, provenance={"model": "fake"},
                              validation_version="v1")

    with sqlite3.connect(store.path) as reader, ThreadPoolExecutor(max_workers=1) as pool:
        assert reader.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        reader.execute("BEGIN")
        reader.execute("SELECT * FROM brief_jobs").fetchall()
        future = pool.submit(publish)
        try:
            assert started.wait(1)
            # Hold a normal reader over the real deadline, as in the parent probe.
            Event().wait(max(0, (deadline - datetime.now(UTC)).total_seconds()) + 0.15)
        finally:
            reader.rollback()
        with pytest.raises(LeaseLost):
            future.result(timeout=3)
    assert datetime.now(UTC) >= deadline
    reopened = BriefStore(store.path)
    assert reopened.get(SCOPE, job.job_id).state == "generating"
    assert reopened.outbox(SCOPE, job.job_id) is None
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT count(*) FROM brief_revisions").fetchone()[0] == 0


def test_renewal_cannot_hide_original_lease_expiry_during_update(store, monkeypatch):
    from contextlib import contextmanager

    admit(store)
    first = store.claim_one(WORKER, clock=lambda: at())
    current = [at(29)]
    updated = []
    original = store._connection

    @contextmanager
    def delayed_update(**kwargs):
        with original(**kwargs) as db:
            def trace(sql):
                if sql.startswith("UPDATE brief_jobs"):
                    updated.append(sql)
                    current[0] = at(30)
            db.set_trace_callback(trace)
            yield db

    with monkeypatch.context() as patch:
        patch.setattr(store, "_connection", delayed_update)
        with pytest.raises(LeaseLost):
            store.renew(first.lease, clock=lambda: current[0])
    assert updated
    # Tentative renewal to 59 must not supersede the original expiry at 30.
    assert BriefStore(store.path).get(SCOPE, first.job_id) == first
