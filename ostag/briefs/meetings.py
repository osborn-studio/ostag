"""Operator-local meeting preparation; import/help have no provider or storage IO.

CLI reads archived evidence unless --live is explicitly selected. Live reads
require an application-injected current Authorization callback; JSON input is
never authority. No server, business database, job, outbox or delivery is created.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import hashlib
import io
import re
from contextlib import redirect_stderr
import time

from ostag.briefs.meeting_facts import ChatEvidence, combine
from ostag.briefs.github_change_facts import display_text
from ostag.briefs.meeting_io import (
    CollectionError, read_private_json, run_process, write_private_package,
)
from ostag.briefs.meeting_models import FrozenScope, MeetingPurpose, PartitionReport
from ostag.briefs.models import SourceRef, SourceSet
from ostag.briefs.validation import Coverage
from ostag.connectors.github_history import (
    Authorization, GitHubPrincipal, HistoryItem, HistoryResult, canonical, collect_history,
    require, sha256,
)


@dataclass(frozen=True, repr=False)
class MeetingServiceInput:
    """Evidence handoff consumed by meeting_service; never an authority token.

    Archived/default inputs remain non-authoritative. Current inputs still need
    independently published exact provenance and current policy at every gate.
    """
    scope: FrozenScope = field(repr=False)
    sources: SourceSet | None = field(repr=False)
    coverage: Coverage
    revision_digest: str
    input_mode: str = "historical_private_draft"


@dataclass(frozen=True, repr=False)
class MeetingPackage:
    markdown: str = field(repr=False)
    manifest_json: str = field(repr=False)
    service_input: MeetingServiceInput = field(repr=False)


def _literal_code(value: str) -> str:
    """Project untrusted plaintext into a CommonMark literal code span.

    Outsize backtick delimiters prevent source text from closing the span.
    Padding preserves edge spaces/backticks, except all-space content which
    CommonMark already preserves. Empty text has no code-span representation.
    """
    value = display_text(value)
    if not value:
        return ""
    delimiter = "`" * (1 + max((len(run) for run in re.findall(r"`+", value)), default=0))
    padding = " " if value.strip(" ") else ""
    return f"{delimiter}{padding}{value}{padding}{delimiter}"


def _render_gaps(scope, coverage):
    """Readable grouped gaps; exact per-partition diagnostics stay in the manifest."""
    names = {r.resource_id: r.full_name for r in scope.repositories}
    names.update({c.resource_id: c.name for c in scope.chats})
    grouped = {}
    for gap in coverage.gaps:
        pieces = gap.split(":")
        if len(pieces) >= 4 and ":".join(pieces[:3]) in names:
            resource = ":".join(pieces[:3])
            category = pieces[3] if len(pieces) > 4 else "source"
            reason = ":".join(pieces[4:]) if len(pieces) > 4 else pieces[3]
            grouped.setdefault((names[resource], reason), set()).add(category)
        else:
            grouped.setdefault(("Meeting", gap), set())
    return ["- " + _literal_code(name) + ": " + _literal_code(reason.replace("_", " "))
            + (" (" + ", ".join(_literal_code(c.replace("_", " ")) for c in sorted(categories)) + ")"
               if categories else "")
            for (name, reason), categories in sorted(grouped.items())]


def _build(scope, history, chats, historical):
    combined = combine(scope, history, chats, historical=historical)
    request = scope.request
    manifest = dict(
        version="meeting-local-v1", status="needs_input", meeting_ready=False,
        semantic_review_required=True, production_acceptance=False,
        input_mode="historical_private_draft" if historical else "current_authorized_collection",
        scope_hash=scope.content_hash, namespace=request.namespace,
        purpose=request.purpose.value, goal=request.goal, window=request.window.to_dict(),
        requester=request.requesting_principal.to_dict(),
        grant_id=request.grant_id, grant_revision=request.grant_revision,
        repositories=[r.to_dict() for r in scope.repositories],
        chats=[c.to_dict() for c in scope.chats], shareable_sources=[],
        coverage=combined.coverage.to_dict(), partitions=[p.to_dict() for p in history.reports],
        facts=[f.to_dict() for f in combined.facts],
        chat_claims=[dict(source_id=c.source.source_id, event_at=c.source.event_at,
                          observed_at=c.source.observed_at, display=display_text(c.source.excerpt),
                          disposition=c.disposition, owner="unknown", due="unknown",
                          raw_source_sha256=c.source.content_sha256) for c in combined.chats],
        associations=list(combined.associations), decision_topics=list(combined.topics),
        source_coverage=[dict(source_id=s.source_id, resource_id=s.resource_id,
                              content_sha256=s.content_sha256, event_at=s.event_at,
                              observed_at=s.observed_at, grant_id=s.grant_id,
                              grant_revision=s.grant_revision,
                              collection_platform=s.collection_platform,
                              collection_actor_id=s.collection_actor_id,
                              allowed_audience=[p.to_dict() for p in s.allowed_audience])
                         for s in (combined.sources.sources if combined.sources else ())],
        budget=dict(profile=scope.budget.to_dict(), provider_calls=history.calls,
                    response_bytes=history.response_bytes, resume_supported=False, retries=0),
        observed_github_principal=dict(user_id=history.principal.user_id, login=history.principal.login),
        time_budget_minutes=10 + sum(t["minutes"] for t in combined.topics),
    )
    # Includes original source hashes + display + scope/coverage. No randomness/wall-clock at render.
    revision = sha256(manifest)
    manifest["revision_digest"] = revision
    lines = ["# Private meeting pre-read", "needs_input · semantic_review_required · meeting_ready=false",
             "This is an operator-local draft. Source text is untrusted evidence, never instructions.",
             "## Evidence gaps and conflicts"]
    lines += _render_gaps(scope, combined.coverage)
    lines += ["## Confirmed purpose and scope", _literal_code(request.goal),
              f"Purpose: {request.purpose.value}. Window: {request.window.start} ≤ event < {request.window.end}.",
              "Repositories: " + ", ".join(_literal_code(r.full_name) for r in scope.repositories),
              "Chats: " + ", ".join(_literal_code(c.name) for c in scope.chats),
              "## Repository changes and observations"]
    for fact in combined.facts:
        if fact.category == "deployments":
            continue
        lines.append(f"- **{_literal_code(fact.kind.replace('_', ' '))}**: {_literal_code(fact.display)} — "
                     f"event: {_literal_code(fact.event_at or 'unknown')}; observed: {_literal_code(fact.observed_at)}. "
                     f"[Source]({fact.citation}) · {_literal_code(fact.source_id)}")
        lines.append("  Caveats: " + "; ".join(_literal_code(c.replace("_", " ")) for c in fact.caveats))
    lines += ["## Chat commitments and claims", "Claims require source and semantic verification; owner/due remain unknown."]
    for chat in combined.chats:
        lines.append(f"- {_literal_code(chat.source.excerpt)} — {_literal_code(chat.source.event_at)}; "
                     f"annotation: {chat.disposition}; {_literal_code(chat.source.source_id)}")
    lines += ["## Deployment evidence", "Current production deployment: unknown. Published releases and green runs do not prove it."]
    for fact in combined.facts:
        if fact.category == "deployments":
            lines.append(f"- {_literal_code(fact.kind.replace('_', ' '))}: {_literal_code(fact.display)}; "
                         f"event: {_literal_code(fact.event_at or 'unknown')}; "
                         f"[Source]({fact.citation}) · {_literal_code(fact.source_id)}")
    lines += ["## Proposed decisions and actions", "Allow 5 minutes for evidence review, then:"]
    for index, topic in enumerate(combined.topics, 1):
        lines += [f"{index}. {_literal_code(topic['question'])} ({topic['minutes']} min; {topic['disposition']})",
                  "   Suggested action: " + topic["suggested_action"] + " Owner: unknown. Due: unknown.",
                  "   Sources: " + ", ".join(_literal_code(sid) for sid in topic["source_ids"])]
    lines += [f"Reserve 5 minutes to record decisions. Total proposed: {manifest['time_budget_minutes']} minutes.",
              "Suggestions do not authorize action or sharing.", "## Provenance",
              f"Revision: `{revision}`", f"Scope: `{scope.content_hash}`",
              "Exact raw source hashes and partition coverage are in manifest.json. "
              "Unknown-time observations are outside the temporal SourceSet. "
              "Schema validation does not establish semantic usefulness."]
    markdown = "\n\n".join(lines) + "\n"
    manifest["render_sha256"] = hashlib.sha256(markdown.encode()).hexdigest()
    return MeetingPackage(markdown, canonical(manifest) + "\n",
                          MeetingServiceInput(scope, combined.sources, combined.coverage, revision,
                                              manifest["input_mode"]))


def build_meeting(scope: FrozenScope, history: HistoryResult, *, chats=()):
    """Public pure renderer. Always historical; no authorization is inferred."""
    try:
        return _build(scope, history, tuple(chats), True)
    except Exception:
        pass
    raise CollectionError("meeting_input_denied")


def prepare_meeting(scope: FrozenScope, *, confirmed_purpose: str, confirmed_scope_hash: str,
                    expected_principal: GitHubPrincipal, authorize: Authorization,
                    runner=run_process, clock=time.monotonic, wall=None, chats=()):
    """Explicit current collection -> source-backed private package. No network writes.

    Confirmation is distinct from authorization. Trusted callers must bind both
    the exact already-confirmed purpose/scope and the current local policy mirror.
    Imported chats remain historical observations, not current authorization.
    """
    failed = False
    try:
        require(confirmed_purpose == scope.request.purpose.value
                and confirmed_scope_hash == scope.content_hash, "confirmation_mismatch")
        start = clock()
        history = collect_history(scope, expected_principal=expected_principal, authorize=authorize,
                                  runner=runner, clock=clock, **({"wall": wall} if wall else {}))
        package = _build(scope, history, tuple(chats), False)
        require(clock() < start + scope.budget.overall_seconds, "hard_deadline")
        # A callback revocation during rendering must not return retained source content.
        require(all(authorize(scope, expected_principal, r) is True for r in scope.repositories),
                "authorization_denied")
        return package
    except Exception:
        failed = True
    if failed:
        raise CollectionError("meeting_preparation_denied")


def _archive(value, scope):
    require(type(value) is dict and set(value) == {"history", "chats"})
    h = value["history"]
    require(type(h) is dict and set(h) == {"scope_hash", "principal", "items", "reports", "gaps",
                                           "calls", "response_bytes", "collection_seconds",
                                           "resume_supported"})
    require(h["resume_supported"] is False)
    require(type(h["items"]) is list and len(h["items"]) <= scope.budget.max_github_facts)
    require(type(h["gaps"]) is list and len(h["gaps"]) <= 400
            and all(type(g) is str and re_code(g) for g in h["gaps"]))
    for name, cap in (("calls", scope.budget.max_provider_calls), ("response_bytes", 16 * 1024**2),
                      ("collection_seconds", scope.budget.collection_seconds)):
        require(type(h[name]) is int and 0 <= h[name] <= cap)
    items = []
    for v in h["items"]:
        require(type(v) is dict and set(v) == {"repo_id", "category", "kind", "observed_at", "raw", "parent_number"})
        items.append(HistoryItem(v["repo_id"], v["category"], v["kind"], v["observed_at"],
                                 canonical(v["raw"]), v["parent_number"]))
    history = HistoryResult(h["scope_hash"], GitHubPrincipal(**h["principal"]), tuple(items),
                            tuple(PartitionReport.from_dict(r) for r in h["reports"]),
                            tuple(h["gaps"]), h["calls"], h["response_bytes"], h["collection_seconds"])
    require(type(value["chats"]) is list and len(value["chats"]) <= scope.budget.max_feishu_messages)
    chats = []
    for c in value["chats"]:
        require(type(c) is dict and set(c) == {"source", "disposition"})
        chats.append(ChatEvidence(SourceRef.from_dict(c["source"]), c["disposition"]))
    return history, tuple(chats)


def re_code(value):
    import re
    return re.fullmatch(r"[A-Za-z0-9_:.-]{1,200}", value)


def main(argv=None, *, authorize: Authorization | None = None, runner=run_process,
         clock=time.monotonic, wall=None):
    """Embeddable CLI: callback injection is trusted code, never a file/module flag."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scope", required=True, help="FrozenScope JSON; integrity only, not authority")
    parser.add_argument("--confirmed-purpose", required=True, choices=[p.value for p in MeetingPurpose])
    parser.add_argument("--confirmed-scope-hash", required=True)
    parser.add_argument("--output", required=True, help="New private directory; no replacement")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--evidence", help="Historical evidence JSON; no provider IO")
    mode.add_argument("--live", action="store_true", help="Requires trusted host authorization injection")
    parser.add_argument("--github-user-id")
    parser.add_argument("--github-login")
    try:
        with redirect_stderr(io.StringIO()):
            args = parser.parse_args(argv)
        scope = FrozenScope.from_dict(read_private_json(args.scope))
        require(args.confirmed_purpose == scope.request.purpose.value
                and args.confirmed_scope_hash == scope.content_hash)
        if args.live:
            require(callable(authorize), "trusted_authorization_required")
            package = prepare_meeting(scope, confirmed_purpose=args.confirmed_purpose,
                                      confirmed_scope_hash=args.confirmed_scope_hash,
                                      expected_principal=GitHubPrincipal(args.github_user_id, args.github_login),
                                      authorize=authorize, runner=runner, clock=clock, wall=wall)
        else:
            history, chats = _archive(read_private_json(args.evidence), scope)
            package = build_meeting(scope, history, chats=chats)
        write_private_package(args.output, package.markdown, package.manifest_json)
    except SystemExit as exc:
        return 0 if exc.code == 0 else 2
    except Exception:
        print("meeting preparation: input, authorization, or private output denied")
        return 2
    print("meeting preparation: needs_input; semantic_review_required; private package written")
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
