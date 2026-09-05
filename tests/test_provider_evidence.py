from __future__ import annotations

import json
from pathlib import Path

import pytest

from ostag.config import load_settings
from ostag.connectors.github import GhCLI, GitHubPullRequestConnector
from ostag.connectors.gitlab import GitLabMergeRequestConnector
from ostag.core.provider_evidence import ProviderChangeEvidence
from ostag.core.schemas import SchemaError
from ostag.main import main_async


HEAD = "a" * 40
MERGE = "b" * 40


class FakeCLI:
    def __init__(self, *outputs: object) -> None:
        self.outputs = [json.dumps(output) for output in outputs]
        self.calls: list[tuple[str, ...]] = []

    async def run(self, *args: str) -> str:
        self.calls.append(args)
        return self.outputs.pop(0)


def _config(tmp_path: Path, **overrides: object):
    return load_settings(None, audit_log=tmp_path / "audit.log", **overrides)


def _github_payload(*, state: str = "MERGED") -> dict:
    return {
        "number": 12,
        "url": "https://github.com/owner/repo/pull/12",
        "state": state,
        "isDraft": False,
        "mergedAt": "2026-08-02T01:02:03Z" if state == "MERGED" else None,
        "headRefOid": HEAD,
        "mergeCommit": {"oid": MERGE} if state == "MERGED" else None,
        "updatedAt": "2026-08-02T01:02:03Z",
    }


def _github_check_pages(
    *,
    check_total: int = 1,
    status_total: int = 1,
    check_sha: str = HEAD,
    status_sha: str = HEAD,
) -> tuple[dict, dict]:
    return (
        {
            "total_count": check_total,
            "check_runs": [
                {
                    "name": "tests",
                    "status": "completed",
                    "conclusion": "success",
                    "head_sha": check_sha,
                    "html_url": "https://github.com/owner/repo/actions/runs/1",
                }
            ],
        },
        {
            "sha": status_sha,
            "total_count": status_total,
            "statuses": [
                {
                    "context": "lint",
                    "state": "success",
                    "target_url": "https://github.com/owner/repo/actions/runs/2",
                }
            ],
        },
    )


async def test_github_requires_complete_current_head_check_pages(tmp_path: Path) -> None:
    check_runs, statuses = _github_check_pages()
    cli = FakeCLI(_github_payload(), check_runs, statuses)
    connector = GitHubPullRequestConnector(cli, _config(tmp_path))  # type: ignore[arg-type]

    evidence = await connector.collect("owner/repo", 12, expected_sha=HEAD)

    assert evidence.checks_complete is True
    assert evidence.checks_state == "passed"
    assert [check.name for check in evidence.checks] == ["tests", "lint"]
    assert cli.calls[1:] == [
        (
            "api",
            f"repos/owner/repo/commits/{HEAD}/check-runs?filter=latest&per_page=100",
        ),
        ("api", f"repos/owner/repo/commits/{HEAD}/status?per_page=100"),
    ]


async def test_github_truncated_all_passing_page_stays_pending(tmp_path: Path) -> None:
    check_runs, statuses = _github_check_pages(check_total=2)
    evidence = await GitHubPullRequestConnector(  # type: ignore[arg-type]
        FakeCLI(_github_payload(), check_runs, statuses), _config(tmp_path)
    ).collect("owner/repo", 12, expected_sha=HEAD)

    assert evidence.checks_complete is False
    assert evidence.checks_state == "pending"
    assert evidence.completion_state == "pending"


async def test_github_empty_complete_pages_stay_pending(tmp_path: Path) -> None:
    check_runs = {"total_count": 0, "check_runs": []}
    statuses = {"sha": HEAD, "total_count": 0, "statuses": []}
    evidence = await GitHubPullRequestConnector(  # type: ignore[arg-type]
        FakeCLI(_github_payload(), check_runs, statuses), _config(tmp_path)
    ).collect("owner/repo", 12, expected_sha=HEAD)

    assert evidence.checks_complete is True
    assert evidence.checks == ()
    assert evidence.checks_state == "pending"


async def test_github_cross_sha_check_page_stays_pending(tmp_path: Path) -> None:
    check_runs, statuses = _github_check_pages(check_sha="c" * 40)
    evidence = await GitHubPullRequestConnector(  # type: ignore[arg-type]
        FakeCLI(_github_payload(), check_runs, statuses), _config(tmp_path)
    ).collect("owner/repo", 12, expected_sha=HEAD)

    assert evidence.checks_complete is False
    assert [check.name for check in evidence.checks] == ["lint"]
    assert evidence.checks_state == "pending"


async def test_github_rejects_invalid_repository_before_provider_read(tmp_path: Path) -> None:
    cli = FakeCLI()
    connector = GitHubPullRequestConnector(cli, _config(tmp_path))  # type: ignore[arg-type]

    with pytest.raises(SchemaError):
        await connector.collect("owner/repo?per_page=1", 12)

    assert cli.calls == []


def test_legacy_github_evidence_without_completeness_fails_closed() -> None:
    payload = {
        "schemaVersion": 1,
        "provider": "github",
        "repository": "owner/repo",
        "number": 12,
        "url": "https://github.com/owner/repo/pull/12",
        "state": "merged",
        "draft": False,
        "headSha": HEAD,
        "mergeSha": MERGE,
        "observedAt": "2026-08-02T01:02:03+00:00",
        "checks": [
            {
                "name": "tests",
                "state": "passed",
                "sha": HEAD,
                "url": "https://github.com/owner/repo/actions/runs/1",
            }
        ],
        "checksState": "passed",
        "completionState": "passed",
    }

    evidence = ProviderChangeEvidence.from_dict(payload)

    assert evidence.checks_complete is False
    assert evidence.checks_state == "pending"
    assert evidence.completion_state == "pending"


def test_check_completeness_changes_semantic_digest() -> None:
    complete = ProviderChangeEvidence.from_dict(
        {
            "schemaVersion": 2,
            "provider": "github",
            "repository": "owner/repo",
            "number": 12,
            "url": "https://github.com/owner/repo/pull/12",
            "state": "open",
            "draft": False,
            "headSha": HEAD,
            "observedAt": "2026-08-02T01:02:03+00:00",
            "checks": [
                {"name": "tests", "state": "passed", "sha": HEAD}
            ],
            "checksComplete": True,
        }
    )
    incomplete_payload = complete.to_dict()
    incomplete_payload["checksComplete"] = False
    incomplete_payload.pop("checksState")
    incomplete_payload.pop("completionState")
    incomplete = ProviderChangeEvidence.from_dict(incomplete_payload)

    assert complete.payload_digest != incomplete.payload_digest


def test_schema_v2_requires_explicit_check_completeness() -> None:
    payload = {
        "schemaVersion": 2,
        "provider": "github",
        "repository": "owner/repo",
        "number": 12,
        "url": "https://github.com/owner/repo/pull/12",
        "state": "open",
        "draft": False,
        "headSha": HEAD,
        "observedAt": "2026-08-02T01:02:03+00:00",
        "checks": [],
    }

    with pytest.raises(SchemaError, match="checksComplete"):
        ProviderChangeEvidence.from_dict(payload)


async def test_github_collects_merged_sha_bound_checks_without_mutation(tmp_path: Path) -> None:
    check_runs, statuses = _github_check_pages()
    cli = FakeCLI(_github_payload(), check_runs, statuses)
    connector = GitHubPullRequestConnector(cli, _config(tmp_path))  # type: ignore[arg-type]

    evidence = await connector.collect("owner/repo", 12, expected_sha=HEAD)

    assert evidence.state == "merged"
    assert evidence.checks_state == "passed"
    assert evidence.completion_state == "passed"
    assert evidence.head_sha == HEAD
    assert evidence.merge_sha == MERGE
    assert cli.calls == [
        (
            "pr",
            "view",
            "12",
            "--repo",
            "owner/repo",
            "--json",
            "number,url,state,isDraft,mergedAt,headRefOid,mergeCommit,updatedAt",
        ),
        (
            "api",
            f"repos/owner/repo/commits/{HEAD}/check-runs?filter=latest&per_page=100",
        ),
        ("api", f"repos/owner/repo/commits/{HEAD}/status?per_page=100"),
    ]
    assert all("merge" not in call[:2] and "close" not in call for call in cli.calls)


@pytest.mark.parametrize(
    ("state", "conclusion", "expected"),
    [("OPEN", "", "pending"), ("MERGED", "FAILURE", "failed")],
)
async def test_github_normalizes_pending_and_failed_states(
    tmp_path: Path, state: str, conclusion: str, expected: str
) -> None:
    check_runs, statuses = _github_check_pages()
    check_runs["check_runs"][0]["conclusion"] = conclusion.casefold() or None
    evidence = await GitHubPullRequestConnector(  # type: ignore[arg-type]
        FakeCLI(_github_payload(state=state), check_runs, statuses),
        _config(tmp_path),
    ).collect("owner/repo", 12)

    assert evidence.completion_state == expected


async def test_github_rejects_sha_mismatch_malformed_and_unbounded_checks(tmp_path: Path) -> None:
    connector = GitHubPullRequestConnector(  # type: ignore[arg-type]
        FakeCLI(_github_payload()), _config(tmp_path)
    )
    with pytest.raises(SchemaError) as mismatch:
        await connector.collect("owner/repo", 12, expected_sha="c" * 40)
    assert mismatch.value.code == "provider_sha_mismatch"

    malformed = GitHubPullRequestConnector(FakeCLI([]), _config(tmp_path))  # type: ignore[arg-type]
    with pytest.raises(SchemaError) as malformed_error:
        await malformed.collect("owner/repo", 12)
    assert malformed_error.value.code == "invalid_provider_evidence"

    check_runs, statuses = _github_check_pages()
    check_runs["check_runs"] = check_runs["check_runs"] * 3
    bounded = GitHubPullRequestConnector(  # type: ignore[arg-type]
        FakeCLI(_github_payload(), check_runs, statuses),
        _config(tmp_path, provider_evidence_max_checks=2),
    )
    with pytest.raises(SchemaError) as limit:
        await bounded.collect("owner/repo", 12)
    assert limit.value.code == "provider_evidence_limit"


async def test_gitlab_collects_one_bounded_pipeline_page_and_binds_sha(tmp_path: Path) -> None:
    mr = {
        "iid": 7,
        "web_url": "https://gitlab.com/group/repo/-/merge_requests/7",
        "state": "merged",
        "draft": False,
        "sha": HEAD,
        "merge_commit_sha": MERGE,
        "updated_at": "2026-08-02T01:02:03Z",
    }
    pipelines = [
        {
            "id": 44,
            "sha": HEAD,
            "status": "success",
            "web_url": "https://gitlab.com/group/repo/-/pipelines/44",
        },
        {"id": 43, "sha": "c" * 40, "status": "failed"},
    ]
    cli = FakeCLI(mr, pipelines)
    connector = GitLabMergeRequestConnector(cli, _config(tmp_path))  # type: ignore[arg-type]

    evidence = await connector.collect("group/repo", 7, expected_sha=HEAD)

    assert evidence.state == "merged"
    assert evidence.completion_state == "passed"
    assert [check.name for check in evidence.checks] == ["pipeline:44"]
    assert cli.calls[0] == ("mr", "view", "7", "--repo", "group/repo", "--output", "json")
    assert cli.calls[1] == (
        "api",
        "projects/group%2Frepo/merge_requests/7/pipelines?per_page=20&page=1",
    )
    assert all("merge" not in call[:2] and "close" not in call for call in cli.calls)


def test_provider_evidence_emits_completion_facts_bound_to_same_subject() -> None:
    evidence = ProviderChangeEvidence.from_dict(
        {
            "schemaVersion": 2,
            "provider": "github",
            "repository": "owner/repo",
            "number": 12,
            "url": "https://github.com/owner/repo/pull/12",
            "state": "merged",
            "draft": False,
            "headSha": HEAD,
            "mergeSha": MERGE,
            "observedAt": "2026-08-02T01:02:03+00:00",
            "checks": [
                {
                    "name": "tests",
                    "state": "passed",
                    "sha": HEAD,
                    "url": "https://github.com/owner/repo/actions/runs/1",
                }
            ],
            "checksComplete": True,
        }
    )

    facts = evidence.to_completion_facts(
        work_thread_id="thread-1", cycle=1, received_at="2026-08-02T01:03:00+00:00"
    )

    assert {fact.kind for fact in facts} == {"provider_state", "provider_checks"}
    assert {fact.subject.resource_version for fact in facts} == {HEAD}
    assert {fact.claim.outcome for fact in facts} == {"merged", "passed"}
    assert all(fact.assurance == "verified" for fact in facts)
    assert ProviderChangeEvidence.from_dict(evidence.to_dict()) == evidence


async def test_provider_evidence_cli_emits_normalized_facts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("OSTAG_PROVIDER_EVIDENCE_ENABLED", "true")
    monkeypatch.setenv("OSTAG_GITHUB_DEFAULT_REPO", "owner/repo")

    check_runs, statuses = _github_check_pages()

    async def fake_run(self, *args: str) -> str:
        if args[0] == "pr":
            return json.dumps(_github_payload())
        if "check-runs" in args[1]:
            return json.dumps(check_runs)
        return json.dumps(statuses)

    monkeypatch.setattr(GhCLI, "run", fake_run)

    exit_code = await main_async(
        [
            "connector",
            "evidence",
            "github",
            "--number",
            "12",
            "--expected-sha",
            HEAD,
            "--work-thread",
            "thread-1",
            "--cycle",
            "1",
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["evidence"]["completionState"] == "passed"
    assert len(payload["completionFacts"]) == 2
    assert len(payload["payloadDigest"]) == 64
