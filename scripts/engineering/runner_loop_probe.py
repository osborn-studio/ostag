"""Fixed synthetic osstack single-attempt diagnostics; never calls a model.

This checks queue selection and rejection plumbing, not CLI sandbox isolation or
provider model identity. All command bodies and model receipts are fixtures.
Artifacts remain in a fresh operator-selected directory, including on failure.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from pathlib import Path
import shlex
import signal
import subprocess
import sys

SCENARIOS = (
    "success",
    "builder_failure",
    "verifier_failure",
    "wrong_model",
    "empty_verifier",
    "timeout",
)
MODEL = "claude-opus-5"


def validate_verification(receipt, *, candidate_sha256, expected_model):
    """Reject absent tests, wrong candidate/model and non-approval evidence.

    The caller must obtain this receipt from its trusted verifier, not its
    builder or model-written files. Shape checks cannot authenticate provenance.
    """
    if (
        type(candidate_sha256) is not str
        or re.fullmatch(r"[0-9a-f]{64}", candidate_sha256) is None
        or type(expected_model) is not str
        or not expected_model.strip()
        or type(receipt) is not dict
        or set(receipt) != {"candidate_sha256", "model", "tests", "verdict", "blockers"}
        or receipt["candidate_sha256"] != candidate_sha256
        or receipt["model"] != expected_model
        or receipt["verdict"] != "approved"
        or receipt["blockers"] != []
        or type(receipt["tests"]) is not list
        or not receipt["tests"]
    ):
        raise ValueError("invalid verification evidence")
    for test in receipt["tests"]:
        if (
            type(test) is not dict
            or set(test) != {"name", "passed", "failed", "exit_code"}
            or type(test["name"]) is not str
            or not test["name"].strip()
            or type(test["passed"]) is not int
            or test["passed"] < 1
            or type(test["failed"]) is not int
            or test["failed"] != 0
            or type(test["exit_code"]) is not int
            or test["exit_code"] != 0
        ):
            raise ValueError("empty or failed verifier")


def validate_native_state(state, *, task_id, scenario):
    if type(state) is not dict or state.get("in_progress") != []:
        raise ValueError("incomplete native state")
    completed = state.get("completed_tasks")
    if type(completed) is not list or len(completed) != 1:
        raise ValueError("not exactly one executed task")
    task = completed[0]
    if (
        task.get("task_id") != task_id
        or task.get("iterations") != 1
        or task.get("outcome") != ("success" if scenario == "success" else "escalated")
    ):
        raise ValueError("wrong task or outcome")
    if not any(
        d.get("task", {}).get("task_id") == "old-canary" and d.get("authorization") == "blocked"
        for d in state.get("deferred_tasks", [])
    ):
        raise ValueError("historical task not blocked")
    if scenario == "success":
        checks = task.get("verify_commands")
        if (
            not checks
            or any(c.get("exit_code") != 0 for c in checks)
            or task.get("verify_issue_count") != 0
            or not task.get("evaluations")
            or any(e.get("pass") is not True for e in task["evaluations"])
        ):
            raise ValueError("missing native verification")
    elif scenario in ("wrong_model", "empty_verifier", "verifier_failure"):
        if not any(c.get("exit_code") != 0 for c in task.get("verify_commands", [])):
            raise ValueError("negative verifier did not fail")
    elif not any(c.get("exit_code") != 0 for c in task.get("execution_commands", [])):
        raise ValueError("negative builder did not fail")


def diagnostic_environment(output):
    # Do not inherit credentials, NODE_OPTIONS, daemon overrides, shell startup
    # hooks, or the user's osstack state. These are intentional tool settings;
    # the parent HOME/CODEX_HOME values are neither read nor modified.
    output = Path(output).resolve()
    paths = {
        name: output / dirname
        for name, dirname in {
            "OSSTACK_HOME": "osstack-state",
            "XDG_CONFIG_HOME": "config",
            "XDG_CACHE_HOME": "cache",
            "TMPDIR": "tmp",
            "GH_CONFIG_DIR": "gh-config",
        }.items()
    }
    for path in paths.values():
        path.mkdir(mode=0o700, exist_ok=True)
    return {
        "PATH": "/opt/homebrew/bin:/usr/local/bin:" + os.defpath,
        "LANG": "C.UTF-8",
        **{name: str(path) for name, path in paths.items()},
    }


def _command(args, *, cwd, output, name, timeout):
    """Bound and reap our own process group; retain both output streams."""
    with (
        (output / (name + ".stdout")).open("wb") as out,
        (output / (name + ".stderr")).open("wb") as err,
    ):
        process = subprocess.Popen(
            args,
            cwd=cwd,
            stdout=out,
            stderr=err,
            stdin=subprocess.DEVNULL,
            env=diagnostic_environment(output),
            start_new_session=True,
        )
        try:
            code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass
            finally:
                # A promptly exiting leader can leave a TERM-ignoring child.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=3)
            return {"exit_code": process.returncode, "timed_out": True}
    return {"exit_code": code, "timed_out": False}


def run_probe(*, osstack, output, scenario):
    if scenario not in SCENARIOS:
        raise ValueError("unknown fixed scenario")
    osstack = Path(osstack)
    if not osstack.is_absolute() or not osstack.is_file():
        raise ValueError("absolute installed osstack executable required")
    output = Path(output).absolute()
    output.mkdir(mode=0o700, parents=False, exist_ok=False)
    workspace = output / "workspace"
    workspace.mkdir()
    report = dict(
        schema="ostag-loop-probe/1",
        scenario=scenario,
        model_invoked=None,
        automatic_writes_allowed=False,
        repository_mutation_audited=False,
        result="incomplete",
    )
    try:
        version = _command(
            [str(osstack), "--version"], cwd=workspace, output=output, name="version", timeout=10
        )
        if (
            version["exit_code"] != 0
            or (output / "version.stdout").read_text().strip() != "osstack 0.19.0"
        ):
            raise ValueError("unreviewed osstack version")
        queue = workspace / "tasks"
        queue.mkdir()
        old = queue / "old.json"
        old.write_text(
            json.dumps(
                dict(
                    task_id="old-canary",
                    prompt="Must never execute",
                    capability="historical.test",
                    priority=100,
                )
            )
        )
        old_hash = hashlib.sha256(old.read_bytes()).hexdigest()
        cap = "ostag.fixed.single_attempt_probe"
        policy = output / "policy.json"
        policy.write_text(
            json.dumps(
                dict(
                    default_authorization="blocked",
                    auto_execute_capabilities=[cap],
                    max_auto_priority=90,
                )
            )
        )
        enqueue = _command(
            [
                str(osstack),
                "loop",
                "enqueue",
                "--workspace",
                str(workspace),
                "Fixed synthetic probe only",
                "--capability",
                cap,
                "--priority",
                "80",
            ],
            cwd=workspace,
            output=output,
            name="enqueue",
            timeout=10,
        )
        if enqueue["exit_code"] != 0:
            raise ValueError("enqueue failed")
        task_files = [p for p in queue.glob("*.json") if p != old]
        if len(task_files) != 1:
            raise ValueError("queue is not a single attempt")
        task_id = json.loads(task_files[0].read_text())["task_id"]
        report["task_id"] = task_id
        script = output / "fixed_stage.py"
        script.write_text(
            "import hashlib,json,pathlib,sys,time\n"
            f"sys.path.insert(0, {str(Path(__file__).resolve().parent)!r})\n"
            "from runner_loop_probe import validate_verification\n"
            f"scenario={scenario!r}\n"
            'p=pathlib.Path("candidate.txt")\n'
            'if sys.argv[1]=="build":\n'
            ' if scenario=="timeout": time.sleep(30)\n'
            ' if scenario=="builder_failure": sys.exit(9)\n'
            ' p.write_text("fixed-synthetic-candidate")\n'
            "else:\n"
            ' if scenario=="verifier_failure": sys.exit(8)\n'
            " digest=hashlib.sha256(p.read_bytes()).hexdigest()\n"
            ' receipt=dict(candidate_sha256=digest,model="wrong" if scenario=="wrong_model" else "claude-opus-5",'
            'tests=[] if scenario=="empty_verifier" else [dict(name="fixed fixture",passed=1,failed=0,exit_code=0)],'
            'verdict="approved",blockers=[])\n'
            ' pathlib.Path("synthetic-verifier.json").write_text(json.dumps(receipt))\n'
            ' validate_verification(receipt,candidate_sha256=digest,expected_model="claude-opus-5")\n'
        )
        result = _command(
            [
                str(osstack),
                "loop",
                "run",
                "--workspace",
                str(workspace),
                "--source",
                "task_queue",
                "--discovery-policy",
                str(policy),
                "--execute",
                shlex.join([sys.executable, str(script), "build"]),
                "--verify",
                shlex.join([sys.executable, str(script), "verify"]),
                "--max-iter",
                "1",
                "--cycles",
                "1",
                "--command-timeout-ms",
                "2000",
                "--state",
                str(output / "LOOP-STATE.json"),
                "--markdown",
                str(output / "LOOP-STATE.md"),
                "--no-lesson-drafts",
            ],
            cwd=workspace,
            output=output,
            name="loop",
            timeout=20,
        )
        report["process"] = result
        if result["timed_out"]:
            raise ValueError("native loop exceeded observation budget")
        state = json.loads((output / "LOOP-STATE.json").read_text())
        validate_native_state(state, task_id=task_id, scenario=scenario)
        task = state["completed_tasks"][0]
        if (
            task.get("agent_id") != "local-command"
            or task.get("verifier_agent_id") != "local-verifier"
        ):
            raise ValueError("unexpected model-backed adapter")
        report["model_invoked"] = False  # Native records + fixed fixture commands.
        report["environment_keys"] = sorted(diagnostic_environment(output))
        report["single_attempt_verified"] = True
        report["old_queue_unchanged"] = hashlib.sha256(old.read_bytes()).hexdigest() == old_hash
        if not report["old_queue_unchanged"]:
            raise ValueError("historical queue changed")
        report["native_exit_matches_scenario"] = result["exit_code"] == (
            0 if scenario == "success" else 2
        )
        if not report["native_exit_matches_scenario"]:
            raise ValueError("unexpected native outcome")
        report["result"] = "synthetic_diagnostic_passed"
    except Exception as error:
        report["error_type"] = type(error).__name__
        raise
    finally:
        (output / "receipt.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--osstack", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--scenario", choices=SCENARIOS, required=True)
    args = parser.parse_args()
    run_probe(osstack=args.osstack, output=args.output, scenario=args.scenario)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
