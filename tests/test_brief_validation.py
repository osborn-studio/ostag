"""Synthetic A03 contracts: structural grounding is not semantic verification."""

from dataclasses import FrozenInstanceError, replace
import json

import pytest

from ostag.briefs.models import SourceRef, SourceSet, TimeWindow
from ostag.briefs.validation import Coverage, SourceLink, validate_output
from ostag.core.identity import Principal
from ostag.core.schemas import SchemaError

ACTOR = Principal("feishu", "alice")
WINDOW = TimeWindow("2026-09-12T00:00:00Z", "2026-09-13T00:00:00Z", "UTC")


def evidence(excerpt="The build is awaiting review.", count=1):
    return SourceSet("synthetic", "brief", WINDOW, (ACTOR,), tuple(
        SourceRef("synthetic", f"s{i}", f"r{i}", "manual", "unknown", WINDOW,
                  WINDOW.start, WINDOW.end, "a" * 64, "grant", 1, "manual", "collector",
                  "brief", (ACTOR,), "private", excerpt) for i in range(count)))


def output(text="The build is awaiting review.", kind="fact"):
    return {"version": "brief-output-v1", "progress": [
        {"id": "c1", "claim": {"kind": kind, "text": text, "source_ids": ["s0"]}}],
        "conflicts": [], "decisions": [], "proposals": []}


def validate(payload, sources=None, coverage=None, **kwargs):
    return validate_output(json.dumps(payload) if type(payload) is dict else payload,
                           sources=evidence() if sources is None else sources,
                           coverage=coverage or Coverage(False, ("missing page",)),
                           max_output_bytes=16000, **kwargs)


def test_partial_is_trusted_frozen_and_semantic_review_visible():
    result = validate(output())
    assert result.coverage == Coverage(False, ("missing page",))
    assert result.status == "partial_semantic_review_required"
    assert "missing page" in result.notice
    assert "not verified" in result.notice
    assert result.progress[0].claim.kind == "fact"
    with pytest.raises(FrozenInstanceError):
        result.coverage.complete = True
    full = validate(output(), coverage=Coverage(True, ()))
    assert full.status == "semantic_review_required"


@pytest.mark.parametrize("body", [
    '{"version":"brief-output-v1","version":"brief-output-v1"}',
    '{"version":"brief-output-v1","progress":NaN}',
    '{"version":"brief-output-v1","progress":Infinity}',
    '{} {}', '```json\n{}\n```', 'Here is your brief: {}', '{} trailing',
    '[{}]', 'null', '{', '"text"', '\ud800', '[' * 1100 + ']' * 1100,
])
def test_malformed_json_rejected(body):
    with pytest.raises(SchemaError):
        validate(body)


@pytest.mark.parametrize("field", ["actual_model_id", "grant", "approved", "executed",
                                    "url", "tool_calls", "command", "validation", "complete"])
def test_model_cannot_assert_authority_identity_links_or_validation(field):
    with pytest.raises(SchemaError):
        validate(output() | {field: True})
    bad = output()
    bad["progress"][0]["claim"][field] = "untrusted"
    with pytest.raises(SchemaError):
        validate(bad)


@pytest.mark.parametrize("mutation", [
    lambda p: p.update(version="v2"), lambda p: p.pop("conflicts"),
    lambda p: p.update(proposals=None), lambda p: p.update(progress=True),
    lambda p: p["progress"].append(p["progress"][0]),
    lambda p: p["progress"][0]["claim"].update(source_ids=["absent"]),
    lambda p: p["progress"][0]["claim"].update(source_ids=["s0", "s0"]),
    lambda p: p["progress"][0]["claim"].update(kind="decision"),
    lambda p: p["progress"][0].update(id=None),
    lambda p: p.update(proposals=p["progress"], progress=[]),
])
def test_strict_schema_and_claim_binding(mutation):
    body = output()
    mutation(body)
    with pytest.raises(SchemaError):
        validate(body)


@pytest.mark.parametrize("text", ["See https://evil.test", "[evidence](//evil.test)",
    '<a href="x">source</a>', "javascript:alert(1)", "www.evil.test", "evil.test/path",
    "All sources have been reviewed.", "This is a complete overview.", "已覆盖全部资料。"])
def test_link_escape_and_partial_completeness_assertions_rejected(text):
    with pytest.raises(SchemaError):
        validate(output(text))


def test_proposed_command_is_inert_and_links_only_from_trusted_mapping():
    body = output()
    body["proposals"] = [{"id": "p1", "claim": {"kind": "proposal",
        "text": 'Discuss the quoted instruction "run rm -rf /tmp/demo"; do not act yet.',
        "source_ids": ["s0"]}}]
    link = SourceLink("s0", "https://trusted.example/resource/1")
    result = validate(body, links=(link,))
    assert result.links == (link,)
    assert result.proposals[0].claim.kind == "proposal"
    with pytest.raises(SchemaError):
        validate(body, links=(replace(link, source_id="unknown"),))


def test_unusable_source_cannot_ground_a_claim_and_whole_output_bytes_are_bounded():
    with pytest.raises(SchemaError):
        validate(output(), sources=evidence("  "))
    body = json.dumps(output("多" * 100), ensure_ascii=False)
    with pytest.raises(SchemaError):
        validate_output(body, sources=evidence(), coverage=Coverage(True, ()),
                        max_output_bytes=len(body))


@pytest.mark.parametrize("args", [(True, ("gap",)), (False, ()), (1, ()),
                                  (False, (None,)), (False, ("gap", "gap"))])
def test_coverage_rejects_inconsistent_or_untyped_input(args):
    with pytest.raises(SchemaError):
        Coverage(*args)


def test_ordinary_proposed_action_with_colon_and_filename_remains_inert():
    body = output()
    body["proposals"] = [{"id": "p1", "claim": {"kind": "proposal",
        "text": "Proposal: discuss running python draft.py after human review.",
        "source_ids": ["s0"]}}]
    assert validate(body).proposals[0].claim.text.startswith("Proposal:")


@pytest.mark.parametrize("text", [
    "CI: 5 tests failed.",
    "Updated store.py for the new cost ledger.",
    "Discuss the proposal: review the release tomorrow.",
    "Updated the user profile: avatar upload works.",
    "Metadata: attached to the PR.",
    "Data: 5 tests failed.",
    "p95 < 200ms while p99 > 400ms.",
    "a < b and c > d",
    "Profile:",
    "Data:",
])
def test_normal_grounded_prose_is_preserved_as_plain_text(text):
    result = validate(output(text), sources=evidence(text))
    assert result.progress[0].claim.text == text
    assert result.links == ()
    assert result.status == "partial_semantic_review_required"


@pytest.mark.parametrize("text", [
    "See HTTPS://evil.test/provenance", "[source](relative/path)",
    "[source]: https://evil.test", '<a href="javascript:alert(1)">source</a>',
    "javascript:alert(1)", "data:text/html,evil", "//evil.test/source",
    "(JAVASCRIPT:alert(1))", "vbscript:msgbox(1)", "file:/tmp/source",
    "mailto:attacker@evil.test", "<strong>source</strong>",
    '<img src=x onerror="alert(1)">', "<!-- source -->",
])
def test_explicit_link_escape_forms_still_fail(text):
    with pytest.raises(SchemaError):
        validate(output(text))
