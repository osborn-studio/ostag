"""Synthetic accounting tests, never evidence of live model quality."""
from copy import deepcopy
import json
import subprocess
import sys

import pytest

from ostag.briefs.evaluation import ContractError, suite_digest, summarize


def suite():
    return {'version': 'brief-eval-suite-v1', 'cases': [
        dict(id=f'case-{i}', phase='A1' if i < 16 else 'A2',
             split='holdout' if i in (12, 13, 14, 15, 22, 23) else 'development',
             critical=i == 0, assertions=[dict(id='grounded', rubric='Every claim has evidence')])
        for i in range(24)]}


def trial(case=0, repeat=1, **updates):
    return dict(attempt_id=f'attempt-{case}-{repeat}', case_id=f'case-{case}',
                repeat=repeat, suite_sha256=suite_digest(suite()), expected_model='pinned-model',
                actual_model='pinned-model', parameters_sha256='a'*64,
                receipt_sha256='b'*64, outcome='completed', cost_micros=10,
                judgments=[dict(assertion_id='grounded', verdict='pass', evidence=['source:1'], reviewer_id='human-reviewer')],
                **{}) | updates


def complete():
    return [trial(i) for i in range(16)] + [trial(0, 2), trial(0, 3)]


def groups():
    return [dict(expected_model='pinned-model', parameters_sha256='a'*64,
                 parameters_label='frozen-prompt-v1')]


def report(records):
    return summarize(suite(), records, groups())['groups'][0]


def test_all_required_trials_are_reported_without_product_acceptance():
    result = summarize(suite(), complete(), groups())
    group = result['groups'][0]
    assert group['expected_trials'] == 18
    assert group['observed_trials'] == 18
    assert group['evidence_complete'] is True
    assert group['passed_trials'] == 18
    assert group['known_cost_micros'] == 180
    assert result['acceptance_evaluated'] is False
    assert result['scope'] == 'A1'
    assert result['user_adoption'] is None


def test_failure_and_retry_both_count_and_unknown_cost_is_not_zero():
    records = complete()
    records[0] = trial(outcome='error', actual_model=None, judgments=[], cost_micros=None)
    records.append(trial(0, 4))
    group = report(records)
    assert group['observed_trials'] == 19
    assert group['passed_trials'] == 18
    assert group['outcomes']['error'] == 1
    assert group['unknown_cost_trials'] == 1
    assert group['known_cost_micros'] == 180
    assert group['pass_fraction'] == dict(numerator=18, denominator=19)
    assert group['evidence_complete'] is True
    assert group['identity_unavailable_trials'] == 1


def test_versions_cannot_pool_coverage():
    records = complete()
    records[-1]['parameters_sha256'] = 'c'*64
    result = summarize(suite(), records, groups() + [groups()[0] | dict(parameters_sha256='c'*64)])
    assert len(result['groups']) == 2
    assert all(not g['evidence_complete'] for g in result['groups'])


def test_pending_judgment_and_abstention_never_pass():
    records = complete()
    records[0]['judgments'][0]['verdict'] = 'pending'
    records[1].update(outcome='abstained', judgments=[])
    g = report(records)
    assert g['passed_trials'] == 16
    assert g['pending_judgment_trials'] == 1
    assert not g['evidence_complete']


def test_model_identity_is_not_filled_from_expected_pin():
    for actual in (None, 'different-model'):
        g = report([trial(actual_model=actual)])
        assert g['passed_trials'] == 0
        assert g['identity_unverified_trials'] == 1
        assert not g['evidence_complete']


def test_empty_results_still_show_missing_coverage():
    result = summarize(suite(), [], groups())
    assert result['groups'][0]['missing_group'] is True
    assert result['groups'][0]['evidence_complete'] is False
    assert result['acceptance_evaluated'] is False


@pytest.mark.parametrize('mutation', [
    lambda r: r.append(deepcopy(r[0])),
    lambda r: r.append(trial(attempt_id='other')),
    lambda r: r[0].update(repeat=True),
    lambda r: r[0].update(cost_micros=True),
    lambda r: r[0].update(cost_micros=-1),
    lambda r: r[0].update(cost_micros=float('nan')),
    lambda r: r[0].update(suite_sha256='c'*64),
    lambda r: r[0].update(receipt_sha256='bad'),
    lambda r: r[0].update(case_id='unknown'),
    lambda r: r[0].update(outcome='passed'),
    lambda r: r[0]['judgments'][0].update(evidence=[]),
    lambda r: r[0]['judgments'].append(deepcopy(r[0]['judgments'][0])),
    lambda r: r[0]['judgments'][0].update(assertion_id='invented'),
    lambda r: r[0].update(extra=True),
])
def test_invalid_or_duplicate_records_rejected(mutation):
    records = [trial()]
    mutation(records)
    with pytest.raises(ContractError):
        summarize(suite(), records, groups())


@pytest.mark.parametrize('mutation', [
    lambda s: s['cases'].pop(),
    lambda s: s['cases'][0].update(id='case-1'),
    lambda s: s['cases'][0].update(critical=1),
    lambda s: s['cases'][0].update(split='holdout'),
    lambda s: s['cases'][0].update(phase='A2'),
    lambda s: s['cases'][0].update(assertions=[]),
])
def test_suite_contract_cannot_be_weakened(mutation):
    s = suite()
    mutation(s)
    with pytest.raises(ContractError):
        summarize(s, [], groups())


def test_a2_results_do_not_improve_a1_coverage():
    result = summarize(suite(), [trial(23)], groups())
    assert result['excluded_a2_trials'] == 1
    assert result['groups'][0]['missing_group'] is True
    assert result['groups'][0]['evidence_complete'] is False


def test_missing_assertions_do_not_pass():
    g = report([trial(judgments=[])])
    assert g['passed_trials'] == 0
    assert g['pending_judgment_trials'] == 1


def test_cli_reads_bounded_json_and_reports_to_stdout():
    p = subprocess.run([sys.executable, '-m', 'ostag.briefs.evaluation'],
                       input=json.dumps(dict(suite=suite(), trials=complete(), declared_groups=groups())),
                       text=True, capture_output=True)
    assert p.returncode == 0, p.stderr
    assert json.loads(p.stdout)['groups'][0]['passed_trials'] == 18


@pytest.mark.parametrize('body', ['{"suite":{},"suite":{}}', 'NaN', '['*2000])
def test_cli_malformed_json_fails_without_raw_input(body):
    p = subprocess.run([sys.executable, '-m', 'ostag.briefs.evaluation'],
                       input=body, text=True, capture_output=True)
    assert p.returncode == 2
    assert p.stdout == ''
    assert p.stderr == 'Invalid evaluation input\n'


def test_zero_record_group_and_undeclared_group_are_visible():
    declared = groups() + [groups()[0] | dict(parameters_sha256='c'*64)]
    result = summarize(suite(), complete(), declared)
    assert result['groups'][1]['missing_group']
    assert result['groups'][1]['label_hash_conflict']
    with pytest.raises(ContractError):
        summarize(suite(), [trial(parameters_sha256='c'*64)], groups())


def test_started_attempt_is_unfinished_and_pending_cost():
    record = trial(outcome='started', judgments=[], actual_model=None, cost_micros=None)
    g = report([record])
    assert not g['evidence_complete']
    assert g['outcomes']['started'] == 1
    assert g['unknown_cost_trials'] == 1
    assert g['trials'][0]['attempt_id'] == record['attempt_id']


def test_failing_judgment_is_reviewed_but_not_semantic_success():
    rows = complete()
    rows[0]['judgments'][0]['verdict'] = 'fail'
    g = report(rows)
    assert g['evidence_complete']
    assert g['passed_trials'] == 17
    assert not g['trials'][0]['passed']


def test_noncompleted_known_model_conflict_blocks_completeness():
    rows = complete()
    rows[0] = trial(outcome='error', actual_model='wrong-model', judgments=[])
    g = report(rows)
    assert g['identity_unverified_trials'] == 1
    assert not g['evidence_complete']
