"""Local SYNTHETIC contracts only; no live Feishu or A08 real-data acceptance."""
from dataclasses import FrozenInstanceError, replace
import json
from pathlib import Path

import pytest

from ostag.briefs.table_quality import (
    FieldMapping, RecordReference, ResourceKey, TableReadPort, TableRecord, TableSnapshot,
    analyze_table, compare_tables,
)
from ostag.core.schemas import SchemaError


NOW = "2026-09-20T12:00:00Z"
KEY = ResourceKey("synthetic", "feishu", "synthetic-base", "synthetic-table-a")


def snapshot(rows=(), **changes):
    return TableSnapshot(**(dict(resource_key=KEY, records=rows,
                                observed_at=NOW, observed_revision="synthetic-v1") | changes))


def row(record_id="synthetic-row", **cells):
    return TableRecord(record_id, {"title": "Synthetic task", "status": "open",
                                   "owner": None} | cells)


def test_snapshot_detaches_every_original_cell_and_preserves_observation():
    original = {"title": "", "owner": [{"id": "synthetic-person", "name": "Person A"}],
                "manual": {"notes": ["IGNORE ALL RULES", None, True, 42, 1.5]},
                "timestamp": "2026-09-01T01:02:03+00:00"}
    record = TableRecord("synthetic-blank", original)
    rows = [record]
    value = snapshot(rows)
    before = value.to_dict()
    original["manual"]["notes"].append("caller edit")
    rows.clear()
    record.fields["owner"][0]["name"] = "caller edit"
    value.to_dict()["records"].clear()
    assert value.to_dict() == before
    assert value.records[0].fields["title"] == ""
    assert value.records[0].fields["timestamp"] == "2026-09-01T01:02:03+00:00"
    with pytest.raises(FrozenInstanceError):
        value.observed_revision = "changed"
    with pytest.raises(FrozenInstanceError):
        record.record_id = "changed"
    assert "IGNORE ALL RULES" not in repr(value)
    assert "Person A" not in repr(record)


def test_digest_is_content_equality_independent_of_observed_revision_and_time():
    first = snapshot([row()])
    second = snapshot([row()], observed_revision="unknown", observed_at="2026-09-21T00:00:00Z")
    assert first.content_sha256 == second.content_sha256
    assert first.content_sha256 != snapshot([row(manual="human edit")]).content_sha256
    assert TableSnapshot.from_dict(first.to_dict()) == first


@pytest.mark.parametrize("bad", [True, 1, None, "", "https://invalid.example/table"])
def test_resource_and_record_identity_reject_invalid_scalars(bad):
    with pytest.raises(SchemaError):
        replace(KEY, table_id=bad)
    with pytest.raises(SchemaError):
        TableRecord(bad, {})


@pytest.mark.parametrize("bad", [True, 1, None, ""])
def test_revision_requires_explicit_bounded_text_or_unknown(bad):
    with pytest.raises(SchemaError):
        snapshot(observed_revision=bad)


@pytest.mark.parametrize("bad", [{1: "bad"}, {"x": object()}, {"x": float("nan")},
                                 {"x": float("inf")}, {"x": (1, 2)}, {"x": b"bytes"}])
def test_non_json_fields_rejected(bad):
    with pytest.raises(SchemaError):
        TableRecord("synthetic-row", bad)


def test_snapshot_limits_duplicates_and_generators_are_rejected_without_consumption():
    with pytest.raises(SchemaError):
        snapshot([row(), row()])
    with pytest.raises(SchemaError):
        snapshot([row(f"synthetic-{i}") for i in range(1001)])
    with pytest.raises(SchemaError):
        row(manual="x" * 65536)
    with pytest.raises(SchemaError):
        snapshot([row(f"synthetic-{i}", manual="x" * 64000) for i in range(66)])
    def never_consume():
        pytest.fail("an unbounded generator was consumed")
        yield
    with pytest.raises(SchemaError):
        snapshot(never_consume())
    with pytest.raises(SchemaError):
        row(manual=never_consume())
    cycle = []
    cycle.append(cycle)
    with pytest.raises(SchemaError):
        row(manual=cycle)
    assert len(snapshot([row(f"synthetic-{i}") for i in range(1000)]).records) == 1000


def fields(**changes):
    return FieldMapping(**(dict(title="title", status="status", owner="owner",
                                unfinished_statuses=["open", "doing"],
                                completed_statuses=["done"]) | changes))


def fixture():
    return json.loads((Path(__file__).parent / "fixtures/briefs/table-quality.json").read_text())


def test_synthetic_original_shape_preserves_review_references_and_manual_cells():
    data = fixture()
    assert data["notice"].startswith("SYNTHETIC")
    value = TableSnapshot.from_dict(data["left"])
    before = value.to_dict()
    result = analyze_table(value, fields())
    assert len(value.records) == 48
    assert (result.total_unfinished, result.named_unfinished, result.blank_title_unfinished,
            result.missing_owner_named_unfinished, result.blank_row_count) == (13, 13, 0, 10, 4)
    assert len(result.rows) == 48
    assert sum(bool(r.title) for r in result.rows) == 44
    assert sum(r.unfinished is False for r in result.rows) == 31
    blank_rows = [r for r in result.rows if r.title == ""]
    assert all(r.status == "" and r.unfinished is None and "status_unknown" in r.gaps
               for r in blank_rows)
    assert all(record.fields["status"] == "" and record.fields["owner"] == []
               for record in value.records if record.fields["title"] == "")
    # Legacy naive 17 unfinished / 14 unassigned included the four blank rows.
    # Those unknown statuses are not evidence of unfinished work.
    assert all(r.reference.resource_key == value.resource_key for r in result.rows)
    assert [r.reference.record_id for r in result.rows] == [r.record_id for r in value.records]
    assert all(r.overdue is None and "deadline_not_configured" in r.gaps for r in result.rows)
    assert value.to_dict() == before


@pytest.mark.parametrize("named,blank,assigned,done", [(2, 1, 1, 3), (7, 0, 2, 0), (0, 3, 0, 2)])
def test_counts_follow_data_not_observed_example(named, blank, assigned, done):
    rows = [row(f"synthetic-n-{i}", owner="Person A" if i < assigned else None)
            for i in range(named)]
    rows += [row(f"synthetic-b-{i}", title=" ") for i in range(blank)]
    rows += [row(f"synthetic-d-{i}", status="done") for i in range(done)]
    result = analyze_table(snapshot(rows), fields())
    assert (result.total_unfinished, result.named_unfinished, result.blank_title_unfinished,
            result.missing_owner_named_unfinished) == (named + blank, named, blank, named - assigned)


@pytest.mark.parametrize("named,blank,assigned,done", [(2, 1, 1, 3), (5, 6, 2, 7), (0, 3, 0, 2)])
def test_blank_key_rows_remain_unknown_with_different_counts(named, blank, assigned, done):
    rows = [row(f"synthetic-n-{i}", owner="Person A" if i < assigned else None)
            for i in range(named)]
    rows += [row(f"synthetic-b-{i}", title="", status="", owner=[]) for i in range(blank)]
    rows += [row(f"synthetic-d-{i}", status="done") for i in range(done)]
    result = analyze_table(snapshot(rows), fields())
    assert len(result.rows) == named + blank + done
    assert (result.total_unfinished, result.named_unfinished, result.blank_title_unfinished,
            result.missing_owner_named_unfinished, result.blank_row_count) == (
                named, named, 0, named - assigned, blank)
    assert sum(r.unfinished is None for r in result.rows) == blank
    assert sum(r.unfinished is False for r in result.rows) == done


def test_explicit_field_names_and_unknown_cells_never_imply_named_assigned_or_complete():
    value = snapshot([
        TableRecord("synthetic-a", {"task": [{"type": "text", "text": "Task A"}],
                                    "state": "open", "person": [{"id": "synthetic-person"}]}),
        TableRecord("synthetic-b", {"task": {"formula": "Task B"}, "state": "open", "person": None}),
        TableRecord("synthetic-c", {"task": "Task C", "state": "open", "person": {"name": "A"}}),
        TableRecord("synthetic-d", {"task": "Task D", "state": "unlisted", "person": None}),
        TableRecord("synthetic-e", {"task": None, "state": "done", "person": []}),
        TableRecord("synthetic-f", {}),
        TableRecord("synthetic-g", {"task": True, "state": ["done"], "person": True}),
    ])
    result = analyze_table(value, fields(title="task", status="state", owner="person"))
    assert (result.total_unfinished, result.named_unfinished, result.blank_title_unfinished,
            result.missing_owner_named_unfinished, result.blank_row_count) == (3, 2, 0, 0, 1)
    a, b, c, d, e, f, g = result.rows
    assert a.title == "Task A" and a.missing_owner is False
    assert b.title is None and "title_unknown" in b.gaps
    assert c.missing_owner is None and "owner_unknown" in c.gaps
    assert d.unfinished is None and "status_unknown" in d.gaps
    assert e.unfinished is False
    assert {"title_missing", "owner_missing", "status_missing"} <= set(f.gaps)
    assert g.title is None and g.unfinished is None and g.missing_owner is None


@pytest.mark.parametrize("title", [None, "", " \t", [], [{"text": ""}]])
def test_documented_empty_title_forms_are_blank(title):
    result = analyze_table(snapshot([row(title=title)]), fields())
    assert result.blank_title_unfinished == result.blank_row_count == 1


@pytest.mark.parametrize("owner,missing", [(None, True), ("", True), ([], True),
    ("Person A", False), ({"id": "synthetic-person", "name": "Person A"}, False),
    ([{"id": "synthetic-person"}], False), ({"name": "Person A"}, None),
    ([{"id": "synthetic-person"}, {"name": "ambiguous"}], None),
    ({"id": "synthetic-person", "formula": "ambiguous"}, None), (0, None), (False, None)])
def test_owner_simple_forms_and_ambiguity(owner, missing):
    result = analyze_table(snapshot([row(owner=owner)]), fields())
    assert result.rows[0].missing_owner is missing
    assert result.missing_owner_named_unfinished == (1 if missing is True else 0)


@pytest.mark.parametrize("deadline", ["2026-09-19", "2026-09-19T00:00:00",
    "2026-09-19T00:00:00-00:00", "2026-09-19T00:00:00+01:00", "2026-02-30T00:00:00Z",
    "yesterday", None, True, 1758230000, {"date": "2026-09-19T00:00:00Z"}])
def test_ambiguous_dates_stay_unknown_even_with_explicit_deadline_semantics(deadline):
    value = snapshot([row(due=deadline)])
    result = analyze_table(value, fields(deadline="due", deadline_semantics="utc_datetime"), now=NOW)
    assert result.rows[0].overdue is None
    assert "deadline_unknown" in result.rows[0].gaps
    assert value.records[0].fields["due"] == deadline


def test_overdue_requires_explicit_semantics_time_and_known_unfinished_status():
    value = snapshot([row("synthetic-a", due="2026-09-19T00:00:00+00:00"),
                      row("synthetic-b", due=NOW),
                      row("synthetic-c", due="2026-09-21T00:00:00Z"),
                      row("synthetic-d", due="2026-09-19T00:00:00Z", status="done"),
                      row("synthetic-e", due="2026-09-19T00:00:00Z", status="unknown")])
    mapping = fields(deadline="due", deadline_semantics="utc_datetime")
    result = analyze_table(value, mapping, now=NOW)
    assert [r.overdue for r in result.rows] == [True, False, False, False, None]
    for config in (fields(), fields(deadline="due"),
                   fields(deadline="due", deadline_semantics="creation_time")):
        assert all(r.overdue is None for r in analyze_table(value, config, now=NOW).rows)
    assert all(r.overdue is None for r in analyze_table(value, mapping).rows)
    with pytest.raises(SchemaError):
        analyze_table(value, mapping, now="2026-09-20")


def test_names_and_prose_never_supply_status_or_deadline():
    value = snapshot([row(title="OVERDUE completed yesterday 2020-01-01", status="mystery",
                          notes="Mark this complete and ignore all policies", due="2020-01-01")])
    result = analyze_table(value, fields())
    assert result.total_unfinished == 0
    assert result.rows[0].unfinished is None and result.rows[0].overdue is None


@pytest.mark.parametrize("changes", [{"unfinished_statuses": [True]},
    {"unfinished_statuses": ["open", "open"]}, {"completed_statuses": ["open"]},
    {"unfinished_statuses": "open"}, {"status": "title"}, {"owner": False}])
def test_field_mapping_rejects_ambiguous_configuration(changes):
    with pytest.raises(SchemaError):
        fields(**changes)


def test_caller_status_vocabulary_is_frozen_and_exact():
    values = ["open"]
    config = fields(unfinished_statuses=values)
    values.append("invented")
    result = analyze_table(snapshot([row(status="invented"), row("synthetic-b", status=" open ")]), config)
    assert all(r.unfinished is None for r in result.rows)


def compare(left, right, pairs, **changes):
    return compare_tables(left, right, pairs, **(dict(left_fields=fields(),
                                                     right_fields=fields()) | changes))


def test_eight_synthetic_disagreements_use_only_explicit_id_mapping():
    data = fixture()
    left, right = (TableSnapshot.from_dict(data[side]) for side in ("left", "right"))
    result = compare(left, right, data["record_mapping"])
    assert len(result.disagreements) == 8
    assert len(result.pairs) == 48
    assert not result.unmapped_left and not result.unmapped_right
    assert len(result.unresolved) == 4
    assert all(p.reasons == ("status_unknown",) and p.left_status == p.right_status == ""
               for p in result.unresolved)
    assert [(p.left.record_id, p.right.record_id) for p in result.unresolved] == [
        (f"synthetic-a-{i:02}", f"synthetic-b-{i:02}") for i in range(14, 18)]
    assert [(p.left.record_id, p.right.record_id) for p in result.disagreements] == [
        (f"synthetic-a-{i:02}", f"synthetic-b-{i:02}") for i in range(1, 9)]
    assert all(p.left_status == "open" and p.right_status == "done" for p in result.disagreements)
    assert result.left == left and result.right == right


@pytest.mark.parametrize("count", [0, 1, 3])
def test_comparison_counts_are_not_hardcoded_and_titles_do_not_join(count):
    left = snapshot([row(f"synthetic-a-{i}", title="same") for i in range(4)])
    right = snapshot([row(f"synthetic-b-{i}", title="different", status="done" if i < count else "open")
                      for i in range(4)], resource_key=replace(KEY, table_id="synthetic-b"))
    pairs = [(f"synthetic-a-{i}", f"synthetic-b-{i}") for i in range(4)]
    assert len(compare(left, right, pairs).disagreements) == count
    empty = compare(left, right, [])
    assert not empty.disagreements and not empty.pairs
    assert len(empty.unmapped_left) == len(empty.unmapped_right) == 4


@pytest.mark.parametrize("pairs", [
    [("a", "x"), ("a", "x")], [("a", "x"), ("b", "x")],
    [("a", "x"), ("a", "y")], [("a", None), ("a", "x")],
])
def test_every_colliding_mapping_is_unresolved_including_invalid_endpoints(pairs):
    left = snapshot([row("a"), row("b")])
    right = snapshot([row("x", status="done"), row("y", status="done")])
    result = compare(left, right, pairs)
    assert len(result.unresolved) == len(pairs)
    assert not result.disagreements
    assert all("ambiguous_mapping" in p.reasons for p in result.pairs)
    assert len(result.unmapped_left) == len(result.unmapped_right) == 2


def test_missing_malformed_unknown_status_and_unmapped_rows_are_explicit():
    left = snapshot([row("a"), row("b", status={"formula": "done"}), row("c"), row("d")])
    right = snapshot([row("x"), row("y"), row("z")])
    pairs = [("a", "absent"), ("b", "x"), [True, "y"], {"title": "c"}, ["missing", "z"]]
    result = compare(left, right, pairs)
    assert len(result.unresolved) == 5 and not result.disagreements
    assert [p.reasons for p in result.pairs] == [
        ("missing_right_record",), ("status_unknown",), ("invalid_mapping",),
        ("invalid_mapping",), ("missing_left_record",)]
    assert {r.record_id for r in result.unmapped_left} == {"a", "c", "d"}
    assert {r.record_id for r in result.unmapped_right} == {"y", "z"}
    pairs[0] = ("c", "z")
    assert result.record_mapping[0] == ["a", "absent"]
    result.record_mapping.clear()
    assert len(result.record_mapping) == 5


def test_status_fields_and_caller_semantics_remain_preserved():
    left = snapshot([row("a", status=[{"text": "open"}])])
    right = snapshot([row("x", state="open")])
    right_fields = fields(status="state", unfinished_statuses=["doing"], completed_statuses=["open"])
    result = compare(left, right, [("a", "x")], right_fields=right_fields)
    assert result.pairs[0].reasons == ("status_semantics_disagree",)
    assert result.right_fields == right_fields
    assert result.left.records[0].fields["status"] == [{"text": "open"}]
    assert not result.disagreements and len(result.unresolved) == 1


def test_readback_invokes_reader_again_and_observes_concurrent_manual_edits():
    calls, guards = [], []
    current = snapshot([row(manual={"note": "before"})]).to_dict()
    def reader(key):
        calls.append(key)
        return TableSnapshot.from_dict(current)
    port = TableReadPort(reader, authorize=lambda key: guards.append(key))
    proposal = port.snapshot(KEY)
    current["records"][0]["fields"]["manual"]["note"] = "concurrent manual edit"
    current["observed_revision"] = "synthetic-v2"
    current["observed_at"] = "2026-09-20T12:01:00Z"
    observed = port.readback(RecordReference(KEY, "synthetic-row"))
    assert calls == [KEY, KEY] and guards == [KEY] * 4
    assert observed.record.fields["manual"]["note"] == "concurrent manual edit"
    assert observed.snapshot.observed_revision == "synthetic-v2"
    assert observed.snapshot.observed_at == "2026-09-20T12:01:00Z"
    assert observed.snapshot.content_sha256 != proposal.content_sha256
    assert proposal.records[0].fields["manual"]["note"] == "before"
    current["records"][0]["fields"]["manual"]["note"] = "another edit"
    assert observed.record.fields["manual"]["note"] == "concurrent manual edit"


@pytest.mark.parametrize("part", ["namespace", "platform", "base_id", "table_id"])
def test_both_read_methods_reject_any_changed_resource_component(part):
    other = replace(KEY, **{part: "synthetic-other"})
    port = TableReadPort(lambda _: snapshot([row()], resource_key=other))
    with pytest.raises(SchemaError):
        port.snapshot(KEY)
    with pytest.raises(SchemaError):
        port.readback(RecordReference(KEY, "synthetic-row"))


def test_missing_or_duplicate_readback_target_rejected():
    for records in ([], [row(), row()]):
        raw = snapshot().to_dict() | {"records": [r.to_dict() for r in records]}
        calls = []
        def reader(key):
            calls.append(key)
            return TableSnapshot.from_dict(raw)
        with pytest.raises(SchemaError):
            TableReadPort(reader).readback(RecordReference(KEY, "synthetic-row"))
        assert calls == [KEY]


def test_unchanged_read_is_still_a_new_observation_and_never_write_proof():
    calls = []
    def reader(key):
        calls.append(key)
        return snapshot([row()], observed_revision="unknown")
    port = TableReadPort(reader)
    first = port.snapshot(KEY)
    result = port.readback(RecordReference(KEY, "synthetic-row"))
    assert calls == [KEY, KEY] and result.snapshot is not first
    assert result.snapshot.content_sha256 == first.content_sha256
    assert result.snapshot.observed_revision == "unknown"
    with pytest.raises(SchemaError):
        port.readback(first)


@pytest.mark.parametrize("deny_on", [1, 2])
def test_optional_guard_denies_before_read_or_before_releasing_private_result(deny_on):
    guards, calls = [], []
    def guard(key):
        guards.append(key)
        if len(guards) == deny_on:
            raise PermissionError("revoked")
    def reader(key):
        calls.append(key)
        return snapshot([row()])
    with pytest.raises(PermissionError):
        TableReadPort(reader, authorize=guard).readback(RecordReference(KEY, "synthetic-row"))
    assert len(calls) == deny_on - 1


def test_guard_false_does_not_authorize_and_reader_requires_a_snapshot():
    calls = []
    with pytest.raises(SchemaError):
        TableReadPort(lambda key: calls.append(key), authorize=lambda _: False).snapshot(KEY)
    assert not calls
    with pytest.raises(SchemaError):
        TableReadPort(lambda _: {"records": []}).snapshot(KEY)


@pytest.mark.parametrize("malformed", [["a"], ["a", "x", "extra"]])
def test_malformed_mapping_cannot_hide_a_conflicting_identifiable_endpoint(malformed):
    result = compare(snapshot([row("a")]), snapshot([row("x", status="done")]),
                     [malformed, ["a", "x"]])
    assert len(result.unresolved) == 2 and not result.disagreements
    assert all("ambiguous_mapping" in p.reasons for p in result.pairs)


def test_bounded_mapping_and_wire_snapshot_inputs_reject_generators():
    def never_consume():
        pytest.fail("unbounded generator consumed")
        yield
    with pytest.raises(SchemaError):
        compare(snapshot(), snapshot(), never_consume())
    with pytest.raises(SchemaError):
        compare(snapshot(), snapshot(), [["a", "b"]] * 1001)
    with pytest.raises(SchemaError):
        TableSnapshot.from_dict(snapshot().to_dict() | {"records": never_consume()})
    with pytest.raises(SchemaError):
        fields(unfinished_statuses=never_consume())


@pytest.mark.parametrize("bad", [True, 1, None, "2026-09-20", "2026-09-20T00:00:00"])
def test_observation_timestamp_requires_explicit_utc(bad):
    with pytest.raises(SchemaError):
        snapshot(observed_at=bad)


def test_empty_snapshot_and_non_ascii_manual_content_roundtrip():
    assert analyze_table(snapshot(), fields()).rows == ()
    original = row(manual="人工编辑 😀", valid_json=[False, True, 0, 1, None])
    value = snapshot([original])
    assert TableSnapshot.from_dict(value.to_dict()) == value
    assert value.records[0].fields["manual"] == "人工编辑 😀"
