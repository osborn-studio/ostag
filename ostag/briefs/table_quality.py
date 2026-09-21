"""A08 local table analysis and independent read-port contracts.

No live Feishu connector or A08 real-data acceptance. No network, write API,
credential/ACL authority, repair, or business mutation is implemented here.
JSON cells are untrusted content, never instructions or authority. Records and
review references are private local output; do not publish or log them.

Digests describe canonical JSON content equality only, never authenticity,
atomic platform versions, or proof that a write happened. Observation metadata
is kept separately. All original fields (including manual edits and blank rows)
survive unchanged; fields/to_dict return defensive copies.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
import hashlib
import json
import math

from ostag.briefs.models import _identifier, _invalid, _keys, _sequence, _text, _utc
from ostag.core.schemas import SchemaError

MAX_ROWS = 1000
MAX_ROW_BYTES = 64 * 1024
MAX_SNAPSHOT_BYTES = 4 * 1024 * 1024


def _json(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"),
                      allow_nan=False)


def _bounded_json(value):
    """Bound traversal before serialization; accept only concrete JSON containers.

    ASCII canonical bytes include escapes. Depth/node caps also bound cycles,
    shared subtrees and tiny-cell fanout without consuming arbitrary iterables.
    """
    remaining = MAX_ROW_BYTES

    def visit(item, depth=0):
        nonlocal remaining
        remaining -= 1
        if remaining < 0 or depth > 32:
            raise _invalid("table row exceeds traversal limits")
        kind = type(item)
        if kind is str:
            if len(item) > remaining:
                raise _invalid("table row exceeds byte limit")
            remaining -= len(_json(item))
        elif item is None or kind is bool:
            remaining -= 5
        elif kind is int:
            if item.bit_length() > 4096:
                raise _invalid("table integer exceeds scalar limit")
            remaining -= len(str(item))
        elif kind is float:
            if not math.isfinite(item):
                raise _invalid("table numbers must be finite")
            remaining -= len(str(item))
        elif kind in (dict, list):
            if len(item) > remaining:
                raise _invalid("table row exceeds traversal limits")
            if kind is dict:
                for key, cell in item.items():
                    if type(key) is not str:
                        raise _invalid("table JSON keys must be strings")
                    visit(key, depth + 1)
                    visit(cell, depth + 1)
            else:
                for cell in item:
                    visit(cell, depth + 1)
        else:
            raise _invalid("table cells must be JSON values")
        if remaining < 0:
            raise _invalid("table row exceeds byte limit")

    visit(value)
    raw = _json(value)
    if len(raw) > MAX_ROW_BYTES:
        raise _invalid("table row exceeds byte limit")
    return raw


@dataclass(frozen=True, repr=False)
class ResourceKey:
    """Exact application namespace, provider, base and table; no alias resolution."""
    namespace: str
    platform: str
    base_id: str
    table_id: str

    def __post_init__(self):
        for name in ("namespace", "platform", "base_id", "table_id"):
            _identifier(getattr(self, name), name)

    def to_dict(self):
        return dict(namespace=self.namespace, platform=self.platform,
                    base_id=self.base_id, table_id=self.table_id)


@dataclass(frozen=True, repr=False, init=False)
class TableRecord:
    record_id: str
    _json: str

    def __init__(self, record_id, fields):
        _identifier(record_id, "record_id")
        if type(fields) is not dict:
            raise _invalid("table fields must be a JSON object")
        raw = _bounded_json(dict(record_id=record_id, fields=fields))
        object.__setattr__(self, "record_id", record_id)
        object.__setattr__(self, "_json", raw)

    @property
    def fields(self):
        return self.to_dict()["fields"]

    def to_dict(self):
        return json.loads(self._json)


@dataclass(frozen=True, repr=False)
class TableSnapshot:
    resource_key: ResourceKey
    records: tuple[TableRecord, ...]
    observed_at: str
    observed_revision: str
    content_sha256: str = field(init=False)

    def __post_init__(self):
        if type(self.resource_key) is not ResourceKey:
            raise _invalid("snapshot requires an exact resource key")
        _utc(self.observed_at, "observed_at")
        _text(self.observed_revision, "observed_revision")
        records = _sequence(self.records, "records", MAX_ROWS, empty=True)
        if any(type(record) is not TableRecord for record in records):
            raise _invalid("snapshot requires table records")
        if len({r.record_id for r in records}) != len(records):
            raise _invalid("snapshot has duplicate record IDs")
        # Count canonical envelope bytes, including metadata. Record order is
        # content: reordering is observable; observation time/version are not.
        metadata = _json(dict(resource_key=self.resource_key.to_dict(),
                              observed_at=self.observed_at,
                              observed_revision=self.observed_revision, records=[]))
        size = len(metadata) + sum(len(r._json) for r in records) + max(0, len(records) - 1)
        if size > MAX_SNAPSHOT_BYTES:
            raise _invalid("snapshot exceeds byte limit")
        raw = "[" + ",".join(r._json for r in records) + "]"
        object.__setattr__(self, "records", records)
        object.__setattr__(self, "content_sha256", hashlib.sha256(raw.encode("ascii")).hexdigest())

    @classmethod
    def from_dict(cls, payload):
        values = _keys(payload, ("resource_key", "records", "observed_at", "observed_revision"))
        values["resource_key"] = ResourceKey(**_keys(
            values["resource_key"], ("namespace", "platform", "base_id", "table_id")))
        records, size = [], 0
        for row in _sequence(values["records"], "records", MAX_ROWS, empty=True):
            record = TableRecord(**_keys(row, ("record_id", "fields")))
            size += len(record._json)
            if size > MAX_SNAPSHOT_BYTES:
                raise _invalid("snapshot exceeds byte limit")
            records.append(record)
        values["records"] = tuple(records)
        return cls(**values)

    def to_dict(self):
        return dict(resource_key=self.resource_key.to_dict(),
                    records=[r.to_dict() for r in self.records], observed_at=self.observed_at,
                    observed_revision=self.observed_revision)


@dataclass(frozen=True)
class FieldMapping:
    """Caller-owned field names and exact, disjoint known status vocabularies.

    No trimming/case folding of statuses. Unlisted statuses remain unknown.
    Only deadline_semantics='utc_datetime' opts into strict UTC deadlines;
    other/absent semantics stay gaps. No date inference from other fields.
    """
    title: str
    status: str
    owner: str
    unfinished_statuses: tuple[str, ...]
    completed_statuses: tuple[str, ...] = ()
    deadline: str | None = None
    deadline_semantics: str | None = None

    def __post_init__(self):
        names = [self.title, self.status, self.owner]
        if self.deadline is not None:
            names.append(self.deadline)
        for name in names:
            _text(name, "field name")
        if len(set(names)) != len(names):
            raise _invalid("field roles must be distinct")
        all_statuses = []
        for name in ("unfinished_statuses", "completed_statuses"):
            values = _sequence(getattr(self, name), name, 128,
                               empty=name == "completed_statuses")
            for value in values:
                _text(value, "status value")
            all_statuses.extend(values)
            object.__setattr__(self, name, values)
        if len(set(all_statuses)) != len(all_statuses):
            raise _invalid("known status values must be unique and disjoint")
        if self.deadline_semantics is not None:
            _text(self.deadline_semantics, "deadline_semantics")


@dataclass(frozen=True, repr=False)
class RecordReference:
    resource_key: ResourceKey
    record_id: str

    def __post_init__(self):
        if type(self.resource_key) is not ResourceKey:
            raise _invalid("record reference requires an exact resource key")
        _identifier(self.record_id, "record_id")


@dataclass(frozen=True, repr=False)
class RowQuality:
    reference: RecordReference
    title: str | None
    status: str | None
    unfinished: bool | None
    missing_owner: bool | None
    overdue: bool | None
    gaps: tuple[str, ...]


@dataclass(frozen=True, repr=False)
class TableQuality:
    """Known counts only; inspect row gaps for unknowns before using totals.

    blank_row_count means known blank TITLE across all statuses, not an assertion
    that every cell is empty. Missing/unreadable titles are unknown, not blank.
    """
    snapshot: TableSnapshot
    mapping: FieldMapping
    rows: tuple[RowQuality, ...]

    @property
    def total_unfinished(self):
        return sum(r.unfinished is True for r in self.rows)

    @property
    def named_unfinished(self):
        return sum(r.unfinished is True and bool(r.title) for r in self.rows)

    @property
    def blank_title_unfinished(self):
        return sum(r.unfinished is True and r.title == "" for r in self.rows)

    @property
    def missing_owner_named_unfinished(self):
        return sum(r.unfinished is True and bool(r.title) and r.missing_owner is True
                   for r in self.rows)

    @property
    def blank_row_count(self):
        return sum(r.title == "" for r in self.rows)


def _cell_text(value):
    """Text: string, null, or list of {text: str, optional type: 'text'}.

    Empty lists/null are blank. Unknown keys, links, mentions, nested formulas,
    booleans and mixed lists are unknown; never stringify arbitrary cells.
    """
    if value is None:
        return ""
    if type(value) is str:
        return value
    if type(value) is list:
        if all(type(v) is dict and set(v) in ({"text"}, {"text", "type"})
               and type(v["text"]) is str and v.get("type", "text") == "text" for v in value):
            return "".join(v["text"] for v in value)
    return None


def _missing_owner(value):
    """Owner: string, null, empty list, or person object/list with nonblank id.

    A person may also have a string name; other keys/shapes are ambiguous. These
    are cell interpretations, not authenticated identities or permission grants.
    """
    if value is None:
        return True
    if type(value) is str:
        return not value.strip()
    people = [value] if type(value) is dict else value
    if type(people) is not list:
        return None
    if not people:
        return True
    if all(type(p) is dict and set(p) in ({"id"}, {"id", "name"})
           and type(p["id"]) is str and p["id"].strip()
           and ("name" not in p or type(p["name"]) is str) for p in people):
        return False
    return None


def _status(value, mapping):
    text = _cell_text(value)
    if text in mapping.unfinished_statuses:
        return text, True
    if text in mapping.completed_statuses:
        return text, False
    return text, None


def analyze_table(snapshot: TableSnapshot, mapping: FieldMapping, *, now: str | None = None):
    """Pure interpretation; retains the original snapshot and explicit mapping."""
    if type(snapshot) is not TableSnapshot or type(mapping) is not FieldMapping:
        raise _invalid("analysis requires a snapshot and explicit field mapping")
    instant = _utc(now, "now") if now is not None else None
    rows = []
    for record in snapshot.records:
        cells, gaps = record.fields, []
        title = _cell_text(cells.get(mapping.title)) if mapping.title in cells else None
        if title is not None:
            title = title.strip()
        status, unfinished = _status(cells.get(mapping.status), mapping)
        owner = _missing_owner(cells.get(mapping.owner)) if mapping.owner in cells else None
        for role, name, known in (("title", mapping.title, title is not None),
                                  ("status", mapping.status, unfinished is not None),
                                  ("owner", mapping.owner, owner is not None)):
            if name not in cells:
                gaps.append(role + "_missing")
            elif not known:
                gaps.append(role + "_unknown")
        overdue = None
        if mapping.deadline is None:
            gaps.append("deadline_not_configured")
        elif mapping.deadline_semantics != "utc_datetime":
            gaps.append("deadline_semantics_unknown")
        elif mapping.deadline not in cells:
            gaps.append("deadline_missing")
        else:
            try:
                deadline = _utc(cells[mapping.deadline], "deadline")
            except SchemaError:
                gaps.append("deadline_unknown")
            else:
                if instant is None:
                    gaps.append("comparison_time_missing")
                elif unfinished is not None:
                    overdue = unfinished and deadline < instant
        rows.append(RowQuality(RecordReference(snapshot.resource_key, record.record_id),
                               title, status, unfinished, owner, overdue, tuple(gaps)))
    return TableQuality(snapshot, mapping, tuple(rows))


@dataclass(frozen=True, repr=False)
class MappedPair:
    mapping_index: int
    left: RecordReference | None
    right: RecordReference | None
    left_status: str | None
    right_status: str | None
    reasons: tuple[str, ...]


@dataclass(frozen=True, repr=False)
class TableComparison:
    left: TableSnapshot
    right: TableSnapshot
    left_fields: FieldMapping
    right_fields: FieldMapping
    pairs: tuple[MappedPair, ...]
    unmapped_left: tuple[RecordReference, ...]
    unmapped_right: tuple[RecordReference, ...]
    _mapping_json: str

    @property
    def record_mapping(self):
        return json.loads(self._mapping_json)

    @property
    def disagreements(self):
        return tuple(p for p in self.pairs if p.reasons == ("status_disagreement",))

    @property
    def unresolved(self):
        return tuple(p for p in self.pairs if p.reasons and p.reasons != ("status_disagreement",))


def compare_tables(left: TableSnapshot, right: TableSnapshot, record_mapping, *,
                   left_fields: FieldMapping, right_fields: FieldMapping):
    """Compare exact known status labels via explicit ID pairs, never titles.

    Mapping is a concrete list/tuple (<=1000 entries, <=64KiB canonical JSON).
    Malformed JSON entries, missing IDs, and ALL entries in a duplicate/one-to-many/
    many-to-one group are unresolved. Even an invalid opposite endpoint reserves
    its identifiable ID against silent pairing. Unmapped lists include records
    without a unique existing counterpart; status-unknown pairs remain unresolved.

    Labels use caller vocabularies verbatim. Equal labels with conflicting caller
    unfinished/completed semantics are unresolved, not agreement. Different known
    labels disagree even when both mean unfinished. There is no fuzzy translation.
    Original mappings, cells and status vocabularies remain available for review.
    """
    if type(left) is not TableSnapshot or type(right) is not TableSnapshot:
        raise _invalid("comparison requires two snapshots")
    if type(left_fields) is not FieldMapping or type(right_fields) is not FieldMapping:
        raise _invalid("comparison requires explicit field mappings")
    entries = _sequence(record_mapping, "record_mapping", MAX_ROWS, empty=True)
    raw = _bounded_json([list(p) if type(p) is tuple else p for p in entries])
    entries = json.loads(raw)
    parsed = []
    for entry in entries:
        ids = [None, None]
        if type(entry) is list:
            for side in range(min(2, len(entry))):
                try:
                    ids[side] = _identifier(entry[side], "mapping record ID")
                except SchemaError:
                    pass
        parsed.append(tuple(ids))
    left_counts = Counter(a for a, _ in parsed if a is not None)
    right_counts = Counter(b for _, b in parsed if b is not None)
    left_rows = {r.record_id: r for r in left.records}
    right_rows = {r.record_id: r for r in right.records}
    paired_left, paired_right, pairs = set(), set(), []
    for index, (a, b) in enumerate(parsed):
        reasons = []
        if type(entries[index]) is not list or len(entries[index]) != 2 or a is None or b is None:
            reasons.append("invalid_mapping")
        if left_counts[a] > 1 or right_counts[b] > 1:
            reasons.append("ambiguous_mapping")
        if a is not None and a not in left_rows:
            reasons.append("missing_left_record")
        if b is not None and b not in right_rows:
            reasons.append("missing_right_record")
        a_status = a_state = b_status = b_state = None
        if a in left_rows:
            a_status, a_state = _status(left_rows[a].fields.get(left_fields.status), left_fields)
        if b in right_rows:
            b_status, b_state = _status(right_rows[b].fields.get(right_fields.status), right_fields)
        if not reasons:
            paired_left.add(a)
            paired_right.add(b)
            if a_state is None or b_state is None:
                reasons.append("status_unknown")
            elif a_status == b_status and a_state != b_state:
                reasons.append("status_semantics_disagree")
            elif a_status != b_status:
                reasons.append("status_disagreement")
        pairs.append(MappedPair(index,
            RecordReference(left.resource_key, a) if a is not None else None,
            RecordReference(right.resource_key, b) if b is not None else None,
            a_status, b_status, tuple(reasons)))
    return TableComparison(left, right, left_fields, right_fields, tuple(pairs),
        tuple(RecordReference(left.resource_key, r) for r in left_rows if r not in paired_left),
        tuple(RecordReference(right.resource_key, r) for r in right_rows if r not in paired_right), raw)


@dataclass(frozen=True, repr=False)
class RecordReadback:
    """Independent observation, including latest manual cells; never a write receipt."""
    snapshot: TableSnapshot
    record: TableRecord


@dataclass(frozen=True, repr=False)
class TableReadPort:
    """Minimal trusted application seam; no Feishu CLI integration or write method.

    reader(exact_key) MUST perform a fresh bounded read on EVERY invocation and
    supply the independently observed key/time/revision, or 'unknown' revision.
    The wrapper guarantees a new callback call, not an honest provider, upstream
    cache freshness, pagination completeness or an atomic platform snapshot.
    A cached proposal is never accepted as readback input or proof of a write.
    Identical digests/revisions may legitimately occur on independent reads.

    Optional authorize(exact_key) runs before and after the read. It must raise
    on changed/revoked access; explicit False also denies. Application code may
    close over grants.authorize and authenticated context. This seam neither
    creates authority nor substitutes for current platform/user authorization.
    The composition owns callback latency and must keep private output local.
    """
    reader: Callable[[ResourceKey], TableSnapshot]
    authorize: Callable[[ResourceKey], object] | None = None

    def __post_init__(self):
        if not callable(self.reader) or (self.authorize is not None and not callable(self.authorize)):
            raise _invalid("table read callbacks must be trusted callables")

    def _guard(self, key):
        if self.authorize is not None and self.authorize(key) is False:
            raise _invalid("table read authorization denied")

    def snapshot(self, key: ResourceKey):
        if type(key) is not ResourceKey:
            raise _invalid("table read requires an exact resource key")
        self._guard(key)
        observed = self.reader(key)
        if type(observed) is not TableSnapshot or observed.resource_key != key:
            raise _invalid("table reader returned an invalid or different resource")
        self._guard(key)
        return observed

    def readback(self, target: RecordReference):
        if type(target) is not RecordReference:
            raise _invalid("readback requires an exact record reference")
        observed = self.snapshot(target.resource_key)
        matches = [r for r in observed.records if r.record_id == target.record_id]
        if len(matches) != 1:
            raise _invalid("readback target is missing or duplicated")
        return RecordReadback(observed, matches[0])
