from __future__ import annotations

import json
from pathlib import Path

import pytest

import ostag.connectors.gitlab as gitlab_module
from ostag.config import load_settings
from ostag.connectors.gitlab import (
    GitLabApplyExecutor,
    GitLabCLIError,
    GitLabIssuesConnector,
    GlabCLI,
)
from ostag.core.schemas import ActionReceipt, SchemaError, new_id
from ostag.main import main_async


class FakeGlab:
    def __init__(self, output: str = "https://gitlab.com/g/p/-/issues/3") -> None:
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
        run_id="run_gl1",
        title="Create GitLab issue",
        target_system="gitlab_issues",
        target_object=payload.get("repo", "g/p"),
        mutation_type="create",
        state="needs_approval",
        apply_payload=payload,
    )


async def test_create_issue_builds_glab_command() -> None:
    glab = FakeGlab()
    connector = GitLabIssuesConnector(glab)  # type: ignore[arg-type]

    out = await connector.create_issue(
        "g/p", title="Bug", description="d", assignee="alice", labels=("bug", "p1")
    )

    assert out == "https://gitlab.com/g/p/-/issues/3"
    assert glab.calls[0] == (
        "issue",
        "create",
        "--repo",
        "g/p",
        "--title",
        "Bug",
        "--description",
        "d",
        "--assignee",
        "alice",
        "--label",
        "bug,p1",
    )


async def test_comment_issue_builds_glab_command() -> None:
    glab = FakeGlab()
    connector = GitLabIssuesConnector(glab)  # type: ignore[arg-type]

    await connector.comment_issue("g/p", 3, body="thanks")

    assert glab.calls[0] == (
        "issue",
        "note",
        "3",
        "--repo",
        "g/p",
        "--message",
        "thanks",
    )


async def test_executor_default_repo_and_evidence(tmp_path: Path) -> None:
    config = _config(tmp_path, gitlab_default_repo="group/default")
    glab = FakeGlab()
    executor = GitLabApplyExecutor(GitLabIssuesConnector(glab), config)  # type: ignore[arg-type]

    evidence = await executor.apply(
        _receipt({"operation": "create_issue", "title": "T", "body": "B"})
    )

    assert "created issue" in evidence
    assert ("--repo", "group/default") == glab.calls[0][2:4]


async def test_executor_validates_payloads(tmp_path: Path) -> None:
    config = _config(tmp_path)  # no default repo
    executor = GitLabApplyExecutor(GitLabIssuesConnector(FakeGlab()), config)  # type: ignore[arg-type]

    with pytest.raises(SchemaError) as excinfo:
        await executor.apply(_receipt({"operation": "create_issue", "title": "T"}))
    assert excinfo.value.code == "invalid_apply_payload"
    assert "OSTAG_GITLAB_DEFAULT_REPO" in excinfo.value.recovery_hint

    with pytest.raises(SchemaError) as excinfo:
        await executor.apply(
            _receipt({"operation": "comment_issue", "repo": "g/p", "number": -1, "body": "x"})
        )
    assert excinfo.value.code == "invalid_apply_payload"

    with pytest.raises(SchemaError) as excinfo:
        await executor.apply(_receipt({"operation": "delete_issue", "repo": "g/p"}))
    assert excinfo.value.code == "unsupported_operation"


async def test_glab_cli_error_surfaces(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeProc:
        returncode = 1

        async def communicate(self):
            return b"", b"glab: not authenticated"

    async def fake_exec(*args, **kwargs):
        return FakeProc()

    monkeypatch.setattr(gitlab_module.asyncio, "create_subprocess_exec", fake_exec)

    with pytest.raises(GitLabCLIError) as excinfo:
        await GlabCLI(_config(tmp_path)).run("issue", "create")
    assert "not authenticated" in str(excinfo.value)


async def test_full_cli_loop_propose_approve_apply_gitlab(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("OSTAG_RUN_LEDGER_PATH", str(tmp_path / "ledger.jsonl"))
    monkeypatch.setenv("OSTAG_AUDIT_LOG", str(tmp_path / "audit.log"))
    monkeypatch.setenv("OSTAG_APPLY_ENABLED", "true")
    calls: list[tuple[str, ...]] = []

    async def fake_run(self, *args: str) -> str:
        calls.append(args)
        return "https://gitlab.com/g/p/-/issues/9"

    monkeypatch.setattr(GlabCLI, "run", fake_run)

    payload = _receipt(
        {"operation": "create_issue", "repo": "g/p", "title": "From OS Tag", "body": "governed"}
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
    assert calls == []

    assert await main_async(["approve", payload["action_id"], "--json"]) == 0
    capsys.readouterr()
    assert calls == []

    exit_code = await main_async(["apply", payload["action_id"], "--json"])
    assert exit_code == 0
    result = json.loads(capsys.readouterr().out)
    assert result["action"]["state"] == "applied"
    assert "issues/9" in result["evidence"]
    assert len(calls) == 1
    assert calls[0][:2] == ("issue", "create")
