"""Historical event facts and current observations are deliberately separate.

ProviderChangeEvidence remains the current-head checks authority. This module
never promotes a release, workflow snapshot or deployment record to current
production state; deletion, rollback and rerun histories are not reconstructible.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import re
import unicodedata
from urllib.parse import quote

from ostag.briefs.meeting_models import FrozenScope
from ostag.briefs.models import SourceRef, _utc
from ostag.connectors.github_history import HistoryItem, GitHubPrincipal, require, sha256
from ostag.core.provider_evidence import ProviderChangeEvidence


def display_text(value: str) -> str:
    """Plaintext data, never Markdown/HTML; raw provenance is kept separately.

    Matches the consolidated preview's plaintext contract without depending on
    the older preview's URL deletion/Markdown escaping in this builder base.
    """
    return "".join(c if c not in "\r\n\t" else " " for c in value
                   if not unicodedata.category(c).startswith("C") or c in "\r\n\t")


@dataclass(frozen=True, repr=False)
class ChangeFact:
    source_id: str
    resource_id: str
    category: str
    kind: str
    event_at: str | None
    observed_at: str
    raw_source_sha256: str
    display: str = field(repr=False)
    citation: str
    status: str
    caveats: tuple[str, ...]
    source: SourceRef | None = field(default=None, repr=False)
    owner: str = "unknown"
    due: str = "unknown"

    def to_dict(self):
        return {key: getattr(self, key) for key in (
            "source_id", "resource_id", "category", "kind", "event_at", "observed_at",
            "raw_source_sha256", "display", "citation", "status", "owner", "due"
        )} | {"caveats": list(self.caveats)}


def current_checks(evidence: ProviderChangeEvidence, *, repository: str, head_sha: str,
                   branch: str, observed_branch: str, now: str, fresh_seconds: int,
                   latest_attempt_verified: bool = False) -> str:
    """Trusted integration seam, not a workflow-list-to-checks coercion.

    The caller must freshly establish head/branch and latest attempt externally.
    No name matching, merge-SHA substitution or archived green promotion.
    """
    try:
        require(type(evidence) is ProviderChangeEvidence)
        age = (_utc(now, "now") - _utc(evidence.observed_at, "observed_at")).total_seconds()
        require(type(fresh_seconds) is int and 0 < fresh_seconds <= 300)
        require(evidence.provider == "github" and evidence.repository == repository
                and evidence.head_sha == head_sha and branch == observed_branch
                and latest_attempt_verified is True and 0 <= age <= fresh_seconds)
        return evidence.checks_state
    except Exception:
        return "unknown"


def _number(value):
    require(type(value) is int and 0 < value < 10**20)
    return value


def _sha(value):
    require(type(value) is str and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value))
    return value


def _classify(scope: FrozenScope, principal: GitHubPrincipal, item: HistoryItem):
    """Return facts only for real event times in [start,end), plus unknown-time observations.

    Unknown event times never receive a fabricated SourceRef timestamp. Their
    metadata/citations remain in the observation ledger and block readiness.
    """
    repo = next((r for r in scope.repositories if r.repo_id == item.repo_id), None)
    require(repo is not None and item.category in {c.value for c in scope.request.source_categories})
    row = json.loads(item.raw_json)
    require(type(row) is dict)
    _utc(item.observed_at, "observed_at")
    base = f"https://github.com/{repo.full_name}"
    caveats = ["current_text_not_historical_sequence"]
    events = []
    status, version = "observed", "unknown"
    text = row.get("title") or row.get("name") or row.get("body") or item.kind
    require(type(text) is str and len(text.encode()) <= 65536)
    if item.kind == "pulls":
        number = _number(row.get("number"))
        target = row.get("base", {})
        require(type(target) is dict and target.get("repo", {}).get("full_name") == repo.full_name
                and type(target.get("repo", {}).get("id")) is int
                and str(target["repo"]["id"]) == repo.repo_id)
        citation = f"{base}/pull/{number}"
        version = _sha(row.get("head", {}).get("sha"))
        if target.get("ref") != repo.default_branch:
            caveats.append("target_is_not_default_branch")
        require(row.get("state") in ("open", "closed"))
        if row.get("merged_at") is not None:
            require(row["state"] == "closed")
            events.append(("pr_merged", row["merged_at"]))
            caveats.append("merged_not_deployed")
        elif row["state"] == "closed":
            require("merged_at" in row)  # Missing is not an explicit unmerged observation.
            events.append(("pr_closed_unmerged", row.get("closed_at")))
            caveats.append("closed_not_merged_or_deployed")
        events.append(("pr_created", row.get("created_at")))
        # An old open PR is relevant pending input, but its *current* state has no window event.
        if row.get("state") == "open":
            events.append(("pr_open_observed", None))
            status = "pending"
    elif item.kind == "issues":
        if "pull_request" in row:
            return ()
        require(row.get("repository_url") == f"https://api.github.com/repos/{repo.full_name}")
        citation = f"{base}/issues/{_number(row.get('number'))}"
        events.append(("issue_created", row.get("created_at")))
        if row.get("closed_at"):
            events.append(("issue_closed", row["closed_at"]))
        if row.get("state") == "open":
            status = "pending"
            events.append(("issue_open_observed", None))
    elif item.kind in ("issue_comments", "review_comments", "reviews", "timeline"):
        number = item.parent_number
        if item.kind == "issue_comments":
            match = re.fullmatch(re.escape(f"https://api.github.com/repos/{repo.full_name}/issues/")
                                 + r"([1-9][0-9]{0,19})", row.get("issue_url", ""))
            require(match is not None)
            number = int(match[1])
        number = _number(number)
        citation = f"{base}/issues/{number}"
        if item.kind == "issue_comments":
            citation += f"#issuecomment-{_number(row.get('id'))}"
        if item.kind == "timeline":
            # Unknown timeline variants (e.g. commit events without created_at) are observations.
            kind = row.get("event")
            require(type(kind) is str and re.fullmatch(r"[a-z_]{1,80}", kind))
            events.append(("timeline_" + kind, row.get("created_at")))
        else:
            events.append((item.kind, row.get("submitted_at") if item.kind == "reviews"
                           else row.get("created_at")))
    elif item.kind == "commits":
        version = _sha(row.get("sha"))
        citation = f"{base}/commit/{version}"
        text = row.get("commit", {}).get("message")
        require(type(text) is str and len(text.encode()) <= 65536)
        events.append(("default_branch_observed_commit", None))
        caveats.append("arrival_unknown_author_and_committer_dates_not_arrival")
    elif item.kind == "releases":
        require(type(row.get("draft")) is bool)
        if row["draft"] or row.get("published_at") is None:
            return ()
        tag = row.get("tag_name")
        require(type(tag) is str and 0 < len(tag) <= 255 and tag not in (".", ".."))
        citation = base + "/releases/tag/" + quote(tag, safe="")
        events.append(("release_published", row["published_at"]))
        caveats.append("published_release_not_deployment")
    elif item.kind == "checks":
        require(type(row.get("repository", {}).get("id")) is int
                and str(row["repository"]["id"]) == repo.repo_id
                and row["repository"].get("full_name") == repo.full_name)
        citation = f"{base}/actions/runs/{_number(row.get('id'))}"
        version = _sha(row.get("head_sha"))
        kind = "actions_success_observed" if row.get("status") == "completed" and row.get(
            "conclusion") == "success" else "actions_state_observed"
        events.append((kind, None))  # updated_at is NOT a documented completion timestamp.
        caveats.extend(("workflow_snapshot_not_current_head_proof", "rerun_history_unproven"))
        if row.get("head_branch") != repo.default_branch:
            caveats.append("different_branch")
    elif item.kind in ("deployments", "deployment_status"):
        dep = row if item.kind == "deployments" else row.get("deployment", {})
        state = {} if item.kind == "deployments" else row.get("status", {})
        _number(dep.get("id"))
        version = _sha(dep.get("sha"))
        # Code-owned selected-repo commit citation; no environment/log URL dispatch or rendering.
        citation = f"{base}/commit/{version}"
        receipt = (item.kind == "deployment_status" and type(state.get("id")) is int
                   and state["id"] > 0 and state.get("state") == "success"
                   and dep.get("production_environment") is True
                   and type(dep.get("environment")) is str and bool(dep["environment"])
                   and state.get("environment") == dep["environment"]
                   and dep.get("ref") == repo.default_branch)
        kind = "production_deployment_reported" if receipt else "deployment_unknown"
        events.append((kind, state.get("created_at") if receipt else None))
        text = kind + "; environment: " + str(dep.get("environment") or "unknown")
        caveats.extend(("current_deployment_unknown", "rollback_or_deleted_status_unproven"))
    else:
        require(False)
    updated = row.get("updated_at")
    if updated is not None and _utc(updated, "updated_at") >= _utc(scope.request.window.end, "end"):
        caveats.append("text_updated_at_or_after_window_end")
    facts = []
    for kind, at in events:
        if at is not None:
            event = _utc(at, "event_at")
            require(event <= _utc(item.observed_at, "observed_at"))
            if not _utc(scope.request.window.start, "start") <= event < _utc(
                    scope.request.window.end, "end"):
                continue
        else:
            caveats_for_fact = (*caveats, "event_time_unknown_outside_temporal_source_set")
        if at is not None:
            caveats_for_fact = tuple(caveats)
        raw_hash = sha256(row)
        sid = "gh:" + sha256([repo.repo_id, citation, kind, at, row.get("id"),
                               item.parent_number])[:40]
        display = display_text(text[:2000])
        source = None if at is None else SourceRef(
            namespace=scope.request.namespace, source_id=sid, resource_id=repo.resource_id,
            source_kind="github_event", resource_version=version, window=scope.request.window,
            event_at=at, observed_at=item.observed_at, content_sha256=raw_hash,
            grant_id=scope.request.grant_id, grant_revision=scope.request.grant_revision,
            collection_platform="github", collection_actor_id=principal.user_id,
            purpose=scope.request.purpose.value, allowed_audience=scope.request.audience,
            visibility="private", excerpt=text[:2000])
        facts.append(ChangeFact(sid, repo.resource_id, item.category, kind, at, item.observed_at,
                                raw_hash, display, citation, status, caveats_for_fact, source))
    return tuple(facts)


def classify(scope: FrozenScope, principal: GitHubPrincipal, item: HistoryItem):
    """Public normalization boundary: invalid source bodies never escape in exception chains."""
    from ostag.briefs.meeting_io import CollectionError
    try:
        return _classify(scope, principal, item)
    except Exception:
        pass
    raise CollectionError("invalid_change_evidence")
