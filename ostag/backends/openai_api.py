from __future__ import annotations

from ostag.config import Settings


class OpenAIAPIBackend:
    def __init__(self, config: Settings):
        self.config = config

    async def run(self, prompt: str) -> str:
        try:
            from openai import AsyncOpenAI
        except ImportError:
            return "OpenAI SDK is not installed. Run `uv sync --extra openai` or choose another backend."

        client = AsyncOpenAI()
        response = await client.responses.create(
            model=self.config.openai_model,
            input=prompt,
        )
        return response.output_text.strip() or "OpenAI backend finished without output."
