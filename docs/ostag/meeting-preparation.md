# Private meeting evidence and pre-read batch

This batch provides an operator-local meeting package for an explicitly confirmed
purpose and exact frozen scope. It advances the full31+9 objective; it does **not**
complete M02–M05, activate A11/D02, or establish live/semantic acceptance.
Every package says `needs_input`, `meeting_ready=false`, and
`semantic_review_required=true`. Matching schemas is not evidence of usefulness.

## Runnable synthetic CLI example

From the repository root, with development dependencies installed, run the
following. It uses the embedded synthetic fixture in `test_meeting_journey.py`
and the existing M01 synthetic intake fixture. It invokes the actual module CLI,
uses two repositories and a chat claim, and never calls a provider. No private
R2 input, runtime artifact, credential or reviewer is involved.

```bash
PYTHONPATH=tests .venv/bin/python - <<'PY'
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from test_meeting_journey import synthetic_fixture

root = Path(tempfile.mkdtemp(prefix="meeting-synthetic-"))
# Resolve the OS temporary-directory alias; the reader rejects symlink components.
root = root.resolve()
scope, evidence = synthetic_fixture()
for name, value in (("scope.json", scope.to_dict()), ("evidence.json", evidence)):
    fd = os.open(root / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(value, stream)
command = [sys.executable, "-m", "ostag.briefs.meetings",
           "--scope", str(root / "scope.json"),
           "--confirmed-purpose", scope.request.purpose.value,
           "--confirmed-scope-hash", scope.content_hash,
           "--evidence", str(root / "evidence.json"),
           "--output", str(root / "package")]
result = subprocess.run(command, check=False)
assert result.returncode == 3  # useful partial draft, never meeting_ready
print(root / "package" / "meeting.md")
print(root / "package" / "manifest.json")
PY
```

The concise operator command is:

```bash
.venv/bin/python -m ostag.briefs.meetings \
  --scope /absolute/private/scope.json \
  --confirmed-purpose meeting-prep \
  --confirmed-scope-hash EXACT_ALREADY_CONFIRMED_SCOPE_SHA256 \
  --evidence /absolute/private/evidence.json \
  --output /absolute/private/NEW-package-directory
```

`--help` and imports perform no provider or business-storage IO. Exit 3 means a
private partial package was written; 2 means input/authorization/output was denied;
0 is help. The CLI prints only code-owned status, not source bodies or paths.
The output directory must be new. Markdown and manifest are individually atomic,
no-replace files, mode 0600, in a new mode-0700 directory. A failed publication may
leave a partial private directory; the pair is not a transactional database commit.
Existing files/directories are never overwritten. Symlinks in any component,
`..`, FIFOs and oversized input/output are denied. Use canonical paths (for
example `/private/tmp` rather than a symlinked `/tmp` on macOS).

## Current collection callable and authorization boundary

`ostag.briefs.meetings.prepare_meeting(...)` is the public current-read path:

```python
package = prepare_meeting(
    scope,  # existing FrozenScope from trusted freeze_meeting_scope intake
    confirmed_purpose=confirmed_purpose,
    confirmed_scope_hash=confirmed_scope_hash,
    expected_principal=GitHubPrincipal(expected_numeric_user_id, expected_login),
    authorize=current_local_policy_mirror,
)
write_private_package(destination, package.markdown, package.manifest_json)
```

The callback signature is `(FrozenScope, GitHubPrincipal, Repository) -> bool`.
It is **trusted application code**, not a JSON field or dynamically imported user
module. It must check the exact scope hash, namespace, requester, grant ID,
revision and digest, mapping/config pins, repository, and requester-to-GitHub-user
binding against current locally trusted policy. It must fail closed if stale or
revoked. It runs before/after requests and after assembly; it must be nonblocking
and meet the existing service's operational 100 ms authorization budget. Python
cannot forcibly preempt arbitrary trusted callbacks. Do not put network IO there.

The embeddable `meetings.main(argv, authorize=callback)` accepts `--live
--github-user-id ID --github-login LOGIN` instead of `--evidence`. It calls the
same public preparation path. The standalone module **rejects `--live`** because
this repository has no approved production meeting-authority issuance adapter.
An arbitrary scope file cannot install a policy mirror. Tests inject a fake
runner and an explicitly synthetic callback, never a production authority.

The default runner uses the existing `meeting_io.run_process` kill/reap mechanism
to own a Python HTTPS child. The child connects only to `api.github.com`, issues
GET, never follows redirects, and accepts only a **preexisting** `GH_TOKEN` or
`GITHUB_TOKEN` in its inherited environment. No keychain/auth CLI, reauthentication,
proxy discovery, cache, remote URL fetching or token output is performed. An
operator without an already approved environment token cannot use this runner;
this batch does not provision one. All automated collection uses synthetic runners.

The installed `gh` 2.96.0 version/help and official CLI source were checked.
`gh api` supports explicit GET/header/include flags but follows redirects inside
its HTTP client. It is therefore not used as this batch's live HTTP dispatcher.
REST version `2022-11-28` is pinned explicitly, matching the default used by the
existing unversioned connector. No mutating Actions endpoints are permitted.

## Evidence and rendering

The reader enumerates only the selected repositories: PRs, issues, issue comments,
per-issue/PR timelines, PR reviews/review comments, default-branch commits,
releases, Actions runs, deployments and deployment statuses as requested. Timeline
reads belong to the corresponding pulls/issues partition. No org/alias discovery,
Events API or global search supplies completeness. Default-branch history is
anchored at an observed head SHA, without author/committer date filtering; old
commits remain visible with unknown arrival. No prior-head JSON is trusted.

The exact GitHub numeric user ID/login and repository numeric ID/full name/default
branch/read permission are checked before and after each repository. Current
policy is rechecked after other repositories. Failed identity/access checks
withhold that repository's content and fail its partitions, including required
ones. Repositories with other failures retain bounded partial evidence and gaps.
Selected branch metadata remains scope-bound; commit enumeration is explicitly
for the default branch. Non-default PR targets carry a caveat.

Historical events use UTC `[start,end)`. `updated_at` never supplies an event time.
PR merges, commit observations, published releases, Actions-success observations,
and production deployment reports are distinct. A workflow-list `updated_at` is
not an invented completion timestamp. A deployment report requires a numeric
status receipt, success, explicit matching environment, production designation,
branch and SHA. It still cannot establish **current** deployed state or absence
of rollback. Missing evidence means unknown. Releases never imply deployment.

`current_checks` reuses `ProviderChangeEvidence.checks_state`, requiring a fresh
exact repository/head/branch and an externally verified latest attempt. The
collector does not synthesize that current-head authority from old workflow
success, nor does it fetch complete current check-runs/statuses/attempt histories.

Original JSON record hashes use canonical JSON before display transformation;
they are not hashes of the original HTTP wire framing. Original chat source
hashes are preserved. Display fields remain readable plaintext, including URLs,
markup, filenames and quotes, following the consolidated `3376030` contract.
Only control/bidi characters and line breaks are normalized for display. Markdown
projects source text into literal code spans with collision-safe backtick
delimiters; JSON display fields are data, never HTML or pre-escaped Markdown.
Citations are constructed only for selected `github.com/owner/repo` paths.
No instructions or URLs found in content are executed or fetched. Bounded display
excerpts may be shortened; provenance still hashes the whole raw GitHub record.

Temporal facts reuse `SourceRef` and `SourceSet`. Unknown-time observations retain
hashes/citations in the manifest but cannot enter the temporal SourceSet. Imported
chat claims use existing `SourceRef` contracts and explicit operator dispositions;
the batch does not infer commitments, owners or deadlines from prose. Their
current ACL and completeness remain unproven. Exact selected-repo links associate
chat and repository evidence; the frozen project mapping defines the selected
project. Same display names/logins never join people. Semantic association is
only a candidate. Conflicting versions are retained with a conflict gap.

The pre-read puts coverage failures first, separates chat/repository/deployment
evidence, and chooses at most three proposed decision topics from blocked,
conflicting or pending inputs. It reserves 5 minutes for evidence review, 5 per
topic and 5 for decisions. Actions are suggestions with unknown owner/due.
The manifest includes scope/revision/render digests, source hashes, observation
metadata, partition coverage, budget consumption and empty `shareable_sources`.
Identical inputs produce identical manifests/renders; source changes change the
revision. No performance judgment is inferred from absent activity.

## Budgets, failures and unsupported recovery

Budgets are global for one invocation and never restart per repo/category/failure.
Frozen `BudgetProfile` provides lower limits. Absolute ceilings are 20 repos,
200 calls, 500 GitHub records and 500 imported chat messages, 1 MiB model-input
material, 1 MiB per response including captured stderr, and 16 MiB total.
Derived facts also respect the frozen GitHub fact cap. The extra per-repo caps
are 30 pages and 200 records; per category/repo, 4 pages and 100 records. Page size
is 50. Nested timeline/review/status calls consume those same caps.

Each command is at most 15 seconds; collection is at most 120 seconds, reserving
60 seconds before the 180-second hard request ceiling. Profiles may lower these.
Failed commands conservatively consume a full 1 MiB response allowance. A call is
not started unless that allowance remains. No retry/backoff is attempted (zero
retries); rate-limit failures become gaps instead of sleeping or starving Monitor.
The owned process group is killed and reaped on timeout/output overflow.

Pagination validates exact numeric progression and bound query/path fields,
refuses foreign Link targets, detects overlap/loops and missing next pages, and
reports caps. Actions totals at/above 1000 get an explicit possible ceiling gap.
There is no cache/ETag reuse: 304 without an identity-bound body is denied.
Mutable list snapshots cannot prove missing/deleted/reordered history, so all
collected history partitions stay partial even if pagination exhausts.

**Durable resume is unsupported.** Every supplied resume object is rejected before
IO. Serialized consumption, deadline, principal or scope hashes are diagnostics,
never budget authority. There is no new database, trusted resume capability or
second lifecycle. New attempts must be admitted by a future existing-job adapter;
this operator API does not claim retry-safe durable job accounting.

Public error boundaries discard exceptions and raise outside the handler, leaving
both cause and context empty. Bodies/credentials are absent from public reprs and
diagnostics. Unexpected provider shapes fail closed or become visible gaps;
undocumented fields are not invented.

## Original acceptance coverage and remaining work

| Frozen criterion | Implemented in this batch | Still required for original acceptance |
|---|---|---|
| M02-AC1 | Explicit multi-repo bounded category/timeline enumeration; event/observation separation | Complete historical reconstruction and all timeline variants; live acceptance |
| M02-AC2 | Identity revocation, half-open windows, old-author commits, loops/caps/1000 gaps; required failures never ready | Stable complete pagination/history under mutation; live completeness evidence |
| M02-AC3 | Global caps, zero retries, kill/reap, deadlines/reserve, no cache, resume refusal | Trusted durable cursor/deadline/consumption capability through existing approved storage |
| M03-AC1 | Five evidence classes; first-head arrival unknown; no author-date arrival inference | Trusted prior-head TTL/repo/branch/principal comparison |
| M03-AC2 | Existing current-head semantic helper; stale/SHA/repo/branch/rerun denial; deployment unknown | Full current check/status/attempt collection and durable current deployment authority |
| M03-AC3 | Duplicate/conflicting snapshots, release/deployment separation, visible delete/rewrite/rollback uncertainty | Reconstructing deleted/rerun/rollback sequences and actual live validation |
| M04-AC1 | Exact explicit links, frozen project selection, no name-based identity joins | Richer approved mapping associations and semantic candidate review |
| M04-AC2 | Ordered facts, separate chat/repository/deployment sections, unknown owner/due | Human review of contradictions and extracted commitments; no automatic truth oracle |
| M04-AC3 | Gap-first needs_input, deterministic topic/time/count bounds, mandatory semantic review | Key-claim/numeric semantic evaluation against real authorized sources |
| M05-AC1 | Working private pre-read/manifest and typed `MeetingServiceInput` handoff | BriefService/job/grant/revision authority issuance and lifecycle integration; no outbox bypass |
| M05-AC2 | Exportable local Markdown, flags unchanged/default OFF/rejected | Private-chat status integration and production acceptance; A11/D02 remain gated |
| M05-AC3 | Exact confirmation before collection; no calendar/sharing/table writes; empty shareable sources | Any future sharing API must recheck every source ACL and audience intersection |

The typed handoff carries scope, source set, coverage and revision digest. It
creates no `AuthoritySnapshot`, manual-source impersonation, production DB, worker,
scheduler, card or outbox. Existing `ManualRequest` correctly rejects this source
shape. Archived evidence is a private historical draft whose identity fields are
observations, never current authorization. The legacy private R2 artifact has not
been read, copied, adapted or recollected; this CLI expects the new explicit
archive envelope, not a silent legacy import.

## Validation and official contracts

The display integration tests use an installed `pandoc` CommonMark parser to
verify plaintext fidelity, canonical-only links, and no active HTML/images through
the actual module CLI. Pandoc is a required test dependency, installed explicitly
by CI; missing it fails these checks. The fixed outside verifier's display probe
remains an additional independent integration gate.

```bash
.venv/bin/pytest -q tests/test_github_history.py tests/test_meeting_release_facts.py \
  tests/test_meeting_facts.py tests/test_meeting_journey.py \
  tests/test_meeting_contracts.py tests/test_meeting_preflight.py \
  tests/test_meeting_collect.py tests/test_meeting_io.py tests/test_meeting_preview.py \
  tests/test_brief_flags.py
.venv/bin/ruff check ostag/connectors/github_history.py \
  ostag/briefs/github_change_facts.py ostag/briefs/meeting_facts.py \
  ostag/briefs/meetings.py ostag/briefs/meeting_io.py \
  tests/test_github_history.py tests/test_meeting_release_facts.py \
  tests/test_meeting_facts.py tests/test_meeting_journey.py
```

Actual Claude CLI `claude-opus-5` audit and live/semantic acceptance are the fixed
outside harness's responsibility; neither reviewer nor nested agent is invoked.

Primary contracts consulted:

- [Timeline events](https://docs.github.com/en/rest/issues/timeline)
- [Commits](https://docs.github.com/en/rest/commits/commits)
- [Pull requests](https://docs.github.com/en/rest/pulls/pulls)
- [Workflow runs](https://docs.github.com/en/rest/actions/workflow-runs)
- [Deployments](https://docs.github.com/en/rest/deployments/deployments)
- [Deployment statuses](https://docs.github.com/en/rest/deployments/statuses)
- [REST API versions and legacy default](https://docs.github.com/en/rest/about-the-rest-api/api-versions)
- [Actual gh api flags](https://cli.github.com/manual/gh_api)
- [gh API HTTP dispatch source](https://github.com/cli/cli/blob/v2.96.0/pkg/cmd/api/http.go)
