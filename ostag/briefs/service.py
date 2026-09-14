"""Opt-in manual A04 service. No provider adapter, UI, delivery or lifespan wiring.

Syntax: /brief <strict JSON object>. Required keys: version="manual-brief-v1",
namespace, purpose, window={start,end,display_timezone}, grant_id, grant_revision,
coverage={version="brief-coverage-v1",complete,gaps}, goals, sources=[A01 SourceRef
objects]. No extra keys/duplicate keys/coercion. Source text and goals are data.
Only manual sources are accepted; empty sources return needs_input without IO.
Coverage describes the submitted material, not the completeness of live systems.

NormalizedEvent is trusted adapter output, never fabricated from event.raw.
The injected source lookup must use the authenticated requester and source ID
and return the exact currently available snapshot, permitted collection identity
and freshness expiry ONLY after checking its current ACL. Missing means denied.
All callbacks are trusted, bounded, thread-safe and current on EVERY invocation;
settings/grants/source/destination must never be populated from request flags.
They also run in SQLite precommit: they MUST NOT call this store/LeaseGuard,
wait for these locks, or perform unbounded network IO. Use a trusted local policy
mirror that fails closed if stale. The entire authorization pass (all sources,
settings, grants and destination combined) has an operational latency budget of
100 ms, including under the SQLite lock. Provision/monitor that bound externally;
arbitrary Python callbacks cannot be forcibly preempted here. Do not configure
network policy lookups in this gate.

An explicit audited A03 ReadOnlyTransport is required. The Protocol does not
certify read-only behavior, actual provider identity or billing bounds; that
production adapter and D01/D07 semantic/platform pilots remain external gates.
Backend.run is never adapted. Budgets/config are frozen at admission, and current
config must still match on execution. Manual jobs have no Meeting FrozenScope;
future meeting jobs must map persisted first_claim_at and execution_seconds to
ExecutionTiming without reconstructing intake/deadline. They are rejected here.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
import hashlib
import json
from threading import BoundedSemaphore, Event, Lock

from ostag.briefs.generation import GenerationConfig, Provenance, ReadOnlyTransport, Usage, digest
from ostag.briefs.grants import GrantLookup, authorize
from ostag.briefs.models import (
    SourceRef, SourceSet, TimeWindow, _identifier, _invalid, _keys, _positive_int,
    _sequence, _text, _utc,
)
from ostag.briefs.store import (
    BriefStore, Conflict, Job, PrivateDestination, QueueFull, RequesterContext,
    StoreError, utc_now,
)
from ostag.briefs.validation import Coverage, bounded_int, strict_json
from ostag.config import Settings
from ostag.core.event import NormalizedEvent
from ostag.core.schemas import SchemaError


@dataclass(frozen=True)
class ManualRequest:
    namespace: str
    purpose: str
    window: TimeWindow
    grant_id: str
    grant_revision: int
    coverage: Coverage
    goals: str
    sources: tuple[SourceRef, ...]
    version: str = "manual-brief-v1"

    def __post_init__(self):
        if self.version != "manual-brief-v1":
            raise _invalid("only manual-brief-v1 is supported")
        for field in ("namespace", "purpose", "grant_id"):
            _identifier(getattr(self, field), field)
        _positive_int(self.grant_revision, "grant_revision")
        _text(self.goals, "goals", 16000)
        if type(self.window) is not TimeWindow or type(self.coverage) is not Coverage:
            raise _invalid("typed window and coverage required")
        refs = _sequence(self.sources, "sources", 500, empty=True)
        if any(type(s) is not SourceRef or s.source_kind != "manual" for s in refs):
            raise _invalid("only typed manual sources are supported")
        if len({s.source_id for s in refs}) != len(refs):
            raise _invalid("duplicate source ID")
        object.__setattr__(self, "window", replace(self.window))
        object.__setattr__(self, "coverage", replace(self.coverage))
        object.__setattr__(self, "sources", tuple(replace(s) for s in refs))
        # Entire metadata/excerpt envelope is bounded too, not just each excerpt.
        digest(self.to_dict())

    @classmethod
    def from_dict(cls, value):
        v = _keys(value, ("version", "namespace", "purpose", "window", "grant_id",
                          "grant_revision", "coverage", "goals", "sources"))
        v["window"] = TimeWindow.from_dict(v["window"])
        v["coverage"] = Coverage.from_dict(v["coverage"])
        v["sources"] = tuple(SourceRef.from_dict(s) for s in
                             _sequence(v["sources"], "sources", 500, empty=True))
        return cls(**v)

    def to_dict(self):
        return dict(version=self.version, namespace=self.namespace, purpose=self.purpose,
                    window=self.window.to_dict(), grant_id=self.grant_id,
                    grant_revision=self.grant_revision, coverage=self.coverage.to_dict(),
                    goals=self.goals, sources=[s.to_dict() for s in self.sources])

    def source_set(self, scope):
        return SourceSet(self.namespace, self.purpose, self.window, (scope.principal,), self.sources)

    def completion_provenance(self, scope, provenance, intake_hash):
        return provenance.to_dict() | {"sources": self.source_set(scope).to_dict(),
                                       "intake_hash": intake_hash}

    def validate_completion_size(self, scope, config):
        """Bound the full stored envelope before spending, preserving exact sources.

        These maximal numbers/identifier are sizing placeholders ONLY, never
        usage evidence or persisted metadata. Completion uses A03's actual
        response provenance. Identifiers are at most 256 ASCII characters.
        """
        p, b = config.parameters, config.parameters.budget
        upper = Provenance("0" * 64, "0" * 64, p, config, p.expected_model_id,
                           "0" * 256, Usage(b.max_input_bytes, b.max_output_tokens,
                                            b.reservation_micros))
        digest(self.completion_provenance(scope, upper, "0" * 64))


@dataclass(frozen=True)
class SourceSnapshot:
    """Trusted current ACL/availability answer for an exact collected snapshot."""
    scope: RequesterContext
    source: SourceRef
    fresh_until: str

    def __post_init__(self):
        if type(self.scope) is not RequesterContext or type(self.source) is not SourceRef:
            raise _invalid("typed trusted source answer required")
        self.scope.__post_init__()
        object.__setattr__(self, "source", replace(self.source))
        _utc(self.fresh_until, "fresh_until")


@dataclass(frozen=True)
class BriefStatus:
    status: str
    job_id: str | None = None

    def render(self):
        return "brief: " + self.status + (" " + self.job_id if self.job_id else "")


def is_brief_command(text: str) -> bool:
    return type(text) is str and text.strip().split(maxsplit=1)[:1] == ["/brief"]


class ConfigChanged(SchemaError):
    """Trusted pinned configuration mismatch; no private diagnostic payload."""

    def __init__(self):
        super().__init__("config_changed", "Admitted config pins changed.",
                         "Review the pinned configuration before submitting a new request.")


class BriefService:
    def __init__(self, *, store: BriefStore, transport: ReadOnlyTransport | None,
                 config: GenerationConfig | None, settings: Callable[[], Settings],
                 grants: GrantLookup, sources: Callable[[RequesterContext, str], SourceSnapshot | None],
                 destination: Callable[[RequesterContext], PrivateDestination | None],
                 clock: Callable[[], str] = utc_now, max_intake: int = 8):
        for callback in (settings, grants, sources, destination, clock):
            if not callable(callback):
                raise _invalid("mandatory trusted current callbacks required")
        bounded_int(max_intake, "max_intake", 20, 1)
        if config is not None and type(config) is not GenerationConfig:
            raise _invalid("explicit pinned generation config required")
        self.store, self.transport = store, transport
        self.config = None if config is None else replace(config)
        self.settings, self.grants, self.sources = settings, grants, sources
        self.destination, self.clock = destination, clock
        self._slots = BoundedSemaphore(max_intake)
        self._executor = ThreadPoolExecutor(max_workers=min(4, max_intake),
                                            thread_name_prefix="brief-intake")
        self._closed = Event()
        self._cancel_lock = Lock()
        self._active_cancellations = {}
        store.enable_manual()

    @property
    def configured(self):
        return (not self._closed.is_set() and type(self.config) is GenerationConfig
                and callable(getattr(self.transport, "generate", None)))

    def close(self):
        self._closed.set()
        self._executor.shutdown(wait=False, cancel_futures=True)

    def authorize_current(self, request, scope, destination, *, stopped):
        """No store/guard calls: safe for the trusted non-reentrant precommit gate.

        Caller performs ALL blocking persistence work before entering here.
        Each source is individually checked, including its collection identity.
        Retain every returned time bound: later callbacks can exhaust an earlier
        snapshot's freshness or grant lifetime. Recheck them together against a
        final trusted clock after all lookups, without another store/policy read.
        The final stop/time checks are nonblocking. Cross-system atomicity is
        impossible; callback completion is the logical authorization point.
        """
        if stopped() or not self.configured:
            raise _invalid("authorization unavailable")
        if request.namespace != scope.namespace:
            raise _invalid("scope mismatch")
        request.source_set(scope)
        resolved = self.destination(scope)
        if (type(resolved) is not PrivateDestination or resolved != destination
                or resolved.principal != scope.principal):
            raise _invalid("private destination changed")
        snapshots, grants = [], []
        for source in request.sources:
            if (source.grant_id != request.grant_id
                    or source.grant_revision != request.grant_revision
                    or source.content_sha256 != hashlib.sha256(source.excerpt.encode()).hexdigest()):
                raise _invalid("source snapshot mismatch")
            current = self.sources(scope, source.source_id)
            if (type(current) is not SourceSnapshot or current.scope != scope
                    or current.source != source):
                raise _invalid("source unavailable or ACL changed")
            now = self.clock()
            if not _utc(source.observed_at, "observed") <= _utc(now, "now") < _utc(
                    current.fresh_until, "fresh_until"):
                raise _invalid("source snapshot stale")
            snapshots.append(current)
            grant = authorize(lookup=self.grants, config=self.settings(), grant_id=request.grant_id,
                      grant_revision=request.grant_revision, namespace=scope.namespace,
                      principal=scope.principal, resource_ids=(source.resource_id,),
                      purpose=request.purpose, allowed_audience=(scope.principal,),
                      operation="generate", now=self.clock())
            grants.append(grant)
        final_now = _utc(self.clock(), "now")
        for snapshot in snapshots:
            if not _utc(snapshot.source.observed_at, "observed") <= final_now < _utc(
                    snapshot.fresh_until, "fresh_until"):
                raise _invalid("source snapshot stale")
        for grant in grants:
            if not _utc(grant.valid_from, "valid_from") <= final_now < _utc(
                    grant.expires_at, "expires_at"):
                raise _invalid("grant is future or expired")
            if grant.revoked_at is not None and final_now >= _utc(grant.revoked_at, "revoked_at"):
                raise _invalid("grant is revoked")
        if stopped():
            raise _invalid("authorization stopped")

    def load_job(self, job: Job):
        payload = _keys(json.loads(job.payload_json), ("kind", "request", "intake_at", "config"))
        if payload["kind"] != "manual-brief-v1":
            raise _invalid("meeting/foreign jobs require their own frozen scope mapping")
        _utc(payload["intake_at"], "intake_at")
        request = ManualRequest.from_dict(payload["request"])
        config = GenerationConfig.from_dict(payload["config"])
        if (not self.configured or config != self.config
                or job.execution_seconds != config.parameters.budget.timeout_seconds):
            raise ConfigChanged()
        if (job.grant_id != request.grant_id or job.grant_revision != request.grant_revision
                or job.scope.namespace != request.namespace):
            raise _invalid("admission binding mismatch")
        request.validate_completion_size(job.scope, config)
        return request, config

    def _submit(self, event):
        if not self.configured:
            return BriefStatus("unavailable")
        try:
            if type(event) is not NormalizedEvent or not is_brief_command(event.text):
                raise _invalid("normalized brief command required")
            _identifier(event.event_id, "event_id")
            parts = event.text.strip().split(maxsplit=1)
            request = ManualRequest.from_dict(strict_json(parts[1] if len(parts) == 2 else "", 1048576))
            scope = RequesterContext(request.namespace, event.principal)
            request_hash = digest(request.to_dict())
            existing = self.store.get_by_event(scope, event.event_id)
            if existing is not None:
                if not self.store.matches_request(scope, existing.job_id, request_hash):
                    return BriefStatus("conflict")
                return BriefStatus(existing.state, existing.job_id)
            if not request.sources or not any(s.excerpt.strip() for s in request.sources):
                return BriefStatus("needs_input")
            destination = self.destination(scope)
            config = self.config  # immutable admission pin; don't reload after callbacks
            request.validate_completion_size(scope, config)
            try:
                self.authorize_current(request, scope, destination,
                                       stopped=self._closed.is_set)
            except Exception:
                return BriefStatus("denied")
            intake = self.clock()
            job = self.store.admit(scope, idempotency_key=event.event_id,
                payload=dict(kind="manual-brief-v1", request=request.to_dict(), intake_at=intake,
                             config=config.to_dict()), grant_id=request.grant_id,
                grant_revision=request.grant_revision, destination=destination, now=intake,
                execution_seconds=config.parameters.budget.timeout_seconds, request_hash=request_hash,
                precommit=lambda: self.authorize_current(request, scope, destination,
                                       stopped=self._closed.is_set))
            return BriefStatus(job.state, job.job_id)
        except QueueFull:
            return BriefStatus("queue_full")
        except Conflict:
            return BriefStatus("conflict")
        except StoreError:
            return BriefStatus("store_unavailable")
        except SchemaError:
            return BriefStatus("invalid_request")
        except Exception:
            return BriefStatus("unavailable")

    def submit(self, event):
        """Synchronous trusted entry; use handle_event on shared event loops."""
        if not self._slots.acquire(blocking=False):
            return BriefStatus("intake_full")
        try:
            return self._submit(event)
        finally:
            self._slots.release()

    async def handle_event(self, event):
        # Acquire BEFORE executor submission; cancellation must not release a slot
        # while its synchronous callback is still running in another thread.
        if not self._slots.acquire(blocking=False):
            return BriefStatus("intake_full").render()
        try:
            future = self._executor.submit(self._submit, event)
        except RuntimeError:
            self._slots.release()
            return BriefStatus("unavailable").render()
        future.add_done_callback(lambda _: self._slots.release())
        # Shield queued work too: cancelling a concurrent Future would release
        # the semaphore while its cancelled executor queue node remains queued.
        try:
            return (await asyncio.shield(asyncio.wrap_future(future))).render()
        except asyncio.CancelledError:
            # Executor shutdown cancels queued work, not the requesting task.
            # Preserve real caller cancellation even if both happen together.
            task = asyncio.current_task()
            if task is not None and task.cancelling():
                raise
            if self._closed.is_set() and future.cancelled():
                return BriefStatus("unavailable").render()
            raise

    def watch_cancellation(self, scope: RequesterContext, job_id: str) -> Event:
        """Register only an active internal attempt; bounded by the one DB lease."""
        signal = Event()
        with self._cancel_lock:
            self._active_cancellations[(scope, job_id)] = signal
        return signal

    def unwatch_cancellation(self, scope: RequesterContext, job_id: str):
        with self._cancel_lock:
            self._active_cancellations.pop((scope, job_id), None)

    def cancel(self, scope: RequesterContext, job_id: str):
        """Trusted requester context only; persisted cancellation also invalidates outbox."""
        # Signal an active local acceptance gate before waiting for SQLite. The
        # persisted A02 cancelled state remains the durable operator intent;
        # timeout's cooperative asyncio.Event never implies requester cancel.
        with self._cancel_lock:
            signal = self._active_cancellations.get((scope, job_id))
            if signal is not None:
                signal.set()
        try:
            job = self.store.cancel(scope, job_id, clock=self.clock)
            return BriefStatus(job.state, job.job_id)
        except (StoreError, SchemaError):
            return BriefStatus("unavailable")
