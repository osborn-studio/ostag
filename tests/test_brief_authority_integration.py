"""A11b real admission/worker/checkpoint coverage using synthetic manual input."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
import hashlib
import sqlite3
from threading import Event

import httpx
import pytest

from ostag.briefs.authority import TrustedBriefAuthority
from ostag.briefs.generation import GenerationConfig
from ostag.briefs.retention import RetentionCoordinator, RetentionLedger, RetentionPolicy
from ostag.briefs.service import BriefService, ManualRequest
from ostag.briefs.store import BriefStore, PrivateDestination, RequesterContext
from ostag.briefs.worker import BriefWorker
from ostag.core.identity import Principal
from ostag.core.schemas import SchemaError
from test_brief_authority import Clock, snapshot
from test_brief_generation import FakeTransport, at, parameters
from test_brief_service import SCOPE, event, request


@pytest.fixture
def composed(tmp_path):
    clock = Clock()
    authority = clock.authority()
    authority.publish(snapshot())
    service = authority.create_service(store=BriefStore(tmp_path / "brief.db"),
        transport=FakeTransport(), config=GenerationConfig(parameters(), 1000000))
    yield authority, service, clock
    service.close()


@pytest.fixture
def retained(tmp_path):
    clock = Clock()
    authority = clock.authority()
    authority.publish(snapshot())
    store = BriefStore(tmp_path / "brief.db")
    ledger = RetentionLedger(tmp_path / "retention.db")
    ledger.initialize()
    coordinator = RetentionCoordinator(ledger=ledger, store=store, clock=lambda: at(clock.wall))
    service = authority.create_service(store=store, transport=FakeTransport(),
        config=GenerationConfig(parameters(), 1000000), retention=coordinator.guard(store))
    yield authority, service, clock, coordinator
    service.close()


def check(service, **kwargs):
    return service.authorize_current(ManualRequest.from_dict(request()), SCOPE,
        snapshot().destinations[0][1], stopped=kwargs.get("stopped", lambda: False))


def test_actual_admission_and_worker_generate_private_draft(composed):
    _, service, _ = composed
    admitted = service.submit(event())
    assert admitted.status == "queued"
    worker = BriefWorker(service)
    try:
        assert worker.run_once().result(3) == "draft_ready"
        box = service.store.outbox(SCOPE, admitted.job_id)
        assert box.destination.address == "alice-dm"
        assert box.revision.payload["status"] == "partial_semantic_review_required"
        assert len(service.transport.calls) == 1
    finally:
        worker.stop()


def mutate(authority, clock, kind):
    value = snapshot(2)
    if kind == "invalidate":
        authority.invalidate()
        return
    if kind == "stale":
        clock.wall = clock.mono = 60
        return
    if kind in ("identity", "source", "grant", "destination"):
        value = replace(value, **{dict(identity="permissions", source="sources",
            grant="grants", destination="destinations")[kind]: ()})
    elif kind == "permission":
        value = replace(value, permissions=((SCOPE.principal, "read"),))
    elif kind == "binding":
        value = replace(value, destinations=((SCOPE, PrivateDestination(SCOPE.principal, "new-dm")),))
    elif kind in ("content", "source_revision"):
        sources = list(value.sources)
        change = (dict(excerpt="new", content_sha256=hashlib.sha256(b"new").hexdigest())
                  if kind == "content" else dict(resource_version="new-version"))
        sources[-1] = replace(sources[-1], source=replace(sources[-1].source, **change))
        value = replace(value, sources=sources)
    elif kind == "grant_revision":
        value = replace(value, grants=(replace(value.grants[0], revision=2),),
            sources=tuple(replace(s, source=replace(s.source, grant_revision=2)) for s in value.sources))
    elif kind == "revoked":
        value = replace(value, grants=(replace(value.grants[0], revoked_at=at()),))
    elif kind == "source_stale":
        value = replace(value, sources=tuple(replace(s, fresh_until=at(1)) for s in value.sources))
        clock.wall = clock.mono = 1
    authority.publish(value)


@pytest.mark.parametrize("kind", ["identity", "source", "grant", "destination", "permission",
    "binding", "content", "source_revision", "grant_revision", "revoked", "invalidate",
    "stale", "source_stale"])
@pytest.mark.parametrize("phase", ["before_worker", "after_provider"])
def test_worker_current_changes_deny_without_commit(composed, kind, phase):
    authority, service, clock = composed
    admitted = service.submit(event())
    assert admitted.status == "queued"
    if phase == "before_worker":
        mutate(authority, clock, kind)
    else:
        async def effect(_):
            mutate(authority, clock, kind)
        service.transport.effect = effect
    worker = BriefWorker(service)
    try:
        assert worker.run_once().result(3) == "needs_input"
        assert service.store.outbox(SCOPE, admitted.job_id) is None
        assert len(service.transport.calls) == (phase == "after_provider")
    finally:
        worker.stop()


@pytest.mark.parametrize("kind", ["identity", "source", "grant", "destination", "permission",
                                  "invalidate", "stale", "source_stale", "revoked"])
def test_admission_denials_write_no_job(composed, kind):
    authority, service, clock = composed
    mutate(authority, clock, kind)
    assert service.submit(event()).status == "denied"
    assert service.store.get_by_event(SCOPE, "event1") is None
    assert service.transport.calls == []


@pytest.mark.parametrize("platform,actor,namespace", [("lark", "alice", "synthetic"),
    ("feishu", "bob", "synthetic"), ("feishu", "alice", "foreign"),
    ("feishu", "local-admin", "synthetic")])
def test_principal_and_namespace_never_alias(composed, platform, actor, namespace):
    _, service, _ = composed
    body = request()
    body["namespace"] = namespace
    assert service.submit(event(body, platform=platform, sender_id=actor)).status in (
        "denied", "invalid_request")
    assert not service.transport.calls


@pytest.mark.parametrize("principal", [Principal("feishu", "local-admin"), Principal("lark", "alice")])
def test_matching_grant_sources_and_destination_still_need_explicit_permission(composed, principal):
    authority, service, _ = composed
    scope = RequesterContext(SCOPE.namespace, principal)
    value = snapshot(2)
    sources = tuple(replace(s, scope=scope,
        source=replace(s.source, allowed_audience=(principal,))) for s in value.sources)
    value = replace(value, sources=sources,
        grants=(replace(value.grants[0], principal=principal, allowed_audience=(principal,)),),
        destinations=((scope, PrivateDestination(principal, "private-dm")),))
    authority.publish(value)  # Only feishu:alice has an explicit permission.
    body = request() | {"sources": [s.source.to_dict() for s in sources]}
    incoming = event(body, platform=principal.platform, sender_id=principal.actor_id)
    assert service.submit(incoming).status == "denied"
    assert service.store.get_by_event(scope, incoming.event_id) is None
    authority.publish(replace(value, revision=3, permissions=((principal, "propose"),)))
    assert service.submit(incoming).status == "queued"


@pytest.mark.parametrize("kind", ["source", "grant"])
def test_fresh_mirror_cannot_hide_future_evidence_or_grant(composed, kind):
    authority, service, _ = composed
    value = snapshot(2)
    if kind == "source":
        value = replace(value, sources=tuple(replace(s, source=replace(s.source, observed_at=at(1)))
                                            for s in value.sources))
    else:
        value = replace(value, grants=(replace(value.grants[0], valid_from=at(1)),))
    authority.publish(value)
    body = request() | {"sources": [s.source.to_dict() for s in value.sources]}
    assert service.submit(event(body)).status == "denied"
    assert not service.transport.calls


def test_concrete_factory_binding_rejects_mixed_or_replaced_callbacks(composed):
    authority, service, _ = composed
    other = Clock().authority()
    other.publish(snapshot())
    with pytest.raises(SchemaError):
        BriefService(store=service.store, transport=service.transport, config=service.config,
            settings=other.settings, grants=authority.grants, sources=authority.sources,
            destination=authority.destination, clock=authority.clock, authority=authority)
    service.sources = lambda scope, source: snapshot().sources[0]
    with pytest.raises(SchemaError):
        check(service)
    with pytest.raises(TypeError):
        authority.create_service(store=service.store, transport=service.transport,
                                 config=service.config, settings=other.settings)


def test_mutation_between_source_checks_denies_even_identical_views(composed, monkeypatch):
    authority, service, _ = composed
    entered, release = Event(), Event()
    original = TrustedBriefAuthority.sources

    def paused(self, scope, source_id):
        result = original(self, scope, source_id)
        if source_id == "s0":
            entered.set()
            assert release.wait(3)
        return result

    # Instrument the real lookup without installing unrelated policy callbacks.
    monkeypatch.setattr(TrustedBriefAuthority, "sources", paused)
    service.sources = authority.sources
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(check, service)
        try:
            assert entered.wait(2)
            authority.publish(snapshot(2))  # Both policies separately permit all sources.
        finally:
            release.set()
        with pytest.raises(SchemaError):
            future.result(3)
    assert service.transport.calls == []


def test_mutation_during_final_stop_callback_is_fenced_without_holding_lock(composed):
    authority, service, _ = composed
    entered, release = Event(), Event()
    calls = 0

    def stopped():
        nonlocal calls
        calls += 1
        if calls == 2:
            entered.set()
            assert release.wait(3)
        return False

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(check, service, stopped=stopped)
        try:
            assert entered.wait(2)
            authority.invalidate()
        finally:
            release.set()
        with pytest.raises(SchemaError):
            future.result(3)


@pytest.mark.parametrize("bound", ["source", "grant", "mirror"])
def test_final_stop_cannot_consume_remaining_freshness(composed, bound):
    authority, service, clock = composed
    value = snapshot(2)
    if bound == "source":
        value = replace(value, sources=tuple(replace(s, fresh_until=at(1)) for s in value.sources))
    elif bound == "grant":
        value = replace(value, grants=(replace(value.grants[0], expires_at=at(1)),))
    else:
        value = replace(value, fresh_until=at(1))
    authority.publish(value)
    calls = 0

    def stopped():
        nonlocal calls
        calls += 1
        if calls == 2:
            clock.mono = 1  # Wall is stalled, including for source/grant bounds.
        return False

    with pytest.raises(SchemaError):
        check(service, stopped=stopped)


def test_real_service_nested_check_rejected_without_corrupting_outer(composed):
    _, service, _ = composed

    def stopped():
        with pytest.raises(SchemaError):
            check(service)
        return False

    assert check(service, stopped=stopped) is None


@pytest.mark.parametrize("mutation", ["refresh", "invalidate"])
def test_retention_sql_wait_precedes_the_entire_mirror(retained, monkeypatch, mutation):
    authority, service, _, coordinator = retained
    ledger = coordinator.ledger
    entered = Event()
    original = ledger._connection

    @contextmanager
    def traced(**kwargs):
        with original(**kwargs) as db:
            # Give this controlled real lock wait time for scheduler jitter;
            # production's bounded SQLite busy timeout is unchanged.
            db.execute("PRAGMA busy_timeout=3000")
            db.set_trace_callback(lambda sql: entered.set() if sql == "BEGIN IMMEDIATE" else None)
            yield db

    monkeypatch.setattr(ledger, "_connection", traced)
    with ThreadPoolExecutor(max_workers=1) as pool:
        holder = sqlite3.connect(ledger.path, isolation_level=None)
        try:
            holder.execute("BEGIN IMMEDIATE")
            future = pool.submit(check, service)
            assert entered.wait(2)
            if mutation == "refresh":
                # Pinning before SQL would make even a valid refresh fail the
                # epoch fence. The mirror must start after this SQL wait.
                authority.publish(snapshot(2))
            else:
                authority.invalidate()
        finally:
            holder.rollback()
            holder.close()
        if mutation == "refresh":
            assert future.result(3) is None
        else:
            with pytest.raises(SchemaError):
                future.result(3)


@pytest.mark.parametrize("phase", ["connection", "count", "response"])
def test_real_count_create_gate_rechecks_after_await(composed, monkeypatch, phase):
    from test_brief_openai_transport import Wire, adapter, parameters as provider_parameters
    authority, service, _ = composed
    entered, release = Event(), Event()

    async def effect(req):
        if ((phase == "count" and req.url.path.endswith("input_tokens"))
                or (phase == "response" and req.url.path.endswith("/responses"))):
            entered.set()
            assert await asyncio.to_thread(release.wait, 3)

    wire = Wire(effect=effect)
    service.config = GenerationConfig(provider_parameters(), 1000000)
    service.transport = adapter(wire)
    if phase == "connection":
        original = httpx.AsyncClient.__aenter__

        async def waiting(client):
            result = await original(client)
            entered.set()
            assert await asyncio.to_thread(release.wait, 3)
            return result
        monkeypatch.setattr(httpx.AsyncClient, "__aenter__", waiting)
    job = service.submit(event()).job_id
    worker = BriefWorker(service)
    future = worker.run_once()
    try:
        assert entered.wait(2)
        authority.invalidate()
        release.set()
        assert future.result(3) == "needs_input"
        assert len(wire.calls) == {"connection": 0, "count": 1, "response": 2}[phase]
        assert service.store.outbox(SCOPE, job) is None
    finally:
        release.set()
        worker.stop()


def test_completed_job_retention_and_budgets_never_renew_on_refresh(retained):
    authority, service, clock, coordinator = retained
    ledger = coordinator.ledger
    job = service.submit(event()).job_id
    coordinator.register(SCOPE, job, policy=RetentionPolicy(at(30)))
    worker = BriefWorker(service)
    try:
        assert worker.run_once().result(3) == "draft_ready"
        before = service.store.get(SCOPE, job)
        with sqlite3.connect(ledger.path) as db:
            sources = db.execute("SELECT * FROM retention_sources").fetchall()
        clock.wall = clock.mono = 20
        authority.publish(snapshot(2, observed_at=at(20), fresh_until=at(80)))
        check(service)
        assert service.store.get(SCOPE, job) == before
        with sqlite3.connect(ledger.path) as db:
            assert db.execute("SELECT * FROM retention_sources").fetchall() == sources
        clock.wall = clock.mono = 30
        with pytest.raises(SchemaError):
            service.authorize_current(ManualRequest.from_dict(request()), SCOPE,
                before.destination, stopped=lambda: False, job_id=job)
        assert coordinator.status(SCOPE, job).decision == "expired"
    finally:
        worker.stop()
