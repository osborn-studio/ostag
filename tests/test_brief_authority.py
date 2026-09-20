"""Synthetic local composition; receiving these objects does not prove issuance."""
from dataclasses import FrozenInstanceError, replace
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from threading import Barrier, Event, current_thread
import builtins
import socket

import pytest

from ostag.briefs.authority import AuthoritySnapshot, TrustedBriefAuthority
from ostag.briefs.store import PrivateDestination, RequesterContext
from ostag.core.identity import Principal
from ostag.core.schemas import SchemaError
from test_brief_generation import at
from test_brief_service import Authority, SCOPE


def snapshot(revision=1, **changes):
    fixture = Authority()
    return AuthoritySnapshot(**(dict(revision=revision, observed_at=at(), fresh_until=at(60),
        permissions=((SCOPE.principal, "propose"),),
        destinations=((SCOPE, fixture.destination),), grants=(fixture.grant,),
        sources=tuple(fixture.sources.values())) | changes))


class Clock:
    wall = 0
    mono = 0.0

    def authority(self, **kwargs):
        return TrustedBriefAuthority(clock=lambda: at(self.wall),
                                     monotonic=lambda: self.mono, **kwargs)


def test_defensive_snapshot_and_settings():
    original = snapshot()
    permissions = [[SCOPE.principal, "propose"]]
    sources = list(original.sources)
    value = replace(original, permissions=permissions, sources=sources)
    authority = Clock().authority()
    authority.publish(value)
    permissions[0][1] = "read"
    sources.clear()
    assert authority.settings().actor_permissions == ("feishu:alice:propose",)
    assert authority.settings().default_permission_level == "read"
    assert authority.sources(SCOPE, "s0") == original.sources[0]
    with pytest.raises(FrozenInstanceError):
        value.revision = 2
    wire = authority.sources(SCOPE, "s0").source.to_dict()
    wire["allowed_audience"].clear()
    assert authority.sources(SCOPE, "s0").source.allowed_audience == (SCOPE.principal,)
    # Existing contract types predate slots. Their mutable __dict__ must not
    # expose the authority's stored objects, even through nested callback data.
    answer = authority.sources(SCOPE, "s0")
    answer.scope.principal.__dict__["actor_id"] = "bob"
    answer.source.__dict__["excerpt"] = "changed"
    authority.grants("grant").__dict__["revision"] = 99
    authority.destination(SCOPE).__dict__["address"] = "other"
    assert authority.sources(SCOPE, "s0") == original.sources[0]
    assert authority.grants("grant").revision == 1
    assert authority.destination(SCOPE).address == "alice-dm"


def test_invalid_refresh_and_replay_remove_access_keep_revision_highwater():
    authority = Clock().authority()
    authority.publish(snapshot(2))
    with pytest.raises(SchemaError):
        authority.publish({"revision": 3})
    with pytest.raises(SchemaError):
        authority.sources(SCOPE, "s0")
    with pytest.raises(SchemaError):
        authority.publish(snapshot(2))
    authority.publish(snapshot(3))
    authority.invalidate()
    with pytest.raises(SchemaError):
        authority.publish(snapshot(3))
    authority.publish(snapshot(4, permissions=(), destinations=(), grants=(), sources=()))
    assert authority.destination(SCOPE) is None


@pytest.mark.parametrize("wall,mono", [(60, 0), (0, 60)])
def test_expiry_at_equality_even_with_stalled_wall(wall, mono):
    clock = Clock()
    authority = clock.authority()
    authority.publish(snapshot())
    clock.wall, clock.mono = wall, mono
    with pytest.raises(SchemaError):
        authority.sources(SCOPE, "s0")


@pytest.mark.parametrize("field,value", [("wall", -1), ("mono", -1),
                                        ("mono", float("nan")), ("mono", True)])
def test_clock_failure_latches_for_instance(field, value):
    clock = Clock()
    authority = clock.authority()
    authority.publish(snapshot())
    setattr(clock, field, value)
    with pytest.raises(SchemaError):
        authority.sources(SCOPE, "s0")
    clock.wall, clock.mono = 1, 1
    with pytest.raises(SchemaError):
        authority.publish(snapshot(2))
    fresh = clock.authority()
    fresh.publish(snapshot(2))


@pytest.mark.parametrize("changes", [
    {"revision": True}, {"revision": 0}, {"revision": 1.0}, {"revision": 2**63},
    {"observed_at": "2026-09-13T00:00:00"}, {"observed_at": at(60)},
    {"permissions": ((SCOPE.principal, "owner"),)},
    {"permissions": ((SCOPE.principal, " propose "),)},
    {"permissions": ("alice:propose",)},
    {"permissions": ((Principal("feishu", "alice:other"), "propose"),)},
    {"permissions": (({"platform": "feishu", "actor_id": "alice"}, "propose"),)},
    {"grants": ({"grant_id": "grant"},)}, {"sources": (object(),)},
    {"destinations": ((SCOPE, PrivateDestination(Principal("feishu", "bob"), "dm")),)},
])
def test_exact_validation_has_fixed_private_errors(changes):
    with pytest.raises(SchemaError) as exc:
        snapshot(**changes)
    assert exc.value.code == "brief_authorization_denied"
    assert str(exc.value) == str(SchemaError("brief_authorization_denied",
        "Local authority unavailable.", "Re-establish trusted local authority before retrying."))


@pytest.mark.parametrize("collection", ["permissions", "destinations", "grants", "sources"])
def test_duplicate_keys_and_capacity(collection):
    value = snapshot()
    row = getattr(value, collection)[0]
    for rows in ((row, row), (row,) * 501):
        with pytest.raises(SchemaError):
            replace(value, **{collection: rows})


def test_full_capacity_and_empty_snapshot_are_supported():
    authority = Clock().authority()
    permissions = tuple((Principal("feishu", f"user{i}"), "read") for i in range(500))
    authority.publish(snapshot(permissions=permissions))
    assert len(authority.settings().actor_permissions) == 500
    authority.publish(AuthoritySnapshot(2, at(), at(60)))
    assert authority.settings().actor_permissions == ()
    assert authority.grants("grant") is None
    assert authority.sources(SCOPE, "s0") is None
    assert authority.destination(SCOPE) is None


@pytest.mark.parametrize("kind", ["namespace", "audience", "grant_principal", "grant_revision",
                                   "resource", "hash", "address_owner"])
def test_cross_bindings_and_hash_mismatch_are_invalid(kind):
    value = snapshot()
    first = value.sources[0]
    if kind == "namespace":
        changes = dict(sources=(replace(first, scope=RequesterContext("other", SCOPE.principal)),))
    elif kind == "audience":
        changes = dict(sources=(replace(first, source=replace(first.source,
            allowed_audience=(Principal("lark", "alice"),))),))
    elif kind == "grant_principal":
        bob = Principal("feishu", "bob")
        changes = dict(grants=(replace(value.grants[0], principal=bob, allowed_audience=(bob,)),))
    elif kind == "grant_revision":
        changes = dict(grants=(replace(value.grants[0], revision=2),))
    elif kind == "resource":
        changes = dict(grants=(replace(value.grants[0], resource_ids=("unrelated",)),))
    elif kind == "hash":
        changes = dict(sources=(replace(first, source=replace(first.source, excerpt="changed")),))
    else:
        bob = Principal("feishu", "bob")
        changes = dict(destinations=value.destinations + (
            (RequesterContext("synthetic", bob), PrivateDestination(bob, "alice-dm")),))
    with pytest.raises(SchemaError):
        replace(value, **changes)


def test_publication_revalidates_exact_types_before_acceptance():
    authority = Clock().authority()
    authority.publish(snapshot())
    forged = snapshot(2)
    # Frozen dataclasses are not a sandbox: even deliberately forged exact
    # instances must pass validation again before becoming authoritative.
    object.__setattr__(forged, "revision", True)
    with pytest.raises(SchemaError):
        authority.publish(forged)
    with pytest.raises(SchemaError):
        authority.destination(SCOPE)


@pytest.mark.parametrize("ttl", [True, 0, 301, 60.0, "60"])
def test_ttl_is_bounded_strict_int(ttl):
    with pytest.raises(SchemaError):
        Clock().authority(max_ttl_seconds=ttl)


@pytest.mark.parametrize("observed,expiry", [(1, 60), (-60, 0), (-1, 60), (0, 61)])
def test_publication_rejects_future_stale_and_excess_lifetime(observed, expiry):
    authority = Clock().authority()
    authority.publish(snapshot())
    with pytest.raises(SchemaError):
        authority.publish(snapshot(2, observed_at=at(observed), fresh_until=at(expiry)))
    with pytest.raises(SchemaError):
        authority.destination(SCOPE)


def test_maximum_ttl_and_no_monotonic_reset_on_refresh():
    clock = Clock()
    authority = clock.authority(max_ttl_seconds=300)
    authority.publish(snapshot(fresh_until=at(300)))
    clock.mono = 30
    authority.publish(snapshot(2, fresh_until=at(40)))
    clock.mono = 40
    with pytest.raises(SchemaError):
        authority.destination(SCOPE)


def test_small_monotonic_increments_accumulate():
    clock = Clock()
    authority = clock.authority()
    authority.publish(snapshot(fresh_until=at(0.000003)))
    for i in range(1, 20):
        clock.mono = i / 10000000
        assert authority.destination(SCOPE) is not None
    clock.mono = 0.000003
    with pytest.raises(SchemaError):
        authority.destination(SCOPE)


@pytest.mark.parametrize("which", ["wall", "mono"])
def test_clock_exception_sanitized_and_latched(which):
    broken = False

    def sample(value):
        if broken:
            raise RuntimeError("private diagnostic sentinel")
        return value

    authority = TrustedBriefAuthority(clock=lambda: sample(at()) if which == "wall" else at(),
        monotonic=lambda: sample(0.0) if which == "mono" else 0.0)
    authority.publish(snapshot())
    broken = True
    with pytest.raises(SchemaError) as exc:
        authority.clock()
    assert "sentinel" not in str(exc.value)
    broken = False
    with pytest.raises(SchemaError):
        authority.publish(snapshot(2))


def test_parallel_revisions_cannot_replay_accepted_highwater():
    authority = Clock().authority()
    barrier = Barrier(8)

    def publish(revision):
        value = snapshot(revision)
        barrier.wait()
        try:
            authority.publish(value)
            return revision
        except SchemaError:
            return 0

    with ThreadPoolExecutor(max_workers=8) as pool:
        accepted = list(pool.map(publish, range(1, 9)))
    assert max(accepted) > 0
    with pytest.raises(SchemaError):
        authority.publish(snapshot(max(accepted)))
    authority.publish(snapshot(9))
    assert authority.destination(SCOPE) is not None


@pytest.mark.parametrize("mutation", ["publish", "invalidate", "invalid_publish"])
def test_slow_publication_cannot_overtake_newer_mutation(mutation):
    entered, release = Event(), Event()

    def wall():
        if current_thread().name.startswith("slow-publisher"):
            entered.set()
            assert release.wait(3)
        return at()

    authority = TrustedBriefAuthority(clock=wall, monotonic=lambda: 0.0)
    authority.publish(snapshot())
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="slow-publisher") as pool:
        future = pool.submit(authority.publish, snapshot(2))
        try:
            assert entered.wait(2)
            if mutation == "publish":
                authority.publish(snapshot(3))
            elif mutation == "invalidate":
                authority.invalidate()
            else:
                with pytest.raises(SchemaError):
                    authority.publish(object())
        finally:
            release.set()
        with pytest.raises(SchemaError):
            future.result(3)
    if mutation == "publish":
        assert authority.destination(SCOPE) is not None
    else:
        with pytest.raises(SchemaError):
            authority.destination(SCOPE)


def test_reentrant_check_preserves_outer_and_copied_context_expires():
    authority = Clock().authority()
    authority.publish(snapshot())
    with authority.coherent_check():
        context = copy_context()
        with pytest.raises(SchemaError):
            with authority.coherent_check():
                pytest.fail("nested pass entered")
        assert authority.grants("grant") is not None
    with pytest.raises(SchemaError):
        context.run(authority.settings)
    assert authority.settings().default_permission_level == "read"


def test_thread_contexts_are_independent_and_cannot_borrow_a_pin():
    authority = Clock().authority()
    authority.publish(snapshot())
    with ThreadPoolExecutor(max_workers=1) as pool:
        with authority.coherent_check():
            context = copy_context()
            with pytest.raises(SchemaError):
                pool.submit(context.run, authority.settings).result(2)

            def independent():
                with authority.coherent_check():
                    return authority.destination(SCOPE)
            assert pool.submit(independent).result(2) is not None


def test_callback_reads_do_not_open_files_network_or_database(monkeypatch):
    authority = Clock().authority()
    authority.publish(snapshot())

    def forbidden(*args, **kwargs):
        raise AssertionError("unexpected I/O")

    monkeypatch.setattr(builtins, "open", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    import sqlite3
    monkeypatch.setattr(sqlite3, "connect", forbidden)
    with authority.coherent_check():
        assert authority.sources(SCOPE, "s0") is not None
        assert authority.grants("grant") is not None
        assert authority.destination(SCOPE) is not None
        assert authority.settings().default_permission_level == "read"


def test_invalidation_after_last_lookup_denies_final_boundary():
    authority = Clock().authority()
    authority.publish(snapshot())
    with pytest.raises(SchemaError):
        with authority.coherent_check():
            assert authority.sources(SCOPE, "s0") is not None
            authority.invalidate()


def test_successful_historical_pass_cannot_be_reused():
    clock = Clock()
    authority = clock.authority()
    authority.publish(snapshot())
    with authority.coherent_check():
        assert authority.settings().actor_permissions
    clock.wall = clock.mono = 60
    with pytest.raises(SchemaError):
        with authority.coherent_check():
            pytest.fail("expired mirror entered")


def test_lookup_inputs_are_exact_and_bounded():
    authority = Clock().authority()
    authority.publish(snapshot())
    for key in (True, {}, object(), "x" * 257):
        with pytest.raises(SchemaError):
            authority.grants(key)
        with pytest.raises(SchemaError):
            authority.sources(SCOPE, key)
    malformed = replace(SCOPE)
    object.__setattr__(malformed, "principal", {"platform": "feishu", "actor_id": "alice"})
    with pytest.raises(SchemaError):
        authority.destination(malformed)


def test_clock_sample_completion_inversion_is_not_rollback():
    entered, release = Event(), Event()
    wall_value = 0

    def wall():
        value = wall_value
        if current_thread().name.startswith("slow-clock"):
            entered.set()
            assert release.wait(3)
        return at(value)

    authority = TrustedBriefAuthority(clock=wall, monotonic=lambda: 0.0)
    authority.publish(snapshot())
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="slow-clock") as pool:
        future = pool.submit(authority.clock)
        try:
            assert entered.wait(2)
            wall_value = 1
            assert authority.clock() == at(1).replace("+00:00", "Z")
        finally:
            release.set()
        assert future.result(3) == at(1).replace("+00:00", "Z")
    assert authority.destination(SCOPE) is not None
