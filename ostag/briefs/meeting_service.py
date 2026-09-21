"""Explicit local meeting ingress on BriefService/BriefWorker, never a route.

Publication in TrustedBriefAuthority is a trusted host act after independent ACL,
source, mapping, config and origin verification. Parsing this wire format proves
none of those things. No collector resume, implicit migration or model adapter.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from functools import wraps
import hashlib
import json

from ostag.briefs.generation import digest
from ostag.briefs.meeting_facts import combine
from ostag.briefs.meeting_models import ExecutionTiming, FrozenScope
from ostag.briefs.meetings import MeetingServiceInput, _archive, _build, _literal_code
from ostag.briefs.models import _invalid, _keys, _text, _utc
from ostag.briefs.service import BriefStatus, ManualRequest
from ostag.briefs.store import Conflict, RequesterContext
from ostag.briefs.validation import Coverage, SECTIONS, SourceLink, bounded_int, strict_json, validate_output

VERSION = "meeting-brief-v1"


@dataclass(frozen=True, repr=False)
class DurableMeetingRequest:
    """Immutable current-input proposal; authority independently owns exact bytes.

    evidence_json is canonical raw provenance, not a resumption capability.
    first_claimed_at is the original trusted collection origin, NOT caller time.
    The BriefStore worker's separate first claim remains durable too.
    """
    scope: FrozenScope
    evidence_json: str
    first_claimed_at: str
    collection_finished_at: str
    confirmed_purpose: str
    confirmed_scope_hash: str
    collection_calls: int
    collection_bytes: int
    chat_raw_json: str = "{}"
    version: str = VERSION

    def __post_init__(self):
        if type(self.scope) is not FrozenScope or self.version != VERSION:
            raise _invalid("typed meeting scope/version required")
        object.__setattr__(self, "scope", FrozenScope.from_dict(self.scope.to_dict()))
        if len(self.scope.repositories) > self.scope.budget.max_repos:
            raise _invalid("meeting repository budget exceeded")
        if (self.confirmed_scope_hash != self.scope.content_hash
                or self.confirmed_purpose != self.scope.request.purpose.value):
            raise _invalid("meeting confirmation mismatch")
        timing = ExecutionTiming(self.scope, self.first_claimed_at)
        finish = _utc(self.collection_finished_at, "collection finish")
        start = _utc(timing.first_claimed_at, "collection origin")
        if not start <= finish <= _utc(timing.collection_deadline, "collection deadline"):
            raise _invalid("collection budget exhausted")
        history, chats = self.evidence()
        raw = strict_json(self.chat_raw_json, 1048576)
        if type(raw) is not dict or set(raw) - {c.source.source_id for c in chats}:
            raise _invalid("raw chat content must identify selected sources")
        for content in raw.values():
            _text(content, "raw chat content", 65536, empty=True)
        bounded_int(self.collection_calls, "collection calls", self.scope.budget.max_provider_calls,
                    history.calls + len(self.scope.chats))
        bounded_int(self.collection_bytes, "collection bytes", 16 * 1024**2,
                    history.response_bytes + sum(len(raw.get(c.source.source_id,
                                                            c.source.excerpt).encode()) for c in chats))
        # HistoryResult.collection_seconds is a diagnostic profile ceiling,
        # not elapsed time. Only the independently owned origin/finish bound time.
        for item in history.items:
            if not start <= _utc(item.observed_at, "observation") <= finish:
                raise _invalid("source observation outside origin interval")
        for chat in chats:
            s = chat.source
            if (not start <= _utc(s.observed_at, "observation") <= finish
                    or hashlib.sha256(raw.get(s.source_id, s.excerpt).encode()).hexdigest() != s.content_sha256):
                raise _invalid("chat snapshot hash or origin mismatch")
        # Reassemble from canonical raw records; never trust caller display/hash.
        self.assembly()
        digest(self.to_dict())

    @classmethod
    def from_current_collection(cls, scope, *, history, chats=(), first_claimed_at,
                                collection_finished_at, confirmed_purpose, confirmed_scope_hash,
                                collection_calls, collection_bytes, chat_raw_content=None):
        """Trusted host's data builder; MUST NOT be used to authorize archive JSON.

        The host independently checks original collection timing/consumption and
        current selected mapping, identities, ACLs and partition evidence before
        publishing the resulting request in AuthoritySnapshot.meetings.
        """
        chats = tuple(chats)
        value = _build(scope, history, tuple(chats), False).service_input
        return cls.from_input(value, history=history, chats=chats,
            first_claimed_at=first_claimed_at, collection_finished_at=collection_finished_at,
            confirmed_purpose=confirmed_purpose, confirmed_scope_hash=confirmed_scope_hash,
            collection_calls=collection_calls, collection_bytes=collection_bytes,
            chat_raw_content=chat_raw_content)

    @classmethod
    def from_input(cls, value, *, history, chats=(), first_claimed_at,
                   collection_finished_at, confirmed_purpose, confirmed_scope_hash,
                   collection_calls, collection_bytes, chat_raw_content=None):
        chats = tuple(chats)
        if (type(value) is not MeetingServiceInput
                or value.input_mode != "current_authorized_collection"
                or value != _build(value.scope, history, tuple(chats), False).service_input):
            raise _invalid("archived or mismatched meeting input")
        from ostag.connectors.github_history import canonical
        evidence = dict(history=history.to_dict(), chats=[dict(
            source=c.source.to_dict(), disposition=c.disposition) for c in chats])
        return cls(value.scope, canonical(evidence), first_claimed_at,
                   collection_finished_at, confirmed_purpose, confirmed_scope_hash,
                   collection_calls, collection_bytes,
                   canonical({} if chat_raw_content is None else chat_raw_content))

    def evidence(self):
        return _archive(strict_json(self.evidence_json, 1048576), self.scope)

    def assembly(self):
        history, chats = self.evidence()
        return combine(self.scope, history, chats)

    @property
    def namespace(self):
        return self.scope.request.namespace

    @property
    def purpose(self):
        return self.scope.request.purpose.value

    @property
    def grant_id(self):
        return self.scope.request.grant_id

    @property
    def grant_revision(self):
        return self.scope.request.grant_revision

    @property
    def sources(self):
        assembled = self.assembly().sources
        return () if assembled is None else assembled.sources

    @property
    def coverage(self):
        # This request is accepted only through a current trusted ingress owning
        # every source AND report. Remove only the imported-chat authority warning;
        # no missing/partial/semantic fact is upgraded by a complete flag.
        imported = {c.resource_id + ":imported_chat_current_acl_and_completeness_unproven"
                    for c in self.scope.chats}
        gaps = set(self.assembly().coverage.gaps) - imported - {"semantic_review_required"}
        gaps.update(self.scope.intake_reasons)
        return Coverage(not gaps, tuple(sorted(gaps)))

    @property
    def goals(self):
        return self.scope.request.goal

    @property
    def links(self):
        return tuple(SourceLink(f.source_id, f.citation) for f in self.assembly().facts
                     if f.source is not None)

    @property
    def decision_topics(self):
        return tuple(t for t in self.assembly().topics
                     if t["source_ids"] or not self.coverage.complete)[:3]

    def source_set(self, scope):
        if scope != RequesterContext(self.namespace, self.scope.request.requesting_principal):
            raise _invalid("meeting requester mismatch")
        return self.assembly().sources

    def deadline(self, config):
        return (_utc(self.first_claimed_at, "origin") + timedelta(seconds=min(
            self.scope.budget.overall_seconds, config.parameters.budget.timeout_seconds))).isoformat()

    def check_budget(self, config, now):
        if (_utc(now, "now") < _utc(self.collection_finished_at, "finish")
                or _utc(now, "now") >= _utc(self.deadline(config), "deadline")):
            raise MeetingTimeout()

    def to_dict(self):
        return dict(version=self.version, scope=self.scope.to_dict(), evidence_json=self.evidence_json,
                    first_claimed_at=self.first_claimed_at,
                    collection_finished_at=self.collection_finished_at,
                    confirmed_purpose=self.confirmed_purpose,
                    confirmed_scope_hash=self.confirmed_scope_hash,
                    collection_calls=self.collection_calls, collection_bytes=self.collection_bytes,
                    chat_raw_json=self.chat_raw_json)

    @classmethod
    def from_dict(cls, value):
        v = _keys(value, ("version", "scope", "evidence_json", "first_claimed_at",
                          "collection_finished_at", "confirmed_purpose", "confirmed_scope_hash",
                          "collection_calls", "collection_bytes", "chat_raw_json"))
        v["scope"] = FrozenScope.from_dict(v["scope"])
        return cls(**v)

    def completion_provenance(self, scope, provenance, intake_hash):
        history, chats = self.evidence()
        return provenance.to_dict() | dict(sources=self.source_set(scope).to_dict(),
            intake_hash=intake_hash, meeting=self.to_dict(), meeting_request_hash=digest(self.to_dict()),
            input_revision_digest=_build(self.scope, history, chats, False).service_input.revision_digest,
            semantic_review_required=True, meeting_ready=False, shareable_sources=[])

    validate_completion_size = ManualRequest.validate_completion_size


class MeetingTimeout(Exception):
    """Original request budget expired; sanitized durable worker failure."""


def _private(operation):
    @wraps(operation)
    def run(*args, **kwargs):
        try:
            return operation(*args, **kwargs)
        except Exception:
            pass
        raise _invalid("private meeting unavailable")
    return run


def register_retention(service, job):
    """Meeting adapter for the SAME A10 coordinator/ledger registration policy.

    A10's public register currently decodes only manual-v1. Keep its identity,
    expiry, tombstone, target and revision rules here without changing its schema
    or routing meeting provenance through ManualRequest. No authority is issued.
    """
    from ostag.briefs.retention import RetentionDenied, RetentionPolicy, _scope, _keys, _digest
    service.retention.validate()
    c = service.retention.coordinator
    request, _ = service.load_job(job)
    payload = json.loads(job.payload_json)
    policy = RetentionPolicy(payload["retention_expires_at"])
    key = _scope(job.scope)
    if job.state == "cancelled" or type(request) is not DurableMeetingRequest:
        raise RetentionDenied()
    identities = [_keys(job.scope, s) for s in request.sources]
    if not identities:
        raise RetentionDenied()
    box = service.store.outbox(job.scope, job.job_id)
    revision, delivery = (None, None) if box is None else (box.revision.revision_id, box.delivery_id)
    targets = c._targets()
    binding = _digest([job.content_hash, job.grant_id, job.grant_revision, job.destination.address])
    with c.ledger.transaction(c.clock) as db:
        stamp = db.execute('SELECT highwater FROM retention_meta').fetchone()[0]
        old = db.execute('SELECT * FROM retention_jobs WHERE scope=? AND job=?', (key, job.job_id)).fetchone()
        if old is not None:
            _, reason = c._decision(db, key, job.job_id, stamp)
            pinned = {r['kind']: r['identity'] for r in db.execute(
                'SELECT * FROM retention_targets WHERE scope=? AND job=?', (key, job.job_id))}
            if (reason != 'authorized' or old['binding'] != binding or old['expires'] != policy.expires_at
                    or pinned != targets or old['revision'] not in (None, revision)
                    or old['delivery'] not in (None, delivery)):
                raise RetentionDenied()
            db.execute('UPDATE retention_jobs SET revision=?,delivery=? WHERE scope=? AND job=?',
                       (revision, delivery, key, job.job_id))
        else:
            if (stamp >= policy.expires_at or any(c._blocked(db, key, *identity, stamp) is not None
                                               for identity in identities)):
                raise RetentionDenied()
            db.execute('INSERT INTO retention_jobs VALUES(?,?,?,?,?,?,?)',
                       (key, job.job_id, binding, policy.expires_at, 'authorized', revision, delivery))
            for resource, snapshot, grant in identities:
                prior = db.execute('SELECT min(expires) FROM retention_sources WHERE scope=? AND snapshot=?',
                                   (key, snapshot)).fetchone()[0]
                expiry = policy.expires_at if prior is None else min(prior, policy.expires_at)
                db.execute('INSERT OR IGNORE INTO retention_sources VALUES(?,?,?,?,?,0)',
                           (key, snapshot, resource, grant, expiry))
                db.execute('INSERT OR IGNORE INTO retention_links VALUES(?,?,?,?)',
                           (key, job.job_id, snapshot, grant))
            for kind, identity in targets.items():
                db.execute('INSERT INTO retention_targets VALUES(?,?,?,?,0)', (key, job.job_id, kind, identity))
    service.retention.check(job.scope, job.job_id, revision)


class MeetingApplication:
    """Trusted local submit/status/cancel/export surface; no HTTP or sending.

    requester and policy originate in authenticated application composition, never
    in event.raw/model output. The authority must already own the exact request.
    """
    def __init__(self, service, *, policy):
        from ostag.briefs.retention import RetentionPolicy
        from ostag.briefs.authority import TrustedBriefAuthority
        from ostag.briefs.lifecycle import BriefLifecycle
        if (type(service._authority) is not TrustedBriefAuthority or not service._retention_enabled
                or type(service.lifecycle) is not BriefLifecycle or type(policy) is not RetentionPolicy):
            raise _invalid("meeting authority, retention, lifecycle and policy required")
        self.service, self.policy = service, policy

    def submit(self, requester, event_id, request):
        s = self.service
        op = None
        try:
            op = s.lifecycle.begin("intake")
            if type(request) is not DurableMeetingRequest:
                raise _invalid("typed current meeting request required")
            request = DurableMeetingRequest.from_dict(request.to_dict())
            request.source_set(requester)
            destination = s.destination(requester)
            s.authorize_current(request, requester, destination, stopped=s._closed.is_set, operation=op)
            existing = s.store.get_by_event(requester, event_id)
            if existing is not None:
                if (not s.store.matches_request(requester, existing.job_id, request.scope.content_hash)
                        or s.load_job(existing)[0] != request):
                    return BriefStatus("conflict")
                if existing.state == "cancelled":
                    return BriefStatus("cancelled", existing.job_id)
                register_retention(s, existing)
                s.authorize_current(request, requester, existing.destination, stopped=s._closed.is_set,
                                    job_id=existing.job_id, operation=op)
                return BriefStatus(existing.state, existing.job_id)
            request.check_budget(s.config, s.clock())
            if not request.sources:
                return BriefStatus("needs_input")
            request.validate_completion_size(requester, s.config)
            config = s.config
            def gate():
                request.check_budget(config, s.clock())
                if s.config != config:
                    raise _invalid("meeting config changed")
                s.authorize_current(request, requester, destination, stopped=s._closed.is_set, operation=op)
                request.check_budget(config, s.clock())
            job = s.store.admit(requester, idempotency_key=event_id,
                payload=dict(kind=VERSION, request=request.to_dict(), intake_at=request.scope.intake_at,
                             config=config.to_dict(), retention_expires_at=self.policy.expires_at),
                grant_id=request.grant_id, grant_revision=request.grant_revision,
                destination=destination, now=s.clock(), execution_seconds=config.parameters.budget.timeout_seconds,
                request_hash=request.scope.content_hash, precommit=gate,
                meeting_scope_hash=request.scope.content_hash)
            register_retention(s, job)
            s.authorize_current(request, requester, destination, stopped=s._closed.is_set,
                                job_id=job.job_id, operation=op)
            return BriefStatus(job.state, job.job_id)
        except Conflict:
            return BriefStatus("conflict")
        except MeetingTimeout:
            return BriefStatus("timeout")
        except Exception:
            return BriefStatus("denied")
        finally:
            if op is not None:
                op.finish()

    def _read(self, requester, job_id, op):
        s = self.service
        job = s.store.get(requester, job_id)
        if job is None:
            raise _invalid("meeting unavailable")
        request, _ = s.load_job(job)
        if type(request) is not DurableMeetingRequest:
            raise _invalid("meeting job required")
        s.authorize_current(request, requester, job.destination, stopped=s._closed.is_set,
                            job_id=job_id, operation=op)
        if job.state != "cancelled":
            register_retention(s, job)
        return job, request

    def _fence(self, job, request, op, box=None):
        """Finish primary reads BEFORE final retention/current policy observation.

        Cancellation/config changes during rendering must not release the earlier
        private snapshot. Like existing gates, this cannot recall returned bytes.
        """
        s = self.service
        current = s.store.get(job.scope, job.job_id)
        if current != job or (box is not None and s.store.outbox(job.scope, job.job_id) != box):
            raise _invalid("meeting changed during private read")
        _, config = s.load_job(current)
        def authorize():
            s.authorize_current(request, job.scope, job.destination, stopped=s._closed.is_set,
                                job_id=job.job_id, operation=op)
            if s.config != config:
                raise _invalid("meeting config changed during private read")
        s.retention.check(job.scope, job.job_id,
                          None if box is None else box.revision.revision_id, authorize=authorize)

    @_private
    def status(self, requester, job_id):
        op = None
        try:
            op = self.service.lifecycle.begin("intake")
            job, request = self._read(requester, job_id, op)
            result = dict(job_id=job_id, state=job.state, meeting_ready=False,
                          semantic_review_required=True, input_complete=request.coverage.complete,
                          gaps=list(request.coverage.gaps), failure=self.service.store.failure_cause(requester, job_id))
            self._fence(job, request, op)
            return result
        finally:
            if op is not None:
                op.finish()

    def cancel(self, requester, job_id):
        # Existing requester-scoped cancellation stays available even after ACL loss.
        return self.service.cancel(requester, job_id)

    @_private
    def private_export(self, requester, job_id):
        op = None
        try:
            s = self.service
            op = s.lifecycle.begin("intake")
            job, request = self._read(requester, job_id, op)
            box = s.store.outbox(requester, job_id)
            if box is None or box.state != "pending" or job.state != "draft_ready":
                raise _invalid("meeting draft unavailable")
            provenance = strict_json(box.revision.provenance_json, 1048576)
            if (provenance.get("meeting") != request.to_dict()
                    or provenance.get("sources") != request.source_set(requester).to_dict()
                    or provenance.get("intake_hash") != job.content_hash
                    or provenance.get("meeting_request_hash") != digest(request.to_dict())
                    or box.revision.validation_version != s.config.parameters.validation_version):
                raise _invalid("meeting revision identity mismatch")
            payload = strict_json(box.revision.payload_json, 1048576)
            draft = validate_output(json.dumps(dict(version="brief-output-v1") |
                {section: payload[section] for section in SECTIONS}), sources=request.source_set(requester),
                coverage=request.coverage, max_output_bytes=1048576, links=request.links)
            if draft.to_dict() != payload:
                raise _invalid("meeting validated revision mismatch")
            lines = ["# Private meeting pre-read", "needs_input · semantic_review_required · meeting_ready=false",
                     "Suggestions are not approval. Sharing is not authorized.", _literal_code(request.goals),
                     "## Evidence gaps and risks"]
            lines += ["- " + _literal_code(g) for g in request.coverage.gaps]
            for section, title in (("progress", "Important changes"), ("conflicts", "Conflicts and risks"),
                                   ("decisions", "Proposed decisions"), ("proposals", "Suggested actions")):
                lines += ["## " + title]
                for item in getattr(draft, section):
                    lines.append("- " + _literal_code(item.claim.text) + " — " +
                                 ", ".join(_literal_code(sid) for sid in item.claim.source_ids))
            lines += ["## Priority decision topics"]
            topics = request.decision_topics
            for topic in topics:
                lines += ["- " + _literal_code(topic["question"]) + f" ({topic['minutes']} minutes)",
                          "Suggested action: " + _literal_code(topic["suggested_action"])]
            lines += [f"Total: {10 + sum(t['minutes'] for t in topics)} minutes including review and decisions.",
                      "## Code-owned source citations"]
            lines += [f"- {_literal_code(link.source_id)}: [Source]({link.url})" for link in request.links]
            lines += ["## Provenance", "Revision: " + _literal_code(box.revision.revision_id),
                      "Scope: " + _literal_code(request.scope.content_hash)]
            lines += [f"- {_literal_code(src.source_id)}: {_literal_code(src.content_sha256)}" for src in request.sources]
            rendered = "\n\n".join(lines) + "\n"
            self._fence(job, request, op, box)
            return rendered
        finally:
            if op is not None:
                op.finish()
