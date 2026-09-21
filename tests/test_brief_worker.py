"""A04 runner -> actual A03 contract -> SQLite, without live provider traffic."""
import asyncio
from contextlib import contextmanager
from dataclasses import replace
import sqlite3
import json
from threading import Event

import pytest

from ostag.briefs.worker import BriefWorker
from ostag.briefs.store import BriefStore, LeaseLost, StoreBusy, WorkerContext
from ostag.core.runner import OSTagRunner
from ostag.config import Settings
from test_brief_generation import at, SCHEMA1_SQL, parameters, GenerationConfig
from test_brief_service import Authority, ForbiddenBackend, SCOPE, event


@pytest.mark.asyncio
async def test_runner_service_worker_real_generation_contract(tmp_path):
    authority = Authority()
    service = authority.service(BriefStore(tmp_path / "brief.db"))
    runner = OSTagRunner(Settings(run_ledger_path=tmp_path / "runs"),
                         backend=ForbiddenBackend(), brief_service=service)
    reply = await runner.handle_event(event())
    job_id = reply.split()[-1]
    worker = BriefWorker(service)
    try:
        assert await asyncio.wrap_future(worker.run_once()) == "draft_ready"
        job = service.store.get(SCOPE, job_id)
        box = service.store.outbox(SCOPE, job_id)
        assert job.state == "draft_ready"
        assert box.destination.address == "alice-dm" and box.state == "pending"
        assert box.revision.payload["status"] == "partial_semantic_review_required"
        provenance = json.loads(box.revision.provenance_json)
        assert provenance["actual_model_id"] == authority.config.parameters.expected_model_id
        assert provenance["usage"] == {"input_tokens": 100, "output_tokens": 50, "cost_micros": 200}
        assert provenance["config"] == authority.config.to_dict()
        assert len(provenance["sources"]["sources"]) == 2
        assert service.transport.calls[0].parameters == authority.config.parameters
        assert not (tmp_path / "runs").exists()
    finally:
        worker.stop()
        service.close()


@pytest.mark.asyncio
async def test_private_worker_does_not_block_shared_loop(tmp_path):
    authority = Authority()
    entered, release = Event(), Event()
    service = authority.service(BriefStore(tmp_path / "brief.db"))
    async def blocking(request):
        entered.set()
        assert release.wait(2)
    service.transport.effect = blocking
    service.submit(event())
    worker = BriefWorker(service)
    future = worker.run_once()
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        for _ in range(10):
            await asyncio.sleep(0)
        assert not future.done()
    finally:
        release.set()
        assert await asyncio.wrap_future(future) == "draft_ready"
        worker.stop()
        service.close()



@pytest.fixture
def local(tmp_path):
    authority = Authority()
    service = authority.service(BriefStore(tmp_path / "brief.db", busy_timeout=1))
    worker = BriefWorker(service, heartbeat_interval=0.02)
    yield service, authority, worker
    worker.stop()
    service.close()


def test_absent_transport_config_and_changed_pin_do_not_call(local):
    service, authority, worker = local
    service.transport = None
    assert service.submit(event()).status == "unavailable"
    from test_brief_generation import FakeTransport
    service.transport = FakeTransport()
    admitted = service.submit(event())
    service.config = replace(authority.config, daily_limit_micros=999999)
    assert worker.run_once().result(2) == "needs_input"
    assert not service.transport.calls
    assert service.store.outbox(SCOPE, admitted.job_id) is None
    assert service.store.failure_cause(SCOPE, admitted.job_id) == "config_changed"


@pytest.mark.parametrize("change", ["acl", "grant", "purpose", "resource", "collector",
                                   "destination", "freshness"])
def test_revoke_after_generation_before_commit_never_publishes(local, change):
    service, authority, worker = local
    admitted = service.submit(event())
    async def revoke(_):
        if change == "acl":
            authority.sources.pop("s1")
        elif change == "grant":
            authority.grant = replace(authority.grant, revision=2)
        elif change in ("purpose", "resource", "collector"):
            field = {"purpose": "purpose", "resource": "resource_id",
                     "collector": "collection_actor_id"}[change]
            old = authority.sources["s1"]
            authority.sources["s1"] = replace(old, source=replace(old.source, **{field: "changed"}))
        elif change == "destination":
            authority.destination = replace(authority.destination, address="changed-dm")
        else:
            authority.sources["s1"] = replace(authority.sources["s1"], fresh_until=at())
    service.transport.effect = revoke
    assert worker.run_once().result(2) == "needs_input"
    assert service.store.outbox(SCOPE, admitted.job_id) is None
    assert service.store.failure_cause(SCOPE, admitted.job_id) == "checkpoint_denied"


def test_completion_rechecks_acl_after_real_exclusive_lock_wait(local, monkeypatch):
    service, authority, worker = local
    admitted = service.submit(event())
    entered, proceed = Event(), Event()
    original = service.store.complete
    def pause(*args, **kwargs):
        entered.set()
        assert proceed.wait(2)
        return original(*args, **kwargs)
    monkeypatch.setattr(service.store, "complete", pause)
    future = worker.run_once()
    assert entered.wait(1)
    attempting = Event()
    connection = service.store._connection
    @contextmanager
    def traced(**kwargs):
        with connection(**kwargs) as db:
            db.set_trace_callback(lambda sql: attempting.set() if sql == "BEGIN EXCLUSIVE" else None)
            yield db
    with sqlite3.connect(service.store.path) as holder:
        holder.execute("BEGIN IMMEDIATE")
        monkeypatch.setattr(service.store, "_connection", traced)
        proceed.set()
        assert attempting.wait(1)
        authority.sources.pop("s1")
        holder.rollback()
    assert future.result(2) == "needs_input"
    with sqlite3.connect(service.store.path) as db:
        assert db.execute("SELECT count(*) FROM brief_revisions").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM brief_outbox").fetchone()[0] == 0
    assert service.store.failure_cause(SCOPE, admitted.job_id) == "authorization_denied"


def test_store_precommit_is_after_relational_writes_and_rejects_flags(local):
    service, authority, _ = local
    admitted = service.submit(event())
    job = service.store.claim_one(WorkerContext("direct"), clock=lambda: at())
    service.store.start_generating(job.lease, clock=lambda: at())
    from ostag.core.schemas import SchemaError
    with pytest.raises(SchemaError):
        service.store.complete(job.lease, draft={}, provenance={"synthetic": True},
            validation_version="v1", precommit=lambda: True, clock=lambda: at())
    assert service.store.get(SCOPE, admitted.job_id).state == "generating"
    assert service.store.outbox(SCOPE, admitted.job_id) is None


@pytest.mark.asyncio
async def test_persisted_requester_cancel_and_late_uncooperative_transport(local):
    service, _, worker = local
    entered, late = Event(), Event()
    async def uncooperative(req):
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            while not late.is_set():
                await asyncio.sleep(0.001)
    service.transport.effect = uncooperative
    admitted = service.submit(event())
    future = worker.run_once()
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        assert (await asyncio.to_thread(service.cancel, SCOPE, admitted.job_id)).status == "cancelled"
        assert await asyncio.wrap_future(future) == "cancelled"
        assert worker.quarantined
        assert worker.run_once().result(1) == "stopped"
        cost = service.store.cost_record(SCOPE, admitted.job_id, "manual-generation-1")
        assert cost.state == "unknown" and cost.charged_micros > 0
        assert service.store.failure_cause(SCOPE, admitted.job_id) == "cancelled"
    finally:
        late.set()
        # Advance the owned loop until the late transport has actually returned.
        done = Event()
        worker._loop.call_soon_threadsafe(lambda: worker._loop.call_later(0.01, done.set))
        assert await asyncio.to_thread(done.wait, 1)
    assert service.store.outbox(SCOPE, admitted.job_id) is None


def test_heartbeat_loss_is_independent_of_blocked_model(local, monkeypatch):
    from threading import current_thread

    service, _, worker = local
    entered, lost, release, inject_loss, renewed = (Event() for _ in range(5))
    renew, load_job = service.store.renew, service.load_job
    failed_renewals = []

    def renewal(*args, **kwargs):
        if not inject_loss.is_set():
            result = renew(*args, **kwargs)
            renewed.set()
            return result
        failed_renewals.append((current_thread(), args[0]))
        lost.set()
        raise LeaseLost("synthetic private error must not escape")

    def delayed_load(job):
        # Force a heartbeat BEFORE model entry. The old always-failing renewal
        # loses the guard here and never reaches the blocked transport.
        assert renewed.wait(1)
        return load_job(job)

    monkeypatch.setattr(service.store, "renew", renewal)
    monkeypatch.setattr(service, "load_job", delayed_load)
    async def blocked(_):
        entered.set()
        assert release.wait(2)
    service.transport.effect = blocked
    admitted = service.submit(event())
    future = worker.run_once()
    try:
        assert entered.wait(1)
        inject_loss.set()
        assert lost.wait(1)
        heartbeat, lease = failed_renewals[0]
        heartbeat.join(1)  # Observe latched loss, not merely entry to renew().
        assert not heartbeat.is_alive()
        assert not future.done()
        assert service.store.assert_current(lease, clock=service.clock).state == "generating"
        assert service.store.outbox(SCOPE, admitted.job_id) is None
    finally:
        release.set()
    assert future.result(2) == "needs_input"
    assert len(service.transport.calls) == 1
    assert service.store.failure_cause(SCOPE, admitted.job_id) == "lease_lost"
    assert service.store.outbox(SCOPE, admitted.job_id) is None
    with pytest.raises(LeaseLost):
        service.store.assert_current(lease, clock=service.clock)


def test_deadline_during_model_keeps_original_timing_and_records_cause(local):
    service, authority, worker = local
    async def deadline(_):
        authority.now = 180
    service.transport.effect = deadline
    admitted = service.submit(event())
    assert worker.run_once().result(2) == "needs_input"
    job = service.store.get(SCOPE, admitted.job_id)
    assert job.first_claim_at == at().replace("+00:00", "Z")
    assert job.deadline == at(180).replace("+00:00", "Z")
    assert service.store.failure_cause(SCOPE, job.job_id) == "lease_lost"
    assert service.store.outbox(SCOPE, job.job_id) is None


@pytest.mark.parametrize("cost_state", [None, "reserved", "started", "unknown", "settled"])
def test_recovery_never_replays_prior_spend_or_resets_first_claim(local, cost_state):
    service, authority, worker = local
    admitted = service.submit(event())
    owner = WorkerContext("crashed-worker")
    job = service.store.claim_one(owner, clock=lambda: at())
    service.store.start_generating(job.lease, clock=lambda: at())
    if cost_state:
        reservation = service.store.reserve_cost(job.lease, call_id="manual-generation-1",
            profile=authority.config.parameters.cost_profile,
            daily_limit_micros=authority.config.daily_limit_micros,
            config_hash=authority.config.config_hash, clock=lambda: at())
        if cost_state != "reserved":
            service.store.start_cost(job.lease, reservation.reservation_id,
                daily_limit_micros=authority.config.daily_limit_micros, clock=lambda: at())
        if cost_state == "unknown":
            service.store.mark_cost_unknown(owner, reservation.reservation_id, "timeout", clock=lambda: at())
        if cost_state == "settled":
            from ostag.briefs.store import Usage
            service.store.settle_cost(owner, reservation.reservation_id, Usage(100, 50, 200), clock=lambda: at())
    authority.now = 31
    status = worker.run_once().result(2)
    current = service.store.get(SCOPE, admitted.job_id)
    assert current.first_claim_at == job.first_claim_at and current.deadline == job.deadline
    if cost_state is None:
        assert status == "draft_ready" and current.attempt == 2
    elif cost_state in ("started", "unknown"):
        # Quarantine BEFORE recovery touches any job; ledger evidence and the
        # expired attempt remain intact for explicit operator reconciliation.
        assert status == "needs_input" and worker.quarantined
        assert current.state == "generating" and current.attempt == 1
        assert current.fence == job.fence
        assert not service.transport.calls
        assert service.store.cost_record(SCOPE, admitted.job_id, "manual-generation-1").state == cost_state
    else:
        assert status == "idle"
        assert current.state == "needs_input"
        assert not service.transport.calls
        assert service.store.failure_cause(SCOPE, admitted.job_id) == "recovery_required"
        cost = service.store.cost_record(SCOPE, admitted.job_id, "manual-generation-1")
        assert cost.state == ("released" if cost_state == "reserved" else cost_state)


def test_first_failure_survives_late_cost_settlement_and_restart(local):
    service, _, worker = local
    async def failed(_):
        raise RuntimeError("private provider body")
    service.transport.effect = failed
    admitted = service.submit(event())
    assert worker.run_once().result(2) == "needs_input"
    cost = service.store.cost_record(SCOPE, admitted.job_id, "manual-generation-1")
    from ostag.briefs.store import Usage
    service.store.settle_cost(worker.context, cost.reservation_id, Usage(100, 50, 200), clock=lambda: at())
    reopened = BriefStore(service.store.path)
    assert reopened.failure_cause(SCOPE, admitted.job_id) == "transport_error"
    assert reopened.cost_record(SCOPE, admitted.job_id, "manual-generation-1").diagnostic == "settled"


def test_lost_completion_response_reconciles_durable_outbox(local, monkeypatch):
    service, _, worker = local
    admitted = service.submit(event())
    original = service.store.complete
    def lost(*args, **kwargs):
        original(*args, **kwargs)
        raise StoreBusy("lost response")
    monkeypatch.setattr(service.store, "complete", lost)
    assert worker.run_once().result(2) == "draft_ready"
    assert service.store.outbox(SCOPE, admitted.job_id) is not None
    assert len(service.transport.calls) == 1
    assert service.cancel(SCOPE, admitted.job_id).status == "cancelled"
    assert service.store.outbox(SCOPE, admitted.job_id).state == "cancelled"
    assert service.store.failure_cause(SCOPE, admitted.job_id) is None


@pytest.mark.parametrize("schema", [1, 2])
def test_optin_migration_preserves_actual_old_rows_and_costs(tmp_path, schema):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as db:
        db.executescript(SCHEMA1_SQL)
    old = BriefStore(path)
    if schema == 2:
        # Actual A03 schema2 with all original schema1 queued/active/ready effects.
        from ostag.core.identity import Principal
        from ostag.briefs.store import RequesterContext
        scope = RequesterContext("migration-fixture", Principal("feishu", "synthetic-alice"))
        job = old.get(scope, "b22e7058b1ac47f48638e14c2e4e4ddc")
        config = GenerationConfig(parameters(), 100000)
        r = old.reserve_cost(job.lease, call_id="old-paid", profile=parameters().cost_profile,
                             daily_limit_micros=100000, config_hash=config.config_hash, clock=lambda: at(6))
        old.start_cost(job.lease, r.reservation_id, daily_limit_micros=100000, clock=lambda: at(6))
        old.mark_cost_unknown(job.lease.worker, r.reservation_id, "timeout", clock=lambda: at(6))
    else:
        # Restore the actual schema1 fixture before exercising both forward steps.
        path.unlink()
        with sqlite3.connect(path) as db:
            db.executescript(SCHEMA1_SQL)
    with sqlite3.connect(path) as db:
        tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        before = {t: db.execute(f"SELECT * FROM {t} ORDER BY 1").fetchall() for t in tables}
    reopened = BriefStore(path)
    reopened.enable_manual()
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 3
        assert {t: db.execute(f"SELECT * FROM {t} ORDER BY 1").fetchall() for t in tables} == before
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_cancel_intent_during_completion_lock_rolls_back_private_rows(local, monkeypatch):
    service, _, worker = local
    admitted = service.submit(event())
    inside, finish = Event(), Event()
    original = service.store.complete
    def pause(*args, **kwargs):
        gate = kwargs["precommit"]
        def precommit():
            inside.set()
            assert finish.wait(2)
            return gate()
        return original(*args, **(kwargs | {"precommit": precommit}))
    monkeypatch.setattr(service.store, "complete", pause)
    future = worker.run_once()
    assert inside.wait(1)
    cancelling = Event()
    connection = service.store._connection
    @contextmanager
    def traced(**kwargs):
        with connection(**kwargs) as db:
            db.set_trace_callback(lambda sql: cancelling.set() if sql == "BEGIN" else None)
            yield db
    monkeypatch.setattr(service.store, "_connection", traced)
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=1) as pool:
        cancel = pool.submit(service.cancel, SCOPE, admitted.job_id)
        assert cancelling.wait(1)
        finish.set()
        cancel.result(2)
        future.result(2)
    with sqlite3.connect(service.store.path) as db:
        assert db.execute("SELECT count(*) FROM brief_revisions").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM brief_outbox").fetchone()[0] == 0


def test_unexpected_precommit_exception_is_sanitized_and_cause_persisted(local, monkeypatch):
    service, _, worker = local
    admitted = service.submit(event())
    def fail(*args, **kwargs):
        raise RuntimeError("private secret provider diagnostic")
    monkeypatch.setattr(service.store, "complete", fail)
    assert worker.run_once().result(2) == "needs_input"
    assert service.store.failure_cause(SCOPE, admitted.job_id) == "commit_failed"


def test_stop_returns_while_sync_transport_blocks_and_late_return_cannot_commit(local):
    service, _, worker = local
    entered, release, returned = Event(), Event(), Event()
    async def blocking(_):
        entered.set()
        assert release.wait(2)
        returned.set()
    service.transport.effect = blocking
    admitted = service.submit(event())
    future = worker.run_once()
    assert entered.wait(1)
    try:
        assert worker.stop(timeout=0.01) == "local_stopping"
        assert future.result(1) == "stopped"
        assert worker.run_once().result(1) == "stopped"
        assert service.store.cost_record(SCOPE, admitted.job_id, "manual-generation-1").state == "started"
    finally:
        release.set()
        assert returned.wait(1)
        worker._thread.join(1)
    assert not worker._thread.is_alive()
    assert service.store.outbox(SCOPE, admitted.job_id) is None
    assert service.store.failure_cause(SCOPE, admitted.job_id) == "stopped"


def test_guard_loss_retires_by_maintenance_cas_not_owner_finish(local, monkeypatch):
    service, _, worker = local
    entered, release, lost, inject_loss = (Event() for _ in range(4))
    original_renew = service.store.renew
    def renew(*args, **kwargs):
        if not inject_loss.is_set():
            return original_renew(*args, **kwargs)
        lost.set()
        raise LeaseLost("heartbeat ownership lost")
    def forbidden_finish(*args, **kwargs):
        raise AssertionError("lost guard used the active owner finish path")
    monkeypatch.setattr(service.store, "renew", renew)
    monkeypatch.setattr(service.store, "finish", forbidden_finish)
    async def block(_):
        entered.set()
        assert release.wait(2)
    service.transport.effect = block
    admitted = service.submit(event())
    future = worker.run_once()
    try:
        assert entered.wait(1)
        inject_loss.set()
        assert lost.wait(1)
    finally:
        release.set()
    assert future.result(2) == "needs_input"
    assert service.store.failure_cause(SCOPE, admitted.job_id) == "lease_lost"


def test_restart_does_not_start_another_job_while_prior_call_is_unknown(local):
    service, authority, worker = local
    first = service.submit(event())
    async def failed(_):
        raise RuntimeError("uncertain invocation")
    service.transport.effect = failed
    assert worker.run_once().result(2) == "needs_input"
    second = service.submit(event(event_id="second"))
    restarted = BriefWorker(service)
    try:
        assert restarted.run_once().result(2) == "needs_input"
        assert len(service.transport.calls) == 1
        assert service.store.get(SCOPE, second.job_id).state == "queued"
        assert service.store.cost_record(SCOPE, first.job_id, "manual-generation-1").state == "unknown"
    finally:
        restarted.stop()


def test_cancelled_result_future_does_not_release_worker_submission_slot(local):
    service, _, worker = local
    entered, release = Event(), Event()
    async def blocked(_):
        entered.set()
        assert release.wait(2)
    service.transport.effect = blocked
    service.submit(event())
    future = worker.run_once()
    assert entered.wait(1)
    try:
        future.cancel()
        second = worker.run_once()
        assert second.done() and second.result() == "busy"
    finally:
        release.set()
        worker.stop()


def test_egress_rechecks_sources_after_cost_database_work(local, monkeypatch):
    service, authority, worker = local
    admitted = service.submit(event())
    original = service.store.start_cost
    def revoke_after_db(*args, **kwargs):
        result = original(*args, **kwargs)
        authority.sources.pop("s1")
        return result
    monkeypatch.setattr(service.store, "start_cost", revoke_after_db)
    assert worker.run_once().result(2) == "needs_input"
    assert not service.transport.calls
    assert service.store.failure_cause(SCOPE, admitted.job_id) == "checkpoint_denied"
    assert service.store.cost_record(SCOPE, admitted.job_id, "manual-generation-1").state == "unknown"


def test_foreign_meeting_payload_fails_closed_without_generation(local):
    service, _, worker = local
    from ostag.briefs.store import PrivateDestination
    from test_brief_validation import ACTOR
    job = service.store.admit(SCOPE, idempotency_key="meeting", payload={
        "kind": "meeting-frozen-scope", "accepted": True, "authorized": True},
        grant_id="grant", grant_revision=1, destination=PrivateDestination(ACTOR, "alice-dm"), now=at())
    assert worker.run_once().result(2) == "needs_input"
    assert not service.transport.calls
    assert service.store.outbox(SCOPE, job.job_id) is None


def test_precommit_callback_elapsed_deadline_rolls_back(local):
    service, authority, _ = local
    admitted = service.submit(event())
    job = service.store.claim_one(WorkerContext("direct"), clock=lambda: at())
    service.store.start_generating(job.lease, clock=lambda: at())
    from ostag.briefs.store import LeaseLost
    def expire():
        authority.now = 180
    with pytest.raises(LeaseLost):
        service.store.complete(job.lease, draft={}, provenance={"test": True}, validation_version="v1",
                               clock=service.clock, precommit=expire)
    assert service.store.outbox(SCOPE, admitted.job_id) is None
    assert service.store.get(SCOPE, admitted.job_id).state == "generating"


def test_operator_cancel_intent_wins_race_with_worker_failure(local, monkeypatch):
    service, _, worker = local
    entered, cancelling, release = Event(), Event(), Event()
    async def pending(_):
        entered.set()
        await asyncio.Event().wait()
    service.transport.effect = pending
    admitted = service.submit(event())
    future = worker.run_once()
    assert entered.wait(1)
    original = service.store.cancel
    from threading import current_thread
    def delayed_cancel(*args, **kwargs):
        if current_thread().name != "brief-worker":
            cancelling.set()
            assert release.wait(2)
        return original(*args, **kwargs)
    monkeypatch.setattr(service.store, "cancel", delayed_cancel)
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=1) as pool:
        cancel = pool.submit(service.cancel, SCOPE, admitted.job_id)
        try:
            assert cancelling.wait(1)
            assert future.result(1) == "cancelled"
        finally:
            release.set()
        assert cancel.result(1).status == "cancelled"
    assert service.store.failure_cause(SCOPE, admitted.job_id) == "cancelled"


@pytest.mark.parametrize("action", ["run_once", "stop", "block"])
def test_future_callbacks_never_hold_worker_or_stop_hostage(tmp_path, action):
    from threading import Thread
    service = Authority().service(BriefStore(tmp_path / "callbacks.db"))
    worker = BriefWorker(service)
    entered, release, callback_entered, callback_returned = Event(), Event(), Event(), Event()
    unblock, stop_returned = Event(), Event()

    async def execute():
        entered.set()
        while not release.is_set():
            await asyncio.sleep(0.001)
        return "idle"

    worker._execute = execute
    future = worker.run_once()
    assert entered.wait(1)

    def callback(_):
        callback_entered.set()
        if action == "run_once":
            worker.run_once()
        elif action == "stop":
            worker.stop(timeout=0.01)
        else:
            unblock.wait(2)
        callback_returned.set()

    future.add_done_callback(callback)
    release.set()
    assert callback_entered.wait(1)
    stopper = Thread(target=lambda: (worker.stop(timeout=0.05), stop_returned.set()), daemon=True)
    stopper.start()
    try:
        assert stop_returned.wait(0.5), "bounded stop blocked behind a Future callback"
        if action != "block":
            assert callback_returned.wait(0.5), "reentrant Future callback deadlocked"
    finally:
        unblock.set()
        service.close()


def test_stop_publishes_stopped_without_running_slow_callbacks_on_caller(local):
    from threading import Thread
    service, _, worker = local
    entered, release, callback_entered, unblock, stopped = (Event() for _ in range(5))
    async def execute():
        entered.set()
        release.wait(2)
        return "idle"
    worker._execute = execute
    future = worker.run_once()
    assert entered.wait(1)
    future.add_done_callback(lambda _: (callback_entered.set(), unblock.wait(2)))
    thread = Thread(target=lambda: (worker.stop(timeout=0.01), stopped.set()), daemon=True)
    thread.start()
    try:
        assert callback_entered.wait(1)
        assert stopped.wait(0.3), "stop itself ran arbitrary Future callbacks"
        assert future.result(0.1) == "stopped"
    finally:
        unblock.set()
        release.set()
        thread.join(1)


def test_idle_store_busy_is_retryable_without_quarantine(local):
    service, _, worker = local
    service.store.busy_timeout = 0.01
    with sqlite3.connect(service.store.path) as holder:
        holder.execute("BEGIN EXCLUSIVE")
        assert worker.run_once().result(1) == "store_unavailable"
        assert not worker.quarantined
        holder.rollback()
    assert worker.run_once().result(1) == "idle"


def test_healthy_heartbeat_contention_recovers_without_unknown_spend(local, monkeypatch):
    service, _, worker = local
    service.store.busy_timeout = 0.01
    entered, release, busy, renewed = (Event() for _ in range(4))
    original = service.store.renew
    def renew(*args, **kwargs):
        try:
            result = original(*args, **kwargs)
        except StoreBusy:
            busy.set()
            raise
        if busy.is_set():
            renewed.set()
        return result
    monkeypatch.setattr(service.store, "renew", renew)
    async def transport(_):
        entered.set()
        while not release.is_set():
            await asyncio.sleep(0.001)
    service.transport.effect = transport
    admitted = service.submit(event())
    future = worker.run_once()
    try:
        assert entered.wait(1)
        with sqlite3.connect(service.store.path) as holder:
            holder.execute("BEGIN EXCLUSIVE")
            assert busy.wait(1)
            holder.rollback()
        assert renewed.wait(0.5), "transient busy permanently lost a healthy lease"
    finally:
        release.set()
    assert future.result(2) == "draft_ready"
    assert not worker.quarantined
    assert service.store.failure_cause(SCOPE, admitted.job_id) is None
    assert service.store.cost_record(SCOPE, admitted.job_id, "manual-generation-1").state == "settled"


def test_unknown_cost_does_not_consume_unrelated_expired_attempt(local):
    service, authority, worker = local
    first = service.submit(event())
    job = service.store.claim_one(WorkerContext("old"), clock=service.clock)
    service.store.start_generating(job.lease, clock=service.clock)
    reservation = service.store.reserve_cost(job.lease, call_id="manual-generation-1",
        profile=authority.config.parameters.cost_profile,
        daily_limit_micros=authority.config.daily_limit_micros,
        config_hash=authority.config.config_hash, clock=service.clock)
    service.store.start_cost(job.lease, reservation.reservation_id,
        daily_limit_micros=authority.config.daily_limit_micros, clock=service.clock)
    service.store.cancel(SCOPE, first.job_id, clock=service.clock)
    second = service.submit(event(event_id="second"))
    unrelated = service.store.claim_one(WorkerContext("other"), clock=service.clock)
    authority.now = 31
    assert worker.run_once().result(1) == "needs_input"
    assert service.store.get(SCOPE, second.job_id) == unrelated
    assert not service.transport.calls


@pytest.mark.parametrize("boundary", ["lease", "deadline", "monotonic", "backward", "invalid"])
def test_busy_grace_cannot_outlive_confirmed_time_bounds(local, monkeypatch, boundary):
    from ostag.briefs.store import LeaseGuard, LeaseLost
    service, authority, _ = local
    service.store.busy_timeout = 0.01
    service.submit(event())
    job = service.store.claim_one(WorkerContext("guard-test"), clock=service.clock)
    service.store.start_generating(job.lease, clock=service.clock)
    busy = Event()
    original = service.store.renew
    def renew(*args, **kwargs):
        try:
            return original(*args, **kwargs)
        except StoreBusy:
            busy.set()
            raise
    monkeypatch.setattr(service.store, "renew", renew)
    tick = [0.0]
    monkeypatch.setattr("ostag.briefs.store.monotonic", lambda: tick[0])
    def clock():
        return "invalid" if boundary == "invalid" and authority.now else at(authority.now)
    with LeaseGuard(service.store, job.lease, clock=clock, interval=0.01, retry_busy=True) as guard:
        with sqlite3.connect(service.store.path) as holder:
            holder.execute("BEGIN EXCLUSIVE")
            assert busy.wait(1)
            if boundary == "monotonic":
                tick[0] = 30.0  # no sleep, and wall clock remains frozen
            else:
                authority.now = {"lease": 30, "deadline": 180, "backward": -1, "invalid": 1}[boundary]
            assert guard.stop_requested.wait(1)
            assert guard.lost
            holder.rollback()
        # Even restoring the clock and releasing contention cannot revive loss.
        authority.now = 0
        with pytest.raises(LeaseLost):
            guard.before_io()
        with pytest.raises(LeaseLost):
            guard.complete(draft={}, provenance={}, validation_version="v1")
    current = service.store.get(SCOPE, job.job_id)
    assert current.lease_until == job.lease_until and current.deadline == job.deadline
    assert not service.transport.calls
    assert service.store.outbox(SCOPE, job.job_id) is None


def test_contention_past_lease_during_paid_call_cannot_commit(local, monkeypatch):
    from ostag.briefs.store import LeaseGuard
    service, authority, worker = local
    service.store.busy_timeout = 0.01
    entered, release, busy, lost = (Event() for _ in range(4))
    original_renew, original_lose = service.store.renew, LeaseGuard._lose
    def renew(*args, **kwargs):
        try:
            return original_renew(*args, **kwargs)
        except StoreBusy:
            busy.set()
            raise
    def lose(guard, exc):
        original_lose(guard, exc)
        lost.set()
    monkeypatch.setattr(service.store, "renew", renew)
    monkeypatch.setattr(LeaseGuard, "_lose", lose)
    async def blocked(_):
        entered.set()
        # Block owned model loop: independent heartbeat must still lose expiry.
        assert release.wait(2)
    service.transport.effect = blocked
    admitted = service.submit(event())
    future = worker.run_once()
    try:
        assert entered.wait(1)
        with sqlite3.connect(service.store.path) as holder:
            holder.execute("BEGIN EXCLUSIVE")
            assert busy.wait(1)
            authority.now = 30
            assert lost.wait(1)
            holder.rollback()
    finally:
        release.set()
    assert future.result(2) == "needs_input"
    assert service.store.outbox(SCOPE, admitted.job_id) is None
    assert service.store.failure_cause(SCOPE, admitted.job_id) == "lease_lost"
    cost = service.store.cost_record(SCOPE, admitted.job_id, "manual-generation-1")
    assert cost.charged_micros > 0 and cost.state in ("settled", "unknown")


def test_run_once_handles_loop_close_between_latch_and_schedule(local, monkeypatch):
    _, _, worker = local
    def closed(*args, **kwargs):
        raise RuntimeError("Event loop is closed")
    with monkeypatch.context() as patch:
        patch.setattr(worker._loop, "call_soon_threadsafe", closed)
        assert worker.run_once().result(1) == "stopped"
    assert worker.run_once().result(1) == "idle", "failed scheduling leaked live work slot"


def test_stop_on_owned_execution_thread_never_joins_itself(local):
    _, _, worker = local
    async def execute():
        assert worker.stop(timeout=0.01) == "local_stopping"
        return "idle"
    worker._execute = execute
    assert worker.run_once().result(1) == "stopped"


@pytest.mark.parametrize("publisher", ["dispatch", "stop"])
def test_result_cancellation_race_keeps_completion_thread_usable(local, monkeypatch, publisher):
    _, _, worker = local
    entered, release, publishing, complete, notified = (Event() for _ in range(5))
    async def execute():
        entered.set()
        while not release.is_set():
            await asyncio.sleep(0.001)
        return "idle"
    worker._execute = execute
    future = worker.run_once()
    assert entered.wait(1)
    original = future.set_result
    def set_result(value):
        publishing.set()
        assert complete.wait(2)
        try:
            original(value)
        finally:
            notified.set()
    monkeypatch.setattr(future, "set_result", set_result)
    try:
        if publisher == "stop":
            worker.stop(timeout=0.01)
        else:
            release.set()
        assert publishing.wait(1)
        assert future.cancel()
    finally:
        complete.set()
        release.set()
    assert notified.wait(1)
    worker.stop(timeout=0.05)
    worker._notifier.join(1)
    assert not worker._notifier.is_alive()
    assert future.cancelled()


def test_idle_clock_failure_requires_fresh_checks_without_quarantine(local):
    from ostag.briefs.store import ClockError
    service, authority, worker = local
    def broken():
        raise ClockError("unavailable trusted clock")
    service.clock = broken
    assert worker.run_once().result(1) == "store_unavailable"
    assert not worker.quarantined
    service.clock = lambda: at(authority.now)
    assert worker.run_once().result(1) == "idle"


@pytest.mark.parametrize("operation", ["settle_cost", "assert_current", "complete"])
def test_received_response_survives_bounded_database_contention(local, monkeypatch, operation):
    """An already received response is retained while safe DB work retries."""
    service, _, worker = local
    service.store.busy_timeout = 0.01
    responded, boundary, locked, busy = (Event() for _ in range(4))
    original = getattr(service.store, operation)
    intercepted = False

    async def transport(_):
        responded.set()

    def contended(*args, **kwargs):
        nonlocal intercepted
        if responded.is_set() and not intercepted:
            intercepted = True
            boundary.set()
            assert locked.wait(1)
        try:
            return original(*args, **kwargs)
        except StoreBusy:
            busy.set()
            raise

    service.transport.effect = transport
    monkeypatch.setattr(service.store, operation, contended)
    admitted = service.submit(event())
    future = worker.run_once()
    try:
        assert boundary.wait(1)
        with sqlite3.connect(service.store.path) as holder:
            holder.execute("BEGIN EXCLUSIVE")
            locked.set()
            assert busy.wait(1), "test must exercise a real SQLite busy timeout"
            holder.rollback()
    finally:
        locked.set()
    assert future.result(2) == "draft_ready"
    assert len(service.transport.calls) == 1
    cost = service.store.cost_record(SCOPE, admitted.job_id, "manual-generation-1")
    assert cost.state == "settled"
    assert cost.usage.to_dict() == {"input_tokens": 100, "output_tokens": 50, "cost_micros": 200}
    assert service.store.failure_cause(SCOPE, admitted.job_id) is None
    assert service.store.outbox(SCOPE, admitted.job_id).state == "pending"
    assert not worker.quarantined


@pytest.mark.parametrize("operation", ["reserve_cost", "complete"])
@pytest.mark.parametrize("expiry", ["lease", "deadline", "monotonic"])
def test_database_retry_cannot_cross_confirmed_bounds(local, monkeypatch, operation, expiry):
    service, authority, worker = local
    service.store.busy_timeout = 0.01
    boundary, locked, busy, advance = (Event() for _ in range(4))
    original = getattr(service.store, operation)
    intercepted = False
    tick = [0.0]
    monkeypatch.setattr("ostag.briefs.store.monotonic", lambda: tick[0])

    def contended(*args, **kwargs):
        nonlocal intercepted
        if not intercepted:
            intercepted = True
            boundary.set()
            assert locked.wait(1)
        try:
            return original(*args, **kwargs)
        except StoreBusy:
            busy.set()
            assert advance.wait(1)
            raise

    monkeypatch.setattr(service.store, operation, contended)
    admitted = service.submit(event())
    future = worker.run_once()
    try:
        assert boundary.wait(1)
        with sqlite3.connect(service.store.path) as holder:
            holder.execute("BEGIN EXCLUSIVE")
            locked.set()
            assert busy.wait(1)
            if expiry == "monotonic":
                tick[0] = 30.0
            else:
                authority.now = 30 if expiry == "lease" else 180
            holder.rollback()
            advance.set()
    finally:
        locked.set()
        advance.set()
    assert future.result(2) == "needs_input"
    assert service.store.outbox(SCOPE, admitted.job_id) is None
    assert service.store.failure_cause(SCOPE, admitted.job_id) == "lease_lost"
    assert len(service.transport.calls) == (0 if operation == "reserve_cost" else 1)
    if operation == "complete":
        assert service.store.cost_record(SCOPE, admitted.job_id, "manual-generation-1").state == "settled"
    with sqlite3.connect(service.store.path) as db:
        assert db.execute("SELECT count(*) FROM brief_revisions").fetchone()[0] == 0


def test_completion_retry_reauthorizes_after_busy_before_accepting(local, monkeypatch):
    service, authority, worker = local
    service.store.busy_timeout = 0.01
    boundary, locked, busy = (Event() for _ in range(3))
    original = service.store.complete
    intercepted = False

    def contended(*args, **kwargs):
        nonlocal intercepted
        if not intercepted:
            intercepted = True
            boundary.set()
            assert locked.wait(1)
        try:
            return original(*args, **kwargs)
        except StoreBusy:
            busy.set()
            raise

    monkeypatch.setattr(service.store, "complete", contended)
    admitted = service.submit(event())
    future = worker.run_once()
    try:
        assert boundary.wait(1)
        with sqlite3.connect(service.store.path) as holder:
            holder.execute("BEGIN EXCLUSIVE")
            locked.set()
            assert busy.wait(1)
            authority.sources.pop("s0")
            holder.rollback()
    finally:
        locked.set()
    assert future.result(2) == "needs_input"
    assert len(service.transport.calls) == 1
    assert service.store.cost_record(SCOPE, admitted.job_id, "manual-generation-1").state == "settled"
    assert service.store.outbox(SCOPE, admitted.job_id) is None
    with sqlite3.connect(service.store.path) as db:
        assert db.execute("SELECT count(*) FROM brief_revisions").fetchone()[0] == 0


@pytest.mark.parametrize("boundary", ["lease", "deadline", "monotonic", "stop"])
def test_first_checkpoint_contention_is_bounded_before_guard_confirmation(local, monkeypatch, boundary):
    service, authority, worker = local
    service.store.busy_timeout = 0.01
    entered, locked, busy = (Event() for _ in range(3))
    original = service.store.assert_current
    tick = [0.0]
    monkeypatch.setattr("ostag.briefs.store.monotonic", lambda: tick[0])

    def checkpoint(*args, **kwargs):
        entered.set()
        assert locked.wait(1)
        try:
            return original(*args, **kwargs)
        except StoreBusy:
            busy.set()
            raise

    monkeypatch.setattr(service.store, "assert_current", checkpoint)
    admitted = service.submit(event())
    future = worker.run_once()
    assert entered.wait(1)
    try:
        with sqlite3.connect(service.store.path) as holder:
            holder.execute("BEGIN EXCLUSIVE")
            locked.set()
            assert busy.wait(1)
            if boundary == "stop":
                worker.stop(timeout=0.05)
                worker._thread.join(0.8)
                assert not worker._thread.is_alive()
            else:
                if boundary == "monotonic":
                    tick[0] = 1_000_000
                else:
                    authority.now = 30 if boundary == "lease" else 180
                assert future.result(0.8) in ("needs_input", "store_unavailable")
                assert not worker.quarantined
            # The foreign lock is STILL held when custody must return.
            holder.rollback()
    finally:
        locked.set()
    assert not service.transport.calls
    assert service.store.outbox(SCOPE, admitted.job_id) is None


@pytest.mark.parametrize("operation", ["assert_current", "cost_record", "start_generating"])
def test_pre_io_database_contention_preserves_healthy_job(local, monkeypatch, operation):
    service, _, worker = local
    service.store.busy_timeout = 0.01
    entered, locked, busy = (Event() for _ in range(3))
    original = getattr(service.store, operation)
    intercepted = False

    def contended(*args, **kwargs):
        nonlocal intercepted
        if not intercepted:
            intercepted = True
            entered.set()
            assert locked.wait(1)
        try:
            return original(*args, **kwargs)
        except StoreBusy:
            busy.set()
            raise

    monkeypatch.setattr(service.store, operation, contended)
    admitted = service.submit(event())
    future = worker.run_once()
    try:
        assert entered.wait(1)
        with sqlite3.connect(service.store.path) as holder:
            holder.execute("BEGIN EXCLUSIVE")
            locked.set()
            assert busy.wait(1)
            holder.rollback()
    finally:
        locked.set()
    assert future.result(2) == "draft_ready"
    assert len(service.transport.calls) == 1
    assert service.store.failure_cause(SCOPE, admitted.job_id) is None
    assert not worker.quarantined


@pytest.mark.parametrize("operation", ["outbox", "get", "record_expired_failure", "cost_record"])
@pytest.mark.parametrize("release_lock", [True, False])
def test_unpaid_failure_reconciliation_contention_never_quarantines(
        local, monkeypatch, operation, release_lock):
    service, authority, worker = local
    service.store.busy_timeout = 0.01
    admitted = service.submit(event())
    service.config = replace(authority.config, daily_limit_micros=999999)
    entered, locked, busy = (Event() for _ in range(3))
    original = getattr(service.store, operation)
    intercepted = False

    def contended(*args, **kwargs):
        nonlocal intercepted
        if not intercepted:
            intercepted = True
            entered.set()
            assert locked.wait(1)
        try:
            return original(*args, **kwargs)
        except StoreBusy:
            busy.set()
            raise

    monkeypatch.setattr(service.store, operation, contended)
    future = worker.run_once()
    try:
        assert entered.wait(1)
        with sqlite3.connect(service.store.path) as holder:
            holder.execute("BEGIN EXCLUSIVE")
            locked.set()
            assert busy.wait(1)
            if release_lock:
                holder.rollback()
                assert future.result(2) == "needs_input"
            else:
                assert future.result(0.8) == "store_unavailable"
            assert not worker.quarantined
            holder.rollback()
    finally:
        locked.set()
    assert not service.transport.calls
    assert not service.store.has_uncertain_cost(worker.context)
    assert worker.run_once().result(1) != "stopped"
    if release_lock:
        assert service.store.failure_cause(SCOPE, admitted.job_id) == "config_changed"


def test_failure_maintenance_retries_renewal_cas_without_changing_owner(local, monkeypatch):
    service, authority, worker = local
    admitted = service.submit(event())
    service.config = replace(authority.config, daily_limit_micros=999999)
    original = service.store.record_expired_failure
    raced = False

    def race(context, expected, **kwargs):
        nonlocal raced
        if not raced:
            raced = True
            authority.now = 1
            service.store.renew(expected.lease, clock=service.clock)
        return original(context, expected, **kwargs)

    monkeypatch.setattr(service.store, "record_expired_failure", race)
    assert worker.run_once().result(2) == "needs_input"
    assert service.store.failure_cause(SCOPE, admitted.job_id) == "config_changed"
    assert not service.transport.calls


@pytest.mark.parametrize("failure", ["busy", "clock"])
def test_recovery_lookup_unavailable_does_not_retire_or_consume_attempt(local, monkeypatch, failure):
    from ostag.briefs.store import ClockError
    service, authority, worker = local
    admitted = service.submit(event())
    claimed = service.store.claim_one(WorkerContext("previous"), clock=service.clock)
    authority.now = 31
    original = service.store.cost_record

    def unavailable(*args, **kwargs):
        raise (StoreBusy if failure == "busy" else ClockError)("trusted read unavailable")

    monkeypatch.setattr(service.store, "cost_record", unavailable)
    assert worker.run_once().result(1) == "store_unavailable"
    assert service.store.get(SCOPE, admitted.job_id) == claimed
    assert not worker.quarantined
    assert not service.transport.calls
    monkeypatch.setattr(service.store, "cost_record", original)
    assert worker.run_once().result(2) == "draft_ready"
    final = service.store.get(SCOPE, admitted.job_id)
    assert final.first_claim_at == claimed.first_claim_at
    assert final.deadline == claimed.deadline
    assert final.attempt == 2
    assert len(service.transport.calls) == 1


def test_optin_guard_without_claim_snapshot_never_retries_unconfirmed_busy(local):
    from ostag.briefs.store import LeaseGuard
    service, _, _ = local
    service.store.busy_timeout = 0.01
    service.submit(event())
    job = service.store.claim_one(WorkerContext("unseeded"), clock=service.clock)
    with sqlite3.connect(service.store.path) as holder:
        holder.execute("BEGIN EXCLUSIVE")
        guard = LeaseGuard(service.store, job.lease, clock=service.clock, retry_busy=True)
        with pytest.raises(LeaseLost):
            with guard:
                pytest.fail("an unavailable initial checkpoint must not authorize IO")
        assert guard.lost
        holder.rollback()
    assert not service.transport.calls
