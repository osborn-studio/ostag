from __future__ import annotations

from ostag.backends.base import Backend
from ostag.backends.factory import create_backend
from ostag.config import Settings
from ostag.core.context import trim_context
from ostag.core.event import NormalizedEvent
from ostag.core.logging import write_audit_log
from ostag.core.prompt import PromptContext, build_chat_prompt
from ostag.core.safety import check_message_length, sanitize_user_text
from ostag.mfs.client import MFSClient


class OSTagRunner:
    def __init__(
        self,
        config: Settings,
        backend: Backend | None = None,
        mfs_client: MFSClient | None = None,
    ):
        self.config = config
        self.backend = backend or create_backend(config)
        self.mfs = mfs_client or MFSClient(config)

    async def handle_event(self, event: NormalizedEvent) -> str:
        if not event.mention_bot:
            reply = ""
            write_audit_log(
                self.config.audit_log,
                event=event,
                backend=self.config.backend,
                mfs_enabled=self.config.mfs_enabled,
                input_text=event.text,
                output_text=reply,
                status="ignored",
                env=self.config.env,
            )
            return reply

        text = sanitize_user_text(event.text)
        length = check_message_length(text, self.config.max_input_chars)
        if not length.ok:
            write_audit_log(
                self.config.audit_log,
                event=event,
                backend=self.config.backend,
                mfs_enabled=self.config.mfs_enabled,
                input_text=text,
                output_text=length.reason or "",
                status="rejected",
                env=self.config.env,
                error=length.reason,
            )
            return f"OS Tag 已拒绝处理：{length.reason}"

        mfs_results = await self.mfs.search(text)
        context_text = trim_context(mfs_results, self.config)
        prompt = build_chat_prompt(
            PromptContext(event=event, user_text=text, mfs_context=context_text)
        )
        try:
            reply = await self.backend.run(prompt)
            status = "ok"
            error = None
        except Exception as exc:  # noqa: BLE001
            reply = f"OS Tag backend failed: {type(exc).__name__}: {exc}"
            status = "error"
            error = str(exc)

        write_audit_log(
            self.config.audit_log,
            event=event,
            backend=self.config.backend,
            mfs_enabled=self.config.mfs_enabled,
            input_text=text,
            output_text=reply,
            status=status,
            env=self.config.env,
            error=error,
        )
        return reply
