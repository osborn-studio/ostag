"""Fail-closed filtering for assistant text allowed into the central relay."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass


_ZERO_WIDTH = re.compile("[\u200b-\u200f\u202a-\u202e\u2060\ufeff]")
_BLOCK_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "private_key",
        re.compile(r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----", re.IGNORECASE),
    ),
    (
        "raw_token",
        re.compile(
            r"(?:\b(?:authorization\s*:\s*)?bearer\s+[A-Za-z0-9._~+/=-]{20,}"
            r"|\b\d{8,12}:[A-Za-z0-9_-]{30,}\b"
            r"|\b(?:api[_-]?key|token|secret)\s*[:=]\s*['\"]?"
            r"(?:sk-[A-Za-z0-9_-]{20,}|[A-Za-z0-9_./+=:-]{24,}))",
            re.IGNORECASE,
        ),
    ),
    ("cookie", re.compile(r"^\s*(?:set-)?cookie\s*:", re.IGNORECASE | re.MULTILINE)),
    (
        "local_path",
        re.compile(r"(?:/(?:Users|home)/[^\s/]+/[^\s]+|[A-Za-z]:\\Users\\[^\s\\]+\\[^\s]+)"),
    ),
    ("diff", re.compile(r"(?:```\s*diff\b|^@@\s+-\d)", re.IGNORECASE | re.MULTILINE)),
    (
        "shell_transcript",
        re.compile(
            r"^\s*(?:\$|#)\s+(?:export|git|pnpm|npm|python|uv|docker|ssh|scp|curl|cd|ls|cat|rm|mv|cp|make)\b",
            re.IGNORECASE | re.MULTILINE,
        ),
    ),
)


@dataclass(frozen=True)
class SanitizedText:
    text: str
    redaction_count: int
    blocked_categories: tuple[str, ...]
    truncated: bool


def _scan_form(text: str) -> str:
    return _ZERO_WIDTH.sub("", unicodedata.normalize("NFKC", text))


def sanitize_assistant_text(text: str, *, max_codepoints: int = 4_000) -> SanitizedText:
    """Return a cloud-safe snapshot, or an empty fail-closed result with reasons."""

    if not isinstance(text, str):
        raise ValueError("assistant text must be a string")
    if isinstance(max_codepoints, bool) or not isinstance(max_codepoints, int) or max_codepoints < 1:
        raise ValueError("max_codepoints must be a positive integer")

    scan_text = _scan_form(text)
    categories: list[str] = []
    match_count = 0
    for category, pattern in _BLOCK_PATTERNS:
        matches = tuple(pattern.finditer(scan_text))
        if matches:
            categories.append(category)
            match_count += len(matches)
    if categories:
        return SanitizedText(
            text="",
            redaction_count=match_count,
            blocked_categories=tuple(categories),
            truncated=False,
        )

    truncated = len(text) > max_codepoints
    return SanitizedText(
        text=text[:max_codepoints],
        redaction_count=0,
        blocked_categories=(),
        truncated=truncated,
    )
