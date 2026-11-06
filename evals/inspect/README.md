# Prompt-quality evaluation harness

This sits beside `evals/`, the scripted-provider case regression that measures
the program. It answers a different question: given synthetic postings and a
synthetic CV, does the current prompt suite answer the way independent, blinded
reviewer agents labeled each case, or clear the shipped verifiers? `METHODOLOGY.md` says what each mode is evidence of,
and what it is not.

The harness is a small in-repo runner with no third-party evaluation framework.

Five families are measured: `match`, `relevance_review`, `draft`, `cover`, and
`critic`. `match` adds a whole-posting `match_job` task, and `draft` and
`cover` add `draft_redraft` and `cover_redraft`, which score the attempt kept
after a redraft. Beside them sit two CV intake tasks (`structure`, and `vision`
for the ingest stage), two jobspec tasks (the boundary task described below and
requirement extraction), and three search tasks: `search_vocabulary`,
`posting_recovery`, and `guided_campaign`. Each has a live task that spends.
`match`, `draft`, `cover`, and `critic` also have a scripted task that runs
offline in the suite; the others do not. `tasks/` is the list.

`posting_recovery` and `guided_campaign` read the real web through hosted
search, so their real cases are private. `--dataset <private-path>` on a
`--live` run replaces the task's tracked synthetic fixture, and the log records
that file's hash.

`runner.py` loads a task module from `tasks/`, iterates its dataset, and writes a
JSONL log to the gitignored `reports/eval/`. Each task names its dataset, the
product question it answers from `product_questions.py`, a solver from
`solvers/`, and a scorer from `scorers/`. `selection.py` selects samples
deterministically and projects the minimal dataset. `labeling.py` and
`review_resolution.py` bind the blinded reviewer passes and their adjudication
to hashes, and `prompts/` holds the reviewer's labeling instructions.
`build_calibration.py` and `diff.py` back the `scripts/eval.sh` subcommands of
the same names, and `cost_estimate.py` gives the estimate a `--live` run is
approved against. A live log's header carries a `usage` rollup of every
provider call (tokens, cache share overall and per trial, latency, and cost per
sample and per passed sample), which the runner prints as a `usage:` line and
`diff` prints for each log.

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
    --i-approve <usd> --output-dir <private-path> --selection-manifest <private-path> \
    --selection-manifest-sha256 <semantic-sha256> \
    --selection-manifest-bytes-sha256 <exact-bytes-sha256>
  ```

  `evals/inspect/tasks/match_job_live_task.py` takes the same flags with a job
  manifest and `--limit 1`; its one row is a whole posting, and the run header
  counts each requirement beside the row.

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

A live run needs a provider API key in the environment variable ClearScout
reads, or stored with `clearscout auth set`; there is no key flag. `--data-root`
names the workspace whose recorded calls price the estimate and whose master CV
`--cv-version` reads. For `relevance_review`, `--render-only <private-dir>`
writes every batch a run would send and calls no provider. The usage block of
`scripts/eval.sh` lists the rest.

## Smoke stage and trials

A `--live` run of more than one call first smokes the selected samples through
the same solver and scorer, in order, until one makes a paid call. A case whose
output carries no provider measurement is scored, kept as its first row, and
the walk moves on (`smoke: unpaid case=<id>`). The smoke fails, and the run
stops, when the solver raises, when the output lacks a field the scorer
declares in its `required_fields`, when the scorer raises or returns no value,
when a paid measurement carries no dollars or no prompt usage, when that one
call cost more than the approval, or when no selected case made a paid call.
A failed smoke output is kept in the partial receipt. A passing call prints
`smoke: ok` with its projected cost and is reused as that sample's first trial,
so it is not paid twice; a projection over the approval only warns.
`--no-smoke` skips the stage, and batching solvers skip it. `_smoke` in
`runner.py` is the rule.

`--trials N` runs every selected sample N times per prompt arm against one
approval. With N above one the solver sets a repeat hint, and the adapters mark
the task block for the provider cache, so later trials of a sample read it
from cache; the log's `usage` rollup reports `cache_share_by_trial`.

Each task names its scorer from `scorers/`; the module list there is the set
of scorers.

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
  --data-root <root> --limit 14 --split development --provider <provider> \
  --i-approve <usd> --output-dir <private-path>
```

Swap in another live task, such as `cover_live_task.py`, `critic_live_task.py`,
`draft_redraft_live_task.py`, `structure_live_task.py`, or
`vision_live_task.py`, with that task's case count. The log header names which checks failed and which warned, with counts,
and, for the critic, how the shipped outcomes were distributed.

`--model <id>` and `--effort <level>` replace the shipped model or effort on the
task's own stage only, alone or together. An effort the model does not accept
is refused before any call, the estimate reads only history at that model and
effort, and the run's settings hash changes with either.

## The jobspec boundary task

`tasks/jobspec_boundary_live_task.py` checks which stated boundary jobspec
extraction fires, since any fired boundary skips the posting. It sends eight
synthetic postings from `evals/fixtures/jobspec_boundary_cases.jsonl` against
the synthetic list in `evals/fixtures/jobspec_boundaries.json`. Each row names
the expected boundary number, or null, and why. The log header counts false
positives, false negatives, and wrong boundaries, and gives trials, hits, and
correct answers per case and arm. It has no scripted task. It takes `--trials`
and `--prompt-arm`, where each arm directory holds `jobspec_system.md` and
`jobspec_user.md`. Eight cases find defects; they do not measure a rate.

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

## Rescore a stored log

`scripts/eval.sh --rescore <task_file> <log.jsonl>` re-applies the task's current
scorer to the solver outputs a finished log stored and writes
`<log>.rescored.jsonl`, so a scorer-only change is measured without a paid run.
