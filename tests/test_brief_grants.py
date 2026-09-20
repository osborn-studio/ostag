from dataclasses import FrozenInstanceError, replace

import pytest

from ostag.briefs.grants import PilotGrant, authorize
from ostag.config import Settings
from ostag.core.identity import Principal
from ostag.core.schemas import SchemaError


ACTOR = Principal("feishu", "same")
START = "2026-09-12T00:00:00Z"
END = "2026-09-13T00:00:00Z"
NOW = "2026-09-12T12:00:00Z"


def grant(**changes):
    values = dict(
        grant_id="pilot-1", namespace="company/project", principal=ACTOR,
        resource_ids=["chat-1", "manual-1"], purpose="meeting-prep",
        allowed_audience=[ACTOR], model_egress_allowed=True,
        valid_from=START, expires_at=END, revision=1,
    )
    return PilotGrant(**(values | changes))


def check(current=None, **changes):
    current = grant() if current is None else current
    values = dict(
        lookup=lambda grant_id: current, config=Settings(default_permission_level="propose"),
        grant_id="pilot-1", grant_revision=1, namespace="company/project", principal=ACTOR,
        resource_ids=["chat-1"], purpose="meeting-prep", allowed_audience=[ACTOR],
        operation="generate", now=NOW,
    )
    return authorize(**(values | changes))


def test_authorization_returns_current_immutable_grant_and_rechecks_lookup():
    current = grant()
    calls = []

    def lookup(grant_id):
        calls.append(grant_id)
        return current

    result = check(lookup=lookup)
    assert result == current
    with pytest.raises(FrozenInstanceError):
        result.revision = 2
    current = replace(current, revision=2)
    with pytest.raises(SchemaError):
        check(lookup=lookup)
    assert check(lookup=lookup, grant_revision=2) == current
    current = replace(current, revoked_at=NOW)
    with pytest.raises(SchemaError):
        check(lookup=lookup, grant_revision=2)
    current = None
    with pytest.raises(SchemaError):
        check(lookup=lookup, grant_revision=2)
    assert calls == ["pilot-1"] * 5


@pytest.mark.parametrize("changes", [
    {"grant_id": "other"}, {"grant_revision": 2}, {"grant_revision": True},
    {"namespace": "other"}, {"principal": Principal("telegram", "same")},
    {"resource_ids": ["other"]}, {"resource_ids": []}, {"resource_ids": "chat-1"},
    {"resource_ids": ["chat-1", "*"]}, {"purpose": "other"},
    {"allowed_audience": [Principal("feishu", "other")]},
    {"allowed_audience": [ACTOR, Principal("feishu", "other")]},
    {"allowed_audience": None}, {"operation": "write"}, {"operation": "approve"},
    {"operation": "apply"}, {"operation": "deliver"}, {"operation": []},
    {"now": "2026-09-12 12:00:00Z"},
])
def test_authorization_rejects_scope_pin_and_operation_mismatch(changes):
    with pytest.raises(SchemaError):
        check(**changes)


def test_lookup_cannot_substitute_different_grant_or_wire_payload():
    for result in (grant(grant_id="other"), grant().to_dict(), None):
        with pytest.raises(SchemaError):
            check(lookup=lambda grant_id: result)


@pytest.mark.parametrize("now,revoked_at,allowed", [
    (START, None, True), ("2026-09-11T23:59:59Z", None, False),
    ("2026-09-12T23:59:59.999999Z", None, True), (END, None, False),
    (NOW, NOW, False), ("2026-09-12T11:59:59.999999Z", NOW, True),
    ("2026-09-12T12:00:00.000001Z", NOW, False),
])
def test_validity_expiry_and_revocation_boundaries(now, revoked_at, allowed):
    current = grant(revoked_at=revoked_at)
    if allowed:
        assert check(current, now=now) == current
    else:
        with pytest.raises(SchemaError):
            check(current, now=now)


def test_a1_material_read_and_generation_require_different_permissions_and_egress():
    config = Settings(default_permission_level="read")
    assert check(config=config, operation="read")
    with pytest.raises(SchemaError):
        check(config=config)
    current = grant(model_egress_allowed=False)
    assert check(current, operation="read")
    with pytest.raises(SchemaError):
        check(current)


def test_same_actor_on_another_platform_does_not_inherit_core_permission():
    config = Settings(default_permission_level="read", actor_permissions=("feishu:same:propose",))
    assert check(config=config)
    other = Principal("telegram", "same")
    with pytest.raises(SchemaError):
        check(grant(principal=other, allowed_audience=[other]), config=config,
              principal=other, allowed_audience=[other])


def test_channel_permissions_take_precedence_over_global_permissions():
    config = Settings(default_permission_level="read", actor_permissions=("feishu:same:admin",))
    with pytest.raises(SchemaError):
        check(config=config, channel_grants=("feishu:same:read",))
    assert check(config=Settings(default_permission_level="read"),
                 channel_grants=("feishu:same:propose",))
    with pytest.raises(SchemaError):
        check(config=Settings(default_permission_level="read"),
              channel_grants=("telegram:same:admin",))


def test_bare_actor_grants_and_local_admin_cannot_escalate():
    config = Settings(default_permission_level="read", actor_permissions=("same:admin",))
    with pytest.warns(UserWarning), pytest.raises(SchemaError):
        check(config=config)
    local = Principal("feishu", "local-admin")
    with pytest.raises(SchemaError):
        check(grant(principal=local, allowed_audience=[local]), principal=local,
              allowed_audience=[local], config=Settings(default_permission_level="read"))


@pytest.mark.parametrize("config,channel", [
    (Settings(default_permission_level="unknown"), ()),
    (Settings(actor_permissions=("feishu:same:unknown",)), ()),
    (Settings(), ("feishu:same:unknown",)),
])
def test_unknown_permission_levels_fail_closed_even_for_read(config, channel):
    with pytest.raises(SchemaError):
        check(config=config, channel_grants=channel, operation="read")


@pytest.mark.parametrize("pair", ["feishu:same:read:extra", "feishu::read", "github:same:read"])
def test_malformed_scoped_permission_configuration_does_not_fall_back(pair):
    with pytest.raises(SchemaError):
        check(channel_grants=(pair,))


def test_current_core_permission_is_resolved_again_after_first_authorization():
    config = Settings(actor_permissions=("feishu:same:propose",))
    assert check(config=config)
    config = replace(config, actor_permissions=("feishu:same:read",))
    with pytest.raises(SchemaError):
        check(config=config)


@pytest.mark.parametrize("changes", [
    {"grant_id": "*"}, {"namespace": "*"}, {"resource_ids": []},
    {"resource_ids": "chat-1"}, {"resource_ids": ["chat-*"]},
    {"resource_ids": ["chat-1", "chat-1"]}, {"purpose": "*"},
    {"principal": {}}, {"principal": None}, {"allowed_audience": []},
    {"allowed_audience": [Principal("telegram", "same")]},
    {"model_egress_allowed": 1}, {"model_egress_allowed": None},
    {"revision": True}, {"revision": 0}, {"revision": 1.5},
    {"valid_from": END}, {"expires_at": START}, {"revoked_at": "yesterday"},
    {"revoked_at": False},
])
def test_malformed_direct_grants_are_rejected(changes):
    with pytest.raises(SchemaError):
        grant(**changes)
    wire_changes = dict(changes)
    if isinstance(wire_changes.get("allowed_audience"), list):
        wire_changes["allowed_audience"] = [actor.to_dict()
                                            for actor in wire_changes["allowed_audience"]]
    with pytest.raises(SchemaError):
        PilotGrant.from_dict(grant().to_dict() | wire_changes)


def test_grant_wire_is_exact_and_null_is_not_omission():
    current = grant()
    payload = current.to_dict()
    assert "revoked_at" not in payload
    assert PilotGrant.from_dict(payload) == current
    revoked = grant(revoked_at=NOW)
    assert PilotGrant.from_dict(revoked.to_dict()) == revoked
    for key in payload:
        with pytest.raises(SchemaError):
            PilotGrant.from_dict({k: v for k, v in payload.items() if k != key})
        with pytest.raises(SchemaError):
            PilotGrant.from_dict(payload | {key: None})
    for addition in ({"revoked_at": None}, {"approved": True}, {"revision": True}):
        with pytest.raises(SchemaError):
            PilotGrant.from_dict(payload | addition)


def test_grant_nested_mutation_is_not_retained_and_lookup_result_is_validated():
    resources = ["chat-1"]
    audience = [ACTOR]
    current = grant(resource_ids=resources, allowed_audience=audience)
    resources.append("other")
    audience.clear()
    wire = current.to_dict()
    wire["principal"]["actor_id"] = "other"
    assert current.resource_ids == ("chat-1",)
    assert check(current) == current
    object.__setattr__(current, "model_egress_allowed", 1)
    with pytest.raises(SchemaError):
        check(current)
