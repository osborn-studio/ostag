"""Branch-only verification of an unaccepted candidate; never a deploy gate waiver."""
from pathlib import Path
import hashlib
import json
import os
import platform
import re
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / '.a11-verification'
MANIFEST = Path(__file__).with_name('a11_candidate_ci.json')


def verify_source(manifest):
    for name, digest in manifest['files'].items():
        assert hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == digest, name
    text = ''.join('\n--- ' + name + ' ---\n' + (ROOT / name).read_text()
                   for name in sorted(manifest['files']))
    assert hashlib.sha256(text.encode()).hexdigest() == manifest['candidate_sha256']


def main():
    OUT.mkdir(exist_ok=True)
    manifest = json.loads(MANIFEST.read_text())
    verify_source(manifest)
    assert platform.system() == 'Darwin' and sys.version_info[:2] == (3, 11)
    receipt = dict(candidate_sha256=manifest['candidate_sha256'],
                   head=subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip(),
                   platform=platform.platform(), python=sys.version,
                   scope='Full macOS fixed gate only; not model approval or live acceptance')
    (OUT / 'environment.json').write_text(json.dumps(receipt, indent=2) + '\n')
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(ROOT / 'tests'),
               str(ROOT / 'scripts/engineering')]),
               OSTAG_IPC_PROBE_OUTPUT=str(OUT / 'mac-ipc-timing.json'))
    command = [sys.executable, '-m', 'pytest', '-q', '-p', 'a11_ipc_probe',
               '-o', 'faulthandler_timeout=30', *manifest['tests']]
    log = OUT / 'pytest.log'
    with log.open('w') as output:
        result = subprocess.run(command, cwd=ROOT, env=env, stdout=output,
                                stderr=subprocess.STDOUT, timeout=1200)
    text = log.read_text()
    print(text, flush=True)
    verify_source(manifest)
    receipt.update(returncode=result.returncode, log_sha256=hashlib.sha256(log.read_bytes()).hexdigest())
    (OUT / 'result.json').write_text(json.dumps(receipt, indent=2) + '\n')
    assert result.returncode == 0, 'Required macOS gate failed'
    assert re.search(r'\b897 passed\b', text), 'Unexpected fixed gate count'
    assert not re.search(r'\b\d+ (?:skipped|xfailed|xpassed|failed|error)\b', text)


if __name__ == '__main__':
    main()
