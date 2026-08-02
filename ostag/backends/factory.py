from __future__ import annotations

from ostag.backends.base import Backend
from ostag.backends.claude_cli import ClaudeCLIBackend
from ostag.backends.codex_cli import CodexCLIBackend
from ostag.backends.kimi_cli import KimiCLIBackend
from ostag.backends.mock import MockBackend
from ostag.backends.openai_api import OpenAIAPIBackend
from ostag.config import Settings


def create_backend(config: Settings) -> Backend:
    if config.backend == "mock":
        return MockBackend(config)
    if config.backend == "codex_cli":
        return CodexCLIBackend(config)
    if config.backend == "claude_cli":
        return ClaudeCLIBackend(config)
    if config.backend == "kimi_cli":
        return KimiCLIBackend(config)
    if config.backend == "openai_api":
        return OpenAIAPIBackend(config)
    raise ValueError(f"Unsupported backend: {config.backend}")
