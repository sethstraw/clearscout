# Prompt-quality evaluation harness

This sits beside `evals/`, the scripted-provider case regression that measures
the program. It answers a different question: given synthetic postings and a
synthetic CV, does the current prompt suite answer the way independent, blinded
reviewer agents labeled each case, or clear the shipped verifiers? `METHODOLOGY.md` says what each mode is evidence of,
and what it is not.

The harness is a small in-repo runner with no third-party evaluation framework.

Five families are measured: `match`, `relevance_review`, `draft`, `cover`, and
`critic`. Each has a live task that spends; all but `relevance_review` also have
a scripted task that runs offline in the suite. `tasks/` is the list.

`runner.py` loads a task module from `tasks/`, iterates its dataset, and writes a
JSONL log to the gitignored `reports/eval/`. Each task names its dataset, the
product question it answers from `product_questions.py`, a solver from
`solvers/`, and a scorer from `scorers/`. `selection.py` selects samples
deterministically and projects the minimal dataset. `labeling.py` and
`review_resolution.py` bind the blinded reviewer passes and their adjudication
to hashes, and `prompts/` holds the reviewer's labeling instructions.
`build_calibration.py` and `diff.py` back the `scripts/eval.sh` subcommands of
the same names, and `cost_estimate.py` gives the estimate a `--live` run is
approved against.

Tracked synthetic inputs live outside this directory: postings and documents in
`evals/fixtures/`, master CVs in `evals/cvs/`, and the reviewer-labeled scoring
sets in `evals/golden/`.

`evals/fixtures/relevance_review_corpus.jsonl` is the input half of the
relevance-review set: synthetic postings, with no labels, spans, or reviewer
conclusions. Its loader checks the shipped synthetic CV hash and derives
citation IDs from posting text. `tasks/relevance_live_task.py` scores it against
the labels in `evals/golden/relevance_review.jsonl`.

## Modes

- The `match_task.py` replay task requires an explicit private calibration
  snapshot via `--dataset /private/path/to/frozen-calibration.jsonl`. It
  measures whether the stored classification changed. It does not measure prompt
  quality or independent-label accuracy.
- `scripts/eval.sh run evals/inspect/tasks/match_scripted_task.py` runs the
  shipped match stage against a canned in-process provider. It catches template
  and vocabulary regressions.
- The live solver runs the shipped match stage against the real provider:

  ```bash
  scripts/eval.sh --live evals/inspect/tasks/match_live_task.py \
    --data-root <root> --limit <manifest-cases> --split development --provider <provider> \
    --i-approve 0.56 --output-dir <private-path> --selection-manifest <private-path> \
    --selection-manifest-sha256 <semantic-sha256> \
    --selection-manifest-bytes-sha256 <exact-bytes-sha256>
  ```

  `--limit` must equal the manifest's case count. It is the only mode that
  spends money and the only one that tests prompt-quality regressions. The output directory must be outside the public
  repository.

  The log records the requested model and effort and the exact dataset and manifest
  identities. It records the immutable match-prompt snapshot and fingerprint,
  the settings identity, the split, and the stopping limit.
  Each result also records the requested and served model, token usage,
  per-call and cumulative priced spend, latency, searches, and service tier. It
  stores neither the prompt nor the raw provider response.

  Completed paid rows are fsynced to a mode-0600 `.partial.jsonl` sidecar. They
  are promoted to the final log only when the run completes. A stopping-limit
  refusal keeps its receipt.

## The document families

`draft`, `cover`, and `critic` share one dataset shape. A case names a tracked
synthetic CV under `evals/cvs`, a posting, and the classification each
requirement gets, and readable `metadata.evidence` aliases stand in for entry
identifiers, which are content hashes. Matching runs through the shipped stage
against a canned provider, so the plan the later stages receive is the one
production selection builds.

Scoring reads the shipped verifiers and nothing else. `draft` scores attempt one
through `verify.check`, `cover` scores the letter through `verify.check_letter`,
and `critic` records the verdict beside the outcome `verify.critic_finding`
assigns it under the shipped floors. Its `solver_output.draws` keeps every
sampled verdict in full, in draw order, so what each draw doubted, flagged, and
advised stays readable beside the merged one.

Offline, each case declares in `expect` the checks its planted defect must fail,
so a verifier that stops catching a defect fails the task rather than passing
quietly. A case may also declare the checks that must warn. The critic cases
declare the outcome the shipped floors give a canned verdict.

```bash
scripts/eval.sh run evals/inspect/tasks/draft_scripted_task.py
scripts/eval.sh run evals/inspect/tasks/cover_scripted_task.py
scripts/eval.sh run evals/inspect/tasks/critic_scripted_task.py
```

Live, only the family's own stage is paid; the critic reviews a document
assembled offline from its case's canned resume draft or cover letter, so its
result belongs to the critic rather than to a fresh draft. Each live task module declares its own
`STAGE`, which the shell reads to estimate the stage the run then records, so the
estimate a run was approved against cannot name a different stage. These datasets
are tracked public fixtures rather than a private reviewed set, so they take no
selection manifest:

```bash
scripts/eval.sh --live evals/inspect/tasks/draft_live_task.py \
  --data-root <root> --limit 6 --split development --provider <provider> \
  --i-approve 0.20 --output-dir <private-path>
```

Swap in `cover_live_task.py` or `critic_live_task.py` with that family's case
count. The log header names which checks failed and which warned, with counts,
and, for the critic, how the shipped outcomes were distributed.

`--model <id>` and `--effort <level>` replace the shipped model or effort on the
task's own stage only, alone or together. An effort the model does not accept
is refused before any call, the estimate reads only history at that model and
effort, and the run's settings hash changes with either.

## Calibration workflow

```bash
scripts/eval.sh build-calibration \
  --data-root <private-data-root> \
  --output <private-path-outside-the-public-repository>
# Writes private calibration evidence, not gold labels.
```

Turning calibration into a scoring dataset takes two isolated reviewer-agent
passes and a separate blinded adjudicator-agent pass. Keep both original
reviews, and record their agent provenance in the private review records.
`METHODOLOGY.md` has the calibration procedure and the limits of small samples.

## Diff two logs

```bash
scripts/eval.sh diff reports/eval/<baseline>.jsonl reports/eval/<candidate>.jsonl
```
