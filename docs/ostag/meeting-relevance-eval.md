# Offline meeting relevance and duplicate evaluation (OT-2)

This is the first S8 engineering slice selected by the user on 2026-09-23.
It is a standalone offline evaluator, not a change to `meeting collect`, the
meeting service, routing, permissions, approvals, or production. S9 records an
independent Claude CLI Opus 5.5 review and the disposition of its findings.

## Run locally

```sh
uv sync --locked --extra dev
uv run python -m ostag.briefs.meeting_relevance_eval < examples/meeting-relevance-eval.json
uv run python -m ostag.briefs.meeting_relevance_eval --snapshot < examples/meeting-relevance-eval.json
uv run python -m ostag.briefs.meeting_relevance_eval --baseline < examples/meeting-relevance-eval.json
```

The default command emits a JSON report. `--snapshot` emits the label-free inputs;
`--baseline` emits the baseline predictions artifact. Only stdin/stdout/stderr are
used. There is no settings load, network, backend, database, delivery, live mode,
or built-in file writer. Invalid input returns exit 2 and a static error; successful
evaluation returns exit 0 even when the baseline performs poorly. Neither exit
code is a product acceptance decision.

Input is an envelope with exactly `suite` and `candidate`. Set `candidate` to
null to run only the baseline. To compare stored offline predictions, replace
null with an artifact in the same shape as `--baseline`, using a declared method
name and the exact `input_sha256`. No model is invoked to produce that artifact;
its method name is not authenticated provider identity. Missing case rows count
as **not run**, whereas an explicit `abstain` counts as **abstained**. Unknown or
duplicate case IDs, extra fields, altered input bindings, and invalid labels fail
the entire evaluation instead of disappearing from the denominator.

## Versioned contract

- `meeting-relevance-suite/1`: synthetic cases with id, family, split, topic,
  literal terms, exactly two materials (id, nullable origin, text), and gold labels.
- `meeting-relevance-input/1`: the same inputs with gold labels removed, copied
  before either method is evaluated.
- `meeting-relevance-predictions/1`: method, input hash, and rows with id,
  two relevance labels and one pair duplicate label. Material order is bound by
  the input hash. IDs are local to the enclosing case; no cross-case identity join.
- `meeting-relevance-report/1`: input, corpus and prediction hashes; separate
  development/holdout accounting; per-case source text hashes and decisions.

Relevance reference labels are `relevant`, `irrelevant`, `uncertain`; duplicate
references are `duplicate`, `distinct`, `uncertain`. Predictions replace
`uncertain` with `abstain`. Gold labels are authored synthetic expectations, not
independent human judgments. Neither relevance nor duplication establishes claim
truth, factual support, responsibility, a deadline, or execution authority.

Input is bounded to 1 MiB, 100 cases, two materials per case, 4,096 UTF-8 bytes per
material and 16 literal terms per topic. Duplicate JSON keys, non-finite numbers,
lone surrogates and malformed structures are rejected. Hashing is SHA-256 over
UTF-8 Python JSON with sorted keys, no whitespace and `ensure_ascii=False`; it is
versioned for this local evaluator and **does not claim RFC 8785 compatibility**.
Source text hashes use the original UTF-8 bytes, not normalized text.

## Baseline and accounting

`literal-v1` reports relevance on a case-folded, whitespace-normalized substring
match of a supplied topic term. Otherwise it abstains; absence of a keyword is
not proof of irrelevance. It marks a duplicate only when both materials have the
same non-null declared origin and equal normalized text. All other pairs abstain,
including paraphrases and identical text with unknown or different origins.
Nothing is removed, suppressed, reordered or written back to the ordinary meeting
output. Literal matching deliberately has false positives and semantic misses.

The bundled corpus contains 12 Chinese synthetic cases: six development and six
holdout. It includes exact copies, paraphrases, additional facts, contradictions,
keyword collisions, unknown origins, different events, missing context and
instruction-like material. Family IDs, non-null origins and normalized identical
texts may not cross the split boundary. This catches declared/exact leakage only;
it cannot detect disguised paraphrases or prove independent event families.

The holdout labels are published engineering fixtures, **not a sealed or blinded
evaluation set**. There is no human pain-point baseline, timing experiment or Jev
benefit claim. A real pilot needs independently labeled, permitted samples and a
predeclared rubric/split before comparing any real model output. Private meeting
packs are not imported by this slice. A caller's `provenance: synthetic` is a
declaration, not an authorization receipt.

Metrics retain integer numerator/denominator pairs; zero denominators remain
zero (undefined), not fabricated 0%/100% scores:

| Metric | Denominator |
|---|---|
| Precision | Positive predictions with a determinate reference |
| Recall | All positive references, including missing/abstained predictions |
| Decision coverage | All determinate references |
| Accuracy on decided | Only determinate references with decisive predictions |

`false_positive`/`false_negative` count decisive errors on determinate references.
Abstentions and missing rows are separately counted and remain in recall/coverage
denominators. Predictions against uncertain references are unscored and reported
as `decisive_on_uncertain`; they cannot improve precision or accuracy. Counts for
missing/abstained and uncertain references can overlap. Development and holdout
are reported separately, with no pooled headline score.

Every report says `inconclusive_synthetic_only`, `acceptance_evaluated=false`, and
`live_model_evaluated=false`. No cost or time-saving estimates are invented.
Remove this module to roll back; no database or production migration is needed.
