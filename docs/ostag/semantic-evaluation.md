# A1 semantic evaluation evidence accounting

`python -m ostag.briefs.evaluation < evaluation-input.json > report.json`

This offline tool summarizes operator-supplied evidence. It does not call a model,
judge semantic truth, authenticate receipts, approve deployment, or complete A12.
CLI exit 0 means a report was produced, including incomplete or failing reports;
exit 2 means invalid input. Input is limited to 4 MiB, 10,000 trials, 100 declared
groups and 100 assertions per case. Errors do not echo private input.

## Prepare and freeze before model calls

The input object has exactly `suite`, `declared_groups`, and `trials`.

- `suite`: `version: brief-eval-suite-v1` and `cases`. Exactly 24 unique case IDs,
  18 development and six holdout cases; 16 A1 cases with at least four holdouts.
  Each case has `id`, `phase` (`A1`/`A2`), `split` (`development`/`holdout`), boolean
  `critical`, and nonempty `assertions` (`id`, human-readable `rubric`). At least
  one A1 case is critical. Counts alone do not prove corpus quality/independence.
- `declared_groups`: nonempty list of `expected_model`, `parameters_sha256`,
  `parameters_label`. Freeze intended production model, prompt, validation and
  generation parameters before executing. Separate parameter versions; never
  pool their coverage. Empty groups appear with every required slot missing.
  Conflicting label/hash associations are exposed in each affected group.
- Compute `suite_sha256` with `suite_digest(suite)`. It covers the full canonical
  JSON including rubrics. Every trial must match. Hashes are opaque evidence
  references, not signatures or proof that referenced files exist.

Keep holdout inputs/expected answers under independent custody. Do not tune on
holdout results and continue to call those same inputs independent holdouts.
The synthetic test fixture is NOT the real 24-scenario corpus.

## Collect every attempt and adjudicate separately

Persist every start before IO. Reconcile a durable collector's original start
record into a terminal record for the report; retain the append-only lifecycle
log outside the report. Unresolved calls export as `started`. The summarizer
accepts one current record per attempt and does not itself implement this ledger.

Each trial has exactly:

```text
attempt_id, case_id, repeat, suite_sha256, expected_model, actual_model,
parameters_sha256, receipt_sha256, outcome, cost_micros, judgments
```

`attempt_id` is globally unique. `(expected_model, parameters_sha256, case_id,
repeat)` is unique. Number independent attempts from one; critical cases require
slots 1, 2, 3 and other A1 cases require slot 1. Retries get NEW numbers and IDs;
never replace the previous failed call. Later attempts count too, including all
failures, abstentions, identity mismatches and incomplete judgments.

`actual_model` comes from trusted transport metadata, never generated text or a
fallback to the expected pin. Missing identity is null and remains unverified.
`receipt_sha256` references the collector's complete evidence or start receipt.
`cost_micros` is an exact nonnegative integer or null for unknown spend; unknown
is reported separately from the sum of known costs. All SHA256 values are 64
lowercase hexadecimal characters. `outcome` is completed/error/abstained/started.

Only completed attempts may have judgments. Each judgment has `assertion_id`,
`verdict` (pass/fail/pending), `evidence` (reference strings), and `reviewer_id`.
Pass and fail both require a nonempty evidence list; missing judgments become
pending, unknown/duplicate assertion IDs are rejected. Reviewers must examine
actual claims against source evidence, retaining reasons in the referenced
adjudication artifact. Reviewer identity does not prove independence.

## Interpret the report

The report covers A1 only. A2 attempts are explicitly listed as excluded.
Every declared group reports missing slots, all per-attempt outcomes and reviewer
IDs, numerator/denominator pass counts, known cost and unknown-cost count.
`evidence_complete` means required slots exist and all supplied attempts have
resolved identity and judgments with no unresolved start. A recorded transport
error/abstention may lack model identity; that absence is counted separately and
does not block evidence completeness. A known conflicting model always does, as
does missing identity on a completed attempt. It is NOT a passing
quality threshold: a fully adjudicated failure may have complete evidence.
`passed_trials` additionally requires completed outcome, matching identity, and
all case assertions passed. Missing costs remain independently visible even
when semantic evidence is complete. Consumers must not equate either field with
production readiness. `acceptance_evaluated` is always false; the machine-readable
`limits` explicitly states that this is not a production gate.

The tool cannot detect a collector omitting calls, fabricated metadata, false
reviewer identities or false evidence references. Preserve raw receipts and
cross-check the start ledger independently. Adoption and user revision time are
separate user observations; `user_adoption` remains null here, never inferred.

## Remaining A12 work and release sequence

1. Independently create and freeze actual 24 input/expected-assertion scenarios;
   the current deliverable validates their shape only.
2. Run the A1 16-case subset through the intended production transport, including
   three real repetitions for each critical case. Record errors, abstentions,
   model/parameter pins and all known/unknown costs.
3. Independently adjudicate claims with source evidence, then run this report;
   separately collect adoption and revision time.
4. A12 remains partial until all original criteria are evidenced. D01 isolation
   and D02 trusted installation/recovery precede D03 release/live acceptance.

This module is offline tooling and changes no service flags, database schema,
provider behavior, container launcher or production host installation.

Identifiers are compared exactly; collectors must preserve their code points.
