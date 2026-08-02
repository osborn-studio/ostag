"""Identity-scoped, allowlisted operational reporting for the monitor service."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from html import escape
from pathlib import Path
from typing import Callable, Literal

from ostag.core.identity import Principal
from ostag.work_monitor.identity import IdentityStore
from ostag.work_monitor.redaction import sanitize_public_text
from ostag.work_monitor.store import MonitorStore


ReportScope = Literal["personal", "team"]
_CONNECTORS = ("dingtalk", "feishu", "github", "gitlab", "lark")
_TASK_STATES = ("triage", "todo", "ready", "running", "blocked", "done")
_GATE_STATES = ("passed", "missing", "failed", "unknown", "waived")


def _iso(moment: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def _bounded_id(value: object, limit: int = 128) -> str:
    return sanitize_public_text(str(value or ""), limit=limit)


@dataclass(frozen=True)
class OperationalReport:
    generated_at: str
    scope: ReportScope
    summary: dict[str, object]
    instances: tuple[dict[str, object], ...]
    workstreams: tuple[dict[str, object], ...]
    completion: tuple[dict[str, object], ...]
    connectors: tuple[dict[str, object], ...]
    escalations: tuple[dict[str, object], ...]
    errors: tuple[dict[str, str], ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "schemaVersion": 1,
            "generatedAt": self.generated_at,
            "scope": self.scope,
            "summary": dict(self.summary),
            "instances": [dict(item) for item in self.instances],
            "workstreams": [dict(item) for item in self.workstreams],
            "completion": [dict(item) for item in self.completion],
            "connectors": [dict(item) for item in self.connectors],
            "escalations": [dict(item) for item in self.escalations],
            "errors": [dict(item) for item in self.errors],
        }


class OperationalReportService:
    def __init__(
        self,
        store: MonitorStore,
        identities: IdentityStore,
        *,
        workstream_path: Path,
        connector_evidence_dir: Path,
        degraded_seconds: int,
        offline_seconds: int,
        max_items: int = 100,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not 0 < degraded_seconds < offline_seconds:
            raise ValueError("report freshness requires 0 < degraded < offline")
        if not 1 <= max_items <= 500:
            raise ValueError("report max_items must be in 1..500")
        self.store = store
        self.identities = identities
        self.workstream_path = Path(workstream_path)
        self.connector_evidence_dir = Path(connector_evidence_dir)
        self.degraded_seconds = degraded_seconds
        self.offline_seconds = offline_seconds
        self.max_items = max_items
        self.clock = clock or (lambda: datetime.now(UTC))

    def build(self, principal: Principal, *, scope: ReportScope) -> OperationalReport:
        now = self.clock()
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        now = now.astimezone(UTC)
        instance_ids = self._instance_scope(principal, scope)
        errors: list[dict[str, str]] = []
        instances = self._instances(instance_ids, now)
        completion = self._completion(instance_ids, errors)
        escalations = self._escalations(instance_ids)
        workstreams = self._workstreams(instance_ids, errors)
        connectors = self._connectors(now, errors)
        total_cost = sum(int(item["costCredits"]) for item in workstreams)
        active_escalations = sum(
            item["state"] in {"open", "acknowledged"} for item in escalations
        )
        connector_counts = {
            state: sum(item["status"] == state for item in connectors)
            for state in ("ready", "degraded", "blocked", "unknown", "error")
        }
        summary: dict[str, object] = {
            "instanceCount": len(instances),
            "workstreamCount": len(workstreams),
            "completionCount": len(completion),
            "activeEscalationCount": active_escalations,
            "costCredits": total_cost,
            "connectorCounts": connector_counts,
        }
        return OperationalReport(
            generated_at=_iso(now),
            scope=scope,
            summary=summary,
            instances=instances,
            workstreams=workstreams,
            completion=completion,
            connectors=connectors,
            escalations=escalations,
            errors=tuple(
                {"component": component, "code": code}
                for component, code in sorted(
                    {(item["component"], item["code"]) for item in errors}
                )
            ),
        )

    def _instance_scope(self, principal: Principal, scope: ReportScope) -> frozenset[str]:
        if scope not in ("personal", "team"):
            raise ValueError("report scope must be personal or team")
        if scope == "team":
            if not self.identities.is_global_owner(principal):
                raise PermissionError("team report requires a bound owner or admin")
            return self.identities.team_instances()
        allowed = self.identities.allowed_instances(principal)
        if allowed is None:
            return self.identities.personal_instances(principal)
        return allowed

    def _instances(
        self,
        instance_ids: frozenset[str],
        now: datetime,
    ) -> tuple[dict[str, object], ...]:
        if not instance_ids:
            return ()
        ordered = tuple(sorted(instance_ids))
        placeholders = ",".join("?" for _ in ordered)
        with self.store.connect() as connection:
            rows = connection.execute(
                f"SELECT instance_id,last_seen_at FROM instances "  # noqa: S608
                f"WHERE instance_id IN ({placeholders})",
                ordered,
            ).fetchall()
            task_rows = connection.execute(
                f"SELECT instance_id,status,COUNT(*) AS count FROM tasks "  # noqa: S608
                f"WHERE instance_id IN ({placeholders}) GROUP BY instance_id,status",
                ordered,
            ).fetchall()
        seen = {str(row["instance_id"]): row["last_seen_at"] for row in rows}
        task_counts = {
            (str(row["instance_id"]), str(row["status"])): int(row["count"])
            for row in task_rows
        }
        names = self.identities.instance_display_names(instance_ids)
        output = []
        for instance_id in ordered[: self.max_items]:
            state_counts = {
                state: task_counts.get((instance_id, state), 0)
                for state in _TASK_STATES
            }
            last_seen = seen.get(instance_id)
            output.append(
                {
                    "instanceId": _bounded_id(instance_id),
                    "displayName": _bounded_id(names.get(instance_id, instance_id), 80),
                    "freshness": self._freshness(last_seen, now),
                    "tasks": {
                        "total": sum(state_counts.values()),
                        "byState": state_counts,
                    },
                }
            )
        return tuple(output)

    def _freshness(self, value: object, now: datetime) -> dict[str, object]:
        observed = _parse_timestamp(value)
        if observed is None:
            return {"state": "unknown", "lastSeenAt": None}
        age = max(0, int((now - observed).total_seconds()))
        if age <= self.degraded_seconds:
            state = "fresh"
        elif age <= self.offline_seconds:
            state = "stale"
        else:
            state = "offline"
        return {"state": state, "lastSeenAt": _iso(observed)}

    def _completion(
        self, instance_ids: frozenset[str], errors: list[dict[str, str]]
    ) -> tuple[dict[str, object], ...]:
        if not instance_ids:
            return ()
        ordered = tuple(sorted(instance_ids))
        placeholders = ",".join("?" for _ in ordered)
        with self.store.connect() as connection:
            rows = connection.execute(
                f"SELECT t.instance_id,t.work_thread_key,t.updated_at,a.state,a.assessment_json,"  # noqa: S608
                "(SELECT COUNT(*) FROM completion_evidence e WHERE e.instance_id=t.instance_id "
                "AND e.work_thread_key=t.work_thread_key) AS evidence_count "
                "FROM completion_threads t LEFT JOIN completion_assessments a "
                "ON a.instance_id=t.instance_id AND a.assessment_id=t.current_assessment_id "
                f"WHERE t.instance_id IN ({placeholders}) "
                "ORDER BY t.instance_id,t.work_thread_key LIMIT ?",
                (*ordered, self.max_items),
            ).fetchall()
        output = []
        for row in rows:
            evidence_backed = False
            gate_counts = {state: 0 for state in _GATE_STATES}
            payload = row["assessment_json"]
            if payload:
                try:
                    assessment = json.loads(str(payload))
                    if not isinstance(assessment, dict):
                        raise ValueError
                    evidence_backed = assessment.get("evidenceBacked") is True
                    gate_results = assessment.get("gateResults", [])
                    if not isinstance(gate_results, list):
                        raise ValueError
                    for result in gate_results:
                        if isinstance(result, dict) and result.get("state") in gate_counts:
                            gate_counts[str(result["state"])] += 1
                except (json.JSONDecodeError, ValueError, TypeError):
                    errors.append(
                        {"component": "completion", "code": "malformed_assessment"}
                    )
            output.append(
                {
                    "instanceId": _bounded_id(row["instance_id"]),
                    "workThreadKey": _bounded_id(row["work_thread_key"]),
                    "state": _bounded_id(row["state"] or "unassessed", 32),
                    "evidenceBacked": evidence_backed,
                    "evidenceCount": int(row["evidence_count"]),
                    "gateCounts": gate_counts,
                    "updatedAt": _bounded_id(row["updated_at"], 40),
                }
            )
        return tuple(output)

    def _escalations(self, instance_ids: frozenset[str]) -> tuple[dict[str, object], ...]:
        if not instance_ids:
            return ()
        ordered = tuple(sorted(instance_ids))
        placeholders = ",".join("?" for _ in ordered)
        with self.store.connect() as connection:
            rows = connection.execute(
                f"SELECT instance_id,escalation_id,work_thread_key,class,state,updated_at "  # noqa: S608
                f"FROM human_escalations WHERE instance_id IN ({placeholders}) "
                "ORDER BY instance_id,created_at,escalation_id LIMIT ?",
                (*ordered, self.max_items),
            ).fetchall()
        return tuple(
            {
                "instanceId": _bounded_id(row["instance_id"]),
                "escalationId": _bounded_id(row["escalation_id"], 80),
                "workThreadKey": _bounded_id(row["work_thread_key"]),
                "class": _bounded_id(row["class"], 32),
                "state": _bounded_id(row["state"], 32),
                "updatedAt": _bounded_id(row["updated_at"], 40),
            }
            for row in rows
        )

    def _workstreams(
        self,
        instance_ids: frozenset[str],
        errors: list[dict[str, str]],
    ) -> tuple[dict[str, object], ...]:
        if not self.workstream_path.is_file():
            return ()
        try:
            uri = self.workstream_path.resolve().as_uri() + "?mode=ro"
            with sqlite3.connect(uri, uri=True) as connection:
                connection.row_factory = sqlite3.Row
                workstream_rows = connection.execute(
                    "SELECT workstream_id,recipe_id,recipe_version,admitted_at "
                    "FROM workstreams ORDER BY admitted_at,workstream_id LIMIT ?",
                    (self.max_items,),
                ).fetchall()
                selected_ids = tuple(str(row["workstream_id"]) for row in workstream_rows)
                if selected_ids:
                    placeholders = ",".join("?" for _ in selected_ids)
                    run_rows = connection.execute(
                        f"SELECT workstream_id,run_id,cost_credits,attached_at,"  # noqa: S608
                        "COUNT(*) OVER(PARTITION BY workstream_id) AS total_runs "
                        f"FROM workstream_runs WHERE workstream_id IN ({placeholders}) "
                        "ORDER BY workstream_id,ordinal LIMIT ?",
                        (*selected_ids, self.max_items * 100),
                    ).fetchall()
                else:
                    run_rows = []
        except (OSError, sqlite3.DatabaseError):
            errors.append({"component": "workstreams", "code": "unavailable"})
            return ()
        run_ids = tuple(sorted({str(run["run_id"]) for run in run_rows}))
        task_instances: dict[str, set[str]] = {}
        task_statuses: dict[tuple[str, str], str] = {}
        if run_ids:
            placeholders = ",".join("?" for _ in run_ids)
            with self.store.connect() as connection:
                task_rows = connection.execute(
                    f"SELECT instance_id,task_id,status FROM tasks "  # noqa: S608
                    f"WHERE task_id IN ({placeholders})",
                    run_ids,
                ).fetchall()
            for task in task_rows:
                task_id = str(task["task_id"])
                instance_id = str(task["instance_id"])
                task_instances.setdefault(task_id, set()).add(instance_id)
                task_statuses[(instance_id, task_id)] = str(task["status"])
        runs_by_workstream: dict[str, list[sqlite3.Row]] = {}
        for run in run_rows:
            runs_by_workstream.setdefault(str(run["workstream_id"]), []).append(run)
        output = []
        for workstream in workstream_rows:
            workstream_id = str(workstream["workstream_id"])
            runs = runs_by_workstream.get(workstream_id, [])
            if runs and len(runs) != int(runs[0]["total_runs"]):
                continue
            owners = [task_instances.get(str(run["run_id"]), set()) for run in runs]
            if not runs or any(len(owner) != 1 for owner in owners):
                continue
            associated = {next(iter(owner)) for owner in owners}
            if len(associated) != 1:
                continue
            instance_id = next(iter(associated))
            if instance_id not in instance_ids:
                continue
            statuses = [
                task_statuses[(instance_id, str(run["run_id"]))] for run in runs
            ]
            if any(status == "blocked" for status in statuses):
                state = "blocked"
            elif statuses and all(status == "done" for status in statuses):
                state = "completed"
            else:
                state = "running"
            updated_at = max(
                [str(workstream["admitted_at"])]
                + [str(run["attached_at"]) for run in runs]
            )
            output.append(
                {
                    "instanceId": _bounded_id(instance_id),
                    "workstreamId": _bounded_id(workstream_id),
                    "recipeId": _bounded_id(workstream["recipe_id"], 80),
                    "recipeVersion": int(workstream["recipe_version"]),
                    "state": state,
                    "childRunCount": len(runs),
                    "costCredits": sum(int(run["cost_credits"]) for run in runs),
                    "updatedAt": _bounded_id(updated_at, 40),
                }
            )
        return tuple(output)

    def _connectors(
        self, now: datetime, errors: list[dict[str, str]]
    ) -> tuple[dict[str, object], ...]:
        output = []
        for platform in _CONNECTORS:
            path = self.connector_evidence_dir / f"connector-live-{platform}.json"
            if not path.is_file():
                output.append(
                    {
                        "platform": platform,
                        "status": "unknown",
                        "readReady": False,
                        "writeReady": False,
                        "observedAt": None,
                        "freshness": "unknown",
                    }
                )
                continue
            try:
                if path.stat().st_size > 262_144:
                    raise ValueError
                payload = json.loads(path.read_text(encoding="utf-8"))
                if (
                    not isinstance(payload, dict)
                    or payload.get("schema_version") != 1
                    or payload.get("platform") != platform
                    or payload.get("mode") != "live"
                    or payload.get("status") not in {"ready", "degraded", "blocked"}
                    or type(payload.get("read_ready")) is not bool
                    or type(payload.get("write_ready")) is not bool
                ):
                    raise ValueError
                observed = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
                age = max(0, int((now - observed).total_seconds()))
                freshness = "fresh" if age <= self.offline_seconds else "stale"
                output.append(
                    {
                        "platform": platform,
                        "status": payload["status"],
                        "readReady": payload["read_ready"],
                        "writeReady": payload["write_ready"],
                        "observedAt": _iso(observed),
                        "freshness": freshness,
                    }
                )
            except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
                errors.append(
                    {"component": f"connectors:{platform}", "code": "malformed_evidence"}
                )
                output.append(
                    {
                        "platform": platform,
                        "status": "error",
                        "readReady": False,
                        "writeReady": False,
                        "observedAt": None,
                        "freshness": "unknown",
                    }
                )
        return tuple(output)


def render_operational_report_html(report: dict[str, object]) -> str:
    def text(value: object) -> str:
        return escape(str(value if value is not None else "-"), quote=True)

    summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
    instances = report.get("instances") if isinstance(report.get("instances"), list) else []
    workstreams = report.get("workstreams") if isinstance(report.get("workstreams"), list) else []
    completion = report.get("completion") if isinstance(report.get("completion"), list) else []
    connectors = report.get("connectors") if isinstance(report.get("connectors"), list) else []
    escalations = report.get("escalations") if isinstance(report.get("escalations"), list) else []

    def rows(items: list, fields: tuple[str, ...]) -> str:
        rendered = []
        for item in items:
            if not isinstance(item, dict):
                continue
            rendered.append(
                "<tr>" + "".join(f"<td>{text(item.get(field))}</td>" for field in fields) + "</tr>"
            )
        return "".join(rendered) or f'<tr><td colspan="{len(fields)}">-</td></tr>'

    instance_rows = []
    for item in instances:
        if not isinstance(item, dict):
            continue
        freshness = item.get("freshness") if isinstance(item.get("freshness"), dict) else {}
        tasks = item.get("tasks") if isinstance(item.get("tasks"), dict) else {}
        instance_rows.append(
            "<tr>"
            f"<td>{text(item.get('displayName'))}</td>"
            f"<td>{text(item.get('instanceId'))}</td>"
            f"<td>{text(freshness.get('state'))}</td>"
            f"<td>{text(tasks.get('total'))}</td>"
            "</tr>"
        )
    instance_body = "".join(instance_rows) or '<tr><td colspan="4">-</td></tr>'
    style = (
        "body{font-family:system-ui,sans-serif;margin:24px;color:#171717;background:#fff}"
        "h1{font-size:22px}h2{font-size:16px;margin-top:24px}"
        "table{border-collapse:collapse;width:100%;font-size:13px}"
        "th,td{border-bottom:1px solid #ddd;padding:7px;text-align:left}"
        ".summary{display:flex;gap:18px;flex-wrap:wrap}.metric{font-weight:600}"
    )
    return (
        "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
        f"<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><style>{style}</style>"
        "<title>OStag Operational Report</title></head><body>"
        "<h1>OStag Operational Report</h1>"
        f"<div class=\"summary\"><span class=\"metric\">Instances {text(summary.get('instanceCount'))}</span>"
        f"<span class=\"metric\">Workstreams {text(summary.get('workstreamCount'))}</span>"
        f"<span class=\"metric\">Cost {text(summary.get('costCredits'))}</span>"
        f"<span>{text(report.get('generatedAt'))}</span></div>"
        "<h2>Instances</h2><table><thead><tr><th>Name</th><th>ID</th><th>Freshness</th><th>Tasks</th></tr></thead>"
        f"<tbody>{instance_body}</tbody></table>"
        "<h2>Workstreams</h2><table><thead><tr><th>ID</th><th>Instance</th><th>State</th><th>Cost</th></tr></thead><tbody>"
        f"{rows(workstreams, ('workstreamId', 'instanceId', 'state', 'costCredits'))}</tbody></table>"
        "<h2>Completion</h2><table><thead><tr><th>Thread</th><th>Instance</th><th>State</th><th>Evidence</th></tr></thead><tbody>"
        f"{rows(completion, ('workThreadKey', 'instanceId', 'state', 'evidenceCount'))}</tbody></table>"
        "<h2>Connectors</h2><table><thead><tr><th>Platform</th><th>Status</th><th>Read</th><th>Freshness</th></tr></thead><tbody>"
        f"{rows(connectors, ('platform', 'status', 'readReady', 'freshness'))}</tbody></table>"
        "<h2>Escalations</h2><table><thead><tr><th>ID</th><th>Instance</th><th>Class</th><th>State</th></tr></thead><tbody>"
        f"{rows(escalations, ('escalationId', 'instanceId', 'class', 'state'))}</tbody></table>"
        "</body></html>"
    )
