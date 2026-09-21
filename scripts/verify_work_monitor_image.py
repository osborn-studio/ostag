#!/usr/bin/env python3
"""Cold-start verification of an exact image on a native Linux/amd64 Docker daemon.

Uses no host mounts, published ports, credentials, network or production service.
One container start only: a failed cold boot is never retried into a passing result.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid


MODULES = ('ostag.briefs.meeting_service', 'ostag.briefs.worker', 'ostag.briefs.store')
FLAGS = ('brief_enabled', 'brief_meetings_enabled', 'brief_history_enabled',
         'brief_actions_enabled', 'brief_routines_enabled', 'brief_sales_enabled')
HEALTH = """import json,urllib.request,urllib.error
try:
 r=urllib.request.urlopen('http://127.0.0.1:8000/healthz',timeout=3)
 print(json.dumps(dict(ready=True,status=r.status,body=json.load(r),brief=r.headers.get('X-OStag-Brief-Status'))))
except urllib.error.HTTPError as e:
 print(json.dumps(dict(ready=True,status=e.code,body=None,brief=None)))
except urllib.error.URLError:
 print(json.dumps(dict(ready=False)))
"""
OBSERVE = """import json,os,hashlib,time
from pathlib import Path
from ostag.config import load_settings,BRIEF_FLAGS
from ostag.briefs.runtime import validate_drain_status
import ostag.briefs.meeting_service as meeting
import ostag.briefs.worker as worker
import ostag.briefs.store as store
s=load_settings(); p=Path('/run/ostag-brief-status/status.json')
status=json.loads(p.read_text()) if p.exists() else None
print(json.dumps(dict(uid=os.getuid(),flags={k:getattr(s,k) for k,_ in BRIEF_FLAGS},status=status,
 files=[str(p) for p in Path('/var/lib/ostag').rglob('*') if p.is_file()],
 modules={m.__name__:hashlib.sha256(Path(m.__file__).read_bytes()).hexdigest() for m in (meeting,worker,store)},
 valid_drain=False if status is None else validate_drain_status(status,status['boot_id'],1,now=time.time()))))
"""


def command(args, *, timeout=30, merge_stderr=False):
    result = subprocess.run(args, text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT if merge_stderr else subprocess.PIPE,
                            timeout=timeout)
    if result.stderr:
        print(result.stderr, file=sys.stderr, end="")
    result.check_returncode()
    return result.stdout


def validate_observation(value, expected):
    """Independent packaging checks; no caller record can substitute for Docker IO."""
    flags = value.get('flags')
    if (type(value.get('uid')) is not int or value['uid'] != 10001
            or type(flags) is not dict or set(flags) != set(FLAGS)
            or any(v is not False for v in flags.values())):
        raise ValueError('image must run nonroot with every Brief flag explicitly off')
    if value.get('modules') != expected:
        raise ValueError('imported runtime source differs from checkout')
    status = value.get('status')
    if (not isinstance(status, dict) or status.get('version') != 1
            or status.get('enabled') is not False or status.get('admission') != 'off'
            or status.get('lifecycle') != 'running'):
        raise ValueError('cold boot did not publish running disabled Brief status')
    if any('brief' in Path(p).name or Path(p).name in
           ('card.db', 'retention.db', 'revision.db', 'callback.db') for p in value['files']):
        raise ValueError('all-off boot created Brief business persistence')


def verify(image, source_sha, output):
    output.mkdir(parents=True, exist_ok=True)
    receipt = dict(status='failed', source_sha=source_sha, scope='native image all-off verification',
                   production_changed=False, network='none', host_mounts=False, credentials=False)
    container = None
    try:
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/@-]{0,255}', image):
            raise ValueError('valid immutable image reference required')
        if not re.fullmatch('[0-9a-f]{40}', source_sha):
            raise ValueError('exact source commit required')
        if command(['git', 'rev-parse', 'HEAD']).strip() != source_sha:
            raise ValueError('checkout/source commit mismatch')
        daemon = json.loads(command(['docker', 'info', '--format', '{{json .}}']))
        if daemon['OSType'] != 'linux' or daemon['Architecture'] not in ('amd64', 'x86_64'):
            raise ValueError('native Linux/amd64 daemon required; emulation is not this gate')
        inspected = json.loads(command(['docker', 'image', 'inspect', image]))[0]
        if (inspected['Architecture'] != 'amd64' or inspected['Os'] != 'linux'
                or inspected['Config']['Labels'].get('org.opencontainers.image.revision') != source_sha
                or inspected['Config']['User'] != 'ostag'):
            raise ValueError('image provenance, architecture or configured user mismatch')
        receipt.update(image_id=inspected['Id'], daemon_architecture=daemon['Architecture'])
        expected = {m: hashlib.sha256(Path(*m.split('.')).with_suffix('.py').read_bytes()).hexdigest()
                    for m in MODULES}
        name = 'ostag-image-check-' + uuid.uuid4().hex
        args = ['docker', 'run', '-d', '--name', name, '--network', 'none']
        for target in ('/var/lib/ostag', '/run/ostag-brief-status', '/run/ostag-brief-control'):
            args += ['--tmpfs', target + ':rw,noexec,nosuid,size=64m,uid=10001,gid=10001,mode=0700']
        args += ['-e', 'OSTAG_MONITOR_SERVER_DB=/var/lib/ostag/work_monitor.sqlite3',
                 '-e', 'OSTAG_WORKSTREAM_STORE_PATH=/var/lib/ostag/workstreams.sqlite3', inspected['Id']]
        container = command(args).strip()
        if not re.fullmatch('[0-9a-f]{64}', container):
            container = None
            raise ValueError('invalid created-container identity')

        def inside(code):
            return json.loads(command(['docker', 'exec', container, 'python', '-c', code]))

        deadline = time.monotonic() + 90
        while True:
            health = inside(HEALTH)
            if health['ready']:
                break
            if time.monotonic() >= deadline:
                raise ValueError('cold-start health deadline')
            time.sleep(.25)
        receipt['health'] = health
        if health.get('status') != 200 or health.get('body') != {'status': 'ok'}:
            raise ValueError('unexpected Monitor health response')
        observed = inside(OBSERVE)
        receipt['initial_observation'] = observed
        validate_observation(observed, expected)
        boot = observed['status']['boot_id']

        def control(boot_id, generation):
            raw = json.dumps(dict(version=1, boot_id=boot_id, generation=generation,
                                  admission='off', maintenance=False))
            code = ("import os,json; from pathlib import Path; "
                    "p=Path('/run/ostag-brief-control/control.next'); "
                    "fd=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600); "
                    "f=os.fdopen(fd,'w'); f.write(" + repr(raw) + "); f.flush(); os.fsync(f.fileno()); "
                    "f.close(); p.replace('/run/ostag-brief-control/control.json'); print('{}')")
            inside(code)

        def observe_control(valid):
            deadline = time.monotonic() + 15
            while True:
                value = inside(OBSERVE)
                validate_observation(value, expected)
                status = value['status']
                if status['boot_id'] != boot:
                    raise ValueError('Brief boot changed during verification')
                if (status['drain_ack'] is valid and status['control_valid'] is valid
                        and (not valid or status['control_generation'] == 1)):
                    return value
                if time.monotonic() >= deadline:
                    raise ValueError('control acknowledgement deadline')
                time.sleep(.1)

        control(boot, 1)
        closed = observe_control(True)
        if not closed['valid_drain']:
            raise ValueError('fresh current-boot drain validation failed')
        receipt['closed_observation'] = closed
        control(str(uuid.uuid4()), 2)
        rejected = observe_control(False)
        if (rejected['valid_drain']
                or rejected['status']['component']['reason'] != 'control_wrong_boot'):
            raise ValueError('wrong-boot control unexpectedly proves drainage')
        receipt['wrong_boot_observation'] = rejected
        receipt['status'] = 'passed'
    except Exception as error:
        receipt['failure'] = type(error).__name__
        raise
    finally:
        original_failure = sys.exc_info()[0] is not None
        cleanup_errors = []
        if container is not None:
            for action in ('logs', 'stop', 'rm'):
                args = (['docker', 'logs', container] if action == 'logs' else
                        ['docker', 'stop', '--time', '10', container] if action == 'stop' else
                        ['docker', 'rm', '--force', container])
                try:
                    result = command(args, merge_stderr=action == 'logs')
                    if action == 'logs':
                        (output / 'container.log').write_text(result)
                except Exception as error:
                    cleanup_errors.append(dict(action=action, error=type(error).__name__))
        if cleanup_errors:
            receipt.update(status='failed', cleanup_errors=cleanup_errors)
        (output / 'receipt.json').write_text(json.dumps(receipt, indent=2) + '\n')
        if cleanup_errors and not original_failure:
            raise RuntimeError('owned image-verification container cleanup failed')
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', required=True)
    parser.add_argument('--source-sha', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = verify(args.image, args.source_sha, args.output)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
