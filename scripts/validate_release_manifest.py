from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    manifest_path = ROOT / "release" / "release-manifest.json"
    data = json.loads(manifest_path.read_text(encoding="utf-8"))

    required = {
        "product",
        "fork_root",
        "management_root",
        "branch",
        "current_commit",
        "upstream",
        "score",
        "readiness_gate",
        "readiness_checks",
        "external_blockers",
        "artifacts",
        "verification_commands",
        "osstack_tasks",
    }
    missing = sorted(required - set(data))
    if missing:
        raise SystemExit(f"manifest missing keys: {missing}")
    if data["product"] != "OS Tag":
        raise SystemExit("manifest product mismatch")
    if data["score"] != "96 / 100":
        raise SystemExit(f"unexpected score: {data['score']}")
    if data["readiness_gate"] not in {"PASS", "PASS_WITH_EXTERNAL_BLOCKER"}:
        raise SystemExit(f"unexpected readiness gate: {data['readiness_gate']}")
    if not data["osstack_tasks"]:
        raise SystemExit("manifest has no osstack tasks")
    required_reviews = {
        "task_37dce3d6",
        "task_acaade5e",
        "task_df75534b",
        "task_a54c03d5",
    }
    task_evidence = {task["task_id"]: task for task in data["osstack_tasks"]}
    incomplete = sorted(
        task_id
        for task_id in required_reviews
        if task_id not in task_evidence
        or not task_evidence[task_id].get("retained")
        or not task_evidence[task_id].get("verified")
    )
    if incomplete:
        raise SystemExit(f"incomplete osstack task evidence in manifest: {incomplete}")
    for artifact in data["artifacts"]:
        if not (ROOT / artifact).exists():
            raise SystemExit(f"missing artifact: {artifact}")
    print("release manifest passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
