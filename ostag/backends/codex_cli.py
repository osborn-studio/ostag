from __future__ import annotations

import asyncio

from ostag.config import Settings


class CodexCLIBackend:
    def __init__(self, config: Settings):
        self.config = config

    async def run(self, prompt: str) -> str:
        cmd = build_codex_command(self.config, prompt)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=self.config.agent_timeout_seconds
            )
        except TimeoutError:
            proc.kill()
            await proc.communicate()
            return f"OS Tag backend timed out after {self.config.agent_timeout_seconds}s."

        out = stdout.decode("utf-8", errors="replace").strip()
        err = stderr.decode("utf-8", errors="replace").strip()
        if proc.returncode != 0:
            return f"Codex CLI backend failed with exit code {proc.returncode}:\n{(err or out)[-3000:]}"
        return out or "Codex CLI finished without output."


def build_codex_command(config: Settings, prompt: str) -> list[str]:
    cmd = [config.codex_bin, "exec", prompt]
    if config.codex_unsafe_mode:
        cmd.insert(2, "--dangerously-bypass-approvals-and-sandbox")
    return cmd
