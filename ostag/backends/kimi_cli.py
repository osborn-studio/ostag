from __future__ import annotations

import re

from ostag.backends.executor_runtime import ExecutorRuntime, executor_profiles
from ostag.config import Settings


_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


class KimiCLIBackend:
    def __init__(self, config: Settings):
        self.config = config

    async def run(self, prompt: str) -> str:
        profile = executor_profiles(self.config)["kimi_cli"]
        return await ExecutorRuntime(
            profile, max_output_bytes=self.config.executor_max_output_bytes
        ).run(prompt)


def build_kimi_command(
    config: Settings, prompt: str, *, resume_session_id: str | None = None
) -> list[str]:
    command = [config.kimi_bin]
    if resume_session_id is not None:
        if not _SESSION_ID_RE.fullmatch(resume_session_id):
            raise ValueError("Kimi session id is invalid")
        command.extend(["--session", resume_session_id])
    command.extend(["--prompt", prompt, "--output-format", "text"])
    return command
