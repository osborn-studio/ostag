"""No installed osstack or real model dependency in the portable regression suite."""

from copy import deepcopy
import sys

import pytest

from scripts.engineering.runner_loop_probe import (
    _command,
    validate_verification,
    validate_native_state,
)


def receipt():
    return dict(
        candidate_sha256="a" * 64,
        model="claude-opus-5",
        verdict="approved",
        blockers=[],
        tests=[dict(name="suite", passed=1, failed=0, exit_code=0)],
    )


def test_valid_receipt_shape():
    validate_verification(receipt(), candidate_sha256="a" * 64, expected_model="claude-opus-5")


@pytest.mark.parametrize(
    "change",
    [
        "hash",
        "model",
        "tests",
        "empty",
        "bool",
        "failed",
        "exit",
        "blockers",
        "verdict",
        "unknown",
        "none",
    ],
)
def test_bad_verification_is_not_approval(change):
    value = receipt()
    if change == "hash":
        value["candidate_sha256"] = "b" * 64
    elif change == "model":
        value["model"] = "another-model"
    elif change == "tests":
        value["tests"] = []
    elif change == "empty":
        value["tests"][0]["passed"] = 0
    elif change == "bool":
        value["tests"][0]["passed"] = True
    elif change == "failed":
        value["tests"][0]["failed"] = 1
    elif change == "exit":
        value["tests"][0]["exit_code"] = 1
    elif change == "blockers":
        value["blockers"] = ["defect"]
    elif change == "verdict":
        value["verdict"] = "changes_requested"
    elif change == "unknown":
        value["automatic_writes_allowed"] = True
    else:
        value = None
    with pytest.raises(ValueError):
        validate_verification(value, candidate_sha256="a" * 64, expected_model="claude-opus-5")


def state():
    return dict(
        in_progress=[],
        completed_tasks=[
            dict(
                task_id="one",
                iterations=1,
                outcome="success",
                verify_issue_count=0,
                verify_commands=[dict(exit_code=0)],
                evaluations=[{"pass": True}],
            )
        ],
        deferred_tasks=[dict(task=dict(task_id="old-canary"), authorization="blocked")],
    )


@pytest.mark.parametrize(
    "change", ["extra", "wrong", "no_tests", "bad_tests", "old", "in_progress"]
)
def test_native_zero_exit_cannot_hide_wrong_or_unverified_tasks(change):
    value = state()
    if change == "extra":
        value["completed_tasks"].append(deepcopy(value["completed_tasks"][0]))
    elif change == "wrong":
        value["completed_tasks"][0]["task_id"] = "old-canary"
    elif change == "no_tests":
        value["completed_tasks"][0]["verify_commands"] = []
    elif change == "bad_tests":
        value["completed_tasks"][0]["evaluations"] = [{"pass": False}]
    elif change == "old":
        value["deferred_tasks"] = []
    else:
        value["in_progress"] = ["pending"]
    with pytest.raises(ValueError):
        validate_native_state(value, task_id="one", scenario="success")


def test_native_success_requires_one_attempt_and_real_verifier_record():
    validate_native_state(state(), task_id="one", scenario="success")


def test_process_failure_preserves_output(tmp_path):
    result = _command(
        [sys.executable, "-c", 'import sys;print("fixture");sys.exit(7)'],
        cwd=tmp_path,
        output=tmp_path,
        name="failed",
        timeout=5,
    )
    assert result == {"exit_code": 7, "timed_out": False}
    assert (tmp_path / "failed.stdout").read_text().strip() == "fixture"
    assert (tmp_path / "failed.stderr").exists()


def test_process_timeout_is_recorded_and_leader_reaped(tmp_path):
    result = _command(
        [sys.executable, "-c", "import time;time.sleep(30)"],
        cwd=tmp_path,
        output=tmp_path,
        name="timeout",
        timeout=0.1,
    )
    assert result["timed_out"] and result["exit_code"] != 0
    assert (tmp_path / "timeout.stdout").exists()
    assert (tmp_path / "timeout.stderr").exists()


def test_timeout_kills_term_ignoring_descendant(tmp_path):
    import os
    import signal
    import time

    pidfile = tmp_path / "child.pid"
    child = (
        "import os,signal,pathlib,time;"
        "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
        f"pathlib.Path({str(pidfile)!r}).write_text(str(os.getpid()));"
        "time.sleep(30)"
    )
    parent = (
        "import subprocess,sys,time;"
        f'subprocess.Popen([sys.executable,"-c",{child!r}]);'
        "time.sleep(30)"
    )
    result = _command(
        [sys.executable, "-c", parent], cwd=tmp_path, output=tmp_path, name="descendants", timeout=1
    )
    assert result["timed_out"]
    assert pidfile.exists(), "child fixture never reached its TERM-ignoring state"
    pid = int(pidfile.read_text())
    # On Linux an orphan killed child may briefly remain a zombie. A zombie
    # cannot continue work; on macOS it disappears after launchd reaps it.
    deadline = time.monotonic() + 3
    try:
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            from pathlib import Path

            stat = Path("/proc") / str(pid) / "stat"
            if stat.exists() and stat.read_text().split(") ", 1)[1].startswith("Z "):
                break
            time.sleep(0.01)
        else:
            pytest.fail("TERM-ignoring child survived timeout cleanup")
    finally:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def test_commands_do_not_inherit_credentials_or_osstack_overrides(tmp_path, monkeypatch):
    import json

    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-secret")
    monkeypatch.setenv("OSSTACK_HOME", "/synthetic/old-state")
    monkeypatch.setenv("NODE_OPTIONS", "--synthetic-host-option")
    result = _command(
        [
            sys.executable,
            "-c",
            "import json,os;print(json.dumps({k:os.environ[k] for k in ('ANTHROPIC_API_KEY','OSSTACK_HOME','NODE_OPTIONS','HOME','CODEX_HOME') if k in os.environ}))",
        ],
        cwd=tmp_path,
        output=tmp_path,
        name="env",
        timeout=5,
    )
    assert result["exit_code"] == 0
    env = json.loads((tmp_path / "env.stdout").read_text())
    assert "ANTHROPIC_API_KEY" not in env and "NODE_OPTIONS" not in env
    assert env["OSSTACK_HOME"] == str(tmp_path / "osstack-state")
    assert "HOME" not in env and "CODEX_HOME" not in env
    assert "synthetic-secret" not in (tmp_path / "env.stdout").read_text()
