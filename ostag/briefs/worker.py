"""One manual A04 worker on an owned daemon thread and private asyncio loop.

run_once submits at most one job; start opts into polling. No production lifespan
is installed. LeaseGuard renews independently of synchronous transport/SQLite
work. The transport must satisfy the A03 trusted runtime contract; a Protocol
cannot sandbox tools, certify billing or force remote cancellation.

stop sets a thread-safe local latch before bridging via call_soon_threadsafe.
The private loop is closed without asyncio.run's unbounded pending-task cleanup.
A cancellation-suppressing or synchronously stuck transport may outlive local
stop; daemon custody and a permanent worker quarantine prevent another call or
late commit. Stop describes LOCAL custody only, never remote termination. Costs
remain reserved/started/unknown until trusted evidence settles them. Call IDs
are stable across recovery; A04 conservatively stops for review rather than
retrying any possibly started invocation. A reserved, never-started record can
be released with its durable ledger evidence; its ID tombstone is retained.
A restart seeing started/unknown spend quarantines BEFORE touching job rows:
expired generating rows and their original attempt/fence remain available for
operator reconciliation, without consuming another job's retry.

Future notifications have one dedicated daemon thread and one mailbox. A slow
callback retains the submission slot; it cannot block the worker loop or stop,
and never causes another executor/thread to be created. stop does not join
arbitrary callback code; local_stopped describes the execution loop only.
Callbacks registered after a Future is done follow the standard Future API and
run on their registering thread.

All precommit policy callbacks are bounded, current, thread-safe, non-reentrant
(see service module). No store/guard operations follow their final ACL checks.
"""
from __future__ import annotations

import asyncio
import json
from concurrent.futures import Future, InvalidStateError
from dataclasses import replace
from threading import Condition, Event, Lock, Thread, current_thread
from time import monotonic
from uuid import uuid4

from ostag.briefs.generation import generate
from ostag.briefs.models import _invalid
from ostag.briefs.lifecycle import LifecycleDenied
from ostag.briefs.service import BriefService, ConfigChanged
from ostag.briefs.meeting_service import DurableMeetingRequest, MeetingTimeout, register_retention
from ostag.briefs.store import (
    ClockError, Conflict, LeaseGuard, LeaseLost, RecoveryValidity, StoreBusy, StoreError, WorkerContext,
)
from ostag.core.schemas import SchemaError

CALL_ID = "manual-generation-1"


class _GenerationStore:
    """A03's explicit persistence surface with A04's bounded busy policy.

    A received response stays in the original generate invocation while its
    usage/final reads wait for SQLite. Only rolled-back database work retries;
    there is no transport retry or Backend adaptation. Every IO/accept checkpoint
    still performs final current authorization after all persistence waits.
    """

    def __init__(self, store, guard):
        self.store, self.guard = store, guard

    def assert_current(self, lease, *, clock):
        if lease != self.guard.lease:
            raise Conflict("generation lease mismatch")
        return self.guard.assert_current()

    def reserve_cost(self, lease, *, clock, **kwargs):
        return self.guard.database(self.store.reserve_cost, lease, **kwargs)

    def start_cost(self, lease, reservation_id, *, clock, **kwargs):
        return self.guard.database(self.store.start_cost, lease, reservation_id, **kwargs)

    def settle_cost(self, worker, reservation_id, usage, *, clock):
        return self.guard.database(self.store.settle_cost, worker, reservation_id, usage,
                                   evidence=True)

    def mark_cost_unknown(self, worker, reservation_id, diagnostic, *, clock):
        return self.guard.database(self.store.mark_cost_unknown, worker, reservation_id,
                                   diagnostic, evidence=True)

    def release_cost(self, worker, reservation_id, *, clock):
        return self.guard.database(self.store.release_cost, worker, reservation_id, evidence=True)


class _OwnedModel:
    """Count the inner adapter coroutine, including detached suppression/GC."""
    def __init__(self, transport, lifecycle, parent):
        self.transport, self.lifecycle, self.parent = transport, lifecycle, parent

    async def generate(self, request):
        op = self.lifecycle.begin("model", parent=self.parent)
        try:
            original_reauthorize = request.reauthorize
            def reauthorize():
                result = original_reauthorize()
                op.check()
                return result
            request = replace(request, reauthorize=reauthorize)
            result = await self.transport.generate(request)
        except BaseException:
            op.uncertain()
            raise
        else:
            op.finish()
            return result


class BriefWorker:
    def __init__(self, service: BriefService, *, heartbeat_interval: float = 10):
        self.service, self.store = service, service.store
        self.lifecycle = service.lifecycle
        if self.lifecycle is not None:
            self.lifecycle.own_worker(self)
        self._operation = None
        self.context = WorkerContext("manual-" + uuid4().hex)
        self.heartbeat_interval = heartbeat_interval
        self._stop = Event()
        self._ready = Event()
        self._lock = Lock()
        self._future: Future | None = None
        self._busy = False
        self._notifying = False
        self._published = False
        self._completion = None
        self._completed = Condition(self._lock)
        self._cancellation: asyncio.Event | None = None
        self._quarantined = False
        self._loop = asyncio.new_event_loop()
        self._thread = Thread(target=self._thread_main, name="brief-worker", daemon=True)
        self._notifier = Thread(target=self._notify, name="brief-completion", daemon=True)
        self._notifier.start()
        self._thread.start()
        self._ready.wait(1)

    @property
    def quarantined(self):
        return self._quarantined

    def _thread_main(self):
        asyncio.set_event_loop(self._loop)
        self._ready.set()
        try:
            self._loop.run_forever()
        finally:
            # Do not await arbitrary transport cleanup. Outstanding tasks cannot
            # resume on this closed loop; synchronous stuck code remains fenced
            # by the thread-safe stop latch when it eventually returns.
            if self._operation is not None:
                self._operation.uncertain()
            self._loop.close()

    def run_once(self) -> Future:
        """One live work/result slot, retained through callback completion.

        Reentrant submissions during notification return busy; callbacks never
        occupy the worker loop or an internal lock. Cancelling this Future does
        not release custody of the underlying work.
        """
        result = Future()
        with self._lock:
            if self._stop.is_set() or self._quarantined:
                status = "stopped"
            elif self._busy or self._notifying:
                status = "busy"
            else:
                try:
                    operation = (None if self.lifecycle is None
                                 else self.lifecycle.begin("worker"))
                except LifecycleDenied:
                    result.set_result("unavailable")
                    return result
                self._operation = operation
                self._future = result
                self._busy = True
                self._published = False
                try:
                    self._loop.call_soon_threadsafe(
                        lambda: self._loop.create_task(self._dispatch(result)))
                except RuntimeError:
                    # stop may close the loop between the latch and scheduling.
                    self._busy = False
                    if operation is not None:
                        operation.finish()
                    self._operation = None
                    status = "stopped"
                else:
                    return result
        result.set_result(status)  # not exposed yet; no registered callbacks
        return result

    def _publish_locked(self, future, status):
        # A single mailbox: dispatch and stop compete for one publication.
        # This only hands off data; Future methods execute outside our locks.
        if not self._published:
            self._published = True
            self._notifying = True
            self._completion = (future, status)
            self._completed.notify()

    def _notify(self):
        while True:
            with self._completed:
                self._completed.wait_for(lambda: self._completion is not None or self._stop.is_set())
                if self._completion is None:
                    return
                future, status = self._completion
                self._completion = None
            try:
                future.set_result(status)
            except InvalidStateError:
                # Public cancellation may win immediately before set_result.
                pass
            finally:
                with self._lock:
                    self._notifying = False

    async def _dispatch(self, future):
        try:
            status = await self._execute()
        except LifecycleDenied:
            status = "unavailable"
        except (StoreBusy, ClockError, Conflict):
            # No confirmed invocation is at risk on these idle/preclaim paths.
            # Every subsequent submission must redo trusted clock/DB checks.
            status = "store_unavailable"
        except Exception:
            self._quarantined = True
            if self._operation is not None:
                self._operation.uncertain()
            status = "store_unavailable"
        except BaseException:
            if self._operation is not None:
                self._operation.uncertain()
            raise
        if self._operation is not None:
            self._operation.finish()
        with self._lock:
            self._operation = None
            self._busy = False
            self._publish_locked(future, status)

    def start(self):
        """Optional polling on the owned loop; global SQLite lease remains authoritative."""
        async def poll():
            while not self._stop.is_set() and not self._quarantined:
                await asyncio.wrap_future(self.run_once())
                await asyncio.sleep(0.05)
        with self._lock:
            if self._stop.is_set() or getattr(self, "_polling", False):
                return
            self._polling = True
        try:
            self._loop.call_soon_threadsafe(lambda: self._loop.create_task(poll()))
        except RuntimeError:
            self._polling = False

    def stop(self, timeout: float = 0.5) -> str:
        """Bounded local shutdown; remote invocation outcome may remain unknown."""
        if type(timeout) not in (float, int) or not 0 <= timeout <= 5:
            raise ValueError("bounded stop timeout required")
        with self._completed:
            self._stop.set()
            self._quarantined = True
            if self._future is not None:
                self._publish_locked(self._future, "stopped")
            self._completed.notify()
        def bridge():
            if self._cancellation is not None:
                self._cancellation.set()
            self._loop.call_later(min(timeout / 2, 0.1), self._loop.stop)
        if not self._loop.is_closed():
            try:
                self._loop.call_soon_threadsafe(bridge)
            except RuntimeError:
                pass
        if current_thread() is not self._thread:
            self._thread.join(timeout)
        return "local_stopping" if self._thread.is_alive() else "local_stopped"

    def _check_operation(self):
        if self.lifecycle is None:
            return
        if self._operation is not None:
            self._operation.check()
        if self._stop.is_set():
            raise LifecycleDenied()

    def _recovery_valid(self, job):
        try:
            self._check_operation()
            request, _ = self.service.load_job(job)
            cost = self.store.cost_record(job.scope, job.job_id, CALL_ID)
            # No retry with a new ID, including settled responses lost before commit.
            if cost is not None:
                if cost.state == "reserved":
                    self.store.release_cost(self.context, cost.reservation_id,
                                            clock=self.service.clock)
                return RecoveryValidity(False, False, False)
            if type(request) is DurableMeetingRequest:
                # Preserve the existing never-started reservation release above,
                # but grant no collection/model resume even when nothing spent.
                return RecoveryValidity(False, False, False)
            self.service.authorize_current(request, job.scope, job.destination,
                                           stopped=self._stop.is_set, job_id=job.job_id,
                                           operation=self._operation)
            return RecoveryValidity(True, True, True)
        except (StoreBusy, ClockError, Conflict):
            # Recovery validation did not complete; leave the detached candidate
            # untouched so a later submission repeats fresh checks.
            raise
        except Exception:
            return RecoveryValidity(False, False, False)

    def _fail(self, job, cause):
        """Bounded maintenance CAS; never reuse a lost guard as an active owner."""
        for _ in range(3):
            current = self.store.get(job.scope, job.job_id)
            if current is None or current.fence != job.fence:
                return
            if self.store.record_expired_failure(self.context, current, cause=cause,
                                                clock=self.service.clock, retire_current=True):
                return
        raise Conflict("failure maintenance row changed")

    def _reconcile(self, operation):
        """Retry only rolled-back maintenance/read work for at most 250 ms.

        This path cannot renew, claim, call a provider or accept private rows.
        Each mutation still checks a fresh trusted clock and its own CAS/cost
        evidence. Expiry does not forbid recording failure or actual usage.
        One SQLite operation may additionally take its configured busy_timeout;
        Python callbacks/fsync are not preemptible. stop interrupts retry waits.
        """
        until = monotonic() + 0.25
        while True:
            try:
                return operation()
            except StoreBusy:
                if monotonic() >= until or self._stop.wait(0.01):
                    raise

    async def _execute(self):
        operation = self._operation
        if self._stop.is_set() or not self.service.configured:
            return "unavailable"
        self._check_operation()
        # Quarantine before recovery consumes an unrelated attempt/fence.
        if self.store.has_uncertain_cost(self.context):
            self._quarantined = True
            if operation is not None:
                operation.uncertain()
            return "needs_input"
        claim_gate = {} if self.lifecycle is None else {"precommit": self._check_operation}
        job = self.store.recover_one(self.context, validate=self._recovery_valid,
                                    clock=self.service.clock, cause="recovery_required",
                                    require_known_costs=True, **claim_gate)
        if job is None:
            job = self.store.claim_one(self.context, clock=self.service.clock,
                                       require_known_costs=True, **claim_gate)
        if job is None and self.store.has_uncertain_cost(self.context):
            self._quarantined = True
            if operation is not None:
                operation.uncertain()
            return "needs_input"
        if job is None:
            return "idle"
        cancellation = asyncio.Event()  # timeout cooperation is NOT requester cancel intent
        self._cancellation = cancellation
        cause = "store_error"
        operator_cancel = self.service.watch_cancellation(job.scope, job.job_id)
        guard = None
        try:
            with LeaseGuard(self.store, job.lease, clock=self.service.clock,
                            interval=self.heartbeat_interval, retry_busy=True,
                            confirmed_job=job, stop_signal=self._stop) as guard:
                self._check_operation()
                request, config = self.service.load_job(job)
                meeting = type(request) is DurableMeetingRequest
                if meeting:
                    request.check_budget(config, self.service.clock())
                    self.service.authorize_current(request, job.scope, job.destination,
                        stopped=self._stop.is_set, operation=operation)
                    register_retention(self.service, job)
                cost = guard.database(self.store.cost_record, job.scope, job.job_id, CALL_ID,
                                      clocked=False)
                if cost is not None:
                    cause = "recovery_required"
                    raise _invalid("prior invocation requires reconciliation")
                job = guard.database(self.store.start_generating, job.lease)

                def stopped():
                    return (self._stop.is_set() or guard.lost or cancellation.is_set()
                            or operator_cancel.is_set()
                            or (operation is not None and not operation.allowed()))

                def authorization():
                    nonlocal cause
                    if self.service.config != config:
                        cause = "config_changed"
                        raise ConfigChanged()
                    if meeting:
                        request.check_budget(config, self.service.clock())
                    try:
                        self.service.authorize_current(request, job.scope, job.destination,
                                                       stopped=stopped, job_id=job.job_id,
                                                       operation=operation)
                        if meeting:
                            request.check_budget(config, self.service.clock())
                    except SchemaError:
                        cause = "authorization_denied"
                        raise

                def checkpoint(phase, lease, sources):
                    # DB/guard checks precede the FINAL current per-source checks.
                    guard.assert_current()
                    authorization()

                async def watch():
                    while not cancellation.is_set():
                        await asyncio.sleep(0.02)
                        # No SQLite polling: the independent heartbeat observes
                        # persisted cancellation/fences; local intent is immediate.
                        if stopped():
                            cancellation.set()

                transport = self.service.transport
                if self.lifecycle is not None:
                    transport = _OwnedModel(transport, self.lifecycle, operation)
                watcher = asyncio.create_task(watch())
                try:
                    result = await generate(store=_GenerationStore(self.store, guard),
                        lease=job.lease, call_id=CALL_ID,
                        sources=request.source_set(job.scope), coverage=request.coverage,
                        goal=request.goals, config=config, transport=transport,
                        checkpoint=checkpoint, cancellation=cancellation, clock=self.service.clock,
                        **(dict(links=request.links, request_deadline=request.deadline(config),
                                request_input_limit=request.scope.budget.max_model_input_bytes)
                           if meeting else {}))
                finally:
                    watcher.cancel()
                if cause != "config_changed":
                    cause = result.status
                if result.status == "draft_candidate":
                    cause = "commit_failed"
                    # precommit runs AFTER lock acquisition, writes and relational
                    # checks. It must not call guard/store while holding their locks.
                    guard.complete(draft=result.draft.to_dict(),
                        provenance=request.completion_provenance(
                            job.scope, result.provenance, job.content_hash),
                        validation_version=config.parameters.validation_version,
                        precommit=authorization)
                    if meeting:
                        register_retention(self.service, self.store.get(job.scope, job.job_id))
                        authorization()
                    return "draft_ready"
                if self._stop.is_set():
                    cause = "stopped"
                elif guard.lost:
                    cause = "lease_lost"
        except ConfigChanged:
            cause = "config_changed"
        except MeetingTimeout:
            cause = "timeout"
        except LeaseLost:
            cause = "lease_lost"
        except SchemaError:
            if cause not in ("commit_failed", "recovery_required"):
                cause = "authorization_denied"
        except Exception:
            # Trusted callback/provider/store bugs are domain failures too. Never
            # serialize an exception string, class-derived status or private data.
            pass
        finally:
            if guard is not None and guard.running and operation is not None:
                operation.uncertain()
            self._cancellation = None
            self.service.unwatch_cancellation(job.scope, job.job_id)
        # Never retransmit to recover a lost successful COMMIT response.
        try:
            return self._reconcile(lambda: self._reconcile_result(job, cause, operator_cancel))
        except (StoreBusy, ClockError, Conflict):
            # Unavailability is not evidence of unknown spend. On the next run,
            # the durable global cost check must succeed BEFORE any new claim.
            if operation is not None:
                operation.uncertain()
            return "store_unavailable"
        except (StoreError, SchemaError):
            if operation is not None:
                operation.uncertain()
            self._quarantined = True
            return "store_unavailable"

    def _reconcile_result(self, job, cause, operator_cancel):
        box = self.store.outbox(job.scope, job.job_id)
        if box is not None:
            if box.state == "pending":
                if json.loads(job.payload_json).get("kind") == "meeting-brief-v1":
                    # Primary completion may precede retention registration or
                    # its acknowledgement. Do not advertise exposure readiness;
                    # authorized status/export can repair registration idempotently.
                    return "needs_input"
            return "draft_ready" if box.state == "pending" else "cancelled"
        if operator_cancel.is_set():
            # The authenticated cancellation handler may still be waiting
            # for SQLite. Persist that intent before any needs_input finish
            # can win the lock race and make A02 cancellation impossible.
            self.store.cancel(job.scope, job.job_id, clock=self.service.clock)
            cause = "cancelled"
        else:
            self._fail(job, cause)
        cost = self.store.cost_record(job.scope, job.job_id, CALL_ID)
        if cost is not None:
            if cost.state == "reserved":
                self.store.release_cost(self.context, cost.reservation_id,
                                        clock=self.service.clock)
            elif cost.state in ("started", "unknown"):
                if self._operation is not None:
                    self._operation.uncertain()
                self._quarantined = True
                self.store.mark_cost_unknown(self.context, cost.reservation_id, cause,
                                             clock=self.service.clock)
        current = self.store.get(job.scope, job.job_id)
        return current.state if current is not None else "unavailable"
