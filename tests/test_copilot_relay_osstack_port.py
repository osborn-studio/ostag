import json
import subprocess

import pytest

from ostag.copilot_relay.models import RelayEnvelope
from ostag.copilot_relay.osstack_port import OsstackPortError, OsstackSessionPort


class FakeRunner:
    def __init__(self, outputs: list[str | BaseException]) -> None:
        self.outputs = list(outputs)
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, argv: list[str], timeout: float):
        self.calls.append(tuple(argv))
        output = self.outputs.pop(0)
        if isinstance(output, BaseException):
            raise output
        return subprocess.CompletedProcess(argv, 0, stdout=output, stderr="")


def _instruction(text: str = "采用方案 B") -> RelayEnvelope:
    return RelayEnvelope.from_dict(
        {
            "schema_version": 1,
            "message_id": "msg_feishu_1",
            "binding": {
                "instance_id": "macmini", "task_id": "task_1", "run_id": "run_1",
                "session_id": "sess_1", "turn_id": "turn_7", "session_version": 4,
            },
            "sequence": 1,
            "created_at": "2026-07-14T01:00:00Z",
            "expires_at": "2026-07-14T02:00:00Z",
            "sender": {"platform": "feishu", "actor_id": "owner"},
            "kind": "instruction.send",
            "payload": {"text": text},
            "correlation_id": None,
            "causation_id": None,
        }
    )


def test_list_sessions_uses_exact_json_argv_and_rejects_native_ids() -> None:
    output = json.dumps(
        {
            "sessions": [
                {
                    "session_id": "sess_1", "task_id": "task_1", "run_id": "run_1",
                    "agent_id": "codex-cli", "state": "idle", "resume_supported": True,
                    "resume_ready": True, "version": 4, "updated_at": 10.0,
                }
            ]
        }
    )
    runner = FakeRunner([output])
    sessions = OsstackSessionPort(runner=runner).list_sessions()
    assert runner.calls == [("osstack", "session", "list", "--json")]
    assert sessions[0].session_id == "sess_1"

    leaked = json.loads(output)
    leaked["sessions"][0]["native_session_id"] = "secret"
    with pytest.raises(OsstackPortError, match="fields"):
        OsstackSessionPort(runner=FakeRunner([json.dumps(leaked)])).list_sessions()


def test_send_uses_bound_version_turn_and_message_without_shell_interpolation() -> None:
    runner = FakeRunner([json.dumps({"state": "applied", "session_version": 5, "events": []})])
    result = OsstackSessionPort(runner=runner).send(_instruction("继续；$(touch /tmp/nope)"))
    argv = runner.calls[0]
    assert argv[:4] == ("osstack", "session", "send", "sess_1")
    assert argv[argv.index("--turn") + 1] == "turn_7"
    assert argv[argv.index("--message-id") + 1] == "msg_feishu_1"
    assert argv[argv.index("--expected-version") + 1] == "4"
    assert "继续；$(touch /tmp/nope)" in argv
    assert result["state"] == "applied"


def test_non_json_timeout_extra_result_fields_and_oversized_snapshot_fail_closed() -> None:
    port = OsstackSessionPort(runner=FakeRunner(["not json"]))
    with pytest.raises(OsstackPortError, match="JSON"):
        port.list_sessions()

    timed = subprocess.TimeoutExpired(["osstack"], 1)
    with pytest.raises(OsstackPortError, match="timed out"):
        OsstackSessionPort(runner=FakeRunner([timed])).list_sessions()

    extra = {"state": "applied", "session_version": 5, "events": [], "native": "secret"}
    with pytest.raises(OsstackPortError, match="fields"):
        OsstackSessionPort(runner=FakeRunner([json.dumps(extra)])).send(_instruction())

    snapshot = {
        "session_id": "sess_1", "task_id": "task_1", "run_id": "run_1",
        "agent_id": "codex-cli", "state": "idle", "version": 4,
        "turn_id": "turn_7", "assistant_text": "x" * 4_001, "decision": None,
        "updated_at": 10.0,
    }
    with pytest.raises(OsstackPortError, match="snapshot"):
        OsstackSessionPort(runner=FakeRunner([json.dumps(snapshot)])).snapshot("sess_1")


def test_broken_codex_availability_is_rejected() -> None:
    agents = [
        {
            "id": "codex-cli", "display_name": "Codex", "cost_tier": "mid",
            "available": {
                "available": False, "status": "installed_but_broken",
                "reason": "wrapper missing", "last_checked_at": 1,
            },
            "capabilities": [],
        }
    ]
    with pytest.raises(OsstackPortError, match="installed_but_broken"):
        OsstackSessionPort(runner=FakeRunner([json.dumps(agents)])).ensure_agent_available(
            "codex-cli"
        )


def test_confirmed_task_maps_to_structured_task_submit_argv() -> None:
    proposal = _instruction().to_dict()
    proposal["kind"] = "task.create.proposal"
    proposal["payload"] = {
        "proposal_id": "proposal_1", "objective": "修复登录；rm -rf /",
        "repository": "ostag", "workspace": "managed", "suggested_agent": "codex-cli",
        "acceptance_criteria": ["tests pass"], "sensitive_operations": [],
    }
    confirm = _instruction().to_dict()
    confirm["kind"] = "task.create.confirm"
    confirm["payload"] = {"proposal_id": "proposal_1", "confirmed": True}
    runner = FakeRunner(['{"type":"done","terminal":true}\n'])

    OsstackSessionPort(runner=runner).create_task(
        RelayEnvelope.from_dict(confirm), RelayEnvelope.from_dict(proposal)
    )
    argv = runner.calls[0]
    assert argv[:3] == ("osstack", "task", "submit")
    assert argv[3] == "修复登录；rm -rf /"
    assert argv[argv.index("--tag") + 1] == "proposal_1"
    assert argv[argv.index("--repo") + 1] == "ostag"
