# Closed pull requests in meeting preparation

Meeting preparation includes a `pr_closed_unmerged` fact when the observed PR is
closed, `merged_at` is explicitly null, and the reported `closed_at` falls in the
requested half-open interval `[start, end)`. A merged PR continues to produce a
merge fact, without a second unmerged-closure fact. Closure does not establish
merge, successful checks, deployment, or completion of the intended work.

GitHub exposes distinct nullable `closed_at` and `merged_at` fields in its
[pull request REST response](https://docs.github.com/en/rest/pulls/pulls).
The feature uses these reported fields, not `updated_at`, collection time,
comment text or model inference. Missing or malformed merge state cannot prove
that the PR was unmerged.

Both the bounded live `meeting collect` path and the frozen multi-repository
fact assembler include the new fact. Without a closure timestamp, live collection
preserves a `closure_time_unknown` gap; the frozen assembler retains an undated
observation with no temporal SourceRef. Neither invents a closure date.

This is the latest observed snapshot, not a reconstruction of repeated close,
reopen and merge events. Existing timeline coverage and its limitations remain
separate. Current text may have changed after the requested window. Existing
identity, source citations, budgets, semantic review and meeting-readiness gates
are unchanged; this increment does not complete all meeting acceptance criteria.
