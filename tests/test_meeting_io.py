"""Transport tests use local child processes and synthetic errors only."""
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from ostag.briefs.meeting_io import (
    RESPONSE_BYTES, TOTAL_BYTES, CollectionError, LocalTransport, decode_json, run_process,
)

PYTHON = sys.executable


def process(code, seconds=2):
    return run_process([PYTHON, "-c", code], deadline=time.monotonic() + seconds)


def test_success_counts_both_streams_and_uses_no_stdin():
    raw, size = process("import sys; assert sys.stdin.read()==''; print('{}'); sys.stderr.write('warn')")
    assert raw == b"{}\n" and size == 7


@pytest.mark.parametrize("code,category", [
    ("import sys; sys.stderr.write('PRIVATE token'); sys.exit(403)", "provider_failed"),
    ("import sys; sys.stderr.write('keychain Get failed: keychain not initialized SECRET'); "
     "sys.exit(1)", "keychain_unavailable"),
    ("import sys; print('keychain Get failed: keychain not initialized SECRET'); sys.exit(1)",
     "keychain_unavailable"),
])
def test_failure_static_no_raw_error_body(code, category):
    with pytest.raises(CollectionError) as caught:
        process(code)
    assert caught.value.category == category and str(caught.value) == category
    assert "SECRET" not in str(caught.value) and "PRIVATE" not in str(caught.value)


def test_missing_executable_sanitized():
    with pytest.raises(CollectionError, match="^transport_unavailable$"):
        run_process(["/nonexistent/PRIVATE"], deadline=time.monotonic() + 1)


@pytest.mark.parametrize("code", ["import time; time.sleep(5)",
                                  "import os,time; os.close(1); os.close(2); time.sleep(5)"])
def test_timeout_with_open_or_closed_pipes(code):
    start = time.monotonic()
    with pytest.raises(CollectionError, match="^timeout$"):
        process(code, .15)
    assert time.monotonic() - start < 1.5


@pytest.mark.parametrize("stream", [1, 2])
def test_output_limit_each_pipe(stream):
    with pytest.raises(CollectionError, match="^response_limit$"):
        process(f"import os; os.write({stream}, b'x'*{RESPONSE_BYTES + 1})")


def test_combined_response_limit():
    with pytest.raises(CollectionError, match="^response_limit$"):
        process(f"import os; os.write(1,b'x'*{RESPONSE_BYTES // 2}); "
                f"os.write(2,b'y'*{RESPONSE_BYTES // 2 + 1})")


def child_running(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # A killed grandchild can briefly remain a zombie owned by init on Linux.
    status = Path(f"/proc/{pid}/stat")
    try:
        if status.exists() and status.read_text().split(")", 1)[1].strip().startswith("Z"):
            return False
    except FileNotFoundError:
        return False
    return True


@pytest.mark.parametrize("mode", ["timeout", "overflow", "leader_exit", "success"])
def test_owned_descendants_killed_and_leader_reaped(tmp_path, monkeypatch, mode):
    pidfile = tmp_path / "child.pid"
    real_popen, leaders = subprocess.Popen, []
    def capture(*args, **kwargs):
        proc = real_popen(*args, **kwargs)
        leaders.append(proc)
        assert kwargs["stdin"] == subprocess.DEVNULL and kwargs["start_new_session"] is True
        assert not kwargs.get("shell", False)
        return proc
    monkeypatch.setattr("ostag.briefs.meeting_io.subprocess.Popen", capture)
    # Child retains stdout/stderr unless success, testing the exited-leader/held-pipe case.
    close = "os.close(1); os.close(2); " if mode == "success" else ""
    child_code = f"import os,time; {close}time.sleep(20)"
    code = ("import os,subprocess,time\n"
            f"p=subprocess.Popen([{PYTHON!r},'-c',{child_code!r}])\n"
            f"open({str(pidfile)!r},'w').write(str(p.pid))\n")
    if mode == "overflow":
        code += f"os.write(1,b'x'*{RESPONSE_BYTES + 1}); time.sleep(20)\n"
    elif mode == "timeout":
        code += "time.sleep(20)\n"
    elif mode == "success":
        code += "time.sleep(.1); print('{}')\n"
    if mode == "success":
        assert process(code, .8)[0] == b"{}\n"
    else:
        with pytest.raises(CollectionError, match="response_limit|timeout"):
            process(code, .5)
    monkeypatch.setattr("ostag.briefs.meeting_io.subprocess.Popen", real_popen)
    assert leaders[0].returncode is not None
    child = int(pidfile.read_text())
    deadline = time.monotonic() + 1
    while child_running(child) and time.monotonic() < deadline:
        time.sleep(.01)
    assert not child_running(child)


def test_keyboard_cancel_still_reaps(tmp_path, monkeypatch):
    real_read = os.read
    spawned = []
    real_popen = subprocess.Popen
    def capture(*a, **k):
        proc = real_popen(*a, **k)
        spawned.append(proc)
        return proc
    def cancelled(fd, amount):
        # Popen uses os.read for its internal exec error pipe before capture returns.
        if spawned:
            raise KeyboardInterrupt()
        return real_read(fd, amount)
    monkeypatch.setattr("ostag.briefs.meeting_io.subprocess.Popen", capture)
    monkeypatch.setattr("ostag.briefs.meeting_io.os.read", cancelled)
    with pytest.raises(KeyboardInterrupt):
        process("import time; print('{}',flush=True); time.sleep(10)")
    assert spawned[0].returncode == -signal.SIGKILL


@pytest.mark.parametrize("raw", [b'{"a":1,"a":2}', b'NaN', b'\xff', b'{', b'[' * 2000])
def test_json_schema_failures_sanitized(raw):
    with pytest.raises(CollectionError, match="^invalid_schema$"):
        decode_json(raw)


def test_get_only_argv_hostname_and_json_params():
    calls = []
    def runner(argv, **kwargs):
        calls.append(argv)
        return b'{}', 2
    transport = LocalTransport(runner=runner)
    transport.get("github", "repos/acme/repo/pulls", {"state": "all", "page": 1}, 100)
    transport.get("feishu", "/open-apis/im/v1/messages",
                  {"container_id": "oc_a", "page_token": "cursor/?&"}, 100)
    assert calls[0] == ["gh", "api", "--hostname", "github.com", "--method", "GET",
                        "repos/acme/repo/pulls?state=all&page=1"]
    assert calls[1][:6] == ["lark-cli", "api", "GET", "/open-apis/im/v1/messages", "--as", "user"]
    assert json.loads(calls[1][7]) == {"container_id": "oc_a", "page_token": "cursor/?&"}
    assert not any(x in ("--paginate", "--data", "POST") for call in calls for x in call)
    with pytest.raises(CollectionError, match="invalid_endpoint"):
        transport.get("feishu", "/open-apis/contact/v3/users", {}, 100)
    with pytest.raises(CollectionError, match="invalid_endpoint"):
        transport.get("github", "https://rogue.test/repos/acme/repo", {}, 100)
    assert len(calls) == 2


def test_response_budget_preserves_final_checks():
    def runner(*args, **kwargs):
        return b'{}', RESPONSE_BYTES
    transport = LocalTransport(runner=runner)
    for _ in range(12):
        transport.get("github", "repos/a/b/pulls", {}, 100)
    with pytest.raises(CollectionError, match="total_response_limit"):
        transport.get("github", "repos/a/b/pulls", {}, 100)
    for _ in range(4):
        transport.get("github", "user", {}, 100)
    assert transport.accepted_bytes == TOTAL_BYTES
    with pytest.raises(CollectionError, match="total_response_limit"):
        transport.get("github", "user", {}, 100)


def test_expired_deadline_does_not_spawn(monkeypatch):
    def forbidden(*a, **k):
        pytest.fail("expired command spawned")
    monkeypatch.setattr("ostag.briefs.meeting_io.subprocess.Popen", forbidden)
    with pytest.raises(CollectionError, match="deadline"):
        run_process(["gh"], deadline=10, clock=lambda: 11)
