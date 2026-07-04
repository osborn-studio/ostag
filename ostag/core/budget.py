"""Cost and budget governance.

Each run that reaches a billable stage (the LLM backend) is charged a fixed
number of *credits* (`OSTAG_RUN_COST_CREDITS`). A channel may cap its daily
spend (`OSTAG_DAILY_COST_BUDGET`, or a per-channel override); a run that would
push the channel over its budget is blocked *before* the paid call is made, so
no cost is incurred on a rejected run.

Credits are an abstract integer unit chosen by the operator — they may map to
cents, tokens/1k, or plain call counts. Budget accounting lives in the run
ledger (`cost_recorded` events), so it is auditable and survives restarts.
"""

from __future__ import annotations

from datetime import UTC, datetime

from ostag.config import Settings
from ostag.core.channel_brain import ChannelPolicy
from ostag.core.run_ledger import RunLedger
from ostag.core.schemas import SchemaError


def utc_today() -> str:
    return datetime.now(UTC).date().isoformat()


class BudgetService:
    def __init__(self, config: Settings, ledger: RunLedger) -> None:
        self.config = config
        self.ledger = ledger

    def effective_daily_budget(self, policy: ChannelPolicy | None) -> int:
        """Channel override when set; else the global budget. 0 means unlimited."""
        if policy is not None and policy.daily_cost_budget is not None:
            return max(0, policy.daily_cost_budget)
        return max(0, self.config.daily_cost_budget)

    def status(
        self, chat_id: str, policy: ChannelPolicy | None, *, date: str | None = None
    ) -> dict[str, int | bool | str]:
        day = date or utc_today()
        budget = self.effective_daily_budget(policy)
        spent = self.ledger.cost_today(chat_id, day)
        return {
            "date": day,
            "unit": "credits",
            "daily_budget": budget,
            "spent_today": spent,
            "remaining": (budget - spent) if budget > 0 else -1,
            "unlimited": budget == 0,
            "run_cost": self.config.run_cost_credits,
        }

    def check(
        self,
        chat_id: str,
        policy: ChannelPolicy | None,
        cost: int,
        *,
        date: str | None = None,
    ) -> None:
        """Raise SchemaError(budget_exceeded) if charging `cost` would exceed budget."""
        budget = self.effective_daily_budget(policy)
        if budget <= 0 or cost <= 0:
            return
        spent = self.ledger.cost_today(chat_id, date or utc_today())
        if spent + cost > budget:
            raise SchemaError(
                code="budget_exceeded",
                message=(
                    f"Channel {chat_id} daily budget exceeded: "
                    f"{spent}+{cost} > {budget} credits"
                ),
                recovery_hint=(
                    "Wait for the daily reset, or an admin can raise the budget "
                    "(OSTAG_DAILY_COST_BUDGET or ostag channel policy set "
                    "--daily-cost-budget N)."
                ),
            )

    def charge(
        self,
        run_id: str,
        chat_id: str,
        platform: str,
        backend: str,
        cost: int,
        *,
        date: str | None = None,
    ) -> None:
        if cost <= 0:
            return
        self.ledger.record_cost(
            run_id,
            chat_id=chat_id,
            platform=platform,
            credits=cost,
            backend=backend,
            date=date or utc_today(),
        )
