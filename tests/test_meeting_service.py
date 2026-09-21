"""Synthetic trusted-host contracts; fixtures are never live acceptance."""
from dataclasses import replace
from datetime import datetime, timedelta
import hashlib
import json
from types import SimpleNamespace

import pytest

from ostag.briefs.authority import AuthoritySnapshot, TrustedBriefAuthority
from ostag.briefs.generation import GenerationConfig
from ostag.briefs.lifecycle import BriefLifecycle
from ostag.briefs.meeting_models import PartitionReport, PartitionStatus, SourceCategory
from ostag.briefs.meeting_service import DurableMeetingRequest
from ostag.briefs.meetings import _build, build_meeting
from ostag.briefs.retention import RetentionCoordinator, RetentionLedger, RetentionPolicy
from ostag.briefs.service import SourceSnapshot
from ostag.briefs.store import BriefStore, PrivateDestination, RequesterContext
from ostag.connectors.github_history import HistoryItem, HistoryResult, canonical
from ostag.core.identity import Principal
from ostag.core.schemas import SchemaError
from test_brief_openai_transport import Wire, adapter, envelope, parameters
from test_brief_validation import output
from test_github_history import PRINCIPAL, collect, pull
from test_meeting_contracts import ACTOR, NOW, grant, intake, request as proposal
from test_meeting_facts import chat


def at(seconds=0):
    return (datetime.fromisoformat(NOW.replace('Z', '+00:00')) + timedelta(seconds=seconds)).isoformat()


def current_request(*, complete=False, text=None):
    scope = intake(proposal(chat_ids=['oc_1'], source_categories=[SourceCategory.PULLS])).scope
    chats = (chat(scope, **({} if text is None else dict(text=text))),)
    if complete:
        # Explicit synthetic complete provider evidence, NOT promotion of the
        # mutable live list collector or its gaps. No semantic scores/assertions.
        history = HistoryResult(scope.content_hash, PRINCIPAL, tuple(
            HistoryItem(str(n), 'pulls', 'pulls', NOW, canonical(pull(n)), None) for n in (42, 43)),
            tuple(PartitionReport(p.partition_id, PartitionStatus.COMPLETE) for p in scope.partitions),
            (), 3, 1000, 0)
    else:
        history = collect(frozen=scope)
    package = _build(scope, history, chats, False)
    req = DurableMeetingRequest.from_input(package.service_input, history=history, chats=chats,
        first_claimed_at=NOW, collection_finished_at=NOW, confirmed_purpose=scope.request.purpose.value,
        confirmed_scope_hash=scope.content_hash, collection_calls=history.calls + len(scope.chats),
        collection_bytes=history.response_bytes + sum(len(c.source.excerpt.encode()) for c in chats))
    return req


def runtime(path, req=None, *, clock=None, policy=None):
    req = current_request() if req is None else req
    clock = SimpleNamespace(seconds=10) if clock is None else clock
    authority = TrustedBriefAuthority(clock=lambda: at(clock.seconds),
                                      monotonic=lambda: float(clock.seconds), max_ttl_seconds=300)
    scope = RequesterContext(req.namespace, ACTOR)
    dest = PrivateDestination(ACTOR, 'oc_private')
    snapshot = AuthoritySnapshot(1, at(), at(290), permissions=((ACTOR, 'propose'),),
        destinations=((scope, dest),), grants=(grant(),),
        sources=tuple(SourceSnapshot(scope, s, at(290)) for s in req.sources), meetings=(req,))
    authority.publish(snapshot)
    store = BriefStore(path / 'brief.db')
    store.enable_manual()  # Existing explicit store initialization; no meeting migration.
    ledger = RetentionLedger(path / 'retention.db')
    ledger.initialize()
    c = RetentionCoordinator(ledger=ledger, store=store, clock=authority.clock)
    life = BriefLifecycle()
    life.apply(1, 'on', False)
    result = envelope()
    model_output = output(text='Synthetic change requires review')
    model_output['progress'][0]['claim']['source_ids'] = [req.sources[0].source_id]
    result['output'][0]['content'][0]['text'] = json.dumps(model_output)
    wire = Wire(result)
    config = GenerationConfig(parameters(max_input_bytes=100000, max_job_micros=300000), 1000000)
    service = authority.create_service(store=store, transport=adapter(wire), config=config,
        retention=c.guard(store), lifecycle=life)
    policy = policy or RetentionPolicy(at(280))
    app = service.meeting_application(policy=policy)
    return SimpleNamespace(request=req, scope=scope, authority=authority, snapshot=snapshot, service=service,
        store=store, retention=c, life=life, clock=clock, wire=wire, app=app, policy=policy)


@pytest.fixture
def local(tmp_path):
    r = runtime(tmp_path)
    yield r
    r.service.close()


def test_current_typed_roundtrip_preserves_raw_hash_display_scope_and_timing():
    req = current_request(complete=True)
    assert DurableMeetingRequest.from_dict(req.to_dict()) == req
    github = next(s for s in req.sources if s.source_kind == 'github_event')
    assert github.content_sha256 != hashlib.sha256(github.excerpt.encode()).hexdigest()
    assert req.coverage.complete
    partial = current_request()
    assert not partial.coverage.complete
    assert any('partial' in gap for gap in partial.coverage.gaps)
    assert not req.scope.request.shareable_sources


def test_archived_handoff_cannot_be_promoted_by_factory():
    req = current_request()
    history, chats = req.evidence()
    value = build_meeting(req.scope, history, chats=chats).service_input
    with pytest.raises(SchemaError):
        DurableMeetingRequest.from_input(value, history=history, chats=chats,
            first_claimed_at=NOW, collection_finished_at=NOW,
            confirmed_purpose=req.purpose, confirmed_scope_hash=req.scope.content_hash,
            collection_calls=req.collection_calls, collection_bytes=req.collection_bytes)


@pytest.mark.parametrize('change', ['scope', 'origin', 'raw', 'actor', 'unpublished', 'consumption'])
def test_constructed_or_forged_request_never_authorizes(local, change):
    r, req = local, local.request
    if change == 'scope':
        frozen = replace(req.scope, mapping_policy_sha256='a' * 64)
        # History scope is also rehashed by attacker; still not published authority.
        v = json.loads(req.evidence_json)
        v['history']['scope_hash'] = frozen.content_hash
        req = replace(req, scope=frozen, confirmed_scope_hash=frozen.content_hash, evidence_json=canonical(v))
    elif change == 'origin':
        req = replace(req, collection_finished_at=at(1))
    elif change == 'consumption':
        req = replace(req, collection_calls=req.collection_calls + 1)
    elif change == 'raw':
        v = json.loads(req.evidence_json)
        v['history']['items'][0]['raw']['title'] = 'forged raw'
        req = replace(req, evidence_json=canonical(v))
    elif change == 'unpublished':
        r.authority.publish(replace(r.snapshot, revision=2, meetings=(), sources=()))
    scope = RequesterContext(req.namespace, Principal('feishu', 'ou_other')) if change == 'actor' else r.scope
    assert r.app.submit(scope, 'meeting-one', req).status == 'denied'
    assert not r.wire.calls
    assert r.store.get_by_event(r.scope, 'meeting-one') is None


def test_source_hash_malformation_and_missing_required_partition_rejected():
    req = current_request(complete=True)
    evidence = json.loads(req.evidence_json)
    evidence['chats'][0]['source']['content_sha256'] = 'f' * 64
    with pytest.raises(SchemaError):
        replace(req, evidence_json=canonical(evidence))
    evidence = json.loads(req.evidence_json)
    evidence['history']['reports'].pop()
    with pytest.raises(Exception):
        replace(req, evidence_json=canonical(evidence))
    evidence = json.loads(req.evidence_json)
    evidence['history']['reports'][-1]['status'] = 'missing'
    partial = replace(req, evidence_json=canonical(evidence))
    assert not partial.coverage.complete


def test_nonmanual_authority_requires_independent_exact_provenance(local):
    r = local
    with pytest.raises(SchemaError):
        replace(r.snapshot, meetings=())
    sources = list(r.snapshot.sources)
    sources[0] = replace(sources[0], source=replace(sources[0].source, content_sha256='0' * 64))
    with pytest.raises(SchemaError):
        replace(r.snapshot, sources=tuple(sources))


def test_optional_dependencies_cannot_disable_meeting_gates(local):
    r = local
    for field, value in (('_authority', None), ('lifecycle', None), ('_retention_enabled', False)):
        original = getattr(r.service, field)
        setattr(r.service, field, value)
        try:
            with pytest.raises(SchemaError):
                r.service.meeting_application(policy=r.policy)
            assert r.app.submit(r.scope, 'missing-' + field, r.request).status == 'denied'
        finally:
            setattr(r.service, field, original)


@pytest.mark.parametrize('field,value', [('collection_calls', 0), ('collection_calls', 201),
    ('collection_calls', True), ('collection_bytes', 0), ('collection_bytes', 16 * 1024**2 + 1)])
def test_request_wide_collection_counters_are_bounded(field, value):
    with pytest.raises(SchemaError):
        replace(current_request(complete=True), **{field: value})


def test_public_current_collection_builder_still_needs_independent_publication(local):
    req = local.request
    history, chats = req.evidence()
    built = DurableMeetingRequest.from_current_collection(req.scope, history=history, chats=chats,
        first_claimed_at=req.first_claimed_at, collection_finished_at=req.collection_finished_at,
        confirmed_purpose=req.confirmed_purpose, confirmed_scope_hash=req.confirmed_scope_hash,
        collection_calls=req.collection_calls, collection_bytes=req.collection_bytes)
    assert built == req
    local.authority.invalidate()
    assert local.app.submit(local.scope, 'no-authority', built).status == 'denied'


def test_lower_repository_profile_cannot_be_promoted_to_partial_admission():
    req = current_request(complete=True)
    frozen = replace(req.scope, budget=replace(req.scope.budget, max_repos=1))
    with pytest.raises(SchemaError, match='repository budget'):
        replace(req, scope=frozen, confirmed_scope_hash=frozen.content_hash)
