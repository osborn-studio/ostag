"""T02 diagnostic bootstrap, NOT an executor, sandbox, or authorization gate.

The negative control touches only files it creates in one temporary directory.
It intentionally demonstrates failed isolation. No worker can be enabled here.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

EXPECTED = {
    "cwd": "allowed",
    "work_read": "allowed",
    "work_write": "allowed",
    "outside_write": "denied",
    "symlink_write": "denied",
    "private_read": "denied",
    "protected_write": "denied",
    "child_write": "denied",
}
SURFACES = ("direct", "ostag_codex_backend", "ostag_claude_backend", "osstack_codex_adapter")
PROMPT = "fixed synthetic isolation negative control"
# Operator-only installed-source diagnostic. No discovery via PATH, profiles,
# availability checks, native queue, or fallback to a real model executable.
OSSTACK_ROOT = Path("/opt/homebrew/lib/node_modules/@osstack/cli")
OSSTACK_NODE = Path("/opt/homebrew/bin/node")
OSSTACK_MODULE = "node_modules/@osstack/agents/dist/adapters/codex-cli.js"
OSSTACK_PINS = {
    "package.json": "26b2150909379c4e6f29f9a6674cc054fa446d4e1464efcf3aeb993f49bb858e",
    "node_modules/@osstack/agents/package.json":
        "448387f48c168471b1ea423c47fef671e312070b2e7ea7f460b877adab86f99d",
    OSSTACK_MODULE: "cd7fd5d9a5b4e2057e65d442c13c864507d8c8eac93d4aadc6d04a9453d134d9",
    "node_modules/@osstack/agents/dist/base.js":
        "633cafb82029b4481cb2da917aa31249aa690d0828e2761f80eeb6b4d83a787c",
}


def assess_observations(observations: object) -> dict:
    """Check diagnostic shape, never promote self-reported data to authority."""
    if type(observations) is not dict or observations.keys() != EXPECTED.keys():
        raise ValueError("exact required probe set is missing or has extra fields")
    if any(
        type(value) is not str or value not in {"allowed", "denied", "error"}
        for value in observations.values()
    ):
        raise ValueError("invalid observation outcome")
    mismatches = [name for name, expected in EXPECTED.items() if observations[name] != expected]
    return {
        "schema": "ostag-runner-diagnostic/1",
        "observations": dict(observations),
        "mismatches": mismatches,
        "observations_match": not mismatches,
        "network": "not_tested",
        "evidence_source": "untrusted_report_shape_only",
        "parent_observations": None,
        "parent_verified_reads": False,
        "automatic_writes_allowed": False,
        "t02_complete": False,
    }


def classify_descendant_exit(code: int) -> str:
    if type(code) is not int:
        raise ValueError("descendant exit code must be an integer")
    return {0: "allowed", 13: "denied"}.get(code, "error")


_CHILD = r"""
import json, os, pathlib, subprocess, sys
root = pathlib.Path(sys.argv[1])
work = root / "work"
results = {}
def observe(name, operation):
    try:
        outcome = operation()
        results[name] = "error" if outcome is False else "allowed"
    except PermissionError:
        results[name] = "denied"
    except OSError:
        results[name] = "error"
observe("cwd", lambda: pathlib.Path.cwd() == work)
observe("work_read", lambda: (work / "input").read_text() == "synthetic-input")
observe("work_write", lambda: (work / "output").write_text("synthetic-output"))
observe("outside_write", lambda: (root / "outside" / "direct").write_text("changed"))
observe("symlink_write", lambda: (work / "escape" / "via-link").write_text("changed"))
observe("private_read", lambda: (root / "outside" / "private").read_text())
observe("protected_write", lambda: (root / "trusted" / "verifier").write_text("changed"))
child = subprocess.run(
    [sys.executable, "-I", "-B", "-c",
     "import pathlib,sys\ntry:\n pathlib.Path(sys.argv[1]).write_text('changed')"
     "\nexcept PermissionError:\n sys.exit(13)",
     str(root / "outside" / "descendant")],
    capture_output=True, timeout=3,
)
print(json.dumps({"observations": results, "descendant_exit_code": child.returncode}))
"""

_BUSINESS_BACKEND = r"""
import asyncio, sys
sys.path.insert(0, sys.argv[1])
from ostag.config import Settings
from ostag.backends.codex_cli import CodexCLIBackend
from ostag.backends.claude_cli import ClaudeCLIBackend
config = Settings(codex_bin=sys.argv[3], claude_bin=sys.argv[3], agent_timeout_seconds=5)
backend = CodexCLIBackend(config) if sys.argv[2] == "ostag_codex_backend" else ClaudeCLIBackend(config)
print(asyncio.run(backend.run(sys.argv[4])))
"""

_OSSTACK_ADAPTER = r"""
const {CodexCliAdapter} = await import(process.argv[1]);
const adapter = new CodexCliAdapter({bin: process.argv[2], cwd: process.argv[3], sandbox: "read-only"});
const controller = new AbortController();
const timer = setTimeout(() => controller.abort(), 5000);
const messages = [];
let completed = false;
try {
  for await (const event of adapter.invoke({
    run_id: "synthetic-run", task_id: "synthetic-task", session_id: "synthetic-session",
    capability_id: "codex-cli.code.test", prompt: process.argv[4],
    context: {repo_path: process.argv[3]},
  }, controller.signal)) {
    if (event.type === "error") throw new Error("adapter error");
    if (event.type === "token.delta") messages.push(event.payload.delta);
    if (event.type === "done") completed = event.payload.status === "completed";
  }
  if (!completed || messages.length !== 1) throw new Error("missing fixed probe output");
  console.log(messages[0]);
} finally { clearTimeout(timer); }
"""


def verify_osstack_installation() -> dict:
    """Recognize reviewed 0.19.0 entry files, not a transitive supply-chain gate."""
    for name, expected in OSSTACK_PINS.items():
        if hashlib.sha256((OSSTACK_ROOT / name).read_bytes()).hexdigest() != expected:
            raise ValueError("unsupported osstack source revision")
    if not OSSTACK_NODE.is_file() or not os.access(OSSTACK_NODE, os.X_OK):
        raise ValueError("unsupported osstack Node runtime")
    return {"version": "0.19.0", "entry_source_hashes": dict(OSSTACK_PINS),
            "transitive_dependencies_verified": False}


def _fake_cli(surface: str, root: Path) -> Path:
    if any(character.isspace() for character in sys.executable):
        raise ValueError("unsupported interpreter shebang")
    fake = root / "fake-cli"
    # Capture fixed child JSON, then emit either plain text or Codex JSONL.
    # exec receives only this module's constant probe, never user/model input.
    fake.write_text(
        f"#!{sys.executable} -I\n"
        "import contextlib, io, json, pathlib, sys\nsys.dont_write_bytecode = True\n"
        f"pathlib.Path({str(root / 'invocation.json')!r}).write_text(json.dumps("
        "{'argv': sys.argv[1:], 'stdin': sys.stdin.read() if sys.argv[1:] == ['-p'] else ''}))\n"
        f"sys.argv = [{str(fake)!r}, {str(root)!r}]\n"
        "output = io.StringIO()\n"
        f"with contextlib.redirect_stdout(output):\n exec({_CHILD!r})\n"
        + (
            "print(json.dumps({'type': 'item.completed', 'item': {'id': 'fixed-probe', "
            "'type': 'agent_message', 'text': output.getvalue()}}))\n"
            if surface == "osstack_codex_adapter" else "print(output.getvalue())\n"
        )
    )
    fake.chmod(0o700)
    return fake


def _business_command(surface: str, root: Path) -> list[str]:
    """Use real backend code, but ONLY an internally generated fake executable.

    No load_settings/.env, real Codex/Claude, profile, or caller-supplied command.
    Python subprocess semantics: https://docs.python.org/3.13/library/subprocess.html
    """
    fake = _fake_cli(surface, root)
    if surface == "osstack_codex_adapter":
        return [str(OSSTACK_NODE), "--input-type=module", "-e", _OSSTACK_ADAPTER,
                (OSSTACK_ROOT / OSSTACK_MODULE).as_uri(), str(fake), str(root / "work"), PROMPT]
    return [sys.executable, "-I", "-B", "-c", _BUSINESS_BACKEND,
            str(Path(__file__).resolve().parents[2]), surface, str(fake), PROMPT]


def run_negative_control(*, surface: str = "direct") -> dict:
    """Exercise generated canaries with a real subprocess inheriting host policy.

    There is deliberately no caller-supplied root, shell command, credential,
    model, Docker startup, or positive-isolation mode in this bootstrap slice.
    """
    if type(surface) is not str or surface not in SURFACES:
        raise ValueError("unknown fixed diagnostic surface")
    installed = verify_osstack_installation() if surface == "osstack_codex_adapter" else None
    with tempfile.TemporaryDirectory(prefix="ostag-negative-control-") as directory:
        root = Path(directory).resolve()
        work = root / "work"
        for name in ("work", "outside", "trusted"):
            (root / name).mkdir()
        (work / "input").write_text("synthetic-input")
        (root / "outside" / "private").write_text("synthetic-private-value")
        (root / "trusted" / "verifier").write_text("synthetic-verifier")
        (work / "escape").symlink_to(root / "outside", target_is_directory=True)
        command = [sys.executable, "-I", "-B", "-c", _CHILD, str(root)]
        if surface != "direct":
            command = _business_command(surface, root)
        process = subprocess.run(
            command,
            cwd=work,
            env={"PATH": os.defpath},
            capture_output=True,
            text=True,
            timeout=8,
            check=True,
        )
        payload = json.loads(process.stdout)
        observations = payload["observations"]
        observations["child_write"] = classify_descendant_exit(payload["descendant_exit_code"])
        result = assess_observations(observations)
        if surface != "direct":
            invocation = json.loads((root / "invocation.json").read_text())
            expected = {
                "ostag_codex_backend": {"argv": ["exec", PROMPT], "stdin": ""},
                "ostag_claude_backend": {"argv": ["-p"], "stdin": PROMPT},
                "osstack_codex_adapter": {
                    "argv": ["exec", "--json", "--skip-git-repo-check", "-s", "read-only",
                             "-C", str(work), PROMPT], "stdin": "",
                },
            }[surface]
            if invocation != expected:
                raise ValueError("backend dispatch did not match the fixed probe")
        # No model by construction: fixed child or generated absolute fake CLI,
        # not a runtime measurement of arbitrary backend/model behavior.
        result.update(surface=surface, dispatch_verified=None if surface == "direct" else True,
                      model_invoked=False, cli_sandbox_behavior="not_tested")
        if installed is not None:
            result["installed_adapter"] = installed
        result["environment"] = "inherited_host_policy"
        result["sandbox_applied_by_probe"] = False
        parent = {
            "work_output_written": (work / "output").read_text() == "synthetic-output",
            "outside_direct_written": (root / "outside" / "direct").is_file(),
            "symlink_target_written": (root / "outside" / "via-link").is_file(),
            "descendant_written": (root / "outside" / "descendant").is_file(),
            "verifier_changed": (root / "trusted" / "verifier").read_text() != "synthetic-verifier",
            "private_canary_changed": (root / "outside" / "private").read_text()
            != "synthetic-private-value",
        }
        result["evidence_source"] = "fixed_child_plus_parent_write_observations"
        result["parent_observations"] = parent
        names = {
            "work_write": "work_output_written",
            "outside_write": "outside_direct_written",
            "symlink_write": "symlink_target_written",
            "child_write": "descendant_written",
            "protected_write": "verifier_changed",
        }
        result["write_reports_consistent"] = all(
            observations[probe] != "error" and parent[field] == (observations[probe] == "allowed")
            for probe, field in names.items()
        )
        result["host_canaries_intact"] = (
            not parent["verifier_changed"]
            and not parent["private_canary_changed"]
            and {p.name for p in (root / "outside").iterdir()} == {"private"}
        )
        return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--negative-control", action="store_true")
    parser.add_argument("--surface", choices=SURFACES, default="direct")
    args = parser.parse_args()
    if not args.negative_control:
        parser.error("only --negative-control is implemented; no automatic writer is permitted")
    try:
        result = run_negative_control(surface=args.surface)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        print(json.dumps({"error": type(error).__name__, "automatic_writes_allowed": False}))
        return 2
    print(json.dumps(result, indent=2))
    return 1  # A deliberately failed isolation diagnostic is NEVER an admission success.


if __name__ == "__main__":
    raise SystemExit(main())
