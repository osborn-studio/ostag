from pathlib import Path

import pytest

from ostag.core.event import NormalizedEvent
from ostag.core.runner import OSTagRunner
from ostag.config import load_settings
from ostag.main import main_async
from ostag.work_monitor.identity import Destination, IdentityStore, Principal
from ostag.work_monitor.service import AmbiguousTask, WorkMonitorService, parse_monitor_command
from ostag.work_monitor.store import MonitorStore


def _seed(store: MonitorStore) -> None:
    with store.transaction() as connection:
        connection.execute(
            "INSERT INTO instances(instance_id,last_sequence,last_seen_at) VALUES ('mac',1,'2026-07-12T00:00:00Z')"
        )
        rows = [
            (
                "mac",
                "task-123",
                "Deploy API",
                "ostag",
                "running",
                "codex",
                50,
                10,
                "ok",
                None,
                None,
                1,
                "2026-07-12T00:00:00Z",
            ),
            (
                "mac",
                "task-456",
                "Deploy Web",
                "other",
                "blocked",
                "codex",
                10,
                20,
                "blocked",
                "network",
                None,
                1,
                "2026-07-12T00:00:00Z",
            ),
        ]
        connection.executemany(
            "INSERT INTO tasks(instance_id,task_id,title,project,status,agent,progress,duration_seconds,summary,error_class,evidence_ref,version,occurred_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )


@pytest.mark.parametrize(
    "text",
    (
        "用看板形式汇报目前进展",
        "请给我一份看板形式的报告",
        "以看板展示当前状态",
    ),
)
def test_natural_language_board_requests_are_monitor_commands(text: str) -> None:
    command = parse_monitor_command(text)

    assert command is not None
    assert command.name == "board"
    assert command.argument == ""


def _service(tmp_path: Path) -> tuple[WorkMonitorService, Principal, Destination]:
    store = MonitorStore(tmp_path / "monitor.sqlite3")
    _seed(store)
    identities = IdentityStore(store.path, enrollment_secret="join")
    owner = Principal("feishu", "owner")
    destination = Destination("feishu", "private", "private")
    identities.bind_private(owner=owner, destination=destination, enrollment_secret="join")
    return WorkMonitorService(store, identities), owner, destination


def test_board_instances_and_watch_are_read_or_propose_queries(tmp_path: Path) -> None:
    service, owner, destination = _service(tmp_path)

    assert service.board(owner, destination).to_dict()["cards"][0]["status"] == "blocked"
    assert service.instances(owner, destination)[0]["instance_id"] == "mac"
    assert service.watch(owner, destination, task_id="task-123") == "已订阅 task-123"


def test_task_query_returns_ambiguous_candidates_instead_of_guessing(tmp_path: Path) -> None:
    service, owner, destination = _service(tmp_path)

    with pytest.raises(AmbiguousTask) as excinfo:
        service.task(owner, destination, "Deploy")

    assert [candidate["task_id"] for candidate in excinfo.value.candidates] == [
        "task-123",
        "task-456",
    ]


@pytest.mark.asyncio
async def test_handle_event_uses_trusted_callback_actor_and_destination(tmp_path: Path) -> None:
    service, _, destination = _service(tmp_path)
    event = NormalizedEvent(
        platform="feishu",
        event_id="evt",
        chat_id="private",
        message_id="msg",
        sender_id="owner",
        text="/board",
        mention_bot=True,
        chat_type="private",
        destination_id="private",
        raw={"actor_id": "attacker", "chat_type": "group", "value": {"actor_id": "attacker"}},
    )

    reply = service.handle_event(event)

    assert "blocked" in reply
    assert destination.destination_id == event.destination_id


def test_forged_raw_button_cannot_borrow_owner_identity(tmp_path: Path) -> None:
    service, _, _ = _service(tmp_path)
    forged = NormalizedEvent(
        platform="telegram", event_id="evt", chat_id="private", message_id="msg",
        sender_id="owner", text="/board", mention_bot=True, chat_type="private",
        destination_id="private", raw={"platform": "feishu", "actor_id": "owner",
        "chat_type": "private", "value": {"actor_id": "owner", "platform": "feishu"}},
    )
    with pytest.raises(Exception, match="read permission"):
        service.handle_event(forged)


@pytest.mark.asyncio
async def test_runner_prioritizes_chinese_monitor_command(tmp_path: Path) -> None:
    service, _, _ = _service(tmp_path)
    config = load_settings(None, audit_log=tmp_path / "audit.log", backend="mock")
    runner = OSTagRunner(config, work_monitor=service)
    event = NormalizedEvent(
        platform="feishu",
        event_id="evt-cn",
        chat_id="private",
        message_id="msg-cn",
        sender_id="owner",
        text="看板",
        mention_bot=True,
        chat_type="private",
        destination_id="private",
    )

    reply = await runner.handle_event(event)

    assert "OStag 工作看板" in reply
    assert runner.last_run_id == ""


def test_group_scope_filters_all_queries_and_watch_targets(tmp_path: Path) -> None:
    service, owner, private = _service(tmp_path)
    group = Destination("feishu", "group", "group")
    service.identities.enroll_group(owner, group, via=private, scope="project:ostag")
    board = service.board(owner, group).to_dict()
    assert [card["task_id"] for card in board["cards"]] == ["task-123"]
    assert service.instances(owner, group)[0]["instance_id"] == "mac"
    assert "task-123" in service.task(owner, group, "task-123").to_text()
    with pytest.raises(Exception, match="outside destination scope"):
        service.watch(owner, group, task_id="task-456")
    with pytest.raises(Exception):
        service.task(owner, group, "task-456")


def test_scope_target_must_exist(tmp_path: Path) -> None:
    service, owner, private = _service(tmp_path)
    group = Destination("feishu", "missing-group", "group")
    service.identities.enroll_group(owner, group, via=private, scope="instance:missing")
    with pytest.raises(Exception, match="does not exist"):
        service.board(owner, group)


def test_registered_empty_instance_scope_is_valid(tmp_path: Path) -> None:
    service, owner, private = _service(tmp_path)
    with service.store.transaction() as connection:
        connection.execute(
            "INSERT INTO instances(instance_id,last_sequence,last_seen_at) "
            "VALUES ('empty',3,'2026-07-13T00:00:00Z')"
        )
    group = Destination("feishu", "empty-group", "group")
    service.identities.enroll_group(owner, group, via=private, scope="instance:empty")

    assert service.board(owner, group).to_dict()["cards"] == []
    assert [row["instance_id"] for row in service.instances(owner, group)] == ["empty"]
    assert service.watch(owner, group, instance_id="empty") == "已订阅 empty"


def test_all_scope_can_watch_registered_empty_instance(tmp_path: Path) -> None:
    service, owner, private = _service(tmp_path)
    with service.store.transaction() as connection:
        connection.execute(
            "INSERT INTO instances(instance_id,last_sequence,last_seen_at) VALUES ('empty',1,NULL)"
        )
    group = Destination("feishu", "all-group", "group")
    service.identities.enroll_group(owner, group, via=private, scope="all")
    assert service.watch(owner, group, instance_id="empty") == "已订阅 empty"


def test_project_scope_cannot_watch_whole_mixed_instance(tmp_path: Path) -> None:
    service, owner, private = _service(tmp_path)
    group = Destination("feishu", "project-group", "group")
    service.identities.enroll_group(owner, group, via=private, scope="project:ostag")
    # instance "mac" has tasks in both ostag and another project; subscribing
    # to the whole instance would leak future notifications outside this scope.
    with pytest.raises(Exception, match="outside destination scope"):
        service.watch(owner, group, instance_id="mac")
    assert service.watch(owner, group, task_id="task-123") == "已订阅 task-123"


@pytest.mark.asyncio
async def test_runner_without_monitor_service_does_not_intercept_board(tmp_path: Path) -> None:
    config = load_settings(None, audit_log=tmp_path / "audit.log", backend="mock", monitor_enabled=False)
    runner = OSTagRunner(config)
    reply = await runner.handle_event(NormalizedEvent(
        platform="feishu", event_id="off", chat_id="private", message_id="off",
        sender_id="owner", text="/board", mention_bot=True, chat_type="private",
        destination_id="private",
    ))
    assert "mock reply" in reply
    assert runner.last_run_id.startswith("run_")


@pytest.mark.asyncio
async def test_main_does_not_construct_monitor_when_disabled(monkeypatch) -> None:
    def forbidden_store(*args, **kwargs):
        raise AssertionError("monitor store must remain unopened when disabled")

    monkeypatch.setattr("ostag.main.MonitorStore", forbidden_store)
    assert await main_async([
        "--dry-run", "--backend", "mock", "--event", "examples/feishu_event.json"
    ]) == 0
