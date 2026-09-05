from __future__ import annotations

import re
import subprocess
from pathlib import Path

import yaml


WORKFLOW = Path(".github/workflows/deploy-production.yml")
DEPLOY_SCRIPT = Path("scripts/deploy_work_monitor_release.sh")
SSH_WRAPPER = Path("deploy/work-monitor/ostag-github-actions-ssh")


def _workflow() -> dict:
    return yaml.load(WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def test_production_workflow_runs_only_after_main_ci_or_manual_dispatch() -> None:
    workflow = _workflow()

    assert workflow["on"]["workflow_run"] == {
        "workflows": ["OS Tag CI"],
        "branches": ["main"],
        "types": ["completed"],
    }
    assert "workflow_dispatch" in workflow["on"]
    assert workflow["permissions"] == {"checks": "read", "contents": "read"}
    assert workflow["concurrency"] == {
        "group": "ostag-production",
        "cancel-in-progress": "false",
    }
    assert workflow["jobs"]["deploy"]["environment"]["name"] == "production"


def test_production_workflow_pins_actions_and_keeps_credentials_in_environment() -> None:
    workflow = _workflow()
    steps = workflow["jobs"]["deploy"]["steps"]
    uses = [step["uses"] for step in steps if "uses" in step]
    text = WORKFLOW.read_text(encoding="utf-8")

    assert uses == [
        "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1"
    ]
    assert all(re.fullmatch(r"[^@]+@[0-9a-f]{40}", use) for use in uses)
    assert "secrets.PRODUCTION_SSH_PRIVATE_KEY" in text
    assert "secrets.PRODUCTION_SSH_KNOWN_HOSTS" in text
    assert "vars.PRODUCTION_SSH_HOST" in text
    assert "vars.PRODUCTION_DEPLOY_SCRIPT_SHA256" in text
    assert "vars.PRODUCTION_SSH_WRAPPER_SHA256" in text
    assert "ssh-keyscan" not in text
    assert "scp " not in text
    assert "43.160." not in text


def test_production_workflow_is_commit_bound_and_health_checked() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")

    assert '[[ "$candidate" == "$main_sha" ]]' in text
    assert '.name == "release-check" and .conclusion == "success"' in text
    assert "git archive --format=tar" in text
    assert "sha256sum" in text
    assert '"$PRODUCTION_URL/healthz"' in text
    assert "cancel-in-progress: false" in text


def test_server_deploy_script_has_backup_health_and_program_rollback_gates() -> None:
    result = subprocess.run(
        ["bash", "-n", str(DEPLOY_SCRIPT)], capture_output=True, text=True
    )
    text = DEPLOY_SCRIPT.read_text(encoding="utf-8")

    assert result.returncode == 0, result.stderr
    assert "PRAGMA integrity_check" in text
    assert 'verify_archive "$source_archive" "$source_sha256" source' in text
    assert 'verify_archive "$image_archive" "$image_sha256" image' in text
    assert 'fail "$label archive checksum mismatch"' in text
    assert "rollback_previous_release" in text
    assert "previousComposeFile" in text
    assert "docker-compose.production.yml" not in text
    assert "--wait-timeout 180" in text


def test_production_ssh_key_is_forced_through_a_bounded_command_wrapper() -> None:
    result = subprocess.run(
        ["bash", "-n", str(SSH_WRAPPER)], capture_output=True, text=True
    )
    text = SSH_WRAPPER.read_text(encoding="utf-8")

    assert result.returncode == 0, result.stderr
    assert "SSH_ORIGINAL_COMMAND" in text
    assert "upload-source" in text
    assert "upload-image" in text
    assert "deploy\\ " in text
    assert "maximum_bytes" in text
    assert "exec \"$deploy_script\" deploy" in text
    assert "eval " not in text
    assert "bash -c" not in text


def test_server_deploy_script_rejects_bad_inputs_before_touching_runtime() -> None:
    result = subprocess.run(
        [
            str(DEPLOY_SCRIPT),
            "deploy",
            "not-a-sha",
            "source.tar.gz",
            "0" * 64,
            "image.tar.gz",
            "0" * 64,
        ],
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "release SHA must be 40 lowercase hex characters" in result.stderr


def test_deploy_runbook_is_persisted_for_future_release_runs() -> None:
    deployment = Path("docs/ostag/deployment.md").read_text(encoding="utf-8")

    assert "## GitHub Actions production deployment" in deployment
    assert ".github/workflows/deploy-production.yml" in deployment
    assert "https://ostag-monitor.ihiking.me" in deployment
    assert "PRODUCTION_SSH_KNOWN_HOSTS" in deployment
    assert "deploy-receipt.json" in deployment
