"""Current trusted configuration and requester ACL intersection; no live providers."""
from dataclasses import replace

import pytest

from ostag.briefs.meeting_models import BudgetProfile, MeetingPurpose, SourceCategory
from ostag.briefs.models import TimeWindow
from ostag.config import Settings
from ostag.core.identity import Principal
from ostag.core.schemas import SchemaError
from tests.test_meeting_contracts import (
    ACTOR, NOW, collection, decision, grant, intake, mapping, repository, request,
)


def test_realistic_two_repo_two_chat_intake_is_explicit_and_authorized():
    result = intake()
    assert result.state == "accepted"
    assert result.reasons == ()
    scope = result.scope
    assert scope.request.repo_ids == ("42", "43")
    assert scope.request.chat_ids == ("oc_1", "oc_2")
    assert len(scope.partitions) == 6
    assert sum(p.required for p in scope.partitions) == 3
    # Two categories + one metadata validation call per repo, plus each chat.
    assert result.estimate.minimum_provider_calls == 8
    assert result.estimate.minimum_collection_seconds == 8
    assert scope.budget.collection_seconds == 120
    confirmation = result.confirmation
    assert confirmation["goal"] == request().goal
    assert confirmation["repositories"][0]["full_name"] == "acme/private-42"
    assert confirmation["audience"] == [ACTOR.to_dict()]
    assert confirmation["window"] == request().window.to_dict()
    assert confirmation["content_hash"] == scope.content_hash
    assert "approval" not in confirmation
    assert type(scope).from_dict(scope.to_dict()) == scope


@pytest.mark.parametrize(
    "overrides",
    [
        {"requester_can_read": lambda *args: False},
        {"requester_can_read": lambda *args: 1},
        {"requester_can_read": lambda *args: {"bot_visible": True}},
        {"requester_can_read": lambda ns, actor, rid: rid != "feishu:chat:oc_2"},
        {"requester_can_read": lambda ns, actor, rid: rid != "github:repo:43"},
        {"grant_lookup": lambda _: grant(model_egress_allowed=False)},
        {
            "grant_lookup": lambda _: grant(
                resource_ids=["github:repo:42", "github:repo:43", "oc_1", "oc_2"]
            )
        },
        {"grant_lookup": lambda _: grant(namespace="other")},
        {"config": Settings(default_permission_level="read")},
        {"channel_grants": ("feishu:ou_alice:read",)},
        {"mapping_lookup": lambda *args: mapping(namespace="other", repositories=[], chats=[])},
        {"mapping_lookup": lambda *args: None},
        {"budget_lookup": lambda _: None},
        {"budget_lookup": lambda _: BudgetProfile("meeting-default", 2)},
        {"budget_lookup": lambda _: BudgetProfile("other", 1)},
    ],
)
def test_intake_denials_do_not_expose_private_names(overrides):
    with pytest.raises(SchemaError) as error:
        intake(**overrides)
    assert "private-" not in str(error.value)
    assert "Private planning" not in str(error.value)
    assert "Private QA" not in str(error.value)


def test_lookup_exception_is_sanitized():
    def broken(*args):
        raise RuntimeError("cannot read acme/private-42 or Private planning")
    for hook in ("mapping_lookup", "repository_lookup", "requester_can_read", "grant_lookup"):
        with pytest.raises(SchemaError) as error:
            intake(**{hook: broken})
        assert "private-42" not in str(error.value)
        assert "Private planning" not in str(error.value)


def test_same_actor_other_platform_requires_separate_mapping_grant_core_and_acl():
    other = Principal("telegram", ACTOR.actor_id)
    req = request(requesting_principal=other, audience=[other])
    with pytest.raises(SchemaError):
        intake(req)
    with pytest.raises(SchemaError):
        intake(req, mapping_lookup=lambda *args: mapping(allowed_requesters=[other]),
               grant_lookup=lambda _: grant(principal=other, allowed_audience=[other]),
               requester_can_read=lambda *args: True)
    with pytest.raises(SchemaError):
        intake(request(namespace="another-tenant"))


def test_every_call_re_resolves_mapping_grant_and_resource_acl():
    current_mapping, current_grant, readable = mapping(), grant(), True
    hooks = dict(mapping_lookup=lambda *args: current_mapping,
                 grant_lookup=lambda _: current_grant,
                 requester_can_read=lambda *args: readable)
    initial = intake(**hooks)
    current_mapping = replace(current_mapping, revision=2)
    with pytest.raises(SchemaError):
        intake(**hooks)
    current_mapping = mapping()
    current_grant = replace(current_grant, revision=2)
    with pytest.raises(SchemaError):
        intake(**hooks)
    current_grant = grant(revoked_at=NOW)
    with pytest.raises(SchemaError):
        intake(**hooks)
    current_grant, readable = grant(), False
    with pytest.raises(SchemaError):
        intake(**hooks)
    assert initial.scope.request.grant_revision == 1  # Snapshot does not turn into authority.


@pytest.mark.parametrize("changed", [
    repository(full_name="acme/renamed"), repository(full_name="other/private-42"),
    repository(branch="release"), repository(default_branch="release"),
    repository(namespace="other"), repository(99), None, {},
])
def test_current_repo_metadata_must_match_frozen_identity_name_owner_and_branches(changed):
    with pytest.raises(SchemaError):
        intake(repository_lookup=lambda ns, rid, branch: changed if rid == "42" else repository(43))


def test_new_repo_is_not_implicitly_selected_even_with_new_mapping_and_grant():
    expanded = mapping(revision=2, repositories=[repository(), repository(43), repository(44)])
    result = intake(
        request(mapping_revision=2),
        mapping_lookup=lambda *args: expanded,
        grant_lookup=lambda _: grant(resource_ids=[*grant().resource_ids, "github:repo:44"]),
    )
    assert [r.repo_id for r in result.scope.repositories] == ["42", "43"]
    assert "private-44" not in str(result.confirmation)
    with pytest.raises(SchemaError):
        intake(request(repo_ids=["42", "43", "44"]))


def test_confirmation_hash_changes_with_execution_relevant_edits():
    original = intake().scope
    requests = [request(goal="Choose a release date"),
                request(window=TimeWindow("2026-09-07T00:00:00Z", request().window.end, "UTC")),
                request(required_partition_ids=["github:repo:42:pulls"]),
                request(chat_ids=["oc_1"]), request(requested_as_of=request().window.end)]
    for changed in requests:
        assert intake(changed).scope.content_hash != original.content_hash
    changed_policy = mapping(name="New team name")
    assert (
        intake(mapping_lookup=lambda *args: changed_policy).scope.content_hash
        != original.content_hash
    )
    assert (
        intake(
            budget_lookup=lambda _: BudgetProfile("meeting-default", 1, conservative_call_seconds=2)
        ).scope.content_hash
        != original.content_hash
    )
    assert (
        intake(grant_lookup=lambda _: grant(expires_at="2026-09-15T00:00:00Z")).scope.content_hash
        != original.content_hash
    )
    other_purpose = request(purpose=MeetingPurpose.DECISION_REVIEW)
    assert (
        intake(
            other_purpose, grant_lookup=lambda _: grant(purpose=other_purpose.purpose.value)
        ).scope.content_hash
        != original.content_hash
    )
    assert intake().scope.content_hash == original.content_hash


def test_future_and_history_policy_windows_are_not_silently_cut_off():
    with pytest.raises(SchemaError):
        intake(request(window=TimeWindow(request().window.start, "2026-09-13T12:00:01Z", "UTC")))
    with pytest.raises(SchemaError):
        intake(request(window=TimeWindow("2026-07-31T23:59:59Z", request().window.end, "UTC")))
    assert (
        intake(request(window=TimeWindow("2026-08-01T00:00:00Z", NOW, "UTC"))).state == "accepted"
    )


def many_repos(count, categories, **budget_changes):
    repos = [repository(n) for n in range(1, count + 1)]
    req = request(repo_ids=[r.repo_id for r in repos], chat_ids=[], source_categories=categories,
                  required_partition_ids=[f"github:repo:1:{categories[0].value}"])
    return intake(
        req,
        mapping_lookup=lambda *args: mapping(repositories=repos, chats=[]),
        repository_lookup=lambda ns, rid, branch: repos[int(rid) - 1],
        requester_can_read=lambda *args: True,
        grant_lookup=lambda _: grant(resource_ids=[f"github:repo:{r.repo_id}" for r in repos]),
        budget_lookup=lambda _: BudgetProfile("meeting-default", 1, **budget_changes),
    )


def test_repo_call_and_time_boundaries_are_global():
    assert many_repos(20, [SourceCategory.PULLS]).state == "accepted"
    over = many_repos(21, [SourceCategory.PULLS])
    assert over.state == "needs_input" and "repo_limit_exceeded" in over.reasons
    exact = many_repos(20, list(SourceCategory), conservative_call_seconds=0.6)
    assert exact.estimate.minimum_provider_calls == 200
    assert exact.estimate.minimum_collection_seconds == 120
    assert exact.state == "accepted"
    slow = many_repos(20, list(SourceCategory), conservative_call_seconds=0.600001)
    assert slow.state == "needs_input" and "collection_time_infeasible" in slow.reasons
    calls = many_repos(20, list(SourceCategory), conservative_call_seconds=0.5,
                       metadata_calls_per_repo=2)
    assert calls.estimate.minimum_provider_calls == 220
    assert calls.state == "needs_input" and "provider_call_limit_exceeded" in calls.reasons
    assert not decision(over.scope).meeting_ready


def test_older_as_of_is_explicitly_unproven_at_intake_and_sealing():
    result = intake(request(requested_as_of=request().window.end))
    assert result.state == "needs_input"
    assert "historical_as_of_unproven" in result.reasons
    sealed = collection(scope=result.scope)
    assert sealed.requested_as_of == request().window.end
    assert sealed.historical_state == "unproven"
    assert not decision(result.scope).meeting_ready
    with pytest.raises(SchemaError):
        intake(request(requested_as_of="2026-09-14T00:00:00Z"))


def test_canonical_provider_ids_cannot_share_a_requester_acl_entry():
    from ostag.briefs.project_mapping import Chat
    req = request(repo_ids=["42"], chat_ids=["42"], required_partition_ids=["github:repo:42:pulls"])
    hooks = dict(mapping_lookup=lambda *args: mapping(repositories=[repository()],
                                                       chats=[Chat("acme", "42", "Numeric chat")]),
                 grant_lookup=lambda _: grant(resource_ids=["github:repo:42", "feishu:chat:42"]))
    with pytest.raises(SchemaError):
        intake(req, **hooks, requester_can_read=lambda ns, actor, rid: rid == "github:repo:42")
    accepted = intake(req, **hooks, requester_can_read=lambda ns, actor, rid: rid in {
        "github:repo:42", "feishu:chat:42"})
    assert accepted.scope.request.resource_ids == ("github:repo:42", "feishu:chat:42")


def test_one_chat_over_exact_200_calls_is_needs_input():
    from ostag.briefs.project_mapping import Chat
    repos = [repository(n) for n in range(1, 21)]
    req = request(
        repo_ids=[r.repo_id for r in repos],
        chat_ids=["oc_1"],
        source_categories=list(SourceCategory),
        required_partition_ids=["github:repo:1:pulls"],
    )
    result = intake(
        req,
        mapping_lookup=lambda *args: mapping(
            repositories=repos, chats=[Chat("acme", "oc_1", "Planning")]
        ),
        repository_lookup=lambda ns, rid, branch: repos[int(rid) - 1],
        requester_can_read=lambda *args: True,
        grant_lookup=lambda _: grant(
            resource_ids=[*(r.resource_id for r in repos), "feishu:chat:oc_1"]
        ),
        budget_lookup=lambda _: BudgetProfile("meeting-default", 1, conservative_call_seconds=0.5),
    )
    assert result.estimate.minimum_provider_calls == 201
    assert result.reasons == ("provider_call_limit_exceeded",)


def test_current_core_policy_changes_are_bound_into_confirmation_hash():
    first = intake()
    second = intake(channel_grants=("feishu:ou_alice:admin",))
    assert first.state == second.state == "accepted"
    assert first.scope.content_hash != second.scope.content_hash


def test_chat_only_intake_has_no_implicit_github_categories_or_heads():
    req = request(repo_ids=[], chat_ids=["oc_1"], source_categories=[],
                  required_partition_ids=["feishu:chat:oc_1:messages"])
    result = intake(req)
    assert result.estimate.minimum_provider_calls == 1
    assert len(result.scope.partitions) == 1
    from ostag.briefs.meeting_models import ProviderObservation
    sealed = collection(scope=result.scope, repo_heads=[],
                        observations=[ProviderObservation("feishu:chat:oc_1", NOW)])
    assert sealed.repo_heads == ()
    assert sealed.observation_cutoff == NOW


def test_confirmation_omits_unset_historical_constraint():
    assert "requested_as_of" not in intake().confirmation
    historical = intake(request(requested_as_of=request().window.end))
    assert historical.confirmation["requested_as_of"] == request().window.end
