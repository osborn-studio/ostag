from __future__ import annotations

import json
from pathlib import Path

import pytest

from ostag.core.schemas import SchemaError
from ostag.core.workstreams import (
    RecipeDefinition,
    RecipeStep,
    WorkstreamAdmission,
    WorkstreamStore,
)
from ostag.main import main_async


def _recipe(**overrides: object) -> RecipeDefinition:
    values: dict[str, object] = {
        "recipe_id": "software-delivery",
        "version": 1,
        "title": "Software delivery",
        "steps": (
            RecipeStep("build", "Build"),
            RecipeStep("verify", "Verify", depends_on=("build",)),
        ),
        "max_runs": 3,
        "max_cost_credits": 100,
        "required_evidence": ("tests", "provider_merge"),
        "allowed_operations": ("github.read_pr", "github.read_checks"),
    }
    values.update(overrides)
    return RecipeDefinition(**values)  # type: ignore[arg-type]


def _store(tmp_path: Path, *, max_batch: int = 25) -> WorkstreamStore:
    return WorkstreamStore(tmp_path / "workstreams.sqlite3", max_batch=max_batch)


def test_recipe_schema_is_strict_and_rejects_cycles() -> None:
    payload = _recipe().to_dict()
    payload["unexpected"] = True
    with pytest.raises(SchemaError) as unknown:
        RecipeDefinition.from_dict(payload)
    assert unknown.value.code == "unknown_field"

    with pytest.raises(SchemaError) as cycle:
        _recipe(
            steps=(
                RecipeStep("build", "Build", depends_on=("verify",)),
                RecipeStep("verify", "Verify", depends_on=("build",)),
            )
        )
    assert cycle.value.code == "recipe_cycle"


def test_recipe_digest_is_canonical_and_content_addressed() -> None:
    first = _recipe()
    identical = RecipeDefinition.from_dict(first.to_dict())
    changed = _recipe(required_evidence=("tests",))

    assert first.digest == identical.digest
    assert first.digest != changed.digest
    assert len(first.digest) == 64


def test_recipe_registration_is_idempotent_but_version_is_immutable(tmp_path: Path) -> None:
    store = _store(tmp_path)
    recipe = _recipe()

    assert store.register_recipe(recipe).digest == recipe.digest
    assert store.register_recipe(recipe).digest == recipe.digest
    with pytest.raises(SchemaError) as conflict:
        store.register_recipe(_recipe(title="Changed in place"))
    assert conflict.value.code == "recipe_version_conflict"


def test_admission_is_idempotent_and_freezes_recipe_governance(tmp_path: Path) -> None:
    store = _store(tmp_path)
    recipe = store.register_recipe(_recipe())
    request = WorkstreamAdmission(
        admission_key="feishu:oc_1:goal_1",
        recipe_id=recipe.recipe_id,
        recipe_version=recipe.version,
        goal="Ship governed change",
    )

    first = store.admit(request)
    second = store.admit(request)

    assert first.workstream_id == second.workstream_id
    assert first.recipe_digest == recipe.digest
    assert first.max_runs == 3
    assert first.required_evidence == ("tests", "provider_merge")


def test_batch_admission_is_bounded_before_any_write(tmp_path: Path) -> None:
    store = _store(tmp_path, max_batch=2)
    store.register_recipe(_recipe())
    requests = tuple(
        WorkstreamAdmission(f"key-{index}", "software-delivery", 1, f"Goal {index}")
        for index in range(3)
    )

    with pytest.raises(SchemaError) as excinfo:
        store.admit_batch(requests)
    assert excinfo.value.code == "batch_limit_exceeded"
    assert store.list_workstreams() == ()


def test_child_runs_are_idempotent_and_enforce_count_and_cost_budget(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.register_recipe(_recipe(max_runs=2, max_cost_credits=10))
    workstream = store.admit(WorkstreamAdmission("key", "software-delivery", 1, "Goal"))

    store.attach_run(workstream.workstream_id, "run_1", cost_credits=4)
    store.attach_run(workstream.workstream_id, "run_1", cost_credits=4)
    store.attach_run(workstream.workstream_id, "run_2", cost_credits=6)
    assert len(store.child_runs(workstream.workstream_id)) == 2

    with pytest.raises(SchemaError) as count_error:
        store.attach_run(workstream.workstream_id, "run_3", cost_credits=0)
    assert count_error.value.code == "workstream_run_limit"

    other = store.admit(WorkstreamAdmission("other", "software-delivery", 1, "Other"))
    with pytest.raises(SchemaError) as budget_error:
        store.attach_run(other.workstream_id, "run_expensive", cost_credits=11)
    assert budget_error.value.code == "workstream_budget_exceeded"


def test_projection_derives_completion_only_from_child_runs_and_evidence(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.register_recipe(_recipe())
    workstream = store.admit(WorkstreamAdmission("key", "software-delivery", 1, "Goal"))
    store.attach_run(workstream.workstream_id, "run_1")
    store.attach_run(workstream.workstream_id, "run_2")

    running = store.project(
        workstream.workstream_id,
        run_statuses={"run_1": "succeeded", "run_2": "running"},
        evidence_by_run={"run_1": frozenset({"tests", "provider_merge"})},
    )
    complete = store.project(
        workstream.workstream_id,
        run_statuses={"run_1": "succeeded", "run_2": "succeeded"},
        evidence_by_run={
            "run_1": frozenset({"tests"}),
            "run_2": frozenset({"provider_merge"}),
        },
    )
    blocked = store.project(
        workstream.workstream_id,
        run_statuses={"run_1": "failed", "run_2": "succeeded"},
        evidence_by_run={},
    )

    assert running.state == "running"
    assert complete.state == "completed"
    assert complete.missing_evidence == ()
    assert blocked.state == "blocked"


def test_store_recovers_workstream_and_children_after_restart(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.register_recipe(_recipe())
    admitted = store.admit(WorkstreamAdmission("key", "software-delivery", 1, "Goal"))
    store.attach_run(admitted.workstream_id, "run_1", cost_credits=3)

    reopened = _store(tmp_path)
    recovered = reopened.get_workstream(admitted.workstream_id)

    assert recovered.recipe_digest == admitted.recipe_digest
    assert reopened.child_runs(admitted.workstream_id)[0].run_id == "run_1"


def test_frozen_snapshot_corruption_and_undeclared_operations_fail_closed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.register_recipe(_recipe())
    admitted = store.admit(WorkstreamAdmission("key", "software-delivery", 1, "Goal"))

    assert store.authorize_operation(admitted.workstream_id, "github.read_pr") is True
    with pytest.raises(SchemaError) as denied:
        store.authorize_operation(admitted.workstream_id, "github.merge_pr")
    assert denied.value.code == "workstream_operation_denied"

    with store.connect() as connection, connection:
        connection.execute(
            "UPDATE workstreams SET recipe_snapshot_json=? WHERE workstream_id=?",
            (json.dumps({**_recipe().to_dict(), "title": "tampered"}), admitted.workstream_id),
        )
    with pytest.raises(SchemaError) as corrupt:
        store.get_workstream(admitted.workstream_id)
    assert corrupt.value.code == "workstream_snapshot_corrupt"


async def test_workstream_cli_registers_admits_and_shows_frozen_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    recipe_path = tmp_path / "recipe.json"
    recipe_path.write_text(json.dumps(_recipe().to_dict()), encoding="utf-8")
    monkeypatch.setenv("OSTAG_WORKSTREAMS_ENABLED", "true")
    monkeypatch.setenv("OSTAG_WORKSTREAM_STORE_PATH", str(tmp_path / "workstreams.sqlite3"))

    assert await main_async(["workstream", "recipe-register", "--file", str(recipe_path), "--json"]) == 0
    capsys.readouterr()
    assert await main_async(
        [
            "workstream",
            "admit",
            "--recipe",
            "software-delivery",
            "--version",
            "1",
            "--key",
            "cli-key",
            "--goal",
            "Ship",
            "--json",
        ]
    ) == 0
    admitted = json.loads(capsys.readouterr().out)
    assert admitted["recipeDigest"] == _recipe().digest

    assert await main_async(
        ["workstream", "show", "--id", admitted["workstreamId"], "--json"]
    ) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["workstream"]["goal"] == "Ship"
    assert shown["childRuns"] == []
