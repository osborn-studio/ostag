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
from typing import Any

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

    async def collect(
        self, repo: str, number: int, *, expected_sha: str = ""
    ) -> ProviderChangeEvidence:
        raw = await self.cli.run(
            "pr", "view", str(number), "--repo", repo, "--json", self._FIELDS
        )
        if len(raw.encode("utf-8")) > 1_048_576:
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
