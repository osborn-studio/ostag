import os
import stat

import pytest

from ostag.briefs.deepseek_credentials import read_key, store_key


def test_secret_roundtrip_exclusive_private(tmp_path):
    directory = tmp_path / 'secrets'
    store_key(directory, 'synthetic-not-real')
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE((directory / 'deepseek-api-key').stat().st_mode) == 0o600
    assert read_key(directory) == 'synthetic-not-real'
    with pytest.raises(ValueError):
        store_key(directory, 'replacement')
    assert read_key(directory) == 'synthetic-not-real'


@pytest.mark.parametrize('key', ['', 'with space', 'a\nb', '界', 'x'*4097])
def test_invalid_key_never_persisted(tmp_path, key):
    with pytest.raises(ValueError):
        store_key(tmp_path / 'secrets', key)
    assert not (tmp_path / 'secrets/deepseek-api-key').exists()


@pytest.mark.parametrize('kind', ['symlink', 'hardlink', 'fifo', 'public'])
def test_reader_refuses_unsafe_entries(tmp_path, kind):
    directory = tmp_path / 'secrets'
    directory.mkdir(mode=0o700)
    path = directory / 'deepseek-api-key'
    target = tmp_path / 'target'
    target.write_text('synthetic')
    target.chmod(0o600)
    if kind == 'symlink':
        path.symlink_to(target)
    elif kind == 'hardlink':
        os.link(target, path)
    elif kind == 'fifo':
        os.mkfifo(path, 0o600)
    else:
        path.write_text('synthetic')
        path.chmod(0o644)
    with pytest.raises(ValueError):
        read_key(directory)


def test_refuses_insecure_or_symlink_directory(tmp_path):
    directory = tmp_path / 'public'
    directory.mkdir(mode=0o755)
    with pytest.raises(ValueError):
        store_key(directory, 'synthetic')
    link = tmp_path / 'link'
    link.symlink_to(directory)
    with pytest.raises(ValueError):
        store_key(link, 'synthetic')


def test_deepseek_secret_redaction():
    from ostag.core.safety import redact_secrets
    assert 'private' not in redact_secrets('DEEPSEEK_API_KEY=private')


def test_parent_symlink_refused(tmp_path):
    real = tmp_path / 'real'
    real.mkdir()
    link = tmp_path / 'link'
    link.symlink_to(real)
    with pytest.raises(ValueError):
        store_key(link / 'secrets', 'synthetic')
    assert not (real / 'secrets').exists()


def test_cli_refuses_noninteractive_input(tmp_path):
    import subprocess
    import sys
    result = subprocess.run([sys.executable, '-m', 'ostag.briefs.deepseek_credentials',
        '--directory', str(tmp_path / 'secret')], input='synthetic-secret',
        text=True, capture_output=True)
    assert result.returncode == 1
    assert 'synthetic-secret' not in result.stdout + result.stderr
    assert not (tmp_path / 'secret').exists()


async def test_connectivity_check_uses_only_synthetic_bounded_data(tmp_path, monkeypatch):
    from ostag.briefs import deepseek_check
    from ostag.briefs.generation import TransportResponse
    from ostag.briefs.store import Usage
    directory = tmp_path / 'secrets'
    store_key(directory, 'synthetic-secret')
    seen = []
    class Transport:
        def __init__(self, *, api_key):
            assert api_key == 'synthetic-secret'
        async def generate(self, request):
            seen.append(request)
            return TransportResponse('{"ok":true}', 'deepseek-flash', Usage(10, 5, 9))
    monkeypatch.setattr(deepseek_check, 'DeepSeekReadOnlyTransport', Transport)
    assert await deepseek_check.check(directory) == 9
    assert seen[0].parameters.budget.max_output_tokens == 128
    assert 'synthetic-secret' not in seen[0].serialized_input


def test_failed_write_never_publishes_truncated_key(tmp_path, monkeypatch):
    directory = tmp_path / 'secrets'
    def fail(_):
        raise OSError('synthetic disk error')
    monkeypatch.setattr(os, 'fsync', fail)
    with pytest.raises(ValueError):
        store_key(directory, 'synthetic')
    assert list(directory.iterdir()) == []
