"""Offline A1 evaluation accounting, not a semantic judge or release gate.

Inputs come from a trusted collector and independent human/adjudicator. Hashes
bind supplied records; they do not authenticate receipt contents or prove that
all model calls were supplied. No IO other than bounded CLI stdin/stdout.
"""
from __future__ import annotations

from collections import Counter
import hashlib
import json
import re
import sys


class ContractError(ValueError):
    """Malformed or inconsistent operator evidence."""


def _require(condition):
    if not condition:
        raise ContractError('invalid evaluation evidence')


def _text(value):
    _require(type(value) is str and 0 < len(value.strip()) <= 4096)
    return value


def _keys(value, keys):
    _require(type(value) is dict and set(value) == set(keys.split()))


def _list(value, maximum):
    _require(type(value) is list and len(value) <= maximum)
    return value


def _sha(value):
    _require(type(value) is str and re.fullmatch('[0-9a-f]{64}', value) is not None)


def suite_digest(suite):
    """Canonical input identity, not proof of corpus independence or quality."""
    return hashlib.sha256(json.dumps(suite, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _suite(suite):
    _keys(suite, 'version cases')
    _require(suite['version'] == 'brief-eval-suite-v1')
    cases = _list(suite['cases'], 24)
    _require(len(cases) == 24)
    by_id = {}
    for case in cases:
        _keys(case, 'id phase split critical assertions')
        cid = _text(case['id'])
        _require(cid not in by_id)
        _require(case['phase'] in ('A1', 'A2'))
        _require(case['split'] in ('development', 'holdout'))
        _require(type(case['critical']) is bool)
        assertions = _list(case['assertions'], 100)
        _require(bool(assertions))
        ids = set()
        for assertion in assertions:
            _keys(assertion, 'id rubric')
            aid = _text(assertion['id'])
            _text(assertion['rubric'])
            _require(aid not in ids)
            ids.add(aid)
        by_id[cid] = case
    _require(sum(c['split'] == 'development' for c in cases) == 18)
    a1 = [c for c in cases if c['phase'] == 'A1']
    _require(len(a1) == 16 and sum(c['split'] == 'holdout' for c in a1) >= 4)
    _require(any(c['critical'] for c in a1))
    return by_id


def summarize(suite, trials, declared_groups):
    """Report every supplied attempt, including failures and unresolved starts.

    Repeat numbers identify independent trials, not retries to replace failures.
    A retry requires a NEW number and attempt ID. Missing slots remain missing.
    Actual model identity never falls back to the expected model pin. Different
    intended models or parameter hashes have independent coverage denominators.
    """
    cases = _suite(suite)
    digest = suite_digest(suite)
    trials = _list(trials, 10000)
    groups, labels, attempts, slots = {}, {}, set(), set()
    for group in _list(declared_groups, 100):
        _keys(group, 'expected_model parameters_sha256 parameters_label')
        model = _text(group['expected_model'])
        _sha(group['parameters_sha256'])
        label = _text(group['parameters_label'])
        key = (model, group['parameters_sha256'])
        _require(key not in groups)
        groups[key], labels[key] = [], label
    _require(bool(groups))
    excluded = 0
    for trial in trials:
        _keys(trial, 'attempt_id case_id repeat suite_sha256 expected_model actual_model '
              'parameters_sha256 receipt_sha256 outcome cost_micros judgments')
        attempt = _text(trial['attempt_id'])
        _require(attempt not in attempts)
        attempts.add(attempt)
        cid = _text(trial['case_id'])
        _require(cid in cases)
        case = cases[cid]
        _require(type(trial['repeat']) is int and 1 <= trial['repeat'] <= 10000)
        _require(trial['suite_sha256'] == digest)
        model = _text(trial['expected_model'])
        if trial['actual_model'] is not None:
            _text(trial['actual_model'])
        _sha(trial['parameters_sha256'])
        _sha(trial['receipt_sha256'])
        cost = trial['cost_micros']
        _require(cost is None or (type(cost) is int and 0 <= cost <= 2**63-1))
        outcome = trial['outcome']
        _require(outcome in ('completed', 'error', 'abstained', 'started'))
        key = (model, trial['parameters_sha256'])
        _require(key in groups)
        slot = (*key, cid, trial['repeat'])
        _require(slot not in slots)
        slots.add(slot)
        assertions = {a['id'] for a in case['assertions']}
        judgments = {}
        for judgment in _list(trial['judgments'], 100):
            _keys(judgment, 'assertion_id verdict evidence reviewer_id')
            _text(judgment['reviewer_id'])
            aid = _text(judgment['assertion_id'])
            _require(aid in assertions and aid not in judgments)
            verdict = judgment['verdict']
            _require(verdict in ('pass', 'fail', 'pending'))
            evidence = _list(judgment['evidence'], 100)
            for ref in evidence:
                _text(ref)
            _require(verdict == 'pending' or bool(evidence))
            judgments[aid] = verdict
        _require(outcome == 'completed' or not judgments)
        if case['phase'] != 'A1':
            excluded += 1
            continue
        identity = trial['actual_model'] == model
        pending = outcome == 'completed' and (
            set(judgments) != assertions or 'pending' in judgments.values())
        passed = (outcome == 'completed' and identity and not pending
                  and all(v == 'pass' for v in judgments.values()))
        groups[key].append((trial, passed, pending, identity))
    a1 = {cid: c for cid, c in cases.items() if c['phase'] == 'A1'}
    required = {(cid, n) for cid, c in a1.items()
                for n in range(1, 4 if c['critical'] else 2)}
    reports = []
    for (model, params), rows in sorted(groups.items()):
        observed = {(r['case_id'], r['repeat']) for r, *_ in rows}
        missing = sorted(required - observed)
        outcomes = Counter(r['outcome'] for r, *_ in rows)
        passed = sum(p for _, p, _, _ in rows)
        pending = sum(p for _, _, p, _ in rows)
        unknown_identity = sum(not identity and (r['outcome'] == 'completed'
                               or r['actual_model'] is not None)
                               for r, _, _, identity in rows)
        reports.append(dict(
            expected_model=model, parameters_sha256=params, parameters_label=labels[(model, params)],
            missing_group=not rows,
            label_hash_conflict=any(
                (labels[k] == labels[(model, params)] and k[1] != params)
                or (k[1] == params and labels[k] != labels[(model, params)]) for k in labels),
            actual_models=sorted({r['actual_model'] for r, *_ in rows
                                  if r['actual_model'] is not None}),
            expected_trials=len(required), observed_trials=len(rows),
            missing_trials=[dict(case_id=c, repeat=n) for c, n in missing],
            outcomes={o: outcomes[o] for o in ('completed', 'error', 'abstained', 'started')},
            passed_trials=passed, pending_judgment_trials=pending,
            identity_unverified_trials=unknown_identity,
            identity_unavailable_trials=sum(r['actual_model'] is None
                                           and r['outcome'] != 'completed' for r, *_ in rows),
            pass_fraction=dict(numerator=passed, denominator=len(rows)),
            trials=[dict(attempt_id=r['attempt_id'], case_id=r['case_id'], repeat=r['repeat'],
                         outcome=r['outcome'], passed=p, pending_judgment=pending,
                         identity_verified=identity,
                         reviewer_ids=sorted({j['reviewer_id'] for j in r['judgments']}))
                    for r, p, pending, identity in rows],
            known_cost_micros=sum(r['cost_micros'] for r, *_ in rows
                                 if r['cost_micros'] is not None),
            unknown_cost_trials=sum(r['cost_micros'] is None for r, *_ in rows),
            evidence_complete=not (missing or outcomes['started'] or pending or unknown_identity),
        ))
    return dict(version='brief-eval-report-v1', suite_sha256=digest, scope='A1',
                groups=reports, excluded_a2_trials=excluded,
                excluded_a2_attempt_ids=[r['attempt_id'] for r in trials
                                         if cases[r['case_id']]['phase'] == 'A2'],
                acceptance_evaluated=False,
                limits=dict(cannot_detect_omitted_records=True, cannot_authenticate_receipts=True,
                            reviewer_independence_unverified=True, not_a_production_gate=True,
                            model_identity_operator_declared=True),
                user_adoption=None,
                limitations=['Operator-supplied evidence is not authenticated.',
                             'Omitted attempts and holdout independence cannot be detected.',
                             'No production model execution or live acceptance is inferred.'])


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ContractError('duplicate JSON key')
        result[key] = value
    return result


def main():
    try:
        raw = sys.stdin.buffer.read(4*1024*1024 + 1)
        _require(len(raw) <= 4*1024*1024)
        value = json.loads(raw, object_pairs_hook=_object,
                           parse_constant=lambda _: (_ for _ in ()).throw(ContractError()))
        _keys(value, 'suite trials declared_groups')
        result = summarize(value['suite'], value['trials'], value['declared_groups'])
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    except (ValueError, TypeError, KeyError, RecursionError, UnicodeError):
        print('Invalid evaluation input', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
