"""Factory composition through real authority, worker and local SQLite."""
import sqlite3
from threading import Event

import pytest

from ostag.briefs.generation import GenerationConfig
from ostag.briefs.lifecycle import BriefLifecycle, LifecycleDenied
from ostag.briefs.store import BriefStore
from ostag.briefs.worker import BriefWorker
from test_brief_authority import Clock, snapshot
from test_brief_generation import FakeTransport, parameters
from test_brief_service import SCOPE, event


@pytest.fixture
def compose(tmp_path):
    authority = Clock().authority()
    authority.publish(snapshot())
    services, workers = [], []

    def create(**kwargs):
        service = authority.create_service(store=BriefStore(tmp_path / "brief.db"),
            transport=FakeTransport(), config=GenerationConfig(parameters(), 1000000), **kwargs)
        services.append(service)
        worker = BriefWorker(service)
        workers.append(worker)
        return authority, service, worker

    yield create
    for worker in workers:
        worker.stop()
    for service in services:
        service.close()


def assert_empty(store):
    with sqlite3.connect(store.path) as db:
        for table in ("brief_jobs", "brief_intakes", "brief_cost_profiles",
                      "brief_cost_calls", "brief_revisions", "brief_outbox"):
            assert db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0


def test_absent_control_denies_without_rows_or_provider_io(compose):
    life = BriefLifecycle()
    before = life.snapshot()
    _, service, worker = compose(lifecycle=life)
    assert service.lifecycle is worker.lifecycle is life
    assert life.snapshot() == before
    assert service.submit(event()).status == "unavailable"
    assert worker.run_once().result(5) == "unavailable"
    assert_empty(service.store)
    assert service.transport.calls == []
    assert life.snapshot() == before


@pytest.mark.parametrize("binding", ["omitted", "none", "on"])
def test_factory_intake_and_worker_publish_private_draft(compose, binding):
    life = BriefLifecycle() if binding == "on" else None
    if life is not None:
        life.apply(7, "on", False)
        before = life.snapshot()
    _, service, worker = compose(**({} if binding == "omitted" else {"lifecycle": life}))
    assert service.lifecycle is worker.lifecycle is life
    if life is not None:
        assert life.snapshot() == before
    admitted = service.submit(event())
    assert admitted.status == "queued"
    assert worker.run_once().result(5) == "draft_ready"
    assert service.store.get(SCOPE, admitted.job_id).state == "draft_ready"
    box = service.store.outbox(SCOPE, admitted.job_id)
    assert box.state == "pending" and box.destination.address == "alice-dm"
    assert box.revision.payload["status"] == "partial_semantic_review_required"
    assert len(service.transport.calls) == 1
    if life is not None:
        life.apply(8, "off", False)
        assert life.snapshot().drained


def test_pause_before_worker_preserves_queued_job_without_provider_io(compose):
    life = BriefLifecycle()
    life.apply(1, "on", False)
    _, service, worker = compose(lifecycle=life)
    admitted = service.submit(event())
    assert admitted.status == "queued"
    before = service.store.get(SCOPE, admitted.job_id)
    life.apply(2, "off", False)
    assert worker.run_once().result(5) == "unavailable"
    assert service.store.get(SCOPE, admitted.job_id) == before
    assert service.store.outbox(SCOPE, admitted.job_id) is None
    assert service.transport.calls == []
    assert life.snapshot().drained


def test_pause_during_provider_keeps_custody_until_actual_finish(compose):
    life = BriefLifecycle()
    life.apply(1, "on", False)
    _, service, worker = compose(lifecycle=life)
    entered, release = Event(), Event()

    async def held(_):
        # Hold the actual transport on its private worker loop until explicitly released.
        entered.set()
        assert release.wait(10)

    service.transport.effect = held
    admitted = service.submit(event())
    assert admitted.status == "queued"
    future = worker.run_once()
    try:
        assert entered.wait(5)
        life.apply(2, "off", False)
        paused = life.snapshot()
        assert paused.custody["model"] == paused.custody["worker"] == 1
        assert not paused.drained and not future.done()
        assert service.store.get(SCOPE, admitted.job_id).state == "generating"
        assert service.store.outbox(SCOPE, admitted.job_id) is None
        release.set()
        assert future.result(5) == "needs_input"
        assert service.store.get(SCOPE, admitted.job_id).state == "needs_input"
        assert service.store.outbox(SCOPE, admitted.job_id) is None
        assert len(service.transport.calls) == 1
        finished = life.snapshot()
        assert finished.custody["model"] == finished.custody["worker"] == 0
        assert finished.drained and not finished.uncertain
    finally:
        release.set()
        worker.stop(timeout=5)


@pytest.mark.parametrize("phase", ["admission", "before_worker", "after_provider"])
def test_authority_invalidation_denies_while_lifecycle_stays_on(compose, phase):
    life = BriefLifecycle()
    life.apply(1, "on", False)
    authority, service, worker = compose(lifecycle=life)
    if phase == "admission":
        authority.invalidate()
        assert service.submit(event()).status == "denied"
        assert_empty(service.store)
    else:
        admitted = service.submit(event())
        assert admitted.status == "queued"
        if phase == "before_worker":
            authority.invalidate()
        else:
            async def invalidate(_):
                authority.invalidate()
            service.transport.effect = invalidate
        assert worker.run_once().result(5) == "needs_input"
        assert service.store.get(SCOPE, admitted.job_id).state == "needs_input"
        assert service.store.outbox(SCOPE, admitted.job_id) is None
    assert len(service.transport.calls) == (phase == "after_provider")
    assert life.snapshot().control_valid and life.snapshot().admission == "on"


@pytest.mark.parametrize("binding", [object(), False, "on"])
def test_invalid_lifecycle_rejected_before_constructor_side_effects(tmp_path, monkeypatch, binding):
    authority = Clock().authority()
    authority.publish(snapshot())
    store = BriefStore(tmp_path / "brief.db")
    transport = FakeTransport()
    before = store.path.read_bytes()

    def forbidden(*args, **kwargs):
        pytest.fail("invalid lifecycle reached a service constructor side effect")

    monkeypatch.setattr("ostag.briefs.service.ThreadPoolExecutor", forbidden)
    monkeypatch.setattr(store, "enable_manual", forbidden)
    with pytest.raises(LifecycleDenied):
        authority.create_service(store=store, transport=transport,
            config=GenerationConfig(parameters(), 1000000), lifecycle=binding)
    assert store.path.read_bytes() == before
    assert transport.calls == []
