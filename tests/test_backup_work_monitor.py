from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path


def test_backup_and_restore_are_verified_atomic_and_do_not_modify_source(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite3"
    backup = tmp_path / "backup.sqlite3"
    restored = tmp_path / "restored.sqlite3"
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE proof(value TEXT)")
        connection.execute("INSERT INTO proof VALUES ('source-intact')")
    before = source.read_bytes()

    subprocess.run(
        ["scripts/backup_work_monitor.sh", "backup", str(source), str(backup)],
        check=True,
    )
    assert source.read_bytes() == before
    assert backup.stat().st_mode & 0o777 == 0o600
    assert not list(tmp_path.glob("*.tmp.*"))

    subprocess.run(
        ["scripts/backup_work_monitor.sh", "restore", str(backup), str(restored)],
        check=True,
    )
    with sqlite3.connect(restored) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("SELECT value FROM proof").fetchone() == ("source-intact",)
    assert backup.read_bytes() != b""
    assert restored.stat().st_mode & 0o777 == 0o600


def test_restore_refuses_to_overwrite_existing_target(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite3"
    backup = tmp_path / "backup.sqlite3"
    target = tmp_path / "target.sqlite3"
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE proof(value TEXT)")
    target.write_text("preserve", encoding="utf-8")
    subprocess.run(
        ["scripts/backup_work_monitor.sh", "backup", str(source), str(backup)], check=True
    )

    result = subprocess.run(
        ["scripts/backup_work_monitor.sh", "restore", str(backup), str(target)],
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert target.read_text(encoding="utf-8") == "preserve"
