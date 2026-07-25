"""Evidence-aware completion board (Phase 2 of the v0.3→v0.4 upgrade).

One authoritative, identity-scoped projection of completion governance,
reused by every surface (text render today; HTML/API later). The split the
board makes visible, per OpenTag v0.7's ``WorkLoopView``:

- **execution** truth comes from the existing work-monitor ``tasks`` row
  (what the executor reported);
- **completion** truth comes from the governance assessment (what the
  evidence proves).

Scoping reuses the existing authorization boundary
(:mod:`ostag.work_monitor.identity`): a personal view only ever queries the
principal's own instances; the admin view requires a bound global owner.
Rows for other instances are never constructed — not even as hidden JSON.
External text (gate reasons, waiver reasons) is sanitized before rendering.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ostag.core.completion import CompletionAssessment
from ostag.core.safety import sanitize_user_text
from ostag.core.schemas import SchemaError
from ostag.work_monitor.completion_store import CompletionStore
from ostag.core.identity import Principal
from ostag.work_monitor.identity import IdentityStore
from ostag.work_monitor.store import MonitorStore

_BADGE_FAMILIES = {
    "verification": "tests",
    "external_state": "deploy",
    "human_acceptance": "accept",
}


def _clean(text: str, limit: int = 200) -> str:
    """Sanitize externally influenced text for single-line board rendering."""
    flat = " ".join(sanitize_user_text(text).split())
    return flat[:limit]


def _parse_ts(value: str):
    from datetime import datetime

    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _is_expired(expires_at: str, now: str) -> bool:
    """Timestamp comparison by parsed value — valid UTC forms are not lexically
    equivalent (e.g. space-separated vs T-separated), so never compare strings."""
    if not expires_at:
        return False
    expiry = _parse_ts(expires_at)
    moment = _parse_ts(now)
    if expiry is None or moment is None:
        return False
    return expiry <= moment


@dataclass(frozen=True)
class WaiverBadge:
    actor: str
    reason: str
    gate_ids: tuple[str, ...]
    waived_at: str
    expires_at: str
    expired: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "actor": self.actor,
            "reason": self.reason,
            "gateIds": list(self.gate_ids),
            "waivedAt": self.waived_at,
            "expiresAt": self.expires_at,
            "expired": self.expired,
        }


@dataclass(frozen=True)
class CompletionBoardRow:
    instance_id: str
    work_thread_key: str
    execution_status: str  # from the tasks projection; "" when no task row exists
    agent_done: bool
    completion: str  # assessment state
    evidence_backed: bool
    tests_passed: bool
    deploy_verified: bool
    prod_accepted: bool
    missing_gate_ids: tuple[str, ...]
    failed_gate_ids: tuple[str, ...]
    blocked_gate_ids: tuple[str, ...]
    waiver: WaiverBadge | None
    next_action: str

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "instanceId": self.instance_id,
            "workThreadKey": self.work_thread_key,
            "executionStatus": self.execution_status,
            "agentDone": self.agent_done,
            "completion": self.completion,
            "evidenceBacked": self.evidence_backed,
            "testsPassed": self.tests_passed,
            "deployVerified": self.deploy_verified,
            "prodAccepted": self.prod_accepted,
            "missingGateIds": list(self.missing_gate_ids),
            "failedGateIds": list(self.failed_gate_ids),
            "blockedGateIds": list(self.blocked_gate_ids),
            "nextAction": self.next_action,
        }
        if self.waiver is not None:
            payload["waiver"] = self.waiver.to_dict()
        return payload


class CompletionBoardService:
    def __init__(
        self,
        monitor_store: MonitorStore,
        completion_store: CompletionStore,
        identities: IdentityStore,
    ) -> None:
        self.monitor_store = monitor_store
        self.completion_store = completion_store
        self.identities = identities

    # -- authorized entry points ----------------------------------------------

    def personal_rows(self, principal: Principal, *, now: str) -> tuple[CompletionBoardRow, ...]:
        """Rows for the principal's own instances only."""
        instances = self.identities.personal_instances(principal)
        return self._rows(sorted(instances), now=now)

    def admin_rows(self, principal: Principal, *, now: str) -> tuple[CompletionBoardRow, ...]:
        """Rows across all registered instances; requires a bound global owner."""
        if not self.identities.is_global_owner(principal):
            raise SchemaError(
                code="permission_denied",
                message="The team completion board requires a bound global owner",
                recovery_hint="Use the personal board, or bind an owner/admin principal.",
            )
        return self._rows(sorted(self.identities.team_instances()), now=now)

    # -- projection -----------------------------------------------------------

    def _rows(self, instance_ids: list[str], *, now: str) -> tuple[CompletionBoardRow, ...]:
        rows: list[CompletionBoardRow] = []
        for instance_id in instance_ids:
            with self.completion_store.store.connect() as connection:
                threads = connection.execute(
                    "SELECT work_thread_key FROM completion_threads WHERE instance_id=? "
                    "ORDER BY work_thread_key",
                    (instance_id,),
                ).fetchall()
            for thread in threads:
                row = self._row(instance_id, str(thread["work_thread_key"]), now=now)
                if row is not None:
                    rows.append(row)
        return tuple(rows)

    def _row(
        self, instance_id: str, work_thread_key: str, *, now: str
    ) -> CompletionBoardRow | None:
        assessment = self.completion_store.current_assessment(instance_id, work_thread_key)
        if assessment is None:
            return None
        task_id = work_thread_key.removeprefix(f"{instance_id}/")
        task = self.monitor_store.get_task(instance_id, task_id)
        execution_status = task.status if task is not None else ""
        codes = {result.reason_code for result in assessment.gate_results}
        # Badge families come from the CONTRACT's gate kinds; a family badge is
        # green only when every gate of that family has result state "passed"
        # (a waived or failed sibling gate must not light the badge).
        family_gate_ids: dict[str, set[str]] = {"tests": set(), "deploy": set(), "accept": set()}
        try:
            contract = self.completion_store.get_contract(
                instance_id, assessment.contract_id, assessment.contract_version
            )
            for gate in contract.gates:
                family = _BADGE_FAMILIES.get(gate.kind)
                if family:
                    family_gate_ids[family].add(gate.id)
        except SchemaError:
            pass  # contract snapshot unavailable: badges stay off, gaps still render
        state_by_gate = {result.gate_id: result.state for result in assessment.gate_results}

        def family_passed(family: str) -> bool:
            ids = family_gate_ids[family]
            return bool(ids) and all(state_by_gate.get(gate_id) == "passed" for gate_id in ids)

        waived_states = {result.gate_id for result in assessment.gate_results if result.state == "waived"}
        waiver = None
        if assessment.waiver is not None:
            expires_at = assessment.waiver.expires_at
            waiver = WaiverBadge(
                actor=_clean(assessment.waiver.actor.display, 80),
                reason=_clean(assessment.waiver.reason),
                gate_ids=tuple(_clean(g, 60) for g in (sorted(waived_states) or assessment.waiver.gate_ids)),
                waived_at=_clean(assessment.waiver.waived_at, 40),
                expires_at=_clean(expires_at, 40),
                expired=_is_expired(expires_at, now),
            )
        return CompletionBoardRow(
            instance_id=instance_id,
            work_thread_key=work_thread_key,
            execution_status=execution_status,
            agent_done=execution_status == "done"
            or "execution_succeeded" in codes
            or (assessment.evidence_backed and assessment.state in ("satisfied", "waived")),
            completion=assessment.state,
            evidence_backed=assessment.evidence_backed,
            tests_passed=family_passed("tests"),
            deploy_verified=family_passed("deploy"),
            prod_accepted=family_passed("accept"),
            missing_gate_ids=tuple(
                _clean(r.gate_id, 60) for r in assessment.gate_results if r.state == "missing"
            ),
            failed_gate_ids=tuple(
                _clean(r.gate_id, 60) for r in assessment.gate_results if r.state == "failed"
            ),
            blocked_gate_ids=tuple(
                _clean(r.gate_id, 60) for r in assessment.gate_results if r.state == "unknown"
            ),
            waiver=waiver,
            next_action=_clean(_next_action(assessment)),
        )


def _next_action(assessment: CompletionAssessment) -> str:
    missing = [r.gate_id for r in assessment.gate_results if r.state == "missing"]
    failed = [r.gate_id for r in assessment.gate_results if r.state == "failed"]
    blocked = [r.gate_id for r in assessment.gate_results if r.state == "unknown"]
    if assessment.state in ("satisfied", "waived"):
        return "No completion action is required."
    if blocked:
        return f"Reconcile blocked gate(s): {', '.join(blocked)}."
    if failed:
        return f"Repair failed gate(s): {', '.join(failed)}."
    if missing:
        return f"Provide evidence for gate(s): {', '.join(missing)}."
    return "Await executor progress."


_MARK = {True: "✅", False: "—"}


def render_completion_board_text(rows: tuple[CompletionBoardRow, ...]) -> str:
    """Plain-text six-line truth per work thread (universal fallback surface)."""
    if not rows:
        return "暂无受治理的完成记录。"
    blocks: list[str] = []
    for row in rows:
        lines = [
            f"{_clean(row.work_thread_key, 120)}（completion: {row.completion}"
            f"{'，证据背书' if row.evidence_backed else '，兼容模式'}）",
            f"  Agent 已完成：{_MARK[row.agent_done]}"
            + (f"（执行状态 {row.execution_status}）" if row.execution_status else ""),
            f"  测试已通过：{_MARK[row.tests_passed]}",
            f"  部署已验证：{_MARK[row.deploy_verified]}",
            f"  生产验收已通过：{_MARK[row.prod_accepted]}",
        ]
        gaps: list[str] = []
        if row.missing_gate_ids:
            gaps.append(f"缺失 {', '.join(row.missing_gate_ids)}")
        if row.failed_gate_ids:
            gaps.append(f"失败 {', '.join(row.failed_gate_ids)}")
        if row.blocked_gate_ids:
            gaps.append(f"阻塞 {', '.join(row.blocked_gate_ids)}")
        lines.append(f"  仍缺证据：{'；'.join(gaps) if gaps else '—'}")
        if row.waiver is not None:
            expiry = (
                f"已于 {row.waiver.expires_at} 过期"
                if row.waiver.expired
                else (f"{row.waiver.expires_at} 到期" if row.waiver.expires_at else "无期限")
            )
            lines.append(
                f"  例外：{row.waiver.actor} 豁免 {', '.join(row.waiver.gate_ids)}"
                f"（{row.waiver.reason}；{expiry}）"
            )
        else:
            lines.append("  例外：—")
        lines.append(f"  下一步：{row.next_action}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)
