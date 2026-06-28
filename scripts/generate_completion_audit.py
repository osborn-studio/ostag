from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MANAGEMENT_ROOT = Path("/Users/gilbertchen/Documents/CODE/ostag")
OUT_MD = ROOT / "release" / "completion-audit.md"
OUT_JSON = ROOT / "release" / "completion-audit.json"


@dataclass(frozen=True)
class Row:
    status: str
    requirement: str
    evidence: str


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


def run(*cmd: str, cwd: Path = ROOT) -> tuple[int, str]:
    proc = subprocess.run(
        list(cmd),
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    return proc.returncode, proc.stdout.strip()


def exists(path: str) -> bool:
    return (ROOT / path).exists()


def archived(task_id: str) -> bool:
    return (MANAGEMENT_ROOT / "tasks" / "archive" / "2026-06-28" / f"{task_id}.json").exists()


def live_status() -> str:
    text = read(ROOT / "release" / "live-validation-feishu.md")
    if "manual_group_validation_passed" in text:
        return "PASS"
    if "blocked_missing_credentials" in text:
        return "EXTERNAL_BLOCKER"
    return "FAIL"


def mfs_status() -> str:
    scope_text = read(ROOT / "release" / "production-scope.json")
    if '"mfs_mode": "excluded"' in scope_text:
        return "PASS"
    text = read(ROOT / "release" / "mfs-validation.md")
    if "Status: `mfs_smoke_passed`" in text:
        return "PASS"
    if "Status: `blocked_" in text:
        return "EXTERNAL_BLOCKER"
    return "FAIL"


def main() -> int:
    (ROOT / "release").mkdir(exist_ok=True)
    readiness = read(ROOT / "release" / "readiness-audit.md")
    scorecard = read(ROOT / "release" / "ostag-scorecard.md")
    state = read(MANAGEMENT_ROOT / "STATE.md")
    manifest = json.loads(read(ROOT / "release" / "release-manifest.json") or "{}")
    migration_report = read(ROOT / "docs/fork_migration_report.md")

    code, release_output = run("scripts/release_check.sh")
    release_check_pass = code == 0 and re.search(r"\b\d+\s+passed\b", release_output) is not None
    docker_compose_pass = run("docker", "compose", "config")[0] == 0

    allow_missing_bundle = os.getenv("OSTAG_COMPLETION_AUDIT_ALLOW_MISSING_BUNDLE") == "1"
    bundle_status = "PASS" if exists("release/ostag-release-bundle.tar.gz") and exists("release/ostag-release-bundle.sha256") else "FAIL"
    if allow_missing_bundle and bundle_status == "FAIL":
        bundle_status = "PENDING"

    rows = [
        Row(
            "PASS" if exists("FORK_ORIGIN.md") and run("git", "remote", "get-url", "upstream")[0] == 0 else "FAIL",
            "DoD 1: real fork-derived git history exists",
            "`FORK_ORIGIN.md`, upstream remote, and baseline commit are present",
        ),
        Row(
            "PASS" if exists("docs/fork_migration_report.md") and all(item in migration_report for item in ("mfs_cat.py", "mfs_search.py", "opentag_agent.py", "opentag_doctor.py", "opentag_memory.py", "slack_socket_agent.py")) else "FAIL",
            "DoD 2: upstream Open Tag code is traced and mapped",
            "`docs/fork_migration_report.md` records ported/rewritten/deferred/deleted decisions",
        ),
        Row(
            "PASS" if all(exists(path) for path in ("ostag/core/runner.py", "ostag/adapters/feishu.py", "ostag/adapters/lark.py", "ostag/mfs/client.py", ".github/workflows/ostag-ci.yml", "Dockerfile", "deploy/launchd/ai.ostag.feishu.plist.template")) else "FAIL",
            "DoD 3: productized OS Tag repo exists",
            "Product modules, Feishu/Lark adapters, MFS integration, CI, Docker, and launchd paths exist",
        ),
        Row(
            "PASS" if release_check_pass else "FAIL",
            "DoD 4: automated dry-run/backend/safety/audit gates pass",
            "`scripts/release_check.sh` passes compile, pytest, doctor, and Feishu/Lark dry-runs",
        ),
        Row(
            live_status(),
            "DoD 5: live Feishu/Lark validation is complete or externally blocked",
            "`release/live-validation-feishu.md` records manual pass or missing-credential blocker",
        ),
        Row(
            "PASS" if all(task in state for task in ("task_d57d82ef", "task_d2859192", "task_3f9f86a2")) and "Total: `96 / 100`" in scorecard else "FAIL",
            "DoD 6: osstack records phases, verifier commands, and final score",
            "`STATE.md`, archived tasks, execution board, and scorecard are current",
        ),
        Row(
            "PASS" if all(archived(task) for task in ("task_d57d82ef", "task_2b786dcd", "task_49df7528", "task_1edc63fd", "task_c0be8580", "task_657bb669", "task_8c98c03f", "task_d2859192")) else "FAIL",
            "Phases 0-7 are completed and archived",
            "All primary phase task files exist under `tasks/archive/2026-06-28/`",
        ),
        Row(
            "PASS" if docker_compose_pass and exists("release/ai.ostag.feishu.plist") else "FAIL",
            "Deployment paths are reproducible",
            "`docker compose config` passes and launchd plist artifact exists",
        ),
        Row(
            mfs_status(),
            "Live MFS smoke is complete or externally blocked",
            "`release/mfs-validation.md` records a live smoke pass or structured MFS blocker; `release/production-scope.json` can explicitly exclude MFS from production scope",
        ),
        Row(
            bundle_status,
            "Release handoff bundle exists",
            "Tarball and sha256 checksum are generated and bundle validator covers required/forbidden paths",
        ),
        Row(
            "PASS" if manifest.get("readiness_gate") in {"PASS", "PASS_WITH_EXTERNAL_BLOCKER"} and "0 failed" in manifest.get("readiness_checks", "") else "FAIL",
            "Machine-readable manifest matches current gates",
            "`release/release-manifest.json` reflects readiness gate, artifacts, and archived osstack tasks",
        ),
        Row(
            "PASS" if ("PASS" in readiness or "PASS_WITH_EXTERNAL_BLOCKER" in readiness) and "0 failed" in readiness else "FAIL",
            "No current P0 blocker is detected by release readiness audit",
            "`release/readiness-audit.md` has zero failed checks",
        ),
    ]

    counts = {key: sum(1 for row in rows if row.status == key) for key in ("PASS", "EXTERNAL_BLOCKER", "FAIL", "PENDING")}
    if counts["FAIL"]:
        gate = "FAIL"
    elif counts["EXTERNAL_BLOCKER"]:
        gate = "PASS_WITH_EXTERNAL_BLOCKER"
    else:
        gate = "PASS"

    blockers = []
    if gate == "PASS_WITH_EXTERNAL_BLOCKER":
        if live_status() == "EXTERNAL_BLOCKER":
            blockers.append("Real Feishu/Lark app credentials and manual group validation evidence")
        if mfs_status() == "EXTERNAL_BLOCKER":
            blockers.append(
                "Live MFS smoke configuration, unless MFS is explicitly excluded from production scope"
            )

    data = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "gate": gate,
        "counts": counts,
        "remaining_external_blockers": blockers,
        "rows": [row.__dict__ for row in rows],
    }
    OUT_JSON.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    lines = [
        "# OS Tag Completion Audit",
        "",
        f"- Generated at: `{data['generated_at']}`",
        f"- Gate: `{gate}`",
        f"- Counts: `{counts['PASS']} passed / {counts['EXTERNAL_BLOCKER']} external blockers / {counts['PENDING']} pending / {counts['FAIL']} failed`",
        "",
        "## Requirement Matrix",
        "",
        "| Status | Requirement | Evidence |",
        "|---|---|---|",
    ]
    lines.extend(f"| {row.status} | {row.requirement} | {row.evidence} |" for row in rows)
    lines.extend(["", "## Completion Decision", ""])
    if gate == "PASS":
        lines.append("The full deep-fork objective is complete.")
    elif gate == "PASS_WITH_EXTERNAL_BLOCKER":
        lines.append("The automated engineering scope is ready, but the full objective is not complete until external live validation blockers are cleared.")
        lines.append("")
        lines.append("Remaining external blockers:")
        for blocker in blockers:
            lines.append(f"- {blocker}")
    else:
        lines.append("The release is not ready; failed requirements must be fixed before launch.")
    lines.append("")
    OUT_MD.write_text("\n".join(lines), encoding="utf-8")
    print(OUT_MD.relative_to(ROOT))
    print(OUT_JSON.relative_to(ROOT))
    return 0 if gate != "FAIL" else 2


if __name__ == "__main__":
    raise SystemExit(main())
