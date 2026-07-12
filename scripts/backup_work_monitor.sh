#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 3 || ( "$1" != "backup" && "$1" != "restore" ) ]]; then
  echo "usage: $0 {backup|restore} SOURCE.sqlite3 DESTINATION.sqlite3" >&2
  exit 64
fi

mode="$1"
source_db="$2"
destination_db="$3"

uv run python - "$mode" "$source_db" "$destination_db" <<'PY'
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from pathlib import Path

mode, source_text, destination_text = sys.argv[1:]
source = Path(source_text).resolve()
destination = Path(destination_text).resolve()
if source == destination:
    raise SystemExit("source and destination must differ")
if not source.is_file():
    raise SystemExit("source database does not exist")
if mode == "restore" and destination.exists():
    raise SystemExit("restore target already exists; refusing overwrite")
destination.parent.mkdir(parents=True, exist_ok=True)

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
    os.replace(temporary, destination)
    directory_fd = os.open(destination.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
finally:
    temporary.unlink(missing_ok=True)

print(f"{mode} verified: {destination}")
PY
