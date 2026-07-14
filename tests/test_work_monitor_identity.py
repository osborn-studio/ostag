from pathlib import Path
import sqlite3

import pytest

from ostag.core.identity import Principal
from ostag.work_monitor.identity import Binding, Destination, IdentityError, IdentityStore


def test_principal_is_platform_scoped_and_frozen() -> None:
    feishu = Principal("feishu", "same-user")
    telegram = Principal("telegram", "same-user")

    assert feishu != telegram
    with pytest.raises(AttributeError):
        feishu.actor_id = "changed"  # type: ignore[misc]
    assert Principal.from_dict(feishu.to_dict()) == feishu
    with pytest.raises(ValueError, match="exactly"):
        Principal.from_dict({"platform": "feishu", "actor_id": "same-user", "role": "admin"})


def test_private_binding_requires_local_admin_or_enrollment_secret(tmp_path: Path) -> None:
    identities = IdentityStore(tmp_path / "monitor.sqlite3", enrollment_secret="join-me")
    owner = Principal("feishu", "ou_owner")
    private = Destination("feishu", "oc_private", "private")

    with pytest.raises(IdentityError, match="enrollment"):
        identities.bind_private(owner=owner, destination=private)

    identities.bind_private(owner=owner, destination=private, enrollment_secret="join-me")
    assert identities.list_bindings(owner) == (private,)
    assert identities.bindings(owner) == (Binding(owner, private),)

    with pytest.raises(IdentityError, match="consumed"):
        identities.bind_private(
            owner=Principal("feishu", "other"),
            destination=Destination("feishu", "other-private", "private"),
            enrollment_secret="join-me",
        )


def test_owner_can_enroll_and_revoke_group_and_revoked_access_is_denied(tmp_path: Path) -> None:
    identities = IdentityStore(tmp_path / "monitor.sqlite3", enrollment_secret="join-me")
    owner = Principal("feishu", "ou_owner")
    private = Destination("feishu", "oc_private", "private")
    group = Destination("feishu", "oc_group", "group")
    identities.bind_private(owner=owner, destination=private, enrollment_secret="join-me")

    identities.enroll_group(owner, group, via=private, scope="project:ostag")
    assert identities.is_owner(owner, group)
    assert identities.bindings(owner) == (
        Binding(owner, group, role="owner", scope="project:ostag"),
        Binding(owner, private),
    )
    group_binding = identities.bindings(owner)[0]
    assert (
        group_binding.destination.platform,
        group_binding.destination.destination_id,
        group_binding.scope,
    ) == ("feishu", "oc_group", "project:ostag")
    identities.revoke(owner, group)

    assert not identities.is_owner(owner, group)
    with pytest.raises(IdentityError, match="owner"):
        identities.enroll_group(
            owner, Destination("feishu", "other", "group"), via=group, scope="project:other"
        )


def test_grants_and_subscriptions_never_cross_platform(tmp_path: Path) -> None:
    identities = IdentityStore(tmp_path / "monitor.sqlite3", enrollment_secret="join-me")
    owner = Principal("feishu", "owner")
    member_feishu = Principal("feishu", "same-user")
    member_telegram = Principal("telegram", "same-user")
    group = Destination("feishu", "group", "group")
    identities.bind_private(
        owner=owner,
        destination=Destination("feishu", "private", "private"),
        enrollment_secret="join-me",
    )
    identities.enroll_group(
        owner, group, via=Destination("feishu", "private", "private"), scope="instance:mac"
    )
    identities.grant(owner, group, member_feishu, "propose")
    identities.subscribe(member_feishu, group, task_id="task-1")

    assert identities.allows(member_feishu, group, "propose")
    assert not identities.allows(member_telegram, group, "propose")
    assert identities.subscriptions(member_feishu, group) == (("", "task-1", 0),)
    assert identities.subscriptions(member_telegram, group) == ()


def test_read_cannot_watch_and_revoke_hides_subscriptions_with_cursor(tmp_path: Path) -> None:
    identities = IdentityStore(tmp_path / "monitor.sqlite3", enrollment_secret="join")
    owner = Principal("feishu", "owner")
    member = Principal("feishu", "member")
    private = Destination("feishu", "private", "private")
    group = Destination("feishu", "group", "group")
    identities.bind_private(owner=owner, destination=private, enrollment_secret="join")
    identities.enroll_group(owner, group, via=private, scope="task:task-1")
    identities.grant(owner, group, member, "read")
    with pytest.raises(IdentityError, match="propose"):
        identities.subscribe(member, group)
    identities.grant(owner, group, member, "propose")
    identities.subscribe(member, group, task_id="task-1", delivery_cursor=7)
    assert identities.subscriptions(member, group) == (("", "task-1", 7),)
    identities.revoke(owner, group)
    assert identities.subscriptions(member, group) == ()


def test_group_enrollment_requires_explicit_authorization_scope(tmp_path: Path) -> None:
    identities = IdentityStore(tmp_path / "monitor.sqlite3", enrollment_secret="join")
    owner = Principal("feishu", "owner")
    private = Destination("feishu", "private", "private")
    identities.bind_private(owner=owner, destination=private, enrollment_secret="join")
    with pytest.raises(IdentityError, match="scope"):
        identities.enroll_group(
            owner, Destination("feishu", "group", "group"), scope="project:ostag"
        )

    with pytest.raises(IdentityError, match="scope"):
        identities.enroll_group(
            owner, Destination("feishu", "group", "group"), via=private, scope=""
        )


def test_local_admin_private_binding_persists_admin_role(tmp_path: Path) -> None:
    identities = IdentityStore(tmp_path / "monitor.sqlite3")
    admin = Principal("feishu", "admin")
    private = Destination("feishu", "private", "private")
    identities.bind_private(owner=admin, destination=private, local_admin=True)
    assert identities.bindings(admin) == (Binding(admin, private, role="admin"),)


def test_owner_and_group_reader_permission_matrix(tmp_path: Path) -> None:
    identities = IdentityStore(tmp_path / "monitor.sqlite3")
    owner = Principal("feishu", "owner")
    reader = Principal("feishu", "reader")
    private = Destination("feishu", "private", "private")
    group = Destination("feishu", "group", "group")
    identities.bind_private(owner=owner, destination=private, local_admin=True)
    identities.enroll_group(owner, group, scope="all", local_admin=True)
    identities.grant(owner, group, reader, "read")

    assert identities.allows(owner, private, "read")
    assert identities.allows(owner, private, "propose")
    assert identities.allows(owner, private, "write")
    assert identities.allows(reader, group, "read")
    assert not identities.allows(reader, group, "propose")
    assert not identities.allows(reader, group, "write")
    assert identities.allowed_instances(owner) is None
    assert identities.allowed_instances(reader) == frozenset()


def test_non_owner_instance_scope_never_expands_to_other_employee_instances(tmp_path: Path) -> None:
    identities = IdentityStore(tmp_path / "monitor.sqlite3")
    owner = Principal("feishu", "owner")
    member = Principal("feishu", "member")
    private = Destination("feishu", "private", "private")
    group = Destination("feishu", "group", "group")
    identities.bind_private(owner=owner, destination=private, local_admin=True)
    identities.enroll_group(owner, group, scope="instance:employee-a", local_admin=True)
    identities.grant(owner, group, member, "write")

    assert identities.is_global_owner(owner)
    assert not identities.is_global_owner(member)
    assert identities.allowed_instances(member) == frozenset({"employee-a"})


def test_legacy_binding_schema_migrates_scope_column(tmp_path: Path) -> None:
    path = tmp_path / "monitor.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE monitor_bindings (platform TEXT NOT NULL, destination_id TEXT NOT NULL, "
            "chat_type TEXT NOT NULL, owner_platform TEXT NOT NULL, owner_actor_id TEXT NOT NULL, "
            "revoked INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(platform,destination_id))"
        )
    IdentityStore(path)
    with sqlite3.connect(path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(monitor_bindings)")}
    assert {"role", "scope"} <= columns


def test_group_binding_cannot_be_stolen_by_another_owner(tmp_path: Path) -> None:
    identities = IdentityStore(tmp_path / "monitor.sqlite3", enrollment_secret="join")
    first = Principal("feishu", "first")
    second = Principal("feishu", "second")
    first_dm = Destination("feishu", "first-dm", "private")
    second_dm = Destination("feishu", "second-dm", "private")
    group = Destination("feishu", "group", "group")
    identities.bind_private(owner=first, destination=first_dm, enrollment_secret="join")
    identities.bind_private(owner=second, destination=second_dm, local_admin=True)
    identities.enroll_group(first, group, via=first_dm, scope="all")
    with pytest.raises(IdentityError, match="another owner"):
        identities.enroll_group(second, group, via=second_dm, scope="project:other")
    assert identities.binding(group) == Binding(first, group, scope="all")
    identities.enroll_group(second, group, scope="project:other", local_admin=True)
    assert identities.binding(group) == Binding(second, group, role="admin", scope="project:other")


def test_subscription_cursor_is_nonnegative_monotonic_and_atomically_advanced(tmp_path: Path) -> None:
    identities = IdentityStore(tmp_path / "monitor.sqlite3")
    owner = Principal("feishu", "owner")
    private = Destination("feishu", "private", "private")
    identities.bind_private(owner=owner, destination=private, local_admin=True)
    with pytest.raises(ValueError, match="non-negative"):
        identities.subscribe(owner, private, delivery_cursor=-1)
    identities.subscribe(owner, private, task_id="task", delivery_cursor=8)
    identities.subscribe(owner, private, task_id="task")
    assert identities.subscriptions(owner, private) == (("", "task", 8),)
    assert identities.advance_subscription_cursor(
        owner, private, task_id="task", delivery_cursor=5
    ) == 8
    assert identities.advance_subscription_cursor(
        owner, private, task_id="task", delivery_cursor=11
    ) == 11
