from __future__ import annotations

from pathlib import Path

import pytest

from ostag.config import load_settings


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
