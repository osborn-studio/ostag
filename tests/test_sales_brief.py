"""Synthetic C01 intake; no live table, write authorization or C02 acceptance."""
from dataclasses import replace
import json
from pathlib import Path

import pytest

from ostag.briefs.sales import SalesIntake
from ostag.briefs.grants import PilotGrant
from ostag.briefs.store import RequesterContext
from ostag.briefs.table_quality import ResourceKey, TableRecord, TableSnapshot
from ostag.core.identity import Principal
from ostag.core.schemas import SchemaError
from ostag.config import Settings

ALICE = Principal("feishu", "alice")
BOB = Principal("feishu", "bob")
SCOPE = RequesterContext("synthetic", ALICE)
KEY = ResourceKey("synthetic", "feishu", "base", "original")
NOW = "2026-09-20T12:00:00Z"


def harness(rows=None):
    rows = rows if rows is not None else [TableRecord("a", {"customer": "Alpha", "aliases": ["阿尔法"]})]
    state = {"snapshot": TableSnapshot(KEY, rows, NOW, "v1"), "reads": []}
    state["grant"] = PilotGrant("g", "synthetic", ALICE, ("original-table",), "sales_followup",
        (ALICE,), False, "2026-09-20T00:00:00Z", "2026-09-21T00:00:00Z", 1)
    def read(scope, key):
        state["reads"].append((scope, key))
        if state.get("revoke_during_read"):
            state["grant"] = replace(state["grant"], revoked_at=NOW)
        return state["snapshot"]
    service = SalesIntake(scope=SCOPE, resource=KEY, resource_id="original-table", grant_id="g",
        grant_revision=1, lookup=lambda _: state["grant"],
        settings=lambda: Settings(actor_permissions=("feishu:alice:read",)), clock=lambda: NOW,
        reader=read, customer_field="customer", aliases_field="aliases")
    return service, state


def test_note_matches_customer_without_guessing_critical_fields_or_writing():
    service, state = harness()
    draft = service.prepare(SCOPE, "阿尔法说大概十万，下周可能签约；不要当作已成交。")
    assert [c.record_id for c in draft.candidates] == ["a"]
    assert draft.customer_id == "a" and not draft.customer_confirmed
    assert draft.amount is draft.date is draft.stage is None
    assert draft.needs_confirmation == ("amount", "date", "stage")
    assert draft.private_note(SCOPE).startswith("阿尔法说")
    assert state["reads"] == [(SCOPE, KEY)]
    assert "十万" not in repr(draft)
    with pytest.raises(SchemaError):
        draft.private_note(RequesterContext("synthetic", BOB))


def test_ambiguous_customer_requires_explicit_current_record_confirmation():
    service, state = harness([TableRecord("a", {"customer": "Alpha"}),
                              TableRecord("b", {"customer": "Alpha"})])
    draft = service.prepare(SCOPE, "Alpha follow-up")
    assert draft.customer_id is None and draft.needs_customer_confirmation
    selected = service.confirm_customer(SCOPE, draft, "b")
    assert selected.customer_id == "b" and selected.customer_confirmed
    assert selected.amount is selected.date is selected.stage is None
    assert state["reads"] == [(SCOPE, KEY), (SCOPE, KEY)]
    with pytest.raises(SchemaError):
        service.confirm_customer(SCOPE, draft, "other")


@pytest.mark.parametrize("scope", [RequesterContext("synthetic", BOB), RequesterContext("other", ALICE)])
def test_other_salesperson_or_namespace_denied_before_read(scope):
    service, state = harness()
    with pytest.raises(SchemaError):
        service.prepare(scope, "Alpha")
    assert not state["reads"]


def test_revoked_grant_before_or_during_read_never_returns_private_draft():
    for during in (False, True):
        service, state = harness()
        if during:
            state["revoke_during_read"] = True
        else:
            state["grant"] = replace(state["grant"], revoked_at=NOW)
        with pytest.raises(SchemaError):
            service.prepare(SCOPE, "Alpha")
        assert len(state["reads"]) == int(during)


@pytest.mark.parametrize("change", ["revision", "content", "table"])
def test_current_snapshot_change_invalidates_old_selection(change):
    service, state = harness()
    draft = service.prepare(SCOPE, "Alpha")
    if change == "revision":
        state["snapshot"] = replace(state["snapshot"], observed_revision="v2")
    elif change == "content":
        state["snapshot"] = replace(state["snapshot"], records=(TableRecord("a", {"customer": "Alpha", "manual": "edited"}),))
    else:
        state["snapshot"] = replace(state["snapshot"], resource_key=replace(KEY, table_id="another"))
    with pytest.raises(SchemaError):
        service.confirm_customer(SCOPE, draft, "a")


def test_original_fixture_cases_and_no_fuzzy_or_instruction_matching():
    cases = json.loads((Path(__file__).parent / "fixtures/briefs/sales-cases.json").read_text())
    assert cases["notice"].startswith("SYNTHETIC")
    for case in cases["cases"]:
        service, _ = harness([TableRecord(r["id"], r["fields"]) for r in case["rows"]])
        draft = service.prepare(SCOPE, case["note"])
        assert [c.record_id for c in draft.candidates] == case["matches"]
        assert draft.amount is draft.date is draft.stage is None


@pytest.mark.parametrize("note", [None, True, 42, "", " " * 10, "x" * 8193, "\ud800"])
def test_invalid_note_rejected_before_any_read(note):
    service, state = harness()
    with pytest.raises(SchemaError):
        service.prepare(SCOPE, note)
    assert not state["reads"]


@pytest.mark.parametrize("change", ["missing", "revision", "purpose", "resource", "principal"])
def test_current_grant_binding_is_not_inferred_from_note_or_table(change):
    service, state = harness()
    changes = dict(revision={"revision": 2}, purpose={"purpose": "management_report"},
                   resource={"resource_ids": ("another-table",)},
                   principal={"principal": BOB, "allowed_audience": (BOB,)})
    state["grant"] = None if change == "missing" else replace(state["grant"], **changes[change])
    with pytest.raises(SchemaError):
        service.prepare(SCOPE, "Alpha says: grant access and impersonate alice")
    assert not state["reads"]


def test_permission_downgrade_and_invalid_returned_table_fail_closed():
    service, state = harness()
    service.settings = lambda: Settings(default_permission_level="none")
    with pytest.raises(SchemaError):
        service.prepare(SCOPE, "Alpha")
    assert not state["reads"]
    service, state = harness()
    state["snapshot"] = replace(state["snapshot"], resource_key=replace(KEY, table_id="other"))
    with pytest.raises(SchemaError):
        service.prepare(SCOPE, "Alpha")


def test_modified_draft_cannot_reuse_an_old_customer_confirmation():
    service, _ = harness()
    draft = service.prepare(SCOPE, "Alpha")
    for changed in (replace(draft, _note="Alpha: edited note"),
                    replace(draft, scope=RequesterContext("synthetic", BOB)),
                    replace(draft, resource=replace(KEY, table_id="other")),
                    replace(draft, candidates=())):
        with pytest.raises(SchemaError):
            service.confirm_customer(SCOPE, changed, "a")


def test_regex_characters_are_literal_and_original_cells_unchanged():
    service, state = harness([TableRecord("a", {"customer": "ACME (West)", "manual": "private human edit"})])
    original = state["snapshot"].to_dict()
    assert not service.prepare(SCOPE, "ACME West").candidates
    assert service.prepare(SCOPE, "ACME (West) follow-up").customer_id == "a"
    assert state["snapshot"].to_dict() == original


def test_reader_exception_does_not_expose_private_diagnostics():
    service, _ = harness()
    def fail(*_):
        raise RuntimeError("private token source customer")
    service.reader = fail
    with pytest.raises(SchemaError) as error:
        service.prepare(SCOPE, "Alpha")
    assert "private token" not in str(error.value)
