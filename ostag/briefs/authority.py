"""Trusted LOCAL composition for the manual BriefService, not an issuance API.

Only application code with independently established identity/ACL evidence may
publish. Python types, wire parsing, event.raw and model content prove no rights.
Refresh accepts only a trusted application callable, never event.raw or model
data. There is no credential loader, environment switch, route, scheduler or
background refresh; loader latency and cancellation belong to the caller.

Snapshot collections accept bounded builtin lists/tuples and freeze to tuples;
permissions are (Principal, level), destinations are (RequesterContext,
PrivateDestination). Omitted entries grant nothing. Source/grant references,
when present together, must agree. Collection identity remains distinct from
the requesting principal, as in SourceRef's existing contract.

Publication starts by clearing access and advancing an epoch, validates outside
the mutex, and commits only if no newer mutation started. The final coherent
check linearizes policy observation. It cannot atomically revoke bytes already
handed to a network. Successful checks are never durable authorization.
"""
from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
import hashlib
import math
from threading import Lock, get_ident
from time import monotonic as system_monotonic
from types import MethodType

from ostag.briefs.grants import PilotGrant
from ostag.briefs.models import _identifier, _principal, _sequence, _utc
from ostag.briefs.service import BriefService, SourceSnapshot
from ostag.briefs.store import BriefStore, PrivateDestination, RequesterContext, utc_now
from ostag.config import Settings
from ostag.core.identity import Principal
from ostag.core.schemas import PERMISSION_LEVELS, SchemaError


def _denied():
    return SchemaError("brief_authorization_denied", "Local authority unavailable.",
                       "Re-establish trusted local authority before retrying.")


def _exact(value, cls):
    if type(value) is not cls:
        raise _denied()
    return value


def _rows(value):
    return _sequence(value, "authority entries", 500, empty=True)


def _pairs(value):
    return tuple(_sequence(row, "authority pair", 2) for row in _rows(value))


def _lookup_key(value, *, scope=False):
    try:
        return replace(_exact(value, RequesterContext)) if scope else _identifier(value, "key")
    except Exception:
        raise _denied() from None


@dataclass(frozen=True)
class AuthoritySnapshot:
    """Complete immutable policy input; constructing one does not publish it.

    At most 500 entries per collection and a 4 MiB total excerpt budget. No
    implicit principal permissions, grant issuance or destination resolution.
    UTC timestamps use the existing Brief contracts (Z or +00:00).
    """
    revision: int
    observed_at: str
    fresh_until: str
    permissions: tuple[tuple[Principal, str], ...] = ()
    destinations: tuple[tuple[RequesterContext, PrivateDestination], ...] = ()
    grants: tuple[PilotGrant, ...] = ()
    sources: tuple[SourceSnapshot, ...] = ()

    def __post_init__(self):
        try:
            self._freeze()
        except Exception:
            raise _denied() from None

    def _freeze(self):
        if type(self.revision) is not int or not 0 < self.revision <= 2**63 - 1:
            raise _denied()
        if _utc(self.observed_at, "observed") >= _utc(self.fresh_until, "expiry"):
            raise _denied()
        permissions, destinations, grants, sources = [], [], [], []
        for principal, level in _pairs(self.permissions):
            principal = _principal(principal)
            # Settings' exact platform:actor:level parser must round trip.
            if ":" in principal.actor_id or type(level) is not str or level not in PERMISSION_LEVELS:
                raise _denied()
            permissions.append((principal, level))
        for scope, destination in _pairs(self.destinations):
            scope = replace(_exact(scope, RequesterContext))
            destination = replace(_exact(destination, PrivateDestination))
            if scope.principal != destination.principal:
                raise _denied()
            destinations.append((scope, destination))
        for grant in _rows(self.grants):
            grants.append(replace(_exact(grant, PilotGrant)))
        for answer in _rows(self.sources):
            _exact(answer, SourceSnapshot)
            scope = replace(_exact(answer.scope, RequesterContext))
            answer = SourceSnapshot(scope, answer.source, answer.fresh_until)
            source = answer.source
            if (scope.namespace != source.namespace
                    or source.allowed_audience != (scope.principal,)
                    or hashlib.sha256(source.excerpt.encode()).hexdigest() != source.content_sha256
                    or _utc(source.observed_at, "observed") >= _utc(answer.fresh_until, "expiry")):
                raise _denied()
            sources.append(answer)
        if sum(len(s.source.excerpt.encode()) for s in sources) > 4 * 1024 * 1024:
            raise _denied()
        for keys in ([p for p, _ in permissions], [s for s, _ in destinations],
                     [g.grant_id for g in grants], [(s.scope, s.source.source_id) for s in sources]):
            if len(keys) != len(set(keys)):
                raise _denied()
        # One private address cannot name two different principals on a platform.
        owners = {}
        for scope, destination in destinations:
            key = (scope.principal.platform, destination.address)
            if key in owners and owners[key] != scope.principal:
                raise _denied()
            owners[key] = scope.principal
        by_grant = {g.grant_id: g for g in grants}
        for answer in sources:
            s, scope = answer.source, answer.scope
            grant = by_grant.get(s.grant_id)
            if grant is not None and (grant.namespace != scope.namespace
                    or grant.principal != scope.principal or grant.purpose != s.purpose
                    or grant.revision != s.grant_revision or s.resource_id not in grant.resource_ids
                    or grant.allowed_audience != s.allowed_audience):
                raise _denied()
        for name, value in (("permissions", permissions), ("destinations", destinations),
                            ("grants", grants), ("sources", sources)):
            object.__setattr__(self, name, tuple(value))


@dataclass(frozen=True)
class _State:
    snapshot: AuthoritySnapshot
    permissions: tuple[str, ...]
    destinations: dict
    grants: dict
    sources: dict
    observed: datetime
    expiry: datetime


@dataclass
class _Pass:
    state: _State
    epoch: int
    thread: int
    observed: datetime
    expiry: datetime
    active: bool = True


class TrustedBriefAuthority:
    """Bounded, thread-safe mirror with fail-closed optimistic authorization.

    clock and monotonic are trusted local clock functions, sampled outside the
    mutex; neither may load policy. max_ttl_seconds is a strict int in [1, 300]
    (default 60). Invalid clocks permanently latch this instance closed.
    publish/refresh failures and invalidate retain the accepted revision high-water.
    """
    def __init__(self, *, clock=utc_now, monotonic=system_monotonic, max_ttl_seconds=60):
        if (type(max_ttl_seconds) is not int or not 1 <= max_ttl_seconds <= 300
                or not callable(clock) or not callable(monotonic)):
            raise _denied()
        self._wall, self._monotonic, self._ttl = clock, monotonic, max_ttl_seconds
        self._lock = Lock()
        self._state, self._epoch, self._revision = None, 0, 0
        self._broken = False
        self._last_wall = self._last_mono = self._effective = None
        self._anchor_wall = self._anchor_mono = None
        self._pin = ContextVar("brief_authority_pass", default=None)
        self._sampling = ContextVar("brief_authority_clock", default=False)

    def invalidate(self):
        """Immediately revoke usable state and all in-flight passes."""
        with self._lock:
            self._state = None
            self._epoch += 1

    def _break_clock(self):
        with self._lock:
            self._broken = True
            self._state = None
            self._epoch += 1

    def _now(self):
        # Compare against observations completed BEFORE this sample began.
        # Overlapping samples can complete out of order; merge their maxima,
        # never interpret scheduling inversion as a physical clock rollback.
        with self._lock:
            if self._broken:
                raise _denied()
            before_wall, before_mono = self._last_wall, self._last_mono
        if self._sampling.get():
            self._break_clock()
            raise _denied()
        token = self._sampling.set(True)
        try:
            wall = _utc(self._wall(), "clock")
            mono = self._monotonic()
            if (type(mono) not in (int, float) or not math.isfinite(mono) or mono < 0
                    or (before_wall is not None and wall < before_wall)
                    or (before_mono is not None and mono < before_mono)):
                raise _denied()
        except Exception:
            self._break_clock()
            raise _denied() from None
        finally:
            self._sampling.reset(token)
        with self._lock:
            if self._broken:
                raise _denied()
            try:
                if self._last_mono is None:
                    self._anchor_wall, self._anchor_mono = wall, mono
                    effective = wall
                else:
                    # Use an anchor, not a sum of rounded datetime deltas:
                    # sub-microsecond clock increments must still accumulate.
                    effective = self._anchor_wall + timedelta(
                        seconds=max(mono, self._last_mono) - self._anchor_mono)
                    if wall > effective:
                        self._anchor_wall = effective = wall
                        self._anchor_mono = max(mono, self._last_mono)
                self._effective = effective if self._effective is None else max(self._effective, effective)
                self._last_wall = wall if self._last_wall is None else max(wall, self._last_wall)
                self._last_mono = mono if self._last_mono is None else max(mono, self._last_mono)
            except (OverflowError, ValueError):
                self._broken, self._state = True, None
                self._epoch += 1
                raise _denied() from None
            return self._effective

    def clock(self):
        """Operational time also bounds sources/grants; no freshness renewal.

        Usable policy is not required for store failure/cancellation accounting.
        A broken clock still denies. Elapsed monotonic time advances a stalled
        wall clock, including across mirror refreshes.
        """
        return self._now().isoformat().replace("+00:00", "Z")

    def _begin_publication(self):
        with self._lock:
            self._state = None
            self._epoch += 1
            return self._epoch

    def publish(self, snapshot: AuthoritySnapshot):
        """Replace the complete mirror. Invalid input never falls back."""
        self._commit_snapshot(snapshot, self._begin_publication())

    def refresh(self, loader: Callable[[], AuthoritySnapshot] | None = None):
        """Revoke first, then synchronously load and publish trusted policy.

        loader is trusted application code with independently established
        identity/ACL evidence, not event.raw or model data. It takes no arguments
        and must return an exact AuthoritySnapshot. This API issues no rights.
        Loading, parsing and clock sampling run outside the authority mutex;
        caller code owns loader latency, timeouts and cancellation. Exceptions,
        including cancellation, become a fixed private denial.

        Any newer mutation (including a reentrant one or a failed attempt)
        supersedes this refresh. Failure never restores old state or clears a
        newer success. Freshness and accepted revision high-water do not reset.
        The return value conveys no durable authorization or reconciliation.
        """
        epoch = self._begin_publication()
        try:
            if not callable(loader):
                raise _denied()
            snapshot = loader()
            self._commit_snapshot(snapshot, epoch)
        except BaseException:
            # Revocation already happened at entry. Cleanup here could revoke a
            # later successful mutation; even cancellation must not do that.
            raise _denied() from None

    def _commit_snapshot(self, snapshot, epoch):
        """Shared validation and commit; epoch was assigned before caller work."""
        try:
            snapshot = replace(_exact(snapshot, AuthoritySnapshot))
            observed = _utc(snapshot.observed_at, "observed")
            expiry = _utc(snapshot.fresh_until, "expiry")
            if (expiry - observed).total_seconds() > self._ttl:
                raise _denied()
            state = _State(snapshot,
                tuple(f"{p.platform}:{p.actor_id}:{level}" for p, level in snapshot.permissions),
                dict(snapshot.destinations), {g.grant_id: g for g in snapshot.grants},
                {(s.scope, s.source.source_id): s for s in snapshot.sources}, observed, expiry)
            now = self._now()
        except Exception:
            raise _denied() from None
        with self._lock:
            if (self._broken or epoch != self._epoch or snapshot.revision <= self._revision
                    or not observed <= max(now, self._effective) < expiry):
                raise _denied()
            self._revision, self._state = snapshot.revision, state

    def _read(self):
        now = self._now()
        pin = self._pin.get()
        with self._lock:
            state = self._state
            if (self._broken or state is None or not state.observed <= max(now, self._effective) < state.expiry
                    or (pin is not None and (not pin.active or pin.thread != get_ident()
                        or pin.epoch != self._epoch or pin.state is not state
                        or not pin.observed <= max(now, self._effective) < pin.expiry))):
                raise _denied()
            return state, self._epoch

    @contextmanager
    def coherent_check(self):
        """Pin ONE view, then fence all mutations and time after policy work.

        No lock spans caller work. Synchronous nesting is rejected without
        replacing the outer context. A copied expired context cannot reuse it.
        """
        if self._pin.get() is not None:
            raise _denied()
        state, epoch = self._read()
        pin = _Pass(state, epoch, get_ident(), state.observed, state.expiry)
        token = self._pin.set(pin)
        try:
            yield
            self._read()
        finally:
            pin.active = False
            self._pin.reset(token)

    def settings(self):
        state, _ = self._read()
        return Settings(default_permission_level="read", actor_permissions=state.permissions)

    def grants(self, grant_id):
        grant_id = _lookup_key(grant_id)
        state, _ = self._read()
        grant = state.grants.get(grant_id)
        if grant is not None:
            self._bounds(grant.valid_from, grant.expires_at)
            if grant.revoked_at is not None:
                self._bounds(grant.valid_from, grant.revoked_at)
        return deepcopy(grant)

    def sources(self, scope, source_id):
        scope, source_id = _lookup_key(scope, scope=True), _lookup_key(source_id)
        state, _ = self._read()
        answer = state.sources.get((scope, source_id))
        if answer is not None:
            self._bounds(answer.source.observed_at, answer.fresh_until)
        return deepcopy(answer)

    def destination(self, scope):
        scope = _lookup_key(scope, scope=True)
        state, _ = self._read()
        return deepcopy(state.destinations.get(scope))

    def _bounds(self, observed, expiry):
        # Final fencing includes every consulted source/grant even if a final
        # stopped()/clock callback consumes its remaining lifetime. Parsing is
        # outside the mutex; the final locked comparison is constant time.
        pin = self._pin.get()
        if pin is not None:
            pin.observed = max(pin.observed, _utc(observed, "observed"))
            pin.expiry = min(pin.expiry, _utc(expiry, "expiry"))

    def _check_binding(self, callbacks):
        # Concrete methods only: no duck-typed policy boundary or mixed mirrors.
        expected = (TrustedBriefAuthority.settings, TrustedBriefAuthority.grants,
                    TrustedBriefAuthority.sources, TrustedBriefAuthority.destination,
                    TrustedBriefAuthority.clock)
        for actual, method in zip(callbacks, expected, strict=True):
            if (type(actual) is not MethodType or actual.__self__ is not self
                    or actual.__func__ is not method):
                raise _denied()

    def create_service(self, *, store, transport, config, retention=None, max_intake=8,
                       lifecycle=None):
        """Explicit initialized store + audited read-only transport/config only.

        No path creation, lifecycle enablement, routing or retention ownership.
        Transport audit remains the caller's responsibility, as in A03/A04.
        """
        from ostag.briefs.generation import GenerationConfig
        if (type(store) is not BriefStore or type(config) is not GenerationConfig
                or not callable(getattr(transport, "generate", None))):
            raise _denied()
        return BriefService(store=store, transport=transport, config=config,
            settings=self.settings, grants=self.grants, sources=self.sources,
            destination=self.destination, clock=self.clock, retention=retention,
            max_intake=max_intake, authority=self, lifecycle=lifecycle)
