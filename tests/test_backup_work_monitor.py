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


def test_backup_and_restore_never_follow_destination_symlink(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite3"
    backup = tmp_path / "backup.sqlite3"
    victim = tmp_path / "victim.txt"
    link = tmp_path / "link.sqlite3"
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE proof(value TEXT)")
    victim.write_text("do-not-touch", encoding="utf-8")
    link.symlink_to(victim)

    for mode, input_path in (("backup", source), ("restore", backup)):
        if mode == "restore":
            subprocess.run(
                ["scripts/backup_work_monitor.sh", "backup", str(source), str(backup)],
                check=True,
            )
        result = subprocess.run(
            ["scripts/backup_work_monitor.sh", mode, str(input_path), str(link), "--force"],
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0
        assert link.is_symlink()
        assert victim.read_text(encoding="utf-8") == "do-not-touch"


def test_backup_refuses_existing_regular_file_even_with_force(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite3"
    target = tmp_path / "backup.sqlite3"
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE proof(value TEXT)")
    target.write_text("preserve", encoding="utf-8")

    refused = subprocess.run(
        ["scripts/backup_work_monitor.sh", "backup", str(source), str(target)]
    )
    assert refused.returncode != 0
    assert target.read_text(encoding="utf-8") == "preserve"

    forced = subprocess.run(
        ["scripts/backup_work_monitor.sh", "backup", str(source), str(target), "--force"],
    )
    assert forced.returncode != 0
    assert target.read_text(encoding="utf-8") == "preserve"


def _embedded(monkeypatch, mode, source, destination, force=''):
    import sys
    code = Path('scripts/backup_work_monitor.sh').read_text().split("<<'PY'\n", 1)[1].rsplit('\nPY', 1)[0]
    monkeypatch.setattr(sys, 'argv', ['backup', mode, str(source), str(destination), force])
    exec(compile(code, 'backup_work_monitor_embedded', 'exec'), {'__name__': '__main__'})


def test_force_restore_refuses_existing_live_wal_database(tmp_path, monkeypatch):
    import pytest
    source, target = tmp_path / 'backup.db', tmp_path / 'live.db'
    with sqlite3.connect(source) as db:
        db.execute('CREATE TABLE proof(value)')
    live = sqlite3.connect(target)
    try:
        live.execute('PRAGMA journal_mode=WAL')
        live.execute('CREATE TABLE recent(value)')
        live.execute("INSERT INTO recent VALUES ('keep-new-data')")
        live.commit()
        before = {p.name: p.read_bytes() for p in tmp_path.glob('live.db*')}
        with pytest.raises(SystemExit):
            _embedded(monkeypatch, 'restore', source, target, '--force')
        assert before == {p.name: p.read_bytes() for p in tmp_path.glob('live.db*')}
        assert live.execute('SELECT value FROM recent').fetchone() == ('keep-new-data',)
    finally:
        live.close()


def test_publication_does_not_replace_concurrent_target(tmp_path, monkeypatch):
    import os
    import pytest
    source, target = tmp_path / 'source.db', tmp_path / 'destination.db'
    with sqlite3.connect(source) as db:
        db.execute('CREATE TABLE proof(value)')
    fired = []
    for name in ('replace', 'link'):
        real = getattr(os, name)
        def intercept(*args, _real=real, **kwargs):
            if not fired:
                target.write_bytes(b'concurrent-writer')
                fired.append(True)
            return _real(*args, **kwargs)
        monkeypatch.setattr(os, name, intercept)
    with pytest.raises((SystemExit, FileExistsError)):
        _embedded(monkeypatch, 'backup', source, target)
    assert fired and target.read_bytes() == b'concurrent-writer'


def test_sqlite_uri_special_characters_are_literal(tmp_path, monkeypatch):
    source, target = tmp_path / 'source?#%.db', tmp_path / 'copy?#%.db'
    with sqlite3.connect(source) as db:
        db.execute('CREATE TABLE proof(value)')
        db.execute("INSERT INTO proof VALUES ('correct-file')")
    _embedded(monkeypatch, 'backup', source, target)
    with sqlite3.connect(target) as db:
        assert db.execute('SELECT value FROM proof').fetchone() == ('correct-file',)


def test_restore_refuses_orphaned_sidecars(tmp_path, monkeypatch):
    import pytest
    source, target = tmp_path / 'backup.db', tmp_path / 'new.db'
    with sqlite3.connect(source) as db:
        db.execute('CREATE TABLE proof(value)')
    wal = Path(str(target) + '-wal')
    wal.write_bytes(b'preserve')
    with pytest.raises(SystemExit, match='sidecars'):
        _embedded(monkeypatch, 'restore', source, target)
    assert not target.exists() and wal.read_bytes() == b'preserve'


def test_wal_copy_closes_all_connections_before_publication(tmp_path, monkeypatch):
    import os
    source, target = tmp_path / 'wal-source.db', tmp_path / 'copy.db'
    live = sqlite3.connect(source)
    live.execute('PRAGMA journal_mode=WAL')
    live.execute('CREATE TABLE proof(value)')
    live.execute("INSERT INTO proof VALUES ('committed-in-wal')")
    live.commit()
    real_connect, real_link = sqlite3.connect, os.link
    connections = []
    class Tracked(sqlite3.Connection):
        closed = False
        def close(self):
            super().close()
            self.closed = True
    def connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs, factory=Tracked)
        connections.append(conn)
        return conn
    def publish(*args, **kwargs):
        assert len(connections) == 3 and all(c.closed for c in connections)
        return real_link(*args, **kwargs)
    monkeypatch.setattr(sqlite3, 'connect', connect)
    monkeypatch.setattr(os, 'link', publish)
    try:
        _embedded(monkeypatch, 'backup', source, target)
        with real_connect(target) as db:
            assert db.execute('SELECT value FROM proof').fetchone() == ('committed-in-wal',)
        assert not any('.tmp.' in p.name for p in tmp_path.iterdir())
    finally:
        for conn in connections:
            conn.close()
        live.close()
