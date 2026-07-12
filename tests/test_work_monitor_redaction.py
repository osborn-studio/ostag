import json

from ostag.work_monitor.redaction import sanitize_task_input


def test_sanitize_projects_only_public_task_fields_and_redacts_secrets() -> None:
    sanitized = sanitize_task_input(
        {
            "task_id": "task-42",
            "title": "Deploy with TOKEN=abc123",
            "project": "ostag",
            "status": "running",
            "agent": "codex",
            "progress": 60,
            "duration_seconds": 120,
            "summary": "Authorization: Bearer very-secret-value",
            "error_class": None,
            "evidence_ref": "run:42",
            "source": "private source code",
            "prompt": "system prompt contents",
            "raw_log": "complete log TOKEN=never-leak",
            "full_log": "all output",
        }
    )

    serialized = json.dumps(sanitized, sort_keys=True)
    assert set(sanitized) == {
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
    }
    assert "abc123" not in serialized
    assert "very-secret-value" not in serialized
    assert "never-leak" not in serialized
    assert "private source code" not in serialized
    assert "system prompt contents" not in serialized
    assert "complete log" not in serialized
    assert "all output" not in serialized
    assert serialized.count("[REDACTED]") == 2


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
        }
    )

    assert len(sanitized["title"]) == 500
    assert len(sanitized["summary"]) == 500
