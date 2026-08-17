"""GitHub system-of-record connector, transported over the `gh` CLI.

Consistent with the repo's other connectors (lark-cli, dws-cli), all GitHub
operations shell out to an already-authenticated CLI — OS Tag never handles
GitHub tokens itself. Only non-destructive operations are implemented:
issue creation and issue comments. There is no close/delete path.

Writes reach GitHub exclusively through the action receipt protocol:
propose → approve → apply (`GitHubApplyExecutor`).
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from ostag.config import Settings
from ostag.core.safety import scrub_environment
from ostag.core.provider_evidence import (
    ProviderChangeEvidence,
    ProviderCheck,
    normalize_utc_timestamp,
)
from ostag.core.schemas import ActionReceipt, SchemaError


class GitHubCLIError(RuntimeError):
    pass


_GITHUB_REPOSITORY = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*$"
)
_GITHUB_SHA = re.compile(r"^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$")
_MAX_PROVIDER_BYTES = 1_048_576


@dataclass(frozen=True)
class PullRequestSource:
    repository: str
    number: int
    url: str
    head_sha: str
    head_ref: str


@dataclass(frozen=True)
class PullRequestReviewEvidence:
    review_id: int
    url: str
    state: str
    commit_id: str


def _provider_object(raw: str, description: str) -> dict[str, Any]:
    if len(raw.encode("utf-8")) > _MAX_PROVIDER_BYTES:
        raise SchemaError(
            "provider_evidence_limit",
            f"GitHub {description} exceeded 1 MiB",
            "Reduce the provider response.",
        )
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SchemaError(
            "invalid_provider_evidence",
            f"GitHub returned invalid {description} JSON",
            "Retry the provider read.",
        ) from exc
    if not isinstance(payload, dict):
        raise SchemaError(
            "invalid_provider_evidence",
            f"GitHub {description} response is not an object",
            "Retry the provider read.",
        )
    return payload


def _require_pull_target(repository: str, number: int) -> None:
    if not _GITHUB_REPOSITORY.fullmatch(repository):
        raise SchemaError(
            "invalid_apply_payload",
            "GitHub repository must use owner/name syntax",
            "Create a fresh receipt for an allowlisted repository.",
        )
    if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
        raise SchemaError(
            "invalid_apply_payload",
            "GitHub pull request number must be a positive integer",
            "Create a fresh receipt with a valid pull request URL.",
        )


def _valid_pull_url(value: object, repository: str, number: int) -> str:
    if not isinstance(value, str) or len(value) > 2048:
        return ""
    parsed = urlparse(value)
    expected_path = f"/{repository}/pull/{number}"
    if (
        parsed.scheme != "https"
        or parsed.hostname != "github.com"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path != expected_path
    ):
        return ""
    return value


class GhCLI:
    def __init__(self, config: Settings):
        self.config = config

    async def run(self, *args: str) -> str:
        proc = await asyncio.create_subprocess_exec(
            self.config.gh_bin,
            *args,
            env=scrub_environment(extra=self.config.env_scrub_extra),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=self.config.auto_base_cli_timeout_seconds
        )
        out = stdout.decode("utf-8", errors="replace").strip()
        err = stderr.decode("utf-8", errors="replace").strip()
        if proc.returncode != 0:
            raise GitHubCLIError((err or out or f"gh exited {proc.returncode}")[-3000:])
        return out


class GitHubIssuesConnector:
    def __init__(self, cli: GhCLI):
        self.cli = cli

    async def create_issue(
        self,
        repo: str,
        *,
        title: str,
        body: str = "",
        assignee: str = "",
        labels: tuple[str, ...] = (),
    ) -> str:
        """Create an issue; returns its URL (gh prints it on stdout)."""
        args = ["issue", "create", "--repo", repo, "--title", title, "--body", body]
        if assignee:
            args.extend(["--assignee", assignee])
        for label in labels:
            args.extend(["--label", label])
        return await self.cli.run(*args)

    async def comment_issue(self, repo: str, number: int, *, body: str) -> str:
        """Comment on an issue; returns the comment URL."""
        return await self.cli.run(
            "issue", "comment", str(number), "--repo", repo, "--body", body
        )


class GitHubPullRequestConnector:
    """Collect read-only PR, check, merge, and commit evidence."""

    _FIELDS = (
        "number,url,state,isDraft,mergedAt,headRefOid,mergeCommit,updatedAt,"
        "statusCheckRollup"
    )

    def __init__(self, cli: GhCLI, config: Settings):
        self.cli = cli
        self.config = config

    async def resolve_source(
        self, repo: str, number: int, *, expected_sha: str = ""
    ) -> PullRequestSource:
        """Resolve the immutable source for a same-repository pull request."""
        _require_pull_target(repo, number)
        payload = _provider_object(
            await self.cli.run("api", f"repos/{repo}/pulls/{number}"), "pull request"
        )
        head = payload.get("head")
        base = payload.get("base")
        if not isinstance(head, dict) or not isinstance(base, dict):
            raise SchemaError(
                "invalid_provider_evidence",
                "GitHub pull request source is malformed",
                "Retry the provider read.",
            )
        head_repo = head.get("repo")
        base_repo = base.get("repo")
        head_name = head_repo.get("full_name") if isinstance(head_repo, dict) else None
        base_name = base_repo.get("full_name") if isinstance(base_repo, dict) else None
        head_sha = head.get("sha")
        head_ref = head.get("ref")
        url = _valid_pull_url(payload.get("html_url"), repo, number)
        valid = (
            payload.get("number") == number
            and not isinstance(payload.get("number"), bool)
            and payload.get("state") == "open"
            and payload.get("draft") is False
            and payload.get("merged_at") is None
            and isinstance(head_sha, str)
            and _GITHUB_SHA.fullmatch(head_sha) is not None
            and isinstance(head_ref, str)
            and 0 < len(head_ref) <= 255
            and not any(ord(char) < 32 or ord(char) == 127 for char in head_ref)
            and isinstance(head_name, str)
            and isinstance(base_name, str)
            and bool(url)
        )
        if not valid:
            raise SchemaError(
                "invalid_provider_evidence",
                "GitHub pull request response is malformed or not releasable",
                "Use an open, non-draft pull request and retry.",
            )
        if head_name.casefold() != repo.casefold() or base_name.casefold() != repo.casefold():
            raise SchemaError(
                "provider_fork_unsupported",
                "GitHub fork pull requests are not eligible for Production release",
                "Release from a branch in the allowlisted repository.",
            )
        if expected_sha and head_sha != expected_sha:
            raise SchemaError(
                "provider_sha_mismatch",
                "GitHub PR head SHA changed after release approval",
                "Create a fresh release receipt for the new SHA.",
            )
        return PullRequestSource(repo, number, url, head_sha, head_ref)

    async def approve(
        self,
        repo: str,
        number: int,
        *,
        commit_id: str,
        body: str = "Approved by OStag release receipt.",
    ) -> PullRequestReviewEvidence:
        """Submit an APPROVE review bound to one exact PR commit."""
        _require_pull_target(repo, number)
        if not _GITHUB_SHA.fullmatch(commit_id):
            raise SchemaError(
                "invalid_apply_payload",
                "GitHub review commit_id is not a valid SHA",
                "Create a fresh SHA-bound release receipt.",
            )
        if not isinstance(body, str) or len(body) > 1000 or "\x00" in body:
            raise SchemaError(
                "invalid_apply_payload",
                "GitHub review body is invalid",
                "Use the bounded OStag review message.",
            )
        payload = _provider_object(
            await self.cli.run(
                "api",
                f"repos/{repo}/pulls/{number}/reviews",
                "--method",
                "POST",
                "--raw-field",
                f"commit_id={commit_id}",
                "--raw-field",
                "event=APPROVE",
                "--raw-field",
                f"body={body}",
            ),
            "pull request review",
        )
        review_id = payload.get("id")
        url = payload.get("html_url")
        valid_url = isinstance(url, str) and url.startswith(
            f"https://github.com/{repo}/pull/{number}#pullrequestreview-"
        )
        if (
            isinstance(review_id, bool)
            or not isinstance(review_id, int)
            or review_id <= 0
            or payload.get("state") != "APPROVED"
            or payload.get("commit_id") != commit_id
            or not valid_url
        ):
            raise SchemaError(
                "invalid_provider_evidence",
                "GitHub review response did not confirm the requested SHA approval",
                "Inspect GitHub and retry with a fresh release receipt.",
            )
        return PullRequestReviewEvidence(review_id, url, "APPROVED", commit_id)

    async def collect(
        self, repo: str, number: int, *, expected_sha: str = ""
    ) -> ProviderChangeEvidence:
        raw = await self.cli.run(
            "pr", "view", str(number), "--repo", repo, "--json", self._FIELDS
        )
        if len(raw.encode("utf-8")) > _MAX_PROVIDER_BYTES:
            raise SchemaError(
                "provider_evidence_limit", "GitHub evidence exceeded 1 MiB", "Reduce checks."
            )
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SchemaError(
                "invalid_provider_evidence", "GitHub returned invalid JSON", "Retry the read."
            ) from exc
        if not isinstance(payload, dict) or payload.get("number") != number:
            raise SchemaError(
                "invalid_provider_evidence", "GitHub PR response is malformed", "Retry the read."
            )
        checks = payload.get("statusCheckRollup", ())
        if not isinstance(checks, list) or len(checks) > self.config.provider_evidence_max_checks:
            raise SchemaError(
                "provider_evidence_limit", "GitHub check count exceeded its bound", "Reduce checks."
            )
        head_sha = payload.get("headRefOid", "")
        if expected_sha and head_sha != expected_sha:
            raise SchemaError(
                "provider_sha_mismatch", "GitHub PR head SHA changed", "Refresh expected SHA."
            )
        normalized_checks = tuple(self._check(item, head_sha) for item in checks)
        merged_at = payload.get("mergedAt")
        raw_state = str(payload.get("state", "")).upper()
        if raw_state not in {"OPEN", "CLOSED", "MERGED"}:
            raise SchemaError(
                "invalid_provider_evidence", "GitHub PR state is unknown", "Retry the read."
            )
        state = "merged" if merged_at else "open" if raw_state == "OPEN" else "closed"
        merge_commit = payload.get("mergeCommit")
        merge_sha = merge_commit.get("oid", "") if isinstance(merge_commit, dict) else ""
        return ProviderChangeEvidence(
            provider="github",
            repository=repo,
            number=number,
            url=payload.get("url", ""),
            state=state,
            draft=payload.get("isDraft"),
            head_sha=head_sha,
            merge_sha=merge_sha,
            observed_at=normalize_utc_timestamp(payload.get("updatedAt", "")),
            checks=normalized_checks,
        )

    @staticmethod
    def _check(payload: object, head_sha: str) -> ProviderCheck:
        if not isinstance(payload, dict):
            raise SchemaError(
                "invalid_provider_evidence", "GitHub check is malformed", "Retry the read."
            )
        kind = payload.get("__typename")
        if kind not in {"CheckRun", "StatusContext"}:
            raise SchemaError(
                "invalid_provider_evidence", "GitHub check type is unknown", "Retry the read."
            )
        if kind == "StatusContext":
            name = payload.get("context", "status")
            raw_state = str(payload.get("state", "")).upper()
            url = payload.get("targetUrl") or ""
            state = "passed" if raw_state == "SUCCESS" else "failed" if raw_state in {
                "ERROR", "FAILURE"
            } else "pending"
        else:
            name = payload.get("name", "check")
            conclusion = str(payload.get("conclusion") or "").upper()
            status = str(payload.get("status") or "").upper()
            url = payload.get("detailsUrl") or ""
            if conclusion in {"SUCCESS", "NEUTRAL", "SKIPPED"}:
                state = "passed"
            elif conclusion in {
                "FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE", "STALE"
            }:
                state = "failed"
            else:
                state = "pending" if status != "COMPLETED" or not conclusion else "failed"
        return ProviderCheck(str(name), state, head_sha, str(url))


class GitHubApplyExecutor:
    """Applies approved GitHub receipts. Non-destructive operations only."""

    def __init__(self, connector: GitHubIssuesConnector, config: Settings):
        self.connector = connector
        self.config = config

    def _repo(self, payload: dict[str, Any], action_id: str) -> str:
        repo = payload.get("repo") or self.config.github_default_repo
        if not repo or "/" not in repo:
            raise SchemaError(
                code="invalid_apply_payload",
                message=f"Receipt {action_id} has no valid 'repo' (owner/name)",
                recovery_hint=(
                    "Set 'repo' in the payload or configure OSTAG_GITHUB_DEFAULT_REPO."
                ),
            )
        return repo

    async def apply(self, receipt: ActionReceipt) -> str:
        payload: dict[str, Any] = receipt.apply_payload
        operation = payload.get("operation")
        if operation == "create_issue":
            title = payload.get("title", "")
            if not title:
                raise SchemaError(
                    code="invalid_apply_payload",
                    message=f"Receipt {receipt.action_id} has no issue title",
                    recovery_hint="Re-propose the action with a 'title'.",
                )
            url = await self.connector.create_issue(
                self._repo(payload, receipt.action_id),
                title=title,
                body=payload.get("body", ""),
                assignee=payload.get("assignee", ""),
                labels=tuple(payload.get("labels", ())),
            )
            return f"created issue {url}"
        if operation == "comment_issue":
            number = payload.get("number")
            body = payload.get("body", "")
            if not isinstance(number, int) or number <= 0 or not body:
                raise SchemaError(
                    code="invalid_apply_payload",
                    message=f"Receipt {receipt.action_id} needs a positive issue "
                    "'number' and a non-empty 'body'",
                    recovery_hint="Re-propose the action with 'number' and 'body'.",
                )
            url = await self.connector.comment_issue(
                self._repo(payload, receipt.action_id), number, body=body
            )
            return f"commented on issue #{number}: {url}"
        raise SchemaError(
            code="unsupported_operation",
            message=f"Receipt {receipt.action_id} has unsupported GitHub operation "
            f"{operation!r}",
            recovery_hint="Supported operations: create_issue, comment_issue.",
        )
