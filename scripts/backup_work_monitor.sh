#!/usr/bin/env bash
set -euo pipefail

if [[ ( $# -ne 3 && $# -ne 4 ) || ( "$1" != "backup" && "$1" != "restore" ) || \
      ( $# -eq 4 && "$4" != "--force" ) ]]; then
  echo "usage: $0 {backup|restore} SOURCE.sqlite3 DESTINATION.sqlite3 [--force]" >&2
  exit 64
fi

mode="$1"
source_db="$2"
destination_db="$3"
force="${4:-}"

uv run python - "$mode" "$source_db" "$destination_db" "$force" <<'PY'
from __future__ import annotations

import os
import sqlite3
import stat
import sys
import tempfile
from pathlib import Path

mode, source_text, destination_text, force_flag = sys.argv[1:]
source = Path(source_text).resolve()
raw_destination = Path(destination_text).absolute()
destination_parent = raw_destination.parent.resolve(strict=True)
destination = destination_parent / raw_destination.name
force = force_flag == "--force"
if source == destination:
    raise SystemExit("source and destination must differ")
if not source.is_file():
    raise SystemExit("source database does not exist")
initial_target = None
try:
    target_stat = os.lstat(destination)
except FileNotFoundError:
    pass
else:
    if not stat.S_ISREG(target_stat.st_mode):
        raise SystemExit("destination must not be a symlink or non-regular file")
    if not force:
        raise SystemExit("destination exists; pass --force to replace a regular file")
    initial_target = (target_stat.st_dev, target_stat.st_ino)

fd, temporary_text = tempfile.mkstemp(
    prefix=f".{destination.name}.tmp.", dir=destination.parent
)
os.close(fd)
temporary = Path(temporary_text)
try:
    with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as source_connection:
        integrity = source_connection.execute("PRAGMA integrity_check").fetchone()
        if integrity != ("ok",):
            raise SystemExit("source database integrity check failed")
        with sqlite3.connect(temporary) as destination_connection:
            source_connection.backup(destination_connection)
            destination_connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        with sqlite3.connect(f"file:{temporary}?mode=ro", uri=True) as verification:
            if verification.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                raise SystemExit("copied database integrity check failed")
    os.chmod(temporary, 0o600)
    with temporary.open("rb") as copied:
        os.fsync(copied.fileno())
    try:
        final_stat = os.lstat(destination)
        final_target = (final_stat.st_dev, final_stat.st_ino)
        if not stat.S_ISREG(final_stat.st_mode) or final_target != initial_target:
            raise SystemExit("destination changed during backup; refusing replacement")
    except FileNotFoundError:
        if initial_target is not None:
            raise SystemExit("destination changed during backup; refusing replacement")
    directory_fd = os.open(destination_parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.replace(
            temporary.name,
            destination.name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
finally:
    temporary.unlink(missing_ok=True)

print(f"{mode} verified: {destination}")
PY
