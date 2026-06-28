from __future__ import annotations

import asyncio

from ostag.config import Settings


class ClaudeCLIBackend:
    def __init__(self, config: Settings):
        self.config = config

    async def run(self, prompt: str) -> str:
        cmd = build_claude_command(self.config)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input=prompt.encode("utf-8")),
                timeout=self.config.agent_timeout_seconds,
            )
        except TimeoutError:
            proc.kill()
            await proc.communicate()
            return f"OS Tag backend timed out after {self.config.agent_timeout_seconds}s."

        out = stdout.decode("utf-8", errors="replace").strip()
        err = stderr.decode("utf-8", errors="replace").strip()
        if proc.returncode != 0:
            return f"Claude CLI backend failed with exit code {proc.returncode}:\n{(err or out)[-3000:]}"
        return out or "Claude CLI finished without output."


def build_claude_command(config: Settings) -> list[str]:
    cmd = [config.claude_bin, "-p"]
    if config.claude_unsafe_mode:
        cmd.append("--dangerously-skip-permissions")
    return cmd
