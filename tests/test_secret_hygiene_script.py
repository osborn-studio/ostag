from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


def test_secret_hygiene_reports_are_atomic_and_self_scan_clean_twice(tmp_path: Path) -> None:
    env = os.environ | {"OSTAG_SECRET_HYGIENE_REPORT_DIR": str(tmp_path)}

    for _ in range(2):
        subprocess.run(["scripts/secret_hygiene_check.sh"], check=True, env=env)
        report = json.loads((tmp_path / "secret-hygiene.json").read_text(encoding="utf-8"))
        assert report["status"] == "pass"
        assert report["match_count"] == 0
        assert not list(tmp_path.glob("*.tmp.*"))

    combined = (tmp_path / "secret-hygiene.json").read_text() + (
        tmp_path / "secret-hygiene.md"
    ).read_text()
    assert "Potential secret-like values were found:" not in combined
    assert "current/" not in combined and "bundle/" not in combined
