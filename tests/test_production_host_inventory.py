"""Host observations must never grant migration or expose application credentials."""
import importlib.util
import json
from pathlib import Path
import sys

import pytest

PATH = Path(__file__).parents[1] / 'scripts/inspect_production_host.py'


def module():
    spec = importlib.util.spec_from_file_location('host_inventory', PATH)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def fixture(tmp_path):
    root = tmp_path / 'host'
    root.mkdir()
    for name in ('launcher', 'wrapper'):
        (root / name).write_text('#!/bin/sh\nexit 0\n')
    sha = 'a' * 40
    release = root / sha
    release.mkdir()
    (release / 'deploy-receipt.json').write_text(json.dumps({
        'release_sha': sha, 'image': 'ostag-work-monitor:sha-' + sha[:12],
        'credential': 'receipt-secret-must-not-leak'}))
    container = {'id': 'b' * 64, 'image_id': 'sha256:' + 'c' * 64,
        'started_at': '2026-09-22T00:00:00Z', 'restart_count': 0,
        'running': True, 'health': 'healthy', 'revision': sha,
        'mounts': [{'Type': 'bind', 'Source': '/var/lib/ostag',
                    'Destination': '/var/lib/ostag', 'RW': True}],
        'env': ['SECRET=credential-must-not-leak', 'OSTAG_BRIEF_ENABLED=false']}
    calls = []

    def run(args):
        calls.append(args)
        if args[1] == 'ps':
            return container['id'] + '\n'
        if args[1] == 'inspect':
            return json.dumps(container)
        raise AssertionError(args)

    return root, sha, container, calls, run


def test_observes_without_secrets_mutations_or_authority(tmp_path):
    m = module()
    root, sha, container, calls, run = fixture(tmp_path)
    result = m.collect(sha, root / 'launcher', root / 'wrapper', root,
                       root / 'maintenance.json', root / 'lock', run=run)
    assert result['container']['id'] == container['id']
    assert result['configured_flags']['OSTAG_BRIEF_ENABLED'] == 'false'
    assert result['configured_flags']['OSTAG_BRIEF_MEETINGS_ENABLED'] == 'unset'
    assert result['maintenance']['observation'] == 'missing'
    assert result['migration_authorized'] is False
    assert result['activation_ready'] is False
    assert result['container_observation_consistent'] is True
    assert 'secret' not in json.dumps(result).lower()
    assert all(a[1] in ('ps', 'inspect') for a in calls)
    assert not (root / 'lock').exists()


@pytest.mark.parametrize('field,value', [('revision', 'd' * 40), ('running', False)])
def test_wrong_release_or_stopped_container_denied(tmp_path, field, value):
    m = module()
    root, sha, container, _, run = fixture(tmp_path)
    container[field] = value
    with pytest.raises(m.InventoryError):
        m.collect(sha, root / 'launcher', root / 'wrapper', root,
                  root / 'maintenance.json', root / 'lock', run=run)


def test_restart_between_samples_denies_consistency(tmp_path):
    m = module()
    root, sha, container, calls, run = fixture(tmp_path)

    def changing(args):
        if args[1] == 'inspect' and any(a[1] == 'inspect' for a in calls):
            container['restart_count'] += 1
        return run(args)

    with pytest.raises(m.InventoryError):
        m.collect(sha, root / 'launcher', root / 'wrapper', root,
                  root / 'maintenance.json', root / 'lock', run=changing)


def test_unsafe_files_never_followed(tmp_path):
    m = module()
    secret = tmp_path / 'private'
    secret.write_text('never-disclose')
    link = tmp_path / 'link'
    link.symlink_to(secret)
    assert m.file_observation(link)['observation'] == 'unsafe'
    assert 'sha256' not in m.file_observation(link)
    directory = tmp_path / 'alias'
    directory.symlink_to(tmp_path, target_is_directory=True)
    assert m.file_observation(directory / 'private')['observation'] == 'unsafe'


def test_duplicate_invalid_flags_are_unknown_not_enabled():
    m = module()
    flags = m.flag_observation(['OSTAG_BRIEF_ENABLED=false', 'OSTAG_BRIEF_ENABLED=true',
                              'OSTAG_BRIEF_SALES_ENABLED=secret-value'])
    assert flags['OSTAG_BRIEF_ENABLED'] == 'invalid'
    assert flags['OSTAG_BRIEF_SALES_ENABLED'] == 'invalid'
    assert 'secret-value' not in json.dumps(flags)


def test_bounded_process_kills_output_flood():
    m = module()
    with pytest.raises(m.InventoryError, match='command unavailable'):
        m.command([sys.executable, '-c', 'import os; os.write(1,b"x"*20000)'], maximum=1024)


def test_bounded_process_kills_timeout():
    m = module()
    with pytest.raises(m.InventoryError, match='command unavailable'):
        m.command([sys.executable, '-c', 'import time; time.sleep(20)'], timeout=.1)


def test_raw_command_error_not_disclosed():
    m = module()
    with pytest.raises(m.InventoryError) as error:
        m.command([sys.executable, '-c', 'import sys; sys.stderr.write("private-token"); sys.exit(1)'])
    assert 'private-token' not in str(error.value)


def test_fifo_and_hardlink_are_not_read(tmp_path):
    import os
    m = module()
    fifo = tmp_path / 'fifo'
    os.mkfifo(fifo)
    assert m.file_observation(fifo)['observation'] == 'unsafe'
    source = tmp_path / 'source'
    source.write_text('payload')
    os.link(source, tmp_path / 'hardlink')
    assert m.file_observation(source)['observation'] == 'unsafe'


def test_multiple_containers_refused_before_inspection(tmp_path):
    m = module()
    root, sha, _, _, _ = fixture(tmp_path)
    calls = []

    def run(args):
        calls.append(args)
        return 'b' * 64 + '\n' + 'c' * 64 + '\n'

    with pytest.raises(m.InventoryError):
        m.collect(sha, root / 'launcher', root / 'wrapper', root,
                  root / 'maintenance.json', root / 'lock', run=run)
    assert len(calls) == 1


def test_output_hashes_do_not_claim_missing_receipt_valid(tmp_path):
    m = module()
    root, sha, _, _, run = fixture(tmp_path)
    (root / sha / 'deploy-receipt.json').unlink()
    result = m.collect(sha, root / 'launcher', root / 'wrapper', root,
                       root / 'maintenance.json', root / 'lock', run=run)
    assert result['release_receipt'] == {'observation': 'missing'}
    assert result['activation_ready'] is False


def test_stderr_flood_is_bounded():
    m = module()
    with pytest.raises(m.InventoryError):
        m.command([sys.executable, '-c', 'import os; os.write(2,b"s"*20000)'], maximum=1024)


def test_file_change_is_not_claimed_consistent(tmp_path):
    m = module()
    root, sha, _, calls, run = fixture(tmp_path)

    def changing(args):
        if args[1] == 'inspect' and any(a[1] == 'inspect' for a in calls):
            (root / 'launcher').write_text('new-version')
        return run(args)

    result = m.collect(sha, root / 'launcher', root / 'wrapper', root,
                       root / 'maintenance.json', root / 'lock', run=changing)
    assert result['container_observation_consistent'] is True
    assert result['file_observations_resampled'] is False
    assert 'observation_consistent' not in result
    assert result['deployment_lock_path_observation']['writer_exclusion_proven'] is False
    assert result['deployment_lock_path_observation']['lock_presence_is_not_exclusivity_evidence'] is True
    assert all(type(v) is str for v in result['configured_flags'].values())


def test_file_refusal_reasons_differ(tmp_path):
    import os
    m = module()
    f = tmp_path / 'f'
    f.write_text('value')
    alias = tmp_path / 'alias'
    alias.symlink_to(f)
    os.link(f, tmp_path / 'hard')
    assert m.file_observation(f)['reason'] == 'multiple_links'
    assert m.file_observation(alias)['reason'] == 'symlink'


def test_environment_drift_refused(tmp_path):
    m = module()
    root, sha, container, calls, run = fixture(tmp_path)

    def changing(args):
        if args[1] == 'inspect' and any(a[1] == 'inspect' for a in calls):
            container['env'].append('OSTAG_BRIEF_ENABLED=true')
        return run(args)

    with pytest.raises(m.InventoryError, match='production changed'):
        m.collect(sha, root / 'launcher', root / 'wrapper', root,
                  root / 'maintenance.json', root / 'lock', run=changing)


@pytest.mark.parametrize('field,value', [('Type', 'npipe'), ('RW', None)])
def test_unsupported_mount_refused(tmp_path, field, value):
    m = module()
    root, sha, container, _, run = fixture(tmp_path)
    container['mounts'][0][field] = value
    with pytest.raises(m.InventoryError, match='mount observation'):
        m.collect(sha, root / 'launcher', root / 'wrapper', root,
                  root / 'maintenance.json', root / 'lock', run=run)


@pytest.mark.parametrize('sha', ['short', 'A' * 40])
def test_invalid_sha_refused_before_command(tmp_path, sha):
    m = module()
    def run(args):
        pytest.fail('must refuse before Docker')
    with pytest.raises(m.InventoryError, match='exact release SHA'):
        m.collect(sha, tmp_path, tmp_path, tmp_path, tmp_path, tmp_path, run=run)


def test_nonroot_cli_refusal_is_machine_readable(monkeypatch, capsys):
    m = module()
    monkeypatch.setattr(sys, 'argv', [str(PATH), '--sha', 'a' * 40])
    monkeypatch.setattr(m.os, 'geteuid', lambda: 1000)
    with pytest.raises(SystemExit) as error:
        m.main()
    assert error.value.code == 1
    result = json.loads(capsys.readouterr().out)
    assert result['reason'] == 'trusted host administrator required'
    assert result['activation_ready'] is False


def test_error_printer_allowlists_only_fixed_reasons():
    m = module()
    assert m.failure(m.InventoryError('command unavailable'))['reason'] in m.REASONS
    for error in (m.InventoryError('private-value'), ValueError('private-value')):
        assert 'private-value' not in json.dumps(m.failure(error))
