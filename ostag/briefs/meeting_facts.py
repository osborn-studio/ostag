"""Source-backed deterministic meeting assembly; never an identity or semantic oracle."""
from __future__ import annotations

from dataclasses import dataclass, field
import re

from ostag.briefs.github_change_facts import ChangeFact, classify, display_text
from ostag.briefs.meeting_models import FrozenScope
from ostag.briefs.models import SourceRef, SourceSet, _utc
from ostag.briefs.validation import Coverage
from ostag.connectors.github_history import HistoryResult, canonical, require


@dataclass(frozen=True, repr=False)
class ChatEvidence:
    source: SourceRef = field(repr=False)
    # These are explicit operator annotations requiring semantic review, not extracted facts.
    disposition: str = "pending"

    def __post_init__(self):
        require(type(self.source) is SourceRef and self.source.source_kind == "feishu_message")
        require(self.disposition in ("pending", "blocked", "conflicting", "observed"))


@dataclass(frozen=True, repr=False)
class MeetingFacts:
    facts: tuple[ChangeFact, ...]
    chats: tuple[ChatEvidence, ...]
    sources: SourceSet | None
    coverage: Coverage
    associations: tuple[dict, ...]
    topics: tuple[dict, ...]


def combine(scope: FrozenScope, history: HistoryResult, chats: tuple[ChatEvidence, ...] = (),
            *, historical: bool = False) -> MeetingFacts:
    require(history.scope_hash == scope.content_hash, "scope_mismatch")
    require(len(chats) <= scope.budget.max_feishu_messages)
    require(len(history.items) <= scope.budget.max_github_facts)
    expected = {p.partition_id for p in scope.partitions}
    reports = {p.partition_id: p for p in history.reports}
    require(len(reports) == len(history.reports) and set(reports) == expected)
    gaps = set(history.gaps)
    gaps.add("semantic_review_required")
    if historical:
        gaps.add("historical_input_identity_is_observation_not_current_authorization")
    facts = {}
    for item in history.items:
        pid = f"github:repo:{item.repo_id}:{item.category}"
        require(pid in reports)
        # Parsed archived inputs cannot smuggle withheld content back in.
        require(reports[pid].status.value not in ("failed", "missing"), "withheld_partition")
        try:
            normalized = classify(scope, history.principal, item)
        except Exception:
            gaps.add(pid + ":invalid_fact_withheld")
            continue
        for fact in normalized:
            if len(facts) >= scope.budget.max_github_facts and fact.source_id not in facts:
                gaps.add(pid + ":normalized_fact_limit")
                break
            previous = facts.get(fact.source_id)
            if previous and previous.raw_source_sha256 != fact.raw_source_sha256:
                gaps.add(pid + ":conflicting_snapshots")
                # Keep both immutable versions, never silently pick a last writer.
                from dataclasses import replace
                sid = fact.source_id + ":" + fact.raw_source_sha256[:12]
                fact = replace(fact, source_id=sid,
                               source=replace(fact.source, source_id=sid) if fact.source else None,
                               status="conflicting")
            facts[fact.source_id] = fact
    ordered = tuple(sorted(facts.values(), key=lambda f: (f.event_at or f.observed_at,
                                                          f.resource_id, f.source_id)))
    sources = [f.source for f in ordered if f.source is not None]
    known_resources = {c.resource_id for c in scope.chats}
    for chat in chats:
        source = chat.source
        require(source.namespace == scope.request.namespace
                and source.resource_id in known_resources
                and source.window == scope.request.window
                and source.purpose == scope.request.purpose.value
                and source.grant_id == scope.request.grant_id
                and source.grant_revision == scope.request.grant_revision
                and source.allowed_audience == scope.request.audience, "chat_binding_denied")
        # Chat material is imported, never current ACL evidence. No name/login joins.
        gaps.add(source.resource_id + ":imported_chat_current_acl_and_completeness_unproven")
        sources.append(source)
    require(len(sources) <= 500)
    source_set = SourceSet(scope.request.namespace, scope.request.purpose.value,
                           scope.request.window, scope.request.audience, tuple(sources)) if sources else None
    for report in history.reports:
        if report.status.value != "complete":
            gaps.add(report.partition_id + ":" + report.status.value)
    if any(f.event_at is None for f in ordered):
        gaps.add("unknown_event_times_excluded_from_temporal_source_set")
    associations = []
    for chat in chats:
        # Only literal exact known canonical links. Bare numbers/names/semantic overlap do not join.
        links = set(re.findall(r'https://github\.com/[^\s<>"\[\]()]+', chat.source.excerpt))
        linked = sorted(f.source_id for f in ordered if f.citation in links)
        associations.append(dict(chat_source_id=chat.source.source_id, source_ids=linked,
                                 basis="explicit_link" if linked else "unassociated",
                                 semantic_association="candidate_only", identity_join=False))
    topics = []
    if gaps - {"semantic_review_required"}:
        topics.append(dict(question="Which missing evidence must be supplied before a decision?",
                           disposition="blocked", source_ids=[], gap_references=sorted(gaps),
                           suggested_action="Assign evidence verification; confirm owner and due date.",
                           owner="unknown", due="unknown", minutes=5))
    candidates = [(f.status, f.source_id, f.display) for f in ordered
                  if f.status in ("pending", "blocked", "conflicting")]
    candidates += [(c.disposition, c.source.source_id, display_text(c.source.excerpt[:1000]))
                   for c in chats if c.disposition != "observed"]
    priority = {"conflicting": 0, "blocked": 1, "pending": 2}
    for status, sid, display in sorted(candidates, key=lambda v: (priority[v[0]], v[1])):
        if len(topics) == 3:
            break
        topics.append(dict(question="Confirm decision or next step: " + display,
                           disposition=status, source_ids=[sid], gap_references=[],
                           suggested_action="Verify cited evidence, then record the proposed next step.",
                           owner="unknown", due="unknown", minutes=5))
    # Whole combined model envelope is capped too. No model is called by this batch.
    require(len(canonical([f.to_dict() for f in ordered] +
                          [c.source.to_dict() for c in chats]).encode())
            <= scope.budget.max_model_input_bytes, "combined_input_limit")
    require(len(gaps) <= 500, "coverage_limit")
    return MeetingFacts(ordered, tuple(sorted(chats, key=lambda c: (
        _utc(c.source.event_at, "event_at"), c.source.source_id))), source_set,
        Coverage(False, tuple(sorted(gaps))), tuple(associations), tuple(topics))
