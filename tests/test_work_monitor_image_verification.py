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
