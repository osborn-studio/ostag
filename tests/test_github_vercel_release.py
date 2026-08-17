from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from ostag.config import load_settings
from ostag.connectors.github import (
    GitHubPullRequestConnector,
    PullRequestSource,
)
from ostag.connectors.vercel import VercelAPIError, VercelClient, VercelDeploymentError
from ostag.core.schemas import SchemaError


def _config(tmp_path: Path, **overrides: object):
    return load_settings(
        None,
        audit_log=tmp_path / "audit.log",
        run_ledger_path=tmp_path / "ledger.jsonl",
        **overrides,
    )


def _enabled_config(tmp_path: Path, **overrides: object):
    values: dict[str, object] = {
        "github_vercel_release_enabled": True,
        "github_vercel_release_repository": "owner/repo",
        "github_vercel_release_channels": ("feishu:oc_release",),
        "vercel_access_token": "vercel-test-token",
        "vercel_team_id": "team_test",
        "vercel_project_id": "prj_test",
        "vercel_project_name": "release-app",
    }
    values.update(overrides)
    return _config(tmp_path, **values)


def test_github_vercel_release_defaults_disabled(tmp_path: Path) -> None:
    config = _config(tmp_path)

    assert config.github_vercel_release_enabled is False
    assert config.github_vercel_release_repository == ""
    assert config.github_vercel_release_channels == ()
    assert config.github_vercel_require_checks is True
    assert config.vercel_access_token == ""
    assert config.vercel_deploy_timeout_seconds == 600
    assert config.vercel_poll_interval_seconds == 2


@pytest.mark.parametrize(
    ("field", "message"),
    [
        ("github_vercel_release_repository", "github_vercel_release_repository"),
        ("github_vercel_release_channels", "github_vercel_release_channels"),
        ("vercel_access_token", "vercel_access_token"),
        ("vercel_team_id", "vercel_team_id"),
        ("vercel_project_id", "vercel_project_id"),
        ("vercel_project_name", "vercel_project_name"),
    ],
)
def test_enabled_release_requires_complete_server_configuration(
    tmp_path: Path, field: str, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        _enabled_config(tmp_path, **{field: () if field.endswith("channels") else ""})


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"github_vercel_release_repository": "owner"}, "owner/name"),
        ({"github_vercel_release_repository": "https://github.com/owner/repo"}, "owner/name"),
        ({"github_vercel_release_channels": ("oc_release",)}, "platform:chat_id"),
        ({"github_vercel_release_channels": ("unknown:chat",)}, "platform"),
        ({"vercel_deploy_timeout_seconds": 29}, "30..3600"),
        ({"vercel_deploy_timeout_seconds": 3601}, "30..3600"),
        ({"vercel_poll_interval_seconds": 0}, "1..60"),
        ({"vercel_poll_interval_seconds": 61}, "1..60"),
    ],
)
def test_enabled_release_rejects_unsafe_configuration(
    tmp_path: Path, overrides: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        _enabled_config(tmp_path, **overrides)


def test_enabled_release_accepts_explicit_single_project_mapping(tmp_path: Path) -> None:
    config = _enabled_config(tmp_path)

    assert config.github_vercel_release_repository == "owner/repo"
    assert config.github_vercel_release_channels == ("feishu:oc_release",)
    assert config.vercel_team_id == "team_test"
    assert config.vercel_project_id == "prj_test"
    assert config.vercel_project_name == "release-app"


class FakeGh:
    def __init__(self, *outputs: dict[str, object]) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.outputs = [json.dumps(output) for output in outputs]

    async def run(self, *args: str) -> str:
        self.calls.append(args)
        return self.outputs.pop(0)


def _github_pr_payload(*, sha: str = "a" * 40, head_repo: str = "owner/repo"):
    return {
        "number": 17,
        "html_url": "https://github.com/owner/repo/pull/17",
        "state": "open",
        "draft": False,
        "merged_at": None,
        "head": {
            "sha": sha,
            "ref": "feature/release",
            "repo": {"full_name": head_repo},
        },
        "base": {"repo": {"full_name": "owner/repo"}},
    }


async def test_github_resolves_strict_same_repository_pr_source() -> None:
    gh = FakeGh(_github_pr_payload())
    connector = GitHubPullRequestConnector(gh, object())  # type: ignore[arg-type]

    source = await connector.resolve_source("owner/repo", 17)

    assert source == PullRequestSource(
        repository="owner/repo",
        number=17,
        url="https://github.com/owner/repo/pull/17",
        head_sha="a" * 40,
        head_ref="feature/release",
    )
    assert gh.calls == [("api", "repos/owner/repo/pulls/17")]


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        (_github_pr_payload(head_repo="fork/repo"), "provider_fork_unsupported"),
        (_github_pr_payload(sha="not-a-sha"), "invalid_provider_evidence"),
        ({"number": 17}, "invalid_provider_evidence"),
    ],
)
async def test_github_rejects_untrusted_pr_source_payloads(
    payload: dict[str, object], code: str
) -> None:
    connector = GitHubPullRequestConnector(FakeGh(payload), object())  # type: ignore[arg-type]

    with pytest.raises(SchemaError) as excinfo:
        await connector.resolve_source("owner/repo", 17)

    assert excinfo.value.code == code


async def test_github_source_rejects_sha_drift() -> None:
    connector = GitHubPullRequestConnector(
        FakeGh(_github_pr_payload()), object()  # type: ignore[arg-type]
    )

    with pytest.raises(SchemaError) as excinfo:
        await connector.resolve_source("owner/repo", 17, expected_sha="b" * 40)

    assert excinfo.value.code == "provider_sha_mismatch"


async def test_github_submits_sha_bound_approval_review() -> None:
    gh = FakeGh(
        {
            "id": 91,
            "state": "APPROVED",
            "commit_id": "a" * 40,
            "html_url": "https://github.com/owner/repo/pull/17#pullrequestreview-91",
        }
    )
    connector = GitHubPullRequestConnector(gh, object())  # type: ignore[arg-type]

    review = await connector.approve(
        "owner/repo", 17, commit_id="a" * 40, body="Approved by OStag release receipt."
    )

    assert review.review_id == 91
    assert review.state == "APPROVED"
    assert review.commit_id == "a" * 40
    assert gh.calls == [
        (
            "api",
            "repos/owner/repo/pulls/17/reviews",
            "--method",
            "POST",
            "--raw-field",
            f"commit_id={'a' * 40}",
            "--raw-field",
            "event=APPROVE",
            "--raw-field",
            "body=Approved by OStag release receipt.",
        )
    ]


async def test_github_rejects_mismatched_review_response() -> None:
    gh = FakeGh(
        {
            "id": 91,
            "state": "APPROVED",
            "commit_id": "b" * 40,
            "html_url": "https://github.com/owner/repo/pull/17#pullrequestreview-91",
        }
    )
    connector = GitHubPullRequestConnector(gh, object())  # type: ignore[arg-type]

    with pytest.raises(SchemaError) as excinfo:
        await connector.approve("owner/repo", 17, commit_id="a" * 40)

    assert excinfo.value.code == "invalid_provider_evidence"


class FakeVercelOpen:
    def __init__(self, *responses: dict[str, object]) -> None:
        self.responses = list(responses)
        self.requests = []

    def __call__(self, request, timeout: int):
        self.requests.append((request, timeout))
        return self.responses.pop(0)


async def _no_sleep(_: float) -> None:
    return None


def _deployment(state: str, *, alias_assigned: bool = False) -> dict[str, object]:
    return {
        "id": "dpl_release",
        "url": "release-app-a1b2c3.vercel.app",
        "readyState": state,
        "target": "production",
        "aliasAssigned": alias_assigned,
        "alias": ["release.example.com"] if alias_assigned else [],
        "gitSource": {"sha": "a" * 40},
    }


async def test_vercel_creates_and_waits_for_exact_sha_production_deployment(
    tmp_path: Path,
) -> None:
    opener = FakeVercelOpen(
        _deployment("BUILDING"),
        _deployment("BUILDING"),
        _deployment("READY", alias_assigned=True),
    )
    config = _enabled_config(tmp_path)
    client = VercelClient(config, opener=opener, sleeper=_no_sleep)
    source = PullRequestSource(
        "owner/repo",
        17,
        "https://github.com/owner/repo/pull/17",
        "a" * 40,
        "feature/release",
    )

    deployment = await client.deploy_production(source, action_id="action_release")

    assert deployment.state == "READY"
    assert deployment.alias_assigned is True
    assert deployment.production_url == "https://release.example.com"
    create_request, create_timeout = opener.requests[0]
    assert create_request.get_method() == "POST"
    assert urlparse(create_request.full_url).path == "/v13/deployments"
    assert parse_qs(urlparse(create_request.full_url).query) == {"teamId": ["team_test"]}
    assert create_request.headers["Authorization"] == "Bearer vercel-test-token"
    assert create_timeout == 30
    body = json.loads(create_request.data)
    assert body == {
        "name": "release-app",
        "project": "prj_test",
        "target": "production",
        "gitSource": {
            "type": "github",
            "org": "owner",
            "repo": "repo",
            "ref": "feature/release",
            "sha": "a" * 40,
        },
        "meta": {
            "ostagActionId": "action_release",
            "githubPullRequest": "17",
            "githubCommitSha": "a" * 40,
        },
    }
    get_request, _ = opener.requests[-1]
    assert get_request.get_method() == "GET"
    assert urlparse(get_request.full_url).path == "/v13/deployments/dpl_release"
    assert parse_qs(urlparse(get_request.full_url).query) == {
        "teamId": ["team_test"],
        "withGitRepoInfo": ["true"],
    }


@pytest.mark.parametrize("state", ["ERROR", "CANCELED", "BLOCKED"])
async def test_vercel_fails_closed_on_terminal_deployment_error(
    tmp_path: Path, state: str
) -> None:
    opener = FakeVercelOpen(_deployment(state))
    client = VercelClient(_enabled_config(tmp_path), opener=opener, sleeper=_no_sleep)
    source = PullRequestSource(
        "owner/repo", 17, "https://github.com/owner/repo/pull/17", "a" * 40, "release"
    )

    with pytest.raises(VercelDeploymentError, match=state):
        await client.deploy_production(source, action_id="action_release")


async def test_vercel_does_not_treat_ready_without_production_alias_as_live(
    tmp_path: Path,
) -> None:
    responses = [_deployment("READY") for _ in range(31)]
    opener = FakeVercelOpen(*responses)
    config = _enabled_config(
        tmp_path, vercel_deploy_timeout_seconds=30, vercel_poll_interval_seconds=1
    )
    client = VercelClient(config, opener=opener, sleeper=_no_sleep)
    source = PullRequestSource(
        "owner/repo", 17, "https://github.com/owner/repo/pull/17", "a" * 40, "release"
    )

    with pytest.raises(VercelDeploymentError, match="alias"):
        await client.deploy_production(source, action_id="action_release")


async def test_vercel_error_redacts_access_token(tmp_path: Path) -> None:
    token = "vercel-secret-never-log"

    def failing_open(request, timeout: int):
        raise RuntimeError(f"provider echoed {token}")

    config = _enabled_config(tmp_path, vercel_access_token=token)
    client = VercelClient(config, opener=failing_open, sleeper=_no_sleep)
    source = PullRequestSource(
        "owner/repo", 17, "https://github.com/owner/repo/pull/17", "a" * 40, "release"
    )

    with pytest.raises(VercelAPIError) as excinfo:
        await client.deploy_production(source, action_id="action_release")

    assert token not in str(excinfo.value)
