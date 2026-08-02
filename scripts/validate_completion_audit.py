from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    data = json.loads((ROOT / "release" / "completion-audit.json").read_text(encoding="utf-8"))
    if data["gate"] not in {"PASS", "PASS_WITH_EXTERNAL_BLOCKER"}:
        raise SystemExit(f"completion audit gate is not acceptable: {data['gate']}")
    if data["counts"]["FAIL"] != 0:
        raise SystemExit(f"completion audit has failures: {data['counts']['FAIL']}")
    if data["counts"].get("PENDING", 0) != 0:
        raise SystemExit(f"completion audit has pending checks: {data['counts']['PENDING']}")
    requirements = {row["requirement"] for row in data["rows"]}
    expected = {
        "DoD 1: real fork-derived git history exists",
        "DoD 2: upstream Open Tag code is traced and mapped",
        "DoD 3: productized OS Tag repo exists",
        "DoD 4: automated dry-run/backend/safety/audit gates pass",
        "DoD 5: live Feishu/Lark validation is complete or externally blocked",
        "DoD 6: osstack records phases, verifier commands, and final score",
        "OpenTag P0-P2 delivery task records are retained",
        "Live MFS smoke is complete or externally blocked",
    }
    missing = sorted(expected - requirements)
    if missing:
        raise SystemExit(f"completion audit missing requirements: {missing}")
    print("completion audit validation passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
