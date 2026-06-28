from __future__ import annotations

import asyncio
import logging

from ostag.config import Settings
from ostag.mfs.helpers import build_mfs_search_command

logger = logging.getLogger(__name__)


class MFSClient:
    def __init__(self, config: Settings):
        self.config = config

    async def search(self, query: str) -> list[str]:
        if not self.config.mfs_enabled:
            return []

        results: list[str] = []
        for scope in self.config.mfs_allowed_scopes:
            cmd = build_mfs_search_command(self.config, query, scope)
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
            except Exception as exc:  # noqa: BLE001
                logger.warning("MFS search failed for scope %s: %s", scope, exc)
                continue
            if proc.returncode != 0:
                logger.warning("MFS search failed: %s", stderr.decode(errors="replace")[:1000])
                continue
            text = stdout.decode("utf-8", errors="replace").strip()
            if text:
                results.append(text)
        return results[: self.config.mfs_max_results]
