from __future__ import annotations

from pathlib import Path

import pytest

from ostag.config import load_settings
from ostag.core.permissions import (
    LOCAL_ADMIN_ACTOR,
    ensure_verb_allowed,
    level_allows,
    parse_actor_permissions,
    resolve_permission,
    resolve_principal_permission,
)
from ostag.core.identity import Principal
from ostag.core.schemas import SchemaError


def _config(tmp_path: Path, **overrides: object):
    return load_settings(None, audit_log=tmp_path / "audit.log", **overrides)


def test_parse_actor_permissions_skips_malformed_pairs() -> None:
    mapping = parse_actor_permissions(("u1:admin", "u2:write", "broken", "u3:god", ":read"))

    assert mapping == {"u1": "admin", "u2": "write"}


def test_resolve_permission_uses_config_then_default(tmp_path: Path) -> None:
    config = _config(
        tmp_path,
        actor_permissions=("boss:admin", "dev:write"),
        default_permission_level="read",
    )

    assert resolve_permission(config, "boss") == "admin"
    assert resolve_permission(config, "dev") == "write"
    assert resolve_permission(config, "stranger") == "read"


def test_local_admin_defaults_to_admin_unless_configured(tmp_path: Path) -> None:
    config = _config(tmp_path)
    assert resolve_permission(config, LOCAL_ADMIN_ACTOR) == "admin"

    demoted = _config(tmp_path, actor_permissions=(f"{LOCAL_ADMIN_ACTOR}:read",))
    assert resolve_permission(demoted, LOCAL_ADMIN_ACTOR) == "read"


def test_level_ordering() -> None:
    assert level_allows("admin", "write")
    assert level_allows("write", "propose")
    assert not level_allows("read", "propose")
    assert not level_allows("propose", "write")


def test_read_only_actor_cannot_approve_or_apply() -> None:
    for verb in ("approve", "apply"):
        with pytest.raises(SchemaError) as excinfo:
            ensure_verb_allowed("viewer", "read", verb)
        assert excinfo.value.code == "permission_denied"
        assert "OSTAG_ACTOR_PERMISSIONS" in excinfo.value.recovery_hint

    ensure_verb_allowed("viewer", "read", "status")  # read may check status


def test_propose_actor_can_reject_but_not_approve() -> None:
    ensure_verb_allowed("member", "propose", "reject")
    ensure_verb_allowed("member", "propose", "continue")

    with pytest.raises(SchemaError):
        ensure_verb_allowed("member", "propose", "approve")


def test_unknown_verb_is_rejected() -> None:
    with pytest.raises(SchemaError) as excinfo:
        ensure_verb_allowed("member", "admin", "detonate")
    assert excinfo.value.code == "unsupported_verb"


def test_principal_permissions_do_not_alias_same_actor_across_platforms(tmp_path: Path) -> None:
    config = _config(
        tmp_path,
        actor_permissions=("feishu:same:write",),
        default_permission_level="read",
    )

    assert resolve_principal_permission(config, Principal("feishu", "same")) == "write"
    assert resolve_principal_permission(config, Principal("telegram", "same")) == "read"


@pytest.mark.parametrize("platform", ["slack", "feishu", "lark", "dingtalk", "telegram"])
def test_remote_local_admin_name_never_escalates(tmp_path: Path, platform: str) -> None:
    config = _config(tmp_path, default_permission_level="read")
    assert resolve_principal_permission(config, Principal(platform, LOCAL_ADMIN_ACTOR)) == "read"


def test_remote_permission_resolution_warns_for_legacy_bare_grant(tmp_path: Path) -> None:
    config = _config(tmp_path, actor_permissions=("dev:write",))
    with pytest.warns(UserWarning, match="platform:actor_id:level"):
        assert resolve_principal_permission(config, Principal("feishu", "dev")) == "propose"
