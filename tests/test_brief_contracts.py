from dataclasses import FrozenInstanceError, replace
from datetime import datetime

import pytest

from ostag.briefs.models import BriefClaim, SourceRef, SourceSet, TimeWindow, validate_claims
from ostag.core.identity import Principal
from ostag.core.schemas import SchemaError


ACTOR = Principal("feishu", "requester")
START = "2026-09-12T00:00:00Z"
END = "2026-09-13T00:00:00Z"


def source(**changes):
    values = dict(
        namespace="company/project", source_id="message-1", resource_id="chat-1",
        source_kind="feishu_message", resource_version="unknown",
        window=TimeWindow(START, END, "America/Los_Angeles"),
        event_at=START, observed_at=END, content_sha256="a" * 64,
        grant_id="pilot-1", grant_revision=1, collection_platform="feishu",
        collection_actor_id="collector", purpose="meeting-prep",
        allowed_audience=[ACTOR], visibility="private", excerpt="The release is ready.",
    )
    return SourceRef(**(values | changes))


def source_set(sources=None):
    return SourceSet(
        namespace="company/project", purpose="meeting-prep",
        window=TimeWindow(START, END, "America/Los_Angeles"),
        allowed_audience=[ACTOR], sources=[source()] if sources is None else sources,
    )


@pytest.mark.parametrize("bad", [
    "2026-02-30T00:00:00Z", "2026-09-12", "2026-09-12 00:00:00Z",
    "2026-09-12T00:00:00", "2026-09-12T00:00:00+01:00",
    "2026-09-12T00:00:00-00:00", "2026-09-12T00:00:00.1234567Z",
    "2026-09-12T24:00:00Z", None, True, datetime(2026, 9, 12),
])
def test_window_rejects_invalid_or_ambiguous_utc_in_both_paths(bad):
    with pytest.raises(SchemaError):
        TimeWindow(bad, END, "UTC")
    with pytest.raises(SchemaError):
        TimeWindow.from_dict(dict(start=bad, end=END, display_timezone="UTC"))


@pytest.mark.parametrize("changes", [
    {"start": END}, {"start": "2026-09-14T00:00:00Z"},
    {"display_timezone": "Mars/Olympus"}, {"display_timezone": "../UTC"},
    {"display_timezone": ""}, {"display_timezone": None},
])
def test_window_requires_order_and_real_iana_zone(changes):
    with pytest.raises(SchemaError):
        replace(TimeWindow(START, END, "UTC"), **changes)


def test_utc_offset_normalizes_and_calendar_leap_day_is_valid():
    window = TimeWindow("2024-02-29T00:00:00+00:00", "2024-03-01T00:00:00Z", "UTC")
    assert window.start == "2024-02-29T00:00:00Z"
    assert TimeWindow.from_dict(window.to_dict()) == window


@pytest.mark.parametrize("changes", [
    {"grant_revision": True}, {"grant_revision": 0}, {"grant_revision": 1.0},
    {"content_sha256": "a" * 63}, {"content_sha256": "g" * 64},
    {"content_sha256": None}, {"source_id": "https://invented.test/source"},
    {"resource_id": "*"}, {"namespace": ""}, {"purpose": "x" * 257},
    {"source_kind": "url"}, {"source_kind": []}, {"resource_version": None},
    {"collection_platform": "github"}, {"collection_actor_id": " "},
    {"allowed_audience": "requester"}, {"allowed_audience": []},
    {"allowed_audience": ["requester"]}, {"allowed_audience": [ACTOR, ACTOR]},
    {"visibility": "public"}, {"excerpt": "x" * 4001}, {"excerpt": None},
    {"window": {}}, {"event_at": END}, {"event_at": "2026-09-11T23:59:59Z"},
    {"observed_at": "2026-09-11T23:59:59Z"},
])
def test_source_direct_constructor_rejects_invalid_contract(changes):
    with pytest.raises(SchemaError):
        source(**changes)
    wire_changes = dict(changes)
    if isinstance(wire_changes.get("allowed_audience"), list):
        wire_changes["allowed_audience"] = [
            actor.to_dict() if isinstance(actor, Principal) else actor
            for actor in wire_changes["allowed_audience"]
        ]
    with pytest.raises(SchemaError):
        SourceRef.from_dict(source().to_dict() | wire_changes)


@pytest.mark.parametrize("factory", [
    lambda: TimeWindow(START, END, "UTC"), source, source_set,
    lambda: BriefClaim("fact", "Release is ready", ["message-1"]),
])
def test_wire_is_exact_and_required_fields_cannot_be_null(factory):
    model = factory()
    payload = model.to_dict()
    assert type(model).from_dict(payload) == model
    with pytest.raises(SchemaError):
        type(model).from_dict(payload | {"approved": True})
    for key in payload:
        with pytest.raises(SchemaError):
            type(model).from_dict({k: v for k, v in payload.items() if k != key})
        with pytest.raises(SchemaError):
            type(model).from_dict(payload | {key: None})
    for bad in ([], "", None, {1: "bad"}):
        with pytest.raises(SchemaError):
            type(model).from_dict(bad)


def test_nested_inputs_and_serialized_outputs_do_not_mutate_frozen_sources():
    audience = [ACTOR]
    item = source(allowed_audience=audience)
    items = [item]
    frozen = source_set(items)
    audience.clear()
    items.clear()
    wire = frozen.to_dict()
    wire["sources"][0]["allowed_audience"][0]["actor_id"] = "intruder"
    wire["sources"].clear()
    assert frozen.sources == (item,)
    assert item.allowed_audience == (ACTOR,)
    with pytest.raises(FrozenInstanceError):
        item.excerpt = "changed"


def test_nested_instances_are_revalidated():
    actor = Principal("feishu", "requester")
    object.__setattr__(actor, "actor_id", " " * 300)
    with pytest.raises(SchemaError):
        source(allowed_audience=[actor])
    window = TimeWindow(START, END, "UTC")
    object.__setattr__(window, "start", END)
    with pytest.raises(SchemaError):
        source(window=window)
    item = source()
    object.__setattr__(item, "grant_revision", True)
    with pytest.raises(SchemaError):
        source_set([item])


@pytest.mark.parametrize("changes", [
    {"namespace": "other"}, {"purpose": "other"},
    {"window": TimeWindow(START, END, "UTC")},
    {"allowed_audience": [Principal("telegram", "requester")]},
])
def test_source_set_rejects_scope_mismatch(changes):
    with pytest.raises(SchemaError):
        source_set([source(**changes)])


def test_duplicate_sources_unknown_references_and_url_provenance_are_rejected():
    with pytest.raises(SchemaError):
        source_set([source(), source()])
    for reference in ("unknown", "https://invented.test/proof"):
        with pytest.raises(SchemaError):
            validate_claims(source_set(), [BriefClaim("fact", "ready", [reference])])


@pytest.mark.parametrize("kind", ["fact", "commitment", "proposal"])
def test_claims_require_source_binding_without_granting_approval(kind):
    claim = BriefClaim(kind, "Release tomorrow", ["message-1"])
    assert validate_claims(source_set(), [claim]) == (claim,)
    assert set(claim.to_dict()) == {"kind", "text", "source_ids"}
    with pytest.raises(SchemaError):
        BriefClaim(kind, "Release tomorrow", [])
    with pytest.raises(SchemaError):
        BriefClaim.from_dict(claim.to_dict() | {"approved": True})


@pytest.mark.parametrize("args", [
    ("approved", "ready", ["message-1"]), ([], "ready", ["message-1"]),
    ("fact", "", ["message-1"]), ("fact", "ready", "message-1"),
    ("fact", "ready", ["message-1", "message-1"]),
])
def test_malformed_claims_are_rejected(args):
    with pytest.raises(SchemaError):
        BriefClaim(*args)


def test_manual_and_github_sources_keep_explicit_collection_identity():
    assert source(source_kind="manual").source_kind == "manual"
    github = source(source_kind="github_event", collection_platform="github",
                    resource_id="owner/repo", resource_version="abc123")
    assert SourceRef.from_dict(github.to_dict()) == github


def test_binding_validates_nested_claims_and_sequence_shape():
    claim = BriefClaim("fact", "ready", ["message-1"])
    object.__setattr__(claim, "kind", "approved")
    for claims in ([claim], "message-1", ["message-1"], None):
        with pytest.raises(SchemaError):
            validate_claims(source_set(), claims)


def test_source_and_claim_limits_reject_oversized_collections_without_truncating():
    with pytest.raises(SchemaError):
        source_set([source(source_id=f"message-{i}") for i in range(501)])
    with pytest.raises(SchemaError):
        validate_claims(source_set(), [BriefClaim("fact", "ready", ["message-1"])] * 501)
