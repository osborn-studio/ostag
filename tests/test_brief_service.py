"""Manual A04 local contracts; synthetic evidence is not a live ACL/provider pilot."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
import hashlib
import json
from threading import Barrier, Event

import pytest

from ostag.briefs.generation import GenerationConfig
from ostag.briefs.grants import PilotGrant
from ostag.briefs.service import BriefService, ManualRequest, SourceSnapshot
from ostag.briefs.store import BriefStore, PrivateDestination, RequesterContext
from ostag.briefs.validation import Coverage
from ostag.config import Settings
from ostag.core.event import NormalizedEvent
from ostag.core.identity import Principal
from ostag.core.runner import OSTagRunner
from ostag.core.schemas import SchemaError
from test_brief_generation import FakeTransport, at, parameters
from test_brief_validation import ACTOR, evidence

SCOPE = RequesterContext("synthetic", ACTOR)


def request():
    sources = evidence(count=2)
    refs = [replace(s, content_sha256=hashlib.sha256(s.excerpt.encode()).hexdigest())
            for s in sources.sources]
    return dict(version="manual-brief-v1", namespace="synthetic", purpose="brief",
                window=sources.window.to_dict(), grant_id="grant", grant_revision=1,
                coverage=Coverage(False, ("manual materials only",)).to_dict(),
                goals="Prepare a private draft", sources=[s.to_dict() for s in refs])


def event(body=None, **changes):
    return NormalizedEvent(**(dict(platform="feishu", event_id="event1", chat_id="group",
        message_id="message1", sender_id="alice", text="/brief " + json.dumps(
            request() if body is None else body), mention_bot=True, chat_type="group",
        destination_id="group", raw={"authorized": True, "destination": "attacker"}) | changes))


class Authority:
    def __init__(self):
        self.now = 0
        self.grant = PilotGrant("grant", "synthetic", ACTOR, ("r0", "r1"), "brief", (ACTOR,),
            True, "2026-09-12T00:00:00Z", "2026-09-14T00:00:00Z", 1)
        self.sources = {s.source_id: SourceSnapshot(SCOPE, s, "2026-09-14T00:00:00Z")
                        for s in ManualRequest.from_dict(request()).sources}
        self.destination = PrivateDestination(ACTOR, "alice-dm")
        self.config = GenerationConfig(parameters(), 1000000)
        self.settings = Settings(default_permission_level="propose")
        self.checks = []

    def lookup(self, scope, source_id):
        self.checks.append(source_id)
        return self.sources.get(source_id)

    def service(self, store, **changes):
        return BriefService(**(dict(store=store, transport=FakeTransport(), config=self.config,
            settings=lambda: self.settings, grants=lambda _: self.grant, sources=self.lookup,
            destination=lambda _: self.destination, clock=lambda: at(self.now)) | changes))


@pytest.fixture
def setup(tmp_path):
    authority = Authority()
    service = authority.service(BriefStore(tmp_path / "brief.db"))
    yield service, authority
    service.close()


class ForbiddenBackend:
    async def run(self, *args, **kwargs):
        raise AssertionError("manual input reached generic backend")


@pytest.mark.asyncio
async def test_runner_closed_and_invalid_never_open_legacy_ledger(tmp_path, setup):
    service, _ = setup
    config = Settings(run_ledger_path=tmp_path / "runs.jsonl", mfs_enabled=False)
    runner = OSTagRunner(config, backend=ForbiddenBackend())
    assert await runner.handle_event(event()) == "brief: unavailable"
    runner.brief_service = service
    assert await runner.handle_event(event({"authorized": True})) == "brief: invalid_request"
    assert not config.run_ledger_path.exists()


def test_strict_immutable_request():
    parsed = ManualRequest.from_dict(request())
    with pytest.raises(FrozenInstanceError):
        parsed.goals = "changed"
    for change in ({"grant_revision": True}, {"authorized": True}, {"version": "meeting-v1"}):
        with pytest.raises(SchemaError):
            ManualRequest.from_dict(request() | change)


def test_event_replay_precedes_clock_and_freezes_original_snapshot(setup):
    service, authority = setup
    first = service.submit(event())
    assert first.status == "queued"
    authority.now = 10
    reopened = authority.service(BriefStore(service.store.path))
    reopened.clock = lambda: (_ for _ in ()).throw(AssertionError("replay sampled intake clock"))
    try:
        again = reopened.submit(event())
        assert again == first
        assert reopened.submit(event(request() | {"goals": "changed"})).status == "conflict"
        job = reopened.store.get(SCOPE, first.job_id)
        assert job.created_at == at().replace("+00:00", "Z")
        assert json.loads(job.payload_json)["intake_at"] == at()
        assert reopened.store.get(RequesterContext("synthetic", Principal("feishu", "bob")),
                                  first.job_id) is None
    finally:
        reopened.close()


def test_concurrent_duplicate_events_have_one_snapshot(setup):
    service, _ = setup
    barrier = Barrier(5)
    def submit():
        barrier.wait()
        return service.submit(event())
    with ThreadPoolExecutor(max_workers=5) as pool:
        results = list(pool.map(lambda _: submit(), range(5)))
    assert {r.status for r in results} == {"queued"}
    assert len({r.job_id for r in results}) == 1


@pytest.mark.parametrize("field,value", [
    ("namespace", "foreign"), ("purpose", "other"), ("grant_id", "other"),
    ("grant_revision", 2), ("resource_id", "other"), ("collection_actor_id", "other"),
    ("collection_platform", "feishu"),
    ("allowed_audience", [{"platform": "feishu", "actor_id": "bob"}]),
])
def test_each_source_is_bound_to_current_authority(setup, field, value):
    service, _ = setup
    body = request()
    body["sources"][1][field] = value
    assert service.submit(event(body)).status in ("denied", "invalid_request")
    assert service.store.get_by_event(SCOPE, "event1") is None


@pytest.mark.parametrize("change", ["acl", "freshness", "grant", "destination", "permission"])
def test_current_authority_denials_persist_no_content(setup, change):
    service, authority = setup
    if change == "acl":
        authority.sources.pop("s1")
    elif change == "freshness":
        authority.sources["s1"] = replace(authority.sources["s1"], fresh_until=at())
    elif change == "grant":
        authority.grant = replace(authority.grant, revoked_at=at())
    elif change == "destination":
        authority.destination = PrivateDestination(Principal("feishu", "bob"), "bob-dm")
    else:
        authority.settings = Settings(default_permission_level="read")
    assert service.submit(event()).status == "denied"
    assert service.store.get_by_event(SCOPE, "event1") is None


@pytest.mark.parametrize("delayed_callback", ["source", "settings", "grant"])
@pytest.mark.parametrize("bound", ["source_expiry", "grant_expiry", "grant_revocation",
                                  "source_observed", "grant_valid_from"])
def test_authorization_rechecks_all_time_bounds_after_last_lookup(setup, delayed_callback, bound):
    service, authority = setup
    if bound == "source_expiry":
        authority.sources["s0"] = replace(authority.sources["s0"], fresh_until=at(1))
    elif bound == "grant_expiry":
        authority.grant = replace(authority.grant, expires_at=at(1))
    elif bound == "grant_revocation":
        authority.grant = replace(authority.grant, revoked_at=at(1))
    elif bound == "grant_valid_from":
        authority.grant = replace(authority.grant, valid_from=at(-1))
    parsed = ManualRequest.from_dict(request())
    # Control: exactly the same trusted source/grant answers are valid at t=0.
    service.authorize_current(parsed, SCOPE, authority.destination, stopped=lambda: False)
    calls = 0

    def delay():
        nonlocal calls
        calls += 1
        if calls == 2:
            authority.now = -2 if bound in ("source_observed", "grant_valid_from") else 2

    def sources(scope, source_id):
        delay()
        return authority.lookup(scope, source_id)

    def settings():
        delay()
        return authority.settings

    def grants(_):
        delay()
        return authority.grant

    if delayed_callback == "source":
        service.sources = sources
    elif delayed_callback == "settings":
        service.settings = settings
    else:
        service.grants = grants
    with pytest.raises(SchemaError):
        service.authorize_current(parsed, SCOPE, authority.destination, stopped=lambda: False)
    assert service.transport.calls == []
    assert service.store.get_by_event(SCOPE, "event1") is None


def test_empty_input_and_queue_capacity(setup):
    service, _ = setup
    assert service.submit(event(request() | {"sources": []})).status == "needs_input"
    jobs = [service.submit(event(event_id=f"event{i}")) for i in range(20)]
    assert all(j.status == "queued" for j in jobs)
    assert service.submit(event(event_id="overflow")).status == "queue_full"
    assert service.submit(event(event_id="event0")) == jobs[0]


@pytest.mark.asyncio
async def test_bounded_async_intake_offloads_blocking_callbacks(setup):
    service, authority = setup
    entered, release = Event(), Event()
    def blocking(scope, source_id):
        entered.set()
        assert release.wait(2)
        return authority.sources[source_id]
    service.sources = blocking
    other = authority.service(service.store, sources=blocking, max_intake=1)
    pending = asyncio.create_task(other.handle_event(event()))
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        assert await other.handle_event(event(event_id="overflow")) == "brief: intake_full"
        ticks = 0
        for _ in range(5):
            await asyncio.sleep(0)
            ticks += 1
        assert ticks == 5
    finally:
        release.set()
        await pending
        other.close()


@pytest.mark.asyncio
async def test_cancelled_async_intake_retains_slot_until_executor_work_finishes(setup):
    service, authority = setup
    from threading import Lock
    lock = Lock()
    started, release = Event(), Event()
    count = 0
    def block(scope, source_id):
        nonlocal count
        with lock:
            count += 1
            if count == 4:
                started.set()
        assert release.wait(2)
        return authority.sources[source_id]
    other = authority.service(service.store, sources=block, max_intake=5)
    tasks = [asyncio.create_task(other.handle_event(event(event_id=f"parallel{i}")))
             for i in range(5)]
    try:
        assert await asyncio.to_thread(started.wait, 1)
        tasks[4].cancel()  # This request is queued behind the four blocked callbacks.
        with pytest.raises(asyncio.CancelledError):
            await tasks[4]
        assert await asyncio.wait_for(other.handle_event(event(event_id="overflow")), 0.1) == "brief: intake_full"
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        other.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("caller_cancel", [False, True])
async def test_close_sanitizes_queued_intake_but_preserves_caller_cancellation(setup, caller_cancel):
    service, authority = setup
    entered, release = Event(), Event()
    count = 0
    from threading import Lock
    lock = Lock()
    def blocked(scope, source_id):
        nonlocal count
        with lock:
            count += 1
            if count == 4:
                entered.set()
        assert release.wait(2)
        return authority.lookup(scope, source_id)
    other = authority.service(service.store, sources=blocked, max_intake=5)
    tasks = [asyncio.create_task(other.handle_event(event(event_id=str(i)))) for i in range(4)]
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        queued = asyncio.create_task(other.handle_event(event(event_id="queued")))
        await asyncio.sleep(0)
        other.close()
        if caller_cancel:
            queued.cancel()
            with pytest.raises(asyncio.CancelledError):
                await queued
        else:
            assert await queued == "brief: unavailable"
    finally:
        release.set()
        await asyncio.gather(*tasks)
        other.close()


def test_admission_bounds_full_completion_provenance_before_paid_work(setup):
    from ostag.briefs.generation import Provenance, Usage, digest
    from ostag.briefs.store import _canonical
    service, authority = setup
    budget = replace(authority.config.parameters.budget, max_input_bytes=1048576,
                     max_job_micros=2_000_000)
    service.config = replace(authority.config, daily_limit_micros=3_000_000,
                             parameters=replace(authority.config.parameters, budget=budget))
    body = request()
    template = authority.sources["s0"].source

    def material(length):
        excerpt = "x" * length
        sha = hashlib.sha256(excerpt.encode()).hexdigest()
        return [replace(template, source_id=f"large-{i}", excerpt=excerpt, content_sha256=sha)
                for i in range(240)]

    # Pick a real 1 MiB boundary, using the current canonical admission schema.
    # The input and durable job fit, but completion repeats the pinned parameters
    # and adds actual model/usage metadata, making its envelope too large.
    low, high = 0, 4000
    while low < high:
        middle = (low + high + 1) // 2
        body["sources"] = [s.to_dict() for s in material(middle)]
        try:
            _canonical(dict(kind="manual-brief-v1", request=body, intake_at=at(),
                            config=service.config.to_dict()))
        except SchemaError:
            high = middle - 1
        else:
            low = middle
    refs = material(low)
    body["sources"] = [s.to_dict() for s in refs]
    parsed = ManualRequest.from_dict(body)
    sources = parsed.source_set(SCOPE)
    config = service.config
    metadata = Provenance(digest(sources.to_dict()), "0" * 64, config.parameters, config,
                          config.parameters.expected_model_id, "0" * 32,
                          Usage(100, 50, 200)).to_dict()
    with pytest.raises(SchemaError):
        _canonical(metadata | {"sources": sources.to_dict(), "intake_hash": "0" * 64})
    authority.sources = {s.source_id: SourceSnapshot(SCOPE, s, "2026-09-14T00:00:00Z") for s in refs}
    normalized = replace(event(), text="/brief " + _canonical(body))
    assert service.submit(normalized).status == "invalid_request"
    assert service.store.get_by_event(SCOPE, normalized.event_id) is None
    assert not service.transport.calls
