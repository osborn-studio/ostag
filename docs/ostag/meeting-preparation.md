# Private meeting evidence and pre-read batch

This batch provides an operator-local meeting package for an explicitly confirmed
purpose and exact frozen scope. It advances the full31+9 objective; it does **not**
complete M02–M05, activate A11/D02, or establish live/semantic acceptance.
Operator packages and private exports say `needs_input`, `meeting_ready=false`, and
`semantic_review_required=true`. The local durable job can reach `draft_ready`,
meaning a validated private revision exists, not meeting readiness. Matching
schemas is not evidence of usefulness.

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
second lifecycle. The local durable API below accepts independently trusted pre-collected input
with its original origin/finish and consumption. It never resumes collection.
This operator CLI still does not claim retry-safe durable collection accounting.

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
| M05-AC1 | Local trusted meeting admission through BriefService, persisted job, existing worker, validated revision/provenance and private outbox; restart-tested status/export | Production authority issuer/owner, durable live collection and semantic acceptance |
| M05-AC2 | Current-authorized local status/cancel/private export, retention and lifecycle gates; flags unchanged/default OFF/rejected | Live private-chat status/delivery and production acceptance; A11/D02 remain gated |
| M05-AC3 | Exact confirmation before collection; no calendar/sharing/table writes; empty shareable sources | Any future sharing API must recheck every source ACL and audience intersection |

The typed handoff carries scope, source set, coverage and revision digest.
`DurableMeetingRequest.from_input` consumes an exact current handoff with raw
provenance and original timing. Construction grants no authority; `ManualRequest`
continues to reject this source shape. The operator CLI creates no worker/outbox;
the explicit durable API below uses the existing ones. Archived evidence is a private historical draft whose identity fields are
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

## Local durable application API

This is explicit application composition, not an HTTP route or self-grant
endpoint. A11 production ownership and D02 activation remain the next dependency.
All six flags remain OFF and unsupported activation still rejects. Frozen31+9,
all 40 tasks and all 120 acceptance criteria remain the objective.

The host explicitly initializes the existing BriefStore/manual schema and
RetentionLedger, composes one RetentionCoordinator and BriefLifecycle, and
publishes a current TrustedBriefAuthority snapshot. No meeting schema is added.
The snapshot's `meetings=(request,)` entry means the trusted host independently
owns that exact frozen scope, canonical raw evidence, partition reports, original
collection origin/finish and consumption. `sources` must separately contain every
exact current SourceSnapshot after ACL checks; grants, permissions and private
destinations remain mandatory. Refresh must revalidate current mapping, identities,
configuration and grant pins. Omission or stale publication denies. Archived JSON,
input-mode strings, event.raw, model output and constructing a request grant nothing.

```python
from ostag.briefs.meeting_service import DurableMeetingRequest

request = DurableMeetingRequest.from_current_collection(
    frozen_scope, history=trusted_history, chats=trusted_chat_evidence,
    first_claimed_at=original_collection_origin,
    collection_finished_at=original_collection_finish,
    collection_calls=trusted_total_calls, collection_bytes=trusted_total_bytes,
    chat_raw_content=trusted_original_chat_contents_by_source_id,
    confirmed_purpose=confirmed_purpose,
    confirmed_scope_hash=confirmed_scope_hash,
)
# Trusted host independently publishes request + exact SourceSnapshots and policy.
# It must not publish a request by trusting a caller-supplied archive.
service = authority.create_service(
    store=store, transport=explicit_read_only_transport, config=pinned_config,
    retention=coordinator.guard(store), lifecycle=lifecycle,
)
meetings = service.meeting_application(policy=original_retention_policy)
accepted = meetings.submit(authenticated_requester, stable_event_id, request)
# Execute with the existing BriefWorker(service), under the same lifecycle owner.
status = meetings.status(authenticated_requester, accepted.job_id)
markdown = meetings.private_export(authenticated_requester, accepted.job_id)
# Existing requester-scoped durable cancellation:
# meetings.cancel(authenticated_requester, accepted.job_id)
```

`submit` returns BriefStatus (`queued`, existing state, `conflict`, `timeout`,
`needs_input`, or `denied`). Status returns durable state, input completeness,
coverage gaps, sanitized failure cause and mandatory semantic-review flag.
Status/export raise a sanitized contract error on current authorization failure.
Export returns inert Markdown to the authenticated requester; it never sends,
marks an outbox sent, or creates a human receipt. Revision/provenance are committed
atomically by the existing worker. Suggestions never constitute content adoption,
sharing permission or business approval.

The existing read-only model adapter is explicitly injected; no default model or
Backend.run bridge is installed. Canonical raw GitHub hashes remain distinct from
display-excerpt hashes. Feishu hashes bind explicitly supplied `chat_raw_content` by source ID when the
original content differs from its display excerpt; that raw content is persisted
and independently owned by current authority. Omitting it is valid only when the
existing source hash already matches the exact excerpt. No hashes are rewritten.
Full provider-record reconstruction remains outside this ingress. Source kinds
are preserved. Code constructs citations; untrusted text is literal
Markdown. At most three priority decision topics include suggested actions and
five-minute allocations, plus ten minutes for review and recording decisions.

A10's public registration currently decodes manual jobs only. The meeting adapter
uses the same coordinator, ledger, target identity pins, source/grant tombstones
and expiry policy. Registration happens before model dispatch and after primary
completion, before status/export exposes a revision. Failure between transactions
leaves a durable primary result inaccessible by revision until registration with
the original policy succeeds. Status/export can repair registration idempotently;
they never repeat generation. The worker reports `needs_input` when its final
registration/authorization fails. No cross-database atomicity is claimed. Raw
BriefStore access is an internal trusted interface, not an authorized preview API.

Original collection origin/finish, scope, confirmation, raw provenance, consumption
and configuration are preserved in versioned JSON. The worker separately persists
its own first claim/deadline. Generation uses the earlier of its persisted deadline
and the original origin plus the lower meeting/config timeout. Collection must
finish within the frozen ceiling (at most 120 seconds with the profile's generation
reserve inside 180); queue delay after precollection consumes remaining time.
`HistoryResult.collection_seconds` is a profile diagnostic, not elapsed-time
authority. Explicit total collection calls/bytes include GitHub and Feishu reads
and must fit the same global caps; incomplete accounting cannot be admitted.
These counters are frozen host evidence, never a resume allowance.
A new event ID cannot create another job for the same frozen scope, even after
retirement: the existing intake hash remains an identity tombstone. An expired
claimed meeting records `recovery_required`; no implicit retry/resume is granted.

Explicitly trusted complete partition evidence can yield `input_complete=true`.
All selected partitions must be reported; missing/partial/failed reports or other
collection gaps keep coverage partial. Semantic review is never waived. The
existing live-list collector still produces partial coverage. The complete
synthetic case is not live history reconstruction or live acceptance.

### Runnable synthetic durable example

This uses physical temporary SQLite, the real BriefWorker, and the existing
OpenAI read-only adapter with only its HTTP boundary mocked. It closes/reopens the
service before status/export and cleans up afterwards. It needs no credentials,
real messages, calendar/table writes or model traffic.

```bash
PYTHONPATH=tests .venv/bin/python - <<'PY'
from pathlib import Path
from tempfile import TemporaryDirectory
from ostag.briefs.worker import BriefWorker
from test_meeting_service import current_request, runtime

with TemporaryDirectory(prefix="meeting-durable-synthetic-") as directory:
    path = Path(directory)
    local = runtime(path, current_request())  # two repos + Feishu, honest gaps
    worker = BriefWorker(local.service)
    try:
        accepted = local.app.submit(local.scope, "synthetic-meeting", local.request)
        assert accepted.status == "queued"
        assert worker.run_once().result(5) == "draft_ready"
    finally:
        worker.stop()
        local.service.close()
    reopened = runtime(path, local.request, clock=local.clock)
    try:
        status = reopened.app.status(local.scope, accepted.job_id)
        assert status["state"] == "draft_ready" and not status["input_complete"]
        assert not status["meeting_ready"] and status["semantic_review_required"]
        print(status)
        print(reopened.app.private_export(local.scope, accepted.job_id))
    finally:
        reopened.service.close()
PY
```

The fixed local verifier is:

```bash
.venv/bin/pytest -q tests/test_meeting_service.py tests/test_meeting_runtime_journey.py \
  tests/test_brief_service.py tests/test_brief_worker.py tests/test_brief_store.py \
  tests/test_brief_authority*.py tests/test_brief_retention.py tests/test_brief_revocation.py \
  tests/test_brief_recovery.py tests/test_brief_generation.py tests/test_brief_validation.py \
  tests/test_brief_grants.py tests/test_brief_lifecycle*.py tests/test_brief_flags.py
```

The new durable Markdown test requires Pandoc and fails rather than skipping if
it is absent. Full M02–M05 still require complete historical reconstruction,
approved live collection and cursor/resume accounting, prior-head/current
deployment provenance, semantic conflict/commitment evaluation, live private-chat
delivery and sharing ACL checks. A11 production ownership/D02 activation are not
cancelled. D01/D07/M07 require actual model/platform/user evidence, including the
held-out scenarios and repeated semantic cases. No fixture supplies semantic
scores, provider acceptance or human receipts. The actualClaudeOpus5 independent
audit remains the outside harness's responsibility; no nested reviewer was run.
