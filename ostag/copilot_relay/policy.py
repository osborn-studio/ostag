"""Deterministic authorization and risk classification for relay instructions."""

from __future__ import annotations

from dataclasses import dataclass
import unicodedata

from ostag.core.identity import Principal


@dataclass(frozen=True)
class PolicyDecision:
    outcome: str
    reason: str


class RelayPolicy:
    def __init__(
        self,
        owner: Principal,
        *,
        instance_scopes: dict[Principal, frozenset[str]] | None = None,
    ) -> None:
        self.owner = owner
        self.instance_scopes = instance_scopes or {}

    def can_access(self, principal: Principal, instance_id: str) -> bool:
        return principal == self.owner or instance_id in self.instance_scopes.get(principal, ())

    def classify(self, text: str) -> PolicyDecision:
        normalized = unicodedata.normalize("NFKC", text).translate(
            {codepoint: None for codepoint in range(0x200B, 0x2010)}
        ).casefold()
        approval_terms = (
            "deploy", "部署", "上线", "发布", "delete", "删除", "rm ", "credential", "password",
            "token", "密钥", "billing", "付款", "购买", "发消息", "send message", "权限",
            "permission",
        )
        if any(term in normalized for term in approval_terms):
            return PolicyDecision("requires_approval", "high-risk instruction requires receipt approval")
        if normalized.startswith(("新任务", "new task")):
            return PolicyDecision("requires_confirmation", "task creation requires confirmation")
        return PolicyDecision("allow", "ordinary scoped collaboration instruction")
