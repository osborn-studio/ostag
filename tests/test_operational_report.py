from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ostag.config import load_settings
from ostag.core.identity import Principal
from ostag.core.workstreams import (
    RecipeDefinition,
    RecipeStep,
    WorkstreamAdmission,
    WorkstreamStore,
)
from ostag.main import main_async
from ostag.work_monitor.identity import Destination, IdentityStore
from ostag.work_monitor.operational_report import (
    OperationalReportService,
    render_operational_report_html,
)
from ostag.work_monitor.projection import Projector
from ostag.work_monitor.server import HumanCredential, ServerAuth, create_app
from ostag.work_monitor.service import WorkMonitorService
from ostag.work_monitor.store import MonitorStore


NOW = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)
MEMBER = Principal("feishu", "member")
OWNER = Principal("feishu", "owner")


def _bind(
    identities: IdentityStore,
    principal: Principal,
    instance_id: str,
    *,
    role: str,
    display_name: str,
) -> Destination:
    destination = Destination("feishu", f"chat-{principal.actor_id}", "private")
    identities.bind_private(
        owner=principal,
        destination=destination,
        local_admin=True,
        role=role,
    )
    identities.bind_instance(
        principal,
        instance_id,
        display_name=display_name,
        bound_at="2026-08-01T00:00:00Z",
    )
    return destination


def _task(
    store: MonitorStore,
    instance_id: str,
    run_id: str,
    *,
    status: str,
    seen_at: str,
) -> None:
    with store.connect() as connection:
        connection.execute(
            "INSERT INTO instances(instance_id,last_sequence,last_seen_at) VALUES(?,1,?) "
            "ON CONFLICT(instance_id) DO UPDATE SET last_seen_at=excluded.last_seen_at",
            (instance_id, seen_at),
        )
        connection.execute(
            "INSERT INTO tasks(instance_id,task_id,title,project,status,agent,progress,"
            "duration_seconds,summary,error_class,evidence_ref,occurred_at,version) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                instance_id,
                run_id,
                "raw prompt Authorization: Bearer never-show-this",
                "/Users/private/secret-repo",
                status,
                "codex",
                100 if status == "done" else 50,
                60,
                "password=hunter2 full log must stay private",
                None,
                "/tmp/private.log",
                seen_at,
                1,
            ),
        )


def _completion(store: MonitorStore, instance_id: str, thread: str, *, state: str) -> None:
    assessment_id = f"assessment-{instance_id}"
    assessment = {
        "evidenceBacked": True,
        "gateResults": [
            {"state": "passed"},
            {"state": "missing"},
        ],
        "rawPrompt": "Authorization: Bearer never-show-this",
    }
    with store.connect() as connection:
        connection.execute(
            "INSERT INTO completion_threads VALUES(?,?,?,?,?)",
            (instance_id, thread, assessment_id, "2026-08-01T00:00:00Z", NOW.isoformat()),
        )
        connection.execute(
            "INSERT INTO completion_assessments VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                instance_id,
                assessment_id,
                thread,
                "contract-1",
                1,
                1,
                1,
                "",
                "digest",
                state,
                json.dumps(assessment),
                NOW.isoformat(),
            ),
        )
        connection.execute(
            "INSERT INTO completion_evidence VALUES(?,?,?,?,?,?,?)",
            (
                instance_id,
                f"evidence-{instance_id}",
                thread,
                1,
                json.dumps({"secret": "Bearer never-show-this"}),
                NOW.isoformat(),
                NOW.isoformat(),
            ),
        )


def _escalation(store: MonitorStore, instance_id: str, thread: str) -> None:
    payload = {
        "summary": "<script>alert(1)</script>",
        "reason": "token=never-show-this",
    }
    with store.connect() as connection:
        connection.execute(
            "INSERT INTO human_escalations VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                instance_id,
                f"escalation-{instance_id}",
                thread,
                "decision",
                "open",
                "dedupe",
                "dedupe",
                json.dumps(payload),
                NOW.isoformat(),
                NOW.isoformat(),
            ),
        )


def _workstream(path: Path, run_id: str, *, key: str, cost: int) -> str:
    store = WorkstreamStore(path)
    recipe = RecipeDefinition(
        recipe_id="delivery",
        version=1,
        title="Delivery",
        steps=(RecipeStep("verify", "Verify"),),
        max_runs=5,
        max_cost_credits=100,
        required_evidence=(),
        allowed_operations=("github.read_pr",),
    )
    store.register_recipe(recipe)
    admitted = store.admit(WorkstreamAdmission(key, "delivery", 1, "raw private goal"))
    store.attach_run(admitted.workstream_id, run_id, cost_credits=cost)
    return admitted.workstream_id


def _connector_evidence(directory: Path, platform: str, status: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"connector-live-{platform}.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "platform": platform,
                "mode": "live",
                "status": status,
                "read_ready": status == "ready",
                "write_ready": False,
                "checks": [],
                "target": {"path": "/private/path/must-not-leak"},
            }
        ),
        encoding="utf-8",
    )


@pytest.fixture()
def report_fixture(tmp_path: Path):
    store = MonitorStore(tmp_path / "monitor.sqlite3")
    identities = IdentityStore(store.path)
    member_destination = _bind(
        identities, MEMBER, "member-mac", role="member", display_name="Member <Mac>"
    )
    owner_destination = _bind(
        identities, OWNER, "owner-mac", role="owner", display_name="Owner & Mac"
    )
    _task(store, "member-mac", "run_member", status="running", seen_at=NOW.isoformat())
    _task(store, "owner-mac", "run_owner", status="done", seen_at=NOW.isoformat())
    _completion(store, "member-mac", "member-thread", state="pending")
    _completion(store, "owner-mac", "owner-thread", state="satisfied")
    _escalation(store, "member-mac", "member-thread")
    workstream_path = tmp_path / "workstreams.sqlite3"
    member_workstream = _workstream(workstream_path, "run_member", key="member", cost=3)
    owner_workstream = _workstream(workstream_path, "run_owner", key="owner", cost=7)
    evidence_dir = tmp_path / "evidence"
    _connector_evidence(evidence_dir, "github", "ready")
    service = OperationalReportService(
        store,
        identities,
        workstream_path=workstream_path,
        connector_evidence_dir=evidence_dir,
        degraded_seconds=120,
        offline_seconds=600,
        clock=lambda: NOW,
    )
    return (
        store,
        identities,
        service,
        member_destination,
        owner_destination,
        member_workstream,
        owner_workstream,
        workstream_path,
        evidence_dir,
    )


def test_operational_report_scopes_member_and_owner_without_raw_data(report_fixture) -> None:
    _, _, service, _, _, member_workstream, owner_workstream, _, _ = report_fixture

    member = service.build(MEMBER, scope="personal").to_dict()
    team = service.build(OWNER, scope="team").to_dict()

    assert [item["instanceId"] for item in member["instances"]] == ["member-mac"]
    assert [item["workstreamId"] for item in member["workstreams"]] == [member_workstream]
    assert member["summary"]["costCredits"] == 3
    assert member["presence"]["state"] == "needs_attention"
    assert member["presence"]["counts"]["needs_attention"] == 1
    assert member["instances"][0]["presence"]["reasonCode"] == "escalation_active"
    assert {item["workstreamId"] for item in team["workstreams"]} == {
        member_workstream,
        owner_workstream,
    }
    assert team["summary"]["costCredits"] == 10
    assert team["presence"]["state"] == "needs_attention"
    assert {item["instanceId"]: item["presence"]["state"] for item in team["instances"]} == {
        "member-mac": "needs_attention",
        "owner-mac": "available",
    }
    serialized = json.dumps(team)
    for forbidden in (
        "never-show-this",
        "hunter2",
        "raw private goal",
        "/Users/private",
        "/tmp/private.log",
        "/private/path",
    ):
        assert forbidden not in serialized
    with pytest.raises(PermissionError, match="team"):
        service.build(MEMBER, scope="team")


def test_operational_report_handles_empty_stale_and_component_errors(tmp_path: Path) -> None:
    store = MonitorStore(tmp_path / "monitor.sqlite3")
    identities = IdentityStore(store.path)
    _bind(identities, MEMBER, "stale-mac", role="member", display_name="Stale Mac")
    with store.connect() as connection:
        connection.execute(
            "INSERT INTO instances VALUES(?,?,?)",
            ("stale-mac", 0, "2026-08-02T10:00:00Z"),
        )
    workstream_path = tmp_path / "broken-workstreams.sqlite3"
    workstream_path.write_text("not sqlite", encoding="utf-8")
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    (evidence_dir / "connector-live-github.json").write_text("{broken", encoding="utf-8")
    service = OperationalReportService(
        store,
        identities,
        workstream_path=workstream_path,
        connector_evidence_dir=evidence_dir,
        degraded_seconds=120,
        offline_seconds=600,
        clock=lambda: NOW,
    )

    report = service.build(MEMBER, scope="personal").to_dict()

    assert report["instances"][0]["freshness"]["state"] == "offline"
    assert report["instances"][0]["presence"]["state"] == "offline"
    assert report["presence"]["state"] == "offline"
    assert report["workstreams"] == []
    assert report["completion"] == []
    assert report["escalations"] == []
    assert {error["component"] for error in report["errors"]} == {
        "connectors:github",
        "workstreams",
    }
    assert "broken-workstreams" not in json.dumps(report)


def test_presence_does_not_reuse_legacy_heartbeat_after_device_revocation(
    tmp_path: Path,
) -> None:
    store = MonitorStore(tmp_path / "monitor.sqlite3")
    identities = IdentityStore(store.path)
    _bind(identities, MEMBER, "revoked-mac", role="member", display_name="Revoked Mac")
    with store.connect() as connection:
        connection.execute(
            "INSERT INTO instances VALUES(?,?,?)",
            ("revoked-mac", 1, NOW.isoformat()),
        )
        connection.execute(
            "INSERT INTO monitor_devices(device_id,principal_platform,actor_id,display_name,"
            "public_key,paired_at,last_seen_at,revoked_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                "revoked-mac",
                "feishu",
                "member",
                "Revoked Mac",
                "test-public-key",
                "2026-08-01T00:00:00Z",
                NOW.isoformat(),
                "2026-08-02T11:59:00Z",
            ),
        )
    service = OperationalReportService(
        store,
        identities,
        workstream_path=tmp_path / "workstreams.sqlite3",
        connector_evidence_dir=tmp_path / "evidence",
        degraded_seconds=120,
        offline_seconds=600,
        clock=lambda: NOW,
    )

    report = service.build(MEMBER, scope="personal").to_dict()

    assert report["instances"][0]["freshness"] == {
        "state": "unknown",
        "lastSeenAt": None,
    }
    assert report["instances"][0]["presence"]["state"] == "setup_required"
    assert report["instances"][0]["presence"]["reasonCode"] == "instance_missing"


def test_operational_report_json_is_stable_and_html_escapes(report_fixture) -> None:
    _, _, service, _, _, _, _, _, _ = report_fixture

    first = service.build(OWNER, scope="team").to_dict()
    second = service.build(OWNER, scope="team").to_dict()
    html = render_operational_report_html(first)

    assert first == second
    assert first["schemaVersion"] == 2
    assert first["presence"]["state"] == "needs_attention"
    assert "Presence needs_attention" in html
    assert "Owner &amp; Mac" in html
    assert "Member &lt;Mac&gt;" in html
    assert "<script>" not in html
    assert "never-show-this" not in html


def test_operational_report_http_authorization_and_feature_flag(report_fixture) -> None:
    (
        store,
        identities,
        _,
        member_destination,
        owner_destination,
        _,
        _,
        workstream_path,
        evidence_dir,
    ) = report_fixture
    settings = load_settings(
        None,
        monitor_server_db=store.path,
        workstream_store_path=workstream_path,
        operational_report_connector_evidence_dir=evidence_dir,
        operational_report_enabled=True,
        monitor_scheduler_enabled=False,
    )
    auth = ServerAuth(
        {},
        human_credentials={
            "member-token": HumanCredential(MEMBER, member_destination),
            "owner-token": HumanCredential(OWNER, owner_destination),
        },
    )
    app = create_app(
        settings=settings,
        service=WorkMonitorService(store, identities),
        projector=Projector(store),
        auth=auth,
    )

    with TestClient(app) as client:
        assert client.get("/v1/report").status_code == 401
        member = client.get(
            "/v1/report", headers={"Authorization": "Bearer member-token"}
        )
        assert member.status_code == 200
        assert member.headers["cache-control"] == "no-store"
        assert [item["instanceId"] for item in member.json()["instances"]] == ["member-mac"]
        assert member.json()["presence"]["state"] == "needs_attention"
        denied = client.get(
            "/v1/report?scope=team", headers={"Authorization": "Bearer member-token"}
        )
        assert denied.status_code == 403
        team = client.get(
            "/v1/report.html?scope=team", headers={"Authorization": "Bearer owner-token"}
        )
        assert team.status_code == 200
        assert team.headers["content-type"].startswith("text/html")
        assert team.headers["cache-control"] == "no-store"
        assert team.headers["x-content-type-options"] == "nosniff"
        assert "default-src 'none'" in team.headers["content-security-policy"]
        assert "frame-ancestors 'none'" in team.headers["content-security-policy"]
        assert "Member &lt;Mac&gt;" in team.text

    disabled = create_app(
        settings=load_settings(
            None,
            monitor_server_db=store.path,
            operational_report_enabled=False,
            monitor_scheduler_enabled=False,
        ),
        service=WorkMonitorService(store, identities),
        projector=Projector(store),
        auth=auth,
    )
    with TestClient(disabled) as client:
        assert client.get(
            "/v1/report", headers={"Authorization": "Bearer owner-token"}
        ).status_code == 404


@pytest.mark.asyncio
async def test_operational_report_cli_is_identity_scoped(
    report_fixture,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _, _, _, _, _, _, workstream_path, evidence_dir = report_fixture
    monkeypatch.setenv("OSTAG_MONITOR_SERVER_DB", str(store.path))
    monkeypatch.setenv("OSTAG_WORKSTREAM_STORE_PATH", str(workstream_path))
    monkeypatch.setenv("OSTAG_OPERATIONAL_REPORT_CONNECTOR_EVIDENCE_DIR", str(evidence_dir))
    monkeypatch.setenv("OSTAG_OPERATIONAL_REPORT_ENABLED", "true")

    exit_code = await main_async(
        [
            "monitor",
            "report",
            "--platform",
            "feishu",
            "--actor",
            "member",
            "--scope",
            "personal",
            "--format",
            "json",
        ]
    )

    assert exit_code == 0
    report = json.loads(capsys.readouterr().out)
    assert [item["instanceId"] for item in report["instances"]] == ["member-mac"]
