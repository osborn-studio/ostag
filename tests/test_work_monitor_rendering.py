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


def test_task_detail_only_renders_supported_capabilities() -> None:
    view = TaskDetailView.from_task(
        {"task_id": "task-1", "title": "Deploy", "status": "running", "progress": 70},
        capabilities=("pause", "cancel"),
    )

    assert view.to_dict()["actions"] == ["pause", "cancel"]
