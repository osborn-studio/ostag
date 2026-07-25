import pytest

from ostag.copilot_relay.status_intent import classify_osstack_status_intent


@pytest.mark.parametrize(
    "text",
    (
        "现在 osstack 在干嘛？",
        "OS Stack 正在做什么",
        "目前开发进展怎么样",
        "有哪些设备和 Agent 正在运行？",
        "给我看一下工作状态",
        "用看板汇报目前进展",
    ),
)
def test_status_queries_are_read_only(text: str) -> None:
    assert classify_osstack_status_intent(text) == "status_query"


@pytest.mark.parametrize(
    "text",
    (
        "继续开发",
        "换成方案 B",
        "先补测试再部署",
        "修改 osstack 状态显示组件",
        "同意",
    ),
)
def test_mutating_instructions_are_not_status_queries(text: str) -> None:
    assert classify_osstack_status_intent(text) == "none"


@pytest.mark.parametrize(
    "text",
    (
        "告诉我现在进展，然后换成方案 B",
        "看一下 osstack 状态并继续开发",
    ),
)
def test_mixed_query_and_mutation_is_explicit(text: str) -> None:
    assert classify_osstack_status_intent(text) == "mixed"


def test_unrelated_status_question_is_not_claimed() -> None:
    assert classify_osstack_status_intent("服务器状态码为什么是 500？") == "none"
