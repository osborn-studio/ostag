"""Reported PR closure is a meeting fact, never merge or deployment proof."""
from datetime import datetime, timedelta

import pytest

from ostag.briefs.github_change_facts import classify
from ostag.connectors.github_history import HistoryItem, canonical
from test_github_history import NOW, PRINCIPAL, pull, scope
from test_meeting_collect import Scripted, run, pr, REPO, START, END


def closure_time(start, end, boundary):
    if boundary in ('start', 'end'):
        return start if boundary == 'start' else end
    offset = 60 if boundary == 'inside' else -1
    return (datetime.fromisoformat(start.replace('Z', '+00:00'))
            + timedelta(seconds=offset)).isoformat().replace('+00:00', 'Z')


@pytest.mark.parametrize('pipeline', ['history', 'collect'])
@pytest.mark.parametrize('boundary,included', [
    ('start', True), ('inside', True), ('end', False), ('before', False),
])
def test_unmerged_closure_uses_half_open_closure_time(pipeline, boundary, included):
    if pipeline == 'history':
        frozen = scope()
        at = closure_time(frozen.request.window.start, frozen.request.window.end, boundary)
        row = pull(merged_at=None, closed_at=at, state='closed')
        facts = [f.to_dict() for f in classify(frozen, PRINCIPAL,
            HistoryItem('42', 'pulls', 'pulls', NOW, canonical(row)))]
    else:
        at = closure_time(START, END, boundary)
        fake = Scripted()
        fake.responses[f'repos/{REPO}/pulls'] = [dict(pr(), merged_at=None,
                                                   closed_at=at, state='closed')]
        facts = [f for f in run(fake, chats=())['facts'] if f['kind'].startswith('pr_')]
    closed = [f for f in facts if f['kind'] == 'pr_closed_unmerged']
    assert bool(closed) is included
    assert not any(f['kind'] == 'pr_merged' for f in facts)
    if closed:
        assert closed[0]['event_at'] == at
        assert 'closed_not_merged_or_deployed' in closed[0]['caveats']


@pytest.mark.parametrize('pipeline', ['history', 'collect'])
def test_merged_pr_is_not_also_reported_as_unmerged_closure(pipeline):
    if pipeline == 'history':
        row = pull(closed_at='2026-09-08T12:00:00Z')
        kinds = [f.kind for f in classify(scope(), PRINCIPAL,
            HistoryItem('42', 'pulls', 'pulls', NOW, canonical(row)))]
    else:
        fake = Scripted()
        fake.responses[f'repos/{REPO}/pulls'] = [dict(pr(),
            state='closed', closed_at='2026-09-15T10:00:00Z')]
        kinds = [f['kind'] for f in run(fake, chats=())['facts']]
    assert 'pr_merged' in kinds
    assert 'pr_closed_unmerged' not in kinds


def test_missing_closure_time_is_only_an_undated_observation():
    row = pull(merged_at=None, closed_at=None, state='closed')
    facts = classify(scope(), PRINCIPAL,
        HistoryItem('42', 'pulls', 'pulls', NOW, canonical(row)))
    fact = next(f for f in facts if f.kind == 'pr_closed_unmerged')
    assert fact.event_at is None and fact.source is None
    assert 'event_time_unknown_outside_temporal_source_set' in fact.caveats


def test_live_collection_with_missing_closure_time_preserves_a_gap():
    fake = Scripted()
    fake.responses[f'repos/{REPO}/pulls'] = [dict(pr(), state='closed',
                                               merged_at=None, closed_at=None)]
    result = run(fake, chats=())
    assert result['status'] != 'draft_ready'
    assert any(gap['reason'] == 'closure_time_unknown' for gap in result['gaps'])
    assert not any(f['kind'] == 'pr_closed_unmerged' for f in result['facts'])


def test_open_snapshot_does_not_claim_an_unmerged_closure():
    row = pull(state='open', merged_at=None, closed_at='2026-09-08T12:00:00Z')
    facts = classify(scope(), PRINCIPAL,
        HistoryItem('42', 'pulls', 'pulls', NOW, canonical(row)))
    assert not any(f.kind == 'pr_closed_unmerged' for f in facts)
    assert any(f.kind == 'pr_open_observed' for f in facts)


@pytest.mark.parametrize('merged_at', [False, 0, '', 'missing'])
def test_unknown_or_malformed_merge_state_cannot_claim_unmerged(merged_at):
    from ostag.briefs.meeting_io import CollectionError
    row = pull(state='closed', merged_at=merged_at, closed_at='2026-09-08T12:00:00Z')
    if merged_at == 'missing':
        del row['merged_at']
    with pytest.raises(CollectionError):
        classify(scope(), PRINCIPAL, HistoryItem('42', 'pulls', 'pulls', NOW, canonical(row)))
    fake = Scripted()
    live = dict(pr(), state='closed', merged_at=merged_at, closed_at='2026-09-15T10:00:00Z')
    if merged_at == 'missing':
        del live['merged_at']
    fake.responses[f'repos/{REPO}/pulls'] = [live]
    result = run(fake, chats=())
    assert result['status'] != 'draft_ready'
    assert not any(f['kind'] == 'pr_closed_unmerged' for f in result['facts'])
