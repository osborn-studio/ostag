"""GET-only local CLI IO. Credentials stay in the providers' normal configuration."""
from __future__ import annotations

import json
import os
import re
import selectors
import signal
import subprocess
import time
from urllib.parse import urlencode

RESPONSE_BYTES = 1024 * 1024
TOTAL_BYTES = 16 * RESPONSE_BYTES


class CollectionError(Exception):
    """Only code-owned categories may cross the CLI output boundary."""

    def __init__(self, category):
        self.category = category
        super().__init__(category)


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError()
        result[key] = value
    return result


def decode_json(raw):
    try:
        return json.loads(raw, object_pairs_hook=_object,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError, RecursionError):
        raise CollectionError("invalid_schema") from None


def run_process(argv, *, deadline, clock=time.monotonic):
    """Stream both pipes with a shared cap; always kill the owned session and reap.

    Also handles a leader that exits while descendants retain its pipe handles.
    No shell, inherited stdin, provider stderr, or unbounded communicate buffer.
    """
    end = min(deadline, clock() + 15)
    if end <= clock():
        raise CollectionError("deadline")
    proc = None
    stdout, stderr = bytearray(), bytearray()
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, start_new_session=True)
        with selectors.DefaultSelector() as selector:
            for pipe, target in ((proc.stdout, stdout), (proc.stderr, stderr)):
                os.set_blocking(pipe.fileno(), False)
                selector.register(pipe, selectors.EVENT_READ, target)
            while selector.get_map():
                remaining = end - clock()
                if remaining <= 0:
                    raise CollectionError("timeout")
                for key, _ in selector.select(min(remaining, .05)):
                    chunk = os.read(key.fd, min(65536, RESPONSE_BYTES + 1
                                               - len(stdout) - len(stderr)))
                    if not chunk:
                        selector.unregister(key.fileobj)
                    else:
                        key.data.extend(chunk)
                        if len(stdout) + len(stderr) > RESPONSE_BYTES:
                            raise CollectionError("response_limit")
            try:
                code = proc.wait(timeout=max(.001, end - clock()))
            except subprocess.TimeoutExpired:
                raise CollectionError("timeout") from None
        if code:
            diagnostic = (stdout + stderr).lower()
            category = ("keychain_unavailable" if b"keychain" in diagnostic
                        and b"not initialized" in diagnostic else "provider_failed")
            raise CollectionError(category)
        return bytes(stdout), len(stdout) + len(stderr)
    except OSError:
        raise CollectionError("transport_unavailable") from None
    finally:
        if proc is not None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()
            proc.stdout.close()
            proc.stderr.close()


class LocalTransport:
    def __init__(self, *, clock=time.monotonic, runner=run_process):
        self.clock, self.runner, self.accepted_bytes = clock, runner, 0

    def get(self, provider, endpoint, params, deadline):
        if provider == "github":
            allowed = (endpoint == "user" or re.fullmatch(
                r"repos/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+"
                r"(?:/(?:pulls|issues|issues/comments|commits|releases)"
                r"|/commits/[A-Za-z0-9%_.~-]+)?", endpoint))
            if not allowed:
                raise CollectionError("invalid_endpoint")
            query = "?" + urlencode(params) if params else ""
            argv = ["gh", "api", "--hostname", "github.com", "--method", "GET",
                    endpoint + query]
        elif provider == "feishu" and endpoint in (
            "/open-apis/authen/v1/user_info", "/open-apis/im/v1/messages"
        ):
            argv = ["lark-cli", "api", "GET", endpoint, "--as", "user", "--params",
                    json.dumps(params, ensure_ascii=True), "--format", "json"]
        else:
            raise CollectionError("invalid_endpoint")
        # Keep capacity for final identity responses even after collection exhaustion.
        reserve = 0 if endpoint == "user" or endpoint.endswith("user_info") or (
            provider == "github" and endpoint.count("/") == 2) else 4 * RESPONSE_BYTES
        if self.accepted_bytes >= TOTAL_BYTES - reserve:
            raise CollectionError("total_response_limit")
        raw, size = self.runner(argv, deadline=deadline, clock=self.clock)
        if self.accepted_bytes + size > TOTAL_BYTES:
            raise CollectionError("total_response_limit")
        self.accepted_bytes += size
        return decode_json(raw)
