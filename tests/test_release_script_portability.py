import os
import plistlib
import subprocess
from pathlib import Path


def test_release_readiness_defaults_to_repo_local_osstack_evidence() -> None:
    script = Path("scripts/release_readiness_audit.sh").read_text(encoding="utf-8")

    assert "/Users/gilbertchen" not in script
    assert 'MANAGEMENT_ROOT="${OSTAG_MANAGEMENT_ROOT:-$ROOT}"' in script
    assert 'report="${OSTAG_READINESS_REPORT:-release/readiness-audit.md}"' in script
    assert "LOOP-STATE.json" in script
    assert "git remote get-url upstream" not in script


def test_deploy_preflight_accepts_only_pass_or_structured_connector_blocker() -> None:
    script = Path("scripts/deploy_preflight.sh").read_text(encoding="utf-8")

    assert "for platform in feishu dingtalk github gitlab" in script
    assert '"$connector_code" -ne 0 && "$connector_code" -ne 3' in script


def test_release_scripts_avoid_bash4_case_conversion() -> None:
    scripts = Path("scripts")

    for path in scripts.glob("*.sh"):
        source = path.read_text(encoding="utf-8")
        assert ",,}" not in source, f"{path} requires Bash 4 lowercase expansion"
        assert "^^}" not in source, f"{path} requires Bash 4 uppercase expansion"


def test_launchd_renderer_uses_the_target_hosts_uv_binary(tmp_path: Path) -> None:
    output = tmp_path / "ai.ostag.feishu.plist"
    env = {**os.environ, "OSTAG_UV_BIN": "/usr/bin/true"}

    subprocess.run(
        ["bash", "scripts/render_launchd_plist.sh", str(output)],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )

    plist = plistlib.loads(output.read_bytes())
    assert plist["ProgramArguments"][0] == "/usr/bin/true"


def test_live_validation_is_bound_to_the_release_commit() -> None:
    recorder = Path("scripts/live_validation_record.sh").read_text(encoding="utf-8")
    validator = Path("scripts/validate_live_validation_record.sh").read_text(encoding="utf-8")

    assert "Release commit" in recorder
    assert "Release commit" in validator
    assert "Release version" in recorder
    assert "Release version" in validator
    assert "git rev-parse HEAD" in recorder
    assert "git merge-base --is-ancestor" in validator
