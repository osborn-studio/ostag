# Local meeting collection draft

Run from the repository using the existing Python 3.11 environment:

```sh
/Volumes/AIMATE_WORK/Developer/CodeX/OStag-company-integration/.venv/bin/python -m ostag.main meeting collect \
  --repo osborn-studio/ostag \
  --start 2026-09-13T07:00:00Z --end 2026-09-20T07:00:00Z \
  --timezone America/Los_Angeles --goal '项目周会' \
  --format markdown
```

Add `--chat-id` with one or two explicitly selected `oc_...` chat IDs to include
Feishu messages. The command never invents IDs or enumerates chats. Without chat
scope it produces useful GitHub-only output with `feishu_scope_missing` and exit
3. Use `--format json` for machine-readable output. Everything goes to stdout;
only caller redirection saves the pack. Invalid arguments produce a generic
stdout error and exit 2, without provider IO. Existing `meeting preview` stays
separate and synthetic.

The window is half-open `[start,end)`, requires explicit UTC timestamps and an
IANA display timezone, must already have ended, and cannot exceed 31 days. The
repo, goal and up to two distinct chat IDs are validated before any CLI call.
Leading root options such as `--config`, `--backend`, `--doctor` and `--live` do
not activate service execution for this command.

## Credential and privacy boundary

This is **private local output under the operator's already authenticated CLI
accounts**. It is not a service authenticating arbitrary requesters or an issued
grant. There is no requester namespace, simulated grant, model call, settings
load, business database, server route, provider write, or production activation.
The code does not extract credentials: `gh` and `lark-cli` use their usual local
configuration. Do not publish redirected private packs without reviewing them.

GitHub user ID/login and Feishu user `open_id` are observed separately; they are
never claimed to identify the same person. GitHub `user` and repository ID,
exact name, default branch and pull permission are checked before and after
collection. Feishu requires the CLI success envelope with `ok:true` and
`identity:'user'`, and checks `open_id` again after collecting selected chats.
A failed or changed final identity/access check withholds that provider's
content. Individual chat transport/schema failures retain other successful chat
content; a resource mismatch withholds provider content.

Only constructed GET calls are used:

- `gh api --hostname github.com --method GET <relative endpoint with query>`
- `lark-cli api GET <allowlisted path> --as user --params '<JSON>' --format json`

There are no retries, automatic pagination, response URL/Link following, contact
lookups, reaction enrichment, downloads, or writes. Provider errors are static
categories, never raw stderr or API error bodies.

If Feishu reports `keychain_unavailable`, the installed CLI could not access its
normal credential store (the parent's preflight reported “keychain Get failed:
keychain not initialized”). Use the operator's normal terminal/session to resolve
credential access according to the installed CLI's instructions. This command
does **not** downgrade the CLI, reinitialize the keychain, print tokens, or treat
an auth failure as an empty chat. GitHub evidence can still be returned with exit
3. The builder did not perform live credential troubleshooting.

## Evidence and limits

The five GitHub partitions are **pulls, issues, issue_comments, commits,
releases**. Issues exclude PR objects. PR creation, merge and current updated
snapshots have separate event timestamps. Issue creation/closure/current updated
snapshots and comment creation/current updated snapshots are likewise distinct.
A PR created before the window and merged inside it can appear even if its
current update timestamp is after the window. A comment created inside the
window but edited later retains the creation event and a newer-content caveat.

### SPEC boundary addendum

GitHub issues, issue comments and commits use a query `since` equal to the
window start rounded down to a whole UTC second, minus one second. This overlap
preserves events exactly at the inclusive start under strict-after provider
filtering, including fractional UTC window boundaries. Every returned event
still passes the exact local `[start,end)` filter; overlap records before start
and events exactly at end are excluded and counted as `outside_window`.

Commit collection pins the observed default-branch tip SHA. Inclusion uses the
committer timestamp, not author time. This is not the first time a commit landed
on that branch. Unpushed local commits are never GitHub activity. Releases are
publication observations, **not deployment evidence**; release records do not
provide an `updated_at` timestamp, so `provider_updated_at` is null. It is also
null for commits, whose timestamp is the event timestamp, not an update time.
All PR check and deployment states remain `unknown`. Checks, deployment
histories, timelines and reviews are not collected; this is not full M02/M03.

Feishu pagination uses explicit selected chat IDs, epoch time bounds and
`ByCreateTimeAsc`; records are filtered again against the exact local half-open
window. Text is an observed chat claim. Edited, deleted, media and thread records
carry caveats/gaps; media and threads are never expanded or claimed complete.
Only an explicit exact selected-repository PR URL can associate with an actually
collected PR. Bare `#12` and unresolved explicit references produce proposed
questions, not semantic joins, decisions, assignees or deadlines.

Each invocation is a new bounded observation:

| Bound | Maximum |
| --- | --- |
| Overall monotonic deadline | 120 seconds |
| Each CLI command | 15 seconds |
| Each partition | 4 explicit pages of up to 50 records |
| Included normalized facts | 500 |
| Individual serialized record | 65,536 bytes (ASCII JSON representation) |
| Each command's stdout + stderr | 1 MiB |
| Accepted command responses | 16 MiB |

The GitHub budget reserves two final verification calls and time for Feishu;
Feishu reserves its final user check. The transport reserves response capacity
for final identity checks. Both pipes stream under a shared cap. Each subprocess
starts its own session with stdin disabled; its process group is killed and its
leader reaped on timeout, overflow, cancellation, error, and completion, including
when descendants retain pipes after the leader exits.

Coverage reports received/included/excluded pages and records, facts, exhaustion,
withholding and reasons. Out-of-window records and PRs in the issues partition
are expected exclusions. Invalid/oversized records, duplicate drift, repeated
cursors, pagination schema failures and caps are visible gaps. A page counts as
included when its bounded record list was processed, even if some records were
excluded. Exhaustion means only the endpoint pagination ended during this mutable
read; it is not an atomic historical snapshot or proof of all activity. A new
invocation does not resume a durable checkpoint.

## Output contract

JSON top-level fields include `status`, `synthetic`, `meeting_ready`,
`semantic_review_required`, `window`, `facts`, `sources`, `coverage`, `gaps`,
`agenda`, `questions`, `identities`, `observation`, `limits` and `provenance`.

- `synthetic:false` describes the normal authenticated CLI transport path; it
  asserts neither semantic truth nor an authenticated server issuer.
- `status:draft_ready` / exit **0** means technically exhausted scoped collection
  with no reported gaps. It does not certify meeting quality.
- `status:needs_input` / exit **3** means gaps or useful partial output; inspect
  coverage before interpreting it. Empty/failed records never prove no progress.
- `meeting_ready:false` and `semantic_review_required:true` are unconditional.
  This command does not complete MC24, M05 or D07.

Each source has a stable `source_id`, provider resource ID, `content`,
`content_sha256`, `record_sha256`, observation/update times, typed provenance and
caveats. For GitHub, `content` is the untouched body/message, prefixed with the
untouched title/name and a newline for PRs, issues and releases. For Feishu,
`content` is the original `body.content` JSON string, and `text` is its decoded
text when understood. Sender IDs and thread markers stay provider-specific.
`content_sha256` hashes the exact UTF-8 `content`; `record_sha256` hashes the
record's canonical sorted ASCII JSON representation. No full raw provider error
or credentials are output. Facts and proposed agenda entries have `source_ids`
resolving to sources. A gap-only agenda entry has an empty array plus explicit
`gap_references`.

Markdown puts coverage gaps first, followed by scope/identities, limitations,
coverage, relevant changes, proposed agenda/questions and sources. Untrusted text
is in fenced code blocks whose fence exceeds every input backtick run; only
constructed repository/number/SHA source citations are clickable. Chinese and
English, adjacent URLs, versions, decimals, file names, quotes, underscores,
`#12` and line breaks remain readable. Control/bidi characters are visibly
projected to `\u...`; nothing is silently truncated. JSON uses Unicode escapes
while preserving decoded strings. Hashes cover original content, not the display
projection.

## Verification status

Builder validation uses scripted GitHub and Feishu fixtures and temporary fake
provider executables, plus real local subprocess timeout/output/descendant tests.
Boundary regression fixtures apply strict-after provider filtering before
normalization for issues, issue comments and commits, with whole-second and
fractional starts, and verify inclusion at start and exclusion at end.
No live private GitHub/Feishu fetch, credential access, baseline full-suite rerun,
install, model call or commit was performed by the builder. The parent owns the
final targeted checks, Ruff, one full-suite run, authenticated GitHub smoke run,
Fable independent review and fixed-verifier approval before osstack commits.
Live provider behavior and independent review are **pending**, not claimed passed.
