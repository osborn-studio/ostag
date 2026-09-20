"""Validate planning artifacts; this does not authorize or execute backlog tasks."""
from pathlib import Path
import json
import re

root = Path(__file__).resolve().parent
plan = json.loads((root / 'backlog.json').read_text())
assert plan['kind'] == 'planning_backlog_not_osstack_queue'
tasks = plan['tasks']
ids = {task['id'] for task in tasks}
assert len(ids) == len(tasks) == plan['expected_task_count'] == 40
by_id = {task['id']: task for task in tasks}
visiting, done = set(), set()

def visit(task_id):
    assert task_id not in visiting, f'dependency cycle at {task_id}'
    if task_id in done:
        return
    visiting.add(task_id)
    for dep in by_id[task_id]['depends_on']:
        assert dep in ids, f'unknown dependency {dep}'
        visit(dep)
    visiting.remove(task_id)
    done.add(task_id)

text = (root / 'TASKS.md').read_text()
for task in tasks:
    visit(task['id'])
    assert task['status'] == 'planned'
    assert task['execution'] == 'planning_only'
    assert 1 <= len(task['files']) <= 5
    assert len(task['acceptance']) == 3
    assert task['verify'].strip()
    assert task['live_gate']
    assert f"## {task['id']} {task['title']}" in text
    for i, acceptance in enumerate(task['acceptance'], 1):
        assert f"{task['id']}-AC{i}：{acceptance}" in text
    for path in task['files']:
        assert not Path(path).is_absolute() and '..' not in Path(path).parts
        label = 'existing' if task['file_status'][path] == 'existing' else 'new'
        assert f'`{path}`（{label}）' in text

def closure(task_id):
    result = set()
    for dep in by_id[task_id]['depends_on']:
        result.add(dep)
        result.update(closure(dep))
    return result

assert {'A01', 'A02', 'A03', 'A04', 'A05', 'A06', 'A10', 'A11', 'A12', 'D01', 'D02'} <= closure('D03')
assert {'A07', 'A08', 'A09', 'A10', 'D03'} <= closure('D04')
assert {'P00', 'A02', 'A11'} <= closure('D02')
assert {'A08', 'B01'} <= closure('B02')
for task_id in ['A09', 'B02', 'B04', 'C02', 'A12']:
    assert 'A11' in closure(task_id)
assert 'A10' in closure('A12')
assert {'B01', 'B02', 'B03', 'B04', 'D04'} <= closure('D05')
assert {'C01', 'C02', 'D05'} <= closure('D06')
assert not {'P01', 'I01'} & closure('D03')
assert {'M01', 'M02', 'M03', 'M04', 'M05', 'M06', 'M07', 'D04', 'D02', 'U01'} <= closure('D07')
assert not {'B01', 'B02', 'B03', 'B04', 'C01', 'C02', 'U03'} & closure('D07')
assert {'U01', 'U02', 'U03', 'U04', 'D05'} <= closure('D08')
assert 'U02' in closure('B02')
for task in tasks:
    for path, creator in task.get('file_reuse_from', {}).items():
        assert path in task['files']
        assert creator in closure(task['id']), (task['id'], path, creator)
assert {'M05', 'D07'} <= closure('D05')
assert 'U01' in closure('A05')
delta = json.loads((root / 'upstream-delta.json').read_text())
assert len(delta['commits']) == delta['total_commits'] == 59
assert len(delta['files']) == delta['files_count'] == 199
for commit in delta['commits']:
    assert all(task_id in ids for task_id in commit['tasks'])
meeting_text = (root / 'MEETING-PREP.md').read_text()
assert {f'MC{i:02}' for i in range(1, 25)} <= set(re.findall(r'MC\d{2}', meeting_text))
for document in root.glob('*.md'):
    for target in re.findall(r'\]\(([^)]+)\)', document.read_text()):
        if '://' not in target and not target.startswith('/'):
            assert (document.parent / target.split('#')[0]).exists(), (document.name, target)
print(json.dumps({'status': 'passed', 'tasks': len(tasks), 'acceptance_items': len(tasks) * 3,
                  'acyclic': True, 'release_dependency_closures': 'passed',
                  'local_links': 'passed', 'implementation_verified': False}, indent=2))
