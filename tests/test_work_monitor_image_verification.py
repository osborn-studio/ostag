"""A green health endpoint alone must not pass the cold-image release gate."""
from copy import deepcopy
from pathlib import Path
import runpy

import pytest
import yaml


CHECK = runpy.run_path('scripts/verify_work_monitor_image.py')
EXPECTED = dict.fromkeys(CHECK['MODULES'], 'a' * 64)


def observed():
    return dict(uid=10001, flags=dict.fromkeys(CHECK['FLAGS'], False), modules=EXPECTED.copy(),
                files=['/var/lib/ostag/work_monitor.sqlite3'],
                status=dict(version=1, enabled=False, admission='off', lifecycle='running'))


def test_running_disabled_image_with_matching_sources():
    CHECK['validate_observation'](observed(), EXPECTED)


@pytest.mark.parametrize('change', ['missing_status', 'stopped', 'enabled', 'source', 'root',
                                   'business_database', 'numeric_flag', 'missing_flag'])
def test_unhealthy_or_mismatched_image_never_passes_health_only(change):
    value = deepcopy(observed())
    if change == 'missing_status':
        value['status'] = None
    elif change == 'stopped':
        value['status']['lifecycle'] = 'stopped'
    elif change == 'enabled':
        value['flags']['brief_enabled'] = True
    elif change == 'source':
        value['modules'][CHECK['MODULES'][0]] = 'b' * 64
    elif change == 'root':
        value['uid'] = 0
    elif change == 'business_database':
        value['files'].append('/var/lib/ostag/brief.db')
    elif change == 'numeric_flag':
        value['flags']['brief_enabled'] = 0
    else:
        value['flags'].pop('brief_meetings_enabled')
    with pytest.raises(ValueError):
        CHECK['validate_observation'](value, EXPECTED)


def test_native_image_check_is_part_of_deployment_trigger_workflow():
    workflow = yaml.load(Path('.github/workflows/ostag-ci.yml').read_text(), Loader=yaml.BaseLoader)
    image_job = workflow['jobs']['image-smoke']
    assert workflow['name'] == 'OS Tag CI'
    assert image_job['runs-on'] == 'ubuntu-latest'
    assert image_job['permissions'] == {'contents': 'read'}
    assert 'continue-on-error' not in image_job
    assert all('continue-on-error' not in step for step in image_job['steps'])
    assert any('verify_work_monitor_image.py' in step.get('run', '') for step in image_job['steps'])
    deploy = yaml.load(Path('.github/workflows/deploy-production.yml').read_text(), Loader=yaml.BaseLoader)
    assert 'OS Tag CI' in deploy['on']['workflow_run']['workflows']


def _ci_run(identifier, *, conclusion='success', status='completed', **changes):
    return dict(id=identifier, head_sha='a' * 40, head_branch='main', event='push',
                status=status, conclusion=conclusion) | changes


@pytest.mark.parametrize('runs,allowed', [
    ([_ci_run(1)], True),
    ([], False),
    ([_ci_run(1), _ci_run(2, conclusion='failure')], False),
    ([_ci_run(1), _ci_run(2, conclusion=None, status='in_progress')], False),
    ([_ci_run(2), _ci_run(1, conclusion='failure')], True),
    ([_ci_run(1, event='pull_request')], False),
    ([_ci_run(1, head_branch='feature')], False),
    ([_ci_run(1, head_sha='b' * 40)], False),
])
def test_manual_deploy_executes_latest_complete_ci_gate(tmp_path, runs, allowed):
    import json
    import os
    import subprocess

    workflow = yaml.load(Path('.github/workflows/deploy-production.yml').read_text(), Loader=yaml.BaseLoader)
    step = next(step for step in workflow['jobs']['deploy']['steps']
                if step['name'] == 'Resolve and validate release commit')
    fake = tmp_path / 'bin'
    fake.mkdir()
    git = fake / 'git'
    git.write_text('#!/bin/sh\ntest "$1 $2" = "rev-parse origin/main" || exit 2\n'
                   "printf '%s\\n' '" + 'a' * 40 + "'\n")
    gh = fake / 'gh'
    gh.write_text('#!/bin/sh\nprintf "%s\\n" "$*" > "$API_CAPTURE"\ncat "$CI_FIXTURE"\n')
    git.chmod(0o755)
    gh.chmod(0o755)
    fixture = tmp_path / 'ci.json'
    fixture.write_text(json.dumps({'workflow_runs': runs}))
    capture = tmp_path / 'api.txt'
    output = tmp_path / 'output'
    result = subprocess.run(['bash', '-c', step['run']], capture_output=True, text=True, timeout=10,
        env=os.environ | dict(PATH=str(fake) + os.pathsep + os.environ['PATH'],
            GITHUB_EVENT_NAME='workflow_dispatch', GITHUB_REPOSITORY='example/ostag',
            WORKFLOW_RUN_SHA='', INPUT_SHA='', RUNNER_TEMP=str(tmp_path), GITHUB_OUTPUT=str(output),
            CI_FIXTURE=str(fixture), API_CAPTURE=str(capture)))
    assert (result.returncode == 0) is allowed, result.stderr
    assert '/actions/workflows/ostag-ci.yml/runs?head_sha=' + 'a' * 40 in capture.read_text()
    assert 'branch=main&event=push' in capture.read_text()
    assert output.exists() is allowed


def test_process_warnings_do_not_corrupt_machine_json(capsys):
    import json
    import sys

    raw = CHECK['command']([sys.executable, '-c',
        'import sys; print("{\\"status\\":\\"ok\\"}"); print("diagnostic warning", file=sys.stderr)'])
    assert json.loads(raw) == {'status': 'ok'}
    assert 'diagnostic warning' in capsys.readouterr().err


def test_deployed_rebuild_is_verified_before_packaging_and_ssh():
    workflow = yaml.load(Path('.github/workflows/deploy-production.yml').read_text(), Loader=yaml.BaseLoader)
    steps = workflow['jobs']['deploy']['steps']
    names = [step['name'] for step in steps]
    position = names.index('Verify exact production image')
    assert names.index('Build immutable production image') < position
    assert position < names.index('Package immutable release')
    assert position < names.index('Configure production SSH identity')
    step = steps[position]
    assert 'if' not in step and 'continue-on-error' not in step
    assert step['env'] == {'DEPLOY_SHA': '${{ steps.release.outputs.sha }}',
                           'IMAGE': '${{ steps.release.outputs.image }}'}
    assert '--image "$IMAGE" --source-sha "$DEPLOY_SHA"' in step['run']
    assert 'verify_work_monitor_image.py' in step['run']
    assert 'set -euo pipefail' in step['run']
    assert steps[position + 1]['if'] == 'always()'
    assert steps[position + 1]['uses'] == 'actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02'
