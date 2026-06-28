from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MANAGEMENT_ROOT = Path("/Users/gilbertchen/Documents/CODE/ostag")


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def first_match(pattern: str, text: str, default: str = "") -> str:
    match = re.search(pattern, text, re.MULTILINE)
    return match.group(1) if match else default


def archived(task_id: str) -> bool:
    return (MANAGEMENT_ROOT / "tasks" / "archive" / "2026-06-28" / f"{task_id}.json").exists()


def main() -> int:
    scorecard = read(ROOT / "release" / "ostag-scorecard.md")
    readiness = read(ROOT / "release" / "readiness-audit.md")
    evidence = read(ROOT / "release" / "ostag-launch-evidence.md")

    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    branch = subprocess.check_output(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=ROOT, text=True
    ).strip()

    task_ids = sorted(set(re.findall(r"task_[a-f0-9]{8}", evidence)))

    artifacts = [
        "FORK_ORIGIN.md",
        "docs/fork_migration_report.md",
        "release/ostag-scorecard.md",
        "release/readiness-audit.md",
        "release/ostag-launch-evidence.md",
        "release/live-validation-feishu.md",
        "release/live-validation-feishu-transcript-20260628.txt",
        "release/launchd-production-validation-20260628.txt",
        "release/mfs-validation.md",
        "release/manual-live-validation-handoff.md",
        "release/release-manifest.json",
        "release/ai.ostag.feishu.plist",
        ".github/workflows/ostag-ci.yml",
    ]
    for optional_artifact in (
        "release/completion-audit.md",
        "release/completion-audit.json",
        "release/launch-status.md",
        "release/launch-status.json",
        "release/production-scope.md",
        "release/production-scope.json",
        "release/external-validation-handoff.md",
        "release/external-validation-handoff.json",
        "release/secret-hygiene.md",
        "release/secret-hygiene.json",
        "release/ostag-release-bundle.tar.gz",
        "release/ostag-release-bundle.sha256",
    ):
        if (ROOT / optional_artifact).exists():
            artifacts.append(optional_artifact)

    external_blockers = []
    live_validation = read(ROOT / "release" / "live-validation-feishu.md")
    mfs_validation = read(ROOT / "release" / "mfs-validation.md")
    production_scope = read(ROOT / "release" / "production-scope.json")
    if "manual_group_validation_passed" not in live_validation:
        external_blockers.append("Manual Feishu/Lark group validation record is not completed.")
    if '"mfs_mode": "excluded"' not in production_scope and "Status: `mfs_smoke_passed`" not in mfs_validation:
        external_blockers.append("Live MFS smoke is not configured in this environment.")

    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "product": "OS Tag",
        "fork_root": str(ROOT),
        "management_root": str(MANAGEMENT_ROOT),
        "branch": branch,
        "current_commit": commit,
        "upstream": {
            "url": "https://github.com/zilliztech/mfs",
            "baseline_commit": "16574f9b365bc67f3716c552dad416da730088b8",
        },
        "score": first_match(r"Total: `([^`]+)`", scorecard),
        "readiness_gate": first_match(r"Gate status: `([^`]+)`", readiness),
        "readiness_checks": first_match(r"Checks: `([^`]+)`", readiness),
        "production_candidate": "not_recorded",
        "external_blockers": external_blockers,
        "artifacts": artifacts,
        "verification_commands": [
            "scripts/release_check.sh",
            "scripts/deploy_preflight.sh",
            "scripts/ci_release_gates.sh",
            "scripts/validate_live_validation_record.sh feishu",
            "scripts/promote_production_candidate.sh feishu",
            "scripts/generate_completion_audit.py",
            "scripts/validate_completion_audit.py",
            "scripts/launch_status.sh",
            "scripts/production_scope_record.sh",
            "scripts/validate_production_scope.sh",
            "scripts/external_validation_handoff.sh",
            "scripts/validate_external_validation_handoff.sh",
            "scripts/secret_hygiene_check.sh",
            "scripts/validate_secret_hygiene.sh",
            "scripts/release_readiness_audit.sh",
        ],
        "osstack_tasks": [
            {"task_id": task_id, "archived": archived(task_id)} for task_id in task_ids
        ],
    }

    out = ROOT / "release" / "release-manifest.json"
    out.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(out.relative_to(ROOT))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
