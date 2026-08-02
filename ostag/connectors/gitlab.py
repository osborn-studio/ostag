"""GitLab system-of-record connector, transported over the `glab` CLI.

Mirrors the GitHub connector: all operations shell out to an
already-authenticated `glab` CLI, so OS Tag never handles GitLab tokens
itself. Only non-destructive operations are implemented — issue creation and
issue comments. There is no close/delete path, and every write reaches GitLab
exclusively through the action receipt protocol.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from urllib.parse import quote

from ostag.config import Settings
from ostag.core.safety import scrub_environment
from ostag.core.provider_evidence import (
    ProviderChangeEvidence,
    ProviderCheck,
    normalize_utc_timestamp,
)
from ostag.core.schemas import ActionReceipt, SchemaError


class GitLabCLIError(RuntimeError):
    pass


class GlabCLI:
    def __init__(self, config: Settings):
        self.config = config

    async def run(self, *args: str) -> str:
        proc = await asyncio.create_subprocess_exec(
            self.config.glab_bin,
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
            raise GitLabCLIError((err or out or f"glab exited {proc.returncode}")[-3000:])
        return out


class GitLabIssuesConnector:
    def __init__(self, cli: GlabCLI):
        self.cli = cli

    async def create_issue(
        self,
        repo: str,
        *,
        title: str,
        description: str = "",
        assignee: str = "",
        labels: tuple[str, ...] = (),
    ) -> str:
        """Create an issue; returns glab's stdout (includes the issue URL)."""
        args = [
            "issue",
            "create",
            "--repo",
            repo,
            "--title",
            title,
            "--description",
            description,
        ]
        if assignee:
            args.extend(["--assignee", assignee])
        if labels:
            args.extend(["--label", ",".join(labels)])
        return await self.cli.run(*args)

    async def comment_issue(self, repo: str, number: int, *, body: str) -> str:
        """Comment on an issue; returns glab's stdout."""
        return await self.cli.run(
            "issue", "note", str(number), "--repo", repo, "--message", body
        )


class GitLabMergeRequestConnector:
    """Collect read-only MR, pipeline, merge, and commit evidence."""

    def __init__(self, cli: GlabCLI, config: Settings):
        self.cli = cli
        self.config = config

    async def collect(
        self, repo: str, number: int, *, expected_sha: str = ""
    ) -> ProviderChangeEvidence:
        mr_raw = await self.cli.run(
            "mr", "view", str(number), "--repo", repo, "--output", "json"
        )
        encoded_repo = quote(repo, safe="")
        pipelines_raw = await self.cli.run(
            "api",
            f"projects/{encoded_repo}/merge_requests/{number}/pipelines?per_page=20&page=1",
        )
        if len(mr_raw.encode()) + len(pipelines_raw.encode()) > 1_048_576:
            raise SchemaError(
                "provider_evidence_limit", "GitLab evidence exceeded 1 MiB", "Reduce pipelines."
            )
        try:
            payload = json.loads(mr_raw)
            pipelines = json.loads(pipelines_raw)
        except json.JSONDecodeError as exc:
            raise SchemaError(
                "invalid_provider_evidence", "GitLab returned invalid JSON", "Retry the read."
            ) from exc
        if not isinstance(payload, dict) or payload.get("iid") != number:
            raise SchemaError(
                "invalid_provider_evidence", "GitLab MR response is malformed", "Retry the read."
            )
        if not isinstance(pipelines, list) or len(pipelines) > min(
            20, self.config.provider_evidence_max_checks
        ):
            raise SchemaError(
                "provider_evidence_limit", "GitLab pipeline page exceeded its bound", "Retry."
            )
        if any(not isinstance(item, dict) for item in pipelines):
            raise SchemaError(
                "invalid_provider_evidence", "GitLab pipeline response is malformed", "Retry."
            )
        head_sha = payload.get("sha", "")
        if expected_sha and head_sha != expected_sha:
            raise SchemaError(
                "provider_sha_mismatch", "GitLab MR head SHA changed", "Refresh expected SHA."
            )
        current = next(
            (item for item in pipelines if isinstance(item, dict) and item.get("sha") == head_sha),
            None,
        )
        checks = (self._pipeline(current, head_sha),) if current is not None else ()
        raw_state = str(payload.get("state", "")).casefold()
        if raw_state not in {"opened", "closed", "merged"}:
            raise SchemaError(
                "invalid_provider_evidence", "GitLab MR state is unknown", "Retry the read."
            )
        state = raw_state
        if state == "opened":
            state = "open"
        return ProviderChangeEvidence(
            provider="gitlab",
            repository=repo,
            number=number,
            url=payload.get("web_url", ""),
            state=state,
            draft=payload.get("draft", False),
            head_sha=head_sha,
            merge_sha=payload.get("merge_commit_sha") or "",
            observed_at=normalize_utc_timestamp(payload.get("updated_at", "")),
            checks=checks,
        )

    @staticmethod
    def _pipeline(payload: dict[str, Any], head_sha: str) -> ProviderCheck:
        status = str(payload.get("status", "")).casefold()
        if status in {"success", "skipped"}:
            state = "passed"
        elif status in {"failed", "canceled"}:
            state = "failed"
        else:
            state = "pending"
        return ProviderCheck(
            name=f"pipeline:{payload.get('id', 'unknown')}",
            state=state,
            sha=head_sha,
            url=str(payload.get("web_url") or ""),
        )


class GitLabApplyExecutor:
    """Applies approved GitLab receipts. Non-destructive operations only."""

    def __init__(self, connector: GitLabIssuesConnector, config: Settings):
        self.connector = connector
        self.config = config

    def _repo(self, payload: dict[str, Any], action_id: str) -> str:
        repo = payload.get("repo") or self.config.gitlab_default_repo
        if not repo or "/" not in repo:
            raise SchemaError(
                code="invalid_apply_payload",
                message=f"Receipt {action_id} has no valid 'repo' (group/project)",
                recovery_hint=(
                    "Set 'repo' in the payload or configure OSTAG_GITLAB_DEFAULT_REPO."
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
            out = await self.connector.create_issue(
                self._repo(payload, receipt.action_id),
                title=title,
                description=payload.get("body", ""),
                assignee=payload.get("assignee", ""),
                labels=tuple(payload.get("labels", ())),
            )
            return f"created issue: {out}"
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
            out = await self.connector.comment_issue(
                self._repo(payload, receipt.action_id), number, body=body
            )
            return f"commented on issue #{number}: {out}"
        raise SchemaError(
            code="unsupported_operation",
            message=f"Receipt {receipt.action_id} has unsupported GitLab operation "
            f"{operation!r}",
            recovery_hint="Supported operations: create_issue, comment_issue.",
        )
