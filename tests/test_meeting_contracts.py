"""Synthetic M01 contracts: dates, immutable scope, sealing and decision behavior."""
from dataclasses import FrozenInstanceError, replace
from zoneinfo import ZoneInfoNotFoundError

import pytest

import ostag.briefs.meeting_models as meeting_models
from ostag.briefs.grants import PilotGrant
from ostag.briefs.meeting_models import (
    BudgetProfile, CollectionSeal, MeetingPurpose, MeetingRequest, PartitionReport,
    PartitionStatus, ProviderObservation, RepoHead, ResultState, SourceCategory,
    decide_result, local_time_window, seal_collection,
)
from ostag.briefs.models import TimeWindow
from ostag.briefs.project_mapping import (
    Chat, PersonMapping, ProjectMapping, Repository, freeze_meeting_scope,
)
from ostag.config import Settings
from ostag.core.identity import Principal
from ostag.core.schemas import SchemaError

ACTOR = Principal("feishu", "ou_alice")
NOW = "2026-09-13T12:00:00Z"
WINDOW = TimeWindow("2026-09-06T00:00:00Z", "2026-09-13T00:00:00Z", "Asia/Shanghai")
HISTORY = TimeWindow("2026-08-01T00:00:00Z", "2026-10-01T00:00:00Z", "UTC")


def repository(number=42, **changes):
    return Repository(**(dict(namespace="acme", repo_id=str(number),
                              full_name=f"acme/private-{number}", branch="main",
                              default_branch="main") | changes))


def mapping(**changes):
    return ProjectMapping(**(dict(
        namespace="acme", mapping_id="release-team", revision=1, name="Release team",
        allowed_requesters=[ACTOR], repositories=[repository(), repository(43)],
        chats=[Chat("acme", "oc_1", "Private planning"), Chat("acme", "oc_2", "Private QA")],
        people=[PersonMapping("ou_alice", "7", "alice-dev")], history_window=HISTORY,
    ) | changes))


def request(**changes):
    return MeetingRequest(**(dict(
        namespace="acme", requesting_principal=ACTOR, goal="Decide whether to open the pilot",
        purpose=MeetingPurpose.PROJECT_REVIEW, mapping_id="release-team", mapping_revision=1,
        window=WINDOW, repo_ids=["42", "43"], chat_ids=["oc_1", "oc_2"],
        source_categories=[SourceCategory.PULLS, SourceCategory.ISSUES],
        required_partition_ids=["github:repo:42:pulls", "github:repo:43:pulls",
                                "feishu:chat:oc_1:messages"],
        audience=[ACTOR], shareable_sources=[], grant_id="pilot", grant_revision=1,
        budget_profile_id="meeting-default", budget_profile_revision=1,
    ) | changes))


# Shared synthetic setup stays here; imports between test modules are one-way.
def grant(**changes):
    # Issued today, but explicitly allows reading the mapping's older history.
    return PilotGrant(**(dict(
        grant_id="pilot", namespace="acme", principal=ACTOR,
        resource_ids=["github:repo:42", "github:repo:43", "feishu:chat:oc_1", "feishu:chat:oc_2"],
        purpose="meeting-prep", allowed_audience=[ACTOR], model_egress_allowed=True,
        valid_from="2026-09-13T00:00:00Z", expires_at="2026-09-14T00:00:00Z", revision=1,
    ) | changes))


def intake(req=None, **changes):
    current_mapping = mapping()
    metadata = {r.repo_id: r for r in current_mapping.repositories}
    requester_acl = {(ACTOR, rid) for rid in grant().resource_ids}
    values = dict(
        request=request() if req is None else req,
        mapping_lookup=lambda namespace, mapping_id: current_mapping,
        repository_lookup=lambda namespace, repo_id, branch: metadata.get(repo_id),
        requester_can_read=lambda namespace, principal, rid: (principal, rid) in requester_acl,
        budget_lookup=lambda profile_id: BudgetProfile("meeting-default", 1),
        grant_lookup=lambda grant_id: grant(),
        config=Settings(
            default_permission_level="read", actor_permissions=("feishu:ou_alice:propose",)
        ),
        now=NOW,
    )
    return freeze_meeting_scope(**(values | changes))


def test_request_roundtrip_and_mutable_inputs_are_copied():
    ids = ["42", "43"]
    value = request(repo_ids=ids)
    ids.append("44")
    wire = value.to_dict()
    assert MeetingRequest.from_dict(wire) == value
    wire["requesting_principal"]["actor_id"] = "outsider"
    wire["repo_ids"].clear()
    assert value.repo_ids == ("42", "43")
    assert value.requesting_principal == ACTOR
    with pytest.raises(FrozenInstanceError):
        value.goal = "Different goal"
    assert "requested_as_of" not in value.to_dict()
    older = request(requested_as_of=WINDOW.end)
    assert MeetingRequest.from_dict(older.to_dict()) == older


@pytest.mark.parametrize("changes", [
    {"goal": " "}, {"goal": "x" * 4001}, {"purpose": "meeting-prep"},
    {"mapping_revision": True}, {"grant_revision": 0}, {"budget_profile_revision": False},
    {"requesting_principal": {}}, {"window": {}}, {"repo_ids": ["42", "42"]},
    {"repo_ids": [], "chat_ids": []}, {"repo_ids": ["github:repo:42"]},
    {"repo_ids": ["042"]}, {"chat_ids": ["feishu:chat:oc_1"]},
    {"source_categories": []}, {"source_categories": [SourceCategory.PULLS] * 2},
    {"source_categories": ["pulls"]}, {"required_partition_ids": []},
    {"required_partition_ids": ["github:repo:99:pulls"]},
    {"required_partition_ids": ["github:repo:42:releases"]},
    {"audience": [ACTOR, Principal("feishu", "ou_bob")]},
    {"audience": [Principal("telegram", "ou_alice")]},
    {"shareable_sources": ["github:repo:42"]}, {"requested_as_of": "last Friday"},
])
def test_direct_request_rejects_malformed_or_implicit_scope(changes):
    with pytest.raises(SchemaError):
        request(**changes)


def test_wire_keys_are_exact_and_null_is_not_omission():
    value = request().to_dict()
    for key in value:
        with pytest.raises(SchemaError):
            MeetingRequest.from_dict({k: v for k, v in value.items() if k != key})
        with pytest.raises(SchemaError):
            MeetingRequest.from_dict(value | {key: None})
    for extra in ({"approved": True}, {"requested_as_of": None}, {"budget_profile": {}}):
        with pytest.raises(SchemaError):
            MeetingRequest.from_dict(value | extra)


def test_mapping_roundtrip_and_no_display_name_identity_inference():
    people = [PersonMapping("ou_alice", "7", "alice-dev")]
    value = mapping(people=people)
    people.clear()
    assert ProjectMapping.from_dict(value.to_dict()) == value
    assert len(value.people) == 1
    for person in [PersonMapping("ou_alice", "8", "bob"),
                   PersonMapping("ou_bob", "7", "alice-dev"),
                   PersonMapping("ou_bob", "8", "ALICE-dev"), value.people[0]]:
        with pytest.raises(SchemaError):
            mapping(people=[*value.people, person])
    with pytest.raises(SchemaError):
        PersonMapping.from_dict({"open_id": "ou_alice", "github_user_id": "7",
                                 "github_login": "alice-dev", "display_name": "Alice"})


@pytest.mark.parametrize("changes", [
    {"repositories": [repository(), repository()]},
    {"repositories": [repository(), repository(43, full_name="ACME/PRIVATE-42")]},
    {"repositories": [repository(namespace="other")]},
    {"chats": [Chat("other", "oc_1", "Secret")]},
    {"chats": [Chat("acme", "oc_1", "A"), Chat("acme", "oc_1", "B")]},
    {"allowed_requesters": [ACTOR, ACTOR]}, {"allowed_requesters": []},
    {"revision": True}, {"history_window": {}}, {"people": [{}]},
])
def test_mapping_rejects_conflicts_and_cross_namespace_inputs(changes):
    with pytest.raises(SchemaError):
        mapping(**changes)


@pytest.mark.parametrize("start,end,zone", [
    ("yesterday", "today", "UTC"),
    ("2026-09-12T12:00:00", "2026-09-13T12:00:00", "UTC"),
    ("2026-03-08T02:30:00-08:00", "2026-03-08T04:00:00-07:00", "America/Los_Angeles"),
    ("2026-09-12T12:00:00-08:00", "2026-09-13T12:00:00-08:00", "America/Los_Angeles"),
    ("2026-11-01T01:30:00", "2026-11-01T03:00:00-08:00", "America/Los_Angeles"),
    ("2026-09-12T12:00:00Z", "2026-09-13T12:00:00Z", "local"),
])
def test_local_window_rejects_relative_naive_dst_gap_and_wrong_offset(start, end, zone):
    with pytest.raises(SchemaError):
        local_time_window(start, end, zone)


def test_dst_fold_offsets_and_cross_midnight_are_preserved_as_utc_interval():
    fold = local_time_window("2026-11-01T01:30:00-07:00", "2026-11-01T01:30:00-08:00",
                             "America/Los_Angeles")
    assert fold.start == "2026-11-01T08:30:00Z"
    assert fold.end == "2026-11-01T09:30:00Z"
    assert fold.display_timezone == "America/Los_Angeles"
    midnight = local_time_window("2026-09-12T23:30:00+08:00", "2026-09-13T00:30:00+08:00",
                                 "Asia/Shanghai")
    assert midnight.start == "2026-09-12T15:30:00Z"
    assert midnight.end == "2026-09-12T16:30:00Z"


@pytest.mark.parametrize("changes", [
    {"max_repos": 21}, {"max_github_facts": 501}, {"max_feishu_messages": 501},
    {"max_provider_calls": 201}, {"max_model_input_bytes": 1048577},
    {"overall_seconds": 181}, {"collection_seconds": 121}, {"generation_reserve_seconds": 61},
    {"overall_seconds": 100}, {"conservative_call_seconds": float("nan")},
    {"conservative_call_seconds": float("inf")}, {"conservative_call_seconds": -1},
    {"conservative_call_seconds": 0}, {"conservative_call_seconds": True},
    {"max_repos": True}, {"max_provider_calls": -1}, {"revision": False},
    {"metadata_calls_per_repo": 0},
])
def test_budget_is_finite_bounded_global_and_consistent(changes):
    with pytest.raises(SchemaError):
        BudgetProfile(**(dict(profile_id="meeting-default", revision=1) | changes))


def test_default_budget_roundtrip():
    budget = BudgetProfile("meeting-default", 1)
    assert BudgetProfile.from_dict(budget.to_dict()) == budget
    assert (budget.max_repos, budget.max_github_facts, budget.max_feishu_messages,
            budget.max_provider_calls, budget.max_model_input_bytes) == (20, 500, 500, 200, 1048576)
    assert budget.collection_seconds + budget.generation_reserve_seconds == budget.overall_seconds


def frozen_scope():
    return intake().scope


def reports(scope, status=PartitionStatus.COMPLETE):
    return [PartitionReport(p.partition_id, status) for p in scope.partitions]


def decision(scope=None, **changes):
    scope = frozen_scope() if scope is None else scope
    values = dict(scope=scope, timing=execution_timing(scope), reports=reports(scope),
                  now=NOW, cancelled=False,
                  grant_current=True, lease_current=True, fence_current=True, finished=True,
                  model_valid=True, key_claims_valid=True, unsupported_required_question=False)
    return decide_result(**(values | changes))


def test_decision_is_bound_to_complete_manifest_with_visible_optional_gaps():
    scope = frozen_scope()
    assert decision(scope).state is ResultState.DRAFT_READY
    required_only = [
        r for r in reports(scope) if r.partition_id in scope.request.required_partition_ids
    ]
    result = decision(scope, reports=required_only)
    assert result.meeting_ready and result.can_commit
    assert set(result.gaps) == {p.partition_id for p in scope.partitions if not p.required}
    assert decision(scope, reports=[]).state is ResultState.NEEDS_INPUT
    assert not decision(scope, reports=[]).meeting_ready
    for status in (PartitionStatus.MISSING, PartitionStatus.PARTIAL, PartitionStatus.FAILED):
        altered = reports(scope)
        altered[0] = replace(altered[0], status=status)
        assert decision(scope, reports=altered).state is ResultState.NEEDS_INPUT


@pytest.mark.parametrize("changes,state,commit,reasons", [
    ({"cancelled": True, "grant_current": False}, ResultState.CANCELLED, True, ("cancelled",)),
    ({"grant_current": False}, ResultState.NEEDS_INPUT, True, ("authorization_revoked",)),
    ({"lease_current": False}, ResultState.STALE_ATTEMPT, False, ("stale_lease_or_fence",)),
    ({"fence_current": False}, ResultState.STALE_ATTEMPT, False, ("stale_lease_or_fence",)),
    ({"cancelled": True, "fence_current": False}, ResultState.CANCELLED, False, ("cancelled",)),
    ({"grant_current": False, "lease_current": False}, ResultState.NEEDS_INPUT, False,
     ("authorization_revoked",)),
    ({"now": "2026-09-13T12:03:00Z", "finished": False}, ResultState.FAILED, True,
     ("deadline_exceeded",)),
    ({"now": "2026-09-13T12:03:00Z", "finished": False,
      "fence_current": False}, ResultState.STALE_ATTEMPT, False, ("stale_lease_or_fence",)),
    ({"now": "2026-09-13T13:00:00Z", "finished": True,
      "fence_current": False}, ResultState.STALE_ATTEMPT, False, ("stale_lease_or_fence",)),
    ({"now": "2026-09-13T13:00:00Z", "cancelled": True,
      "fence_current": False}, ResultState.CANCELLED, False, ("cancelled",)),
    ({"now": "2026-09-13T13:00:00Z", "grant_current": False},
     ResultState.NEEDS_INPUT, True, ("authorization_revoked",)),
    ({"model_valid": False}, ResultState.FAILED, True, ("model_validation_failed",)),
    ({"key_claims_valid": False}, ResultState.NEEDS_INPUT, True, ("key_claims_unproven",)),
    ({"unsupported_required_question": True}, ResultState.NEEDS_INPUT, True,
     ("unsupported_required_question",)),
])
def test_result_precedence(changes, state, commit, reasons):
    result = decision(**changes)
    assert result.state is state
    assert result.can_commit is commit
    assert not result.meeting_ready
    assert result.reasons == reasons


def test_deadline_is_frozen_and_equality_rejects_any_new_ready_commit():
    scope = frozen_scope()
    timing = execution_timing(scope)
    assert timing.hard_deadline == "2026-09-13T12:03:00Z"
    assert not decision(scope, now=timing.hard_deadline).meeting_ready
    assert decision(scope, now="2026-09-13T12:02:59.999999Z").meeting_ready
    assert not decision(scope, now="2026-09-13T12:02:59Z", finished=False).meeting_ready
    restored = type(timing).from_dict(timing.to_dict())
    assert restored.hard_deadline == timing.hard_deadline
    expired = decision(restored.scope, timing=restored, now=timing.hard_deadline, finished=False)
    assert expired.reasons == ("deadline_exceeded",)


@pytest.mark.parametrize("bad", [
    [PartitionReport("github:repo:999:pulls", PartitionStatus.COMPLETE)],
    [PartitionReport("github:repo:42:pulls", PartitionStatus.COMPLETE)] * 2,
    [{}], None,
])
def test_invalid_reports_fail_closed(bad):
    with pytest.raises(SchemaError):
        decision(reports=bad)


def test_decision_flags_require_actual_booleans():
    for name in ("cancelled", "grant_current", "lease_current", "fence_current", "finished",
                 "model_valid", "key_claims_valid", "unsupported_required_question"):
        with pytest.raises(SchemaError):
            decision(**{name: 1})


def collection(**changes):
    scope = changes.get("scope", frozen_scope())
    values = dict(scope=scope, timing=execution_timing(scope), collection_started_at=NOW,
                  collection_finished_at="2026-09-13T12:00:30Z", now="2026-09-13T12:00:30Z",
                  observations=[ProviderObservation("github:repo:42", "2026-09-13T12:00:04Z"),
                                ProviderObservation("feishu:chat:oc_1", "2026-09-13T12:00:20Z")],
                  repo_heads=[RepoHead(repository(), "a" * 40, "2026-09-13T12:00:02Z"),
                              RepoHead(repository(43), "b" * 64, "2026-09-13T12:00:25Z")])
    return seal_collection(**(values | changes))


def test_seal_uses_actual_max_observation_and_binds_heads_not_atomic_history():
    sealed = collection()
    assert sealed.observation_cutoff == "2026-09-13T12:00:25Z"
    assert sealed.scope_hash == frozen_scope().content_hash
    assert CollectionSeal.from_dict(sealed.to_dict()) == sealed
    assert sealed.historical_state == "current_observations_only"
    assert sealed.requested_as_of is None


@pytest.mark.parametrize("changes", [
    {"observations": []}, {"repo_heads": []},
    {"observations": [ProviderObservation("feishu:chat:oc_1", "2026-09-13T11:59:59Z")]},
    {"observations": [ProviderObservation("feishu:chat:oc_1", "2026-09-13T12:00:31Z")]},
    {"observations": [ProviderObservation("feishu:chat:unknown", NOW)]},
    {"collection_finished_at": "2026-09-13T12:00:31Z"},
    {"collection_started_at": "2026-09-13T12:00:31Z"},
    {"repo_heads": [RepoHead(repository(branch="dev"), "a" * 40, NOW),
                    RepoHead(repository(43), "b" * 40, NOW)]},
    {"repo_heads": [RepoHead(repository(), "a" * 40, NOW)] * 2},
])
def test_seal_rejects_time_resource_and_missing_head_mismatches(changes):
    with pytest.raises(SchemaError):
        collection(**changes)


@pytest.mark.parametrize("sha", ["main", "a" * 39, "g" * 40, True])
def test_heads_require_full_sha(sha):
    with pytest.raises(SchemaError):
        RepoHead(repository(), sha, NOW)


def test_serialized_outputs_include_verified_manifest_estimates_and_observation_cutoff():
    outcome = intake()
    wire = outcome.to_dict()
    assert wire["state"] == "accepted"
    assert wire["reasons"] == []
    assert wire["estimate"]["minimum_provider_calls"] == 8
    assert wire["scope"]["partitions"] == [p.to_dict() for p in outcome.scope.partitions]
    assert wire["scope"]["budget"]["overall_seconds"] == 180
    assert type(outcome).from_dict(wire) == outcome
    sealed = collection()
    assert sealed.to_dict()["observation_cutoff"] == "2026-09-13T12:00:25Z"
    assert sealed.to_dict()["scope_hash"] == outcome.scope.content_hash
    assert sealed.to_dict()["historical_state"] == "current_observations_only"
    for key, bad in (("state", "draft_ready"), ("reasons", ["invented"]),
                     ("estimate", {"minimum_provider_calls": 1, "minimum_collection_seconds": 1})):
        with pytest.raises(SchemaError):
            type(outcome).from_dict(wire | {key: bad})
    for key, bad in (("observation_cutoff", NOW), ("scope_hash", "0" * 64),
                     ("historical_state", "verified"), ("requested_as_of", None)):
        with pytest.raises(SchemaError):
            CollectionSeal.from_dict(sealed.to_dict() | {key: bad})


def test_restored_scope_cannot_override_derived_partition_manifest_or_deadline():
    scope = frozen_scope()
    wire = scope.to_dict()
    for changed in (
        {"partitions": []},
        {"hard_deadline": "2026-09-13T13:00:00Z"},
        {"contract_version": "unknown"},
        {"estimate": {"minimum_provider_calls": True, "minimum_collection_seconds": 8}},
    ):
        with pytest.raises(SchemaError):
            type(scope).from_dict(wire | changed)


@pytest.mark.parametrize("start,end,zone", [
    ("0001-01-01T00:00:00+14:00", "0001-01-02T00:00:00+14:00", "Pacific/Kiritimati"),
    ("9999-12-30T23:00:00-12:00", "9999-12-31T23:00:00-12:00", "Etc/GMT+12"),
])
def test_local_date_overflow_is_a_schema_error(start, end, zone):
    with pytest.raises(SchemaError):
        local_time_window(start, end, zone)


def test_all_public_records_reject_unknown_missing_and_null_wire_fields():
    scope = frozen_scope()
    records = [
        repository(),
        mapping().chats[0],
        mapping().people[0],
        mapping(),
        BudgetProfile("meeting-default", 1),
        scope.estimate,
        scope.partitions[0],
        scope,
        execution_timing(scope),
        intake(),
        ProviderObservation("github:repo:42", NOW),
        RepoHead(repository(), "a" * 40, NOW),
        collection(),
        reports(scope)[0],
        decision(scope),
    ]
    for record in records:
        payload = record.to_dict()
        assert type(record).from_dict(payload) == record
        with pytest.raises(SchemaError):
            type(record).from_dict(payload | {"extra": True})
        for key in payload:
            with pytest.raises(SchemaError):
                type(record).from_dict({k: v for k, v in payload.items() if k != key})
            with pytest.raises(SchemaError):
                type(record).from_dict(payload | {key: None})


def test_exact_observation_bounds_and_fractional_order_are_supported():
    sealed = collection(
        observations=[
            ProviderObservation("feishu:chat:oc_1", NOW),
            ProviderObservation("feishu:chat:oc_2", "2026-09-13T12:00:30Z"),
            ProviderObservation("github:repo:42", "2026-09-13T12:00:30.000001Z"),
        ],
        collection_finished_at="2026-09-13T12:00:30.000001Z",
        now="2026-09-13T12:00:30.000001Z",
    )
    assert sealed.observation_cutoff == "2026-09-13T12:00:30.000001Z"


def test_seal_rejects_head_observation_outside_interval_and_wrong_full_name():
    for head in (RepoHead(repository(), "a" * 40, "2026-09-13T12:00:31Z"),
                 RepoHead(repository(full_name="other/transferred"), "a" * 40, NOW)):
        with pytest.raises(SchemaError):
            collection(repo_heads=[head, RepoHead(repository(43), "b" * 40, NOW)])


def test_frozen_nested_inputs_and_output_projections_are_detached():
    scope = frozen_scope()
    wire = scope.to_dict()
    wire["repositories"][0]["branch"] = "release"
    wire["budget"]["max_github_facts"] = 500000
    wire["request"]["audience"].clear()
    assert scope.repositories[0].branch == "main"
    assert scope.budget.max_github_facts == 500
    assert scope.request.audience == (ACTOR,)
    with pytest.raises(FrozenInstanceError):
        scope.budget.max_github_facts = 501
    with pytest.raises(FrozenInstanceError):
        scope.repositories[0].branch = "dev"


@pytest.mark.parametrize("error", [OSError, ZoneInfoNotFoundError, ValueError])
def test_timezone_construction_failures_are_schema_errors(monkeypatch, error):
    # The cached registry can outlive a removed, unreadable or invalid tzdata file.
    def unavailable(key):
        raise error(key)

    monkeypatch.setattr(meeting_models, "ZoneInfo", unavailable)
    with pytest.raises(SchemaError):
        local_time_window("2026-09-12T12:00:00+08:00", "2026-09-13T12:00:00+08:00",
                          "Asia/Shanghai")


@pytest.mark.parametrize("now", [
    "2026-09-13T12:03:00Z", "2026-09-13T12:03:00.000001Z", "2026-09-13T13:00:00Z",
])
def test_finished_flag_cannot_authorize_a_new_ready_commit_at_or_after_deadline(now):
    result = decision(now=now, finished=True)
    assert result.state is ResultState.FAILED
    assert not result.meeting_ready
    assert result.reasons == ("deadline_exceeded",)
    assert result.can_commit  # Current owner may record sanitized failure status.
    assert result.gaps == ()


@pytest.mark.parametrize("finish", ["2026-09-13T12:02:00.000001Z", "2026-09-13T12:10:00Z"])
def test_collection_cannot_run_past_its_frozen_ceiling(finish):
    with pytest.raises(SchemaError):
        collection(collection_finished_at=finish, now=finish)


def test_confirmation_has_durations_without_starting_execution_clock():
    result = intake()
    assert "hard_deadline" not in result.confirmation
    assert "collection_deadline" not in result.confirmation
    assert "hard_deadline" not in result.scope.to_dict()
    assert "collection_deadline" not in result.scope.to_dict()
    assert result.confirmation["budget"]["overall_seconds"] == 180
    assert result.confirmation["budget"]["collection_seconds"] == 120
    assert result.confirmation["budget"]["generation_reserve_seconds"] == 60


def execution_timing(scope=None, first_claimed_at=NOW):
    return meeting_models.ExecutionTiming(
        frozen_scope() if scope is None else scope, first_claimed_at)


def test_first_claim_excludes_confirmation_and_queue_wait_from_execution():
    scope = frozen_scope()
    timing = execution_timing(scope, "2026-09-13T12:04:10Z")
    assert timing.collection_deadline == "2026-09-13T12:06:10Z"
    assert timing.hard_deadline == "2026-09-13T12:07:10Z"
    assert timing.scope.content_hash == scope.content_hash
    sealed = collection(
        scope=scope, timing=timing, collection_started_at=timing.first_claimed_at,
        collection_finished_at="2026-09-13T12:04:30Z", now="2026-09-13T12:04:30Z",
        observations=[ProviderObservation("feishu:chat:oc_1", "2026-09-13T12:04:25Z")],
        repo_heads=[RepoHead(repository(), "a" * 40, "2026-09-13T12:04:20Z"),
                    RepoHead(repository(43), "b" * 40, "2026-09-13T12:04:21Z")])
    assert sealed.timing == timing
    assert sealed.observation_cutoff == "2026-09-13T12:04:25Z"
    assert decision(scope, timing=timing, now=sealed.sealed_at).meeting_ready


def test_retry_restores_first_claim_and_job_budget_without_reintake():
    timing = execution_timing(first_claimed_at="2026-09-13T12:04:10Z")
    restored = type(timing).from_dict(timing.to_dict())
    assert restored == timing
    assert restored.scope.budget == timing.scope.budget
    assert restored.scope.intake_at == NOW
    assert decision(
        restored.scope, timing=restored, now="2026-09-13T12:07:09.999999Z"
    ).meeting_ready
    expired = decision(restored.scope, timing=restored, now="2026-09-13T12:07:10Z")
    assert expired.reasons == ("deadline_exceeded",)
    assert not expired.meeting_ready
    with pytest.raises(FrozenInstanceError):
        restored.first_claimed_at = "2026-09-13T12:07:10Z"


@pytest.mark.parametrize("first_claimed_at", [
    None, True, "today", "2026-09-13T12:04:10", "2026-09-13T11:59:59Z",
    "9999-12-31T23:59:59Z",
])
def test_execution_timing_rejects_invalid_anchor_and_calendar_overflow(first_claimed_at):
    with pytest.raises(SchemaError):
        execution_timing(first_claimed_at=first_claimed_at)


def test_execution_timing_rejects_wrong_nested_types_and_restored_deadline_overrides():
    timing = execution_timing()
    with pytest.raises(SchemaError):
        meeting_models.ExecutionTiming(timing.scope.to_dict(), NOW)
    wire = timing.to_dict()
    for changed in ({"first_claimed_at": "2026-09-13T12:01:00Z"},
                    {"collection_deadline": "2026-09-13T13:00:00Z"},
                    {"hard_deadline": "2026-09-13T13:00:00Z"}, {"scope": {}}):
        with pytest.raises(SchemaError):
            type(timing).from_dict(wire | changed)


def test_timing_cannot_be_reused_for_edited_scope_or_budget():
    scope = frozen_scope()
    timing = execution_timing(scope)
    edited = replace(scope, request=replace(scope.request, goal="A different decision"))
    reduced = replace(scope, budget=replace(scope.budget, collection_seconds=60,
                                           overall_seconds=120))
    for other in (edited, reduced):
        with pytest.raises(SchemaError):
            decision(other, timing=timing)
        with pytest.raises(SchemaError):
            collection(scope=other, timing=timing)


@pytest.mark.parametrize("bad_timing", [None, {}, True])
def test_decision_and_seal_require_strict_typed_execution_timing(bad_timing):
    with pytest.raises(SchemaError):
        decision(timing=bad_timing)
    with pytest.raises(SchemaError):
        collection(timing=bad_timing)


def test_decision_and_collection_cannot_precede_first_claim():
    timing = execution_timing(first_claimed_at="2026-09-13T12:00:01Z")
    with pytest.raises(SchemaError):
        decision(timing=timing, now=NOW)
    with pytest.raises(SchemaError):
        collection(timing=timing)


def test_collection_ceiling_equality_is_supported_but_hard_deadline_is_exclusive():
    timing = execution_timing()
    sealed = collection(timing=timing, collection_finished_at=timing.collection_deadline,
                        now=timing.collection_deadline)
    assert sealed.collection_finished_at == "2026-09-13T12:02:00Z"
    for now in (timing.hard_deadline, "2026-09-13T12:03:00.000001Z"):
        with pytest.raises(SchemaError):
            collection(timing=timing, now=now)


def test_late_retry_collection_cannot_reset_the_collection_ceiling():
    timing = execution_timing()
    with pytest.raises(SchemaError):
        collection(timing=timing, collection_started_at="2026-09-13T12:01:50Z",
                   collection_finished_at="2026-09-13T12:02:10Z", now="2026-09-13T12:02:10Z",
                   observations=[ProviderObservation("feishu:chat:oc_1", "2026-09-13T12:02:05Z")],
                   repo_heads=[RepoHead(repository(), "a" * 40, "2026-09-13T12:01:55Z"),
                               RepoHead(repository(43), "b" * 40, "2026-09-13T12:02:00Z")])
