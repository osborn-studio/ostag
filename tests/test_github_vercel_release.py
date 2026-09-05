from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from ostag.config import load_settings
from ostag.connectors.apply import build_apply_executors
from ostag.connectors.github import (
    GitHubPullRequestConnector,
    PullRequestReviewEvidence,
    PullRequestSource,
)
from ostag.connectors.vercel import (
    VercelAPIError,
    VercelClient,
    VercelDeployment,
    VercelDeploymentError,
)
from ostag.core.event import NormalizedEvent
from ostag.core.approvals import ApprovalService
from ostag.core.provider_evidence import ProviderChangeEvidence, ProviderCheck
from ostag.core.run_ledger import RunLedger
from ostag.core.runner import OSTagRunner
from ostag.core.schemas import ActionReceipt, SchemaError
from ostag.workflows.github_vercel_release import (
    GitHubVercelPartialFailure,
    GitHubVercelReleaseExecutor,
    GitHubVercelReleaseWorkflow,
    PullRequestReference,
    parse_pull_request_reference,
)


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
        "github_vercel_required_checks": ("release-check",),
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
    assert config.github_vercel_release_base_branch == "main"
    assert config.github_vercel_required_checks == ()
    assert config.vercel_access_token == ""
    assert config.vercel_deploy_timeout_seconds == 600
    assert config.vercel_poll_interval_seconds == 2


@pytest.mark.parametrize(
    ("field", "message"),
    [
        ("github_vercel_release_repository", "github_vercel_release_repository"),
        ("github_vercel_release_channels", "github_vercel_release_channels"),
        ("github_vercel_required_checks", "github_vercel_required_checks"),
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
        ({"github_vercel_release_base_branch": "release..main"}, "base_branch"),
        ({"github_vercel_required_checks": "release-check"}, "tuple"),
        ({"github_vercel_required_checks": ("release-check", "release-check")}, "unique"),
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


def test_release_settings_load_from_documented_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    values = {
        "OSTAG_GITHUB_VERCEL_RELEASE_ENABLED": "true",
        "OSTAG_GITHUB_VERCEL_RELEASE_REPOSITORY": "owner/repo",
        "OSTAG_GITHUB_VERCEL_RELEASE_CHANNELS": "feishu:oc_one,slack:C123",
        "OSTAG_GITHUB_VERCEL_RELEASE_BASE_BRANCH": "production",
        "OSTAG_GITHUB_VERCEL_REQUIRED_CHECKS": "release-check,security-scan",
        "OSTAG_VERCEL_ACCESS_TOKEN": "test-token",
        "OSTAG_VERCEL_TEAM_ID": "team_test",
        "OSTAG_VERCEL_PROJECT_ID": "prj_test",
        "OSTAG_VERCEL_PROJECT_NAME": "release-app",
        "OSTAG_VERCEL_DEPLOY_TIMEOUT_SECONDS": "900",
        "OSTAG_VERCEL_POLL_INTERVAL_SECONDS": "3",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)

    config = load_settings(
        str(tmp_path / "missing.env"),
        audit_log=tmp_path / "audit.log",
        run_ledger_path=tmp_path / "ledger.jsonl",
    )

    assert config.github_vercel_release_enabled is True
    assert config.github_vercel_release_channels == ("feishu:oc_one", "slack:C123")
    assert config.github_vercel_release_base_branch == "production"
    assert config.github_vercel_required_checks == ("release-check", "security-scan")
    assert config.vercel_deploy_timeout_seconds == 900
    assert config.vercel_poll_interval_seconds == 3


class FakeGh:
    def __init__(self, *outputs: dict[str, object]) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.outputs = [json.dumps(output) for output in outputs]

    async def run(self, *args: str) -> str:
        self.calls.append(args)
        return self.outputs.pop(0)


def _github_pr_payload(
    *,
    sha: str = "a" * 40,
    head_repo: str = "owner/repo",
    base_ref: str = "main",
):
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
        "base": {"ref": base_ref, "repo": {"full_name": "owner/repo"}},
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
        base_ref="main",
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


@pytest.mark.parametrize(("field", "value"), [("state", "closed"), ("draft", True)])
async def test_github_source_rejects_closed_or_draft_pr(field: str, value: object) -> None:
    payload = _github_pr_payload()
    payload[field] = value
    connector = GitHubPullRequestConnector(
        FakeGh(payload), object()  # type: ignore[arg-type]
    )

    with pytest.raises(SchemaError) as excinfo:
        await connector.resolve_source("owner/repo", 17)

    assert excinfo.value.code == "invalid_provider_evidence"


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


async def test_vercel_total_deadline_includes_slow_http_requests(tmp_path: Path) -> None:
    class Clock:
        now = 0.0

        def __call__(self) -> float:
            return self.now

        async def sleep(self, seconds: float) -> None:
            self.now += seconds

    clock = Clock()
    requests: list[float] = []

    def slow_open(request, timeout: float):
        del request
        requests.append(timeout)
        clock.now += timeout
        return _deployment("BUILDING")

    config = _enabled_config(
        tmp_path, vercel_deploy_timeout_seconds=30, vercel_poll_interval_seconds=1
    )
    client = VercelClient(
        config,
        opener=slow_open,
        sleeper=clock.sleep,
        monotonic=clock,
    )

    with pytest.raises(VercelDeploymentError, match="timed out"):
        await client.deploy_production(_source(), action_id="action_release")

    assert requests == [30]


async def test_vercel_ready_deployment_must_confirm_frozen_sha(tmp_path: Path) -> None:
    payload = _deployment("READY", alias_assigned=True)
    payload["gitSource"] = {"sha": "b" * 40}
    opener = FakeVercelOpen(payload)
    client = VercelClient(_enabled_config(tmp_path), opener=opener, sleeper=_no_sleep)
    source = PullRequestSource(
        "owner/repo", 17, "https://github.com/owner/repo/pull/17", "a" * 40, "release"
    )

    with pytest.raises(VercelDeploymentError, match="frozen GitHub SHA"):
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


def _event(
    text: str = "PR 已提交，请处理 https://github.com/owner/repo/pull/17",
    *,
    chat_id: str = "oc_release",
    chat_type: str = "group",
) -> NormalizedEvent:
    return NormalizedEvent(
        platform="feishu",
        event_id="evt_release",
        chat_id=chat_id,
        message_id="msg_release",
        sender_id="ou_author",
        text=text,
        mention_bot=True,
        chat_type=chat_type,  # type: ignore[arg-type]
        destination_id=chat_id,
    )


def _source(
    *,
    sha: str = "a" * 40,
    ref: str = "feature/release",
    base_ref: str = "main",
) -> PullRequestSource:
    return PullRequestSource(
        "owner/repo",
        17,
        "https://github.com/owner/repo/pull/17",
        sha,
        ref,
        base_ref,
    )


def _evidence(*states: str) -> ProviderChangeEvidence:
    checks = tuple(
        ProviderCheck(
            "release-check" if index == 1 else f"check-{index}",
            state,
            "a" * 40,
            f"https://github.com/owner/repo/actions/runs/{index}",
        )
        for index, state in enumerate(states, start=1)
    )
    return ProviderChangeEvidence(
        provider="github",
        repository="owner/repo",
        number=17,
        url="https://github.com/owner/repo/pull/17",
        state="open",
        draft=False,
        head_sha="a" * 40,
        merge_sha="",
        observed_at="2026-08-17T08:00:00+00:00",
        checks=checks,
        checks_complete=True,
    )


def _incomplete_passing_evidence() -> ProviderChangeEvidence:
    payload = _evidence("passed").to_dict()
    payload["schemaVersion"] = 2
    payload["checksComplete"] = False
    payload.pop("checksState")
    payload.pop("completionState")
    return ProviderChangeEvidence.from_dict(payload)


class FakeReleaseGitHub:
    def __init__(
        self,
        *,
        source: PullRequestSource | None = None,
        evidence: ProviderChangeEvidence | None = None,
        order: list[str] | None = None,
    ) -> None:
        self.source = source or _source()
        self.evidence = evidence or _evidence("passed")
        self.order = order if order is not None else []
        self.resolve_calls: list[tuple[str, int, str]] = []
        self.collect_calls: list[tuple[str, int, str]] = []
        self.approve_calls: list[tuple[str, int, str]] = []

    async def resolve_source(self, repo: str, number: int, *, expected_sha: str = ""):
        self.order.append("resolve")
        self.resolve_calls.append((repo, number, expected_sha))
        if expected_sha and expected_sha != self.source.head_sha:
            raise SchemaError(
                "provider_sha_mismatch",
                "GitHub PR head SHA changed after release approval",
                "Create a fresh receipt.",
            )
        return self.source

    async def collect(self, repo: str, number: int, *, expected_sha: str = ""):
        self.order.append("collect")
        self.collect_calls.append((repo, number, expected_sha))
        return self.evidence

    async def approve(self, repo: str, number: int, *, commit_id: str, body: str = ""):
        self.order.append("approve")
        self.approve_calls.append((repo, number, commit_id))
        return PullRequestReviewEvidence(
            91,
            "https://github.com/owner/repo/pull/17#pullrequestreview-91",
            "APPROVED",
            commit_id,
        )


class FakeReleaseVercel:
    def __init__(self, *, order: list[str] | None = None, error: Exception | None = None):
        self.order = order if order is not None else []
        self.error = error
        self.calls: list[tuple[PullRequestSource, str]] = []

    async def deploy_production(self, source: PullRequestSource, *, action_id: str):
        self.order.append("deploy")
        self.calls.append((source, action_id))
        if self.error is not None:
            raise self.error
        return VercelDeployment(
            "dpl_release",
            "release-app-a1b2c3.vercel.app",
            "READY",
            "production",
            True,
            ("release.example.com",),
            source.head_sha,
        )


def test_parse_pull_request_reference_requires_one_exact_github_url() -> None:
    assert parse_pull_request_reference(
        "请发布 https://github.com/Owner/Repo/pull/17。"
    ) == PullRequestReference("Owner/Repo", 17)
    assert parse_pull_request_reference("https://github.example.com/owner/repo/pull/17") is None

    with pytest.raises(SchemaError, match="one GitHub pull request"):
        parse_pull_request_reference(
            "https://github.com/owner/repo/pull/17 "
            "https://github.com/owner/repo/pull/18"
        )


async def test_release_workflow_prepares_frozen_high_risk_receipt(tmp_path: Path) -> None:
    github = FakeReleaseGitHub()
    workflow = GitHubVercelReleaseWorkflow(_enabled_config(tmp_path), github=github)

    receipt = await workflow.prepare(_event(), "run_release")

    assert isinstance(receipt, ActionReceipt)
    assert receipt.target_system == "github_vercel_release"
    assert receipt.target_object == "owner/repo#17 -> Vercel release-app (Production)"
    assert receipt.risk == "high"
    assert receipt.state == "needs_approval"
    assert receipt.required_permissions == ("write",)
    assert receipt.principal == _event().principal
    assert receipt.apply_payload == {
        "operation": "approve_and_deploy",
        "repository": "owner/repo",
        "number": 17,
        "pull_url": "https://github.com/owner/repo/pull/17",
        "head_sha": "a" * 40,
        "head_ref": "feature/release",
        "base_ref": "main",
        "required_checks": ["release-check"],
        "vercel_team_id": "team_test",
        "vercel_project_id": "prj_test",
        "vercel_project_name": "release-app",
    }
    assert {check.status for check in receipt.verification} == {"pass"}
    assert github.approve_calls == []


@pytest.mark.parametrize(
    "event",
    [
        _event(chat_id="oc_other"),
        _event(chat_type="private"),
        _event("请发布 https://github.com/other/repo/pull/17"),
    ],
)
async def test_release_workflow_refuses_out_of_scope_notifications(
    tmp_path: Path, event: NormalizedEvent
) -> None:
    github = FakeReleaseGitHub()
    workflow = GitHubVercelReleaseWorkflow(_enabled_config(tmp_path), github=github)

    with pytest.raises(SchemaError):
        await workflow.prepare(event, "run_release")

    assert github.resolve_calls == []


async def test_release_workflow_rejects_pr_targeting_another_base_branch(
    tmp_path: Path,
) -> None:
    github = FakeReleaseGitHub(source=_source(base_ref="develop"))
    workflow = GitHubVercelReleaseWorkflow(_enabled_config(tmp_path), github=github)

    with pytest.raises(SchemaError) as excinfo:
        await workflow.prepare(_event(), "run_release")

    assert excinfo.value.code == "release_base_branch_denied"
    assert github.approve_calls == []


@pytest.mark.parametrize("states", [(), ("pending",), ("failed",), ("passed", "pending")])
async def test_release_workflow_requires_at_least_one_all_passing_check(
    tmp_path: Path, states: tuple[str, ...]
) -> None:
    github = FakeReleaseGitHub(evidence=_evidence(*states))
    workflow = GitHubVercelReleaseWorkflow(_enabled_config(tmp_path), github=github)

    with pytest.raises(SchemaError) as excinfo:
        await workflow.prepare(_event(), "run_release")

    assert excinfo.value.code in {"provider_checks_missing", "provider_checks_incomplete"}
    assert github.approve_calls == []


async def test_release_workflow_rejects_incomplete_all_passing_checks(tmp_path: Path) -> None:
    github = FakeReleaseGitHub(evidence=_incomplete_passing_evidence())
    workflow = GitHubVercelReleaseWorkflow(_enabled_config(tmp_path), github=github)

    with pytest.raises(SchemaError) as excinfo:
        await workflow.prepare(_event(), "run_release")

    assert excinfo.value.code == "provider_checks_incomplete"
    assert github.approve_calls == []


async def test_release_workflow_rejects_untrusted_passing_check_when_required_is_missing(
    tmp_path: Path,
) -> None:
    evidence = _evidence("passed")
    untrusted = ProviderChangeEvidence.from_dict(
        evidence.to_dict()
        | {
            "checks": [
                evidence.checks[0].to_dict() | {"name": "unrelated-preview"}
            ]
        }
    )
    github = FakeReleaseGitHub(evidence=untrusted)
    workflow = GitHubVercelReleaseWorkflow(_enabled_config(tmp_path), github=github)

    with pytest.raises(SchemaError) as excinfo:
        await workflow.prepare(_event(), "run_release")

    assert excinfo.value.code == "provider_required_checks_missing"
    assert github.approve_calls == []


async def test_release_workflow_ignores_non_pr_text_and_disabled_feature(tmp_path: Path) -> None:
    github = FakeReleaseGitHub()
    disabled = GitHubVercelReleaseWorkflow(_config(tmp_path), github=github)
    enabled = GitHubVercelReleaseWorkflow(_enabled_config(tmp_path), github=github)

    assert await disabled.prepare(_event(), "run_release") is None
    assert await enabled.prepare(_event("请总结今天的进展"), "run_release") is None
    assert github.resolve_calls == []


async def test_release_workflow_rejects_empty_checks_even_when_legacy_flag_is_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OSTAG_GITHUB_VERCEL_REQUIRE_CHECKS", "false")
    github = FakeReleaseGitHub(evidence=_evidence())
    workflow = GitHubVercelReleaseWorkflow(_enabled_config(tmp_path), github=github)

    with pytest.raises(SchemaError) as excinfo:
        await workflow.prepare(_event(), "run_release")

    assert excinfo.value.code == "provider_checks_missing"
    assert github.approve_calls == []


async def test_composite_executor_revalidates_then_approves_then_deploys(
    tmp_path: Path,
) -> None:
    order: list[str] = []
    github = FakeReleaseGitHub(order=order)
    vercel = FakeReleaseVercel(order=order)
    config = _enabled_config(tmp_path)
    workflow = GitHubVercelReleaseWorkflow(config, github=github)
    receipt = await workflow.prepare(_event(), "run_release")
    assert isinstance(receipt, ActionReceipt)
    executor = GitHubVercelReleaseExecutor(config, github=github, vercel=vercel)

    evidence = await executor.apply(receipt.with_state("applying"))

    assert order == ["resolve", "collect", "resolve", "collect", "approve", "deploy"]
    assert github.resolve_calls[-1] == ("owner/repo", 17, "a" * 40)
    assert github.approve_calls == [("owner/repo", 17, "a" * 40)]
    assert vercel.calls[0][0] == _source()
    assert "pullrequestreview-91" in evidence
    assert "https://release.example.com" in evidence


async def test_composite_executor_stops_before_writes_when_sha_drifted(tmp_path: Path) -> None:
    config = _enabled_config(tmp_path)
    initial_github = FakeReleaseGitHub()
    receipt = await GitHubVercelReleaseWorkflow(config, github=initial_github).prepare(
        _event(), "run_release"
    )
    assert isinstance(receipt, ActionReceipt)
    drifted = FakeReleaseGitHub(source=_source(sha="b" * 40))
    vercel = FakeReleaseVercel()
    executor = GitHubVercelReleaseExecutor(config, github=drifted, vercel=vercel)

    with pytest.raises(SchemaError) as excinfo:
        await executor.apply(receipt.with_state("applying"))

    assert excinfo.value.code == "provider_sha_mismatch"
    assert drifted.approve_calls == []
    assert vercel.calls == []


async def test_composite_executor_stops_before_writes_when_pr_is_retargeted(
    tmp_path: Path,
) -> None:
    config = _enabled_config(tmp_path)
    receipt = await GitHubVercelReleaseWorkflow(
        config, github=FakeReleaseGitHub()
    ).prepare(_event(), "run_release")
    assert isinstance(receipt, ActionReceipt)
    retargeted = FakeReleaseGitHub(source=_source(base_ref="develop"))
    vercel = FakeReleaseVercel()
    executor = GitHubVercelReleaseExecutor(config, github=retargeted, vercel=vercel)

    with pytest.raises(SchemaError) as excinfo:
        await executor.apply(receipt.with_state("applying"))

    assert excinfo.value.code == "provider_ref_mismatch"
    assert retargeted.approve_calls == []
    assert vercel.calls == []


async def test_composite_executor_rechecks_trusted_checks_before_writes(
    tmp_path: Path,
) -> None:
    config = _enabled_config(tmp_path)
    receipt = await GitHubVercelReleaseWorkflow(
        config, github=FakeReleaseGitHub()
    ).prepare(_event(), "run_release")
    assert isinstance(receipt, ActionReceipt)
    untrusted = _evidence("passed")
    untrusted = ProviderChangeEvidence.from_dict(
        untrusted.to_dict()
        | {
            "checks": [
                untrusted.checks[0].to_dict() | {"name": "unrelated-preview"}
            ]
        }
    )
    github = FakeReleaseGitHub(evidence=untrusted)
    vercel = FakeReleaseVercel()
    executor = GitHubVercelReleaseExecutor(config, github=github, vercel=vercel)

    with pytest.raises(SchemaError) as excinfo:
        await executor.apply(receipt.with_state("applying"))

    assert excinfo.value.code == "provider_required_checks_missing"
    assert github.approve_calls == []
    assert vercel.calls == []


async def test_composite_executor_rejects_server_target_drift_before_provider_writes(
    tmp_path: Path,
) -> None:
    config = _enabled_config(tmp_path)
    github = FakeReleaseGitHub()
    receipt = await GitHubVercelReleaseWorkflow(config, github=github).prepare(
        _event(), "run_release"
    )
    assert isinstance(receipt, ActionReceipt)
    payload = dict(receipt.apply_payload)
    payload["vercel_project_id"] = "prj_attacker"
    changed = ActionReceipt.from_dict(receipt.to_dict() | {"apply_payload": payload})
    github.resolve_calls.clear()
    vercel = FakeReleaseVercel()
    executor = GitHubVercelReleaseExecutor(config, github=github, vercel=vercel)

    with pytest.raises(SchemaError) as excinfo:
        await executor.apply(changed.with_state("applying"))

    assert excinfo.value.code == "release_target_mismatch"
    assert github.resolve_calls == []
    assert github.approve_calls == []
    assert vercel.calls == []


async def test_composite_executor_rejects_unbounded_action_id_before_writes(
    tmp_path: Path,
) -> None:
    config = _enabled_config(tmp_path)
    github = FakeReleaseGitHub()
    receipt = await GitHubVercelReleaseWorkflow(config, github=github).prepare(
        _event(), "run_release"
    )
    assert isinstance(receipt, ActionReceipt)
    changed = ActionReceipt.from_dict(receipt.to_dict() | {"action_id": "invalid action id"})
    github.resolve_calls.clear()
    vercel = FakeReleaseVercel()
    executor = GitHubVercelReleaseExecutor(config, github=github, vercel=vercel)

    with pytest.raises(SchemaError) as excinfo:
        await executor.apply(changed.with_state("applying"))

    assert excinfo.value.code == "invalid_apply_payload"
    assert github.resolve_calls == []
    assert github.approve_calls == []
    assert vercel.calls == []


async def test_composite_executor_reports_partial_failure_without_token(tmp_path: Path) -> None:
    config = _enabled_config(tmp_path)
    github = FakeReleaseGitHub()
    receipt = await GitHubVercelReleaseWorkflow(config, github=github).prepare(
        _event(), "run_release"
    )
    assert isinstance(receipt, ActionReceipt)
    vercel = FakeReleaseVercel(
        error=VercelAPIError(f"failed with {config.vercel_access_token}")
    )
    executor = GitHubVercelReleaseExecutor(config, github=github, vercel=vercel)

    with pytest.raises(GitHubVercelPartialFailure) as excinfo:
        await executor.apply(receipt.with_state("applying"))

    assert "pullrequestreview-91" in str(excinfo.value)
    assert config.vercel_access_token not in str(excinfo.value)


def test_default_apply_registry_includes_github_vercel_composite_executor(
    tmp_path: Path,
) -> None:
    class WorkflowShell:
        def __init__(self) -> None:
            self.config = _enabled_config(tmp_path)
            self.requirements = object()

    registry = build_apply_executors(WorkflowShell())

    assert isinstance(registry["github_vercel_release"], GitHubVercelReleaseExecutor)


async def test_runner_full_chat_loop_proposes_approves_and_applies_release(
    tmp_path: Path,
) -> None:
    order: list[str] = []
    github = FakeReleaseGitHub(order=order)
    vercel = FakeReleaseVercel(order=order)
    config = _enabled_config(
        tmp_path,
        apply_enabled=True,
        actor_permissions=("feishu:ou_author:write",),
        auto_base_enabled=False,
        backend="mock",
    )
    ledger = RunLedger(config.run_ledger_path)
    workflow = GitHubVercelReleaseWorkflow(config, github=github)
    executor = GitHubVercelReleaseExecutor(config, github=github, vercel=vercel)
    approvals = ApprovalService(
        config,
        ledger,
        {"github_vercel_release": executor},
    )
    runner = OSTagRunner(
        config,
        run_ledger=ledger,
        approvals=approvals,
        github_vercel_release_workflow=workflow,
    )

    proposal = await runner.handle_event(_event())

    assert "GitHub/Vercel Production 待审批" in proposal
    assert "approve 1" in proposal
    assert order == ["resolve", "collect"]
    receipt = ledger.list_receipts(chat_id="oc_release")[0]
    assert receipt.state == "needs_approval"

    approved = await runner.handle_event(_event("approve 1"))
    assert "已批准" in approved
    assert order == ["resolve", "collect"]

    applied = await runner.handle_event(_event("apply 1"))
    assert "已执行" in applied
    assert order == ["resolve", "collect", "resolve", "collect", "approve", "deploy"]
    assert ledger.get_receipt(receipt.action_id).state == "applied"
    details = [
        record.get("payload", {}).get("detail", "")
        for record in ledger.export_run(receipt.run_id)
        if record.get("event_type") == "action_state_changed"
    ]
    assert any("https://release.example.com" in detail for detail in details)


async def test_runner_returns_safe_release_refusal_without_falling_through(
    tmp_path: Path,
) -> None:
    token = "vercel-secret-never-render"
    github = FakeReleaseGitHub()
    config = _enabled_config(tmp_path, vercel_access_token=token, auto_base_enabled=False)
    workflow = GitHubVercelReleaseWorkflow(config, github=github)
    runner = OSTagRunner(config, github_vercel_release_workflow=workflow)

    reply = await runner.handle_event(_event(chat_id="oc_denied"))

    assert "PR 发布未进入审批" in reply
    assert "not authorized" in reply
    assert token not in reply
    assert github.resolve_calls == []
    assert runner.run_ledger.list_receipts(chat_id="oc_denied") == []
