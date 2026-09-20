"""Scripted provider fixtures only. Never invoke installed authenticated provider CLIs."""
import asyncio
import copy
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ostag.briefs.meeting_collect import collect, inert, markdown
from ostag.briefs.meeting_io import CollectionError
from ostag.main import build_parser, main_async

PYTHON = sys.executable
REPO = "osborn-studio/ostag"
START, END = "2026-09-13T07:00:00Z", "2026-09-20T07:00:00Z"
NOW = datetime(2026, 9, 21, tzinfo=UTC)
AT, OLD, NEW = "2026-09-15T10:00:00Z", "2026-08-01T00:00:00Z", "2026-09-21T00:00:00Z"
NORMAL = '中文 https://example.org/a旁边 English v1.2.3 3.14 file_name.py "quotes" #12\n第二行'
SHA = "a" * 40


def pr(**changes):
    return dict(id=100, number=12, title="完成增量 v1.2.3", body=NORMAL, created_at=OLD,
                merged_at=AT, updated_at=NEW, base={"repo": {"id": 8, "full_name": REPO}},
                **changes)


def issue():
    return dict(id=101, number=13, title="需要确认", body="issue body", created_at=AT,
                updated_at=AT, closed_at=None, repository_url=f"https://api.github.com/repos/{REPO}")


def message(chat="oc_one", **changes):
    at = str(int(datetime.fromisoformat(AT.replace("Z", "+00:00")).timestamp() * 1000))
    value = dict(message_id="om_one", chat_id=chat, message_type="text", create_time=at,
                 update_time=at, deleted=False, sender={"id": "ou_other", "id_type": "open_id"},
                 body={"content": json.dumps({"text": NORMAL + f"\nhttps://github.com/{REPO}/pull/12"},
                                             ensure_ascii=False)})
    value.update(changes)
    return value


def envelope(data):
    return {"ok": True, "identity": "user", "data": data}


class Scripted:
    def __init__(self):
        self.calls = []
        self.overrides = {}
        self.responses = {
            "user": {"id": 7, "login": "operator"},
            f"repos/{REPO}": {"id": 8, "full_name": REPO, "default_branch": "feature/main",
                               "permissions": {"pull": True}},
            f"repos/{REPO}/commits/feature%2Fmain": {"sha": SHA},
            f"repos/{REPO}/pulls": [pr()],
            f"repos/{REPO}/issues": [issue()],
            f"repos/{REPO}/issues/comments": [dict(id=102, body=NORMAL, created_at=AT,
                                                  updated_at=NEW,
                                                  issue_url=f"https://api.github.com/repos/{REPO}/issues/13")],
            f"repos/{REPO}/commits": [{"sha": SHA, "commit": {
                "message": "remote commit", "author": {"date": OLD}, "committer": {"date": AT}}}],
            # Real release list schema has no updated_at.
            f"repos/{REPO}/releases": [dict(id=103, name="v1.2", body="release body",
                                           tag_name="v1.2", draft=False, published_at=AT,
                                           created_at=OLD)],
            "/open-apis/authen/v1/user_info": envelope({"open_id": "ou_operator"}),
            "/open-apis/im/v1/messages": envelope({"items": [message()], "has_more": False}),
        }

    def get(self, provider, endpoint, params, deadline):
        self.calls.append((provider, endpoint, dict(params), deadline))
        override = self.overrides.get(endpoint)
        if override:
            return override(params, sum(c[1] == endpoint for c in self.calls))
        return copy.deepcopy(self.responses[endpoint])


def run(fake=None, chats=("oc_one",), **kwargs):
    return collect(REPO, START, END, "America/Los_Angeles", "项目周会", chats,
                   transport=fake or Scripted(), wall=lambda: NOW, **kwargs)


def reasons(result):
    return {g["reason"] for g in result["gaps"]}


def test_complete_two_sources_real_contracts_and_references():
    fake = Scripted()
    result = run(fake)
    assert result["status"] == "draft_ready"
    assert result["synthetic"] is False and result["meeting_ready"] is False
    assert result["semantic_review_required"] is True
    assert result["identities"]["github_observed"] == {"id": 7, "login": "operator"}
    assert result["identities"]["feishu_observed"] == {"open_id": "ou_operator"}
    assert all(c["exhausted"] for c in result["coverage"].values())
    sources = {s["source_id"]: s for s in result["sources"]}
    for item in result["facts"] + result["agenda"]:
        assert item["source_ids"] and set(item["source_ids"]) <= sources.keys()
    assert len(result["agenda"]) <= 3
    for source in sources.values():
        assert source["content_sha256"] == hashlib.sha256(source["content"].encode()).hexdigest()
    chat = next(s for s in sources.values() if s["provider"] == "feishu")
    assert chat["associated_source_ids"] == ["github:pulls:100"]
    assert result["questions"][0]["proposed"]
    assert result["github_tip_sha"] == SHA
    commit_call = next(c for c in fake.calls if c[1] == f"repos/{REPO}/commits")
    assert commit_call[2]["sha"] == SHA
    assert all(c[2].get("per_page") == 50 for c in fake.calls if "page" in c[2])
    assert all(c[2].get("since") is None for c in fake.calls if c[1].endswith("pulls"))
    msg = next(c for c in fake.calls if c[1].endswith("messages"))
    assert msg[2]["sort_type"] == "ByCreateTimeAsc" and msg[2]["container_id"] == "oc_one"


def test_window_merge_edge_edited_comment_and_release_semantics():
    result = run()
    facts = result["facts"]
    merged = next(f for f in facts if f["kind"] == "pr_merged")
    assert merged["event_at"] == AT and merged["provider_updated_at"] == NEW
    assert merged["check_state"] == merged["deployment_state"] == "unknown"
    assert "observed_content_updated_at_or_after_window_end" in merged["caveats"]
    assert not any(f["kind"] == "pr_created" for f in facts)
    comment = next(f for f in facts if f["kind"] == "comment_created")
    assert comment["event_at"] == AT and comment["provider_updated_at"] == NEW
    assert any(f["kind"] == "commit_timestamp" for f in facts)  # Old author date ignored.
    release = next(f for f in facts if f["kind"] == "release_published")
    assert "release_not_deployment" in release["caveats"]
    assert release["provider_updated_at"] is None
    assert not any("deployment_success" in f["kind"] for f in facts)


@pytest.mark.parametrize("start", [START, "2026-09-13T07:00:00.500001Z"])
@pytest.mark.parametrize("part,endpoint", [
    ("issues", "issues"), ("issue_comments", "issues/comments"), ("commits", "commits"),
])
def test_strict_after_provider_since_preserves_inclusive_start(start, part, endpoint):
    fake = Scripted()
    path = f"repos/{REPO}/{endpoint}"
    lower = datetime.fromisoformat(start)
    end = "2026-09-20T07:00:00.500001Z"
    upper = datetime.fromisoformat(end)
    times = [lower - timedelta(microseconds=1), lower, lower + timedelta(microseconds=1),
             upper - timedelta(microseconds=1), upper]
    records = []
    for index, at in enumerate(times):
        record = copy.deepcopy(fake.responses[path][0])
        timestamp = at.isoformat().replace("+00:00", "Z")
        if part == "commits":
            record["sha"] = f"{index + 1:040x}"
            record["commit"]["committer"]["date"] = timestamp
        else:
            record.update(id=200 + index, created_at=timestamp, updated_at=timestamp)
        records.append(record)

    def provider_filter(params, count):
        # Apply the provider's strict-after lower bound before local normalization.
        since = datetime.fromisoformat(params["since"])
        until = datetime.fromisoformat(params["until"]) if "until" in params else None
        selected = []
        for record in records:
            at = datetime.fromisoformat(record["commit"]["committer"]["date"]
                                        if part == "commits" else record["updated_at"])
            if at > since and (until is None or at <= until):
                selected.append(copy.deepcopy(record))
        offset = (params["page"] - 1) * params["per_page"]
        return selected[offset:offset + params["per_page"]]

    fake.overrides[path] = provider_filter
    result = collect(REPO, start, end, "America/Los_Angeles", "项目周会", ("oc_one",),
                     transport=fake, wall=lambda: NOW)
    sources = [s for s in result["sources"] if s["partition"] == f"github:{part}"]
    expected_ids = ({f"{i + 1:040x}" for i in (1, 2, 3)} if part == "commits"
                    else {str(200 + i) for i in (1, 2, 3)})
    assert {s["resource_id"] for s in sources} == expected_ids
    source_ids = {s["source_id"] for s in sources}
    facts = [f for f in result["facts"] if set(f["source_ids"]) & source_ids]
    assert {datetime.fromisoformat(f["event_at"]) for f in facts} == set(times[1:4])
    cov = result["coverage"][f"github:{part}"]
    assert cov["records_received"] == 5
    assert cov["records_included"] == 3 and cov["records_excluded"] == 2
    assert cov["reasons"] == {"outside_window": 2} and cov["exhausted"]
    assert result["status"] == "draft_ready"
    query = next(c[2] for c in fake.calls if c[1] == path)
    assert query["since"] == "2026-09-13T06:59:59Z"
    assert result["window"]["start"] == start


def test_no_chat_is_useful_partial_without_lark_io():
    fake = Scripted()
    result = run(fake, chats=())
    assert result["status"] == "needs_input" and result["facts"]
    assert reasons(result) == {"feishu_scope_missing"}
    assert all(c[0] == "github" for c in fake.calls)


@pytest.mark.parametrize("category", ["keychain_unavailable", "provider_failed"])
def test_feishu_auth_failure_keeps_github(category):
    fake = Scripted()
    def fail(*_):
        raise CollectionError(category)
    fake.overrides["/open-apis/authen/v1/user_info"] = fail
    result = run(fake)
    assert result["status"] == "needs_input" and category in reasons(result)
    assert {f["provider"] for f in result["facts"]} == {"github"}


@pytest.mark.parametrize("target", ["user", f"repos/{REPO}", "/open-apis/authen/v1/user_info"])
@pytest.mark.parametrize("failure", [False, True])
def test_final_identity_or_repo_change_withholds_only_that_provider(target, failure):
    fake = Scripted()
    def mutate(params, count):
        value = copy.deepcopy(fake.responses[target])
        if count == 2:
            if failure:
                raise CollectionError("provider_failed")
            if target == "user":
                value["id"] = 19
            elif target.startswith("repos"):
                value["id"] = 20
            else:
                value["data"]["open_id"] = "ou_changed"
        return value
    fake.overrides[target] = mutate
    result = run(fake)
    withheld = "feishu" if target.startswith("/") else "github"
    assert all(s["provider"] != withheld for s in result["sources"])
    assert all(f["provider"] != withheld for f in result["facts"])
    assert result["facts"] and result["status"] == "needs_input"
    if withheld == "github":
        assert "github_tip_sha" not in result and "github_repository" not in result
    assert all(c["records_included"] == 0 and c["withheld"]
               for p, c in result["coverage"].items() if p.startswith(withheld + ":"))


@pytest.mark.parametrize("payload", [{"code": 0, "data": {"open_id": "ou_a"}},
                                      {"ok": True, "identity": "bot", "data": {}},
                                      {"ok": False, "identity": "user", "data": {}}, []])
def test_lark_requires_user_success_envelope(payload):
    fake = Scripted()
    fake.responses["/open-apis/authen/v1/user_info"] = payload
    assert "user_envelope_required" in reasons(run(fake))


def test_one_chat_failure_preserves_other_chat():
    fake = Scripted()
    def messages(params, count):
        if params["container_id"] == "oc_one":
            raise CollectionError("provider_failed")
        return envelope({"items": [message("oc_two")], "has_more": False})
    fake.overrides["/open-apis/im/v1/messages"] = messages
    result = run(fake, chats=("oc_one", "oc_two"))
    assert any(s["partition"] == "feishu:oc_two" for s in result["sources"])
    assert result["identities"]["feishu_verified"]


@pytest.mark.parametrize("provider", ["github", "feishu"])
def test_resource_mismatch_withholds_provider(provider):
    fake = Scripted()
    if provider == "github":
        fake.responses[f"repos/{REPO}/pulls"][0]["base"]["repo"]["id"] = True
    else:
        fake.responses["/open-apis/im/v1/messages"]["data"]["items"][0]["chat_id"] = "oc_other"
    result = run(fake)
    assert "resource_mismatch" in reasons(result)
    assert all(s["provider"] != provider for s in result["sources"])
    for p, cov in result["coverage"].items():
        if p.startswith(provider + ":"):
            assert cov["records_received"] == cov["records_excluded"]


@pytest.mark.parametrize("field,value", [("id", True), ("number", "12"), ("updated_at", "yesterday"),
                                          ("title", 3), ("body", []), ("body", "x" * 65536)])
def test_bad_records_do_not_erase_valid_records(field, value):
    fake = Scripted()
    bad = pr()
    bad[field] = value
    fake.responses[f"repos/{REPO}/pulls"] = [bad, pr()]
    result = run(fake)
    assert any(s["source_id"] == "github:pulls:100" for s in result["sources"])
    cov = result["coverage"]["github:pulls"]
    assert cov["records_received"] == 2 and cov["records_included"] == 1
    assert cov["records_excluded"] == 1 and result["gaps"]


@pytest.mark.parametrize("has_more", [None, 1, "false"])
def test_missing_or_wrong_pagination_boolean_is_partial(has_more):
    fake = Scripted()
    fake.responses["/open-apis/im/v1/messages"]["data"]["has_more"] = has_more
    result = run(fake)
    assert "pagination_schema" in reasons(result)
    assert not result["coverage"]["feishu:oc_one"]["exhausted"]
    assert any(s["provider"] == "feishu" for s in result["sources"])


def test_repeat_cursor_and_different_valid_records():
    fake = Scripted()
    fake.overrides["/open-apis/im/v1/messages"] = lambda params, count: envelope({
        "items": [message(message_id=f"om_{count}")], "has_more": True, "page_token": "same"})
    result = run(fake)
    assert "repeat_cursor" in reasons(result)
    assert result["coverage"]["feishu:oc_one"]["pages_included"] == 2


@pytest.mark.parametrize("repeat", [False, True])
def test_github_page_cap_or_repeat_never_claims_exhaustion(repeat):
    fake = Scripted()
    def pulls(params, count):
        values = []
        for i in range(50):
            record = pr()
            record.update(id=1000 + i + (0 if repeat else count * 50), merged_at=None,
                          updated_at=AT)
            values.append(record)
        return values
    fake.overrides[f"repos/{REPO}/pulls"] = pulls
    result = run(fake)
    assert ("duplicate_drift" if repeat else "page_limit") in reasons(result)
    cov = result["coverage"]["github:pulls"]
    assert cov["pages_received"] == (2 if repeat else 4)
    assert not cov["exhausted"]
    ids = [s["source_id"] for s in result["sources"]]
    assert len(ids) == len(set(ids))


def test_issue_pr_exclusion_and_half_open_local_filter():
    fake = Scripted()
    excluded = issue()
    excluded["pull_request"] = {}
    fake.responses[f"repos/{REPO}/issues"] = [excluded]
    end_message = message(create_time=str(int(datetime.fromisoformat(END.replace(
        "Z", "+00:00")).timestamp() * 1000)))
    fake.responses["/open-apis/im/v1/messages"]["data"]["items"] = [end_message]
    result = run(fake)
    assert not any(s["partition"] in ("github:issues", "feishu:oc_one") for s in result["sources"])
    assert result["coverage"]["github:issues"]["reasons"]["pr_in_issues_partition"] == 1
    assert result["coverage"]["feishu:oc_one"]["reasons"]["outside_window"] == 1


@pytest.mark.parametrize("change,reason", [
    ({"message_type": "image"}, "non_text_message_not_expanded"),
    ({"deleted": True}, "deleted_message_historical_content_unavailable"),
    ({"update_time": "1790000000000"}, "edited_message_historical_content_unavailable"),
    ({"thread_id": "omt_a"}, "thread_not_expanded"),
])
def test_media_deleted_edited_thread_are_observations_with_gaps(change, reason):
    fake = Scripted()
    fake.responses["/open-apis/im/v1/messages"]["data"]["items"] = [message(**change)]
    result = run(fake)
    assert reason in reasons(result)
    assert any(s["provider"] == "feishu" for s in result["sources"])


def test_explicit_exact_pr_only_no_semantic_auto_join():
    fake = Scripted()
    text = (f"https://github.com/{REPO}/pull/12中文 #12 "
            "https://github.com/other/repo/pull/12 https://github.com/osborn-studio/ostag/pull/999 "
            f"https://github.com/{REPO}/pull/123evil https://github.com/{REPO}/pull/12/files")
    fake.responses["/open-apis/im/v1/messages"]["data"]["items"] = [
        message(body={"content": json.dumps({"text": text})})]
    result = run(fake)
    chat = next(s for s in result["sources"] if s["provider"] == "feishu")
    assert chat["associated_source_ids"] == ["github:pulls:100"]
    assert len(result["questions"]) == 3


def test_markdown_inert_and_normal_text_readable_json_lossless():
    fake = Scripted()
    malicious = '<img src=x onerror=alert(1)>\n````\n[x]([nested](https://evil.test))\n\x1b[2J\r\u202eend'
    fake.responses[f"repos/{REPO}/pulls"][0]["body"] = NORMAL + "\n" + malicious
    result = run(fake)
    rendered = markdown(result)
    assert rendered.startswith("# Coverage gaps")
    assert NORMAL in rendered and malicious not in rendered
    assert "\\u001b[2J\\u000d\\u202eend" in rendered
    assert "`````text" in rendered and '<img src=x onerror=alert(1)>' in rendered
    outside, fence = [], None
    for line in rendered.splitlines():
        if fence is None and re.fullmatch(r"`{3,}text", line):
            fence = line[:-4]
        elif fence == line:
            fence = None
        elif fence is None:
            outside.append(line)
    assert fence is None
    assert not any("evil.test" in line or "<img" in line for line in outside)
    assert any(f"[GitHub source](https://github.com/{REPO}/pull/12)" == line for line in outside)
    encoded = json.dumps(result, ensure_ascii=True)
    source = next(s for s in json.loads(encoded)["sources"] if s["source_id"] == "github:pulls:100")
    assert source["content"].endswith(NORMAL + "\n" + malicious)
    assert source["content_sha256"] == hashlib.sha256(source["content"].encode()).hexdigest()
    assert inert("normal\nnext") == "```text\nnormal\nnext\n```"


@pytest.mark.parametrize("changes", [
    {"repo": "../oops"}, {"repo": "a/.."}, {"start": END}, {"end": "2027-01-01T00:00:00Z"},
    {"start": "2026-07-01T00:00:00Z"}, {"start": "2026-09-13T07:00:00"},
    {"timezone": "Mars/Base"}, {"goal": ""}, {"goal": "\x1b[2J"},
    {"chats": ("oc_one", "oc_two", "oc_three")}, {"chats": ("oc_one", "oc_one")},
    {"chats": ("oc_../../",)},
])
def test_validation_before_all_io(changes):
    fake = Scripted()
    args = dict(repo=REPO, start=START, end=END, timezone="America/Los_Angeles",
                goal="项目周会", chats=("oc_one",))
    args.update(changes)
    with pytest.raises((ValueError, CollectionError)):
        collect(**args, transport=fake, wall=lambda: NOW)
    assert fake.calls == []


def test_schema_failure_is_gap_no_traceback():
    fake = Scripted()
    fake.responses[f"repos/{REPO}/pulls"] = {"message": "PRIVATE ERROR"}
    result = run(fake)
    assert "invalid_schema" in reasons(result)
    assert "PRIVATE ERROR" not in json.dumps(result)
    assert result["facts"]


def test_cli_invalid_stdout_generic_and_no_settings(monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        pytest.fail("settings must not run")
    monkeypatch.setattr("ostag.main.load_settings", forbidden)
    code = asyncio.run(main_async(["--config", "/no/file", "--backend", "openai_api",
                                  "meeting", "collect", "--repo", "SECRET\x1b"]))
    output = capsys.readouterr()
    assert code == 2 and output.err == "" and output.out == "meeting collect: invalid input\n"
    assert build_parser().parse_args(["meeting", "preview", "--input", "a"]).input == "a"


def test_real_process_cli_leading_globals_uses_only_scripted_provider_binaries(tmp_path):
    fake = Scripted()
    responses = tmp_path / "responses.json"
    responses.write_text(json.dumps(fake.responses))
    script = (f"#!{PYTHON}\nimport json,sys\n"
              f"data=json.load(open({str(responses)!r}))\n"
              "args=sys.argv[1:]\n"
              "endpoint=args[2] if args[0]=='api' and args[1]=='GET' else args[-1].split('?')[0]\n"
              "print(json.dumps(data[endpoint]))\n")
    for name in ("gh", "lark-cli"):
        path = tmp_path / name
        path.write_text(script)
        path.chmod(0o755)
    (tmp_path / "sitecustomize.py").write_text(
        "import ostag.config,sqlite3\n"
        "def forbidden(*a,**k): raise RuntimeError('forbidden service execution')\n"
        "ostag.config.load_settings=forbidden\nsqlite3.connect=forbidden\n"
        "import ostag.core.runner\nostag.core.runner.OSTagRunner.__init__=forbidden\n"
        "import ostag.adapters.feishu\nostag.adapters.feishu.FeishuAdapter.__init__=forbidden\n")
    env = dict(os.environ, PATH=str(tmp_path) + os.pathsep + os.environ["PATH"],
               PYTHONPATH=str(tmp_path) + os.pathsep + str(Path.cwd()))
    args = [PYTHON, "-m", "ostag.main", "--config", str(tmp_path / "does-not-exist"),
            "--backend", "openai_api", "--doctor", "--live", "meeting", "collect",
            "--repo", REPO, "--start", START, "--end", END, "--timezone", "America/Los_Angeles",
            "--goal", "项目周会", "--chat-id", "oc_one", "--format", "json"]
    completed = subprocess.run(args, capture_output=True, text=True, env=env, timeout=20)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert completed.stderr == ""
    result = json.loads(completed.stdout)
    assert result["status"] == "draft_ready" and result["facts"]
    assert not (tmp_path / "does-not-exist").exists()


def test_fact_limit_is_visible_and_never_overflows():
    fake = Scripted()
    def pulls(params, count):
        records = []
        for i in range(50):
            record = pr()
            record.update(id=1000 + count * 50 + i, created_at=AT, updated_at=AT)
            records.append(record)
        return records
    fake.overrides[f"repos/{REPO}/pulls"] = pulls
    result = run(fake)
    assert len(result["facts"]) <= 500 and "fact_limit" in reasons(result)
    assert result["status"] == "needs_input"
    assert result["identities"]["github_verified"] and result["identities"]["feishu_verified"]


def test_feishu_page_cap_visible_even_short_pages_with_has_more():
    fake = Scripted()
    fake.overrides["/open-apis/im/v1/messages"] = lambda params, count: envelope({
        "items": [message(message_id=f"om_{count}")], "has_more": True, "page_token": str(count)})
    result = run(fake)
    assert "page_limit" in reasons(result)
    assert result["coverage"]["feishu:oc_one"]["pages_received"] == 4
    assert not result["coverage"]["feishu:oc_one"]["exhausted"]


def test_provider_and_final_identity_time_reserves():
    fake = Scripted()
    monotonic = [1000.0]
    real_get = fake.get
    def slow_get(provider, endpoint, params, deadline):
        assert monotonic[0] < deadline <= 1120
        monotonic[0] = min(deadline, monotonic[0] + 10)
        return real_get(provider, endpoint, params, deadline)
    fake.get = slow_get
    result = run(fake, clock=lambda: monotonic[0])
    assert result["observation"]["elapsed_seconds"] <= 120
    assert "deadline" in reasons(result)
    assert result["identities"]["github_verified"]
    assert result["identities"]["feishu_verified"]
    assert len([c for c in fake.calls if c[1] == "user"]) == 2
    assert len([c for c in fake.calls if c[1].endswith("user_info")]) == 2


def test_oversized_page_counts_excluded_records():
    fake = Scripted()
    fake.responses[f"repos/{REPO}/pulls"] = [pr()] * 51
    result = run(fake)
    cov = result["coverage"]["github:pulls"]
    assert cov["pages_received"] == cov["pages_excluded"] == 1
    assert cov["records_received"] == cov["records_excluded"] == 51
    assert "page_size_limit" in reasons(result)


def test_duplicate_with_changed_body_does_not_silently_overwrite():
    fake = Scripted()
    changed = pr()
    changed["body"] = "changed body"
    fake.responses[f"repos/{REPO}/pulls"] = [pr(), changed]
    result = run(fake)
    assert "duplicate_drift" in reasons(result)
    source = next(s for s in result["sources"] if s["source_id"] == "github:pulls:100")
    assert NORMAL in source["content"] and "changed body" not in source["content"]


@pytest.mark.parametrize("change", [{"message_id": True}, {"create_time": 123},
                                    {"deleted": "false"}, {"sender": {}},
                                    {"thread_id": 42}, {"body": {"content": '{"text": []}'}}])
def test_feishu_invalid_schemas_keep_valid_sibling(change):
    fake = Scripted()
    fake.responses["/open-apis/im/v1/messages"]["data"]["items"] = [
        message(**change), message(message_id="om_valid")]
    result = run(fake)
    assert "invalid_schema" in reasons(result)
    assert any(s["source_id"].endswith("om_valid") for s in result["sources"])


def test_cli_exit_three_for_missing_scope_and_lossless_json(monkeypatch, capsys):
    import ostag.briefs.meeting_collect as module
    original = module.collect
    monkeypatch.setattr(module, "collect", lambda *a, **k: original(
        *a, **k, transport=Scripted(), wall=lambda: NOW))
    code = asyncio.run(main_async(["meeting", "collect", "--repo", REPO, "--start", START,
                                  "--end", END, "--timezone", "America/Los_Angeles",
                                  "--goal", "项目周会", "--format", "json"]))
    output = capsys.readouterr()
    assert code == 3 and output.err == ""
    assert json.loads(output.out)["status"] == "needs_input"


@pytest.mark.parametrize("field,value", [("id", True), ("full_name", "other/repo"),
                                        ("permissions", {"pull": 1}), ("default_branch", "..")])
def test_invalid_repository_preflight_never_fetches_records(field, value):
    fake = Scripted()
    fake.responses[f"repos/{REPO}"][field] = value
    result = run(fake)
    assert not any(c[0] == "github" and c[1] not in ("user", f"repos/{REPO}") for c in fake.calls)
    assert {f["provider"] for f in result["facts"]} == {"feishu"}


def test_leading_abbreviated_global_still_isolates_collect(monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        pytest.fail("settings must not run")
    monkeypatch.setattr("ostag.main.load_settings", forbidden)
    code = asyncio.run(main_async(["--conf=/missing", "meeting", "collect", "--repo", REPO,
                                  "--start", START, "--end", END, "--timezone", "UTC", "--goal", ""]))
    assert code == 2 and capsys.readouterr().out == "meeting collect: invalid input\n"
