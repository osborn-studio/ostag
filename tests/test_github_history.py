"""Synthetic documented REST envelopes through the actual bounded-worker argv contract."""
from dataclasses import replace
import json
from urllib.parse import parse_qs, urlsplit
import sys

import pytest

from ostag.briefs.meeting_io import CollectionError, RESPONSE_BYTES
from ostag.briefs.meeting_models import SourceCategory
from ostag.connectors.github_history import API_VERSION, GitHubPrincipal, collect_history
from test_meeting_contracts import NOW, intake, request

SHA = "a" * 40
AT = "2026-09-08T12:00:00Z"
OLD = "2025-01-01T00:00:00Z"
PRINCIPAL = GitHubPrincipal("7", "alice-dev")


def scope(categories=(SourceCategory.PULLS, SourceCategory.COMMITS, SourceCategory.RELEASES,
                      SourceCategory.CHECKS, SourceCategory.DEPLOYMENTS)):
    return intake(request(chat_ids=[], source_categories=categories,
                          required_partition_ids=["github:repo:42:" + categories[0].value,
                                                  "github:repo:43:" + categories[0].value])).scope


def pull(repo=42, **changes):
    return dict(id=100, number=1, title="Decide rollout", body="Synthetic only", state="closed",
                created_at=OLD, updated_at=NOW, merged_at=AT,
                head={"sha": SHA}, base={"ref": "main", "repo": {
                    "id": repo, "full_name": f"acme/private-{repo}"}}) | changes


class Runner:
    def __init__(self):
        self.calls = []
        self.override = None

    def __call__(self, argv, *, deadline, clock):
        assert argv[:3] == [sys.executable, "-c",
                            "from ostag.connectors.github_history import _http_get; _http_get()"]
        assert 0 < deadline - clock() <= 15
        endpoint = urlsplit(argv[-1])
        assert not endpoint.scheme and not endpoint.netloc
        params = parse_qs(endpoint.query)
        if params:
            assert params.get("per_page", ["50"]) == ["50"]
        self.calls.append((endpoint.path, params))
        if self.override:
            value = self.override(endpoint.path, params, self.calls)
            if value is not None:
                if type(value) is tuple:
                    return value
                return response(value)
        return response(self.body(endpoint.path))

    def body(self, path):
        if path == "user":
            return {"id": 7, "login": "alice-dev"}
        repo = int(path.split("/")[2].removeprefix("private-"))
        assert repo in (42, 43)
        suffix = "/".join(path.split("/")[3:])
        if not suffix:
            return dict(id=repo, full_name=f"acme/private-{repo}", default_branch="main",
                        permissions={"pull": True})
        if suffix == "pulls":
            return [pull(repo)] if repo == 42 else [pull(repo, merged_at=None, state="open")]
        if suffix == "issues/1/timeline":
            return [{"id": 200, "event": "merged" if repo == 42 else "labeled", "created_at": AT}]
        if suffix == "commits/main":
            return {"sha": SHA}
        if suffix == "commits":
            return [{"sha": SHA, "commit": {"message": "Old author newly observed", "author": {
                "date": OLD}, "committer": {"date": OLD}}}]
        if suffix == "releases":
            return [{"id": 300, "tag_name": "v1.0", "name": "Release", "draft": False,
                     "published_at": AT}]
        if suffix == "actions/runs":
            return {"total_count": 1, "workflow_runs": [dict(id=400, name="Build", head_sha=SHA,
                    repository={"id": repo, "full_name": f"acme/private-{repo}"}, head_branch="main",
                    status="completed", conclusion="success", run_attempt=2, updated_at=NOW)]}
        if suffix == "deployments":
            return [dict(id=500, sha=SHA, ref="main", environment="production",
                         production_environment=True)]
        if suffix == "deployments/500/statuses":
            return [dict(id=501, state="success", environment="production", created_at=AT)]
        return []


def response(value, header=""):
    raw = ("HTTP/2.0 200 OK\r\nContent-Type: application/json\r\n" + header + "\r\n" +
           json.dumps(value)).encode()
    return raw, len(raw)


def collect(fake=None, frozen=None, **kwargs):
    return collect_history(frozen or scope(), expected_principal=PRINCIPAL,
                           authorize=kwargs.pop("authorize", lambda *_: True), runner=fake or Runner(),
                           clock=kwargs.pop("clock", lambda: 10.0), wall=lambda: NOW, **kwargs)


def test_two_repo_read_identity_before_after_no_date_filter_and_global_counts():
    fake = Runner()
    result = collect(fake)
    assert {i.repo_id for i in result.items} == {"42", "43"}
    assert result.calls == len(fake.calls)
    assert [p for p, _ in fake.calls].count("user") == 4
    assert all("since" not in q and "until" not in q for p, q in fake.calls if p.endswith("commits"))
    assert all(q["sha"] == [SHA] for p, q in fake.calls if p.endswith("commits"))
    assert all(p.status.value == "partial" for p in result.reports)
    assert any("arrival_unknown" in g for g in result.gaps)
    assert all("events" not in p and not p.startswith("orgs") for p, _ in fake.calls)


@pytest.mark.parametrize("failure", ["id", "login", "repo_id", "name", "branch", "permission"])
def test_after_identity_failure_withholds_partition_content(failure):
    fake = Runner()
    def override(path, params, calls):
        if path == "user" and sum(p == "user" for p, _ in calls) == 2:
            if failure == "id":
                return dict(id=99, login="alice-dev")
            if failure == "login":
                return dict(id=7, login="other")
        if path == "repos/acme/private-42" and sum(p == path for p, _ in calls) == 2:
            value = fake.body(path)
            for f, key, val in [("repo_id", "id", 99), ("name", "full_name", "evil/repo"),
                                ("branch", "default_branch", "changed"),
                                ("permission", "permissions", {"pull": False})]:
                if failure == f:
                    value[key] = val
            return value
    fake.override = override
    result = collect(fake)
    assert not any(i.repo_id == "42" for i in result.items)
    assert any(i.repo_id == "43" for i in result.items)
    assert all(r.status.value == "failed" for r in result.reports if ":42:" in r.partition_id)


def test_current_authorization_receives_exact_scope_principal_and_repo_and_revokes():
    frozen = scope()
    fake = Runner()
    def authorize(s, principal, repo):
        assert s == frozen and principal == PRINCIPAL and repo in frozen.repositories
        return len(fake.calls) < 4
    result = collect(fake, frozen, authorize=authorize)
    assert not result.items
    assert len(fake.calls) == 4


@pytest.mark.parametrize("status", ["304 Not Modified", "302 Found", "403 Forbidden", "429 Too Many Requests"])
def test_no_cache_redirect_retry_or_rate_limit_wait(status):
    fake = Runner()
    fake.override = lambda p, q, c: (f"HTTP/2.0 {status}\r\n\r\n{{}}".encode(), 100)
    assert not collect(fake).items
    assert len(fake.calls) == 2


def test_global_call_budget_never_resets_per_repo():
    frozen = scope((SourceCategory.PULLS,))
    frozen = replace(frozen, budget=replace(frozen.budget, max_provider_calls=8))
    fake = Runner()
    result = collect(fake, frozen)
    assert result.calls == len(fake.calls) == 8
    assert not any(i.repo_id == "43" for i in result.items)


def test_response_limit_and_resume_rejected_without_reset():
    fake = Runner()
    fake.override = lambda *a: (b"x" * (RESPONSE_BYTES + 1), RESPONSE_BYTES + 1)
    assert not collect(fake).items
    fake = Runner()
    with pytest.raises(CollectionError) as error:
        collect(fake, resume={"calls": 0, "scope_hash": scope().content_hash})
    assert not fake.calls
    assert error.value.__context__ is None and error.value.__cause__ is None


@pytest.mark.parametrize("mode", ["loop", "foreign", "missing", "skipped", "empty_next"])
def test_pagination_negatives_are_visible(mode):
    fake = Runner()
    def override(path, params, calls):
        if not path.endswith("/pulls"):
            return None
        next_page = 3 if mode == "skipped" else 2
        repo = int(path.split("/")[2].removeprefix("private-"))
        rows = [] if mode == "empty_next" else [pull(repo, id=i + 1, number=i + 1) for i in range(50)]
        if mode == "missing":
            return response(rows)
        target = "https://evil.invalid/" if mode == "foreign" else "https://api.github.com/"
        link = f'Link: <{target}{path}?state=all&per_page=50&page={next_page}>; rel="next"\r\n'
        return response(rows, link)
    fake.override = override
    result = collect(fake, scope((SourceCategory.PULLS,)))
    assert result.gaps and all(r.status.value != "complete" for r in result.reports)
    assert result.calls <= scope().budget.max_provider_calls


def test_runner_failure_chain_never_retains_provider_secrets():
    def broken(*a, **k):
        raise ValueError("SECRET SOURCE BODY")
    result = collect(broken)
    assert not result.items and "SECRET" not in repr(result)


def test_https_worker_real_entry_pins_headers_and_does_not_follow_redirect(monkeypatch):
    import io
    from ostag.connectors.github_history import _http_get
    calls = []
    class Reply:
        status = 302
        def getheaders(self):
            return [("Location", "https://evil.invalid"), ("Link", '<https://evil.invalid>; rel="next"')]
        def read(self, size):
            assert size == RESPONSE_BYTES + 1
            return b"{}"
    class Connection:
        def __init__(self, hostname, timeout):
            assert hostname == "api.github.com" and timeout == 15
        def request(self, method, path, headers):
            calls.append((method, path, headers))
        def getresponse(self):
            return Reply()
        def close(self):
            pass
    output = io.TextIOWrapper(io.BytesIO())
    monkeypatch.setattr("http.client.HTTPSConnection", Connection)
    monkeypatch.setenv("GH_TOKEN", "synthetic-token")
    monkeypatch.setattr(sys, "argv", ["worker", "repos/acme/private-42/pulls?per_page=50&page=1"])
    monkeypatch.setattr(sys, "stdout", output)
    with pytest.raises(SystemExit) as result:
        _http_get()
    assert result.value.code == 0
    assert len(calls) == 1 and calls[0][0] == "GET"
    assert calls[0][2]["X-GitHub-Api-Version"] == API_VERSION
    assert "synthetic-token" not in output.buffer.getvalue().decode()
    assert output.buffer.getvalue().startswith(b"HTTP/1.1 302")


def test_byte_budget_charges_failures_and_never_resets():
    fake = Runner()
    def override(path, params, calls):
        raw, _ = response(fake.body(path))
        return raw, RESPONSE_BYTES
    fake.override = override
    result = collect(fake)
    assert result.response_bytes == 16 * RESPONSE_BYTES
    assert len(fake.calls) == 16
    assert any("byte_limit" in gap for gap in result.gaps)


def test_collection_deadline_withholds_content_and_preserves_reserve():
    fake = Runner()
    now = [10.0]
    def override(path, params, calls):
        now[0] += 14
        return fake.body(path)
    fake.override = override
    result = collect(fake, clock=lambda: now[0])
    assert not result.items
    assert now[0] < 190  # hard deadline, 60 seconds generation reserve not spent collecting
    assert len(fake.calls) <= 9


def test_page_cap_and_repeated_record_are_explicit_gaps():
    for repeated in (False, True):
        fake = Runner()
        def override(path, params, calls):
            if not path.endswith("/releases"):
                return None
            page = int(params["page"][0])
            rows = [{"id": 1 if repeated else page}]
            link = f'Link: <https://api.github.com/{path}?per_page=50&page={page + 1}>; rel="next"\r\n'
            return response(rows, link)
        fake.override = override
        result = collect(fake, scope((SourceCategory.RELEASES,)))
        reason = "pagination_overlap_or_loop" if repeated else "category_page_limit"
        assert any(reason in gap for gap in result.gaps)
        assert len([p for p, _ in fake.calls if p.endswith("/releases")]) <= 8


def test_global_fact_cap_is_not_per_repository():
    frozen = scope((SourceCategory.RELEASES,))
    frozen = replace(frozen, budget=replace(frozen.budget, max_github_facts=1))
    result = collect(frozen=frozen)
    assert len(result.items) == 1
    assert any("global_fact_limit" in gap for gap in result.gaps)


def test_actions_1000_ceiling_is_explicit_gap():
    fake = Runner()
    def override(path, params, calls):
        if path.endswith("/actions/runs"):
            return fake.body(path) | {"total_count": 1000}
    fake.override = override
    result = collect(fake)
    assert any("search_1000" in gap for gap in result.gaps)
