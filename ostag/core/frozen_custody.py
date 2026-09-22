"""Read-only retired receipt projection; no writer or maintenance authority.

A trusted ledger may contain a validated projection after a future verified
migration. Parsing supplies resource custody only, never permission to dispatch.
Unknown versions are denied globally by strict consumers. No raw payload survives.
"""

from dataclasses import dataclass
import re

from ostag.core.identity import Principal
from ostag.core.schemas import ActionReceipt, SchemaError

KIND = "brief-field-retired-v1"
TEXT = "Content retired; execution forbidden."


def retired_error():
    return SchemaError(
        "frozen_content_retired",
        "Frozen content is retired or unavailable.",
        "Use retained custody evidence; an old approval cannot be replayed.",
    )


def is_retired(receipt):
    payload = (
        receipt.apply_payload
        if type(receipt) is ActionReceipt
        else (receipt.get("apply_payload") if type(receipt) is dict else None)
    )
    kind = payload.get("kind") if type(payload) is dict else None
    return type(kind) is str and kind.startswith("brief-field-retired-")


def deny_retired(receipt):
    if is_retired(receipt):
        raise retired_error()


@dataclass(frozen=True, repr=False)
class RetiredCustody:
    intent_sha256: str
    physical_resource_sha256: str
    ledger_epoch: str
    job_id: str
    original_proposal_binding: str
    maintenance_id: str

    @classmethod
    def from_receipt(cls, receipt):
        if (
            type(receipt) is not ActionReceipt
            or receipt.state != "blocked"
            or type(receipt.principal) is not Principal
            or receipt.title != "Retired field change"
            or receipt.target_system != "brief_field_change"
            or receipt.target_object != "retired"
            or receipt.mutation_type != "update"
            or receipt.summary != TEXT
            or receipt.diff != TEXT
            or receipt.verification
            or receipt.required_permissions != ("propose", "write")
        ):
            raise retired_error()
        p = receipt.apply_payload
        keys = {
            "kind",
            "intent_sha256",
            "physical_resource_sha256",
            "ledger_epoch",
            "job_id",
            "original_proposal_binding",
            "maintenance_id",
        }
        if (
            type(p) is not dict
            or set(p) != keys
            or p["kind"] != KIND
            or any(type(v) is not str for v in p.values())
        ):
            raise retired_error()
        for key in ("intent_sha256", "physical_resource_sha256", "original_proposal_binding"):
            if not re.fullmatch("[0-9a-f]{64}", p[key]):
                raise retired_error()
        if (
            receipt.action_id != "action_frozen" + p["intent_sha256"]
            or (
                p["ledger_epoch"] != "initial"
                and not re.fullmatch("[0-9a-f]{64}", p["ledger_epoch"])
            )
            or not re.fullmatch("[A-Za-z0-9][A-Za-z0-9._:-]{0,255}", p["job_id"])
            or not re.fullmatch("retire_[0-9a-f]{64}", p["maintenance_id"])
        ):
            raise retired_error()
        return cls(**{key: p[key] for key in keys if key != "kind"})
