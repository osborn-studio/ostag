from ostag.work_monitor.rendering import MobileBoardView, TaskDetailView


def test_mobile_board_is_critical_first_paginated_and_truncates_long_titles() -> None:
    tasks = [
        {"task_id": "done", "title": "done", "status": "done", "progress": 100},
        {"task_id": "run", "title": "R" * 90, "status": "running", "progress": 40},
        {"task_id": "block", "title": "blocked", "status": "blocked", "progress": 20},
    ]

    view = MobileBoardView.from_tasks(tasks, page=1, page_size=2)
    payload = view.to_dict()

    assert [card["task_id"] for card in payload["cards"]] == ["block", "run"]
    assert len(payload["cards"][1]["title"]) <= 48
    assert payload["pagination"] == {"page": 1, "page_size": 2, "total": 3, "has_more": True}


def test_mobile_board_exposes_operations_minimalist_summary() -> None:
    tasks = [
        {"task_id": "triage", "title": "Triage", "status": "triage", "progress": 0},
        {"task_id": "todo", "title": "Todo", "status": "todo", "progress": 0},
        {"task_id": "ready", "title": "Ready", "status": "ready", "progress": 0},
        {"task_id": "run-1", "title": "Run 1", "status": "running", "progress": 20},
        {"task_id": "run-2", "title": "Run 2", "status": "running", "progress": 70},
        {
            "task_id": "blocked",
            "title": "Blocked",
            "status": "blocked",
            "progress": 50,
            "instance_id": "ostag-local",
            "agent": "codex",
            "summary": "Needs owner attention",
        },
        {"task_id": "done-1", "title": "Done 1", "status": "done", "progress": 100},
        {"task_id": "done-2", "title": "Done 2", "status": "done", "progress": 100},
        {"task_id": "done-3", "title": "Done 3", "status": "done", "progress": 100},
    ]

    view = MobileBoardView.from_tasks(tasks, page=1, page_size=6)
    payload = view.to_dict()

    assert payload["stage_counts"] == {
        "triage": 1,
        "todo": 1,
        "ready": 1,
        "running": 2,
        "blocked": 1,
        "done": 3,
    }
    assert payload["kpis"] == {"running": 2, "blocked": 1, "attention": 1, "done": 3}
    assert payload["attention"][0]["task_id"] == "blocked"
    assert [task["task_id"] for task in payload["running_tasks"]] == ["run-1", "run-2"]
    assert payload["attention"][0]["instance_id"] == "ostag-local"
    assert payload["attention"][0]["agent"] == "codex"


def test_task_detail_only_renders_supported_capabilities() -> None:
    view = TaskDetailView.from_task(
        {"task_id": "task-1", "title": "Deploy", "status": "running", "progress": 70},
        capabilities=("pause", "cancel"),
    )

    assert view.to_dict()["actions"] == ["pause", "cancel"]
