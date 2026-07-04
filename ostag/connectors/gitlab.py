"""GitLab system-of-record connector, transported over the `glab` CLI.

Mirrors the GitHub connector: all operations shell out to an
already-authenticated `glab` CLI, so OS Tag never handles GitLab tokens
itself. Only non-destructive operations are implemented — issue creation and
issue comments. There is no close/delete path, and every write reaches GitLab
exclusively through the action receipt protocol.
"""

from __future__ import annotations

import asyncio
from typing import Any

from ostag.config import Settings
from ostag.core.safety import scrub_environment
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
