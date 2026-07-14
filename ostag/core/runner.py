from __future__ import annotations

from typing import Any

from ostag.backends.base import Backend, BackendError
from ostag.backends.factory import create_backend
from ostag.config import Settings
from ostag.core.ambient import is_ambient_status_request
from ostag.core.channel_brain import (
    ChannelBrainStore,
    extract_remember_fact,
    format_channel_brain,
    is_brain_status_request,
)
from ostag.core.context import trim_context
from ostag.core.event import NormalizedEvent
from ostag.core.logging import write_audit_log
from ostag.connectors.apply import build_apply_executors
from ostag.core.approvals import ApprovalService
from ostag.core.budget import BudgetService, utc_today
from ostag.core.prompt import PromptContext, build_chat_prompt
from ostag.core.receipts import parse_receipt_command
from ostag.core.run_ledger import RunLedger
from ostag.core.safety import check_message_length, sanitize_user_text
from ostag.core.schemas import ContextPacket, LedgerEvent, Run, SchemaError, new_id
from ostag.mfs.client import MFSClient
from ostag.workflows.auto_base import AutoBaseWorkflow
from ostag.work_monitor.service import WorkMonitorService, parse_monitor_command


class OSTagRunner:
    def __init__(
        self,
        config: Settings,
        backend: Backend | None = None,
        mfs_client: MFSClient | None = None,
        auto_base_workflow: AutoBaseWorkflow | None = None,
        channel_brain_store: ChannelBrainStore | None = None,
        run_ledger: RunLedger | None = None,
        approvals: ApprovalService | None = None,
        work_monitor: WorkMonitorService | None = None,
        copilot_relay: Any | None = None,
    ):
        self.config = config
        self.backend = backend or create_backend(config)
        self.mfs = mfs_client or MFSClient(config)
        self.auto_base = auto_base_workflow or AutoBaseWorkflow(config)
        self.channel_brain = channel_brain_store or ChannelBrainStore(config)
        self.run_ledger = run_ledger or RunLedger(config.run_ledger_path)
        executors = (
            build_apply_executors(self.auto_base) if hasattr(self.auto_base, "requirements") else {}
        )
        self.approvals = approvals or ApprovalService(config, self.run_ledger, executors)
        self.budget = BudgetService(config, self.run_ledger)
        self.work_monitor = work_monitor
        self.copilot_relay = copilot_relay
        # Run id of the most recent handled event; set by handle_event so CLI
        # callers can report which run their submission created.
        self.last_run_id = ""

    def _finish(self, run: Run, reply: str, *, ok: bool, detail: str = "") -> str:
        self.run_ledger.record_status(run.run_id, "succeeded" if ok else "failed", detail=detail)
        return reply

    def _build_context_packet(
        self, run: Run, event: NormalizedEvent, text: str, mfs_results: list[str]
    ) -> ContextPacket:
        """Record what context this run's workflow/backend was given."""
        brain = self.channel_brain.load(event.platform, event.chat_id)
        packet = ContextPacket(
            context_packet_id=new_id("ctx"),
            run_id=run.run_id,
            source_thread_summary=text[:500],
            linked_resources=tuple(result[:200] for result in mfs_results[:8]),
            channel_brain_facts=brain.facts[:20],
            connector_platforms=(event.platform,),
            policy_constraints={
                "backend": self.config.backend,
                "governed_writes": self.config.governed_writes,
                "apply_enabled": self.config.apply_enabled,
                "mfs_enabled": self.config.mfs_enabled,
            },
            citations=(f"{event.platform}://{event.chat_id}/{event.message_id}",),
        )
        self.run_ledger.append(
            LedgerEvent(
                event_id=new_id("evt"),
                run_id=run.run_id,
                event_type="context_packet_built",
                payload={
                    "context_packet": packet.to_dict(),
                    "hash": packet.content_hash(),
                },
            )
        )
        return packet

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

        # Monitor queries are deterministic and authorization-sensitive. Route
        # them before generic workflows/backends and use only normalized,
        # adapter-derived actor and destination fields.
        if self.work_monitor is not None and parse_monitor_command(event.text) is not None:
            return self.work_monitor.handle_event(event)

        if self.copilot_relay is not None and event.platform == "feishu":
            response = self.copilot_relay.handle_event(event)
            if response is not None:
                return response.text

        run = self.run_ledger.open_run(event, executor_id=self.config.backend)
        self.last_run_id = run.run_id
        self.run_ledger.record_status(run.run_id, "running")

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
            return self._finish(
                run,
                f"OS Tag 已拒绝处理：{length.reason}",
                ok=False,
                detail=length.reason or "rejected",
            )

        receipt_command = parse_receipt_command(text)
        if receipt_command is not None:
            reply = await self.approvals.handle_text_command(
                receipt_command,
                principal=event.principal,
                chat_id=event.chat_id,
            )
            write_audit_log(
                self.config.audit_log,
                event=event,
                backend="receipts",
                mfs_enabled=self.config.mfs_enabled,
                input_text=text,
                output_text=reply,
                status="ok",
                env=self.config.env,
            )
            return self._finish(run, reply, ok=True)

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
            return self._finish(run, reply, ok=True)

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
            return self._finish(run, reply, ok=True)

        if is_ambient_status_request(text):
            brain = self.channel_brain.load(event.platform, event.chat_id)
            mode = "已开启" if brain.policy.ambient_enabled else "未开启"
            scheduler = "已开启" if self.config.ambient_enabled else "未开启"
            digest = (
                f"每日提醒：{self.config.ambient_morning_digest_hour:02d}:00 / "
                f"{self.config.ambient_evening_digest_hour:02d}:00。"
                f"管理者：{len(self.config.ambient_manager_open_ids)} 人。"
                if self.config.ambient_daily_digest_enabled
                else "每日提醒未开启。"
            )
            reply = (
                f"Ambient 主动模式{mode}。后台调度{scheduler}。"
                f"巡检间隔：{self.config.ambient_interval_seconds} 秒。"
                f"{digest}"
                "当前只会提出提醒建议，不会自动写表或创建任务。"
            )
            write_audit_log(
                self.config.audit_log,
                event=event,
                backend="ambient",
                mfs_enabled=self.config.mfs_enabled,
                input_text=text,
                output_text=reply,
                status="ok",
                env=self.config.env,
            )
            return self._finish(run, reply, ok=True)

        mfs_results = await self.mfs.search(text)
        context_text = trim_context(mfs_results, self.config)
        self._build_context_packet(run, event, text, mfs_results)
        try:
            self.auto_base.current_run_id = run.run_id
            workflow_reply = await self.auto_base.handle(event)
        except Exception as exc:  # noqa: BLE001
            workflow_reply = f"自动入表未完成：{type(exc).__name__}: {exc}"
        if workflow_reply is not None:
            workflow_ok = not (
                workflow_reply.startswith("自动入表未完成")
                or workflow_reply.startswith("需求运营未完成")
            )
            write_audit_log(
                self.config.audit_log,
                event=event,
                backend="auto_base",
                mfs_enabled=self.config.mfs_enabled,
                input_text=text,
                output_text=workflow_reply,
                status="ok" if workflow_ok else "error",
                env=self.config.env,
                error=None if workflow_ok else workflow_reply,
            )
            return self._finish(
                run,
                workflow_reply,
                ok=workflow_ok,
                detail="" if workflow_ok else workflow_reply[:200],
            )

        cost = self.config.run_cost_credits
        if cost > 0:
            policy = self.channel_brain.load(event.platform, event.chat_id).policy
            today = utc_today()
            try:
                self.budget.check(event.chat_id, policy, cost, date=today)
            except SchemaError as exc:
                reply = f"OS Tag 已暂停这个群今天的付费问答：{exc}。{exc.recovery_hint}"
                write_audit_log(
                    self.config.audit_log,
                    event=event,
                    backend=self.config.backend,
                    mfs_enabled=self.config.mfs_enabled,
                    input_text=text,
                    output_text=reply,
                    status="blocked",
                    env=self.config.env,
                    error=exc.code,
                )
                self.run_ledger.record_status(run.run_id, "blocked", detail=str(exc))
                return reply

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
        if cost > 0:
            # The backend was invoked, so the cost is incurred regardless of
            # whether it returned an answer or an error.
            self.budget.charge(
                run.run_id,
                event.chat_id,
                event.platform,
                self.config.backend,
                cost,
                date=today,
            )
        return self._finish(run, reply, ok=status == "ok", detail=error or "")
