from __future__ import annotations

import json
from pathlib import Path

import pytest

from ostag.config import load_settings
from ostag.core.approvals import ApprovalService
from ostag.core.channel_brain import ChannelBrainStore
from ostag.core.permissions import effective_flag, resolve_permission
from ostag.core.receipts import ReceiptCommand
from ostag.core.run_ledger import RunLedger
from ostag.core.schemas import ActionReceipt, SchemaError, new_id
from ostag.main import main_async


def _config(tmp_path: Path, **overrides: object):
    return load_settings(None, audit_log=tmp_path / "audit.log", **overrides)


def _receipt(run_id: str = "run_cp1") -> ActionReceipt:
    return ActionReceipt(
        action_id=new_id("action"),
        run_id=run_id,
        title="更新需求 REQ-3",
        target_system="feishu_base",
        target_object="rec_3",
        mutation_type="update",
        state="needs_approval",
        apply_payload={
            "operation": "batch_update",
            "base_token": "bt",
            "table_id": "tbl",
            "record_id": "rec_3",
            "patch": {"需求状态": "已完成"},
        },
    )


class OkExecutor:
    async def apply(self, receipt: ActionReceipt) -> str:
        return "done"


def test_policy_round_trip_and_backward_compat(tmp_path: Path) -> None:
    config = _config(tmp_path)
    store = ChannelBrainStore(config, root=tmp_path / "brains")

    # legacy brain without governance fields loads as inherit
    legacy = store.load("feishu", "oc_legacy")
    assert legacy.policy.governed_writes is None
    assert legacy.policy.apply_enabled is None
    assert legacy.policy.actor_permissions == ()

    store.update_policy(
        "feishu",
        "oc_x",
        governed_writes=True,
        apply_enabled=False,
        actor_permissions=("ou_alice:write",),
    )
    loaded = store.load("feishu", "oc_x").policy
    assert loaded.governed_writes is True
    assert loaded.apply_enabled is False
    assert loaded.actor_permissions == ("ou_alice:write",)

    # inherit reset persists as null
    store.update_policy("feishu", "oc_x", governed_writes=None)
    assert store.load("feishu", "oc_x").policy.governed_writes is None


def test_update_policy_rejects_unknown_fields(tmp_path: Path) -> None:
    store = ChannelBrainStore(_config(tmp_path), root=tmp_path / "brains")

    with pytest.raises(ValueError):
        store.update_policy("feishu", "oc_x", explode=True)


def test_channel_grants_take_precedence(tmp_path: Path) -> None:
    config = _config(
        tmp_path,
        actor_permissions=("ou_alice:read",),
        default_permission_level="read",
    )

    assert resolve_permission(config, "ou_alice") == "read"
    assert resolve_permission(config, "ou_alice", channel_grants=("ou_alice:write",)) == "write"
    assert resolve_permission(config, "ou_bob", channel_grants=("ou_alice:write",)) == "read"


def test_effective_flag_inherit_semantics() -> None:
    assert effective_flag(None, True) is True
    assert effective_flag(None, False) is False
    assert effective_flag(True, False) is True
    assert effective_flag(False, True) is False


@pytest.mark.asyncio
async def test_channel_apply_enabled_overrides_global_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OSTAG_MEMORY_ROOT", str(tmp_path / "brains"))
    config = _config(tmp_path, actor_permissions=("dev:write",), apply_enabled=False)
    ChannelBrainStore(config).update_policy("feishu", "chat1", apply_enabled=True)
    ledger = RunLedger(config.run_ledger_path)
    receipt = _receipt()
    ledger.record_receipt(receipt, chat_id="chat1", platform="feishu")
    service = ApprovalService(config, ledger, {"feishu_base": OkExecutor()})
    await service.execute(
        ReceiptCommand(verb="approve", reference="1"),
        actor_id="dev",
        chat_id="chat1",
        platform="feishu",
    )

    result = await service.execute(
        ReceiptCommand(verb="apply", reference="1"),
        actor_id="dev",
        chat_id="chat1",
        platform="feishu",
    )

    assert result["action"]["state"] == "applied"


@pytest.mark.asyncio
async def test_channel_apply_disabled_overrides_global_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OSTAG_MEMORY_ROOT", str(tmp_path / "brains"))
    config = _config(tmp_path, actor_permissions=("dev:write",), apply_enabled=True)
    ChannelBrainStore(config).update_policy("feishu", "chat1", apply_enabled=False)
    ledger = RunLedger(config.run_ledger_path)
    receipt = _receipt()
    ledger.record_receipt(receipt, chat_id="chat1", platform="feishu")
    service = ApprovalService(config, ledger, {"feishu_base": OkExecutor()})
    await service.execute(
        ReceiptCommand(verb="approve", reference="1"),
        actor_id="dev",
        chat_id="chat1",
        platform="feishu",
    )

    with pytest.raises(SchemaError) as excinfo:
        await service.execute(
            ReceiptCommand(verb="apply", reference="1"),
            actor_id="dev",
            chat_id="chat1",
            platform="feishu",
        )

    assert excinfo.value.code == "apply_disabled"


@pytest.mark.asyncio
async def test_channel_grant_elevates_actor_in_channel_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OSTAG_MEMORY_ROOT", str(tmp_path / "brains"))
    config = _config(tmp_path, default_permission_level="read")
    ChannelBrainStore(config).update_policy(
        "feishu", "chat1", actor_permissions=("ou_carol:write",)
    )
    ledger = RunLedger(config.run_ledger_path)
    receipt = _receipt()
    ledger.record_receipt(receipt, chat_id="chat1", platform="feishu")
    other = _receipt(run_id="run_cp2")
    ledger.record_receipt(other, chat_id="chat2", platform="feishu")
    service = ApprovalService(config, ledger)

    result = await service.execute(
        ReceiptCommand(verb="approve", reference=receipt.action_id),
        actor_id="ou_carol",
        chat_id="chat1",
        platform="feishu",
    )
    assert result["action"]["state"] == "ready_to_apply"

    with pytest.raises(SchemaError) as excinfo:
        await service.execute(
            ReceiptCommand(verb="approve", reference=other.action_id),
            actor_id="ou_carol",
            chat_id="chat2",
            platform="feishu",
        )
    assert excinfo.value.code == "permission_denied"


async def test_cli_channel_policy_show_and_set(
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
            "oc_cli",
            "--governed-writes",
            "true",
            "--apply-enabled",
            "false",
            "--grant",
            "ou_alice:write",
            "--json",
        ]
    )
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["channel"]["governed_writes"] is True
    assert payload["channel"]["apply_enabled"] is False
    assert payload["channel"]["actor_permissions"] == ["ou_alice:write"]
    assert payload["effective"]["governed_writes"] is True
    assert payload["effective"]["apply_enabled"] is False

    exit_code = await main_async(
        [
            "channel",
            "policy",
            "set",
            "--platform",
            "feishu",
            "--chat",
            "oc_cli",
            "--governed-writes",
            "inherit",
            "--revoke",
            "ou_alice",
            "--json",
        ]
    )
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["channel"]["governed_writes"] is None
    assert payload["channel"]["actor_permissions"] == []

    exit_code = await main_async(
        ["channel", "policy", "show", "--platform", "feishu", "--chat", "oc_cli", "--json"]
    )
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["effective"]["governed_writes"] is False  # inherits global default


async def test_cli_channel_policy_set_rejects_bad_grant(
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
            "oc_cli",
            "--grant",
            "ou_alice:god",
            "--json",
        ]
    )

    assert exit_code == 1
    error = json.loads(capsys.readouterr().out)["error"]
    assert error["code"] == "invalid_grant"


async def test_cli_channel_policy_set_requires_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("OSTAG_MEMORY_ROOT", str(tmp_path / "brains"))

    exit_code = await main_async(
        ["channel", "policy", "set", "--platform", "feishu", "--chat", "oc_cli", "--json"]
    )

    assert exit_code == 1
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "no_changes"
