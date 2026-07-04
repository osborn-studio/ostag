from __future__ import annotations

import json
from pathlib import Path

import pytest

from ostag.config import load_settings
from ostag.core.budget import BudgetService
from ostag.core.channel_brain import ChannelBrainStore, ChannelPolicy
from ostag.core.event import NormalizedEvent
from ostag.core.run_ledger import RunLedger
from ostag.core.runner import OSTagRunner
from ostag.core.schemas import SchemaError
from ostag.main import main_async

DATE = "2026-07-04"


def _config(tmp_path: Path, **overrides: object):
    return load_settings(None, audit_log=tmp_path / "audit.log", backend="mock", **overrides)


def _event(text: str = "请总结项目") -> NormalizedEvent:
    return NormalizedEvent(
        platform="feishu",
        event_id="evt",
        chat_id="chat1",
        message_id="m1",
        sender_id="u1",
        text=text,
        mention_bot=True,
    )


def test_effective_budget_channel_overrides_global(tmp_path: Path) -> None:
    config = _config(tmp_path, daily_cost_budget=100)
    service = BudgetService(config, RunLedger(config.run_ledger_path))

    assert service.effective_daily_budget(None) == 100
    assert service.effective_daily_budget(ChannelPolicy(daily_cost_budget=20)) == 20
    # None on the channel inherits the global value
    assert service.effective_daily_budget(ChannelPolicy(daily_cost_budget=None)) == 100
    # 0 means unlimited
    assert service.effective_daily_budget(ChannelPolicy(daily_cost_budget=0)) == 0


def test_check_and_charge_accumulate(tmp_path: Path) -> None:
    config = _config(tmp_path, daily_cost_budget=25)
    ledger = RunLedger(config.run_ledger_path)
    service = BudgetService(config, ledger)
    policy = ChannelPolicy(daily_cost_budget=25)

    # three charges of 10 fit 25? 10, 20 ok; 30 would exceed
    service.check("chat1", policy, 10, date=DATE)
    service.charge("run_1", "chat1", "feishu", "mock", 10, date=DATE)
    service.check("chat1", policy, 10, date=DATE)
    service.charge("run_2", "chat1", "feishu", "mock", 10, date=DATE)

    assert ledger.cost_today("chat1", DATE) == 20
    with pytest.raises(SchemaError) as excinfo:
        service.check("chat1", policy, 10, date=DATE)
    assert excinfo.value.code == "budget_exceeded"
    assert "raise the budget" in excinfo.value.recovery_hint


def test_unlimited_budget_never_blocks(tmp_path: Path) -> None:
    config = _config(tmp_path, daily_cost_budget=0)
    service = BudgetService(config, RunLedger(config.run_ledger_path))

    service.check("chat1", ChannelPolicy(daily_cost_budget=0), 10_000, date=DATE)  # no raise


def test_cost_today_is_scoped_by_chat_and_date(tmp_path: Path) -> None:
    ledger = RunLedger(tmp_path / "ledger.jsonl")
    ledger.record_cost("r1", chat_id="a", platform="feishu", credits=5, backend="mock", date=DATE)
    ledger.record_cost("r2", chat_id="a", platform="feishu", credits=7, backend="mock", date=DATE)
    ledger.record_cost("r3", chat_id="b", platform="feishu", credits=9, backend="mock", date=DATE)
    ledger.record_cost(
        "r4", chat_id="a", platform="feishu", credits=3, backend="mock", date="2026-07-05"
    )

    assert ledger.cost_today("a", DATE) == 12
    assert ledger.cost_today("b", DATE) == 9
    assert ledger.cost_today("a", "2026-07-05") == 3


@pytest.mark.asyncio
async def test_runner_charges_per_backend_run(tmp_path: Path) -> None:
    config = _config(tmp_path, run_cost_credits=5)
    runner = OSTagRunner(config)

    await runner.handle_event(_event())

    ledger = RunLedger(config.run_ledger_path)
    # cost recorded for the mock-backend run
    from ostag.core.budget import utc_today

    assert ledger.cost_today("chat1", utc_today()) == 5


@pytest.mark.asyncio
async def test_runner_blocks_when_budget_exhausted(tmp_path: Path) -> None:
    config = _config(tmp_path, run_cost_credits=10, daily_cost_budget=15)
    runner = OSTagRunner(config)

    first = await runner.handle_event(_event("第一次"))
    assert first.startswith("OS Tag mock reply:")

    # second would be 10+10 > 15 → blocked before the backend call
    second = await runner.handle_event(_event("第二次"))
    assert "已暂停" in second
    assert "预算" in second or "budget" in second.lower()

    ledger = RunLedger(config.run_ledger_path)
    runs = ledger.list_runs()
    blocked = [run for run in runs if run.get("status") == "blocked"]
    assert len(blocked) == 1
    from ostag.core.budget import utc_today

    # blocked run incurred no charge: only the first run's 10 credits recorded
    assert ledger.cost_today("chat1", utc_today()) == 10


@pytest.mark.asyncio
async def test_channel_budget_override_blocks_when_global_unlimited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OSTAG_MEMORY_ROOT", str(tmp_path / "brains"))
    config = _config(tmp_path, run_cost_credits=10, daily_cost_budget=0)
    ChannelBrainStore(config).update_policy("feishu", "chat1", daily_cost_budget=5)
    runner = OSTagRunner(config)

    reply = await runner.handle_event(_event())

    assert "已暂停" in reply  # 10 > channel budget 5, blocked immediately


@pytest.mark.asyncio
async def test_zero_cost_is_backward_compatible(tmp_path: Path) -> None:
    config = _config(tmp_path)  # run_cost_credits defaults to 0
    runner = OSTagRunner(config)

    reply = await runner.handle_event(_event())

    assert reply.startswith("OS Tag mock reply:")
    ledger = RunLedger(config.run_ledger_path)
    from ostag.core.budget import utc_today

    assert ledger.cost_today("chat1", utc_today()) == 0


async def test_cli_budget_status_and_policy_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("OSTAG_MEMORY_ROOT", str(tmp_path / "brains"))
    monkeypatch.setenv("OSTAG_RUN_LEDGER_PATH", str(tmp_path / "ledger.jsonl"))

    exit_code = await main_async(
        [
            "channel",
            "policy",
            "set",
            "--platform",
            "feishu",
            "--chat",
            "oc_b",
            "--daily-cost-budget",
            "50",
            "--json",
        ]
    )
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["channel"]["daily_cost_budget"] == 50
    assert payload["effective"]["daily_cost_budget"] == 50

    exit_code = await main_async(
        ["budget", "status", "--platform", "feishu", "--chat", "oc_b", "--json"]
    )
    assert exit_code == 0
    status = json.loads(capsys.readouterr().out)
    assert status["daily_budget"] == 50
    assert status["spent_today"] == 0
    assert status["remaining"] == 50
    assert status["unit"] == "credits"
    assert status["unlimited"] is False


async def test_cli_policy_set_invalid_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("OSTAG_MEMORY_ROOT", str(tmp_path / "brains"))

    exit_code = await main_async(
        [
            "channel",
            "policy",
            "set",
            "--platform",
            "feishu",
            "--chat",
            "oc_b",
            "--daily-cost-budget",
            "lots",
            "--json",
        ]
    )

    assert exit_code == 1
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "invalid_budget"
