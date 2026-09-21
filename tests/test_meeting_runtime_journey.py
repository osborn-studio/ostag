"""Physical SQLite, real BriefWorker and real read-only adapter; synthetic wire only."""
from dataclasses import replace
import json
import sqlite3
from threading import Event

import pytest

from ostag.briefs.meeting_service import register_retention
from ostag.briefs.retention import RetentionPolicy
from ostag.briefs.store import WorkerContext
from ostag.briefs.worker import BriefWorker
from test_meeting_service import at, current_request, runtime


@pytest.mark.parametrize('complete', [False, True])
def test_durable_two_repo_chat_reopen_status_private_export_and_exactly_once(tmp_path, complete):
    r = runtime(tmp_path, current_request(complete=complete))
    worker = BriefWorker(r.service)
    try:
        admitted = r.app.submit(r.scope, 'meeting-one', r.request)
        assert admitted.status == 'queued'
        before = r.store.get(r.scope, admitted.job_id)
        assert r.retention.status(r.scope, admitted.job_id).decision == 'authorized'
        assert worker.run_once().result(5) == 'draft_ready'
        box = r.store.outbox(r.scope, admitted.job_id)
        assert box.state == 'pending' and box.destination.principal == r.scope.principal
        assert box.destination.address == 'oc_private'
        provenance = json.loads(box.revision.provenance_json)
        assert provenance['meeting'] == r.request.to_dict()
        assert provenance['intake_hash'] == before.content_hash
        assert provenance['sources'] == r.request.source_set(r.scope).to_dict()
        assert provenance['meeting_ready'] is False
        assert len(r.wire.calls) == 2  # count then generate, never external delivery
        r.service.retention.check(r.scope, admitted.job_id, box.revision.revision_id)
        assert r.app.submit(r.scope, 'meeting-one', r.request).job_id == admitted.job_id
        markdown = r.app.private_export(r.scope, admitted.job_id)
        status = r.app.status(r.scope, admitted.job_id)
        assert status['input_complete'] is complete
        assert not status['meeting_ready'] and status['semantic_review_required']
        assert ('partial' in ' '.join(status['gaps'])) is (not complete)
        assert all(h in markdown for h in ['Important changes', 'Conflicts and risks', 'Suggested actions',
                                          'Priority decision topics', 'Code-owned source citations'])
        after = r.store.get(r.scope, admitted.job_id)
        with sqlite3.connect(r.store.path) as db:
            schema = (db.execute('PRAGMA user_version').fetchone()[0],
                      db.execute('SELECT sql FROM sqlite_master ORDER BY name').fetchall())
            assert db.execute('SELECT count(*) FROM brief_outbox').fetchone()[0] == 1
    finally:
        worker.stop()
        r.service.close()
    reopened = runtime(tmp_path, r.request, clock=r.clock)
    worker = BriefWorker(reopened.service)
    try:
        assert reopened.store.get(r.scope, admitted.job_id) == after
        assert reopened.app.status(r.scope, admitted.job_id) == status
        assert reopened.app.private_export(r.scope, admitted.job_id) == markdown
        assert reopened.app.submit(r.scope, 'meeting-one', r.request).job_id == admitted.job_id
        assert worker.run_once().result(3) == 'idle'
        assert not reopened.wire.calls
        with sqlite3.connect(reopened.store.path) as db:
            assert schema == (db.execute('PRAGMA user_version').fetchone()[0],
                              db.execute('SELECT sql FROM sqlite_master ORDER BY name').fetchall())
    finally:
        worker.stop()
        reopened.service.close()


@pytest.mark.parametrize('change', ['revoke', 'stale', 'source_acl', 'mapping', 'destination', 'grant', 'config'])
@pytest.mark.parametrize('phase', ['execution', 'export'])
def test_current_authority_denied_at_execution_and_export(tmp_path, change, phase):
    r = runtime(tmp_path)
    worker = BriefWorker(r.service)
    try:
        admitted = r.app.submit(r.scope, 'meeting-one', r.request)
        assert admitted.status == 'queued'
        if phase == 'export':
            assert worker.run_once().result(5) == 'draft_ready'
        if change == 'revoke':
            r.authority.invalidate()
        elif change == 'stale':
            r.clock.seconds = 290
        elif change == 'config':
            r.service.config = replace(r.service.config, daily_limit_micros=900000)
        else:
            changes = dict(revision=2)
            if change == 'source_acl':
                changes['sources'] = r.snapshot.sources[1:]
            elif change == 'mapping':
                changes['meetings'] = ()
                changes['sources'] = ()
            elif change == 'destination':
                changes['destinations'] = ((r.scope, replace(r.snapshot.destinations[0][1], address='oc_changed')),)
            elif change == 'grant':
                changes['grants'] = (replace(r.snapshot.grants[0], revoked_at=at(5)),)
            r.authority.publish(replace(r.snapshot, **changes))
        if phase == 'execution':
            assert worker.run_once().result(5) == 'needs_input'
            assert not r.wire.calls and r.store.outbox(r.scope, admitted.job_id) is None
        with pytest.raises(Exception):
            r.app.private_export(r.scope, admitted.job_id)
    finally:
        worker.stop()
        r.service.close()


def test_cancel_and_pause_never_dispatch(tmp_path):
    r = runtime(tmp_path)
    worker = BriefWorker(r.service)
    try:
        job = r.app.submit(r.scope, 'meeting-one', r.request).job_id
        r.life.apply(2, 'off', False)
        assert r.app.submit(r.scope, 'meeting-two', r.request).status == 'denied'
        assert worker.run_once().result(3) == 'unavailable'
        with pytest.raises(Exception):
            r.app.private_export(r.scope, job)
        r.life.apply(3, 'on', False)
        assert r.app.cancel(r.scope, job).status == 'cancelled'
        assert worker.run_once().result(3) == 'idle'
        assert r.app.status(r.scope, job)['state'] == 'cancelled'
        assert not r.wire.calls
    finally:
        worker.stop()
        r.service.close()


@pytest.mark.parametrize('seconds,expected', [(130, 'draft_ready'), (180, 'needs_input')])
def test_original_origin_budget_survives_queued_restart(tmp_path, seconds, expected):
    r = runtime(tmp_path, current_request(complete=True))
    job = r.app.submit(r.scope, 'meeting-one', r.request).job_id
    original = r.store.get(r.scope, job)
    r.service.close()
    r.clock.seconds = seconds
    reopened = runtime(tmp_path, r.request, clock=r.clock)
    worker = BriefWorker(reopened.service)
    try:
        assert worker.run_once().result(5) == expected
        persisted = reopened.store.get(r.scope, job)
        assert persisted.payload_json == original.payload_json
        assert persisted.first_claim_at == at(seconds).replace('+00:00', 'Z')
        if expected == 'needs_input':
            assert reopened.store.failure_cause(r.scope, job) == 'timeout'
            assert not reopened.wire.calls
        else:
            assert len(reopened.wire.calls) == 2
    finally:
        worker.stop()
        reopened.service.close()


def test_claimed_restart_has_no_implicit_resume(tmp_path):
    r = runtime(tmp_path)
    job = r.app.submit(r.scope, 'meeting-one', r.request).job_id
    claimed = r.store.claim_one(WorkerContext('crashed'), clock=r.service.clock)
    r.service.close()
    r.clock.seconds = 50
    reopened = runtime(tmp_path, r.request, clock=r.clock)
    worker = BriefWorker(reopened.service)
    try:
        worker.run_once().result(3)
        after = reopened.store.get(r.scope, job)
        assert after.state == 'needs_input'
        assert after.first_claim_at == claimed.first_claim_at
        assert after.attempt == claimed.attempt
        assert not reopened.wire.calls
    finally:
        worker.stop()
        reopened.service.close()


@pytest.mark.parametrize('phase', ['before', 'during', 'after'])
def test_retention_gate_registration_expiry_and_replay(tmp_path, phase):
    r = runtime(tmp_path, policy=RetentionPolicy(at(20)))
    entered, release = Event(), Event()
    async def hold(_):
        entered.set()
        assert release.wait(3)
    if phase == 'during':
        r.wire.effect = hold
    worker = BriefWorker(r.service)
    try:
        job = r.app.submit(r.scope, 'meeting-one', r.request).job_id
        assert job
        if phase == 'before':
            r.clock.seconds = 20
        future = worker.run_once()
        if phase == 'during':
            assert entered.wait(2)
            r.clock.seconds = 20
            release.set()
        assert future.result(5) == ('draft_ready' if phase == 'after' else 'needs_input')
        if phase == 'after':
            register_retention(r.service, r.store.get(r.scope, job))
            r.clock.seconds = 20
        with pytest.raises(Exception):
            r.app.private_export(r.scope, job)
        assert r.retention.status(r.scope, job).decision == 'expired'
    finally:
        release.set()
        worker.stop()
        r.service.close()


def test_inert_export_with_hostile_source_and_model_text(tmp_path):
    text = '` [bad](https://outside.invalid) ![img](https://outside.invalid/i) <script>x</script> ```'
    r = runtime(tmp_path, current_request(complete=True, text=text))
    r.wire.result['output'][0]['content'][0]['text'] = json.dumps(dict(version='brief-output-v1',
        progress=[dict(id='c', claim=dict(kind='fact', text='` **synthetic review** ```', source_ids=[r.request.sources[0].source_id]))],
        conflicts=[], decisions=[], proposals=[]))
    worker = BriefWorker(r.service)
    try:
        job = r.app.submit(r.scope, 'meeting-one', r.request).job_id
        assert worker.run_once().result(5) == 'draft_ready'
        markdown = r.app.private_export(r.scope, job)
        # Independent CommonMark parser required, never skipped in this verifier.
        import subprocess
        parsed = subprocess.run(['pandoc', '--from=commonmark', '--to=json'], input=markdown,
                                capture_output=True, text=True, timeout=5, check=True)
        tree = json.loads(parsed.stdout)
        def nodes(v):
            if isinstance(v, dict):
                yield v
                for child in v.values():
                    yield from nodes(child)
            elif isinstance(v, list):
                for child in v:
                    yield from nodes(child)
        values = list(nodes(tree))
        assert not any(n.get('t') in ('Image', 'RawInline', 'RawBlock') for n in values)
        assert all(n['c'][2][0].startswith('https://github.com/acme/private-')
                   for n in values if n.get('t') == 'Link')
    finally:
        worker.stop()
        r.service.close()


def test_new_event_cannot_reset_budget_even_after_retirement(tmp_path):
    r = runtime(tmp_path, current_request(complete=True))
    try:
        first = r.app.submit(r.scope, 'first', r.request)
        assert first.status == 'queued'
        assert r.app.submit(r.scope, 'another-event', r.request).status == 'conflict'
        r.retention.revoke_job(r.scope, first.job_id)
        assert r.retention.cleanup(r.scope, first.job_id).cleanup == 'complete'
        # Permanent intake identity remains after all raw payloads are retired.
        with sqlite3.connect(r.store.path) as db:
            assert db.execute('SELECT request_hash FROM brief_intakes WHERE job_id=?',
                              (first.job_id,)).fetchone()[0] == r.request.scope.content_hash
        assert r.app.submit(r.scope, 'another-event', r.request).status == 'conflict'
    finally:
        r.service.close()


@pytest.mark.parametrize('change', ['revoke', 'cancel', 'pause'])
def test_inflight_change_fences_generation_commit(tmp_path, change):
    r = runtime(tmp_path, current_request(complete=True))
    entered, release = Event(), Event()
    async def hold(req):
        if not req.url.path.endswith('input_tokens'):
            entered.set()
            assert release.wait(3)
    r.wire.effect = hold
    worker = BriefWorker(r.service)
    try:
        job = r.app.submit(r.scope, 'meeting-one', r.request).job_id
        future = worker.run_once()
        assert entered.wait(2)
        if change == 'revoke':
            r.authority.invalidate()
        elif change == 'cancel':
            assert r.app.cancel(r.scope, job).status == 'cancelled'
        else:
            r.life.apply(2, 'off', False)
        release.set()
        assert future.result(5) in ('needs_input', 'cancelled')
        assert r.store.outbox(r.scope, job) is None
    finally:
        release.set()
        worker.stop()
        r.service.close()


def test_remaining_transport_budget_and_lower_profile_win(tmp_path):
    r = runtime(tmp_path, current_request(complete=True))
    r.service.config = replace(r.service.config, parameters=replace(r.service.config.parameters,
        budget=replace(r.service.config.parameters.budget, timeout_seconds=30)))
    seen = []
    actual = r.service.transport.generate
    async def observe(req):
        seen.append(req.remaining_seconds)
        return await actual(req)
    r.service.transport.generate = observe
    job = r.app.submit(r.scope, 'meeting-one', r.request).job_id
    r.service.close()
    r.clock.seconds = 20
    reopened = runtime(tmp_path, r.request, clock=r.clock)
    reopened.service.config = r.service.config
    actual = reopened.service.transport.generate
    reopened.service.transport.generate = observe
    worker = BriefWorker(reopened.service)
    try:
        assert worker.run_once().result(5) == 'draft_ready'
        assert len(seen) == 1 and 0 < seen[0] <= 10
        assert reopened.store.get(r.scope, job).execution_seconds == 30
    finally:
        worker.stop()
        reopened.service.close()


def test_post_completion_registration_failure_denies_export_and_retries_same_policy(tmp_path, monkeypatch):
    r = runtime(tmp_path, current_request(complete=True))
    import ostag.briefs.worker as module
    original = module.register_retention
    def blocked(service, job):
        if job.state == 'draft_ready':
            raise RuntimeError('synthetic registration failure')
        return original(service, job)
    worker = BriefWorker(r.service)
    try:
        job = r.app.submit(r.scope, 'meeting-one', r.request).job_id
        with monkeypatch.context() as patch:
            patch.setattr(module, 'register_retention', blocked)
            assert worker.run_once().result(5) == 'needs_input'
        box = r.store.outbox(r.scope, job)
        with pytest.raises(Exception):
            r.service.retention.check(r.scope, job, box.revision.revision_id)
        assert r.app.private_export(r.scope, job)
        r.service.retention.check(r.scope, job, box.revision.revision_id)
        assert len(r.wire.calls) == 2
    finally:
        worker.stop()
        r.service.close()


@pytest.mark.parametrize('change', ['cancel', 'config', 'revoke'])
def test_export_rechecks_after_rendering(tmp_path, monkeypatch, change):
    import ostag.briefs.meeting_service as module
    r = runtime(tmp_path, current_request(complete=True))
    worker = BriefWorker(r.service)
    try:
        job = r.app.submit(r.scope, 'meeting-one', r.request).job_id
        assert worker.run_once().result(5) == 'draft_ready'
        original = module._literal_code
        changed = False
        def render(value):
            nonlocal changed
            if not changed:
                changed = True
                if change == 'cancel':
                    r.app.cancel(r.scope, job)
                elif change == 'config':
                    r.service.config = replace(r.service.config, daily_limit_micros=900000)
                else:
                    r.authority.invalidate()
            return original(value)
        monkeypatch.setattr(module, '_literal_code', render)
        with pytest.raises(Exception):
            r.app.private_export(r.scope, job)
    finally:
        worker.stop()
        r.service.close()


def test_reserved_never_started_cost_released_on_denied_resume(tmp_path):
    from ostag.briefs.worker import CALL_ID
    r = runtime(tmp_path, current_request(complete=True))
    job = r.app.submit(r.scope, 'meeting-one', r.request).job_id
    claim = r.store.claim_one(WorkerContext('crashed'), clock=r.service.clock)
    r.store.start_generating(claim.lease, clock=r.service.clock)
    r.store.reserve_cost(claim.lease, call_id=CALL_ID, profile=r.service.config.parameters.cost_profile,
        daily_limit_micros=r.service.config.daily_limit_micros, config_hash=r.service.config.config_hash,
        clock=r.service.clock)
    r.service.close()
    r.clock.seconds = 50
    reopened = runtime(tmp_path, r.request, clock=r.clock)
    worker = BriefWorker(reopened.service)
    try:
        worker.run_once().result(3)
        assert reopened.store.get(r.scope, job).state == 'needs_input'
        assert reopened.store.cost_record(r.scope, job, CALL_ID).state == 'released'
        assert not reopened.wire.calls
    finally:
        worker.stop()
        reopened.service.close()


def test_feishu_raw_content_hash_is_not_downgraded_to_display_hash(tmp_path):
    import hashlib
    from ostag.connectors.github_history import canonical
    req = current_request(complete=True)
    evidence = json.loads(req.evidence_json)
    source = evidence['chats'][0]['source']
    original_content = canonical({'text': source['excerpt'], 'original': 'synthetic raw provenance'})
    source['content_sha256'] = hashlib.sha256(original_content.encode()).hexdigest()
    assert source['content_sha256'] != hashlib.sha256(source['excerpt'].encode()).hexdigest()
    req = replace(req, evidence_json=canonical(evidence),
                  chat_raw_json=canonical({source['source_id']: original_content}),
                  collection_bytes=req.collection_bytes + len(original_content.encode()))
    r = runtime(tmp_path, req)
    worker = BriefWorker(r.service)
    try:
        job = r.app.submit(r.scope, 'meeting-one', req).job_id
        assert worker.run_once().result(5) == 'draft_ready'
        box = r.store.outbox(r.scope, job)
        provenance = json.loads(box.revision.provenance_json)
        assert provenance['meeting']['chat_raw_json'] == req.chat_raw_json
        assert source['content_sha256'] in r.app.private_export(r.scope, job)
    finally:
        worker.stop()
        r.service.close()
