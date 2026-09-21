from dataclasses import replace
import hashlib
import json

import pytest

from ostag.briefs.meeting_facts import ChatEvidence, combine
from ostag.briefs.meeting_io import CollectionError
from ostag.briefs.meetings import build_meeting
from ostag.briefs.models import SourceRef
from ostag.briefs.service import ManualRequest
from ostag.core.schemas import SchemaError
from test_github_history import AT, NOW, Runner, collect, scope
from test_meeting_contracts import ACTOR, intake, request


def chat_scope():
    frozen = scope()
    return intake(request(chat_ids=["oc_1"], source_categories=frozen.request.source_categories,
                          required_partition_ids=["github:repo:42:pulls", "github:repo:43:pulls",
                                                  "feishu:chat:oc_1:messages"])).scope


def chat(frozen=None, text="Please decide https://github.com/acme/private-42/pull/1"):
    frozen = frozen or chat_scope()
    return ChatEvidence(SourceRef(
        namespace=frozen.request.namespace, source_id="chat:one", resource_id="feishu:chat:oc_1",
        source_kind="feishu_message", resource_version="unknown", window=frozen.request.window,
        event_at=AT, observed_at=NOW, content_sha256=hashlib.sha256(text.encode()).hexdigest(),
        grant_id="pilot", grant_revision=1, collection_platform="feishu", collection_actor_id="ou_alice",
        purpose="meeting-prep", allowed_audience=(ACTOR,), visibility="private", excerpt=text), "blocked")


def test_ordered_distinct_sources_explicit_link_and_three_pending_topics():
    frozen = chat_scope()
    history = collect(frozen=frozen)
    combined = combine(frozen, history, (chat(),))
    assert combined.associations[0]["basis"] == "explicit_link"
    assert combined.associations[0]["identity_join"] is False
    assert len(combined.topics) <= 3
    assert all(t["disposition"] in ("blocked", "pending", "conflicting") for t in combined.topics)
    assert all(t["owner"] == t["due"] == "unknown" for t in combined.topics)
    assert combined.coverage.complete is False
    assert {s.source_kind for s in combined.sources.sources} == {"github_event", "feishu_message"}
    assert combined.facts == tuple(sorted(combined.facts, key=lambda f: (
        f.event_at or f.observed_at, f.resource_id, f.source_id)))


@pytest.mark.parametrize("text", ["alice-dev ou_alice #1", "same project same title",
    "https://github.com/other/repo/pull/1", "https://github.com/acme/private-42/pull/1?external=1"])
def test_no_name_join_or_semantic_url_guess(text):
    frozen = chat_scope()
    combined = combine(frozen, collect(frozen=frozen), (chat(text=text),))
    assert combined.associations[0]["source_ids"] == []
    assert combined.associations[0]["semantic_association"] == "candidate_only"


@pytest.mark.parametrize("patch", [{"grant_revision": 2}, {"namespace": "other"},
    {"resource_id": "feishu:chat:oc_unselected"}, {"purpose": "other"}])
def test_chat_binding_denied_and_redacted(patch):
    frozen = chat_scope()
    with pytest.raises(CollectionError) as error:
        build_meeting(frozen, collect(frozen=frozen), chats=(replace(chat(), source=replace(chat().source, **patch)),))
    assert error.value.__cause__ is None and error.value.__context__ is None
    assert "Please decide" not in str(error.value)


def test_deterministic_revision_changes_on_raw_source_change_and_remains_private():
    frozen = chat_scope()
    history = collect(frozen=frozen)
    first = build_meeting(frozen, history, chats=(chat(),))
    again = build_meeting(frozen, history, chats=(chat(),))
    assert first.markdown == again.markdown and first.manifest_json == again.manifest_json
    changed = build_meeting(frozen, history, chats=(chat(text="Changed commitment"),))
    assert changed.service_input.revision_digest != first.service_input.revision_digest
    manifest = json.loads(first.manifest_json)
    assert manifest["shareable_sources"] == [] and manifest["meeting_ready"] is False
    assert manifest["status"] == "needs_input" and manifest["semantic_review_required"] is True
    assert manifest["render_sha256"] == hashlib.sha256(first.markdown.encode()).hexdigest()
    assert "Evidence gaps" in first.markdown and "Chat commitments" in first.markdown
    assert "Current production deployment: unknown" in first.markdown
    assert "meeting.md" not in repr(first) and "Please decide" not in repr(first.service_input)


def test_manual_brief_service_authority_boundary_not_bypassed():
    frozen = scope()
    package = build_meeting(frozen, collect())
    handoff = package.service_input
    with pytest.raises(SchemaError):
        ManualRequest(frozen.request.namespace, frozen.request.purpose.value, frozen.request.window,
                      frozen.request.grant_id, frozen.request.grant_revision, handoff.coverage,
                      frozen.request.goal, handoff.sources.sources)


def test_changed_repo_source_changes_revision_even_if_display_identical():
    fake = Runner()
    def override(path, params, calls):
        if path.endswith("/releases"):
            row = fake.body(path)[0] | {"body": "new raw body not used as display"}
            return [row]
    original = build_meeting(scope(), collect())
    fake.override = override
    changed = build_meeting(scope(), collect(fake))
    assert original.service_input.revision_digest != changed.service_input.revision_digest


def test_conflicting_versions_retained_but_identical_duplicate_deduplicated():
    from ostag.connectors.github_history import canonical
    history = collect()
    item = next(i for i in history.items if i.kind == "pulls" and i.repo_id == "42")
    duplicate = combine(scope(), replace(history, items=history.items + (item,)))
    baseline = combine(scope(), history)
    assert len(duplicate.facts) == len(baseline.facts)
    changed = json.loads(item.raw_json) | {"title": "Conflicting current title"}
    conflict = combine(scope(), replace(history, items=history.items + (
        replace(item, raw_json=canonical(changed)),)))
    assert len(conflict.facts) > len(baseline.facts)
    assert any(f.status == "conflicting" for f in conflict.facts)
    assert any("conflicting_snapshots" in g for g in conflict.coverage.gaps)


def test_archived_failed_partition_cannot_reintroduce_withheld_content():
    from ostag.briefs.meeting_models import PartitionStatus
    history = collect()
    reports = tuple(replace(r, status=PartitionStatus.FAILED) if ":42:" in r.partition_id else r
                    for r in history.reports)
    with pytest.raises(CollectionError) as error:
        build_meeting(scope(), replace(history, reports=reports))
    assert error.value.__context__ is None
