"""U02 ephemeral authority for one fresh approval-to-executor invocation.

A serialized ActionReceipt, including one in applying state, is not a capability.
Only the ApprovalService path that just durably crossed into applying installs
this context. The journal must claim it once and check it at its commit/IO gates.
This is a trusted in-process boundary, not a network credential or provider grant.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from dataclasses import dataclass
import re
import hashlib
import json
from threading import Lock
from time import monotonic
from uuid import uuid4

from ostag.core.frozen_custody import deny_retired
from ostag.core.identity import Principal
from ostag.core.schemas import ActionReceipt, SchemaError

_CURRENT = ContextVar('ostag_effect_invocation', default=None)
_LIFETIME_SECONDS = 30


def _now():
    return datetime.now(UTC)


def _denied():
    return SchemaError('effect_authority_denied', 'Current execution authority is unavailable.',
                       'Reconcile the original action; do not replay an applying receipt.')


def _receipt_digest(receipt):
    deny_retired(receipt)
    if (type(receipt) is not ActionReceipt or receipt.state != 'applying'
            or not receipt.action_id.startswith('action_frozen')
            or receipt.target_system != 'brief_field_change'
            or type(receipt.principal) is not Principal):
        raise _denied()
    try:
        wire = json.dumps(receipt.to_dict(), ensure_ascii=True, sort_keys=True, allow_nan=False)
    except (ValueError, TypeError, RecursionError):
        raise _denied() from None
    return hashlib.sha256(wire.encode()).hexdigest()


@dataclass(frozen=True, repr=False)
class PendingEffect:
    """An observation awaiting verification, never permission or applied proof."""
    action_id: str
    attempt_id: str
    evidence_sha256: str

    def __post_init__(self):
        if (type(self.action_id) is not str or not re.fullmatch('action_frozen[0-9a-f]{64}', self.action_id)
                or type(self.attempt_id) is not str or not re.fullmatch('attempt_[0-9a-f]{32}', self.attempt_id)
                or type(self.evidence_sha256) is not str or not re.fullmatch('[0-9a-f]{64}', self.evidence_sha256)):
            raise _denied()

    def to_dict(self):
        return dict(action_id=self.action_id, attempt_id=self.attempt_id,
                    evidence_sha256=self.evidence_sha256)


class _Invocation:
    """Issuer-owned live state. No from_dict(), token deserializer or public mint."""
    __slots__ = ('_ledger', '_digest', '_action', '_principal', '_nonce', '_issued',
                 '_expires', '_deadline', '_last', '_active', '_claimed', '_lock')

    def __init__(self, ledger, receipt):
        self._digest = _receipt_digest(receipt)
        self._ledger = ledger
        self._action, self._principal = receipt.action_id, receipt.principal
        self._nonce = 'attempt_' + uuid4().hex
        self._issued = _now()
        self._expires = self._issued + timedelta(seconds=_LIFETIME_SECONDS)
        self._deadline = monotonic() + _LIFETIME_SECONDS
        self._last = self._issued
        self._active, self._claimed = True, False
        self._lock = Lock()

    @property
    def action_id(self):
        return self._action

    @property
    def principal(self):
        return self._principal

    @property
    def attempt_id(self):
        return self._nonce

    @property
    def receipt_digest(self):
        return self._digest

    @property
    def issued_at(self):
        return self._issued

    @property
    def expires_at(self):
        return self._expires

    def _check_locked(self):
        now = _now()
        if (not self._active or now < self._last or now >= self._expires
                or monotonic() >= self._deadline):
            self._active = False
            raise _denied()
        self._last = now

    def checkpoint(self):
        with self._lock:
            self._check_locked()
            if not self._claimed:
                raise _denied()

    def claim(self, ledger, receipt):
        with self._lock:
            self._check_locked()
            if (self._claimed or ledger is not self._ledger
                    or _receipt_digest(receipt) != self._digest):
                raise _denied()
            self._claimed = True
        return self

    def close(self):
        with self._lock:
            self._active = False


@contextmanager
def _authorized_apply_invocation(ledger, receipt):
    """ApprovalService-only issuer, AFTER its durable applying transition."""
    invocation = _Invocation(ledger, receipt)
    handle = _CURRENT.set(invocation)
    try:
        yield invocation
    finally:
        invocation.close()
        _CURRENT.reset(handle)


def claim_apply_invocation(ledger, receipt):
    """Journal entry gate: a current exact receipt and ledger instance, once only."""
    invocation = _CURRENT.get()
    if type(invocation) is not _Invocation:
        raise _denied()
    return invocation.claim(ledger, receipt)
