import json
from pathlib import Path

from ostag.work_monitor.models import TaskSnapshot
from ostag.work_monitor.redaction import sanitize_task_input


def test_sanitize_projects_only_public_task_fields_and_redacts_secrets() -> None:
    sanitized = sanitize_task_input(
        {
            "task_id": "task-42",
            "title": "Deploy with TOKEN = abc123",
            "project": "ostag",
            "status": "running",
            "agent": "codex",
            "progress": 60,
            "duration_seconds": 120,
            "summary": "Authorization: Bearer very-secret-value password=hunter2",
            "error_class": "authorization: Basic base64-secret",
            "evidence_ref": "run:42",
            "version": 7,
            "source": "private source code",
            "prompt": "system prompt contents",
            "raw_log": "complete log TOKEN=never-leak",
            "full_log": "all output",
        }
    )

    serialized = json.dumps(sanitized.to_dict(), sort_keys=True)
    assert isinstance(sanitized, TaskSnapshot)
    assert set(sanitized.to_dict()) == {
        "task_id",
        "title",
        "project",
        "status",
        "agent",
        "progress",
        "duration_seconds",
        "summary",
        "error_class",
        "evidence_ref",
        "version",
    }
    assert "abc123" not in serialized
    assert "very-secret-value" not in serialized
    assert "hunter2" not in serialized
    assert "base64-secret" not in serialized
    assert "never-leak" not in serialized
    assert "private source code" not in serialized
    assert "system prompt contents" not in serialized
    assert "complete log" not in serialized
    assert "all output" not in serialized
    assert serialized.count("[REDACTED]") == 4


def test_sanitize_truncates_projected_text() -> None:
    sanitized = sanitize_task_input(
        {
            "task_id": "task-42",
            "title": "x" * 700,
            "project": "ostag",
            "status": "running",
            "agent": "codex",
            "progress": 1,
            "duration_seconds": 2,
            "summary": "y" * 700,
            "error_class": None,
            "evidence_ref": "run:42",
            "version": 7,
        }
    )

    assert len(sanitized.title) == 500
    assert len(sanitized.summary) == 500


def test_sanitize_result_still_enforces_strict_snapshot_validation() -> None:
    payload = {
        "task_id": "task-42",
        "title": "Ship monitor",
        "project": "ostag",
        "status": "unknown",
        "agent": "codex",
        "progress": 1,
        "duration_seconds": 2,
        "summary": "working",
        "error_class": None,
        "evidence_ref": "run:42",
        "version": 7,
    }

    try:
        sanitize_task_input(payload)
    except ValueError as exc:
        assert "status" in str(exc)
    else:
        raise AssertionError("strict TaskSnapshot validation was bypassed")


def test_sanitize_redacts_quoted_and_existing_secret_formats() -> None:
    secrets = {
        "quoted_password": "hunter2",
        "quoted_bearer": "super secret",
        "openai": "sk-1234567890abcdefghij",
        "slack": "xoxb-1234567890-secret",
        "aws": "AKIA1234567890ABCDEF",
    }
    snapshot = sanitize_task_input(
        {
            "task_id": "task-42",
            "title": f'password="{secrets["quoted_password"]}"',
            "project": "ostag",
            "status": "running",
            "agent": "codex",
            "progress": 1,
            "duration_seconds": 2,
            "summary": f'Authorization: Bearer "{secrets["quoted_bearer"]}"',
            "error_class": f'{secrets["openai"]} {secrets["slack"]}',
            "evidence_ref": secrets["aws"],
            "version": 7,
        }
    )

    serialized = json.dumps(snapshot.to_dict(), sort_keys=True)
    for secret in secrets.values():
        assert secret not in serialized
    assert serialized.count("[REDACTED]") >= 5


def test_secret_hygiene_fixture_is_redacted_before_artifact_serialization() -> None:
    fixture = json.loads(
        Path("tests/fixtures/work_monitor_secret_input.json").read_text(encoding="utf-8")
    )

    serialized = json.dumps(sanitize_task_input(fixture).to_dict(), sort_keys=True)

    assert "fixture-token-that-must-never-ship" not in serialized
    assert "fixture-bearer-that-must-never-ship" not in serialized
    assert "sk-fixture0123456789abcdef" not in serialized
    assert "[REDACTED]" in serialized
