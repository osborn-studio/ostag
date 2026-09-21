"""C01 private sales intake against one host-pinned, read-only original table.

The host supplies an authenticated RequesterContext and a reader enforcing current
platform/row visibility for that requester. Table owner cells, model text and the
returned draft are never authority. A01 grants are rechecked around every read.
No model, write, storage, report, task or permission API is invoked here. C02 owns
field confirmation, durable revisions, ActionReceipt approval and readback.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import re

from ostag.briefs.grants import authorize
from ostag.briefs.models import _identifier, _invalid, _positive_int, _text
from ostag.briefs.store import RequesterContext
from ostag.briefs.table_quality import ResourceKey, TableSnapshot, _cell_text
from ostag.core.schemas import SchemaError


def _denied():
    return SchemaError("sales_access_denied", "Private sales context is unavailable.",
                       "Recheck the authenticated requester and current original-table grant.")


def _note(value):
    _text(value, "sales note", 8192)
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise _invalid("sales note must be valid UTF-8 text") from None
    return value


def _conflict():
    return SchemaError("sales_selection_stale", "Customer selection must be refreshed.",
                       "Prepare the private note again against the current original table.")


@dataclass(frozen=True, repr=False)
class CustomerCandidate:
    record_id: str
    name: str


@dataclass(frozen=True, repr=False)
class SalesDraft:
    """Private, ephemeral matching result; never an action approval or shared report.

    Monetary/date/stage phrases stay in the original note. None means unconfirmed,
    not a claim that the source omitted them. Do not convert these into field clears.
    """
    scope: RequesterContext
    resource: ResourceKey
    observed_revision: str
    content_sha256: str
    note_sha256: str
    candidates: tuple[CustomerCandidate, ...]
    customer_id: str | None
    customer_confirmed: bool
    _note: str

    @property
    def amount(self):
        return None

    @property
    def date(self):
        return None

    @property
    def stage(self):
        return None

    @property
    def needs_confirmation(self):
        return ("amount", "date", "stage")

    @property
    def needs_customer_confirmation(self):
        return not self.customer_confirmed

    def private_note(self, scope):
        if type(scope) is not RequesterContext or scope != self.scope:
            raise _denied()
        return self._note


def _mentioned(note, label):
    # Literal names only. ASCII word boundaries prevent Alpha -> AlphaX while
    # allowing Chinese prose adjacent to either Chinese or Latin customer names.
    needle = label.casefold()
    left = r"(?<![a-z0-9_])" if needle[0].isascii() and needle[0].isalnum() else ""
    right = r"(?![a-z0-9_])" if needle[-1].isascii() and needle[-1].isalnum() else ""
    return re.search(left + re.escape(needle) + right, note.casefold()) is not None


class SalesIntake:
    def __init__(self, *, scope, resource, resource_id, grant_id, grant_revision,
                 lookup, settings, clock, reader, customer_field, aliases_field=None):
        if (type(scope) is not RequesterContext or type(resource) is not ResourceKey
                or scope.namespace != resource.namespace):
            raise _invalid("sales scope and original table must have the same namespace")
        for name, value in (("resource_id", resource_id), ("grant_id", grant_id)):
            _identifier(value, name)
        _positive_int(grant_revision, "grant_revision")
        _text(customer_field, "customer_field")
        if aliases_field is not None:
            _text(aliases_field, "aliases_field")
            if aliases_field == customer_field:
                raise _invalid("customer and alias fields must differ")
        if not all(callable(v) for v in (lookup, settings, clock, reader)):
            raise _invalid("sales intake requires current trusted read adapters")
        self.scope, self.resource, self.resource_id = scope, resource, resource_id
        self.grant_id, self.grant_revision = grant_id, grant_revision
        self.lookup, self.settings, self.clock, self.reader = lookup, settings, clock, reader
        self.customer_field, self.aliases_field = customer_field, aliases_field

    def _access(self, scope):
        if type(scope) is not RequesterContext or scope != self.scope:
            raise _denied()
        try:
            authorize(lookup=self.lookup, config=self.settings(), grant_id=self.grant_id,
                      grant_revision=self.grant_revision, namespace=scope.namespace,
                      principal=scope.principal, resource_ids=(self.resource_id,),
                      purpose="sales_followup", allowed_audience=(scope.principal,),
                      operation="read", now=self.clock())
        except Exception:
            raise _denied() from None

    def _read(self, scope):
        self._access(scope)
        try:
            snapshot = self.reader(scope, self.resource)
            if type(snapshot) is not TableSnapshot or snapshot.resource_key != self.resource:
                raise _denied()
            snapshot = TableSnapshot.from_dict(snapshot.to_dict())
        except Exception:
            raise _denied() from None
        self._access(scope)  # Revocation while the reader was running fails closed.
        return snapshot

    def _draft(self, scope, note, snapshot):
        _note(note)
        candidates = []
        for row in snapshot.records:
            fields = row.fields
            name = _cell_text(fields.get(self.customer_field))
            if not name or not name.strip() or len(name) > 256:
                continue
            labels = [name]
            aliases = fields.get(self.aliases_field) if self.aliases_field else None
            if (type(aliases) is list and len(aliases) <= 32
                    and all(type(a) is str and a.strip() and len(a) <= 256 for a in aliases)):
                labels.extend(aliases)
            if any(_mentioned(note, label) for label in labels):
                candidates.append(CustomerCandidate(row.record_id, name))
        return SalesDraft(scope, self.resource, snapshot.observed_revision, snapshot.content_sha256,
                          hashlib.sha256(note.encode()).hexdigest(), tuple(candidates),
                          candidates[0].record_id if len(candidates) == 1 else None, False, note)

    def prepare(self, scope, note):
        _note(note)
        return self._draft(scope, note, self._read(scope))

    def confirm_customer(self, scope, draft, record_id):
        self._access(scope)
        _identifier(record_id, "record_id")
        if type(draft) is not SalesDraft or draft.scope != scope or draft.resource != self.resource:
            raise _denied()
        current = self._draft(scope, draft.private_note(scope), self._read(scope))
        if (draft.observed_revision != current.observed_revision
                or draft.content_sha256 != current.content_sha256
                or draft.note_sha256 != current.note_sha256
                or draft.candidates != current.candidates
                or record_id not in {c.record_id for c in current.candidates}):
            raise _conflict()
        # This confirms only a customer choice, never critical fields or a write.
        return replace(current, customer_id=record_id, customer_confirmed=True)
