"""Immutable recipes and durable multi-run workstreams."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Any

from ostag.core.schemas import SchemaError, new_id


_IDENTIFIER = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,79}")
_OPERATION = re.compile(r"[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*")


def _required_text(value: object, field: str, schema: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise SchemaError(
            code="missing_field",
            message=f"{schema}.{field} must be a non-empty string",
            recovery_hint=f"Provide {field}.",
        )


def _strict_keys(data: Mapping[str, Any], allowed: frozenset[str], schema: str) -> None:
    unknown = set(data) - allowed
    if unknown:
        raise SchemaError(
            code="unknown_field",
            message=f"{schema} does not accept: {', '.join(sorted(unknown))}",
            recovery_hint=f"Allowed fields: {', '.join(sorted(allowed))}.",
        )


def _string_tuple(value: object, field: str, schema: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise SchemaError(
            code="invalid_field",
            message=f"{schema}.{field} must contain non-empty strings",
            recovery_hint=f"Provide a string list for {field}.",
        )
    result = tuple(value)
    if len(set(result)) != len(result):
        raise SchemaError(
            code="duplicate_key",
            message=f"{schema}.{field} contains duplicates",
            recovery_hint=f"Deduplicate {field}.",
        )
    return result


@dataclass(frozen=True)
class RecipeStep:
    id: str
    title: str
    depends_on: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not _IDENTIFIER.fullmatch(self.id):
            raise SchemaError("invalid_identifier", "RecipeStep.id is invalid", "Use a stable id.")
        _required_text(self.title, "title", "RecipeStep")
        dependencies = _string_tuple(self.depends_on, "depends_on", "RecipeStep")
        if self.id in dependencies:
            raise SchemaError("recipe_cycle", "A recipe step cannot depend on itself", "Fix dependsOn.")
        object.__setattr__(self, "depends_on", dependencies)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"id": self.id, "title": self.title}
        if self.depends_on:
            payload["dependsOn"] = list(self.depends_on)
        return payload

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> RecipeStep:
        _strict_keys(data, frozenset({"id", "title", "dependsOn"}), "RecipeStep")
        return cls(
            id=data.get("id", ""),
            title=data.get("title", ""),
            depends_on=_string_tuple(data.get("dependsOn", ()), "dependsOn", "RecipeStep"),
        )


@dataclass(frozen=True)
class RecipeDefinition:
    recipe_id: str
    version: int
    title: str
    steps: tuple[RecipeStep, ...]
    max_runs: int
    max_cost_credits: int
    required_evidence: tuple[str, ...] = ()
    allowed_operations: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.recipe_id, str) or not _IDENTIFIER.fullmatch(self.recipe_id):
            raise SchemaError("invalid_identifier", "RecipeDefinition.recipe_id is invalid", "Use a stable id.")
        if isinstance(self.version, bool) or not isinstance(self.version, int) or self.version < 1:
            raise SchemaError("invalid_number", "Recipe version must be positive", "Use version >= 1.")
        _required_text(self.title, "title", "RecipeDefinition")
        if not isinstance(self.steps, tuple) or not self.steps:
            raise SchemaError("missing_field", "Recipe steps are required", "Declare at least one step.")
        if not 1 <= self.max_runs <= 100:
            raise SchemaError("invalid_number", "Recipe max_runs must be in 1..100", "Use 1..100.")
        if (
            isinstance(self.max_cost_credits, bool)
            or not isinstance(self.max_cost_credits, int)
            or self.max_cost_credits < 0
        ):
            raise SchemaError(
                "invalid_number", "Recipe max_cost_credits cannot be negative", "Use zero or more."
            )
        evidence = _string_tuple(
            self.required_evidence, "required_evidence", "RecipeDefinition"
        )
        operations = _string_tuple(
            self.allowed_operations, "allowed_operations", "RecipeDefinition"
        )
        if any(not _OPERATION.fullmatch(item) for item in operations):
            raise SchemaError(
                "invalid_operation",
                "Recipe operations must use connector.operation",
                "Use lowercase connector.operation names.",
            )
        ids = {step.id for step in self.steps}
        if len(ids) != len(self.steps):
            raise SchemaError("duplicate_key", "Recipe step ids must be unique", "Rename duplicate steps.")
        if any(dependency not in ids for step in self.steps for dependency in step.depends_on):
            raise SchemaError(
                "invalid_reference", "Recipe dependency references an unknown step", "Fix dependsOn."
            )
        self._assert_acyclic()
        object.__setattr__(self, "required_evidence", evidence)
        object.__setattr__(self, "allowed_operations", operations)

    def _assert_acyclic(self) -> None:
        dependencies = {step.id: step.depends_on for step in self.steps}
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(step_id: str) -> None:
            if step_id in visiting:
                raise SchemaError(
                    "recipe_cycle", "Recipe dependencies contain a cycle", "Remove the cycle."
                )
            if step_id in visited:
                return
            visiting.add(step_id)
            for dependency in dependencies[step_id]:
                visit(dependency)
            visiting.remove(step_id)
            visited.add(step_id)

        for step_id in dependencies:
            visit(step_id)

    @property
    def digest(self) -> str:
        encoded = json.dumps(
            self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "recipeId": self.recipe_id,
            "version": self.version,
            "title": self.title,
            "steps": [step.to_dict() for step in self.steps],
            "maxRuns": self.max_runs,
            "maxCostCredits": self.max_cost_credits,
            "requiredEvidence": list(self.required_evidence),
            "allowedOperations": list(self.allowed_operations),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> RecipeDefinition:
        _strict_keys(
            data,
            frozenset(
                {
                    "recipeId",
                    "version",
                    "title",
                    "steps",
                    "maxRuns",
                    "maxCostCredits",
                    "requiredEvidence",
                    "allowedOperations",
                }
            ),
            "RecipeDefinition",
        )
        steps = data.get("steps", ())
        if not isinstance(steps, (list, tuple)) or not all(
            isinstance(item, Mapping) for item in steps
        ):
            raise SchemaError("invalid_field", "Recipe steps must be objects", "Fix steps.")
        return cls(
            recipe_id=data.get("recipeId", ""),
            version=data.get("version", 0),
            title=data.get("title", ""),
            steps=tuple(RecipeStep.from_dict(item) for item in steps),
            max_runs=data.get("maxRuns", 0),
            max_cost_credits=data.get("maxCostCredits", -1),
            required_evidence=_string_tuple(
                data.get("requiredEvidence", ()), "requiredEvidence", "RecipeDefinition"
            ),
            allowed_operations=_string_tuple(
                data.get("allowedOperations", ()), "allowedOperations", "RecipeDefinition"
            ),
        )


@dataclass(frozen=True)
class WorkstreamAdmission:
    admission_key: str
    recipe_id: str
    recipe_version: int
    goal: str

    def __post_init__(self) -> None:
        for field in ("admission_key", "recipe_id", "goal"):
            _required_text(getattr(self, field), field, "WorkstreamAdmission")
        if not isinstance(self.recipe_version, int) or isinstance(self.recipe_version, bool) or self.recipe_version < 1:
            raise SchemaError("invalid_number", "Recipe version must be positive", "Use version >= 1.")


@dataclass(frozen=True)
class Workstream:
    workstream_id: str
    admission_key: str
    goal: str
    recipe_id: str
    recipe_version: int
    recipe_digest: str
    admitted_at: str
    max_runs: int
    max_cost_credits: int
    required_evidence: tuple[str, ...]
    allowed_operations: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "workstreamId": self.workstream_id,
            "admissionKey": self.admission_key,
            "goal": self.goal,
            "recipeId": self.recipe_id,
            "recipeVersion": self.recipe_version,
            "recipeDigest": self.recipe_digest,
            "admittedAt": self.admitted_at,
            "maxRuns": self.max_runs,
            "maxCostCredits": self.max_cost_credits,
            "requiredEvidence": list(self.required_evidence),
            "allowedOperations": list(self.allowed_operations),
        }


@dataclass(frozen=True)
class WorkstreamRun:
    run_id: str
    ordinal: int
    cost_credits: int
    attached_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "runId": self.run_id,
            "ordinal": self.ordinal,
            "costCredits": self.cost_credits,
            "attachedAt": self.attached_at,
        }


@dataclass(frozen=True)
class WorkstreamProjection:
    workstream_id: str
    state: str
    child_runs: int
    succeeded_runs: int
    missing_evidence: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "workstreamId": self.workstream_id,
            "state": self.state,
            "childRuns": self.child_runs,
            "succeededRuns": self.succeeded_runs,
            "missingEvidence": list(self.missing_evidence),
        }


class WorkstreamStore:
    def __init__(self, path: Path, *, max_batch: int = 25) -> None:
        if not 1 <= max_batch <= 100:
            raise ValueError("workstream max batch must be in 1..100")
        self.path = Path(path)
        self.max_batch = max_batch
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self.connect() as connection, connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS workstream_recipes (
                    recipe_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    digest TEXT NOT NULL,
                    snapshot_json TEXT NOT NULL,
                    registered_at TEXT NOT NULL,
                    PRIMARY KEY (recipe_id, version)
                );
                CREATE TABLE IF NOT EXISTS workstreams (
                    workstream_id TEXT PRIMARY KEY,
                    admission_key TEXT NOT NULL UNIQUE,
                    goal TEXT NOT NULL,
                    recipe_id TEXT NOT NULL,
                    recipe_version INTEGER NOT NULL,
                    recipe_digest TEXT NOT NULL,
                    recipe_snapshot_json TEXT NOT NULL,
                    admitted_at TEXT NOT NULL,
                    FOREIGN KEY (recipe_id, recipe_version)
                        REFERENCES workstream_recipes(recipe_id, version)
                );
                CREATE TABLE IF NOT EXISTS workstream_runs (
                    workstream_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    ordinal INTEGER NOT NULL,
                    cost_credits INTEGER NOT NULL,
                    attached_at TEXT NOT NULL,
                    PRIMARY KEY (workstream_id, run_id),
                    UNIQUE (workstream_id, ordinal),
                    FOREIGN KEY (workstream_id) REFERENCES workstreams(workstream_id)
                );
                """
            )

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat()

    def register_recipe(self, recipe: RecipeDefinition) -> RecipeDefinition:
        snapshot = json.dumps(
            recipe.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        with self.connect() as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT digest, snapshot_json FROM workstream_recipes WHERE recipe_id=? AND version=?",
                (recipe.recipe_id, recipe.version),
            ).fetchone()
            if existing is not None:
                if str(existing["digest"]) != recipe.digest:
                    raise SchemaError(
                        "recipe_version_conflict",
                        "A recipe version cannot be changed after registration",
                        "Publish a new recipe version.",
                    )
                return RecipeDefinition.from_dict(json.loads(existing["snapshot_json"]))
            connection.execute(
                "INSERT INTO workstream_recipes VALUES (?, ?, ?, ?, ?)",
                (recipe.recipe_id, recipe.version, recipe.digest, snapshot, self._now()),
            )
        return recipe

    def get_recipe(self, recipe_id: str, version: int) -> RecipeDefinition:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT snapshot_json FROM workstream_recipes WHERE recipe_id=? AND version=?",
                (recipe_id, version),
            ).fetchone()
        if row is None:
            raise SchemaError("recipe_not_found", "Recipe version was not found", "Register it first.")
        return RecipeDefinition.from_dict(json.loads(row["snapshot_json"]))

    def admit(self, request: WorkstreamAdmission) -> Workstream:
        with self.connect() as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            return self._admit(connection, request)

    def admit_batch(
        self, requests: tuple[WorkstreamAdmission, ...]
    ) -> tuple[Workstream, ...]:
        if len(requests) > self.max_batch:
            raise SchemaError(
                "batch_limit_exceeded",
                f"Workstream admission batch exceeds {self.max_batch}",
                "Split the batch.",
            )
        with self.connect() as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            return tuple(self._admit(connection, request) for request in requests)

    def _admit(
        self, connection: sqlite3.Connection, request: WorkstreamAdmission
    ) -> Workstream:
        existing = connection.execute(
            "SELECT * FROM workstreams WHERE admission_key=?", (request.admission_key,)
        ).fetchone()
        if existing is not None:
            workstream = self._workstream(existing)
            if (
                workstream.recipe_id != request.recipe_id
                or workstream.recipe_version != request.recipe_version
                or workstream.goal != request.goal
            ):
                raise SchemaError(
                    "admission_key_conflict",
                    "Admission key already identifies a different workstream",
                    "Use the original request or a new admission key.",
                )
            return workstream
        recipe_row = connection.execute(
            "SELECT digest, snapshot_json FROM workstream_recipes WHERE recipe_id=? AND version=?",
            (request.recipe_id, request.recipe_version),
        ).fetchone()
        if recipe_row is None:
            raise SchemaError("recipe_not_found", "Recipe version was not found", "Register it first.")
        workstream_id = new_id("workstream")
        admitted_at = self._now()
        connection.execute(
            "INSERT INTO workstreams VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                workstream_id,
                request.admission_key,
                request.goal,
                request.recipe_id,
                request.recipe_version,
                recipe_row["digest"],
                recipe_row["snapshot_json"],
                admitted_at,
            ),
        )
        row = connection.execute(
            "SELECT * FROM workstreams WHERE workstream_id=?", (workstream_id,)
        ).fetchone()
        assert row is not None
        return self._workstream(row)

    @staticmethod
    def _workstream(row: sqlite3.Row) -> Workstream:
        recipe = RecipeDefinition.from_dict(json.loads(row["recipe_snapshot_json"]))
        if recipe.digest != str(row["recipe_digest"]):
            raise SchemaError(
                "workstream_snapshot_corrupt",
                "Frozen workstream recipe digest does not match its snapshot",
                "Restore the workstream database from a trusted backup.",
            )
        return Workstream(
            workstream_id=str(row["workstream_id"]),
            admission_key=str(row["admission_key"]),
            goal=str(row["goal"]),
            recipe_id=str(row["recipe_id"]),
            recipe_version=int(row["recipe_version"]),
            recipe_digest=str(row["recipe_digest"]),
            admitted_at=str(row["admitted_at"]),
            max_runs=recipe.max_runs,
            max_cost_credits=recipe.max_cost_credits,
            required_evidence=recipe.required_evidence,
            allowed_operations=recipe.allowed_operations,
        )

    def get_workstream(self, workstream_id: str) -> Workstream:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM workstreams WHERE workstream_id=?", (workstream_id,)
            ).fetchone()
        if row is None:
            raise SchemaError(
                "workstream_not_found", "Workstream was not found", "Check the workstream id."
            )
        return self._workstream(row)

    def list_workstreams(self) -> tuple[Workstream, ...]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM workstreams ORDER BY admitted_at, workstream_id"
            ).fetchall()
        return tuple(self._workstream(row) for row in rows)

    def attach_run(
        self, workstream_id: str, run_id: str, *, cost_credits: int = 0
    ) -> WorkstreamRun:
        _required_text(run_id, "run_id", "WorkstreamRun")
        if isinstance(cost_credits, bool) or not isinstance(cost_credits, int) or cost_credits < 0:
            raise SchemaError(
                "invalid_number", "Run cost cannot be negative", "Use zero or more credits."
            )
        with self.connect() as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM workstreams WHERE workstream_id=?", (workstream_id,)
            ).fetchone()
            if row is None:
                raise SchemaError(
                    "workstream_not_found", "Workstream was not found", "Check the id."
                )
            existing = connection.execute(
                "SELECT * FROM workstream_runs WHERE workstream_id=? AND run_id=?",
                (workstream_id, run_id),
            ).fetchone()
            if existing is not None:
                if int(existing["cost_credits"]) != cost_credits:
                    raise SchemaError(
                        "run_attachment_conflict",
                        "Attached run cost cannot be changed",
                        "Use the original cost.",
                    )
                return self._run(existing)
            workstream = self._workstream(row)
            aggregate = connection.execute(
                "SELECT COUNT(*) AS count, COALESCE(SUM(cost_credits), 0) AS cost "
                "FROM workstream_runs WHERE workstream_id=?",
                (workstream_id,),
            ).fetchone()
            count = int(aggregate["count"])
            cost = int(aggregate["cost"])
            if count >= workstream.max_runs:
                raise SchemaError(
                    "workstream_run_limit", "Workstream run limit reached", "Admit a new workstream."
                )
            if workstream.max_cost_credits and cost + cost_credits > workstream.max_cost_credits:
                raise SchemaError(
                    "workstream_budget_exceeded",
                    "Workstream cost budget would be exceeded",
                    "Reduce cost or admit a new governed workstream.",
                )
            attached_at = self._now()
            connection.execute(
                "INSERT INTO workstream_runs VALUES (?, ?, ?, ?, ?)",
                (workstream_id, run_id, count + 1, cost_credits, attached_at),
            )
            attached = connection.execute(
                "SELECT * FROM workstream_runs WHERE workstream_id=? AND run_id=?",
                (workstream_id, run_id),
            ).fetchone()
            assert attached is not None
            return self._run(attached)

    @staticmethod
    def _run(row: sqlite3.Row) -> WorkstreamRun:
        return WorkstreamRun(
            run_id=str(row["run_id"]),
            ordinal=int(row["ordinal"]),
            cost_credits=int(row["cost_credits"]),
            attached_at=str(row["attached_at"]),
        )

    def child_runs(self, workstream_id: str) -> tuple[WorkstreamRun, ...]:
        self.get_workstream(workstream_id)
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM workstream_runs WHERE workstream_id=? ORDER BY ordinal",
                (workstream_id,),
            ).fetchall()
        return tuple(self._run(row) for row in rows)

    def authorize_operation(self, workstream_id: str, operation: str) -> bool:
        workstream = self.get_workstream(workstream_id)
        if operation not in workstream.allowed_operations:
            raise SchemaError(
                "workstream_operation_denied",
                "Connector operation is outside the frozen recipe allowlist",
                "Use an allowed read operation or admit a new recipe version.",
            )
        return True

    def project(
        self,
        workstream_id: str,
        *,
        run_statuses: Mapping[str, str],
        evidence_by_run: Mapping[str, frozenset[str]],
    ) -> WorkstreamProjection:
        workstream = self.get_workstream(workstream_id)
        runs = self.child_runs(workstream_id)
        statuses = [run_statuses.get(run.run_id, "") for run in runs]
        supplied = {
            item
            for run in runs
            for item in evidence_by_run.get(run.run_id, frozenset())
        }
        missing = tuple(item for item in workstream.required_evidence if item not in supplied)
        succeeded = sum(status == "succeeded" for status in statuses)
        if not runs:
            state = "admitted"
        elif any(status in {"failed", "cancelled"} for status in statuses):
            state = "blocked"
        elif all(status == "succeeded" for status in statuses) and not missing:
            state = "completed"
        else:
            state = "running"
        return WorkstreamProjection(workstream_id, state, len(runs), succeeded, missing)
