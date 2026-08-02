from pathlib import Path


def test_release_readiness_defaults_to_repo_local_osstack_evidence() -> None:
    script = Path("scripts/release_readiness_audit.sh").read_text(encoding="utf-8")

    assert "/Users/gilbertchen" not in script
    assert 'MANAGEMENT_ROOT="${OSTAG_MANAGEMENT_ROOT:-$ROOT}"' in script
    assert 'report="${OSTAG_READINESS_REPORT:-release/readiness-audit.md}"' in script
    assert "LOOP-STATE.json" in script


def test_deploy_preflight_accepts_only_pass_or_structured_connector_blocker() -> None:
    script = Path("scripts/deploy_preflight.sh").read_text(encoding="utf-8")

    assert "for platform in feishu dingtalk github gitlab" in script
    assert '"$connector_code" -ne 0 && "$connector_code" -ne 3' in script
