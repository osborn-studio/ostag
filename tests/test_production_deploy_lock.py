"""Real kernel-lock subprocess tests; every runtime command is confined to tmp_path."""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import signal
import subprocess
import sys
import tarfile
import time
from pathlib import Path

import pytest


SCRIPT = Path("scripts/deploy_work_monitor_release.sh").resolve()
BUSY = "production deploy refused: deployment lock is busy\n"
UNSAFE = "production deploy refused: deployment lock is unavailable or unsafe\n"
LINUX = pytest.mark.skipif(
    sys.platform != "linux", reason="Full deploy requires Linux stat -c and Bash mapfile"
)

# python3 delegates to the real interpreter, including the REAL lock implementation.
# Only receipt I/O is paused; Docker never executes the supplied production DB code.
FAKE = r'''
import os
import sys
import time
from pathlib import Path

root = Path(os.environ["FAKE_ROOT"])
name = Path(sys.argv[0]).name
args = sys.argv[1:]
stage = None
if name == "docker":
    if args == ["compose", "version"]:
        stage = "first_read"
    elif args[0] == "exec":
        stage = "backup" if len(args) == 7 else "database"
    elif "up" in args:
        stage = "rollback" if os.environ.get("OSTAG_WORK_MONITOR_IMAGE") == "old:image" else "switch"
    elif args[0] == "inspect" and args[-1] == "{{.Image}}":
        stage = "verify_image"
    elif args[0] == "load":
        sys.stdin.buffer.read()
elif name == "curl":
    events = (root / "events").read_text()
    stage = "rollback_health" if "rollback\n" in events else "health"
elif name == "python3" and len(args) > 2 and args[1].endswith("deploy-receipt.json"):
    stage = "receipt_" + args[2]

if name != "python3" or stage:
    with (root / "events").open("a") as log:
        log.write((stage or name) + "\n")
if stage and stage == os.environ.get("PAUSE_STAGE"):
    (root / "entered").touch()
    deadline = time.monotonic() + 15
    while not (root / "continue").exists():
        if time.monotonic() > deadline:
            sys.exit(98)
        time.sleep(0.01)
if stage and stage in os.environ.get("FAIL_STAGE", "").split(","):
    sys.exit(42)
if name == "python3":
    os.execv(os.environ["REAL_PYTHON"], [os.environ["REAL_PYTHON"], *args])
if name == "sha256sum":
    import hashlib
    print(hashlib.sha256(Path(args[0]).read_bytes()).hexdigest(), args[0])
if name == "docker":
    if args[0] == "ps":
        print("fake-container")
    elif args[0] == "inspect":
        fmt = args[-1]
        if "State.Health" in fmt:
            print("healthy")
        elif "Config.Image" in fmt:
            print("old:image")
        elif "config_files" in fmt:
            print(root / "previous.yml")
        else:
            print("sha256:fake")
    elif args[0] == "image":
        print("sha256:fake")
    elif args[0] == "exec":
        sys.stdin.read()
        print(root / "fake-backup.sqlite3" if stage == "backup" else "database integrity: ok")
'''


class Host:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.lock = self.root / "deploy.lock"
        self.maintenance = self.root / 'maintenance.json'
        self.maintenance.write_text(json.dumps(dict(schema_version=1, generation=0,
            state='open', maintenance_id=None, request_sha256=None)))
        self.maintenance.chmod(0o600)
        self.processes: list[subprocess.Popen] = []
        self.outputs = []
        for name in ("docker", "curl", "python3", "sha256sum"):
            command = self.bin / name
            command.write_text(f"#!{sys.executable}\n" + FAKE)
            command.chmod(0o700)
        (self.root / "previous.yml").write_text("services: {}\n")
        env_file = self.root / "env"
        env_file.write_text("TEST_ONLY=1\n")
        env_file.chmod(0o600)
        self.env = {
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "HOME": str(self.root),
            "REAL_PYTHON": sys.executable,
            "FAKE_ROOT": str(self.root),
            "OSTAG_DEPLOY_LOCK_PATH": str(self.lock),
            "OSTAG_MAINTENANCE_STATE_PATH": str(self.maintenance),
            "OSTAG_RELEASE_ROOT": str(self.root),
            "OSTAG_PRODUCTION_ENV_FILE": str(env_file),
            "OSTAG_LOCAL_HEALTH_URL": "http://test.invalid/never-contacted",
        }

    def args(self, command="preflight", sha="a" * 40):
        if command == "preflight":
            return [command]
        source = self.root / f"{sha}.source.gz"
        with tarfile.open(source, "w:gz") as archive:
            data = b"services: {}\n"
            member = tarfile.TarInfo("deploy/work-monitor/docker-compose.yml")
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
        image = self.root / f"{sha}.image.gz"
        image.write_bytes(gzip.compress(b"fake image"))
        return [
            "deploy", sha, str(source), hashlib.sha256(source.read_bytes()).hexdigest(),
            str(image), hashlib.sha256(image.read_bytes()).hexdigest(),
        ]

    def start(self, args=None, **environment):
        output = (self.root / f"output-{len(self.processes)}").open("w+")
        self.outputs.append(output)
        process = subprocess.Popen(
            ["/bin/bash", str(SCRIPT), *(args if args is not None else self.args())],
            env=self.env | environment, stdout=output, stderr=output, start_new_session=True,
        )
        self.processes.append(process)
        return process

    def finish(self, process):
        code = process.wait(timeout=10)
        output = self.outputs[self.processes.index(process)]
        output.seek(0)
        return code, output.read()

    def entered(self, process):
        deadline = time.monotonic() + 10
        while not (self.root / "entered").exists():
            assert process.poll() is None, self.finish(process)
            assert time.monotonic() < deadline, "fake runtime stage was never reached"
            time.sleep(0.01)

    def events(self):
        path = self.root / "events"
        return path.read_text() if path.exists() else ""

    def unblock(self):
        (self.root / "continue").touch()

    def close(self):
        for process in self.processes:
            # Always kill the group, including children left by a terminated parent.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=5)
        for output in self.outputs:
            output.close()


@pytest.fixture
def host(tmp_path):
    instance = Host(tmp_path)
    try:
        yield instance
    finally:
        instance.close()


def rejected_competitor(host, args=None):
    before = host.events()
    process = host.start(args, OSTAG_COMPOSE_PROJECT="another-project",
                         OSTAG_RELEASE_ROOT=str(host.root / "another-root"))
    code, output = host.finish(process)
    assert code != 0
    assert output == BUSY
    assert host.events() == before  # Includes Docker, curl, DB, backup and rollback.


@pytest.mark.parametrize("owner", ["preflight", "deploy"])
@pytest.mark.parametrize("competitor", ["preflight", "deploy"])
def test_independent_invocations_exclude_before_first_production_read(host, owner, competitor):
    process = host.start(host.args(owner), PAUSE_STAGE="first_read", FAIL_STAGE="first_read")
    host.entered(process)
    rejected_competitor(host, host.args(competitor, "b" * 40))
    inode = host.lock.stat().st_ino
    host.unblock()
    assert host.finish(process)[0] != 0
    assert host.lock.stat().st_ino == inode
    again = host.start(FAIL_STAGE="first_read")
    assert host.finish(again)[0] == 42
    assert host.events().count("first_read") == 2
    assert host.lock.stat().st_ino == inode


@LINUX
@pytest.mark.parametrize("stage,fail_stage,status", [
    ("database", "", None),
    ("backup", "", "deployed"),
    ("switch", "", "deployed"),
    ("receipt_ready", "", "deployed"),
    ("receipt_deployed", "", "deployed"),
    ("verify_image", "", "deployed"),
    ("rollback", "switch", "rolled_back"),
    ("receipt_rolled_back", "switch", "rolled_back"),
    ("receipt_rollback_failed", "switch,rollback_health", "rollback_failed"),
])
def test_lock_covers_database_backup_switch_rollback_and_receipts(host, stage, fail_stage, status):
    process = host.start(host.args("preflight" if status is None else "deploy"),
                         PAUSE_STAGE=stage, FAIL_STAGE=fail_stage)
    host.entered(process)
    inode = host.lock.stat().st_ino
    rejected_competitor(host)
    host.unblock()
    code, output = host.finish(process)
    assert (code == 0) == (status in (None, "deployed")), output
    if status:
        receipt = json.loads((host.root / ("a" * 40) / "deploy-receipt.json").read_text())
        assert receipt["status"] == status
    assert host.lock.stat().st_ino == inode
    assert host.finish(host.start())[0] == 0
    assert host.lock.stat().st_ino == inode


@pytest.mark.parametrize("termination", [signal.SIGTERM, signal.SIGKILL])
def test_kernel_releases_after_process_group_exit(host, termination):
    process = host.start(PAUSE_STAGE="first_read")
    host.entered(process)
    inode = host.lock.stat().st_ino
    os.killpg(process.pid, termination)
    assert host.finish(process)[0] != 0
    # A killed child may take a moment to close its inherited descriptor.
    deadline = time.monotonic() + 5
    while True:
        code, output = host.finish(host.start(FAIL_STAGE="first_read"))
        if output != BUSY:
            assert code == 42, output
            break
        assert time.monotonic() < deadline, "lock survived process group exit"
    assert host.lock.stat().st_ino == inode


@LINUX
def test_receipt_failure_releases_lock_without_removing_inode(host):
    process = host.start(host.args("deploy"), FAIL_STAGE="receipt_deployed")
    code, output = host.finish(process)
    assert code == 42, output
    assert "rollback" not in host.events()
    inode = host.lock.stat().st_ino
    assert host.finish(host.start())[0] == 0
    assert host.lock.stat().st_ino == inode


def test_parent_exit_keeps_lock_until_inflight_child_exits(host):
    process = host.start(PAUSE_STAGE="first_read", FAIL_STAGE="first_read")
    host.entered(process)
    process.kill()
    host.finish(process)
    rejected_competitor(host)
    host.unblock()
    deadline = time.monotonic() + 5
    while True:
        code, output = host.finish(host.start(FAIL_STAGE="first_read"))
        if output != BUSY:
            assert code == 42, output
            break
        assert time.monotonic() < deadline, "lock survived child exit"


def test_stale_lock_file_is_reused_without_truncation(host):
    host.lock.write_text("stale file, not a PID lease\n")
    host.lock.chmod(0o600)
    inode = host.lock.stat().st_ino
    assert host.finish(host.start(FAIL_STAGE="first_read"))[0] == 42
    assert host.lock.stat().st_ino == inode
    assert host.lock.read_text() == "stale file, not a PID lease\n"


@pytest.mark.parametrize("kind", [
    "symlink", "hardlink", "permissive", "directory", "fifo", "parent_symlink",
    "parent_writable", "parent_sticky", "relative", "dotdot", "empty", "missing_parent",
    "special_mode",
])
def test_unsafe_lock_paths_fail_closed_without_runtime_or_path_disclosure(host, kind):
    path = host.lock
    if kind in ("symlink", "hardlink"):
        target = host.root / "SECRET_DO_NOT_PRINT"
        target.write_text("secret must remain intact")
        target.chmod(0o600)
        if kind == "symlink":
            path.symlink_to(target)
        else:
            os.link(target, path)
    elif kind in ("permissive", "special_mode"):
        path.touch(mode=0o600)
        path.chmod(0o666 if kind == "permissive" else 0o1600)
    elif kind == "directory":
        path.mkdir()
    elif kind == "fifo":
        os.mkfifo(path, 0o600)
    elif kind in ("parent_symlink", "parent_writable", "parent_sticky"):
        parent = host.root / "parent"
        parent.mkdir()
        if kind == "parent_symlink":
            link = host.root / "link"
            link.symlink_to(parent, target_is_directory=True)
            parent = link
        else:
            parent.chmod(0o1777 if kind == "parent_sticky" else 0o777)
        path = parent / "lock"
    elif kind == "relative":
        path = "SECRET_DO_NOT_PRINT"
    elif kind == "dotdot":
        path = f"{host.root}/../SECRET_DO_NOT_PRINT"
    elif kind == "empty":
        path = ""
    elif kind == "missing_parent":
        path = host.root / "SECRET_DO_NOT_PRINT" / "lock"
    code, output = host.finish(host.start(OSTAG_DEPLOY_LOCK_PATH=str(path)))
    assert code != 0
    assert output == UNSAFE
    assert host.events() == ""
    if kind in ("symlink", "hardlink"):
        assert target.read_text() == "secret must remain intact"


@pytest.mark.parametrize("args,message", [
    ([], "usage:"),
    (["unknown"], "usage:"),
    (["preflight", "extra"], "usage:"),
    (["deploy"], "usage:"),
    (["deploy", "bad-sha", "source", "0" * 64, "image", "0" * 64], "release SHA"),
    (["deploy", "a" * 40, "source", "bad", "image", "0" * 64], "source checksum"),
    (["deploy", "a" * 40, "source", "0" * 64, "image", "bad"], "image checksum"),
    (["preflight", "--skip-lock"], "usage:"),
])
def test_malformed_cli_fails_before_lock_or_runtime(host, args, message):
    # An unsafe path would mask the CLI error if acquisition happened first.
    code, output = host.finish(host.start(args, OSTAG_DEPLOY_LOCK_PATH=""))
    assert code != 0
    assert message in output
    assert "deployment lock" not in output
    assert not host.lock.exists()
    assert host.events() == ""
