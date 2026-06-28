from __future__ import annotations

from pathlib import Path

import pytest

from ostag.config import load_settings
from ostag.mfs.client import MFSClient


def _write_fake_mfs(path: Path, body: str) -> Path:
    path.write_text("#!/usr/bin/env bash\nset -euo pipefail\n" + body, encoding="utf-8")
    path.chmod(0o755)
    return path


@pytest.mark.asyncio
async def test_mfs_client_search_runs_allowed_cli_scope(tmp_path: Path) -> None:
    fake_mfs = _write_fake_mfs(
        tmp_path / "mfs",
        """
if [[ "$1" != "search" ]]; then exit 2; fi
echo "result:$2:$3:$5"
""",
    )
    config = load_settings(
        None,
        audit_log=tmp_path / "audit.log",
        mfs_enabled=True,
        mfs_bin=str(fake_mfs),
        mfs_allowed_scopes=("file://./context",),
        mfs_max_results=3,
    )

    assert await MFSClient(config).search("roadmap") == ["result:roadmap:file://./context:3"]


@pytest.mark.asyncio
async def test_mfs_client_ignores_failed_scope_and_keeps_success(tmp_path: Path) -> None:
    fake_mfs = _write_fake_mfs(
        tmp_path / "mfs",
        """
if [[ "$3" == *"bad"* ]]; then
  echo "scope failed" >&2
  exit 7
fi
echo "ok:$3"
""",
    )
    config = load_settings(
        None,
        audit_log=tmp_path / "audit.log",
        mfs_enabled=True,
        mfs_bin=str(fake_mfs),
        mfs_allowed_scopes=("file://./bad", "file://./good"),
        mfs_max_results=5,
    )

    assert await MFSClient(config).search("query") == ["ok:file://./good"]


@pytest.mark.asyncio
async def test_mfs_client_limits_results_to_configured_max(tmp_path: Path) -> None:
    fake_mfs = _write_fake_mfs(
        tmp_path / "mfs",
        """
echo "scope:$3"
""",
    )
    config = load_settings(
        None,
        audit_log=tmp_path / "audit.log",
        mfs_enabled=True,
        mfs_bin=str(fake_mfs),
        mfs_allowed_scopes=("file://./one", "file://./two", "file://./three"),
        mfs_max_results=2,
    )

    assert await MFSClient(config).search("query") == ["scope:file://./one", "scope:file://./two"]
