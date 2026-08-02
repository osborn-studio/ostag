from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import subprocess


def _fake_cli(tmp_path: Path, payload: dict[str, object], exit_code: int = 0) -> Path:
    path = tmp_path / "ostag-evidence-cli"
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        f"print(json.dumps({payload!r}))\n"
        f"raise SystemExit({exit_code})\n",
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def test_provider_live_smoke_records_missing_number_as_external_blocker(tmp_path: Path) -> None:
    output = tmp_path / "evidence.json"
    result = subprocess.run(
        ["scripts/provider_evidence_live_smoke.sh", "github", "", str(output)],
        text=True,
        capture_output=True,
        check=False,
    )
    payload = json.loads(output.read_text(encoding="utf-8"))

    assert result.returncode == 3
    assert payload["blocker_code"] == "missing_change_number"


def test_provider_live_smoke_validates_and_wraps_pass_evidence(tmp_path: Path) -> None:
    normalized = {
        "evidence": {"provider": "github", "completionState": "passed"},
        "payloadDigest": "a" * 64,
        "completionFacts": [],
    }
    cli = _fake_cli(tmp_path, normalized)
    env = os.environ.copy()
    env["OSTAG_CONNECTOR_CLI_BIN"] = str(cli)
    output = tmp_path / "evidence.json"

    result = subprocess.run(
        ["scripts/provider_evidence_live_smoke.sh", "github", "12", str(output)],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )
    payload = json.loads(output.read_text(encoding="utf-8"))

    assert result.returncode == 0
    assert payload["status"] == "ready"
    assert payload["result"] == normalized
