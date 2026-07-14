"""Bounded subprocess adapter for the local osstack session CLI."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from .models import RelayEnvelope
from .redaction import sanitize_assistant_text


class OsstackPortError(RuntimeError):
    pass


Runner = Callable[[list[str], float], subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class OsstackSession:
    session_id: str
    task_id: str
    run_id: str
    agent_id: str
    state: str
    resume_supported: bool
    resume_ready: bool
    version: int
    updated_at: float


_SESSION_KEYS = {
    "session_id", "task_id", "run_id", "agent_id", "state", "resume_supported",
    "resume_ready", "version", "updated_at",
}
_SNAPSHOT_KEYS = {
    "session_id", "task_id", "run_id", "agent_id", "state", "version", "turn_id",
    "assistant_text", "decision", "updated_at",
}
_START_KEYS = _SESSION_KEYS | {"turn_id", "assistant_text", "decision", "events"}


def _default_runner(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        argv, text=True, capture_output=True, timeout=timeout, check=False
    )


class OsstackSessionPort:
    def __init__(
        self,
        *,
        binary: str = "osstack",
        runner: Runner | None = None,
        timeout_seconds: float = 120,
        default_repo: str = "",
    ) -> None:
        self.binary = binary
        self.runner = runner or _default_runner
        self.timeout_seconds = timeout_seconds
        self.default_repo = default_repo

    def _run(self, argv: list[str]) -> str:
        try:
            result = self.runner(argv, self.timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            raise OsstackPortError("osstack command timed out") from exc
        except (OSError, subprocess.SubprocessError) as exc:
            raise OsstackPortError("osstack command could not run") from exc
        if result.returncode != 0:
            raise OsstackPortError("osstack command failed")
        return result.stdout

    def _run_json(self, argv: list[str]) -> object:
        raw = self._run(argv)
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError) as exc:
            raise OsstackPortError("osstack output was not valid JSON") from exc

    @staticmethod
    def _exact(value: object, keys: set[str], label: str) -> Mapping[str, Any]:
        if not isinstance(value, Mapping) or set(value) != keys:
            raise OsstackPortError(f"{label} contains unexpected fields")
        return value

    def list_sessions(self) -> tuple[OsstackSession, ...]:
        value = self._run_json([self.binary, "session", "list", "--json"])
        root = self._exact(value, {"sessions"}, "session list")
        sessions = root["sessions"]
        if not isinstance(sessions, list) or len(sessions) > 200:
            raise OsstackPortError("session list must be a bounded array")
        parsed: list[OsstackSession] = []
        for value in sessions:
            row = self._exact(value, _SESSION_KEYS, "session")
            try:
                session = OsstackSession(**row)
            except TypeError as exc:
                raise OsstackPortError("session fields are invalid") from exc
            if (
                not all(isinstance(getattr(session, key), str) and getattr(session, key)
                        for key in ("session_id", "task_id", "run_id", "agent_id", "state"))
                or isinstance(session.version, bool)
                or not isinstance(session.version, int)
                or session.version < 1
                or not isinstance(session.resume_supported, bool)
                or not isinstance(session.resume_ready, bool)
                or not isinstance(session.updated_at, int | float)
            ):
                raise OsstackPortError("session fields are invalid")
            parsed.append(session)
        return tuple(parsed)

    def send(self, envelope: RelayEnvelope) -> Mapping[str, Any]:
        if envelope.kind not in {"instruction.send", "decision.answer"}:
            raise OsstackPortError("unsupported inbound relay kind")
        text = envelope.payload["text"] if envelope.kind == "instruction.send" else envelope.payload["answer"]
        value = self._run_json(
            [
                self.binary, "session", "send", envelope.binding.session_id,
                "--turn", envelope.binding.turn_id,
                "--message-id", envelope.message_id,
                "--expected-version", str(envelope.binding.session_version),
                "--text", str(text), "--json",
            ]
        )
        result = self._exact(value, {"state", "session_version", "events"}, "send result")
        if result["state"] not in {"applied", "duplicate"}:
            raise OsstackPortError("send result state is invalid")
        if isinstance(result["session_version"], bool) or not isinstance(result["session_version"], int):
            raise OsstackPortError("send result version is invalid")
        if not isinstance(result["events"], list) or "native_session" in json.dumps(result):
            raise OsstackPortError("send result events are invalid")
        return result

    def snapshot(self, session_id: str) -> Mapping[str, Any]:
        value = self._run_json([self.binary, "session", "snapshot", session_id, "--json"])
        result = self._exact(value, _SNAPSHOT_KEYS, "snapshot")
        sanitized = sanitize_assistant_text(result["assistant_text"])
        if sanitized.blocked_categories or sanitized.truncated:
            raise OsstackPortError("snapshot is unsafe or oversized")
        return result

    def ensure_agent_available(self, agent_id: str) -> None:
        value = self._run_json([self.binary, "agent", "list", "--daemon", "--json"])
        if not isinstance(value, list):
            raise OsstackPortError("agent list must be an array")
        match = next((item for item in value if isinstance(item, Mapping) and item.get("id") == agent_id), None)
        if match is None or not isinstance(match.get("available"), Mapping):
            raise OsstackPortError("agent availability is missing")
        availability = match["available"]
        if availability.get("available") is not True:
            raise OsstackPortError(f"agent unavailable: {availability.get('status', 'unknown')}")

    def create_task(self, confirmation: RelayEnvelope, proposal: RelayEnvelope) -> None:
        if confirmation.kind != "task.create.confirm" or proposal.kind != "task.create.proposal":
            raise OsstackPortError("task creation requires proposal and confirmation")
        if confirmation.payload["proposal_id"] != proposal.payload["proposal_id"]:
            raise OsstackPortError("proposal confirmation mismatch")
        repository = str(proposal.payload["repository"])
        if repository == "current":
            repository = self.default_repo
        if not repository or not Path(repository).is_absolute():
            raise OsstackPortError("task creation requires a configured absolute repository")
        objective = str(proposal.payload["objective"])
        value = self._run_json(
            [
                self.binary, "session", "start",
                "--request-id", str(proposal.payload["proposal_id"]),
                "--agent", str(proposal.payload["suggested_agent"]),
                "--repo", repository,
                "--title", objective[:160],
                "--text", objective,
                "--json",
            ]
        )
        result = self._exact(value, _START_KEYS, "session start result")
        if (
            result["resume_supported"] is not True
            or result["resume_ready"] is not True
            or isinstance(result["version"], bool)
            or not isinstance(result["version"], int)
            or not isinstance(result["events"], list)
            or "native_session" in json.dumps(result)
        ):
            raise OsstackPortError("session start result is invalid")
        sanitized = sanitize_assistant_text(result["assistant_text"])
        if sanitized.blocked_categories or sanitized.truncated:
            raise OsstackPortError("session start result is unsafe or oversized")
