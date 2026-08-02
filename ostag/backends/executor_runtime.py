from __future__ import annotations

import asyncio
import os
import re
import shutil
import sys
from dataclasses import dataclass
from typing import Callable, Literal

from ostag.backends.base import BackendError
from ostag.config import Settings
from ostag.core.safety import scrub_environment


PromptTransport = Literal["argv", "stdin"]
ResumeMode = Literal["none", "session_id", "continue"]
ArtifactMode = Literal["none", "stdout"]
RiskMode = Literal["sandboxed", "approval_required"]

_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")
_WIRE_FIELDS = {
    "schemaVersion",
    "executorId",
    "backend",
    "command",
    "promptTransport",
    "resumeMode",
    "artifactMode",
    "timeoutSeconds",
    "riskMode",
    "enabled",
    "osstackRegistered",
}


def _strict_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a non-empty string")
    return value


@dataclass(frozen=True)
class ExecutorManifest:
    executor_id: str
    backend: str
    command: str
    prompt_transport: PromptTransport
    resume_mode: ResumeMode
    artifact_mode: ArtifactMode
    timeout_seconds: int
    risk_mode: RiskMode
    enabled: bool
    osstack_registered: bool
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("schemaVersion must be 1")
        if not _ID_RE.fullmatch(self.executor_id):
            raise ValueError("executorId must be a bounded snake_case identifier")
        if not _ID_RE.fullmatch(self.backend):
            raise ValueError("backend must be a bounded snake_case identifier")
        if not self.command or "\x00" in self.command or "\n" in self.command:
            raise ValueError("command must be a non-empty single-line string")
        if self.prompt_transport not in ("argv", "stdin"):
            raise ValueError("promptTransport must be argv or stdin")
        if self.resume_mode not in ("none", "session_id", "continue"):
            raise ValueError("resumeMode must be none, session_id, or continue")
        if self.artifact_mode not in ("none", "stdout"):
            raise ValueError("artifactMode must be none or stdout")
        if type(self.timeout_seconds) is not int or not 1 <= self.timeout_seconds <= 3600:
            raise ValueError("timeoutSeconds must be an integer in 1..3600")
        if self.risk_mode not in ("sandboxed", "approval_required"):
            raise ValueError("riskMode must be sandboxed or approval_required")
        if type(self.enabled) is not bool or type(self.osstack_registered) is not bool:
            raise ValueError("enabled and osstackRegistered must be booleans")

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> ExecutorManifest:
        if not isinstance(value, dict):
            raise ValueError("executor manifest must be an object")
        fields = set(value)
        unknown = fields - _WIRE_FIELDS
        missing = _WIRE_FIELDS - fields
        if unknown:
            raise ValueError(f"executor manifest has unknown fields: {sorted(unknown)}")
        if missing:
            raise ValueError(f"executor manifest is missing fields: {sorted(missing)}")
        schema_version = value["schemaVersion"]
        timeout_seconds = value["timeoutSeconds"]
        if type(schema_version) is not int or type(timeout_seconds) is not int:
            raise ValueError("schemaVersion and timeoutSeconds must be integers")
        enabled = value["enabled"]
        osstack_registered = value["osstackRegistered"]
        if type(enabled) is not bool or type(osstack_registered) is not bool:
            raise ValueError("enabled and osstackRegistered must be booleans")
        return cls(
            schema_version=schema_version,
            executor_id=_strict_string(value["executorId"], "executorId"),
            backend=_strict_string(value["backend"], "backend"),
            command=_strict_string(value["command"], "command"),
            prompt_transport=_strict_string(  # type: ignore[arg-type]
                value["promptTransport"], "promptTransport"
            ),
            resume_mode=_strict_string(value["resumeMode"], "resumeMode"),  # type: ignore[arg-type]
            artifact_mode=_strict_string(  # type: ignore[arg-type]
                value["artifactMode"], "artifactMode"
            ),
            timeout_seconds=timeout_seconds,
            risk_mode=_strict_string(value["riskMode"], "riskMode"),  # type: ignore[arg-type]
            enabled=enabled,
            osstack_registered=osstack_registered,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schemaVersion": self.schema_version,
            "executorId": self.executor_id,
            "backend": self.backend,
            "command": self.command,
            "promptTransport": self.prompt_transport,
            "resumeMode": self.resume_mode,
            "artifactMode": self.artifact_mode,
            "timeoutSeconds": self.timeout_seconds,
            "riskMode": self.risk_mode,
            "enabled": self.enabled,
            "osstackRegistered": self.osstack_registered,
        }


CommandBuilder = Callable[[str, str | None], list[str]]


@dataclass(frozen=True)
class ExecutorProfile:
    manifest: ExecutorManifest
    command_builder: CommandBuilder
    env_scrub_extra: tuple[str, ...] = ()

    def build_argv(self, prompt: str, resume_session_id: str | None = None) -> list[str]:
        if not isinstance(prompt, str) or not prompt:
            raise ValueError("executor prompt must be a non-empty string")
        if resume_session_id is not None and self.manifest.resume_mode == "none":
            raise ValueError("executor does not declare resume support")
        argv = self.command_builder(prompt, resume_session_id)
        _validate_argv(argv, self.manifest.command)
        return argv


def _validate_argv(argv: list[str], expected_command: str) -> None:
    if not isinstance(argv, list) or not argv or argv[0] != expected_command:
        raise ValueError("executor argv must start with its declared command")
    if len(argv) > 64:
        raise ValueError("executor argv has too many arguments")
    for argument in argv:
        if not isinstance(argument, str) or not argument or "\x00" in argument:
            raise ValueError("executor argv contains an invalid argument")
        if len(argument) > 100_000:
            raise ValueError("executor argv argument is too large")


async def _run_process(
    argv: list[str],
    *,
    stdin: bytes | None,
    timeout_seconds: float,
    max_output_bytes: int,
    env_scrub_extra: tuple[str, ...],
    environment: dict[str, str] | None = None,
) -> str:
    proc = await asyncio.create_subprocess_exec(
        *argv,
        env=scrub_environment(environment, extra=env_scrub_extra),
        stdin=asyncio.subprocess.PIPE if stdin is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    total_bytes = 0

    async def read_bounded(stream: asyncio.StreamReader) -> bytes:
        nonlocal total_bytes
        chunks = []
        while chunk := await stream.read(65_536):
            total_bytes += len(chunk)
            if total_bytes > max_output_bytes:
                raise BackendError("Executor output exceeded the configured limit.")
            chunks.append(chunk)
        return b"".join(chunks)

    async def exchange() -> tuple[bytes, bytes]:
        if stdin is not None and proc.stdin is not None:
            try:
                proc.stdin.write(stdin)
                await proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                proc.stdin.close()
        assert proc.stdout is not None and proc.stderr is not None
        stdout, stderr = await asyncio.gather(
            read_bounded(proc.stdout), read_bounded(proc.stderr)
        )
        await proc.wait()
        return stdout, stderr

    try:
        stdout, stderr = await asyncio.wait_for(exchange(), timeout=timeout_seconds)
    except TimeoutError as exc:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
        raise BackendError(f"Executor timed out after {timeout_seconds:g}s.") from exc
    except BackendError:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
        raise
    if proc.returncode != 0:
        raise BackendError(f"Executor exited with status {proc.returncode}.")
    return stdout.decode("utf-8", errors="replace").strip()


class ExecutorRuntime:
    def __init__(self, profile: ExecutorProfile, *, max_output_bytes: int):
        if max_output_bytes < 1:
            raise ValueError("max_output_bytes must be positive")
        self.profile = profile
        self.max_output_bytes = max_output_bytes

    async def run(self, prompt: str, *, resume_session_id: str | None = None) -> str:
        manifest = self.profile.manifest
        if not manifest.enabled:
            raise BackendError(f"Executor {manifest.executor_id} is disabled.")
        argv = self.profile.build_argv(prompt, resume_session_id)
        stdin = prompt.encode("utf-8") if manifest.prompt_transport == "stdin" else None
        output = await _run_process(
            argv,
            stdin=stdin,
            timeout_seconds=manifest.timeout_seconds,
            max_output_bytes=self.max_output_bytes,
            env_scrub_extra=self.profile.env_scrub_extra,
        )
        return output or "Executor finished without output."


@dataclass(frozen=True)
class ConformanceCheck:
    name: str
    ok: bool
    detail: str

    def to_dict(self) -> dict[str, object]:
        return {"name": self.name, "ok": self.ok, "detail": self.detail}


@dataclass(frozen=True)
class ConformanceReport:
    executor_id: str
    manifest: ExecutorManifest
    checks: tuple[ConformanceCheck, ...]

    @property
    def conformant(self) -> bool:
        return all(check.ok for check in self.checks)

    def to_dict(self) -> dict[str, object]:
        manifest = self.manifest.to_dict()
        manifest["command"] = os.path.basename(self.manifest.command) or "configured-command"
        return {
            "executorId": self.executor_id,
            "conformant": self.conformant,
            "activation": "enabled" if self.manifest.enabled else "disabled",
            "manifest": manifest,
            "checks": [check.to_dict() for check in self.checks],
        }


async def run_conformance_suite(profile: ExecutorProfile) -> ConformanceReport:
    checks: list[ConformanceCheck] = []
    try:
        ExecutorManifest.from_dict(profile.manifest.to_dict())
        checks.append(ConformanceCheck("manifest", True, "valid schema v1"))
    except ValueError:
        checks.append(ConformanceCheck("manifest", False, "invalid manifest"))

    try:
        injection = "literal; $(touch should-not-run)"
        argv = profile.build_argv(injection)
        expected_count = 1 if profile.manifest.prompt_transport == "argv" else 0
        checks.append(ConformanceCheck("argv", argv.count(injection) == expected_count, "direct argv execution"))
    except ValueError:
        checks.append(ConformanceCheck("argv", False, "invalid argv builder"))

    environment_ok = False
    environment_script = (
        "import os; print('clean' if os.getenv('FEISHU_APP_SECRET') is None else 'leaked')"
    )
    try:
        output = await _run_process(
            [sys.executable, "-c", environment_script],
            stdin=None,
            timeout_seconds=2,
            max_output_bytes=1024,
            env_scrub_extra=profile.env_scrub_extra,
            environment={**os.environ, "FEISHU_APP_SECRET": "conformance-only"},
        )
        environment_ok = output == "clean"
    except BackendError:
        pass
    checks.append(
        ConformanceCheck(
            "environment", environment_ok, "platform credentials scrubbed" if environment_ok else "scrub failed"
        )
    )

    timeout_ok = False
    try:
        await _run_process(
            [sys.executable, "-c", "import time; time.sleep(1)"],
            stdin=None,
            timeout_seconds=0.02,
            max_output_bytes=1024,
            env_scrub_extra=(),
        )
    except BackendError as exc:
        timeout_ok = "timed out" in str(exc)
    checks.append(
        ConformanceCheck("timeout", timeout_ok, "child terminated" if timeout_ok else "timeout failed")
    )

    try:
        if profile.manifest.resume_mode == "none":
            try:
                profile.build_argv("resume-check", "session_123")
            except ValueError:
                resume_ok = True
            else:
                resume_ok = False
        else:
            profile.build_argv("resume-check", "session_123")
            resume_ok = True
    except ValueError:
        resume_ok = False
    checks.append(
        ConformanceCheck(
            "resume", resume_ok, "declaration enforced" if resume_ok else "declaration mismatch"
        )
    )

    available = shutil.which(profile.manifest.command) is not None
    checks.append(
        ConformanceCheck(
            "executable", available, "command available" if available else "command unavailable"
        )
    )
    return ConformanceReport(profile.manifest.executor_id, profile.manifest, tuple(checks))


def executor_profiles(config: Settings) -> dict[str, ExecutorProfile]:
    from ostag.backends.claude_cli import build_claude_command
    from ostag.backends.codex_cli import build_codex_command
    from ostag.backends.kimi_cli import build_kimi_command

    registered = frozenset(config.executor_osstack_registered)
    common_enabled = config.executor_runtime_enabled
    return {
        "codex_cli": ExecutorProfile(
            ExecutorManifest(
                executor_id="codex_cli",
                backend="codex_cli",
                command=config.codex_bin,
                prompt_transport="argv",
                resume_mode="none",
                artifact_mode="stdout",
                timeout_seconds=config.agent_timeout_seconds,
                risk_mode="sandboxed",
                enabled=common_enabled,
                osstack_registered="codex_cli" in registered,
            ),
            lambda prompt, resume: build_codex_command(config, prompt),
            config.env_scrub_extra,
        ),
        "claude_cli": ExecutorProfile(
            ExecutorManifest(
                executor_id="claude_cli",
                backend="claude_cli",
                command=config.claude_bin,
                prompt_transport="stdin",
                resume_mode="none",
                artifact_mode="stdout",
                timeout_seconds=config.agent_timeout_seconds,
                risk_mode="approval_required",
                enabled=common_enabled,
                osstack_registered="claude_cli" in registered,
            ),
            lambda prompt, resume: build_claude_command(config),
            config.env_scrub_extra,
        ),
        "kimi_cli": ExecutorProfile(
            ExecutorManifest(
                executor_id="kimi_cli",
                backend="kimi_cli",
                command=config.kimi_bin,
                prompt_transport="argv",
                resume_mode="session_id",
                artifact_mode="stdout",
                timeout_seconds=config.agent_timeout_seconds,
                risk_mode="approval_required",
                enabled=common_enabled and config.kimi_executor_enabled,
                osstack_registered="kimi_cli" in registered,
            ),
            lambda prompt, resume: build_kimi_command(
                config, prompt, resume_session_id=resume
            ),
            config.env_scrub_extra,
        ),
    }
