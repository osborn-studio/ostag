"""Lightweight spawn targets for real status IO failure injection.

Pickled child targets must not import pytest/FastAPI/test-service fixtures before
production's readiness handshake. Heavy test-module imports can otherwise consume
the actual 5s bootstrap budget. Keep imports here standard-library-only; load the
already-bootstrapped runtime inside each serve function. No production deadlines,
IPC, IO or reaping behavior are replaced.
"""
import json
import os
import time


def _delayed_serve(connection, status_dir, control_path, boot_id, *, entered, release):
    """Spawn-safe delay at the actual rename boundary, controlled by IPC events."""
    from ostag.briefs import runtime

    original = runtime._StatusWriter.publish
    replace = runtime.os.replace

    def publish(self, lifecycle):
        def delayed(*args, **kwargs):
            entered.set()
            release.acquire()
            return replace(*args, **kwargs)

        if lifecycle == "running" and control_path.exists():
            runtime.os.replace = delayed
        try:
            return original(self, lifecycle)
        finally:
            runtime.os.replace = replace

    runtime._StatusWriter.publish = publish
    runtime._serve(connection, status_dir, control_path, boot_id)


def _failing_serve(connection, status_dir, control_path, boot_id):
    from ostag.briefs import runtime

    def denied(self, lifecycle):
        raise PermissionError("/private/secret/must/not/escape")

    runtime._StatusWriter.publish = denied
    runtime._serve(connection, status_dir, control_path, boot_id)


def _controlled_child(connection, status_dir, control_path, boot_id, *, entered, release, mode):
    """Spawn-safe barrier AFTER temp write, BEFORE final identity recheck/rename."""
    from ostag.briefs import runtime

    write = runtime.os.write
    send = runtime._send
    held = False

    def controlled_write(fd, raw):
        nonlocal held
        value = json.loads(raw)
        result = write(fd, raw)
        if not held and value['lifecycle'] == 'running' and value['control_generation'] == 2:
            held = True
            entered.set()
            if not release.acquire(timeout=4):
                raise TimeoutError
            if mode == 'failure':
                raise PermissionError('private source exception must never escape')
        return result

    def controlled_send(pipe, value):
        if (mode == 'stale' and value['phase'] == 'publish'
                and value['data']['control_generation'] == 2):
            value = value | {'request_id': value['request_id'] + 1}
        send(pipe, value)

    runtime.os.write = controlled_write
    runtime._send = controlled_send
    runtime._serve_attached(connection, status_dir, control_path, boot_id)


def _bootstrap_delay(target, args, delay, entered):
    entered.set()
    time.sleep(delay)  # Simulate interpreter/import work before the runtime target.
    target(*args)


def _bad_ready(connection, readiness, serve, status_dir, control_path, boot_id, *, mode):
    from ostag.briefs import runtime
    marker = runtime._CHILD_READY + boot_id.encode('ascii')
    payload = {
        'eof': b'', 'malformed': b'ready', 'wrong_boot': runtime._CHILD_READY + b'0' * 36,
        'extra': marker + b'!', 'partial': marker[:10],
    }[mode]
    os.write(readiness.fileno(), payload)
    if mode == 'partial':
        time.sleep(60)
    readiness.close()
    try:
        if connection.poll(2):
            serve(connection, status_dir, control_path, boot_id)
    finally:
        connection.close()


def _blocked_io(connection, status_dir, control_path, boot_id, *, attached, opening, entered):
    from ostag.briefs import runtime
    serve = runtime._serve_attached if attached else runtime._serve
    original = runtime._StatusWriter.open if opening else runtime._StatusWriter._write

    def blocked(self, *args, **kwargs):
        if opening or (args[0]['lifecycle'] == 'running'
                       and args[0]['control_generation'] == 2):
            entered.set()
            time.sleep(60)
        return original(self, *args, **kwargs)

    setattr(runtime._StatusWriter, 'open' if opening else '_write', blocked)
    serve(connection, status_dir, control_path, boot_id)
