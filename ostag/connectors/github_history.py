"""Bounded explicit-repository GET reader. No discovery, cache, retry or resume.

Authorization is a trusted nonblocking local callback (<=100ms operational SLA),
never deserialized policy. Each invocation checks scope/grant/config/requester
and the exact GitHub principal binding. A parsed FrozenScope grants nothing.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
import hashlib
import json
import re
import time
import sys
from typing import Callable
from urllib.parse import quote, urlencode

from ostag.briefs.meeting_io import (
    CollectionError, RESPONSE_BYTES, TOTAL_BYTES, decode_json, run_process,
)
from ostag.briefs.meeting_models import FrozenScope, PartitionReport, PartitionStatus
from ostag.briefs.models import _utc
from ostag.briefs.project_mapping import Repository

# Existing gh connectors use the default REST contract (2022-11-28); pin it here.
API_VERSION = "2022-11-28"
PAGE_SIZE = 50
CATEGORY_PAGES = 4
REPO_PAGES = 30
CATEGORY_ITEMS = 100
REPO_ITEMS = 200


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False)


def sha256(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def require(value, category="invalid_evidence"):
    if not value:
        raise CollectionError(category)


@dataclass(frozen=True)
class GitHubPrincipal:
    user_id: str
    login: str

    def __post_init__(self):
        require(type(self.user_id) is str and re.fullmatch(r"[1-9][0-9]{0,19}", self.user_id))
        require(type(self.login) is str and re.fullmatch(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*",
                                                       self.login))


@dataclass(frozen=True, repr=False)
class HistoryItem:
    repo_id: str
    category: str
    kind: str
    observed_at: str
    raw_json: str = field(repr=False)
    parent_number: int | None = None

    def to_dict(self):
        return dict(repo_id=self.repo_id, category=self.category, kind=self.kind,
                    observed_at=self.observed_at, raw=json.loads(self.raw_json),
                    parent_number=self.parent_number)


@dataclass(frozen=True, repr=False)
class HistoryResult:
    scope_hash: str
    principal: GitHubPrincipal
    items: tuple[HistoryItem, ...]
    reports: tuple[PartitionReport, ...]
    gaps: tuple[str, ...]
    calls: int
    response_bytes: int
    # Diagnostic only. This record cannot resume/replenish a collection capability.
    collection_seconds: int

    def to_dict(self):
        return dict(scope_hash=self.scope_hash,
                    principal=dict(user_id=self.principal.user_id, login=self.principal.login),
                    items=[i.to_dict() for i in self.items],
                    reports=[r.to_dict() for r in self.reports], gaps=list(self.gaps),
                    calls=self.calls, response_bytes=self.response_bytes,
                    collection_seconds=self.collection_seconds, resume_supported=False)


Authorization = Callable[[FrozenScope, GitHubPrincipal, Repository], bool]


class _Reader:
    def __init__(self, scope, principal, authorize, runner, clock, wall):
        self.scope, self.principal, self.authorize = scope, principal, authorize
        self.runner, self.clock, self.wall = runner, clock, wall
        self.end = clock() + min(scope.budget.collection_seconds,
                                 scope.budget.overall_seconds - scope.budget.generation_reserve_seconds)
        self.calls = self.bytes = 0
        self.pages, self.repo_pages, self.counts, self.repo_counts = {}, {}, {}, {}
        self.items, self.gaps, self.failed = [], set(), set()
        self.paths = {r.repo_id: f"repos/{r.full_name}" for r in scope.repositories}

    def gate(self, repo):
        require(self.authorize(self.scope, self.principal, repo) is True, "authorization_denied")
        require(self.clock() < self.end, "collection_deadline")

    def get(self, repo, suffix="", params=None, *, user=False):
        self.gate(repo)
        # Only code-owned suffixes; provider URLs and pagination Link URLs are never dispatched.
        require(user or re.fullmatch(
            r"(?:|/pulls|/issues|/issues/comments|/commits|/releases|/actions/runs|/deployments"
            r"|/commits/[A-Za-z0-9%_.~-]+|/issues/[1-9][0-9]*/timeline"
            r"|/pulls/[1-9][0-9]*/(?:reviews|comments)"
            r"|/deployments/[1-9][0-9]*/statuses)", suffix), "endpoint_denied")
        require(self.calls < self.scope.budget.max_provider_calls, "call_limit")
        require(self.bytes + RESPONSE_BYTES <= TOTAL_BYTES, "byte_limit")
        path = "user" if user else self.paths[repo.repo_id] + suffix
        query = "?" + urlencode(params) if params else ""
        argv = [sys.executable, "-c",
                "from ostag.connectors.github_history import _http_get; _http_get()", path + query]
        self.calls += 1
        try:
            raw, size = self.runner(argv, deadline=min(self.end, self.clock() + 15), clock=self.clock)
        except Exception:
            # A failed/oversize command still consumed capacity. Reserve its full bounded pipe cap.
            self.bytes += RESPONSE_BYTES
            raise
        require(type(raw) is bytes and type(size) is int and size >= len(raw), "response_limit")
        self.bytes += size
        require(size <= RESPONSE_BYTES and self.bytes <= TOTAL_BYTES, "response_limit")
        self.gate(repo)
        # The owned HTTPS worker emits status + headers + JSON. Reject 304 (no cache),
        # redirects and all non-200 responses; worker never follows redirects.
        header, sep, body = raw.replace(b"\r\n", b"\n").partition(b"\n\n")
        require(sep and re.fullmatch(rb"HTTP/\S+ 200(?: [^\n]*)?", header.split(b"\n")[0]),
                "http_response_denied")
        headers = {}
        for line in header.split(b"\n")[1:]:
            key, colon, val = line.partition(b":")
            require(colon, "invalid_headers")
            key = key.strip().lower()
            require(key not in headers, "invalid_headers")
            headers[key] = val.strip().decode("ascii")
        return decode_json(body), headers

    def identity(self, repo):
        user, _ = self.get(repo, user=True)
        require(type(user) is dict and type(user.get("id")) is int
                and str(user["id"]) == self.principal.user_id
                and user.get("login") == self.principal.login, "identity_mismatch")
        meta, _ = self.get(repo)
        require(type(meta) is dict and type(meta.get("id")) is int
                and str(meta["id"]) == repo.repo_id and meta.get("full_name") == repo.full_name
                and meta.get("default_branch") == repo.default_branch
                and meta.get("permissions", {}).get("pull") is True, "repository_access_denied")

    def gap(self, repo, category, reason):
        self.gaps.add(f"{repo.resource_id}:{category}:{reason}")

    def pages_for(self, repo, category, suffix, params=None):
        key = (repo.repo_id, category)
        seen, page = set(), 1
        while True:
            require(self.pages.get(key, 0) < CATEGORY_PAGES, "category_page_limit")
            require(self.repo_pages.get(repo.repo_id, 0) < REPO_PAGES, "repo_page_limit")
            self.pages[key] = self.pages.get(key, 0) + 1
            self.repo_pages[repo.repo_id] = self.repo_pages.get(repo.repo_id, 0) + 1
            rows, headers = self.get(repo, suffix, (params or {}) | {"per_page": PAGE_SIZE,
                                                                   "page": page})
            if suffix == "/actions/runs":
                require(type(rows) is dict and type(rows.get("total_count")) is int)
                if rows["total_count"] >= 1000:
                    self.gap(repo, category, "search_1000_ceiling_possible")
                rows = rows.get("workflow_runs")
            require(type(rows) is list and len(rows) <= PAGE_SIZE, "invalid_page")
            for row in rows:
                require(type(row) is dict)
                identity = row.get("id", row.get("sha"))
                require(type(identity) in (int, str), "missing_record_id")
                require(identity not in seen, "pagination_overlap_or_loop")
                seen.add(identity)
                yield row
            link = headers.get(b"link", "")
            next_links = [p for p in link.split(",") if re.search(r'rel="next"', p)]
            if next_links:
                require(len(next_links) == 1 and len(rows) > 0, "invalid_next_page")
                # Validate expected numeric continuation; never follow this URL.
                expected = "https://api.github.com/" + self.paths[repo.repo_id] + suffix + "?"
                match = re.fullmatch(r'\s*<([^<>]+)>;\s*rel="next"\s*', next_links[0])
                require(match and match[1].startswith(expected), "invalid_next_page")
                from urllib.parse import parse_qs, urlsplit
                query = parse_qs(urlsplit(match[1]).query)
                wanted = {k: [str(v)] for k, v in (params or {}).items()}
                wanted |= {"page": [str(page + 1)], "per_page": [str(PAGE_SIZE)]}
                require(query == wanted, "invalid_next_page")
            else:
                if len(rows) == PAGE_SIZE:
                    self.gap(repo, category, "missing_next_page")
                return
            page += 1

    def add(self, repo, category, kind, row, parent=None):
        key = (repo.repo_id, category)
        require(len(self.items) < self.scope.budget.max_github_facts, "global_fact_limit")
        require(self.counts.get(key, 0) < CATEGORY_ITEMS, "category_fact_limit")
        require(self.repo_counts.get(repo.repo_id, 0) < REPO_ITEMS, "repo_fact_limit")
        raw = canonical(row)
        require(sum(len(i.raw_json.encode()) for i in self.items) + len(raw.encode())
                <= self.scope.budget.max_model_input_bytes, "input_byte_limit")
        at = self.wall()
        _utc(at, "observed_at")
        self.items.append(HistoryItem(repo.repo_id, category, kind, at, raw, parent))
        self.counts[key] = self.counts.get(key, 0) + 1
        self.repo_counts[repo.repo_id] = self.repo_counts.get(repo.repo_id, 0) + 1

    def category(self, repo, category):
        # Pagination is a current view: absent/deleted/rewritten records cannot be reconstructed.
        self.gap(repo, category, "mutable_history_not_atomic")
        if category in ("reviews", "review_comments"):
            for pr in self.pages_for(repo, category, "/pulls", {"state": "all"}):
                number = pr.get("number")
                require(type(number) is int and number > 0)
                suffix = "reviews" if category == "reviews" else "comments"
                for row in self.pages_for(repo, category, f"/pulls/{number}/{suffix}"):
                    self.add(repo, category, category, row, number)
            return
        if category == "checks":
            self.gap(repo, category, "non_actions_checks_and_attempt_history_uncollected")
        suffix = {"issue_comments": "/issues/comments", "checks": "/actions/runs"}.get(
            category, "/" + category)
        params = {"state": "all"} if category in ("pulls", "issues") else {}
        if category == "commits":
            head, _ = self.get(repo, "/commits/" + quote(repo.default_branch, safe=""))
            require(type(head) is dict and type(head.get("sha")) is str
                    and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", head["sha"]))
            params = {"sha": head["sha"]}  # No author/committer since/until filter.
            self.gap(repo, category, "arrival_unknown_no_trusted_prior_head")
        for row in self.pages_for(repo, category, suffix, params):
            self.add(repo, category, category, row)
            if category in ("pulls", "issues"):
                number = row.get("number")
                require(type(number) is int and number > 0)
                for event in self.pages_for(repo, category, f"/issues/{number}/timeline"):
                    self.add(repo, category, "timeline", event, number)
            elif category == "deployments":
                number = row.get("id")
                require(type(number) is int and number > 0)
                for status in self.pages_for(repo, category, f"/deployments/{number}/statuses"):
                    self.add(repo, category, "deployment_status", {"deployment": row,
                                                                  "status": status}, number)

    def run(self):
        for repo in self.scope.repositories:
            try:
                self.identity(repo)
            except Exception:
                self.failed.add(repo.repo_id)
                continue
            for category in self.scope.request.source_categories:
                reason = None
                try:
                    self.category(repo, category.value)
                except CollectionError as exc:
                    reason = exc.category if exc.category in SAFE_FAILURES else "collection_failed"
                except Exception:
                    reason = "invalid_provider_evidence"
                if reason:
                    self.gap(repo, category.value, reason)
            try:
                self.identity(repo)
            except Exception:
                self.failed.add(repo.repo_id)
        # Recheck all access after other repositories; a later revocation withholds earlier content.
        for repo in self.scope.repositories:
            try:
                self.gate(repo)
            except Exception:
                self.failed.add(repo.repo_id)
        self.items = [i for i in self.items if i.repo_id not in self.failed]
        reports = []
        for partition in self.scope.partitions:
            pid = partition.partition_id
            if pid.startswith("feishu:"):
                status = PartitionStatus.MISSING
                self.gaps.add(pid + ":chat_collection_not_supplied")
            elif pid.split(":")[2] in self.failed:
                status = PartitionStatus.FAILED
                self.gaps.add(pid + ":identity_or_authorization_failed_content_withheld")
            else:
                status = PartitionStatus.PARTIAL
            reports.append(PartitionReport(pid, status))
        return HistoryResult(self.scope.content_hash, self.principal, tuple(self.items),
                             tuple(reports), tuple(sorted(self.gaps)), self.calls, self.bytes,
                             self.scope.budget.collection_seconds)


SAFE_FAILURES = frozenset({"authorization_denied", "collection_deadline", "endpoint_denied",
    "call_limit", "byte_limit", "response_limit", "http_response_denied", "invalid_headers",
    "category_page_limit", "repo_page_limit", "invalid_page", "missing_record_id",
    "pagination_overlap_or_loop", "invalid_next_page", "global_fact_limit",
    "category_fact_limit", "repo_fact_limit", "input_byte_limit", "invalid_evidence"})


def collect_history(scope: FrozenScope, *, expected_principal: GitHubPrincipal,
                    authorize: Authorization, runner=run_process, clock=time.monotonic,
                    wall=lambda: datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                    resume=None) -> HistoryResult:
    """Single-use global budget. Any resume (even a plausible JSON budget) is refused.

    Runner injection is for trusted application code/tests, never request data.
    The callback must revalidate the exact scope hash, namespace, requester,
    grant/revision/hash, mapping/config pins and resource-to-GitHub-user binding.
    No IO occurs unless this callable is explicitly invoked.
    """
    failed = False
    try:
        require(type(scope) is FrozenScope and type(expected_principal) is GitHubPrincipal)
        require(resume is None, "resume_unsupported")
        require(callable(authorize) and not scope.intake_reasons, "scope_denied")
        return _Reader(scope, expected_principal, authorize, runner, clock, wall).run()
    except Exception:
        failed = True
    if failed:
        raise CollectionError("history_request_denied")


def _http_get():
    """Owned child-process entry, never called on import or in automated provider tests.

    Only preexisting GH_TOKEN/GITHUB_TOKEN environment credentials are supported.
    No keychain discovery, auth CLI, proxy, redirects, cache or token diagnostics.
    http.client does not follow redirects. Parent run_process enforces kill/reap,
    pipe size and the tighter collection deadline, including DNS/connect hangs.
    """
    import http.client
    import os
    connection = None
    code = 1
    try:
        path = sys.argv[1]
        require(type(path) is str and len(path) < 4096 and path.startswith(("user", "repos/")))
        require(not any(c in path for c in "\r\n#") and "://" not in path)
        token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
        require(type(token) is str and 0 < len(token) < 4096
                and not any(c.isspace() for c in token))
        connection = http.client.HTTPSConnection("api.github.com", timeout=15)
        connection.request("GET", "/" + path, headers={
            "Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION, "User-Agent": "ostag-meeting-local/1",
            "Accept-Encoding": "identity",
        })
        response = connection.getresponse()
        # Only selected protocol fields; no cookies/credential-related headers in stdout.
        header = f"HTTP/1.1 {response.status}\r\n"
        links = response.getheaders()
        for key, value in links:
            if key.lower() == "link":
                require("\r" not in value and "\n" not in value)
                header += "Link: " + value + "\r\n"
        header += "\r\n"
        raw = header.encode("ascii") + response.read(RESPONSE_BYTES + 1)
        require(len(raw) <= RESPONSE_BYTES, "response_limit")
        sys.stdout.buffer.write(raw)
        code = 0
    except Exception:
        pass
    finally:
        if connection is not None:
            connection.close()
    raise SystemExit(code)
