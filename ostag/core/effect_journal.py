"""U02 central Brief intent/begin journal; observations never imply success.

Only a fresh ApprovalService invocation can create an attempt. The same original
resource is permanently fenced while any result is unresolved, independent of
job, principal, payload, action id or new attempt. B03 must implement evidence-
based settlement before that custody can be released. No provider is installed.
"""
from dataclasses import dataclass
import hashlib
import json
from threading import Lock

from ostag.briefs.actions import ActionProposalService, FrozenFieldChange
from ostag.briefs.store import BriefStore, WorkerContext, EFFECT_SCHEMA_VERSION, _micros
from ostag.core import effects
from ostag.core.effects import PendingEffect, claim_apply_invocation, _denied
from ostag.core.schemas import ActionReceipt, SchemaError
from ostag.core.frozen_custody import RetiredCustody, is_retired


def _json(value):
    raw = json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False,
                     separators=(',', ':'))
    if len(raw.encode()) > 131072:
        raise _denied()
    return raw


def _digest(raw):
    return hashlib.sha256(raw.encode()).hexdigest()


def _identity(receipt):
    value = receipt.to_dict()
    value.pop('created_at', None)
    value.pop('updated_at', None)
    return _json(value)


def _resource(change):
    obs = change.to_dict()['observation']
    resource = obs['resource']
    # The physical resource fence intentionally excludes job, actor, attempt,
    # desired value and namespace: none may reset an unknown external effect.
    return _digest(_json([resource['platform'], resource['base_id'], resource['table_id'],
                          obs['record_id'], obs['field_id']]))


@dataclass(frozen=True, repr=False)
class EffectPermit:
    _json: str
    _invocation: object
    _dispatch: object

    def to_dict(self):
        return json.loads(self._json)

    @property
    def digest(self):
        return _digest(self._json)

    def _consume_dispatch(self):
        lock, state = self._dispatch
        with lock:
            self._invocation.checkpoint()
            if state[0]:
                raise _denied()
            state[0] = True


class EffectJournal:
    def __init__(self, *, store, proposals, worker):
        if (type(store) is not BriefStore or type(proposals) is not ActionProposalService
                or type(worker) is not WorkerContext):
            raise _denied()
        self.store, self.proposals, self.worker = store, proposals, worker

    @property
    def ledger(self):
        return self.proposals.approvals.ledger

    def _current(self, receipt):
        try:
            change = FrozenFieldChange.from_dict(json.loads(receipt.apply_payload['intent_json']))
            actual, original = self.proposals.resolve(change.observation.scope, receipt.action_id)
            if (actual.state != 'applying' or original != change
                    or _identity(actual) != _identity(receipt)
                    or change.to_dict()['ledger_epoch'] != self.ledger.frozen_epoch()):
                raise _denied()
            view = self.proposals.revisions.read(change.observation.scope, change.to_dict()['job_id'])
            return change, view.original.revision_id
        except (KeyError, ValueError, TypeError, AttributeError):
            raise _denied() from None

    def _ledger_custody(self, change, action_id):
        # Read the existing approval authority too: a restored/older SQLite
        # snapshot must not erase a different receipt's possible side effect.
        with self.ledger._exclusive(read_only=True) as handle:
            records = self.ledger._strict_records(handle)
            if self.ledger._epoch(records) != change.to_dict()['ledger_epoch']:
                raise _denied()
            entries = self.ledger._fold_receipts(records)
            for identity, entry in entries.items():
                if identity == action_id or not identity.startswith('action_frozen'):
                    continue
                receipt = entry['receipt']
                if receipt['state'] not in ('applying', 'applied', 'failed', 'blocked'):
                    continue
                if is_retired(receipt):
                    retired = RetiredCustody.from_receipt(ActionReceipt.from_dict(receipt))
                    if retired.physical_resource_sha256 == _resource(change):
                        raise _denied()
                    continue
                try:
                    old = FrozenFieldChange.from_dict(json.loads(receipt['apply_payload']['intent_json']))
                    if old.to_receipt().action_id != identity or _resource(old) == _resource(change):
                        raise _denied()
                except (KeyError, TypeError, ValueError):
                    raise _denied() from None

    def begin(self, receipt):
        invocation = claim_apply_invocation(self.ledger, receipt)
        change, source_revision = self._current(receipt)
        self._ledger_custody(change, receipt.action_id)
        value = change.to_dict()
        scope = change.observation.scope
        resource = _resource(change)
        intent = _json(value)
        with self.store._delivery_transaction() as db:
            if db.execute('PRAGMA user_version').fetchone()[0] != EFFECT_SCHEMA_VERSION:
                raise _denied()
            job = self.store._find(db, scope, value['job_id'])
            revision = db.execute('SELECT revision_id FROM brief_revisions WHERE job_id=?',
                                  (value['job_id'],)).fetchone()
            if (job is None or job['state'] != 'draft_ready' or revision is None
                    or revision[0] != source_revision):
                raise _denied()
            if db.execute('SELECT 1 FROM brief_action_intents WHERE action_id=? OR resource_key=?',
                          (receipt.action_id, resource)).fetchone():
                raise SchemaError('effect_reconciliation_required',
                    'An existing operation retains custody of this resource.',
                    'Reconcile its evidence; a new job or attempt is not a retry permit.')
            meta = db.execute('SELECT epoch,ledger_epoch,highwater FROM brief_effect_meta WHERE id=1').fetchone()
            if (meta is None or type(meta['highwater']) is not int or not 0 <= meta['highwater'] < 2**62
                    or meta['ledger_epoch'] != value['ledger_epoch']):
                raise _denied()
            invocation.checkpoint()
            now = _micros(effects._now().isoformat())
            fence = meta['highwater'] + 1
            permit = EffectPermit(_json(dict(kind='brief.field.update',
                action_id=receipt.action_id, job_id=value['job_id'],
                namespace=scope.namespace, principal=scope.principal.to_dict(),
                resource_key=resource, intent_sha256=change.digest,
                ledger_epoch=value['ledger_epoch'], storage_epoch=meta['epoch'],
                owner_id=self.worker.owner_id, attempt_id=invocation.attempt_id,
                fence=fence, receipt_sha256=invocation.receipt_digest,
                issued_at=invocation.issued_at.isoformat(), expires_at=invocation.expires_at.isoformat())),
                invocation, (Lock(), [False]))
            db.execute('INSERT INTO brief_action_intents VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                (receipt.action_id, value['job_id'], scope.namespace, scope.principal.platform,
                 scope.principal.actor_id, resource, 'brief.field.update', intent, change.digest,
                 value['ledger_epoch'], now))
            db.execute('INSERT INTO brief_effect_attempts VALUES(?,?,?,?,?,?,?,?,?,?)',
                (invocation.attempt_id, receipt.action_id, self.worker.owner_id, fence,
                 permit._json, permit.digest, 'begun', _micros(invocation.issued_at.isoformat()),
                 _micros(invocation.expires_at.isoformat()), now))
            db.execute('UPDATE brief_effect_meta SET highwater=? WHERE id=1', (fence,))
            # Final under-lock lifetime gate; no cross-store callback/reentrant
            # Brief read while SQLite excludes other readers/writers.
            invocation.checkpoint()
        # Current grants/revisions can change across stores; check again after
        # durable commit, never execute when this post-check fails. The begun
        # record remains conservative custody, not a claim that IO occurred.
        self._current(receipt)
        invocation.checkpoint()
        return permit

    def _row(self, db, permit, *, observation_only=False):
        if type(permit) is not EffectPermit:
            raise _denied()
        p = permit.to_dict()
        row = db.execute('SELECT a.*,i.namespace,i.platform,i.actor_id,i.resource_key,i.intent_json,'
            'i.intent_digest,i.job_id,i.ledger_epoch,j.state AS job_state '
            'FROM brief_effect_attempts a JOIN brief_action_intents i ON a.action_id=i.action_id '
            'JOIN brief_jobs j ON j.job_id=i.job_id WHERE a.attempt_id=?',
            (p['attempt_id'],)).fetchone()
        if (row is None or row['permit_digest'] != permit.digest
                or row['owner_id'] != self.worker.owner_id
                or [row['namespace'], row['platform'], row['actor_id']] !=
                   [p['namespace'], p['principal']['platform'], p['principal']['actor_id']]
                or row['resource_key'] != p['resource_key']
                or row['job_id'] != p['job_id'] or row['intent_digest'] != p['intent_sha256']
                or row['ledger_epoch'] != p['ledger_epoch']):
            raise _denied()
        retired = (row['intent_json'] == '{"retired":true}'
                   and row['permit_json'] == '{"retired":true}')
        # A retained exact permit digest authorizes only attribution of a hash
        # after retirement. It cannot resurrect private payload or authorize IO.
        if retired:
            if not observation_only or row['state'] != 'unknown' or row['job_state'] != 'cancelled':
                raise _denied()
        elif row['permit_json'] != permit._json:
            raise _denied()
        try:
            if not retired:
                change = FrozenFieldChange.from_dict(json.loads(row['intent_json']))
                if change.digest != p['intent_sha256'] or _resource(change) != p['resource_key']:
                    raise _denied()
            meta = db.execute('SELECT epoch,ledger_epoch,highwater FROM brief_effect_meta WHERE id=1').fetchone()
            if (row['fence'] != p['fence'] or row['action_id'] != p['action_id']
                    or meta is None or meta['epoch'] != p['storage_epoch']
                    or meta['ledger_epoch'] != p['ledger_epoch']
                    or meta['highwater'] < row['fence']):
                raise _denied()
        except (ValueError, TypeError, KeyError):
            raise _denied() from None
        return row

    def observe(self, permit, *, kind, evidence_sha256):
        if (kind not in ('transport_response', 'transport_error', 'independent_readback')
                or type(evidence_sha256) is not str or len(evidence_sha256) != 64
                or any(c not in '0123456789abcdef' for c in evidence_sha256)):
            raise _denied()
        with self.store._delivery_transaction() as db:
            row = self._row(db, permit, observation_only=True)
            ordinal = db.execute('SELECT count(*) FROM brief_effect_observations WHERE attempt_id=?',
                                 (row['attempt_id'],)).fetchone()[0] + 1
            if ordinal > 32:
                raise _denied()
            observed = _micros(effects._now().isoformat())
            previous = db.execute('SELECT max(observed_us) FROM brief_effect_observations WHERE attempt_id=?',
                                  (row['attempt_id'],)).fetchone()[0]
            if observed < max(row['begun_us'], previous or row['begun_us']):
                raise _denied()
            db.execute('INSERT INTO brief_effect_observations VALUES(?,?,?,?,?)',
                (row['attempt_id'], ordinal, kind, evidence_sha256, observed))
            # A response/readback hash is evidence, not an assertion of success
            # or provider absence. Only B03 reconciliation may release custody.
            db.execute("UPDATE brief_effect_attempts SET state='unknown' WHERE attempt_id=?",
                       (row['attempt_id'],))

    async def dispatch(self, receipt, operation):
        """Trusted B02 adapter seam. The return value is NOT an applied receipt."""
        if not callable(operation):
            raise _denied()
        permit = self.begin(receipt)
        with self.store._delivery_transaction(write=False) as db:
            if self._row(db, permit)['state'] != 'begun':
                raise _denied()
        self._current(receipt)
        permit._consume_dispatch()
        try:
            result = await operation(permit)
        except BaseException:
            try:
                self.observe(permit, kind='transport_error', evidence_sha256=_digest('transport_failed'))
            except Exception:
                pass  # begun already retains custody; preserve original failure
            raise
        evidence = _digest(_json(result))
        self.observe(permit, kind='transport_response', evidence_sha256=evidence)
        p = permit.to_dict()
        return PendingEffect(p['action_id'], p['attempt_id'], evidence)


class JournaledEffectExecutor:
    """Explicit host registration for the existing ApprovalService executor slot.

    operation is a trusted provider adapter, never a user/model callback. Until
    B02/B03 supply verified readback/settlement this returns PendingEffect only.
    """
    def __init__(self, journal, operation):
        if type(journal) is not EffectJournal or not callable(operation):
            raise _denied()
        self.journal, self.operation = journal, operation

    async def apply(self, receipt):
        return await self.journal.dispatch(receipt, self.operation)
