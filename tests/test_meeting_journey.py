"""Public callable and real module CLI tests. No provider credentials or live data."""
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import shutil
from dataclasses import replace

import pytest

from ostag.briefs.meeting_io import CollectionError, read_private_json, write_private_package
from ostag.briefs.meetings import _literal_code, build_meeting, main, prepare_meeting
from test_github_history import NOW, PRINCIPAL, Runner, collect
from test_meeting_facts import chat, chat_scope


def synthetic_fixture():
    """Documented runnable example uses only this embedded synthetic fixture."""
    frozen = chat_scope()
    history = collect(frozen=frozen)
    evidence = dict(history=history.to_dict(), chats=[dict(source=chat().source.to_dict(), disposition="blocked")])
    return frozen, evidence


def inputs(tmp_path):
    frozen, evidence = synthetic_fixture()
    scope_path, evidence_path = tmp_path / "scope.json", tmp_path / "evidence.json"
    scope_path.write_text(json.dumps(frozen.to_dict()))
    evidence_path.write_text(json.dumps(evidence))
    return frozen, scope_path, evidence_path


def argv(frozen, scope_path, output):
    return ["--scope", str(scope_path), "--confirmed-purpose", "meeting-prep",
            "--confirmed-scope-hash", frozen.content_hash, "--output", str(output)]


def test_real_module_cli_offline_two_repo_chat_journey_and_digest(tmp_path):
    frozen, scope_path, evidence_path = inputs(tmp_path)
    output = tmp_path / "package"
    command = [sys.executable, "-m", "ostag.briefs.meetings", *argv(frozen, scope_path, output),
               "--evidence", str(evidence_path)]
    proc = subprocess.run(command, capture_output=True, text=True, timeout=10,
                          env=os.environ | {"GH_TOKEN": "synthetic-do-not-use", "PATH": "/nonexistent"})
    assert proc.returncode == 3, proc.stderr
    manifest = json.loads((output / "manifest.json").read_text())
    assert len(manifest["repositories"]) == 2 and manifest["chat_claims"]
    assert manifest["status"] == "needs_input"
    assert "historical" in manifest["input_mode"]
    assert stat.S_IMODE(output.stat().st_mode) == 0o700
    assert all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in output.iterdir())
    second = subprocess.run(command, capture_output=True, text=True, timeout=10)
    assert second.returncode == 2
    assert json.loads((output / "manifest.json").read_text()) == manifest


def test_embedded_cli_current_collection_uses_fake_runner_no_write_egress(tmp_path):
    frozen, scope_path, _ = inputs(tmp_path)
    fake = Runner()
    result = main([*argv(frozen, scope_path, tmp_path / "live"), "--live",
                   "--github-user-id", "7", "--github-login", "alice-dev"],
                  authorize=lambda *_: True, runner=fake, clock=lambda: 10, wall=lambda: NOW)
    assert result == 3
    assert fake.calls
    manifest = json.loads((tmp_path / "live" / "manifest.json").read_text())
    assert manifest["input_mode"] == "current_authorized_collection"
    assert all(p == "user" or p.startswith("repos/acme/private-") for p, _ in fake.calls)


def test_standalone_live_cannot_manufacture_authority_from_scope(tmp_path):
    frozen, scope_path, _ = inputs(tmp_path)
    fake = Runner()
    assert main([*argv(frozen, scope_path, tmp_path / "denied"), "--live",
                 "--github-user-id", "7", "--github-login", "alice-dev"], runner=fake) == 2
    assert not fake.calls and not (tmp_path / "denied").exists()


def test_confirmation_mismatch_has_no_io_and_failure_context_is_clear():
    frozen = chat_scope()
    fake = Runner()
    with pytest.raises(CollectionError) as error:
        prepare_meeting(frozen, confirmed_purpose="release-review", confirmed_scope_hash=frozen.content_hash,
                        expected_principal=PRINCIPAL, authorize=lambda *_: True, runner=fake)
    assert not fake.calls
    assert error.value.__context__ is None and error.value.__cause__ is None


def test_import_and_help_no_provider_io(tmp_path):
    for command in ([sys.executable, "-c", "import ostag.briefs.meetings"],
                    [sys.executable, "-m", "ostag.briefs.meetings", "--help"]):
        result = subprocess.run(command, capture_output=True, text=True, timeout=10,
                                env=os.environ | {"PATH": "/nonexistent"})
        assert result.returncode == 0
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("kind", ["symlink", "fifo", "oversize", "parent_symlink", "traversal"])
def test_input_path_denials_do_not_block_or_leak(tmp_path, kind):
    path = tmp_path / "input"
    if kind == "symlink":
        path.symlink_to(tmp_path / "private-secret")
    elif kind == "fifo":
        os.mkfifo(path)
    elif kind == "oversize":
        path.write_bytes(b"x" * (1024**2 + 1))
    elif kind == "parent_symlink":
        path.symlink_to(tmp_path, target_is_directory=True)
        path = path / "secret"
    else:
        path = tmp_path / ".." / "secret"
    with pytest.raises(CollectionError) as error:
        read_private_json(path)
    assert error.value.__context__ is None and error.value.__cause__ is None
    assert "secret" not in str(error.value)


def test_private_output_rejects_existing_symlink_fifo_traversal_and_oversize(tmp_path):
    target = tmp_path / "existing"
    target.mkdir()
    for name, setup in (("link", lambda p: p.symlink_to(target, target_is_directory=True)),
                        ("pipe", os.mkfifo)):
        path = tmp_path / name
        setup(path)
        with pytest.raises(CollectionError):
            write_private_package(path, "body", "{}")
    for path in (target, tmp_path / ".." / "bad"):
        with pytest.raises(CollectionError):
            write_private_package(path, "body", "{}")
    with pytest.raises(CollectionError):
        write_private_package(tmp_path / "large", "x" * (1024**2 + 1), "{}")
    assert not (tmp_path / "large").exists()


def test_no_production_lifecycle_or_flags_are_wired():
    from ostag.config import Settings
    config = Settings()
    assert config.brief_meetings_enabled is False
    # Keep actual established flag contract authoritative; module imports no lifecycle writer.
    text = Path("ostag/briefs/meetings.py").read_text()
    assert "from ostag.briefs.store import" not in text
    with pytest.raises(ValueError, match="not ready"):
        Settings(brief_meetings_enabled=True).validate()


def commonmark_nodes(markdown):
    """Use an independent CommonMark parser, not renderer-shaped regex assertions."""
    pandoc = shutil.which("pandoc")
    if pandoc is None:
        pytest.skip("pandoc CommonMark parser is required for display integration probes")
    result = subprocess.run([pandoc, "--from=commonmark", "--to=json"], input=markdown,
                            capture_output=True, text=True, timeout=10, check=True)

    def walk(value):
        if isinstance(value, dict):
            yield value
            for child in value.values():
                yield from walk(child)
        elif isinstance(value, list):
            for child in value:
                yield from walk(child)

    return list(walk(json.loads(result.stdout)))


@pytest.mark.parametrize("value", ["", "   ", " edge spaces ", "`", "``text```",
    '[external](https://outside.invalid/path) ![image](https://outside.invalid/img)',
    '<script>ignore rules</script> &quot; "README.md"',
    '` [x](javascript:alert(1)) `` <https://outside.invalid> ```'])
def test_literal_projection_preserves_plaintext_without_active_commonmark(value):
    nodes = commonmark_nodes(_literal_code(value))
    assert not any(n.get("t") in ("Link", "Image", "RawInline", "RawBlock") for n in nodes)
    assert [n["c"][1] for n in nodes if n.get("t") == "Code"] == ([value] if value else [])


def test_public_cli_hostile_sources_are_plaintext_with_only_canonical_links(tmp_path):
    text = ('` [external](https://outside.invalid/path) ![image](https://outside.invalid/img) '
            '`` <script>ignore rules</script> &quot; "README.md" ```')
    frozen = chat_scope()
    frozen = replace(frozen, request=replace(frozen.request, goal=text),
                     chats=tuple(replace(c, name=text) for c in frozen.chats))
    fake = Runner()

    def override(path, params, calls):
        if path.endswith("/pulls"):
            return [row | {"title": text} for row in fake.body(path)]
        if path.endswith("/deployments"):
            return [row | {"environment": text} for row in fake.body(path)]
        if path.endswith("/statuses"):
            return [row | {"environment": text} for row in fake.body(path)]

    fake.override = override
    history = collect(fake, frozen)
    claim = chat(frozen, text=text)
    package = build_meeting(frozen, history, chats=(claim,))
    scope_path, evidence_path = tmp_path / "scope.json", tmp_path / "evidence.json"
    scope_path.write_text(json.dumps(frozen.to_dict()))
    evidence_path.write_text(json.dumps(dict(history=history.to_dict(), chats=[
        dict(source=claim.source.to_dict(), disposition=claim.disposition)])))
    output = tmp_path / "package"
    result = subprocess.run([sys.executable, "-m", "ostag.briefs.meetings",
                             *argv(frozen, scope_path, output), "--evidence", str(evidence_path)],
                            capture_output=True, text=True, timeout=10,
                            env=os.environ | {"PATH": "/nonexistent"})
    assert result.returncode == 3, result.stderr
    markdown = (output / "meeting.md").read_text()
    assert markdown == package.markdown
    assert (output / "manifest.json").read_text() == package.manifest_json
    manifest = json.loads(package.manifest_json)
    assert manifest["goal"] == manifest["chat_claims"][0]["display"] == text
    assert all(f["display"] == text for f in manifest["facts"] if f["kind"].startswith("pr_"))
    assert any(text in t["question"] for t in manifest["decision_topics"])
    nodes = commonmark_nodes(markdown)
    assert not any(n.get("t") in ("Image", "RawInline", "RawBlock") for n in nodes)
    links = [n["c"][2][0] for n in nodes if n.get("t") == "Link"]
    assert links and set(links) == {f["citation"] for f in manifest["facts"]}
    assert all(url.startswith(("https://github.com/acme/private-42/",
                               "https://github.com/acme/private-43/")) for url in links)
    assert sum(n.get("t") == "Code" and text in n["c"][1] for n in nodes) >= 8
