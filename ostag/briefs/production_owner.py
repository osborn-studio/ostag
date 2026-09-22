"""FastAPI-owned Brief composition. No environment factory or authority JSON loader.

Host code supplies verified current adapters explicitly. This is an authenticated
in-process application surface, not provider ingress or an activation certificate.
One private loop keeps SQLite/card IO off the Monitor loop; BriefWorker retains
its existing model/lease threads. No new queue: discovery reads the existing store.
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, replace
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
from threading import Thread
from time import monotonic, time
from typing import Callable

from ostag.briefs.authority import TrustedBriefAuthority
from ostag.briefs.callbacks import CallbackLedger
from ostag.briefs.card_delivery import CardDeliveryService, CardLedger
from ostag.briefs.card_transport import CardTransport
from ostag.briefs.delivery import FeishuBinding
from ostag.briefs.generation import GenerationConfig
from ostag.briefs.lifecycle import BriefLifecycle, LifecycleDenied
from ostag.briefs.meeting_service import DurableMeetingRequest, register_retention
from ostag.briefs.openai_transport import OpenAIReadOnlyTransport, MODEL_ID as OPENAI_MODEL
from ostag.briefs.deepseek_transport import DeepSeekReadOnlyTransport, MODEL_ID as DEEPSEEK_MODEL
from ostag.briefs.rendering import PrivatePreview, render_private
from ostag.briefs.retention import RetentionCoordinator, RetentionLedger, RetentionPolicy, _identity
from ostag.briefs.revisions import RevisionLedger
from ostag.briefs.runtime import BriefStatusOwner, _directory, _entry, _regular
from ostag.briefs.service import BriefStatus
from ostag.briefs.startup_inventory import RuntimeInventoryConfig, ScanBudget
from ostag.briefs.startup_reconciliation import KINDS, reconcile_startup
from ostag.briefs.store import BriefStore, RequesterContext, _micros
from ostag.briefs.worker import BriefWorker
from ostag.core.event import NormalizedEvent


@dataclass(frozen=True)
class TrustedBriefComposition:
    """Code-owned deployment input, never deserialized from requests or boot JSON.

    authenticate receives an opaque verified-adapter context, never a bearer string
    manufactured by this module. It must return the current authenticated scope or
    deny. authority is the existing bounded revocable mirror. resolve_binding and
    transports remain independently audited host obligations, not Protocol proof.
    initialize_new permits ONLY an entirely absent five-store set in a precreated
    protected directory. maintenance cannot initialize, migrate or construct services.
    """
    directory: Path
    image_sha256: str
    authority: TrustedBriefAuthority
    model_transport: OpenAIReadOnlyTransport | DeepSeekReadOnlyTransport
    generation: GenerationConfig
    authenticate: Callable[[object], RequesterContext]
    resolve_binding: Callable[[], FeishuBinding]
    card_transport: CardTransport
    owners: tuple[RequesterContext, ...]
    policy: RetentionPolicy
    inventory: RuntimeInventoryConfig = RuntimeInventoryConfig()
    initialize_new: bool = False
    maintenance: bool = False

    def __post_init__(self):
        if (not isinstance(self.directory, Path) or type(self.inventory) is not RuntimeInventoryConfig):
            raise ValueError('trusted Brief composition required')
        for kind, version in zip(KINDS, (4, 2, 1, 1, 1)):
            schema = getattr(self.inventory, kind + '_schema')
            budget = getattr(self.inventory, kind + '_budget')
            if (type(schema) is not int or schema != version or type(budget) is not ScanBudget
                    or type(budget.row_limit) is not int or not 1 <= budget.row_limit <= 10000
                    or type(budget.budget_ms) is not int or not 1 <= budget.budget_ms <= 5000):
                raise ValueError('trusted Brief inventory configuration required')
        from ostag.briefs.revisions import MAX_CHAIN, MAX_EVENTS, MAX_ROWS
        for value, maximum in ((self.inventory.card_capacity, 10000),
                (self.inventory.retention_capacity, 100000),
                (self.inventory.revision_capacity, MAX_ROWS),
                (self.inventory.callback_capacity, 10000),
                (self.inventory.revision_chain_limit, MAX_CHAIN),
                (self.inventory.revision_event_limit, MAX_EVENTS)):
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError('trusted Brief inventory configuration required')
        if (not self.directory.is_absolute() or '..' in self.directory.parts
                or type(self.image_sha256) is not str or not re.fullmatch('[0-9a-f]{64}', self.image_sha256)
                or type(self.authority) is not TrustedBriefAuthority
                or type(self.model_transport) not in (OpenAIReadOnlyTransport, DeepSeekReadOnlyTransport)
                or type(self.card_transport) is not CardTransport
                or type(self.generation) is not GenerationConfig
                or type(self.policy) is not RetentionPolicy
                or type(self.inventory) is not RuntimeInventoryConfig
                or type(self.owners) is not tuple or not 1 <= len(self.owners) <= 64
                or any(type(s) is not RequesterContext for s in self.owners)
                or len(set(self.owners)) != len(self.owners)
                or len({s.namespace for s in self.owners}) != 1
                or any(s.principal.platform != 'feishu' for s in self.owners)
                or not all(callable(c) for c in (self.authenticate, self.resolve_binding,
                    getattr(self.model_transport, 'generate', None),
                    getattr(self.card_transport, 'send', None), getattr(self.card_transport, 'check_ack', None)))
                or type(self.initialize_new) is not bool or type(self.maintenance) is not bool
                or (self.maintenance and self.initialize_new)):
            raise ValueError('trusted Brief composition required')
        expected = {OpenAIReadOnlyTransport: OPENAI_MODEL, DeepSeekReadOnlyTransport: DEEPSEEK_MODEL}
        if self.generation.parameters.expected_model_id != expected[type(self.model_transport)]:
            raise ValueError('model transport and generation profile must match')

    def config_identity(self, meetings):
        return hashlib.sha256(json.dumps(dict(generation=self.generation.to_dict(),
            inventory=asdict(self.inventory), directory=str(self.directory),
            owners=[(s.namespace, s.principal.platform, s.principal.actor_id) for s in self.owners],
            retention=self.policy.expires_at, meetings=meetings, maintenance=self.maintenance),
            sort_keys=True, separators=(',', ':')).encode()).hexdigest()


class _BusinessLoop:
    """Single supervised thread, retained on a stall; never report its custody zero."""
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.thread = Thread(target=self.run, name='brief-owner', daemon=True)
        try:
            self.thread.start()
        except BaseException:
            self.loop.close()
            raise

    def run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()
        self.loop.close()

    def submit(self, function, *args):
        async def run():
            value = function(*args)
            return await value if asyncio.iscoroutine(value) else value
        return asyncio.run_coroutine_threadsafe(run(), self.loop)

class BriefProductionOwner:
    def __init__(self, settings, composition):
        self._invalid_composition = (composition is not None
                                     and type(composition) is not TrustedBriefComposition)
        if self._invalid_composition:
            composition = None
        self.settings, self.composition = settings, composition
        self.lifecycle = BriefLifecycle()
        self.status = BriefStatusOwner(settings.brief_status_dir, settings.brief_control_path,
                                       lifecycle=self.lifecycle)
        self.status._production = self
        self.control_lock = asyncio.Lock()
        self._requested_generation = 0
        self.ready = False
        self.reason = 'starting'
        self.report = None
        self.persistence = None
        self.config_sha256 = None if composition is None else composition.config_identity(settings.brief_meetings_enabled)
        self._business = self._pump_task = self._startup_task = None
        self._pending = set()
        self._lock = self._directory = None
        self._identities = None
        self._stopping = False
        self._finalizer = None
        self._observation_digest = None
        self._reconciled_at = None
        self._reconciliation = None
        self._reconciliation_generation = None
        self._reconciliation_deadline = None
        self.service = self.worker = self.cards = self.retention = self.meetings = None
        self.application = BriefApplication(self)

    @property
    def health(self):
        return self.status.health if self.ready else 'degraded'

    def fail(self, reason):
        self.ready, self.reason = False, reason
        self.lifecycle.shutdown()

    async def _call(self, function, *args, timeout=0.5):
        if self._reconciliation is not None and not self._reconciliation.done():
            raise LifecycleDenied()
        future = self._business.submit(function, *args)
        self._pending.add(future)
        def finished(f):
            # Retrieve every result, including timed-out callers' exceptions.
            if not f.cancelled():
                f.exception()
            self._pending.discard(f)
        future.add_done_callback(finished)
        wrapped = asyncio.wrap_future(future)
        wrapped.add_done_callback(lambda f: f.exception() if not f.cancelled() else None)
        try:
            return await asyncio.wait_for(asyncio.shield(wrapped), timeout)
        except TimeoutError:
            self.fail('business_io_timeout')
            raise

    async def start(self):
        await self.status.start()  # lifetime status flock precedes every business IO
        if self.status.health != 'ok' or self.lifecycle.snapshot().shutdown:
            self.fail('status_unavailable')
            return
        if self.composition is None:
            self.fail('trusted_composition_invalid' if self._invalid_composition
                      else 'trusted_composition_missing')
            return
        try:
            self._business = _BusinessLoop()
            self._startup_task = asyncio.create_task(self._start_business())
        except Exception:
            self.fail('startup_unavailable')
            await self._finish_business()

    async def _start_business(self):
        # The application retains this task; Monitor must not await database
        # reconciliation before yielding its lifespan. Intake stays sealed.
        try:
            # Preserve the status-process/IPC deadlines. Construction gets its own
            # finite overhead plus the explicitly configured two-pass scan budget.
            await self._call(self._construct, timeout=5 + self._reconciliation_budget())
            if self._stopping:
                return
            if self.lifecycle.snapshot().shutdown or self.status.health != 'ok':
                raise LifecycleDenied()
            if self.report.ready:
                self.ready, self.reason = True, 'ready'
                if not self.composition.maintenance:
                    self._pump_task = asyncio.create_task(self._pump())
        except asyncio.CancelledError:
            self.fail('startup_cancelled')
            await self._finish_business()
            raise
        except Exception:
            self.fail('startup_unavailable')
            await self._finish_business()

    def _paths(self):
        return {kind: self.composition.directory / (kind + '.db') for kind in KINDS}

    def _check_files(self):
        if self._directory is None or self._lock is None:
            raise ValueError('persistence_owner_missing')
        directory = _directory(self.composition.directory)
        try:
            info = os.fstat(directory)
            if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
                raise ValueError('persistence_permissions')
            if (os.fstat(directory).st_dev, os.fstat(directory).st_ino) != (
                    os.fstat(self._directory).st_dev, os.fstat(self._directory).st_ino):
                raise ValueError('persistence_replaced')
            lock = _entry(directory, 'owner.lock')
            if (lock.st_ino != os.fstat(self._lock).st_ino or lock.st_uid != os.geteuid()
                    or stat.S_IMODE(lock.st_mode) != 0o600):
                raise ValueError('persistence_replaced')
            for path in self._paths().values():
                info = _entry(directory, path.name)
                if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
                    raise ValueError('persistence_permissions')
                for suffix in ('-wal', '-shm', '-journal'):
                    if os.path.lexists(str(path) + suffix):
                        raise ValueError('persistence_recovery_required')
            actual = {}
            for kind, cls in zip(KINDS, (BriefStore, CardLedger, RetentionLedger, RevisionLedger, CallbackLedger)):
                # Identity-only handle: constructors may write/migrate and are forbidden here.
                handle = object.__new__(cls)
                handle.path = self._paths()[kind]
                actual[kind] = _identity(handle)
            if self._identities is not None and actual != self._identities:
                raise ValueError('persistence_replaced')
            return actual
        finally:
            os.close(directory)

    def _reconcile(self):
        identities = self._check_files()
        targets = dict(primary=identities['brief'], cards=identities['card'],
                       callbacks=identities['callback'], revisions=identities['revision'])
        result = reconcile_startup(**{k + '_path': p for k, p in self._paths().items()},
            generation_config=self.composition.generation,
            config=self.composition.inventory, owners=self.composition.owners,
            boot_id=self.status.boot_id, config_sha256=self.config_sha256,
            observed_us=_micros(self.composition.authority.clock()), target_identities=targets)
        self._check_files()
        return replace(result, persistence=hashlib.sha256(json.dumps(
            [result.persistence, identities], sort_keys=True).encode()).hexdigest())

    def _construct(self):
        try:
            return self._construct_owned()
        except BaseException:
            self._close()
            raise

    def _construct_owned(self):
        c = self.composition
        self._directory = _directory(c.directory)
        for parent in c.directory.parents:
            info = parent.stat(follow_symlinks=False)
            if info.st_uid not in (0, os.geteuid()) or (stat.S_IMODE(info.st_mode) & 0o022
                    and not (info.st_uid == 0 and info.st_mode & stat.S_ISVTX)):
                raise ValueError('protected_ancestors_required')
        info = os.fstat(self._directory)
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise ValueError('protected_persistence_required')
        try:
            _entry(self._directory, 'owner.lock')
        except FileNotFoundError:
            pass
        self._lock = os.open('owner.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC,
                             0o600, dir_fd=self._directory)
        info = _regular(self._lock)
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise ValueError('protected_persistence_required')
        fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # These are the host's bounded current local mirrors, never an event's
        # claimed ACLs or a mutable JSON file promoted to authority.
        if not c.maintenance:
            from ostag.briefs.feishu_transport import CredentialSnapshot
            binding = c.resolve_binding()
            credentials = c.card_transport._resolve()
            c.authority.settings()
            if (type(binding) is not FeishuBinding or type(credentials) is not CredentialSnapshot
                    or credentials.binding != binding):
                raise LifecycleDenied()
            for scope in c.owners:
                destination = c.authority.destination(scope)
                if destination is None or dict(credentials.private_owners).get(destination.address) != scope.principal:
                    raise LifecycleDenied()
        paths = self._paths()
        # A missing main database does not make surviving recovery evidence a
        # new store. Reject before reserving files or invoking any schema API.
        # SQLite must never get a chance to recover/rewrite an orphan sidecar.
        for path in paths.values():
            if any(os.path.lexists(str(path) + suffix) for suffix in ('-wal', '-shm', '-journal')):
                raise ValueError('persistence_recovery_required')
        existing = [os.path.lexists(p) for p in paths.values()]
        if not any(existing) and c.initialize_new and not c.maintenance:
            if self.lifecycle.snapshot().shutdown:
                raise LifecycleDenied()
            # O_EXCL reserves each permitted new file under the lifetime owner.
            # A failed partial install stays a blocker; no implicit repair/retry.
            for path in paths.values():
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
                os.close(fd)
            BriefStore(paths['brief'], busy_timeout=0.05).enable_delivery()
            for cls, kind in ((CardLedger, 'card'), (RetentionLedger, 'retention'),
                              (RevisionLedger, 'revision'), (CallbackLedger, 'callback')):
                kw = dict(capacity=getattr(c.inventory, kind + '_capacity'))
                if kind == 'revision':
                    kw.update(chain_limit=c.inventory.revision_chain_limit, event_limit=c.inventory.revision_event_limit)
                cls(paths[kind], **kw).initialize()
        self._identities = self._check_files()
        self.report = self._reconcile()
        self.persistence = self.report.persistence
        if not self.report.ready:
            self.reason = 'startup_quarantined'
            return
        if c.maintenance:
            return
        if self.lifecycle.snapshot().shutdown:
            raise LifecycleDenied()
        # Preflight proved exact existing schemas. Constructors cannot migrate here.
        store = BriefStore(paths['brief'], busy_timeout=0.05)
        card = CardLedger(paths['card'], capacity=c.inventory.card_capacity)
        callbacks = CallbackLedger(paths['callback'], capacity=c.inventory.callback_capacity)
        revision = RevisionLedger(paths['revision'], capacity=c.inventory.revision_capacity,
            chain_limit=c.inventory.revision_chain_limit, event_limit=c.inventory.revision_event_limit)
        self.retention = RetentionCoordinator(ledger=RetentionLedger(paths['retention'],
            capacity=c.inventory.retention_capacity), store=store, clock=c.authority.clock,
            cards=card, callbacks=callbacks, revisions=revision, lifecycle=self.lifecycle)
        self.service = c.authority.create_service(store=store, transport=c.model_transport,
            config=c.generation, retention=self.retention.guard(store), lifecycle=self.lifecycle)
        self.meetings = self.service.meeting_application(policy=c.policy)
        self.cards = CardDeliveryService(ledger=card, namespace=c.owners[0].namespace,
            load=self._preview, resolve_binding=c.resolve_binding, authorize=self._authorize_preview,
            transport=c.card_transport, clock=lambda: _micros(c.authority.clock()) / 1000000,
            retention=self.retention.guard(card), lifecycle=self.lifecycle)
        if self.lifecycle.snapshot().shutdown:
            raise LifecycleDenied()
        self.worker = BriefWorker(self.service, supervised=True)

    def _authorize_preview(self, preview):
        op = self.lifecycle.begin('intake')
        try:
            job = self.service.store.get(preview.scope, preview.job_id)
            request, _ = self.service.load_job(job)
            self.service.authorize_current(request, job.scope, job.destination,
                stopped=lambda: self._stopping, job_id=job.job_id, operation=op)
        finally:
            op.finish()

    def _preview(self, scope, job_id):
        op = self.lifecycle.begin('intake')
        try:
            job = self.service.store.get(scope, job_id)
            if job is None or job.state != 'draft_ready':
                raise LifecycleDenied()
            request, _ = self.service.load_job(job)
            box = self.service.store.outbox(scope, job_id)
            if box is None or box.state != 'pending':
                raise LifecycleDenied()
            preview = PrivatePreview.from_outbox(scope, box, self.composition.resolve_binding(),
                request.source_set(scope), links=request.links if type(request) is DurableMeetingRequest else ())
            self.service.authorize_current(request, scope, job.destination,
                stopped=lambda: self._stopping, job_id=job_id, operation=op)
            return preview
        finally:
            op.finish()

    async def _cycle(self):
        self._check_files()
        outcome = await asyncio.wrap_future(self.worker.run_once())
        if self.worker.quarantined and self.lifecycle.snapshot().uncertain:
            self.ready, self.reason = False, 'remote_effect_quarantined'
            # Preserve the status/control observer, current generation and
            # unknown custody. OFF must still be observed; ON cannot reopen.
            raise LifecycleDenied()
        if outcome == 'store_unavailable' or self.worker.quarantined:
            raise RuntimeError('worker_unavailable')
        if outcome in ('unavailable', 'stopped', 'busy'):
            # A sampled ON is not authority until its publication ACK. The
            # worker may correctly reject that interval (or a concurrent OFF);
            # do not start a cleanup/card cycle after its refusal.
            return
        op = self.lifecycle.begin('cleanup')
        try:
            with sqlite3.connect(self._paths()['brief'].as_uri() + '?mode=ro', uri=True, timeout=0) as db:
                rows = db.execute('SELECT job_id,namespace,platform,actor_id FROM brief_jobs LIMIT ?',
                    (self.composition.inventory.brief_budget.row_limit + 1,)).fetchall()
            if len(rows) > self.composition.inventory.brief_budget.row_limit:
                raise RuntimeError('capacity')
            for job_id, namespace, platform, actor in rows:
                op.check()
                scope = next(s for s in self.composition.owners if
                    (s.namespace, s.principal.platform, s.principal.actor_id) == (namespace, platform, actor))
                state = self.retention.status(scope, job_id)
                if state.decision != 'authorized':
                    self.lifecycle.cleanup(self.retention, scope, job_id)
                    continue
                job = self.service.store.get(scope, job_id)
                if job.state == 'draft_ready':
                    request, _ = self.service.load_job(job)
                    if type(request) is DurableMeetingRequest:
                        register_retention(self.service, job)
                    else:
                        self.retention.register(scope, job_id, policy=self.composition.policy)
                    box = self.service.store.outbox(scope, job_id)
                    await self.cards.deliver(scope, job_id, box.revision.revision_id)
        finally:
            op.finish()

    async def _pump(self):
        try:
            while not self._stopping and self.ready:
                snapshot = self.lifecycle.snapshot()
                if snapshot.shutdown:
                    self.ready, self.reason = False, 'status_unavailable'
                    break
                if snapshot.control_valid and snapshot.admission == 'on' and not snapshot.uncertain:
                    try:
                        # Actual work owns its existing production deadlines; do not
                        # impose a short IO timeout on a legitimate model invocation.
                        await self._call(self._cycle, timeout=self.composition.generation.parameters.budget.timeout_seconds + 35)
                    except LifecycleDenied:
                        pass
                await asyncio.sleep(0.05)
        except Exception:
            self.fail('business_unavailable')

    def control_identity(self):
        return dict(image_sha256=self.composition.image_sha256,
                    config_sha256=self.config_sha256, persistence=self.persistence)

    async def request_control(self, *, boot_id, generation, admission, maintenance=False,
                              image_sha256, config_sha256, persistence):
        """Trusted host/operator API, deliberately absent from HTTP/application routes.

        Serialize with publication. File replacements by an external operator are
        still validated, but a replacement mid-publication may fail closed.
        """
        if (boot_id != self.status.boot_id or type(generation) is not int or not 0 < generation < 2**63
                or admission not in ('on', 'off') or type(maintenance) is not bool
                or (maintenance and admission != 'off')
                or (not self.ready and admission != 'off') or self.report is None
                or dict(image_sha256=image_sha256, config_sha256=config_sha256,
                        persistence=persistence) != self.control_identity()):
            raise LifecycleDenied()
        async with self.control_lock:
            snapshot = self.lifecycle.snapshot()
            if self._startup_task is not None and not self._startup_task.done():
                # Intake is already sealed. Do not queue a control write behind
                # construction and misclassify its wait as a 500ms IO failure.
                raise LifecycleDenied()
            if self._reconciliation is not None:
                raise LifecycleDenied()
            if generation <= max(self._requested_generation, snapshot.control_generation or 0):
                raise LifecycleDenied()
            # Invalidate the preceding drain observation immediately for ON too.
            self.lifecycle._file_close(self.status)
            # Reserve at submission, not the later sampler ACK. Back-to-back
            # operator writes must not reuse an as-yet unobserved generation.
            self._requested_generation = generation
            value = dict(version=2, boot_id=boot_id, generation=generation, admission=admission,
                         maintenance=maintenance, **self.control_identity())
            def write():
                path = self.status.control_path
                directory = _directory(path.parent)
                name = '.brief-control-' + self.status.boot_id
                fd = None
                try:
                    info = os.fstat(directory)
                    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077:
                        raise ValueError('protected_control_required')
                    try:
                        old = _entry(directory, path.name)
                        if old.st_uid != os.geteuid() or stat.S_IMODE(old.st_mode) != 0o600:
                            raise ValueError('protected_control_required')
                    except FileNotFoundError:
                        pass
                    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                 0o600, dir_fd=directory)
                    raw = json.dumps(value).encode()
                    if os.write(fd, raw) != len(raw):
                        raise OSError('control_write')
                    os.fsync(fd)
                    os.replace(name, path.name, src_dir_fd=directory, dst_dir_fd=directory)
                finally:
                    if fd is not None:
                        os.close(fd)
                        try:
                            os.unlink(name, dir_fd=directory)
                        except FileNotFoundError:
                            pass
                    os.close(directory)
            await self._call(write)

    def can_open(self, control):
        return (self.report is not None and
                (control['admission'] == 'off' or (self.ready and self.report.ready and self._reconciliation is None
                 and not self.lifecycle.snapshot().uncertain))
                and all(control.get(k) == v for k, v in self.control_identity().items())
                and not self._stopping and (not self.composition.maintenance or
                    (control['admission'] == 'off' and control['maintenance'])))

    def _reconciliation_budget(self):
        # Structural inventory and cross-store reconciliation each visit all five
        # stores. Their configured budgets are independent of the IPC deadline.
        return 0.5 + 2 * sum(getattr(self.composition.inventory, k + '_budget').budget_ms
                             for k in KINDS) / 1000

    async def evidence(self, snapshot):
        current = False
        eligible = (self.ready and not self._stopping and snapshot.drained
                    and not snapshot.uncertain and not self._pending
                    and self._business is not None)
        reason = self.reason
        future = self._reconciliation
        if future is not None:
            if future.done():
                self._reconciliation = None
                try:
                    report, finished = future.result()
                    if finished > self._reconciliation_deadline:
                        self.ready, self.reason = False, 'reconciliation_timeout'
                    elif not report.ready or report.persistence != self.persistence:
                        self.ready, self.reason = False, 'reconciliation_failed'
                    elif eligible and snapshot.control_generation == self._reconciliation_generation:
                        # Completion is a fresh observation, not an indefinitely
                        # reusable certificate. Stale completions trigger a new scan.
                        if 0 <= monotonic() - finished <= 0.75:
                            self.report, current = report, True
                            self._reconciled_at = finished
                except Exception:
                    self.ready, self.reason = False, 'reconciliation_failed'
                reason = self.reason
            elif monotonic() > self._reconciliation_deadline:
                # Keep the observer alive and the future/lock in custody. A scan
                # that exceeds its total budget cannot grant drain or reopen ON.
                self.ready, self.reason = False, 'reconciliation_timeout'
                reason = self.reason
            else:
                reason = 'reconciliation_pending'
        if eligible and self.ready and not current and self._reconciliation is None:
            self._reconciled_at = None
            self._reconciliation_generation = snapshot.control_generation
            self._reconciliation_deadline = monotonic() + self._reconciliation_budget()
            def scan():
                return self._reconcile(), monotonic()
            self._reconciliation = self._business.submit(scan)
            self._reconciliation.add_done_callback(
                lambda f: f.exception() if not f.cancelled() else None)
            reason = 'reconciliation_pending'
        report = self.report
        return dict(image_sha256=None if self.composition is None else self.composition.image_sha256,
            config_sha256=self.config_sha256, persistence=self.persistence,
            reconciliation=None if report is None else report.binding,
            reconciled=current, ready=self.ready, active_leases=None if report is None else report.active_leases,
            unknown_effects=None if report is None else report.unknown_effects,
            reason=reason)

    async def challenge_drain(self, *, boot_id, generation, image_sha256,
                              config_sha256, persistence):
        """Atomic point-in-time host challenge; not a lease or saved certificate.

        The caller may poll while a scan is pending. Sampling and validation share
        the same lock as publication and control changes, with no intervening IO.
        """
        async with self.control_lock:
            return validate_production_drain(self, self.status.last_status,
                boot_id=boot_id, generation=generation, image_sha256=image_sha256,
                config_sha256=config_sha256, persistence=persistence)

    def record_observation(self, value):
        # Seal the actual acknowledged IPC result; mutating the public JSON
        # dictionary cannot turn an observation into a different drain proof.
        self._observation_digest = hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()

    async def stop(self):
        self._stopping = True
        self.lifecycle.shutdown()
        if self._startup_task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(self._startup_task), 0.5)
            except asyncio.CancelledError:
                if not self._startup_task.cancelled():
                    raise
            except TimeoutError:
                self.reason = 'shutdown_pending'
        if self.cards is not None:
            self.cards.stop()
        if self.worker is not None:
            await asyncio.to_thread(self.worker.stop)
        if self._pump_task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(self._pump_task), 1)
            except TimeoutError:
                self.reason = 'shutdown_pending'
        await self._finish_business()
        await self.status.stop()
        self.ready = False

    async def _finish_business(self):
        if self._business is None:
            return
        if self._finalizer is None:
            self._finalizer = self._business.submit(self._finalize)
            self._finalizer.add_done_callback(lambda f: f.exception() if not f.cancelled() else None)
        try:
            await asyncio.wait_for(asyncio.shield(asyncio.wrap_future(self._finalizer)), 0.5)
            await asyncio.to_thread(self._business.thread.join, 0.5)
        except TimeoutError:
            # The supervised finalizer retains the owner/lock and continues on
            # its own loop after FastAPI exits. It closes only on actual completion.
            self.reason = 'shutdown_pending'

    async def _finalize(self):
        # This runs on the business loop after construction. A worker may have
        # been created after stop() sampled its fields; always seal it here too.
        if self.cards is not None:
            self.cards.stop()
        if self.worker is not None:
            self.worker.stop()
        if self.service is not None:
            self.service.close()
        while True:
            pending = asyncio.all_tasks() - {asyncio.current_task()}
            if pending:
                done, _ = await asyncio.wait(pending, timeout=0.05)
                for task in done:
                    if not task.cancelled():
                        task.exception()
                continue
            if self.worker is not None and (self.worker._thread.is_alive() or self.worker._notifier.is_alive()):
                await asyncio.sleep(0.05)
                continue
            break
        self._close()
        self._business.loop.call_soon(self._business.loop.stop)

    def _close(self):
        if self.service is not None:
            self.service.close()
            self.service._executor.shutdown(wait=True)
        if self.worker is not None and self.worker._thread.is_alive():
            self.reason = 'shutdown_pending'
            return
        for name in ('_lock', '_directory'):
            fd = getattr(self, name)
            if fd is not None:
                os.close(fd)
                setattr(self, name, None)


class BriefApplication:
    """Server-owned async surface for a verified adapter; no HTTP/self-grant route."""
    def __init__(self, owner):
        self.owner = owner

    async def _invoke(self, context, action, *args):
        o = self.owner
        if not o.ready or o.service is None or o._stopping:
            raise LifecycleDenied()
        def call():
            op = o.lifecycle.begin('intake')
            try:
                scope = o.composition.authenticate(context)
                if type(scope) is not RequesterContext or scope not in o.composition.owners:
                    raise LifecycleDenied()
                result = action(scope, *args)
                if o.composition.authenticate(context) != scope:
                    raise LifecycleDenied()
                op.check()
                return result
            finally:
                op.finish()
        try:
            return await o._call(call)
        except Exception:
            raise LifecycleDenied() from None

    async def submit(self, context, event):
        def submit(scope, event):
            if type(event) is not NormalizedEvent or event.principal != scope.principal:
                raise LifecycleDenied()
            from ostag.briefs.service import ManualRequest
            from ostag.briefs.validation import strict_json
            req = ManualRequest.from_dict(strict_json(event.text.split(maxsplit=1)[1], 1048576))
            if req.namespace != scope.namespace:
                raise LifecycleDenied()
            result = self.owner.service.submit(event)
            if result.job_id and result.status != 'cancelled':
                self.owner.retention.register(scope, result.job_id, policy=self.owner.composition.policy)
            return result
        return await self._invoke(context, submit, event)

    async def submit_meeting(self, context, event_id, request):
        if not self.owner.settings.brief_meetings_enabled or self.owner.meetings is None:
            raise LifecycleDenied()
        return await self._invoke(context, self.owner.meetings.submit, event_id, request)

    async def status(self, context, job_id):
        return await self._invoke(context, self._read, job_id, False)

    async def private_export(self, context, job_id):
        return await self._invoke(context, self._read, job_id, True)

    def _read(self, scope, job_id, export):
        o = self.owner
        job = o.service.store.get(scope, job_id)
        request, _ = o.service.load_job(job)
        if type(request) is DurableMeetingRequest:
            return (o.meetings.private_export if export else o.meetings.status)(scope, job_id)
        o.service.authorize_current(request, scope, job.destination,
            stopped=lambda: o._stopping, job_id=job_id)
        result = render_private(o._preview(scope, job_id)) if export else BriefStatus(job.state, job_id)
        o.service.authorize_current(request, scope, job.destination,
            stopped=lambda: o._stopping, job_id=job_id)
        return result

    async def cancel(self, context, job_id):
        if self.owner.service is None:
            raise LifecycleDenied()
        return await self._invoke(context, self.owner.service.cancel, job_id)


def validate_production_drain(owner, status, *, boot_id, generation, image_sha256,
                              config_sha256, persistence):
    """Current owner observation, never a detached caller-supplied ready boolean.

    A filesystem preflight/installer is NOT installed by this API. Consumers need
    a live challenge against this owner; saved JSON alone cannot prove freshness.
    """
    if type(owner) is not BriefProductionOwner or type(status) is not dict:
        return False
    try:
        digest = hashlib.sha256(json.dumps(status, sort_keys=True).encode()).hexdigest()
    except (TypeError, ValueError):
        return False
    snapshot = owner.lifecycle.snapshot()
    return bool(owner.ready and owner.status.health == 'ok' and status is owner.status.last_status
        and digest == owner._observation_digest
        and status.get('version') == 3 and status.get('drain_ack') is True
        and snapshot.drained and not snapshot.uncertain and not owner._pending
        and owner._reconciliation is None and owner._reconciled_at is not None
        and 0 <= monotonic() - owner._reconciled_at <= 0.75
        and owner.report is not None
        and status['production']['reconciliation'] == owner.report.binding
        and status.get('boot_id') == boot_id == owner.status.boot_id
        and type(generation) is int and status.get('control_generation') == generation == snapshot.control_generation
        and status['production']['image_sha256'] == image_sha256 == owner.composition.image_sha256
        and status['production']['config_sha256'] == config_sha256 == owner.config_sha256
        and status['production']['persistence'] == persistence == owner.persistence
        and 0 <= time() - status['updated_at'] <= 0.75)
