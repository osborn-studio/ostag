"""Read-path only: hand-authored future retired records, no migration writer."""

from dataclasses import replace
import json

import pytest

from ostag.core.identity import Principal
from ostag.core.schemas import ActionReceipt, LedgerEvent, SchemaError
from ostag.core.run_ledger import RunLedger, _proposal_binding


def projection(**changes):
    receipt = ActionReceipt(
        action_id="action_frozen" + "a" * 64,
        run_id="run_brief",
        title="Retired field change",
        target_system="brief_field_change",
        target_object="retired",
        mutation_type="update",
        principal=Principal("feishu", "requester"),
        state="blocked",
        summary="Content retired; execution forbidden.",
        diff="Content retired; execution forbidden.",
        required_permissions=("propose", "write"),
        apply_payload={
            "kind": "brief-field-retired-v1",
            "intent_sha256": "a" * 64,
            "physical_resource_sha256": "b" * 64,
            "ledger_epoch": "initial",
            "job_id": "job_1",
            "original_proposal_binding": "c" * 64,
            "maintenance_id": "retire_" + "d" * 64,
        },
    )
    return replace(receipt, **changes)


def fixture_ledger(tmp_path, receipt=None):
    receipt = receipt or projection()
    payload = dict(
        receipt=receipt.to_dict(),
        chat_id="private_chat",
        platform="feishu",
        proposer_id="requester",
        proposal_scope="e" * 64,
    )
    payload["proposal_binding"] = _proposal_binding(payload)
    event = LedgerEvent(
        "evt_fixture", receipt.run_id, "action_proposed", payload, "2026-09-21T10:00:00+00:00"
    ).to_dict()
    ledger = RunLedger(tmp_path / "run.jsonl")
    ledger.path.write_text(json.dumps(event) + "\n")
    return ledger


def test_projection_parses_only_immutable_digest_custody():
    from ostag.core.frozen_custody import RetiredCustody

    receipt = projection()
    parsed = RetiredCustody.from_receipt(receipt)
    receipt.apply_payload["physical_resource_sha256"] = "f" * 64
    assert parsed.physical_resource_sha256 == "b" * 64
    assert not hasattr(parsed, "intent_json")


@pytest.mark.parametrize(
    "damage", ["version", "extra", "digest", "action", "state", "body", "principal"]
)
def test_invalid_projection_is_not_custody(damage):
    from ostag.core.frozen_custody import RetiredCustody

    receipt = projection()
    if damage == "version":
        receipt.apply_payload["kind"] = "brief-field-retired-v2"
    elif damage == "extra":
        receipt.apply_payload["intent_json"] = "private"
    elif damage == "digest":
        receipt.apply_payload["physical_resource_sha256"] = "bad"
    elif damage == "action":
        receipt = replace(receipt, action_id="action_frozen" + "f" * 64)
    elif damage == "state":
        receipt = replace(receipt, state="applying")
    elif damage == "body":
        receipt = replace(receipt, summary="private")
    else:
        receipt = replace(receipt, principal=None)
    with pytest.raises(SchemaError):
        RetiredCustody.from_receipt(receipt)


def test_listing_blocked_but_authoritative_resolution_and_append_deny(tmp_path):
    ledger = fixture_ledger(tmp_path)
    receipt = projection()
    assert ledger.get_receipt(receipt.action_id).state == "blocked"
    with pytest.raises(SchemaError) as error:
        ledger.get_frozen_receipt(receipt.action_id, chat_id="private_chat", platform="feishu")
    assert error.value.code == "frozen_content_retired"
    before = ledger.path.read_bytes()
    with pytest.raises(SchemaError):
        ledger.record_receipt_state(
            receipt.with_state("applying"), previous_state="blocked", actor_id="requester"
        )
    with pytest.raises(SchemaError):
        ledger.record_receipt_once(
            receipt.with_state("needs_approval"),
            chat_id="private_chat",
            platform="feishu",
            proposer_id="requester",
        )
    assert ledger.path.read_bytes() == before


def test_historical_state_cannot_resurrect_projection(tmp_path):
    ledger = fixture_ledger(tmp_path)
    event = LedgerEvent(
        "evt_old",
        "run_brief",
        "action_state_changed",
        {
            "action_id": projection().action_id,
            "from_state": "blocked",
            "to_state": "ready_to_apply",
        },
        "2026-09-21T10:01:00+00:00",
    )
    with ledger.path.open("a") as f:
        f.write(json.dumps(event.to_dict()) + "\n")
    assert ledger.get_receipt(projection().action_id).state == "blocked"


def test_projection_cannot_mint_ephemeral_execution_authority():
    from ostag.core.effects import _receipt_digest

    with pytest.raises(SchemaError):
        _receipt_digest(projection(state="applying"))


@pytest.mark.parametrize("same_resource", [True, False])
async def test_retired_projection_fences_only_its_physical_resource(tmp_path, same_resource):
    from tests.test_effect_journal import journal_rig, apply_journal
    from ostag.briefs.actions import FrozenFieldChange
    from ostag.core.effect_journal import _resource

    calls = []

    async def operation(permit):
        calls.append(permit)
        return {"synthetic": True}

    rig = journal_rig(tmp_path, operation)
    change = FrozenFieldChange.from_dict(json.loads(rig[1].apply_payload["intent_json"]))
    value = projection()
    value.apply_payload["physical_resource_sha256"] = (
        _resource(change) if same_resource else "b" * 64
    )
    separate = tmp_path / "projection-fixture"
    separate.mkdir()
    source = fixture_ledger(separate, value)
    with rig[2].ledger.path.open("ab") as f:
        f.write(source.path.read_bytes())
    if same_resource:
        with pytest.raises(SchemaError):
            await apply_journal(rig)
        assert calls == []
    else:
        result = await apply_journal(rig)
        assert result["action"]["state"] == "applying" and len(calls) == 1


def test_duplicate_proposal_never_replaces_retired_projection(tmp_path):
    ledger = fixture_ledger(tmp_path)
    record = json.loads(ledger.path.read_text())
    record["payload"]["receipt"]["apply_payload"]["kind"] = "brief-field-change-v1"
    record["payload"]["receipt"]["state"] = "needs_approval"
    record["payload"]["proposal_binding"] = _proposal_binding(record["payload"])
    with ledger.path.open("a") as f:
        f.write(json.dumps(record) + "\n")
    assert ledger.get_receipt(projection().action_id).state == "blocked"
    with pytest.raises(SchemaError):
        ledger.get_frozen_receipt(projection().action_id, chat_id="private_chat", platform="feishu")


@pytest.mark.parametrize("verb", ["approve", "apply"])
async def test_retired_action_cannot_reenter_approval_service(tmp_path, verb):
    from ostag.config import Settings
    from ostag.core.approvals import ApprovalService
    from ostag.core.receipts import ReceiptCommand

    ledger = fixture_ledger(tmp_path)
    service = ApprovalService(
        Settings(apply_enabled=True, actor_permissions=("feishu:requester:write",)), ledger
    )
    service.channel_brain.root = tmp_path / "channels"
    with pytest.raises(SchemaError) as error:
        await service.execute(
            ReceiptCommand(verb, projection().action_id),
            principal=projection().principal,
            chat_id="private_chat",
        )
    assert error.value.code == "frozen_content_retired"


def test_supersession_preserves_retired_predecessor_block(tmp_path):
    ledger = fixture_ledger(tmp_path)
    record = json.loads(ledger.path.read_text())
    payload = record['payload']
    payload['receipt']['action_id'] = 'action_frozen' + 'f' * 64
    payload['receipt']['state'] = 'needs_approval'
    payload['receipt']['apply_payload'] = {'kind': 'brief-field-change-v1'}
    payload['supersedes'] = projection().action_id
    payload['proposal_binding'] = _proposal_binding(payload)
    with ledger.path.open('a') as output:
        output.write(json.dumps(record) + '\n')
    assert ledger.get_receipt(projection().action_id).state == 'blocked'
    assert ledger.get_receipt('action_frozen' + 'f' * 64).state == 'needs_approval'


@pytest.mark.parametrize('damage', ['unknown_version', 'private_body', 'identity_digest'])
def test_strict_reader_denies_unrecognized_or_invalid_projection(tmp_path, damage):
    receipt = projection()
    if damage == 'unknown_version':
        receipt.apply_payload['kind'] = 'brief-field-retired-v999'
    elif damage == 'private_body':
        receipt = replace(receipt, diff='private content')
    else:
        receipt.apply_payload['intent_sha256'] = 'f' * 64
    ledger = fixture_ledger(tmp_path, receipt)
    with pytest.raises(SchemaError):
        with ledger._exclusive(read_only=True) as handle:
            ledger._strict_records(handle)
