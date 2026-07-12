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


def test_alidocs_token_path_is_detected_without_flagging_public_urls(tmp_path: Path) -> None:
    fixture = tmp_path / "fixture"
    reports = tmp_path / "reports"
    fixture.mkdir()
    fixture_file = fixture / "links.md"
    synthetic_segment = "synthetic_document_identifier_0001"
    fixture_file.write_text(
        f"https://alidocs.dingtalk.com/i/nodes/{synthetic_segment}\n", encoding="utf-8"
    )
    env = os.environ | {
        "OSTAG_SECRET_HYGIENE_REPORT_DIR": str(reports),
        "OSTAG_SECRET_HYGIENE_EXTRA_SCAN_ROOT": str(fixture),
    }

    detected = subprocess.run(["scripts/secret_hygiene_check.sh"], env=env)

    assert detected.returncode == 2
    failed = json.loads((reports / "secret-hygiene.json").read_text(encoding="utf-8"))
    assert failed["status"] == "fail" and failed["match_count"] == 1
    assert synthetic_segment not in (reports / "secret-hygiene.md").read_text(encoding="utf-8")

    fixture_file.write_text(
        "https://alidocs.dingtalk.com/\n"
        "https://alidocs.dingtalk.com/i/nodes/<document_token>\n"
        "https://www.alibabacloud.com/help/en/public-doc\n",
        encoding="utf-8",
    )
    clean = subprocess.run(["scripts/secret_hygiene_check.sh"], env=env)

    assert clean.returncode == 0
    passed = json.loads((reports / "secret-hygiene.json").read_text(encoding="utf-8"))
    assert passed["status"] == "pass" and passed["match_count"] == 0


def test_scanner_catches_quoted_json_and_shell_values_with_punctuation(tmp_path: Path) -> None:
    fixture = tmp_path / "fixture"
    reports = tmp_path / "reports"
    fixture.mkdir()
    (fixture / "config.json").write_text(
        '{"service_token":"alpha.Beta+123@example.invalid/path"}', encoding="utf-8"
    )
    (fixture / "runtime.env").write_text(
        "DB_PASSWORD='Correct/Horse+Battery:9000'\n"
        "SAFE_TOKEN=<token_from_secret_manager>\n",
        encoding="utf-8",
    )
    env = os.environ | {
        "OSTAG_SECRET_HYGIENE_REPORT_DIR": str(reports),
        "OSTAG_SECRET_HYGIENE_EXTRA_SCAN_ROOT": str(fixture),
    }

    result = subprocess.run(["scripts/secret_hygiene_check.sh"], env=env)

    assert result.returncode == 2
    report = json.loads((reports / "secret-hygiene.json").read_text(encoding="utf-8"))
    assert report["match_count"] == 2
    assert report["category_counts"] == {"credential_assignment": 2}
