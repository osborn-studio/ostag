from __future__ import annotations

import hashlib
import json
import math
import re
import sys
from collections import Counter
from pathlib import Path


KEY = r"[A-Za-z0-9_.-]*(?:secret|token|password|api[_-]?key|access[_-]?key|private[_-]?key)[A-Za-z0-9_.-]*"
SHELL_QUOTED = re.compile(
    rf'''(?im)^[ \t]*(?:export[ \t]+)?{KEY}[ \t]*=[ \t]*(["'])([^"'\n]+)\1'''
)
SHELL_UNQUOTED = re.compile(
    rf"(?im)^[ \t]*(?:export[ \t]+)?{KEY}[ \t]*=[ \t]*([^\s#]+)"
)
JSON_QUOTED = re.compile(rf'''(?i)["']{KEY}["'][ \t]*:[ \t]*(["'])([^"'\n]+)\1''')
ALIDOCS = re.compile(r"https://alidocs\.dingtalk\.com/i/nodes/[A-Za-z0-9_-]{24,}")
KNOWN_TOKEN = re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b")


def placeholder(value: str) -> bool:
    cleaned = value.strip().strip("`\"")
    lowered = cleaned.casefold()
    return (
        not cleaned
        or cleaned.startswith("<") and cleaned.endswith(">")
        or "${" in cleaned
        or "..." in cleaned
        or lowered in {"configured", "verify", "secret", "token", "password"}
        or lowered.startswith(("example-", "changeme", "replace-me"))
    )


def entropy(value: str) -> float:
    counts = Counter(value)
    return -sum((count / len(value)) * math.log2(count / len(value)) for count in counts.values())


def credential_like(value: str) -> bool:
    if placeholder(value) or len(value) < 8:
        return False
    punctuation = any(character in value for character in ".+@:/")
    classes = sum(
        (any(c.islower() for c in value), any(c.isupper() for c in value),
         any(c.isdigit() for c in value), any(not c.isalnum() for c in value))
    )
    return punctuation or classes >= 2 or entropy(value) >= 3.0


def scan(root: Path) -> list[tuple[str, str]]:
    findings: list[tuple[str, str]] = []
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix in {".gz", ".sha256"}:
            continue
        if path.name in {"secret-hygiene.json", "secret-hygiene.md", "secret_hygiene_scan.py"}:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if path.name.startswith(".env") or path.suffix.casefold() in {
            "", ".env", ".json", ".yaml", ".yml", ".toml", ".md", ".sh"
        }:
            for match in (*SHELL_QUOTED.finditer(text), *JSON_QUOTED.finditer(text)):
                if credential_like(match.group(2)):
                    findings.append((str(path.relative_to(root)), "credential_assignment"))
            for match in SHELL_UNQUOTED.finditer(text):
                if not match.group(1).startswith(("'", '"')) and credential_like(match.group(1)):
                    findings.append((str(path.relative_to(root)), "credential_assignment"))
        findings.extend(
            (str(path.relative_to(root)), "token_shaped_alidocs_url")
            for _ in ALIDOCS.finditer(text)
        )
        findings.extend(
            (str(path.relative_to(root)), "known_token_prefix")
            for _ in KNOWN_TOKEN.finditer(text)
        )
    return findings


def main() -> int:
    findings = scan(Path(sys.argv[1]))
    categories = Counter(category for _, category in findings)
    digest_input = "\n".join(f"{path}:{category}" for path, category in sorted(findings))
    print(
        json.dumps(
            {
                "match_count": len(findings),
                "category_counts": dict(sorted(categories.items())),
                "findings_digest_sha256": hashlib.sha256(digest_input.encode()).hexdigest(),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
