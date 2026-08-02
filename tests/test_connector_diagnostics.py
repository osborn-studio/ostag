from __future__ import annotations

import json
from pathlib import Path

import pytest

from ostag.config import load_settings
from ostag.core.connector_diagnostics import ProbeResult, connector_test
from ostag.main import main_async


def _config(tmp_path: Path, **overrides: object):
    return load_settings(None, audit_log=tmp_path / "audit.log", **overrides)


def test_github_local_diagnostic_reports_missing_cli_and_target_without_secrets(
    tmp_path: Path,
) -> None:
    secret = "synthetic-github-secret-must-not-leak"
    config = _config(tmp_path, gh_bin=secret, github_default_repo="")

    report = connector_test(config, "github", which=lambda _: None)
    payload = report.to_dict()
    encoded = json.dumps(payload)

    assert payload["schema_version"] == 1
    assert payload["platform"] == "github"
    assert payload["mode"] == "local"
    assert payload["status"] == "blocked"
    assert payload["read_ready"] is False
    assert payload["write_ready"] is False
    assert {item["id"] for item in payload["checks"]} >= {"cli", "target"}
    assert secret not in encoded


def test_github_live_diagnostic_uses_only_read_only_bounded_commands(tmp_path: Path) -> None:
    calls: list[tuple[tuple[str, ...], float]] = []

    def probe(argv: tuple[str, ...], timeout: float) -> ProbeResult:
        calls.append((argv, timeout))
        return ProbeResult(ok=True)

    config = _config(
        tmp_path,
        gh_bin="gh",
        github_default_repo="owner/repo",
        governed_writes=True,
        apply_enabled=True,
    )

    report = connector_test(
        config,
        "github",
        live=True,
        which=lambda _: "/opt/homebrew/bin/gh",
        probe=probe,
    )

    assert report.status == "ready"
    assert report.read_ready is True
    assert report.write_ready is True
    assert calls == [
        (("gh", "auth", "status", "--hostname", "github.com"), 10.0),
        (("gh", "repo", "view", "owner/repo", "--json", "nameWithOwner"), 10.0),
    ]
    assert all("create" not in argv and "comment" not in argv for argv, _ in calls)


def test_live_probe_failure_is_generic_and_redacts_provider_output(tmp_path: Path) -> None:
    secret = "provider-output-secret"

    def probe(argv: tuple[str, ...], timeout: float) -> ProbeResult:
        return ProbeResult(ok=False, reason=f"timed out with {secret}")

    report = connector_test(
        _config(tmp_path, gh_bin="gh", github_default_repo="owner/repo"),
        "github",
        live=True,
        which=lambda _: "/usr/bin/gh",
        probe=probe,
    )
    encoded = json.dumps(report.to_dict())

    assert report.status == "blocked"
    assert "live authentication probe failed" in encoded
    assert secret not in encoded


def test_feishu_reports_credentials_cli_target_and_governed_write_readiness(
    tmp_path: Path,
) -> None:
    secret = "feishu-secret-must-not-leak"
    config = _config(
        tmp_path,
        feishu_app_id="app-id",
        feishu_app_secret=secret,
        lark_cli_bin="lark-cli",
        auto_base_default_base_token="base-token-must-not-leak",
        auto_base_default_table_id="tbl123",
        governed_writes=True,
        apply_enabled=True,
    )

    report = connector_test(config, "feishu", which=lambda _: "/usr/bin/lark-cli")
    encoded = json.dumps(report.to_dict())

    assert report.status == "ready"
    assert report.read_ready is True
    assert report.write_ready is True
    assert report.target == {"kind": "base_table", "configured": True}
    assert secret not in encoded
    assert "base-token-must-not-leak" not in encoded


def test_dingtalk_resolves_first_argv_of_configured_cli(tmp_path: Path) -> None:
    observed: list[str] = []

    def which(name: str) -> str | None:
        observed.append(name)
        return "/usr/bin/npx" if name == "npx" else None

    config = _config(
        tmp_path,
        dingtalk_client_id="client",
        dingtalk_client_secret="secret",
        dws_cli_bin="npx -y dingtalk-workspace-cli",
        daily_task_board_dingtalk_table_id="table-id",
    )

    report = connector_test(config, "dingtalk", which=which)

    assert observed == ["npx"]
    assert report.read_ready is True
    assert report.write_ready is False


@pytest.mark.asyncio
async def test_connector_test_cli_emits_stable_json_and_failure_exit(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("OSTAG_GH_BIN", "definitely-missing-gh")
    monkeypatch.delenv("OSTAG_GITHUB_DEFAULT_REPO", raising=False)

    exit_code = await main_async(["connector", "test", "github", "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert payload["platform"] == "github"
    assert payload["status"] == "blocked"
    assert isinstance(payload["checks"], list)
