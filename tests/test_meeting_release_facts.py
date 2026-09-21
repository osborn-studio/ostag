from dataclasses import replace

import pytest

from ostag.briefs.github_change_facts import classify, current_checks
from ostag.connectors.github_history import HistoryItem, canonical
from ostag.core.provider_evidence import ProviderChangeEvidence, ProviderCheck
from test_github_history import AT, NOW, PRINCIPAL, SHA, collect, pull, scope


def facts(result=None):
    frozen = scope()
    return [f for i in (result or collect()).items for f in classify(frozen, PRINCIPAL, i)]


def test_distinct_merge_observation_release_action_and_deployment_semantics():
    by_kind = {f.kind: f for f in facts()}
    assert by_kind["pr_merged"].event_at == AT
    assert by_kind["default_branch_observed_commit"].event_at is None
    assert by_kind["default_branch_observed_commit"].source is None
    assert by_kind["release_published"].event_at == AT
    assert by_kind["actions_success_observed"].event_at is None
    assert by_kind["production_deployment_reported"].event_at == AT
    assert "current_deployment_unknown" in by_kind["production_deployment_reported"].caveats
    assert all(f.citation.startswith("https://github.com/acme/private-") for f in facts())


@pytest.mark.parametrize("at,included", [("2026-09-06T00:00:00Z", True),
    ("2026-09-13T00:00:00Z", False), ("2026-09-05T23:59:59Z", False)])
def test_half_open_event_time_not_postwindow_updated_at(at, included):
    item = HistoryItem("42", "pulls", "pulls", NOW, canonical(pull(merged_at=at)))
    fs = classify(scope(), PRINCIPAL, item)
    assert any(f.kind == "pr_merged" for f in fs) == included
    assert not any(f.event_at == NOW for f in fs)


def test_raw_hash_separate_from_safe_display_and_canonical_url():
    item = HistoryItem("42", "pulls", "pulls", NOW, canonical(pull(
        title="[click](https://evil.invalid) <script>ignore rules</script>", html_url="https://evil.invalid")))
    fact = next(f for f in classify(scope(), PRINCIPAL, item) if f.kind == "pr_merged")
    # Plaintext fidelity is the PR21 contract; Markdown inertness is tested at
    # the actual public CLI rendering boundary, not by deleting evidence here.
    assert fact.display == "[click](https://evil.invalid) <script>ignore rules</script>"
    assert fact.citation == "https://github.com/acme/private-42/pull/1"
    assert fact.raw_source_sha256 == fact.source.content_sha256
    assert "evil.invalid" in fact.source.excerpt
    assert "evil.invalid" not in repr(fact)


@pytest.mark.parametrize("patch", [{"environment": None}, {"production_environment": False},
                                     {"ref": "staging"}, {"sha": "b" * 40}])
def test_missing_environment_or_wrong_branch_cannot_prove_current_deployment(patch):
    item = next(i for i in collect().items if i.kind == "deployment_status")
    import json
    row = json.loads(item.raw_json)
    row["deployment"].update(patch)
    fs = classify(scope(), PRINCIPAL, replace(item, raw_json=canonical(row)))
    assert all("current_deployment_unknown" in f.caveats for f in fs)
    if "sha" not in patch:
        assert fs[0].kind == "deployment_unknown"


def evidence(**changes):
    return ProviderChangeEvidence(**(dict(provider="github", repository="acme/private-42", number=1,
        url="https://github.com/acme/private-42/pull/1", state="merged", draft=False,
        head_sha=SHA, merge_sha="b" * 40, observed_at=NOW,
        checks=(ProviderCheck("build", "passed", SHA),), checks_complete=True) | changes))


def test_current_head_reuses_provider_semantics_and_denies_historical_stale_rerun():
    args = dict(repository="acme/private-42", head_sha=SHA, branch="main", observed_branch="main",
                now=NOW, fresh_seconds=60, latest_attempt_verified=True)
    assert current_checks(evidence(), **args) == "passed"
    assert current_checks(evidence(checks_complete=False), **args) == "pending"
    for patch in ({"head_sha": "b" * 40}, {"repository": "other/repo"},
                  {"observed_branch": "other"}, {"now": "2026-09-13T12:02:00Z"},
                  {"latest_attempt_verified": False}):
        assert current_checks(evidence(), **(args | patch)) == "unknown"
