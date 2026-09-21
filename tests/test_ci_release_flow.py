import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
INSTALL_COMMANDS = [
    "sudo apt-get update && sudo apt-get install -y ripgrep pandoc",
    "uv sync --all-extras --locked",
]
RELEASE_COMMANDS = [
    "uv run --locked python -m compileall ostag",
    "uv run ruff check ostag tests",
    "uv run pytest --collect-only -q",
    "uv run pytest -vv",
    "work_monitor_smoke.sh",
    "copilot_relay_smoke.sh",
    "secret_hygiene_check.sh",
    "docker compose -f deploy/work-monitor/docker-compose.yml config",
    "docker compose -f deploy/work-monitor/docker-compose.yml "
    "-f deploy/work-monitor/docker-compose.production.yml config",
    *[
        f"uv run python -m ostag.main --platform {platform} --dry-run --event examples/{event}"
        for platform, event in [
            ("feishu", "feishu_event.json --doctor"),
            ("feishu", "feishu_event.json"),
            ("lark", "lark_event.json"),
            ("slack", "slack_event.json"),
            ("dingtalk", "dingtalk_event.json"),
        ]
    ],
]
DEPLOY_COMMANDS = [
    "docker compose config",
    "render_launchd_plist.sh",
    "live_validation_record.sh feishu",
    *[f"connector_live_smoke.sh {p}" for p in ("feishu", "dingtalk", "github", "gitlab")],
    *[f"provider_evidence_live_smoke.sh {p}" for p in ("github", "gitlab")],
    "mfs_validation_record.sh",
]
EVIDENCE_COMMANDS = [
    "uv run pytest -q tests/test_connector_diagnostics.py tests/test_connector_live_smoke.py",
    "uv run pytest -q tests/test_provider_evidence.py tests/test_provider_evidence_live_smoke.py",
    "validate_live_validation_record.sh feishu",
    "validate_live_validation_record.sh feishu "
    "release/live-validation-gate-fixture/live-validation-feishu-pass.md",
]


def _workflow():
    return yaml.load(
        (ROOT / ".github/workflows/ostag-ci.yml").read_text(), Loader=yaml.BaseLoader
    )


def test_ci_keeps_required_setup_and_one_unconditional_gate():
    workflow = _workflow()
    assert workflow["on"] == {"push": {"branches": ["main", "ostag/**"]}, "pull_request": ""}
    job = workflow["jobs"]["release-check"]
    assert job["runs-on"] == "ubuntu-latest"
    assert job["timeout-minutes"] == "15"
    assert job["env"] == {"OSTAG_SKIP_OSSTACK_CROSS_REPO_SMOKE": "1",
                          "PYTEST_ADDOPTS": "-o faulthandler_timeout=60"}
    setup = next(s for s in job["steps"] if s.get("uses") == "actions/setup-python@v5")
    assert setup["with"]["python-version"] == "3.11"
    assert [s["run"] for s in job["steps"] if "run" in s] == [
        *INSTALL_COMMANDS, "scripts/ci_release_gates.sh"
    ]
    for item in [job, *job["steps"]]:
        assert "if" not in item
        assert "continue-on-error" not in item


@pytest.mark.parametrize("entry", ["workflow", "ci_release_gates.sh", "deploy_preflight.sh"])
@pytest.mark.parametrize("fail_command", ["", "uv run pytest -vv", RELEASE_COMMANDS[-1]])
def test_release_flow_runs_once_and_stops_at_failure(tmp_path, entry, fail_command):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_sample.py").touch()
    (tmp_path / "release").mkdir()
    shutil.copyfile(
        ROOT / "release/manual-live-validation-handoff.md",
        tmp_path / "release/manual-live-validation-handoff.md",
    )
    for name in ("ci_release_gates.sh", "deploy_preflight.sh", "release_check.sh"):
        # Only relocate fixed scratch paths; execute the real shell control flow.
        source = (ROOT / "scripts" / name).read_text()
        target = scripts / name
        target.write_text(source.replace("/tmp/ostag-", f"{tmp_path}/ostag-"))
        target.chmod(0o755)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    rg = shutil.which("rg")
    assert rg, "release gates require ripgrep"
    (bin_dir / "rg").symlink_to(rg)
    (tmp_path / "launch.plist").write_text(f"{tmp_path}\nostag.main\n--live\n")
    (tmp_path / "record.md").write_text("Status: blocked_missing_credentials\n")
    stub = '''#!/bin/bash
set -eu
command="${0##*/}${*:+ $*}"
printf '%s\n' "$command" >> "$TRACE"
if [[ "$command" == "$FAIL_COMMAND" ]]; then exit 42; fi
case "$command" in
  'uv run pytest --collect-only -q') echo 'tests/test_sample.py::test_sample' ;;
  render_launchd_plist.sh) echo "$PWD/launch.plist" ;;
  'live_validation_record.sh feishu'|mfs_validation_record.sh) echo "$PWD/record.md" ;;
  'validate_live_validation_record.sh feishu') exit 3 ;;
  'validate_live_validation_record.sh feishu '*) echo 'live validation record passed' ;;
esac
'''
    for name in (
        "uv", "docker", "work_monitor_smoke.sh", "copilot_relay_smoke.sh",
        "secret_hygiene_check.sh", "render_launchd_plist.sh", "live_validation_record.sh",
        "connector_live_smoke.sh", "provider_evidence_live_smoke.sh",
        "mfs_validation_record.sh", "validate_live_validation_record.sh",
    ):
        target = (bin_dir if name in ("uv", "docker") else scripts) / name
        target.write_text(stub)
        target.chmod(0o755)

    job = _workflow()["jobs"]["release-check"]
    commands = [f"scripts/{entry}"]
    if entry == "workflow":
        commands = [
            step["run"] for step in job["steps"]
            if "run" in step and step["run"] not in INSTALL_COMMANDS
        ]
    trace = tmp_path / "commands.log"
    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", "\n".join(commands)],
        cwd=tmp_path,
        # Deliberately inherit no credentials or optional smoke overrides.
        env={
            "PATH": f"{bin_dir}{os.pathsep}{os.defpath}",
            "TRACE": str(trace), "FAIL_COMMAND": fail_command, **job["env"],
        },
        capture_output=True, text=True, timeout=30,
    )
    observed = trace.read_text().splitlines()
    if fail_command:
        assert result.returncode == 42, result.stdout + result.stderr
        assert observed == RELEASE_COMMANDS[:RELEASE_COMMANDS.index(fail_command) + 1]
        assert not (tmp_path / "release/live-validation-gate-fixture").exists()
    else:
        assert result.returncode == 0, result.stdout + result.stderr
        expected = RELEASE_COMMANDS + DEPLOY_COMMANDS
        if entry != "deploy_preflight.sh":
            expected += EVIDENCE_COMMANDS
            assert "ci release gates passed" in result.stdout
        assert observed == expected


def test_real_collection_nodeids_survive_workflow_diagnostics():
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q",
         "tests/test_ci_release_flow.py"], cwd=ROOT,
        env={"PATH": os.defpath, **_workflow()["jobs"]["release-check"]["env"]},
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    nodes = [line for line in result.stdout.splitlines()
             if line.startswith("tests/test_ci_release_flow.py::")]
    assert len(nodes) == 11
    assert "tests/test_ci_release_flow.py::test_real_collection_nodeids_survive_workflow_diagnostics" in nodes
