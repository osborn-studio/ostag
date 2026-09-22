#!/usr/bin/env python3
"""Read-only trusted-console inventory; observations never authorize maintenance.

Standard library only. No Docker exec, env file reads, uploads or mutations.
Run on the host, not through the restricted deployment key. Output contains
allowlisted observations; command errors and unrelated configuration are omitted.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import signal
import stat
import subprocess
import time

FLAGS = tuple('OSTAG_BRIEF_' + suffix for suffix in (
    'ENABLED', 'HISTORY_ENABLED', 'MEETINGS_ENABLED', 'ACTIONS_ENABLED',
    'ROUTINES_ENABLED', 'SALES_ENABLED'))
FORMAT = '''{"id":{{json .Id}},"image_id":{{json .Image}},
"started_at":{{json .State.StartedAt}},"restart_count":{{json .RestartCount}},
"running":{{json .State.Running}},
"health":{{if .State.Health}}{{json .State.Health.Status}}{{else}}null{{end}},
"revision":{{json (index .Config.Labels "org.opencontainers.image.revision")}},
"mounts":{{json .Mounts}},"env":{{json .Config.Env}}}'''


class InventoryError(RuntimeError):
    pass


REASONS = frozenset((
    'command unavailable', 'exact release SHA required',
    'exactly one production container required', 'container identity unavailable',
    'mount observation unavailable', 'production changed during observation',
    'trusted host administrator required', 'symlink', 'not_regular',
    'multiple_links', 'too_large', 'changed_during_read', 'unsafe file',
))


def failure(error):
    reason = str(error) if type(error) is InventoryError and str(error) in REASONS else 'observation unavailable'
    return dict(status='unavailable', reason=reason, migration_authorized=False, activation_ready=False)


def command(args, *, timeout=10, maximum=131072):
    """Bound both pipes while running; kill/reap the whole command group on error."""
    process = None
    completed = False
    try:
        process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   stdin=subprocess.DEVNULL, start_new_session=True)
        deadline, size, output = time.monotonic() + timeout, 0, bytearray()
        with selectors.DefaultSelector() as selector:
            for stream in (process.stdout, process.stderr):
                selector.register(stream, selectors.EVENT_READ)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise InventoryError('command unavailable')
                for key, _ in selector.select(remaining):
                    data = os.read(key.fileobj.fileno(), 8192)
                    if not data:
                        selector.unregister(key.fileobj)
                        continue
                    size += len(data)
                    if size > maximum:
                        raise InventoryError('command unavailable')
                    if key.fileobj is process.stdout:
                        output.extend(data)
        if process.wait(timeout=max(.001, deadline - time.monotonic())) != 0:
            raise InventoryError('command unavailable')
        completed = True
        return output.decode('utf-8')
    except (OSError, UnicodeError, subprocess.TimeoutExpired):
        raise InventoryError('command unavailable') from None
    finally:
        if process is not None:
            cleanup_failed = False
            if not completed:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except OSError:
                    cleanup_failed = True
                    try:
                        process.kill()
                    except OSError:
                        pass
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                cleanup_failed = True
            finally:
                process.stdout.close()
                process.stderr.close()
            if cleanup_failed:
                raise InventoryError('command unavailable') from None



def _read(path, maximum):
    """Walk without following directory or leaf symlinks; never create a file."""
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise InventoryError('unsafe file')
    directory = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    descriptor = None
    try:
        for part in path.parts[1:-1]:
            if stat.S_ISLNK(os.stat(part, dir_fd=directory, follow_symlinks=False).st_mode):
                raise InventoryError('symlink')
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=directory)
            os.close(directory)
            directory = child
        if stat.S_ISLNK(os.stat(path.name, dir_fd=directory, follow_symlinks=False).st_mode):
            raise InventoryError('symlink')
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                             dir_fd=directory)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise InventoryError('not_regular')
        if before.st_nlink != 1:
            raise InventoryError('multiple_links')
        if before.st_size > maximum:
            raise InventoryError('too_large')
        data = bytearray()
        while len(data) <= maximum:
            chunk = os.read(descriptor, min(8192, maximum + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        after = os.fstat(descriptor)
        current = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
        def identity(s):
            return (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
        if len(data) > maximum or identity(before) != identity(after) or identity(after) != identity(current):
            raise InventoryError('changed_during_read')
        return bytes(data), after
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(directory)


def file_observation(path, *, maximum=1048576):
    try:
        data, info = _read(path, maximum)
        return dict(observation='read', sha256=hashlib.sha256(data).hexdigest(),
                    uid=info.st_uid, mode=oct(stat.S_IMODE(info.st_mode)), bytes=len(data))
    except FileNotFoundError:
        return dict(observation='missing')
    except (OSError, InventoryError) as error:
        return dict(observation='unsafe', reason=failure(error)['reason'])


def flag_observation(env):
    result = {}
    for flag in FLAGS:
        values = [entry.partition('=')[2] for entry in env
                  if isinstance(entry, str) and entry.partition('=')[0] == flag]
        result[flag] = ('unset' if not values else
                        values[0].lower() if len(values) == 1 and
                        values[0].lower() in ('true', 'false') else 'invalid')
    return result


def _container(value, sha):
    if (type(value) is not dict or not re.fullmatch('[0-9a-f]{64}', str(value.get('id')))
            or not re.fullmatch('sha256:[0-9a-f]{64}', str(value.get('image_id')))
            or value.get('revision') != sha or value.get('running') is not True
            or type(value.get('restart_count')) is not int
            or not isinstance(value.get('started_at'), str)
            or not re.fullmatch(r'[0-9T:Z.+-]{10,40}', value['started_at'])
            or value.get('health') not in ('healthy', 'unhealthy', 'starting', None)
            or type(value.get('env')) is not list or type(value.get('mounts')) is not list):
        raise InventoryError('container identity unavailable')
    result = {k: value[k] for k in ('id', 'image_id', 'revision', 'started_at', 'restart_count', 'health')}
    mounts = []
    for mount in value['mounts']:
        if (type(mount) is not dict or mount.get('Type') not in ('bind', 'volume', 'tmpfs')
                or type(mount.get('RW')) is not bool):
            raise InventoryError('mount observation unavailable')
        item = {k: mount[k] for k in ('Type', 'RW')}
        for field in ('Source', 'Destination'):
            path = mount.get(field, '')
            if type(path) is not str or len(path) > 1024 or any(ord(c) < 32 for c in path):
                raise InventoryError('mount observation unavailable')
            item[field] = path
        mounts.append(item)
    result['mounts'] = mounts
    return result


def collect(sha, launcher, wrapper, releases, maintenance, lock, *, run=command):
    if not re.fullmatch('[0-9a-f]{40}', sha):
        raise InventoryError('exact release SHA required')
    ps = ['docker', 'ps', '--no-trunc', '--filter', 'label=com.docker.compose.project=work-monitor',
          '--filter', 'label=com.docker.compose.service=work-monitor', '--format', '{{.ID}}']
    ids = run(ps).splitlines()
    if len(ids) != 1 or not re.fullmatch('[0-9a-f]{64}', ids[0]):
        raise InventoryError('exactly one production container required')
    args = ['docker', 'inspect', '--format', FORMAT, ids[0]]
    initial = json.loads(run(args))
    container = _container(initial, sha)
    files = dict(launcher=file_observation(launcher), wrapper=file_observation(wrapper),
                 maintenance=file_observation(maintenance, maximum=4096),
                 deployment_lock_path_observation=dict(file=file_observation(lock, maximum=4096),
                     writer_exclusion_proven=False, lock_presence_is_not_exclusivity_evidence=True))
    receipt_path = Path(releases) / sha / 'deploy-receipt.json'
    receipt = file_observation(receipt_path)
    # Hash the receipt only. Its content/schema is not trusted or echoed.
    final = json.loads(run(args))
    if _container(final, sha) != container or final['env'] != initial['env'] or run(ps).splitlines() != ids:
        raise InventoryError('production changed during observation')
    return dict(schema_version=1, observed_at=datetime.now(timezone.utc).isoformat(),
                source_sha=sha, container=container, configured_flags=flag_observation(initial['env']),
                **files, release_receipt=receipt, container_observation_consistent=True, file_observations_resampled=False,
                atomic_snapshot=False, migration_authorized=False, activation_ready=False,
                effective_flags_observed=False, host_writer_exclusion_proven=False,
                authority='trusted administrator console observation only')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sha', required=True)
    args = parser.parse_args()
    try:
        if os.geteuid() != 0:
            raise InventoryError('trusted host administrator required')
        result = collect(args.sha, '/usr/local/sbin/ostag-deploy-work-monitor',
                         '/usr/local/sbin/ostag-github-actions-ssh', '/opt/ostag-releases',
                         '/var/lib/ostag-maintenance/work-monitor.json', '/run/ostag-work-monitor-deploy.lock')
        print(json.dumps(result, sort_keys=True))
    except (InventoryError, ValueError, OSError, TypeError, KeyError) as error:
        print(json.dumps(failure(error)))
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
