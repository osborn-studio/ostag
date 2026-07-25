"""Deterministic read-only osstack status intent classification."""

from __future__ import annotations

import re
import unicodedata
from typing import Literal


StatusIntent = Literal["status_query", "mixed", "none"]

_OSSTACK = re.compile(r"\bos\s*stack\b", re.IGNORECASE)
_STATUS_TERMS = (
    "在干嘛",
    "做什么",
    "进展",
    "状态",
    "看板",
    "汇报",
    "报告",
    "运行",
    "当前任务",
)
_SUBJECT_TERMS = ("设备", "agent", "任务", "工作", "开发进展")
_READ_TERMS = (
    "现在",
    "正在",
    "目前",
    "当前",
    "查看",
    "看看",
    "看一下",
    "给我看",
    "告诉我",
    "哪些",
)
_WRITE_TERMS = (
    "继续",
    "改为",
    "修改",
    "换成",
    "采用",
    "先做",
    "开发",
    "部署",
    "上线",
    "发布",
    "删除",
    "停止",
    "暂停",
    "恢复",
    "重试",
    "回答",
    "同意",
    "批准",
)


def _normalize(text: str) -> str:
    value = unicodedata.normalize("NFKC", text).casefold()
    return " ".join(value.replace("os-stack", "osstack").split())


def classify_osstack_status_intent(text: str) -> StatusIntent:
    """Classify only explicit status reads and mixed read/write requests."""
    normalized = _normalize(text)
    has_status = any(term in normalized for term in _STATUS_TERMS)
    has_subject = bool(_OSSTACK.search(normalized)) or any(
        term in normalized for term in _SUBJECT_TERMS
    )
    has_read = any(term in normalized for term in _READ_TERMS)
    status_signal = "看板" in normalized or (
        has_status and has_read and (has_subject or "进展" in normalized)
    )
    if not status_signal:
        return "none"

    # These phrases contain words such as "开发" but are still pure reads.
    mutation_candidate = normalized.replace("开发进展", "进展")
    if any(term in mutation_candidate for term in _WRITE_TERMS):
        return "mixed"
    return "status_query"
