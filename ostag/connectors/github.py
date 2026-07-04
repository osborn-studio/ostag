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
from typing import Any

from ostag.config import Settings
from ostag.core.safety import scrub_environment
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
