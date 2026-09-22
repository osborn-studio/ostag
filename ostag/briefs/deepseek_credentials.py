"""Private file credentials; no env discovery, command-line key or stdout secret."""
from __future__ import annotations

import argparse
import getpass
import os
from pathlib import Path
import stat
import sys
import warnings
from uuid import uuid4

_NAME = 'deepseek-api-key'


def _valid(key):
    return (type(key) is str and 1 <= len(key) <= 4096
            and all(33 <= ord(c) <= 126 for c in key))


def _directory(path, *, create=False):
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('absolute protected credential directory required')
    # Resolve every component relative to a held directory descriptor so an
    # ancestor symlink swap cannot redirect this process between check/open.
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for index, component in enumerate(path.parts[1:]):
            if create and index == len(path.parts) - 2:
                try:
                    os.mkdir(component, mode=0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        info = os.fstat(fd)
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise ValueError('protected credential directory required')
        return fd
    except BaseException:
        os.close(fd)
        raise


def store_key(directory: Path, key: str):
    """Create once; refuses replacement, links and non-private directories."""
    if not _valid(key):
        raise ValueError('invalid API key format')
    failure = None
    try:
        directory_fd = _directory(directory, create=True)
        temporary = '.deepseek-key-' + uuid4().hex
        created = False
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=directory_fd)
            created = True
            try:
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, 'wb', closefd=False) as output:
                    output.write(key.encode('ascii'))
                    output.flush()
                    os.fsync(fd)
            finally:
                os.close(fd)
            # Atomic publication without replacing an existing credential.
            os.link(temporary, _NAME, src_dir_fd=directory_fd, dst_dir_fd=directory_fd,
                    follow_symlinks=False)
            os.unlink(temporary, dir_fd=directory_fd)
            created = False
            os.fsync(directory_fd)
        finally:
            try:
                if created:
                    os.unlink(temporary, dir_fd=directory_fd)
            finally:
                os.close(directory_fd)
    except FileExistsError:
        failure = 'credential already present; replacement refused'
    except FileNotFoundError:
        failure = 'credential parent directory missing'
    except Exception:
        failure = 'credential filesystem or permissions unavailable; requires private modes and hardlinks'
    if failure:
        raise ValueError(failure)


def read_key(directory: Path) -> str:
    key = None
    try:
        directory_fd = _directory(directory)
        try:
            fd = os.open(_NAME, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
            try:
                before = os.fstat(fd)
                if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                        or before.st_uid != os.geteuid() or stat.S_IMODE(before.st_mode) != 0o600
                        or not 1 <= before.st_size <= 4096):
                    raise ValueError('invalid credential file')
                raw = os.read(fd, 4097)
                after = os.fstat(fd)
                if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                        after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                    raise ValueError('credential changed')
                value = raw.decode('ascii')
                if len(raw) != before.st_size or not _valid(value):
                    raise ValueError('invalid credential file')
                key = value
            finally:
                os.close(fd)
        finally:
            os.close(directory_fd)
    except Exception:
        pass
    if key is None:
        raise ValueError('protected DeepSeek credential unavailable')
    return key


def main():
    parser = argparse.ArgumentParser(description='Secure OStag DeepSeek key entry (hidden input).')
    parser.add_argument('--directory', required=True, type=Path)
    args = parser.parse_args()
    if not sys.stdin.isatty():
        print('Please run in an interactive terminal. No key was read.')
        return 1
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', getpass.GetPassWarning)
            key = getpass.getpass('DeepSeek API Key（隐藏输入，不会显示）: ')
        store_key(args.directory, key)
    except ValueError as error:
        if str(error) in {'invalid API key format', 'credential already present; replacement refused',
                'credential parent directory missing',
                'credential filesystem or permissions unavailable; requires private modes and hardlinks'}:
            print('Key setup did not complete: ' + str(error))
        else:
            print('Key setup did not complete.')
        return 1
    except (Exception, KeyboardInterrupt):
        print('Key setup did not complete. No secret or provider error was printed.')
        return 1
    print('Key saved in a private file. No model request was sent; production is not activated.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
