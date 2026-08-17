"""Governed GitHub PR approval followed by a SHA-bound Vercel release."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from ostag.config import Settings
from ostag.connectors.github import (
    GhCLI,
    GitHubPullRequestConnector,
    PullRequestSource,
)
from ostag.connectors.vercel import VercelClient
from ostag.core.event import NormalizedEvent
from ostag.core.provider_evidence import ProviderChangeEvidence
from ostag.core.schemas import (
    ActionReceipt,
    SchemaError,
    VerificationCheck,
    new_id,
)

_URL = re.compile(r"https://[^\s<>()\[\]{}]+", re.IGNORECASE)
_REPOSITORY = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*$"
)
_TRAILING_PUNCTUATION = ".,;:!?，。；：！？'\""
_APPLY_KEYS = frozenset(
    {
        "operation",
        "repository",
        "number",
        "pull_url",
        "head_sha",
        "head_ref",
        "vercel_team_id",
        "vercel_project_id",
        "vercel_project_name",
    }
)


class GitHubVercelPartialFailure(RuntimeError):
    """GitHub approved the frozen SHA, but the Vercel release did not finish."""


@dataclass(frozen=True)
class PullRequestReference:
    repository: str
    number: int


def parse_pull_request_reference(text: str) -> PullRequestReference | None:
    """Extract exactly one canonical GitHub PR target from untrusted chat text."""
    references: set[PullRequestReference] = set()
    for match in _URL.finditer(text):
        raw = match.group(0).rstrip(_TRAILING_PUNCTUATION)
        parsed = urlparse(raw)
        if (
            parsed.scheme.casefold() != "https"
            or parsed.netloc.casefold() != "github.com"
            or parsed.username is not None
            or parsed.password is not None
        ):
            continue
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) != 4 or parts[2].casefold() != "pull" or not parts[3].isdigit():
            continue
        repository = f"{parts[0]}/{parts[1]}"
        number = int(parts[3])
        if number < 1 or not _REPOSITORY.fullmatch(repository):
            continue
        references.add(PullRequestReference(repository, number))
    if not references:
        return None
    if len(references) != 1:
        raise SchemaError(
            "ambiguous_pull_request",
            "Release messages must contain exactly one GitHub pull request",
            "Mention OStag again with one allowlisted PR URL.",
        )
    return next(iter(references))


def _ensure_release_evidence(
    evidence: ProviderChangeEvidence,
    source: PullRequestSource,
    *,
    require_checks: bool,
) -> None:
    consistent = (
        evidence.provider == "github"
        and evidence.repository.casefold() == source.repository.casefold()
        and evidence.number == source.number
        and evidence.url == source.url
        and evidence.state == "open"
        and evidence.draft is False
        and evidence.head_sha == source.head_sha
    )
    if not consistent:
        raise SchemaError(
            "invalid_provider_evidence",
            "GitHub PR evidence does not match the frozen release source",
            "Refresh the PR and create a new release receipt.",
        )
    if require_checks and not evidence.checks:
        raise SchemaError(
            "provider_checks_missing",
            "GitHub PR has no status checks to authorize a Production release",
            "Configure required checks or explicitly disable the check requirement.",
        )
    if any(check.state != "passed" for check in evidence.checks):
        raise SchemaError(
            "provider_checks_incomplete",
            "GitHub PR checks are pending or failed",
            "Wait for all checks to pass and create a fresh release receipt.",
        )


def _verification(evidence: ProviderChangeEvidence) -> tuple[VerificationCheck, ...]:
    checks = [
        VerificationCheck(
            "github_pr_open_non_draft",
            "pass",
            f"{evidence.url} @ {evidence.head_sha}",
        )
    ]
    checks.extend(
        VerificationCheck(
            f"github_check:{check.name}",
            "pass",
            check.url or check.sha,
        )
        for check in evidence.checks
    )
    return tuple(checks)


class GitHubVercelReleaseWorkflow:
    def __init__(
        self,
        config: Settings,
        *,
        github: GitHubPullRequestConnector | Any | None = None,
    ) -> None:
        self.config = config
        self.github = github or GitHubPullRequestConnector(GhCLI(config), config)

    async def prepare(
        self, event: NormalizedEvent, run_id: str
    ) -> ActionReceipt | None:
        """Read and freeze release evidence without performing provider writes."""
        if not self.config.github_vercel_release_enabled:
            return None
        reference = parse_pull_request_reference(event.text)
        if reference is None:
            return None
        if event.chat_type != "group":
            raise SchemaError(
                "release_group_required",
                "GitHub/Vercel releases are accepted only from an allowlisted group",
                "Submit the PR notification in the configured release group.",
            )
        channel = f"{event.platform}:{event.chat_id}"
        if channel not in self.config.github_vercel_release_channels:
            raise SchemaError(
                "release_channel_denied",
                "This group is not authorized for GitHub/Vercel releases",
                "Use an allowlisted release group.",
            )
        repository = self.config.github_vercel_release_repository
        if reference.repository.casefold() != repository.casefold():
            raise SchemaError(
                "release_repository_denied",
                "The GitHub PR repository is not allowlisted for this OStag instance",
                f"Use a PR from {repository}.",
            )
        source = await self.github.resolve_source(repository, reference.number)
        evidence = await self.github.collect(
            repository, reference.number, expected_sha=source.head_sha
        )
        _ensure_release_evidence(
            evidence,
            source,
            require_checks=self.config.github_vercel_require_checks,
        )
        return ActionReceipt(
            action_id=new_id("action"),
            run_id=run_id,
            title=f"批准 GitHub PR #{source.number} 并发布 Vercel Production",
            target_system="github_vercel_release",
            target_object=(
                f"{repository}#{source.number} -> "
                f"Vercel {self.config.vercel_project_name} (Production)"
            ),
            mutation_type="update",
            principal=event.principal,
            summary=(
                f"冻结 SHA {source.head_sha}；执行时重新校验 PR 与 checks，"
                "先提交 GitHub APPROVE，再等待 Vercel Production 上线。"
            ),
            diff=(
                f"GitHub review: APPROVE {repository}#{source.number}@{source.head_sha}\n"
                f"Vercel target: {self.config.vercel_project_name}/production"
            ),
            risk="high",
            required_permissions=("write",),
            state="needs_approval",
            verification=_verification(evidence),
            apply_payload={
                "operation": "approve_and_deploy",
                "repository": repository,
                "number": source.number,
                "pull_url": source.url,
                "head_sha": source.head_sha,
                "head_ref": source.head_ref,
                "vercel_team_id": self.config.vercel_team_id,
                "vercel_project_id": self.config.vercel_project_id,
                "vercel_project_name": self.config.vercel_project_name,
            },
        )


class GitHubVercelReleaseExecutor:
    def __init__(
        self,
        config: Settings,
        *,
        github: GitHubPullRequestConnector | Any | None = None,
        vercel: VercelClient | Any | None = None,
    ) -> None:
        self.config = config
        self.github = github or GitHubPullRequestConnector(GhCLI(config), config)
        self.vercel = vercel or VercelClient(config)

    def _payload(self, receipt: ActionReceipt) -> dict[str, Any]:
        payload = receipt.apply_payload
        if not self.config.github_vercel_release_enabled:
            raise SchemaError(
                "release_disabled",
                "GitHub/Vercel release execution is disabled",
                "Enable the release workflow and create a fresh receipt.",
            )
        valid_action_id = re.fullmatch(r"action_[A-Za-z0-9]{1,64}", receipt.action_id)
        if (
            receipt.target_system != "github_vercel_release"
            or set(payload) != _APPLY_KEYS
            or valid_action_id is None
        ):
            raise SchemaError(
                "invalid_apply_payload",
                "GitHub/Vercel release receipt has an invalid contract",
                "Create a fresh release receipt from an allowlisted PR notification.",
            )
        expected = {
            "operation": "approve_and_deploy",
            "repository": self.config.github_vercel_release_repository,
            "vercel_team_id": self.config.vercel_team_id,
            "vercel_project_id": self.config.vercel_project_id,
            "vercel_project_name": self.config.vercel_project_name,
        }
        if any(payload.get(key) != value for key, value in expected.items()):
            raise SchemaError(
                "release_target_mismatch",
                "GitHub/Vercel release target no longer matches server configuration",
                "Create a fresh release receipt for the configured target.",
            )
        number = payload.get("number")
        strings = ("pull_url", "head_sha", "head_ref")
        if (
            isinstance(number, bool)
            or not isinstance(number, int)
            or number < 1
            or any(not isinstance(payload.get(key), str) or not payload[key] for key in strings)
        ):
            raise SchemaError(
                "invalid_apply_payload",
                "GitHub/Vercel release receipt source is invalid",
                "Create a fresh SHA-bound release receipt.",
            )
        return payload

    async def apply(self, receipt: ActionReceipt) -> str:
        payload = self._payload(receipt)
        repository = self.config.github_vercel_release_repository
        number = payload["number"]
        source = await self.github.resolve_source(
            repository,
            number,
            expected_sha=payload["head_sha"],
        )
        if source.url != payload["pull_url"] or source.head_ref != payload["head_ref"]:
            raise SchemaError(
                "provider_ref_mismatch",
                "GitHub PR URL or head ref changed after release approval",
                "Create a fresh release receipt for the current PR source.",
            )
        evidence = await self.github.collect(
            repository,
            number,
            expected_sha=source.head_sha,
        )
        _ensure_release_evidence(
            evidence,
            source,
            require_checks=self.config.github_vercel_require_checks,
        )
        review = await self.github.approve(
            repository,
            number,
            commit_id=source.head_sha,
            body=f"Approved by OStag release receipt {receipt.action_id}.",
        )
        try:
            deployment = await self.vercel.deploy_production(
                source,
                action_id=receipt.action_id,
            )
        except Exception as exc:
            raise GitHubVercelPartialFailure(
                f"GitHub review succeeded at {review.url}; Vercel Production release failed "
                f"with {type(exc).__name__}. Inspect provider logs before retrying."
            ) from exc
        return (
            f"approved {review.url} at {source.head_sha}; "
            f"deployed {deployment.production_url} "
            f"({deployment.deployment_id}, READY, Production alias assigned)"
        )
