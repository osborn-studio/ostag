"""Durable JSONL run ledger.

Every OS Tag run appends immutable :class:`LedgerEvent` records to a local
JSONL file. The ledger is the audit source of truth: run status can be
reconstructed from it, artifacts can be listed from it, and it can be exported
as stable JSON for humans and agents. All string payload values pass through
secret redaction before they touch disk.
"""

from __future__ import annotations

import json
import fcntl
import hashlib
import os
from contextlib import contextmanager, nullcontext
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic, sleep
from typing import Any

from ostag.core.frozen_custody import RetiredCustody, is_retired, deny_retired
from ostag.core.event import NormalizedEvent
from ostag.core.identity import Principal
from ostag.core.safety import redact_secrets
from ostag.core.schemas import (
    ActionReceipt,
    ApprovalDecision,
    Artifact,
    LedgerEvent,
    Run,
    RunActor,
    RunIntent,
    RunSource,
    SchemaError,
    new_id,
)


def _redact_deep(value: Any) -> Any:
    if isinstance(value, str):
        return redact_secrets(value)
    if isinstance(value, dict):
        return {key: _redact_deep(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_deep(item) for item in value]
    return value


def _proposal_error(code: str) -> SchemaError:
    return SchemaError(code, 'Frozen proposal could not be recorded.',
                       'Verify the current proposal and ledger; do not reuse an old approval.')


def _proposal_binding(payload: dict) -> str:
    value = json.loads(json.dumps(payload, allow_nan=False))
    value.pop('proposal_binding', None)
    for key in ('created_at', 'updated_at', 'state', 'commands'):
        value['receipt'].pop(key, None)
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _is_recovery(record):
    return (record.get('event_type') == 'security_event'
            and record.get('payload', {}).get('kind') == 'frozen_history_recovered')


def _is_frozen_record(record):
    payload = record.get('payload', {})
    return (_is_recovery(record)
          or (record['event_type'] == 'action_state_changed'
              and str(payload.get('action_id', '')).startswith('action_frozen'))
          or (record['event_type'] == 'action_proposed'
              and type(payload.get('receipt')) is dict
              and str(payload['receipt'].get('action_id', '')).startswith('action_frozen')))


def _epoch_valid(value):
    return (type(value) is str and (value == 'initial' or
            (len(value) == 64 and all(c in '0123456789abcdef' for c in value))))


class RunLedger:
    def __init__(self, path: Path, *, clock: Any | None = None, coordination=None) -> None:
        from ostag.core.ledger_coordination import LedgerCoordination
        if coordination is not None:
            if type(coordination) is not LedgerCoordination:
                raise _proposal_error('ledger_coordination_unavailable')
            coordination.bind(path)
        self.coordination = coordination
        self.path = coordination.ledger_path if coordination else Path(path)
        self.clock = clock or (lambda: datetime.now(UTC))

    def append(self, event: LedgerEvent) -> LedgerEvent:
        record = event.to_dict()
        record["payload"] = _redact_deep(record.get("payload", {}))
        if _is_recovery(record):
            raise _proposal_error('recovery_requires_verified_snapshot')
        if record['event_type'] == 'action_proposed':
            # Retirement projections belong to a verified maintenance protocol,
            # never to ordinary append, regardless of action ID or kind version.
            deny_retired(record['payload'].get('receipt'))
        if (record['event_type'] == 'action_proposed'
                and str(record['payload'].get('receipt', {}).get('action_id', '')).startswith(
                    'action_frozen')):
            raise _proposal_error('frozen_proposal_requires_unique_creation')
        frozen_state = (record['event_type'] == 'action_state_changed'
                        and str(record['payload'].get('action_id', '')).startswith('action_frozen'))
        with self._exclusive(bounded=frozen_state) as handle:
            if (record['event_type'] == 'action_state_changed'
                    and str(record['payload'].get('action_id', '')).startswith('action_frozen')):
                records = self._strict_records(handle)
                action_id = record['payload']['action_id']
                if self._frozen_original(records, action_id) is None:
                    raise _proposal_error('action_not_found')
                entry = self._fold_receipts(records)[action_id]
                if (entry.get('retired') or entry.get('quarantined') or entry.get('superseded_by') is not None
                        or entry['receipt']['state'] != record['payload'].get('from_state')):
                    raise _proposal_error('frozen_state_conflict')
            self._write_locked(handle, record, durable=frozen_state)
        return event

    @contextmanager
    def _exclusive(self, *, read_only=False, bounded=True):
        """Optional stable domain lock precedes the existing inode lock.

        Default legacy callers are unchanged. Host opt-in is not proof that old
        binaries stopped; no replacement operation is enabled by this primitive.
        """
        deadline = monotonic() + .5 if bounded and self.coordination else None
        guard = self.coordination.hold(deadline=deadline) if self.coordination else nullcontext()
        with guard:
            if not read_only and self.coordination is None:
                self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.coordination:
                flags = (os.O_RDONLY if read_only else os.O_RDWR | os.O_APPEND) | os.O_NOFOLLOW | os.O_NONBLOCK
                try:
                    descriptor = os.open(self.path, flags)
                    try:
                        opened = os.fdopen(descriptor, 'rb' if read_only else 'a+b')
                    except BaseException:
                        os.close(descriptor)
                        raise
                except OSError:
                    raise SchemaError('ledger_coordination_unavailable', 'Ledger coordination is unavailable.',
                        'Verify the managed ledger domain; do not recreate or bypass its lock.') from None
            else:
                opened = self.path.open('rb' if read_only else 'a+b')
            with opened as handle:
                if self.coordination:
                    self.coordination.verify_ledger(handle)
                if not bounded:
                    # Managed readers share snapshots, so every managed append
                    # must exclude them. Unmanaged legacy admission is unchanged.
                    mode = fcntl.LOCK_EX if self.coordination else fcntl.LOCK_SH
                    fcntl.flock(handle.fileno(), mode)
                else:
                    if deadline is None:
                        deadline = monotonic() + .5
                    while True:
                        if monotonic() >= deadline:
                            raise _proposal_error('ledger_busy')
                        try:
                            mode = (fcntl.LOCK_SH if read_only and self.coordination
                                    else fcntl.LOCK_EX)
                            fcntl.flock(handle.fileno(), mode | fcntl.LOCK_NB)
                            break
                        except BlockingIOError:
                            if monotonic() >= deadline:
                                raise _proposal_error('ledger_busy') from None
                            sleep(.001)
                try:
                    yield handle
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _write_locked(handle, record, *, durable=False):
        frozen = _is_frozen_record(record)
        handle.write((json.dumps(record, ensure_ascii=False, sort_keys=True,
                                 allow_nan=not frozen) + '\n').encode('utf-8'))
        handle.flush()
        if durable:
            os.fsync(handle.fileno())

    @staticmethod
    def _strict_records(handle):
        # Stream all bytes; retain only receipt history. No whole-file capacity
        # cliff. Invalid bytes remain fatal unless a verified recovery marker
        # explicitly quarantines their entire preceding history.
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError('duplicate key')
                result[key] = value
            return result
        handle.seek(0)
        digest, length, uncertain, records = hashlib.sha256(), 0, False, []
        for raw in handle:
            record = None
            try:
                if not raw.endswith(b'\n'):
                    raise ValueError('unterminated record')
                if raw.strip():
                    record = json.loads(raw, object_pairs_hook=unique)
                    if type(record) is not dict or type(record.get('payload', {})) is not dict:
                        raise ValueError('invalid event')
                    LedgerEvent.from_dict(record)
                    payload = record.get('payload', {})
                    if _is_frozen_record(record):
                        # Frozen history must be finite; unrelated legacy audit
                        # payloads historically allowed NaN/Infinity and pruning.
                        json.dumps(record, allow_nan=False)
                    if _is_recovery(record):
                        if (payload.get('prefix_sha256') != digest.hexdigest()
                                or type(payload.get('prefix_bytes')) is not int
                                or payload['prefix_bytes'] != length
                                or not _epoch_valid(payload.get('epoch'))
                                or payload['epoch'] == 'initial'
                                or type(payload.get('reason')) is not str
                                or not payload['reason']):
                            raise ValueError('invalid recovery binding')
                        Principal.from_dict(payload['operator'])
                        uncertain = False
                    elif record['event_type'] == 'action_proposed':
                        receipt = ActionReceipt.from_dict(payload['receipt'])
                        if is_retired(receipt):
                            RetiredCustody.from_receipt(receipt)
                        if (receipt.action_id.startswith('action_frozen')
                                and payload.get('proposal_binding') != _proposal_binding(payload)):
                            raise ValueError('invalid frozen binding')
            except (ValueError, TypeError, AttributeError, KeyError, RecursionError):
                uncertain = True
                record = None
            if record is not None and not uncertain and (record['event_type'] in (
                    'action_proposed', 'action_state_changed') or _is_recovery(record)):
                records.append(record)
            digest.update(raw)
            length += len(raw)
        if uncertain:
            raise _proposal_error('ledger_integrity')
        return records

    @staticmethod
    def _epoch(records):
        return next((r['payload']['epoch'] for r in reversed(records)
                     if _is_recovery(r)), 'initial')

    def frozen_epoch(self):
        """Current proposal generation, supplied by the trusted host when freezing."""
        try:
            with self._exclusive(read_only=True) as handle:
                return self._epoch(self._strict_records(handle))
        except FileNotFoundError:
            return 'initial'

    def recover_frozen_history(self, *, expected_sha256, operator, reason):
        """Explicit trusted-local maintenance; preserve bytes and revoke old approval.

        Stop effect workers first and retain incident evidence. This never asserts
        provider absence/success or authorizes replay. It does not restore a backup
        or replace the inode. Caller must supply the exact inspected file digest.
        """
        if (type(operator) is not Principal or type(reason) is not str
                or not reason.strip() or len(reason) > 500
                or redact_secrets(reason) != reason
                or not _epoch_valid(expected_sha256) or expected_sha256 == 'initial'):
            raise _proposal_error('invalid_recovery_request')
        # Do not manufacture a missing ledger as recovery evidence.
        if not self.path.is_file():
            raise _proposal_error('action_not_found')
        with self._exclusive() as handle:
            handle.seek(0)
            digest, length, last = hashlib.sha256(), 0, b''
            while block := handle.read(1024 * 1024):
                digest.update(block)
                length += len(block)
                last = block[-1:]
            if digest.hexdigest() != expected_sha256:
                raise _proposal_error('recovery_snapshot_changed')
            if last and last != b'\n':
                handle.write(b'\n')
                handle.flush()
                digest.update(b'\n')
                length += 1
            epoch = hashlib.sha256(os.urandom(32)).hexdigest()
            record = LedgerEvent(new_id('evt'), 'run_recovery', 'security_event', {
                'kind': 'frozen_history_recovered', 'epoch': epoch, 'prefix_sha256': digest.hexdigest(), 'prefix_bytes': length,
                'inspected_sha256': expected_sha256, 'operator': operator.to_dict(),
                'reason': reason,
            }, self.clock().isoformat()).to_dict()
            if _redact_deep(record) != record:
                raise _proposal_error('invalid_recovery_request')
            self._write_locked(handle, record)
            self._sync_proposal(handle)
            return epoch

    @staticmethod
    def _frozen_original(records, action_id):
        originals = [r['payload'] for r in records if r.get('event_type') == 'action_proposed'
                     and r['payload']['receipt'].get('action_id') == action_id]
        if not originals:
            return None
        if len(originals) != 1:
            raise _proposal_error('ledger_integrity')
        original = originals[0]
        if original.get('proposal_binding') != _proposal_binding(original):
            raise _proposal_error('ledger_integrity')
        return original

    def _sync_proposal(self, handle):
        os.fsync(handle.fileno())
        directory = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def get_frozen_receipt(self, action_id: str, *, chat_id: str, platform: str) -> ActionReceipt:
        """Fail closed on damaged or ambiguous frozen-proposal history."""
        if type(action_id) is not str or not action_id.startswith('action_frozen'):
            raise _proposal_error('invalid_frozen_proposal')
        try:
            with self._exclusive(read_only=True) as handle:
                records = self._strict_records(handle)
                original = self._frozen_original(records, action_id)
                if original is None:
                    raise _proposal_error('action_not_found')
                if original.get('chat_id') != chat_id or original.get('platform') != platform:
                    raise _proposal_error('receipt_scope_mismatch')
                current = ActionReceipt.from_dict(self._fold_receipts(records)[action_id]['receipt'])
                deny_retired(current)
                return current
        except FileNotFoundError:
            raise _proposal_error('action_not_found') from None

    def record_receipt_once(self, receipt: ActionReceipt, *, chat_id: str,
                            platform: str, proposer_id: str, proposal_scope: str | None = None,
                            replaces: str | None = None) -> ActionReceipt:
        """Durably create a frozen proposal, or return its current approved state.

        The reserved action_frozen namespace cannot be created by legacy append.
        Identity, target, original payload and source channel are immutable. Times
        may differ on a retry, but a retry never appends a creation/state event.
        The parent directory must be provisioned before use. File and directory
        are synchronized before return. This neither approves nor executes it.
        """
        deny_retired(receipt)
        if (type(receipt) is not ActionReceipt or not receipt.action_id.startswith('action_frozen')
                or receipt.state not in ('proposed', 'needs_approval')
                or type(receipt.principal) is not Principal
                or receipt.principal != Principal(platform, proposer_id)
                or type(chat_id) is not str or not chat_id):
            raise _proposal_error('invalid_frozen_proposal')
        if not self.path.parent.is_dir():
            raise _proposal_error('ledger_parent_missing')
        if ((proposal_scope is not None and (type(proposal_scope) is not str
                or len(proposal_scope) != 64 or any(c not in '0123456789abcdef' for c in proposal_scope)))
                or (replaces is not None and (proposal_scope is None or type(replaces) is not str
                    or not replaces.startswith('action_frozen')))):
            raise _proposal_error('invalid_frozen_proposal')
        record = LedgerEvent(new_id('evt'), receipt.run_id, 'action_proposed', {
            'receipt': receipt.to_dict(), 'chat_id': chat_id,
            'platform': platform, 'proposer_id': proposer_id,
        }, self.clock().isoformat()).to_dict()
        # Freeze caller-owned dictionaries once, before binding or waiting.
        try:
            record = json.loads(json.dumps(record, allow_nan=False))
        except (ValueError, TypeError, RecursionError):
            raise _proposal_error('invalid_frozen_proposal') from None
        payload = record['payload']
        if proposal_scope is not None:
            payload['proposal_scope'] = proposal_scope
        if _redact_deep(payload) != payload:
            raise _proposal_error('frozen_proposal_redacted')
        try:
            binding = _proposal_binding(payload)
        except (TypeError, ValueError, RecursionError):
            raise _proposal_error('invalid_frozen_proposal') from None
        record['payload']['proposal_binding'] = binding
        with self._exclusive() as handle:
            records = self._strict_records(handle)
            original = self._frozen_original(records, receipt.action_id)
            if original is not None:
                if 'supersedes' in original:
                    record['payload']['supersedes'] = original['supersedes']
                    binding = _proposal_binding(record['payload'])
                if original['proposal_binding'] != binding:
                    raise _proposal_error('frozen_proposal_conflict')
                current = self._fold_receipts(records)[receipt.action_id]['receipt']
                # A prior attempt may have written but failed to fsync. A replay
                # cannot claim durable creation until that write is synchronized.
                self._sync_proposal(handle)
                return ActionReceipt.from_dict(current)
            if receipt.apply_payload.get('ledger_epoch', 'initial') != self._epoch(records):
                raise _proposal_error('frozen_epoch_mismatch')
            if proposal_scope is not None:
                group = [r['payload']['receipt']['action_id'] for r in records
                         if r.get('event_type') == 'action_proposed'
                         and r['payload'].get('proposal_scope') == proposal_scope
                         and r['payload']['receipt']['action_id'].startswith('action_frozen')]
                if group:
                    previous = group[-1]
                    prior = self._frozen_original(records, previous)
                    entry = self._fold_receipts(records)[previous]
                    if (replaces != previous or prior['receipt'].get('principal') != receipt.principal.to_dict()
                            or entry['receipt']['state'] not in ('proposed', 'needs_approval',
                                'ready_to_apply', 'approved_only', 'rejected', 'stale')):
                        raise _proposal_error('frozen_proposal_conflict')
                    # One authoritative JSONL event creates the new proposal AND
                    # supersedes the old approval; there is no two-append window.
                    record['payload']['supersedes'] = previous
                elif replaces is not None:
                    raise _proposal_error('frozen_proposal_conflict')
            record['payload']['proposal_binding'] = _proposal_binding(record['payload'])
            self._write_locked(handle, record)
            self._sync_proposal(handle)
            return ActionReceipt.from_dict(record['payload']['receipt'])

    def record_notification(
        self, delivery_id: str, action: str, payload: dict[str, Any]
    ) -> LedgerEvent:
        """Audit scheduler enqueue/delivery outcomes with normal deep redaction."""
        return self.append(
            LedgerEvent(
                event_id=new_id("evt"),
                run_id=f"notification:{delivery_id}",
                event_type=f"notification_{action}",
                payload=payload,
            )
        )

    def open_run(self, event: NormalizedEvent, *, executor_id: str = "") -> Run:
        """Create a Run for an inbound event and record it in the ledger."""
        run = Run(
            run_id=new_id("run"),
            source=RunSource(
                platform=event.platform,
                channel_id=event.chat_id,
                thread_id=event.thread_id or "",
                message_id=event.message_id,
            ),
            actor=RunActor(actor_id=event.sender_id),
            intent=RunIntent(raw_text=event.text),
            status="admitted",
            executor_id=executor_id,
        )
        self.append(
            LedgerEvent(
                event_id=new_id("evt"),
                run_id=run.run_id,
                event_type="run_created",
                payload={"run": run.to_dict()},
            )
        )
        return run

    def record_status(self, run_id: str, status: str, *, detail: str = "") -> LedgerEvent:
        payload: dict[str, Any] = {"status": status}
        if detail:
            payload["detail"] = detail
        event_type = (
            "run_finished"
            if status in ("succeeded", "failed", "cancelled")
            else "run_status_changed"
        )
        return self.append(
            LedgerEvent(
                event_id=new_id("evt"),
                run_id=run_id,
                event_type=event_type,
                payload=payload,
            )
        )

    def record_artifact(self, artifact: Artifact) -> LedgerEvent:
        return self.append(
            LedgerEvent(
                event_id=new_id("evt"),
                run_id=artifact.run_id,
                event_type="artifact_recorded",
                payload={"artifact": artifact.to_dict()},
            )
        )

    def _read_all(self) -> list[dict[str, Any]]:
        if self.coordination is None and not self.path.exists():
            return []
        records: list[dict[str, Any]] = []
        reader = (self._exclusive(read_only=True) if self.coordination else
                  self.path.open("r", encoding="utf-8", errors="replace"))
        with reader as handle:
            for line in handle:
                if isinstance(line, bytes):
                    line = line.decode('utf-8', errors='replace')
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return records

    def events(self) -> list[dict[str, Any]]:
        """Return every ledger record, in append order."""
        return self._read_all()

    def export_run(self, run_id: str) -> list[dict[str, Any]]:
        """Return every ledger record for a run, in append order."""
        return [record for record in self._read_all() if record.get("run_id") == run_id]

    def run_status(self, run_id: str) -> dict[str, Any]:
        """Reconstruct the latest run snapshot from ledger events."""
        records = self.export_run(run_id)
        if not records:
            raise SchemaError(
                code="run_not_found",
                message=f"No ledger events found for run_id={run_id!r}",
                recovery_hint=(
                    "Check the run id with 'ostag ledger export' or confirm the ledger path "
                    "(OSTAG_RUN_LEDGER_PATH)."
                ),
            )
        snapshot: dict[str, Any] = {}
        for record in records:
            payload = record.get("payload", {})
            if record.get("event_type") == "run_created" and "run" in payload:
                snapshot = dict(payload["run"])
            elif record.get("event_type") == "context_packet_built":
                packet = payload.get("context_packet", {})
                if packet.get("context_packet_id"):
                    snapshot["context_packet_id"] = packet["context_packet_id"]
            elif "status" in payload:
                snapshot["status"] = payload["status"]
                snapshot["updated_at"] = record.get("created_at", snapshot.get("updated_at"))
                if payload.get("detail"):
                    snapshot["last_detail"] = payload["detail"]
        snapshot["run_id"] = run_id
        snapshot["event_count"] = len(records)
        return snapshot

    def schedule_continuation(
        self,
        run_id: str,
        *,
        due_at: str,
        kind: str,
        reason: str,
        chat_id: str = "",
        platform: str = "",
        action_id: str = "",
    ) -> str:
        """Record a durable scheduled continuation; returns its id."""
        continuation_id = new_id("cont")
        payload: dict[str, Any] = {
            "continuation_id": continuation_id,
            "due_at": due_at,
            "kind": kind,
            "reason": reason,
        }
        if chat_id:
            payload["chat_id"] = chat_id
        if platform:
            payload["platform"] = platform
        if action_id:
            payload["action_id"] = action_id
        self.append(
            LedgerEvent(
                event_id=new_id("evt"),
                run_id=run_id,
                event_type="continuation_scheduled",
                payload=payload,
            )
        )
        return continuation_id

    def record_continuation_done(
        self, continuation_id: str, run_id: str, *, fired: bool, result: str
    ) -> LedgerEvent:
        return self.append(
            LedgerEvent(
                event_id=new_id("evt"),
                run_id=run_id,
                event_type="continuation_fired" if fired else "continuation_cancelled",
                payload={"continuation_id": continuation_id, "result": result[:500]},
            )
        )

    def list_continuations(self, *, pending_only: bool = True) -> list[dict[str, Any]]:
        """Continuations in schedule order; pending ones exclude fired/cancelled."""
        scheduled: dict[str, dict[str, Any]] = {}
        done: set[str] = set()
        for record in self._read_all():
            payload = record.get("payload", {})
            continuation_id = payload.get("continuation_id", "")
            if not continuation_id:
                continue
            if record.get("event_type") == "continuation_scheduled":
                scheduled[continuation_id] = dict(payload) | {"run_id": record.get("run_id", "")}
            elif record.get("event_type") in ("continuation_fired", "continuation_cancelled"):
                done.add(continuation_id)
        items = [
            entry
            for continuation_id, entry in scheduled.items()
            if not pending_only or continuation_id not in done
        ]
        items.sort(key=lambda entry: entry.get("due_at", ""))
        return items

    def due_continuations(self, now_iso: str) -> list[dict[str, Any]]:
        return [
            entry
            for entry in self.list_continuations(pending_only=True)
            if entry.get("due_at", "") <= now_iso
        ]

    def get_context_packet(self, run_id: str) -> dict[str, Any]:
        """Latest context packet recorded for a run, including its audit hash."""
        packets = [
            record["payload"]
            for record in self.export_run(run_id)
            if record.get("event_type") == "context_packet_built"
            and isinstance(record.get("payload", {}).get("context_packet"), dict)
        ]
        if not packets:
            raise SchemaError(
                code="no_context_packet",
                message=f"No context packet recorded for run_id={run_id!r}",
                recovery_hint=(
                    "Context packets are recorded for runs that reach the workflow/backend "
                    "stage; check the run with 'ostag ledger export'."
                ),
            )
        return packets[-1]

    def list_artifacts(self, run_id: str) -> list[dict[str, Any]]:
        return [
            record["payload"]["artifact"]
            for record in self.export_run(run_id)
            if record.get("event_type") == "artifact_recorded"
            and isinstance(record.get("payload", {}).get("artifact"), dict)
        ]

    def record_receipt(
        self,
        receipt: ActionReceipt,
        *,
        chat_id: str = "",
        platform: str = "",
        proposer_id: str = "",
    ) -> LedgerEvent:
        payload: dict[str, Any] = {
            "receipt": receipt.to_dict(),
            "chat_id": chat_id,
            "platform": platform,
        }
        if proposer_id:
            payload["proposer_id"] = proposer_id
        return self.append(
            LedgerEvent(
                event_id=new_id("evt"),
                run_id=receipt.run_id,
                event_type="action_proposed",
                payload=payload,
                created_at=self.clock().isoformat(),
            )
        )

    def record_receipt_state(
        self, receipt: ActionReceipt, *, previous_state: str, actor_id: str = "", detail: str = ""
    ) -> LedgerEvent:
        deny_retired(receipt)
        payload: dict[str, Any] = {
            "action_id": receipt.action_id,
            "from_state": previous_state,
            "to_state": receipt.state,
        }
        if actor_id:
            payload["actor_id"] = actor_id
        if detail:
            payload["detail"] = detail
        return self.append(
            LedgerEvent(
                event_id=new_id("evt"),
                run_id=receipt.run_id,
                event_type="action_state_changed",
                payload=payload,
                created_at=self.clock().isoformat(),
            )
        )

    def record_approval(self, decision: ApprovalDecision) -> LedgerEvent:
        return self.append(
            LedgerEvent(
                event_id=new_id("evt"),
                run_id=decision.run_id or decision.action_id,
                event_type="approval_recorded",
                payload={"decision": decision.to_dict()},
            )
        )

    def receipt_proposer(self, action_id: str) -> str:
        """Actor who proposed the action; empty for pre-tracking receipts."""
        entry = self._fold_receipts().get(action_id)
        return entry.get("proposer_id", "") if entry else ""

    def receipt_for_command(self, command_id: str) -> ActionReceipt:
        """Resolve the receipt bound to an emitted command audit event."""
        action_id = ""
        for record in self._read_all():
            if record.get("event_type") != "action_state_changed":
                continue
            detail = record.get("payload", {}).get("detail", "")
            try:
                parsed = json.loads(detail)
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(parsed, dict) and parsed.get("command_id") == command_id:
                action_id = str(record.get("payload", {}).get("action_id", ""))
        if not action_id:
            raise SchemaError(
                "command_not_found",
                f"No receipt is bound to command {command_id!r}",
                "Reject the acknowledgement and poll for an issued command.",
            )
        return self.get_receipt(action_id)

    def list_approvers(self, action_id: str) -> tuple[str, ...]:
        """Distinct actor ids that recorded an 'approve' decision for an action."""
        approvers: list[str] = []
        for record in self._read_all():
            if record.get("event_type") != "approval_recorded":
                continue
            decision = record.get("payload", {}).get("decision", {})
            if (
                decision.get("action_id") == action_id
                and decision.get("decision") == "approve"
                and decision.get("actor_id")
                and decision["actor_id"] not in approvers
            ):
                approvers.append(decision["actor_id"])
        return tuple(approvers)

    def record_cost(
        self,
        run_id: str,
        *,
        chat_id: str,
        platform: str,
        credits: int,
        backend: str,
        date: str,
    ) -> LedgerEvent:
        """Charge a run's estimated cost, attributed to a channel and day."""
        return self.append(
            LedgerEvent(
                event_id=new_id("evt"),
                run_id=run_id,
                event_type="cost_recorded",
                payload={
                    "chat_id": chat_id,
                    "platform": platform,
                    "credits": int(credits),
                    "backend": backend,
                    "date": date,
                },
            )
        )

    def cost_today(self, chat_id: str, date: str) -> int:
        """Total credits charged to a channel on a given day (UTC date string)."""
        total = 0
        for record in self._read_all():
            if record.get("event_type") != "cost_recorded":
                continue
            payload = record.get("payload", {})
            if payload.get("chat_id") == chat_id and payload.get("date") == date:
                total += int(payload.get("credits", 0))
        return total

    def record_security_event(self, run_id: str, reason: str) -> LedgerEvent:
        return self.append(
            LedgerEvent(
                event_id=new_id("evt"),
                run_id=run_id,
                event_type="security_event",
                payload={"reason": reason},
            )
        )

    def _fold_receipts(self, records=None) -> dict[str, dict[str, Any]]:
        """Reconstruct latest receipt payloads from proposed/state-changed events."""
        entries: dict[str, dict[str, Any]] = {}
        for record in self._read_all() if records is None else records:
            event_type = record.get("event_type")
            payload = record.get("payload", {})
            if _is_recovery(record):
                for action_id, entry in entries.items():
                    if action_id.startswith('action_frozen'):
                        entry['quarantined'] = True
                        entry['receipt']['state'] = 'blocked'
                        entry['receipt']['updated_at'] = record.get('created_at')
            elif event_type == "action_proposed" and isinstance(payload.get("receipt"), dict):
                action_id = payload["receipt"].get("action_id")
                if action_id:
                    receipt_payload = dict(payload["receipt"])
                    # Legacy migration is allowed only when the enclosing event
                    # supplies one unambiguous platform + proposer pair. Never
                    # infer a platform from a bare actor id.
                    if "principal" not in receipt_payload:
                        platform = payload.get("platform", "")
                        proposer_id = payload.get("proposer_id", "")
                        if platform and proposer_id:
                            try:
                                receipt_payload["principal"] = Principal(
                                    str(platform), str(proposer_id)
                                ).to_dict()
                            except ValueError:
                                receipt_payload["legacy_identity"] = "unbound"
                        else:
                            receipt_payload["legacy_identity"] = "unbound"
                    previous = entries.get(action_id)
                    if previous is not None and previous.get('retired'):
                        if records is not None:
                            raise _proposal_error('ledger_integrity')
                        continue  # lenient listing must never resurrect erased authority
                    retired = is_retired(receipt_payload)
                    if retired:
                        try:
                            RetiredCustody.from_receipt(ActionReceipt.from_dict(receipt_payload))
                        except (SchemaError, ValueError, TypeError, KeyError):
                            if records is not None:
                                raise _proposal_error('ledger_integrity') from None
                            entries.pop(action_id, None)
                            continue
                    if previous is not None:
                        old_principal = previous["receipt"].get("principal")
                        new_principal = receipt_payload.get("principal")
                        if old_principal != new_principal:
                            receipt_payload.pop("principal", None)
                            receipt_payload["legacy_identity"] = "unbound"
                    entries[action_id] = {
                        "receipt": receipt_payload,
                        "retired": retired,
                        "chat_id": payload.get("chat_id", ""),
                        "platform": payload.get("platform", ""),
                        "proposer_id": payload.get("proposer_id", ""),
                        "proposal_scope": payload.get("proposal_scope"),
                    }
                    supersedes = payload.get('supersedes')
                    if supersedes is not None and action_id.startswith('action_frozen'):
                        old = entries.get(supersedes)
                        if (old is None or not supersedes.startswith('action_frozen')
                                or not payload.get('proposal_scope')
                                or old.get('proposal_scope') != payload['proposal_scope']
                                or old['receipt'].get('principal') != receipt_payload.get('principal')
                                or payload.get('proposal_binding') != _proposal_binding(payload)):
                            if records is not None:
                                raise _proposal_error('ledger_integrity')
                            # Lenient legacy listing must not become unusable
                            # because a frozen line is corrupt. Quarantine its
                            # frozen view; execution still requires strict scan.
                            for known_id, known in entries.items():
                                if known_id.startswith('action_frozen'):
                                    known['quarantined'] = True
                                    known['receipt']['state'] = 'blocked'
                            continue
                        old['superseded_by'] = action_id
                        if not old.get('retired'):
                            old['receipt']['state'] = 'stale'
                        old['receipt']['updated_at'] = record.get('created_at')
            elif event_type == "action_state_changed":
                entry = entries.get(payload.get("action_id", ""))
                if entry and payload.get("to_state"):
                    if entry.get('retired') or entry.get('quarantined') or entry.get('superseded_by') is not None:
                        continue  # never resurrect a superseded approval
                    entry["receipt"]["state"] = payload["to_state"]
                    entry["receipt"]["updated_at"] = record.get(
                        "created_at", entry["receipt"].get("updated_at")
                    )
        return entries

    def get_receipt(
        self,
        action_id: str,
        *,
        chat_id: str | None = None,
        platform: str | None = None,
    ) -> ActionReceipt:
        entry = self._fold_receipts().get(action_id)
        if entry is None:
            raise SchemaError(
                code="action_not_found",
                message=f"No receipt found for action_id={action_id!r}",
                recovery_hint=(
                    "List pending receipts with 'ostag receipts list --json' "
                    "or check the run with 'ostag ledger export'."
                ),
            )
        if (chat_id is not None and entry["chat_id"] != chat_id) or (
            platform is not None and entry["platform"] != platform
        ):
            raise SchemaError(
                code="receipt_scope_mismatch",
                message=f"Action {action_id!r} does not belong to this source channel",
                recovery_hint="Run the command in the channel where the receipt was proposed.",
            )
        return ActionReceipt.from_dict(entry["receipt"])

    def list_receipts(
        self,
        *,
        chat_id: str | None = None,
        platform: str | None = None,
        states: tuple[str, ...] | None = None,
    ) -> list[ActionReceipt]:
        """Receipts in proposal order, optionally filtered by chat and states."""
        receipts: list[ActionReceipt] = []
        for entry in self._fold_receipts().values():
            if chat_id is not None and entry["chat_id"] != chat_id:
                continue
            if platform is not None and entry["platform"] != platform:
                continue
            receipt = ActionReceipt.from_dict(entry["receipt"])
            if states is not None and receipt.state not in states:
                continue
            receipts.append(receipt)
        receipts.sort(key=lambda item: item.created_at)
        return receipts

    def list_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        """Return the most recent run snapshots, newest first."""
        run_ids: list[str] = []
        for record in self._read_all():
            run_id = record.get("run_id")
            if run_id and run_id not in run_ids:
                run_ids.append(run_id)
        return [self.run_status(run_id) for run_id in reversed(run_ids[-limit:])]
