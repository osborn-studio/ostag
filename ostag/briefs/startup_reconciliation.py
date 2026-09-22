"""Read-only five-store reconciliation, called with exclusive custody and intake sealed.

Supports empty current schemas, unclaimed queued jobs and completed/terminal jobs
with matching retention registrations, original cards and callbacks. Never reclaims
leases, settles spend, retries sends or deletes evidence. Legacy unregistered jobs,
partial retirement and revision event chains require explicit offline repair/review.
The caller owns exclusion: file fingerprints alone do NOT make a global transaction.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
from pathlib import Path

from ostag.briefs import startup_inventory as inv
from ostag.briefs import retention, revisions
from ostag.briefs.generation import GenerationConfig

KINDS = ('brief', 'card', 'retention', 'revision', 'callback')


@dataclass(frozen=True)
class Reconciliation:
    binding: str
    persistence: str
    complete: bool
    blockers: tuple[str, ...]
    active_leases: int
    unknown_effects: int
    queued: int

    @property
    def ready(self):
        return self.complete and not self.blockers


def reconcile_startup(*, target_identities, generation_config: GenerationConfig, **arguments):
    """No constructors/migrations or business IO. Fixed public reasons only.

    All rows/columns use the inventory budgets and schema/header/alias checks.
    Each read has its own transaction; the production owner must exclude writers
    throughout both scans and joins. Unknown includes started work, even if its
    owner PID or lease has expired. Counts never imply remote cancellation.
    """
    if type(generation_config) is not GenerationConfig:
        raise ValueError('pinned generation configuration required')
    report = inv.inspect_runtime_startup(**arguments)
    failures = tuple(kind + ':' + getattr(report, kind).reason for kind in KINDS
                     if not getattr(report, kind).complete)
    persistence = hashlib.sha256(json.dumps([
        (kind, None if getattr(report, kind).fingerprint is None else
         (getattr(report, kind).fingerprint.device, getattr(report, kind).fingerprint.inode,
          getattr(report, kind).fingerprint.schema_version)) for kind in KINDS],
        separators=(',', ':')).encode()).hexdigest()
    if failures:
        return Reconciliation(report.binding, persistence, False, failures, 0, 0, 0)
    # Inventory validates all inputs before normalization or filesystem access.
    # Use its same absolute Path contract for the second, cross-reference scan.
    data = {}
    # SELECT only bounded metadata. Never pull draft bodies/source excerpts.
    columns = {
        'brief': {
            'brief_jobs': 'job_id,namespace,platform,actor_id,state,content_hash,grant_id,destination',
            'brief_outbox': 'delivery_id,job_id,revision_id,state',
        },
        'card': {'cards': 'scope,identity_json,state,message_id'},
        'retention': {'retention_jobs': 'scope,job,binding,decision,revision,delivery',
                      'retention_targets': 'scope,job,kind,identity'},
        'revision': {'revision_roots': 'scope,job'},
        'callback': {'brief_callbacks': 'binding'},
    }
    try:
        paths = {kind: Path(arguments[kind + '_path']).absolute() for kind in KINDS}
        for kind in KINDS:
            tables = {}
            def capture(db, remaining):
                for table, names in columns[kind].items():
                    names = names.split(',')
                    tables[table] = inv._fetch(db, remaining, table,
                        ["CASE WHEN " + c + " IS NULL THEN '' ELSE " +
                         inv._bounded_text(c, 8192 if c in ('binding', 'identity_json') else 1024)
                         + ' END' for c in names])
                if kind == 'brief':
                    tables['grants'] = dict(inv._rows(db, remaining, 'brief_jobs', 'job_id', 'grant_revision'))
                    tables['configs'] = inv._fetch(db, remaining, 'brief_jobs', [
                        "CASE WHEN length(CAST(payload_json AS BLOB))<=1048576 AND json_valid(payload_json) "
                        "THEN CASE WHEN json_extract(payload_json,'$.retired')=1 THEN '{}' ELSE "
                        + inv._bounded_text("json_extract(payload_json,'$.config')", 16384)
                        + ' END END'], nullable=(0,))
                if kind == 'retention':
                    tables['done'] = inv._rows(db, remaining, 'retention_targets', 'scope,job,kind', 'done')
                if kind == 'revision':
                    tables['bindings'] = inv._fetch(db, remaining, 'revision_roots', [
                        inv._bounded_text('job'), *[inv._bounded_text(
                            "json_extract(snapshot,'$." + name + "')", 8192)
                            for name in ('revision', 'outbox', 'destination', 'binding')]], nullable=(1, 2, 3, 4))
                return ()
            budget = getattr(arguments['config'], kind + '_budget')
            result = inv._scan(paths[kind], replace(getattr(report, kind).fingerprint, schema_version=None),
                budget.row_limit, budget.budget_ms, version=getattr(arguments['config'], kind + '_schema'),
                verify=lambda db: None, count=capture)
            # _scan expects a filesystem-only fingerprint, without schema version.
            if not result.complete:
                raise ValueError('changed')
            data[kind] = tables
    except Exception:
        return Reconciliation(report.binding, persistence, False, ('cross_reference_unavailable',), 0, 0, 0)

    counts = {kind: dict(getattr(report, kind).counts) for kind in KINDS}
    leases = counts['brief']['jobs.custody']
    unknown = sum(counts['brief'][k] for k in ('costs.started', 'costs.unknown',
        'deliveries.started', 'deliveries.unknown', 'reads.unsettled'))
    unknown += sum(counts['brief'].get(k, 0) for k in ('effects.begun', 'effects.unknown'))
    unknown += sum(counts['card'][k] for k in ('cards.started', 'cards.unknown'))
    blockers = set()
    try:
        configs_match = all(json.loads(raw) == generation_config.to_dict() for (raw,) in data['brief']['configs'])
    except (TypeError, ValueError):
        configs_match = False
    if not configs_match:
        blockers.add('generation_config_mismatch')
    if leases:
        blockers.add('previous_ownership')
    if unknown:
        blockers.add('unknown_remote_effect')
    if counts['brief']['costs.reserved']:
        blockers.add('unreleased_reservation')
    if counts['brief']['jobs.collecting'] or counts['brief']['jobs.generating']:
        blockers.add('unfinished_execution')
    jobs = {r[0]: r for r in data['brief']['brief_jobs']}
    boxes = {r[1]: r for r in data['brief']['brief_outbox']}
    scopes = {o.namespace + '\0' + o.principal.platform + '\0' + o.principal.actor_id: o
              for o in arguments['owners']}
    retained = {(r[0], r[1]): r for r in data['retention']['retention_jobs']}
    keys = {}
    for job, row in jobs.items():
        scope = scopes['\0'.join(row[1:4])]
        key = (retention._scope(scope), job)
        keys[job] = key
        reg = retained.get(key)
        if reg is None:
            blockers.add('missing_retention')
            continue
        binding = retention._digest([row[5], row[6], data['brief']['grants'][job], row[7]])
        if reg[2] != binding:
            blockers.add('retention_binding')
        box = boxes.get(job)
        if (reg[4], reg[5]) != (('', '') if box is None else (box[2], box[0])):
            blockers.add('retention_revision')
    if set(retained) != set(keys.values()):
        blockers.add('orphan_retention')
    targets = {}
    for scope, job, kind, identity in data['retention']['retention_targets']:
        targets.setdefault((scope, job), {})[kind] = identity
    if any(targets.get(key) != target_identities for key in retained):
        blockers.add('retention_target_identity')
    if any(r[3] != 'authorized' for r in retained.values()):
        # No silent receipt promotion: retirement verification is a separate gate.
        blockers.add('retirement_requires_verification')
    acknowledged = {}
    for scope_raw, raw, state, message in data['card']['cards']:
        try:
            identity = json.loads(raw)
            job = identity['job']
            row, box = jobs[job], boxes[job]
            if (json.loads(scope_raw) != list(row[1:4]) or identity['scope'] != list(row[1:4])
                    or identity['revision'] != box[2] or identity['outbox'] != box[0]
                    or identity['destination'] != row[7]):
                raise ValueError()
            if state == 'create_acknowledged':
                acknowledged[job] = (identity, message)
        except Exception:
            blockers.add('orphan_card')
    revision_scope = {revisions._scope(o): retention._scope(o) for o in arguments['owners']}
    for scope, job in data['revision']['revision_roots']:
        if keys.get(job) != (revision_scope.get(scope), job) or job not in boxes:
            blockers.add('orphan_revision')
    for job, revision, outbox, destination, binding in data['revision']['bindings']:
        if (job not in boxes or (revision, outbox, destination) !=
                (boxes[job][2], boxes[job][0], jobs[job][7])):
            blockers.add('revision_binding')
        if job in acknowledged:
            try:
                matches = json.loads(binding) == acknowledged[job][0]['binding']
            except (TypeError, ValueError):
                matches = False
            if not matches:
                blockers.add('revision_binding')
    if counts['revision'].get('events', 0):
        blockers.add('revision_chain_requires_verification')
    for (raw,) in data['callback']['brief_callbacks']:
        try:
            key = json.loads(raw)
            row, box = jobs[key['job']], boxes[key['job']]
            if ((key['namespace'], key['platform'], key['actor']) != row[1:4]
                    or key.get('revision') != box[2] or key.get('destination') != row[7]):
                raise ValueError()
            original = acknowledged.get(key['job'])
            if original is None or (key['binding'], key['message']) != (original[0]['binding'], original[1]):
                blockers.add('callback_without_acknowledged_card')
        except Exception:
            blockers.add('orphan_callback')
    return Reconciliation(report.binding, persistence, True, tuple(sorted(blockers)),
                          leases, unknown, counts['brief']['jobs.queued'])
