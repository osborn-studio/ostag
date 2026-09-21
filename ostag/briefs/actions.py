"""Single-field proposals from adopted Brief content, using existing approvals.

Host adapters must independently authenticate scope and supply current provider
observations, permission checks and private destinations. Input types are not
authority. No provider write, approval, effect journal or production ingress is
implemented here. The dedicated receipt target has no registered apply executor.
"""
from dataclasses import dataclass
import hashlib
import json
import re

from ostag.briefs.models import _identifier, _text, _positive_int, _invalid
from ostag.briefs.revisions import RevisionService
from ostag.briefs.store import RequesterContext, PrivateDestination
from ostag.briefs.table_quality import ResourceKey, _bounded_json
from ostag.core.approvals import ApprovalService
from ostag.core.identity import Principal
from ostag.core.run_ledger import _epoch_valid
from ostag.core.safety import redact_secrets
from ostag.core.schemas import ActionReceipt, SchemaError


def _denied():
    return SchemaError('action_proposal_denied', 'The frozen change is not currently authorized.',
                       'Confirm the current content, target and permissions again.')


def _hash(value, name):
    if type(value) is not str or not re.fullmatch('[0-9a-f]{64}', value):
        raise _invalid(name + ' must be a SHA256 digest')
    return value


def _frozen_json(value):
    raw = _bounded_json(value)
    def check(item):
        if type(item) is str and redact_secrets(item) != item:
            raise _invalid('secret-like content cannot enter a frozen proposal')
        if type(item) is dict:
            for key, cell in item.items():
                check(key)
                check(cell)
        elif type(item) is list:
            for cell in item:
                check(cell)
    check(json.loads(raw))
    return raw


@dataclass(frozen=True, init=False, repr=False)
class FieldObservation:
    """Detached provider observation; content SHA is NEVER a provider version/CAS."""
    _json: str

    def __init__(self, *, scope, resource, record_id, field_id, value,
                 provider_version, snapshot_sha256, grant_id, grant_revision):
        if (type(scope) is not RequesterContext or type(resource) is not ResourceKey
                or resource.namespace != scope.namespace
                or resource.platform != scope.principal.platform
                or resource.platform != 'feishu'):
            raise _invalid('matching requester and original Feishu resource required')
        _identifier(record_id, 'record_id')
        _text(field_id, 'field_id')
        _text(provider_version, 'provider_version')
        if any(ord(c) < 32 for c in field_id + provider_version):
            raise _invalid('field/version must not contain control characters')
        _hash(snapshot_sha256, 'snapshot_sha256')
        _identifier(grant_id, 'grant_id')
        _positive_int(grant_revision, 'grant_revision')
        raw = _frozen_json(dict(namespace=scope.namespace, principal=scope.principal.to_dict(),
            resource=resource.to_dict(), record_id=record_id, field_id=field_id, value=value,
            provider_version=provider_version, snapshot_sha256=snapshot_sha256,
            grant_id=grant_id, grant_revision=grant_revision))
        object.__setattr__(self, '_json', raw)

    def to_dict(self):
        return json.loads(self._json)

    @property
    def scope(self):
        d = self.to_dict()
        return RequesterContext(d['namespace'], Principal.from_dict(d['principal']))

    @classmethod
    def from_dict(cls, data):
        try:
            d = dict(data)
            scope = RequesterContext(d.pop('namespace'), Principal.from_dict(d.pop('principal')))
            return cls(scope=scope, resource=ResourceKey(**d.pop('resource')), **d)
        except (TypeError, ValueError, KeyError, AttributeError):
            raise _invalid('invalid field observation') from None


@dataclass(frozen=True, init=False, repr=False)
class FrozenFieldChange:
    _json: str

    def __init__(self, *, observation, new_value, job_id, revision_id, root_digest, run_id, proposal_id, ledger_epoch='initial'):
        if type(observation) is not FieldObservation:
            raise _invalid('typed field observation required')
        for name, value in [('job_id', job_id), ('revision_id', revision_id), ('run_id', run_id),
                            ('proposal_id', proposal_id)]:
            _identifier(value, name)
        if not re.fullmatch(r'run_[A-Za-z0-9]+', run_id):
            raise _invalid('run_id must retain the existing receipt command format')
        _hash(root_digest, 'root_digest')
        if not _epoch_valid(ledger_epoch):
            raise _invalid('invalid ledger recovery epoch')
        raw = _frozen_json(dict(observation=observation.to_dict(), new_value=new_value,
                               job_id=job_id, revision_id=revision_id,
                               root_digest=root_digest, run_id=run_id, proposal_id=proposal_id,
                               ledger_epoch=ledger_epoch))
        object.__setattr__(self, '_json', raw)

    def to_dict(self):
        return json.loads(self._json)

    @property
    def observation(self):
        return FieldObservation.from_dict(self.to_dict()['observation'])

    @property
    def digest(self):
        return hashlib.sha256(self._json.encode()).hexdigest()

    @property
    def proposal_scope(self):
        """Stable host/UI logical action slot, not a target or payload digest."""
        d = self.to_dict()
        obs = d['observation']
        raw = json.dumps([obs['namespace'], obs['principal'], d['job_id'], d['proposal_id']],
                         sort_keys=True, ensure_ascii=True)
        return hashlib.sha256(raw.encode()).hexdigest()

    @classmethod
    def from_dict(cls, data):
        try:
            d = dict(data)
            return cls(observation=FieldObservation.from_dict(d.pop('observation')), **d)
        except (TypeError, ValueError, KeyError, AttributeError):
            raise _invalid('invalid frozen field change') from None

    def to_receipt(self):
        d = self.to_dict()
        obs = d['observation']
        target = [obs['resource']['base_id'], obs['resource']['table_id'],
                  obs['record_id'], obs['field_id']]
        visible = json.dumps(dict(field=obs['field_id'], old=obs['value'], new=d['new_value'],
                                  provider_version=obs['provider_version']), ensure_ascii=True,
                             sort_keys=True)
        return ActionReceipt(action_id='action_frozen' + self.digest, run_id=d['run_id'],
            title='Review one frozen field change', target_system='brief_field_change',
            target_object=json.dumps(target, ensure_ascii=True), mutation_type='update',
            principal=self.observation.scope.principal, state='needs_approval',
            required_permissions=('propose', 'write'), summary=visible, diff=visible,
            apply_payload={'kind': 'brief-field-change-v1', 'intent_json': self._json,
                           'intent_sha256': self.digest, 'ledger_epoch': d['ledger_epoch']})


class ActionProposalService:
    """Trusted local entry point. Adoption never authorizes a business mutation.

    observe/authorize/destination must be current independently verified host
    adapters, not mappings manufactured from model output or the submitted change.
    Cross-store observations are not an atomic grant; B02 must check again at apply.
    """
    def __init__(self, *, revisions, approvals, observe, authorize, destination):
        if (type(revisions) is not RevisionService or type(approvals) is not ApprovalService
                or not all(callable(f) for f in (observe, authorize, destination))):
            raise _invalid('existing revision/approval services and trusted adapters required')
        self.revisions, self.approvals = revisions, approvals
        self._observe, self._authorize, self._destination = observe, authorize, destination

    def _current(self, scope, change):
        if (type(scope) is not RequesterContext or type(change) is not FrozenFieldChange
                or scope != change.observation.scope or self._authorize(change) is not True):
            raise _denied()
        d = change.to_dict()
        view = self.revisions.read(scope, d['job_id'])
        if (view.state.head_revision_id != d['revision_id']
                or view.state.adopted_revision_id != d['revision_id']
                or view.root_digest != d['root_digest']):
            raise _denied()
        observed = self._observe(change)
        destination = self._destination(scope)
        if (type(observed) is not FieldObservation or observed != change.observation
                or type(destination) is not PrivateDestination
                or destination.principal != scope.principal
                or self._authorize(change) is not True):
            raise _denied()
        return destination

    def propose(self, scope, change, *, replaces=None):
        destination = self._current(scope, change)
        receipt = self.approvals.propose_once(change.to_receipt(), chat_id=destination.address,
            platform=scope.principal.platform, proposer_id=scope.principal.actor_id,
            proposal_scope=change.proposal_scope, replaces=replaces)
        # A concurrent withdrawal/revocation may leave an unapproved historical
        # proposal, but must not return it as current. Never undo newer decisions.
        if self._current(scope, change) != destination:
            raise _denied()
        return receipt

    def resolve(self, scope, action_id):
        """Re-read authoritative receipt and current bindings, never trust passed approval."""
        if type(scope) is not RequesterContext:
            raise _denied()
        destination = self._destination(scope)
        if type(destination) is not PrivateDestination or destination.principal != scope.principal:
            raise _denied()
        receipt = self.approvals.ledger.get_frozen_receipt(action_id, chat_id=destination.address,
                                                        platform=scope.principal.platform)
        try:
            change = FrozenFieldChange.from_dict(json.loads(receipt.apply_payload['intent_json']))
            expected = change.to_receipt().to_dict()
            actual = receipt.to_dict()
            for key in ('state', 'created_at', 'updated_at'):
                expected.pop(key, None)
                actual.pop(key, None)
            if actual != expected or self._current(scope, change) != destination:
                raise _denied()
        except (TypeError, ValueError, KeyError, AttributeError):
            raise _denied() from None
        return receipt, change
