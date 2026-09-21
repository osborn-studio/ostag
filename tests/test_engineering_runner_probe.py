"""Synthetic diagnostics only: passing these tests never authorizes a writer.

The parent owns the entire fixture; "outside" means outside its child cwd, not
outside an existing host sandbox. Hosts denying this fixture must report a
failed/unsupported diagnostic, never skip it and claim T02 evidence.
"""

import json
from pathlib import Path
import subprocess
import sys

import pytest

from scripts.engineering.runner_probe import assess_observations, run_negative_control

EXPECTED = {
    "cwd": "allowed",
    "work_read": "allowed",
    "work_write": "allowed",
    "outside_write": "denied",
    "symlink_write": "denied",
    "private_read": "denied",
    "protected_write": "denied",
    "child_write": "denied",
}


def test_matching_observations_do_not_authorize_an_automatic_writer():
    result = assess_observations(dict(EXPECTED))
    assert result["observations_match"] is True
    assert result["automatic_writes_allowed"] is False
    assert result["t02_complete"] is False
    assert result["network"] == "not_tested"


@pytest.mark.parametrize("observations", [None, {}, [], "approved", {"exit_code": 0}])
def test_missing_or_noop_evidence_is_rejected(observations):
    with pytest.raises(ValueError):
        assess_observations(observations)


@pytest.mark.parametrize("outcome", [None, True, 1, "passed", "", [], {}])
def test_observation_values_are_strict(outcome):
    observations = dict(EXPECTED)
    observations["private_read"] = outcome
    with pytest.raises(ValueError):
        assess_observations(observations)


def test_unknown_probe_is_not_silently_ignored():
    with pytest.raises(ValueError):
        assess_observations({**EXPECTED, "model_approved": "allowed"})


@pytest.mark.parametrize("probe", list(EXPECTED))
def test_every_required_probe_affects_the_diagnostic(probe):
    observations = dict(EXPECTED)
    observations[probe] = "error"
    result = assess_observations(observations)
    assert result["observations_match"] is False
    assert probe in result["mismatches"]


def test_report_is_a_snapshot_not_an_alias_of_mutable_input():
    observations = dict(EXPECTED)
    result = assess_observations(observations)
    observations["private_read"] = "allowed"
    assert result["observations"]["private_read"] == "denied"


def test_real_unrestricted_child_proves_a_work_directory_is_not_a_sandbox():
    result = run_negative_control()
    assert result["dispatch_verified"] is None
    assert result["environment"] == "inherited_host_policy"
    assert result["sandbox_applied_by_probe"] is False
    assert result["observations_match"] is False
    assert result["host_canaries_intact"] is False
    for probe in (
        "outside_write",
        "symlink_write",
        "private_read",
        "protected_write",
        "child_write",
    ):
        assert result["observations"][probe] == "allowed"
    for probe in ("cwd", "work_read", "work_write"):
        assert result["observations"][probe] == "allowed"
    assert result["automatic_writes_allowed"] is False
    assert "synthetic-private-value" not in json.dumps(result)


@pytest.mark.parametrize("surface", ["ostag_codex_backend", "ostag_claude_backend"])
def test_actual_business_backend_dispatch_is_not_os_isolation(surface):
    result = run_negative_control(surface=surface)
    assert result["surface"] == surface
    assert result["dispatch_verified"] is True
    assert result["model_invoked"] is False
    assert result["observations_match"] is False
    assert result["host_canaries_intact"] is False
    assert result["observations"]["outside_write"] == "allowed"
    assert result["observations"]["private_read"] == "allowed"
    assert result["observations"]["child_write"] == "allowed"
    assert result["automatic_writes_allowed"] is False
    assert result["t02_complete"] is False
    assert result["network"] == "not_tested"
    assert result["write_reports_consistent"] is True
    assert set(result["mismatches"]) == {
        "outside_write", "symlink_write", "private_read", "protected_write", "child_write",
    }
    assert "synthetic-private-value" not in json.dumps(result)


@pytest.mark.parametrize("surface", ["", "codex", "osstack", "approved", None, True])
def test_unknown_surface_is_rejected_before_creating_any_fixture(surface, monkeypatch):
    from scripts.engineering import runner_probe

    def unexpected(**kwargs):
        pytest.fail("invalid surface created a fixture")

    monkeypatch.setattr(runner_probe.tempfile, "TemporaryDirectory", unexpected)
    with pytest.raises(ValueError):
        runner_probe.run_negative_control(surface=surface)


@pytest.mark.parametrize(
    "code,outcome", [(0, "allowed"), (13, "denied"), (1, "error"), (-9, "error")]
)
def test_descendant_exit_has_an_explicit_permission_denied_mapping(code, outcome):
    from scripts.engineering.runner_probe import classify_descendant_exit

    assert classify_descendant_exit(code) == outcome


@pytest.mark.parametrize("code", [False, True, None, "13"])
def test_descendant_exit_does_not_coerce_invalid_values(code):
    from scripts.engineering.runner_probe import classify_descendant_exit

    with pytest.raises(ValueError):
        classify_descendant_exit(code)


def test_report_shape_check_is_not_parent_or_read_verification():
    report = assess_observations(dict(EXPECTED))
    assert report["evidence_source"] == "untrusted_report_shape_only"
    assert report["parent_observations"] is None
    assert report["parent_verified_reads"] is False


def test_parent_checks_writes_without_claiming_it_can_observe_reads():
    report = run_negative_control()
    assert report["write_reports_consistent"] is True
    assert report["parent_verified_reads"] is False
    assert report["evidence_source"] == "fixed_child_plus_parent_write_observations"
    assert report["parent_observations"] == {
        "work_output_written": True,
        "outside_direct_written": True,
        "symlink_target_written": True,
        "descendant_written": True,
        "verifier_changed": True,
        "private_canary_changed": False,
    }


@pytest.mark.parametrize("surface", ["direct", "ostag_codex_backend", "ostag_claude_backend"])
def test_negative_control_cleans_only_its_generated_fixture(monkeypatch, tmp_path, surface):
    from scripts.engineering import runner_probe

    real_temporary_directory = runner_probe.tempfile.TemporaryDirectory
    created = []

    def tracked_directory(**kwargs):
        directory = real_temporary_directory(dir=tmp_path, **kwargs)
        created.append(Path(directory.name))
        return directory

    sentinel = tmp_path / "unrelated"
    sentinel.write_text("untouched")
    monkeypatch.setattr(runner_probe.tempfile, "TemporaryDirectory", tracked_directory)
    runner_probe.run_negative_control(surface=surface)
    assert len(created) == 1
    assert not created[0].exists()
    assert sentinel.read_text() == "untouched"
    assert list(tmp_path.iterdir()) == [sentinel]


def test_tests_import_the_repository_probe_not_another_scripts_package():
    from scripts.engineering import runner_probe

    expected = Path(__file__).resolve().parents[1] / "scripts/engineering/runner_probe.py"
    assert Path(runner_probe.__file__).resolve() == expected


def test_parent_detects_a_false_denial_report_after_an_actual_write(monkeypatch):
    from scripts.engineering import runner_probe

    real_run = runner_probe.subprocess.run

    def alter_report(*args, **kwargs):
        process = real_run(*args, **kwargs)
        payload = json.loads(process.stdout)
        payload["observations"]["outside_write"] = "denied"
        process.stdout = json.dumps(payload)
        return process

    monkeypatch.setattr(runner_probe.subprocess, "run", alter_report)
    report = runner_probe.run_negative_control()
    assert report["parent_observations"]["outside_direct_written"] is True
    assert report["write_reports_consistent"] is False
    assert report["automatic_writes_allowed"] is False


@pytest.mark.parametrize("surface", ["direct", "ostag_codex_backend", "ostag_claude_backend"])
def test_cli_negative_control_exits_nonzero_even_when_the_diagnostic_ran(tmp_path, surface):
    script = Path(__file__).resolve().parents[1] / "scripts/engineering/runner_probe.py"
    process = subprocess.run(
        [sys.executable, "-I", "-B", str(script), "--negative-control", "--surface", surface],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert process.returncode == 1
    result = json.loads(process.stdout)
    assert result["automatic_writes_allowed"] is False
    assert result["t02_complete"] is False
    assert result["surface"] == surface
    assert result["dispatch_verified"] is (None if surface == "direct" else True)
    assert result["cli_sandbox_behavior"] == "not_tested"
    assert list(tmp_path.iterdir()) == []


def test_cli_requires_explicit_negative_control_mode(tmp_path):
    script = Path(__file__).resolve().parents[1] / "scripts/engineering/runner_probe.py"
    process = subprocess.run(
        [sys.executable, "-I", "-B", str(script)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert process.returncode == 2
    assert not process.stdout


@pytest.mark.parametrize(
    "error",
    [
        subprocess.TimeoutExpired("synthetic-sensitive-command", 8, "private-output"),
        KeyError("private-output"),
        TypeError("private-output"),
    ],
)
def test_errors_cannot_leak_process_text_or_become_success(monkeypatch, capsys, error):
    from scripts.engineering import runner_probe

    def fail(**kwargs):
        raise error

    monkeypatch.setattr(runner_probe, "run_negative_control", fail)
    monkeypatch.setattr(sys, "argv", ["runner_probe", "--negative-control"])
    assert runner_probe.main() == 2
    output = capsys.readouterr().out
    assert json.loads(output) == {"error": type(error).__name__, "automatic_writes_allowed": False}
    assert "private-output" not in output
    assert "synthetic-sensitive-command" not in output


@pytest.mark.parametrize("surface", ["ostag_codex_backend", "ostag_claude_backend"])
@pytest.mark.parametrize("receipt", [None, {}])
def test_backend_dispatch_receipt_cannot_be_missing_or_forged(monkeypatch, surface, receipt):
    from scripts.engineering import runner_probe

    real_run = runner_probe.subprocess.run

    def forge_receipt(*args, **kwargs):
        process = real_run(*args, **kwargs)
        path = Path(kwargs["cwd"]).parent / "invocation.json"
        if receipt is None:
            path.unlink()
        else:
            path.write_text(json.dumps(receipt))
        return process

    monkeypatch.setattr(runner_probe.subprocess, "run", forge_receipt)
    with pytest.raises((OSError, ValueError)):
        runner_probe.run_negative_control(surface=surface)


@pytest.mark.parametrize("surface", ["direct", "ostag_codex_backend", "ostag_claude_backend"])
def test_empty_child_output_is_not_evidence(monkeypatch, surface):
    from scripts.engineering import runner_probe

    monkeypatch.setattr(runner_probe, "_CHILD", "pass")
    with pytest.raises(ValueError):
        runner_probe.run_negative_control(surface=surface)


def test_cleanup_failure_cannot_emit_a_completed_diagnostic(monkeypatch, tmp_path, capsys):
    from scripts.engineering import runner_probe

    real_directory = runner_probe.tempfile.TemporaryDirectory

    class FailedCleanup:
        def __init__(self, **kwargs):
            self.inner = real_directory(dir=tmp_path, **kwargs)

        def __enter__(self):
            return self.inner.__enter__()

        def __exit__(self, *args):
            self.inner.__exit__(*args)
            raise OSError("private cleanup details")

    monkeypatch.setattr(runner_probe.tempfile, "TemporaryDirectory", FailedCleanup)
    monkeypatch.setattr(sys, "argv", ["runner_probe", "--negative-control"])
    assert runner_probe.main() == 2
    assert json.loads(capsys.readouterr().out) == {
        "error": "OSError", "automatic_writes_allowed": False,
    }
    assert list(tmp_path.iterdir()) == []


def test_osstack_missing_or_changed_installation_never_dispatches(monkeypatch, tmp_path):
    from scripts.engineering import runner_probe

    monkeypatch.setattr(runner_probe, "OSSTACK_ROOT", tmp_path)

    def unexpected(**kwargs):
        pytest.fail("unsupported osstack installation created a fixture")

    monkeypatch.setattr(runner_probe.tempfile, "TemporaryDirectory", unexpected)
    with pytest.raises((OSError, ValueError)):
        runner_probe.run_negative_control(surface="osstack_codex_adapter")


def test_osstack_installation_rejects_hash_drift(monkeypatch, tmp_path):
    from scripts.engineering import runner_probe

    monkeypatch.setattr(runner_probe, "OSSTACK_ROOT", tmp_path)
    for name in runner_probe.OSSTACK_PINS:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("changed installed source")
    with pytest.raises(ValueError, match="unsupported osstack"):
        runner_probe.verify_osstack_installation()
