from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import subprocess


def _fake_cli(tmp_path: Path, payload: dict[str, object], exit_code: int) -> Path:
    executable = tmp_path / "ostag-test-cli"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        f"print(json.dumps({payload!r}))\n"
        f"raise SystemExit({exit_code})\n",
        encoding="utf-8",
    )
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    return executable


def _run(tmp_path: Path, payload: dict[str, object], exit_code: int) -> subprocess.CompletedProcess[str]:
    executable = _fake_cli(tmp_path, payload, exit_code)
    env = os.environ.copy()
    env["OSTAG_CONNECTOR_CLI_BIN"] = str(executable)
    output = tmp_path / "evidence.json"
    return subprocess.run(
        ["scripts/connector_live_smoke.sh", "feishu", str(output)],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )


def test_live_smoke_writes_atomic_pass_evidence(tmp_path: Path) -> None:
    payload = {
        "schema_version": 1,
        "platform": "feishu",
        "mode": "live",
        "status": "ready",
        "read_ready": True,
        "checks": [{"id": "live_provider", "status": "pass", "required": True}],
    }

    result = _run(tmp_path, payload, 0)
    evidence = json.loads((tmp_path / "evidence.json").read_text(encoding="utf-8"))

    assert result.returncode == 0
    assert evidence == payload


def test_live_smoke_preserves_structured_blocker_with_exit_three(tmp_path: Path) -> None:
    payload = {
        "schema_version": 1,
        "platform": "feishu",
        "mode": "live",
        "status": "blocked",
        "read_ready": False,
        "checks": [{"id": "credentials", "status": "fail", "required": True}],
    }

    result = _run(tmp_path, payload, 1)
    evidence = json.loads((tmp_path / "evidence.json").read_text(encoding="utf-8"))

    assert result.returncode == 3
    assert evidence["status"] == "blocked"


def test_live_smoke_rejects_malformed_or_wrong_platform_evidence(tmp_path: Path) -> None:
    payload = {
        "schema_version": 1,
        "platform": "github",
        "mode": "live",
        "status": "ready",
        "read_ready": True,
        "checks": [],
    }

    result = _run(tmp_path, payload, 0)

    assert result.returncode == 2
    assert (tmp_path / "evidence.json").exists() is False
