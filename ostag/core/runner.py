from __future__ import annotations

from ostag.backends.base import Backend, BackendError
from ostag.backends.factory import create_backend
from ostag.config import Settings
from ostag.core.channel_brain import (
    ChannelBrainStore,
    extract_remember_fact,
    format_channel_brain,
    is_brain_status_request,
)
from ostag.core.context import trim_context
from ostag.core.event import NormalizedEvent
from ostag.core.logging import write_audit_log
from ostag.core.prompt import PromptContext, build_chat_prompt
from ostag.core.safety import check_message_length, sanitize_user_text
from ostag.mfs.client import MFSClient
from ostag.workflows.auto_base import AutoBaseWorkflow


class OSTagRunner:
    def __init__(
        self,
        config: Settings,
        backend: Backend | None = None,
        mfs_client: MFSClient | None = None,
        auto_base_workflow: AutoBaseWorkflow | None = None,
        channel_brain_store: ChannelBrainStore | None = None,
    ):
        self.config = config
        self.backend = backend or create_backend(config)
        self.mfs = mfs_client or MFSClient(config)
        self.auto_base = auto_base_workflow or AutoBaseWorkflow(config)
        self.channel_brain = channel_brain_store or ChannelBrainStore(config)

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

        remember_fact = extract_remember_fact(text)
        if remember_fact is not None:
            brain = self.channel_brain.remember_fact(event.platform, event.chat_id, remember_fact)
            reply = f"已记住这个群的新事实：{remember_fact}"
            write_audit_log(
                self.config.audit_log,
                event=event,
                backend="channel_brain",
                mfs_enabled=self.config.mfs_enabled,
                input_text=text,
                output_text=reply,
                status="ok",
                env=self.config.env,
            )
            return reply

        if is_brain_status_request(text):
            brain = self.channel_brain.load(event.platform, event.chat_id)
            reply = format_channel_brain(brain)
            write_audit_log(
                self.config.audit_log,
                event=event,
                backend="channel_brain",
                mfs_enabled=self.config.mfs_enabled,
                input_text=text,
                output_text=reply,
                status="ok",
                env=self.config.env,
            )
            return reply

        mfs_results = await self.mfs.search(text)
        context_text = trim_context(mfs_results, self.config)
        try:
            workflow_reply = await self.auto_base.handle(event)
        except Exception as exc:  # noqa: BLE001
            workflow_reply = f"自动入表未完成：{type(exc).__name__}: {exc}"
        if workflow_reply is not None:
            write_audit_log(
                self.config.audit_log,
                event=event,
                backend="auto_base",
                mfs_enabled=self.config.mfs_enabled,
                input_text=text,
                output_text=workflow_reply,
                status="ok" if not workflow_reply.startswith("自动入表未完成") else "error",
                env=self.config.env,
                error=None if not workflow_reply.startswith("自动入表未完成") else workflow_reply,
            )
            return workflow_reply

        prompt = build_chat_prompt(
            PromptContext(event=event, user_text=text, mfs_context=context_text)
        )
        try:
            reply = await self.backend.run(prompt)
            status = "ok"
            error = None
        except BackendError as exc:
            reply = "OS Tag 当前通用问答后端暂时不可用；自动入表等内置工作流仍可继续使用。"
            status = "error"
            error = str(exc)
        except Exception as exc:  # noqa: BLE001
            reply = "OS Tag 当前处理失败，我已经记录错误，请稍后再试。"
            status = "error"
            error = f"{type(exc).__name__}: {exc}"

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
