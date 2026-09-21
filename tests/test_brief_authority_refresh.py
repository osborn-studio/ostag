"""Trusted local refresh must revoke before loading and fence by start order."""
from asyncio import CancelledError
from contextlib import contextmanager
from queue import Queue
from threading import Event, Thread
import traceback

import pytest

from ostag.briefs.authority import AuthoritySnapshot, TrustedBriefAuthority
from ostag.core.schemas import SchemaError
from test_brief_authority import Clock, snapshot
from test_brief_generation import at
from test_brief_service import SCOPE


@contextmanager
def background(work):
    """Bound even a lock regression; propagate worker failures to the test."""
    results = Queue()

    def run():
        try:
            results.put((True, work()))
        except BaseException as error:
            results.put((False, error))

    def result():
        ok, value = results.get(timeout=5)
        if not ok:
            raise value
        return value

    thread = Thread(target=run, daemon=True)
    thread.start()
    try:
        yield result
    finally:
        thread.join(timeout=5)
        assert not thread.is_alive(), "authority work did not finish"


def assert_denied(authority):
    for read in (authority.settings, lambda: authority.destination(SCOPE),
                 lambda: authority.grants("grant"), lambda: authority.sources(SCOPE, "s0")):
        with pytest.raises(SchemaError, match="Local authority unavailable") as exc:
            read()
        assert exc.value.code == "brief_authorization_denied"


def fail_load():
    raise RuntimeError("private loader diagnostic sentinel")


def test_old_access_denied_while_loader_blocked_and_success_is_defensive():
    authority = Clock().authority()
    authority.publish(snapshot())
    entered, release = Event(), Event()
    loaded = snapshot(2)
    calls = []

    def loader():
        calls.append(True)
        entered.set()
        assert release.wait(5)
        return loaded

    with background(lambda: authority.refresh(loader)) as result:
        try:
            assert entered.wait(5)
            assert_denied(authority)
        finally:
            release.set()
        assert result() is None
    assert calls == [True]
    loaded.sources[0].source.__dict__["excerpt"] = "changed after refresh"
    assert authority.sources(SCOPE, "s0").source.excerpt != "changed after refresh"
    assert authority.settings().actor_permissions == ("feishu:alice:propose",)


def test_same_thread_construction_failure_happens_after_revocation():
    authority = Clock().authority()
    authority.publish(snapshot())
    entered = []

    def loader():
        assert_denied(authority)
        entered.append(True)
        return snapshot(2, permissions=((SCOPE.principal, "invalid"),))

    with background(lambda: authority.refresh(loader)) as result:
        with pytest.raises(SchemaError):
            result()
    assert entered == [True]
    assert_denied(authority)


@pytest.mark.parametrize("loader", [None, False, {}, object(), fail_load,
    lambda: None, lambda: {}, lambda: object(), lambda required: snapshot(2)])
def test_failed_invalid_or_missing_callable_denies(loader):
    authority = Clock().authority()
    authority.publish(snapshot())
    with pytest.raises(SchemaError):
        authority.refresh(loader)
    assert_denied(authority)


def test_omitted_loader_also_revokes():
    authority = Clock().authority()
    authority.publish(snapshot())
    with pytest.raises(SchemaError):
        authority.refresh()
    assert_denied(authority)


@pytest.mark.parametrize("error_type", [RuntimeError, CancelledError, KeyboardInterrupt])
def test_loader_failure_and_cancellation_are_sanitized(error_type):
    authority = Clock().authority()
    authority.publish(snapshot())

    def loader():
        raise error_type("private loader diagnostic sentinel")

    with pytest.raises(SchemaError) as exc:
        authority.refresh(loader)
    assert exc.value.code == "brief_authorization_denied"
    assert "sentinel" not in str(exc.value)
    assert "sentinel" not in "".join(traceback.format_exception(exc.value))
    assert exc.value.__cause__ is None
    assert_denied(authority)


@pytest.mark.parametrize("kind", ["subclass", "forged", "future", "expired", "ttl"])
def test_refresh_uses_publication_validation(kind):
    authority = Clock().authority()
    authority.publish(snapshot())
    if kind == "subclass":
        class DerivedSnapshot(AuthoritySnapshot):
            pass
        value = DerivedSnapshot(2, at(), at(60))
    elif kind == "forged":
        value = snapshot(2)
        object.__setattr__(value, "revision", True)
    else:
        observed, expiry = {"future": (1, 60), "expired": (-60, 0), "ttl": (0, 61)}[kind]
        value = snapshot(2, observed_at=at(observed), fresh_until=at(expiry))
    with pytest.raises(SchemaError):
        authority.refresh(lambda: value)
    assert_denied(authority)
    authority.refresh(lambda: snapshot(2))
    assert authority.destination(SCOPE) is not None


@pytest.mark.parametrize("newer", ["refresh", "publish", "invalidate", "invalid_publish",
                                    "failed_refresh", "invalid_refresh"])
@pytest.mark.parametrize("older", ["success", "failure", "cancellation", "invalid"])
def test_older_refresh_cannot_mutate_newer_state(newer, older):
    authority = Clock().authority()
    authority.publish(snapshot())
    entered, release = Event(), Event()

    def loader():
        entered.set()
        assert release.wait(5)
        if older == "failure":
            fail_load()
        if older == "cancellation":
            raise CancelledError("private cancellation")
        if older == "invalid":
            return object()
        # Even a higher revision cannot overtake a later-started mutation.
        return snapshot(90, permissions=((SCOPE.principal, "admin"),))

    with background(lambda: authority.refresh(loader)) as result:
        try:
            assert entered.wait(5)
            if newer in ("refresh", "publish"):
                value = snapshot(2, permissions=((SCOPE.principal, "write"),))
                if newer == "refresh":
                    authority.refresh(lambda: value)
                else:
                    authority.publish(value)
            elif newer == "invalidate":
                authority.invalidate()
            else:
                with pytest.raises(SchemaError):
                    if newer == "invalid_publish":
                        authority.publish(object())
                    elif newer == "invalid_refresh":
                        authority.refresh(None)
                    else:
                        authority.refresh(fail_load)
        finally:
            release.set()
        with pytest.raises(SchemaError):
            result()
    if newer in ("refresh", "publish"):
        assert authority.settings().actor_permissions == ("feishu:alice:write",)
    else:
        assert_denied(authority)
    # Superseded revision 90 was never accepted as high-water.
    authority.refresh(lambda: snapshot(3))
    assert authority.destination(SCOPE) is not None


def test_newer_refresh_start_supersedes_older_before_either_finishes():
    authority = Clock().authority()
    authority.publish(snapshot())
    older_entered, newer_entered, older_release, newer_release = (Event() for _ in range(4))

    def loader(entered, release, revision):
        entered.set()
        assert release.wait(5)
        return snapshot(revision)

    with background(lambda: authority.refresh(
            lambda: loader(older_entered, older_release, 90))) as older_result:
        try:
            assert older_entered.wait(5)
            with background(lambda: authority.refresh(
                    lambda: loader(newer_entered, newer_release, 2))) as newer_result:
                try:
                    assert newer_entered.wait(5)
                    older_release.set()
                    with pytest.raises(SchemaError):
                        older_result()
                    assert_denied(authority)
                finally:
                    newer_release.set()
                assert newer_result() is None
        finally:
            older_release.set()
    assert authority.destination(SCOPE) is not None


@pytest.mark.parametrize("succeed", [False, True])
def test_refresh_supersedes_an_already_validating_publish(succeed):
    block_next, entered, release = Event(), Event(), Event()

    def wall():
        if block_next.is_set():
            block_next.clear()
            entered.set()
            assert release.wait(5)
        return at()

    authority = TrustedBriefAuthority(clock=wall, monotonic=lambda: 0.0)
    authority.publish(snapshot())
    block_next.set()
    with background(lambda: authority.publish(snapshot(90))) as result:
        try:
            assert entered.wait(5)
            if succeed:
                authority.refresh(lambda: snapshot(2))
            else:
                with pytest.raises(SchemaError):
                    authority.refresh(fail_load)
        finally:
            release.set()
        with pytest.raises(SchemaError):
            result()
    if succeed:
        assert authority.destination(SCOPE) is not None
    else:
        assert_denied(authority)


@pytest.mark.parametrize("newer", ["refresh", "publish", "invalidate", "failed_refresh"])
def test_reentrant_loader_mutations_supersede_outer_refresh(newer):
    authority = Clock().authority()
    authority.publish(snapshot())

    def loader():
        assert_denied(authority)
        if newer == "refresh":
            authority.refresh(lambda: snapshot(2))
        elif newer == "publish":
            authority.publish(snapshot(2))
        elif newer == "invalidate":
            authority.invalidate()
        else:
            with pytest.raises(SchemaError):
                authority.refresh(fail_load)
        return snapshot(90)

    with background(lambda: authority.refresh(loader)) as result:
        with pytest.raises(SchemaError):
            result()
    if newer in ("refresh", "publish"):
        assert authority.destination(SCOPE) is not None
    else:
        assert_denied(authority)


def test_accepted_high_water_survives_failure_and_is_shared_with_publish():
    authority = Clock().authority()
    authority.publish(snapshot(5))
    with pytest.raises(SchemaError):
        authority.refresh(fail_load)
    for revision in (4, 5):
        with pytest.raises(SchemaError):
            authority.refresh(lambda: snapshot(revision))
        assert_denied(authority)
    authority.refresh(lambda: snapshot(6))
    with pytest.raises(SchemaError):
        authority.publish(snapshot(6))
    assert_denied(authority)
    authority.publish(snapshot(7))
    assert authority.destination(SCOPE) is not None


@pytest.mark.parametrize("finish_load", [False, True])
def test_refresh_start_fences_in_flight_coherent_pass(finish_load):
    authority = Clock().authority()
    authority.publish(snapshot())
    begin, entered, release = Event(), Event(), Event()

    def loader():
        entered.set()
        assert release.wait(5)
        return snapshot(2)

    def refresh():
        assert begin.wait(5)
        authority.refresh(loader)

    # Keep the old coherent check on this thread while refresh runs elsewhere.
    with background(refresh) as result:
        try:
            with pytest.raises(SchemaError):
                with authority.coherent_check():
                    assert authority.destination(SCOPE) is not None
                    begin.set()
                    assert entered.wait(5)
                    if finish_load:
                        release.set()
                        result()
                    assert_denied(authority)
                    # No more policy reads: the final boundary must reject,
                    # including when the loader has not yet returned.
        finally:
            begin.set()
            release.set()
        if not finish_load:
            result()
    assert authority.destination(SCOPE) is not None


@pytest.mark.parametrize("which", ["wall", "mono"])
@pytest.mark.parametrize("elapsed", [30, 60])
def test_blocked_load_consumes_freshness_without_renewal(which, elapsed):
    clock = Clock()
    authority = clock.authority()
    authority.publish(snapshot())
    entered, release = Event(), Event()
    value = snapshot(2)

    def loader():
        entered.set()
        assert release.wait(5)
        return value

    with background(lambda: authority.refresh(loader)) as result:
        try:
            assert entered.wait(5)
            setattr(clock, which, elapsed)
        finally:
            release.set()
        if elapsed == 60:
            with pytest.raises(SchemaError):
                result()
            assert_denied(authority)
        else:
            assert result() is None
            assert authority.destination(SCOPE) is not None
            setattr(clock, which, 60)
            assert_denied(authority)


@pytest.mark.parametrize("which", ["wall", "mono"])
def test_refresh_cannot_reset_clock_rollback_latch(which):
    clock = Clock()
    authority = clock.authority()
    authority.publish(snapshot())
    setattr(clock, which, 10)
    authority.clock()
    setattr(clock, which, 9)
    with pytest.raises(SchemaError):
        authority.refresh(lambda: snapshot(2))
    setattr(clock, which, 10)
    with pytest.raises(SchemaError):
        authority.refresh(lambda: snapshot(3))
    with pytest.raises(SchemaError):
        authority.publish(snapshot(4))
    assert_denied(authority)


def test_refresh_clock_callback_can_invalidate_without_mutex_deadlock():
    mutate = False

    def wall():
        if mutate:
            authority.invalidate()
        return at()

    authority = TrustedBriefAuthority(clock=wall, monotonic=lambda: 0.0)
    authority.publish(snapshot())
    mutate = True
    with background(lambda: authority.refresh(lambda: snapshot(2))) as result:
        with pytest.raises(SchemaError):
            result()
    mutate = False
    assert_denied(authority)
