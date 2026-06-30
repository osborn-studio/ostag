from __future__ import annotations

from ostag.core.stage_ledger import StageLedger


def test_stage_ledger_formats_user_progress() -> None:
    ledger = StageLedger()

    ledger.complete("received", "已接收请求")
    ledger.complete("target_found", "已定位需求")

    assert ledger.format_user() == "阶段：已接收请求 → 已定位需求"
    assert ledger.to_dicts()[0]["key"] == "received"
    assert ledger.to_dicts()[1]["status"] == "completed"
