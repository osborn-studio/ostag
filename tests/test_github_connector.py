from __future__ import annotations

import json
from pathlib import Path

import pytest

import ostag.connectors.github as github_module
from ostag.config import load_settings
from ostag.connectors.github import (
    GhCLI,
    GitHubApplyExecutor,
    GitHubCLIError,
    GitHubIssuesConnector,
)
from ostag.core.schemas import ActionReceipt, SchemaError, new_id
from ostag.main import main_async


class FakeGh:
    def __init__(self, output: str = "https://github.com/o/r/issues/7") -> None:
        self.calls: list[tuple[str, ...]] = []
        self.output = output

    async def run(self, *args: str) -> str:
        self.calls.append(args)
        return self.output


def _config(tmp_path: Path, **overrides: object):
    return load_settings(None, audit_log=tmp_path / "audit.log", **overrides)


def _receipt(payload: dict) -> ActionReceipt:
    return ActionReceipt(
        action_id=new_id("action"),
        run_id="run_gh1",
        title="Create GitHub issue",
        target_system="github_issues",
        target_object=payload.get("repo", "o/r"),
        mutation_type="create",
        state="needs_approval",
        apply_payload=payload,
    )


async def test_create_issue_builds_gh_command() -> None:
    gh = FakeGh()
    connector = GitHubIssuesConnector(gh)  # type: ignore[arg-type]

    url = await connector.create_issue(
        "o/r", title="Bug: crash", body="details", assignee="alice", labels=("bug", "p1")
    )

    assert url == "https://github.com/o/r/issues/7"
    assert gh.calls[0] == (
        "issue",
        "create",
        "--repo",
        "o/r",
        "--title",
        "Bug: crash",
        "--body",
        "details",
        "--assignee",
        "alice",
        "--label",
        "bug",
        "--label",
        "p1",
    )


async def test_comment_issue_builds_gh_command() -> None:
    gh = FakeGh(output="https://github.com/o/r/issues/7#issuecomment-1")
    connector = GitHubIssuesConnector(gh)  # type: ignore[arg-type]

    await connector.comment_issue("o/r", 7, body="looks good")

    assert gh.calls[0] == (
        "issue",
        "comment",
        "7",
        "--repo",
        "o/r",
        "--body",
        "looks good",
    )


async def test_executor_create_issue_and_default_repo(tmp_path: Path) -> None:
    config = _config(tmp_path, github_default_repo="owner/default")
    gh = FakeGh()
    executor = GitHubApplyExecutor(GitHubIssuesConnector(gh), config)  # type: ignore[arg-type]

    evidence = await executor.apply(
        _receipt({"operation": "create_issue", "title": "T", "body": "B"})
    )

    assert "created issue https://github.com/o/r/issues/7" in evidence
    assert ("--repo", "owner/default") == gh.calls[0][2:4]


async def test_executor_validates_payloads(tmp_path: Path) -> None:
    config = _config(tmp_path)  # no default repo
    executor = GitHubApplyExecutor(GitHubIssuesConnector(FakeGh()), config)  # type: ignore[arg-type]

    with pytest.raises(SchemaError) as excinfo:
        await executor.apply(_receipt({"operation": "create_issue", "title": "T"}))
    assert excinfo.value.code == "invalid_apply_payload"
    assert "OSTAG_GITHUB_DEFAULT_REPO" in excinfo.value.recovery_hint

    with pytest.raises(SchemaError) as excinfo:
        await executor.apply(
            _receipt({"operation": "create_issue", "repo": "o/r", "title": ""})
        )
    assert excinfo.value.code == "invalid_apply_payload"

    with pytest.raises(SchemaError) as excinfo:
        await executor.apply(
            _receipt({"operation": "comment_issue", "repo": "o/r", "number": 0, "body": "x"})
        )
    assert excinfo.value.code == "invalid_apply_payload"

    with pytest.raises(SchemaError) as excinfo:
        await executor.apply(_receipt({"operation": "close_issue", "repo": "o/r"}))
    assert excinfo.value.code == "unsupported_operation"


async def test_gh_cli_error_surfaces(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeProc:
        returncode = 1

        async def communicate(self):
            return b"", b"gh: authentication required"

    async def fake_exec(*args, **kwargs):
        return FakeProc()

    monkeypatch.setattr(github_module.asyncio, "create_subprocess_exec", fake_exec)
    cli = GhCLI(_config(tmp_path))

    with pytest.raises(GitHubCLIError) as excinfo:
        await cli.run("issue", "create")
    assert "authentication required" in str(excinfo.value)


async def test_full_cli_loop_propose_approve_apply_github(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("OSTAG_RUN_LEDGER_PATH", str(tmp_path / "ledger.jsonl"))
    monkeypatch.setenv("OSTAG_AUDIT_LOG", str(tmp_path / "audit.log"))
    monkeypatch.setenv("OSTAG_APPLY_ENABLED", "true")
    calls: list[tuple[str, ...]] = []

    async def fake_run(self, *args: str) -> str:
        calls.append(args)
        return "https://github.com/o/r/issues/42"

    monkeypatch.setattr(GhCLI, "run", fake_run)

    payload = _receipt(
        {"operation": "create_issue", "repo": "o/r", "title": "From OS Tag", "body": "governed"}
    ).to_dict()
    receipt_file = tmp_path / "receipt.json"
    receipt_file.write_text(json.dumps(payload), encoding="utf-8")

    assert (
        await main_async(
            ["receipts", "propose", "--file", str(receipt_file), "--chat", "cli", "--json"]
        )
        == 0
    )
    capsys.readouterr()
    assert calls == []  # nothing executed at proposal time

    assert await main_async(["approve", payload["action_id"], "--json"]) == 0
    capsys.readouterr()
    assert calls == []  # nor at approval

    exit_code = await main_async(["apply", payload["action_id"], "--json"])
    assert exit_code == 0
    result = json.loads(capsys.readouterr().out)
    assert result["action"]["state"] == "applied"
    assert "issues/42" in result["evidence"]
    assert len(calls) == 1
    assert calls[0][:2] == ("issue", "create")
