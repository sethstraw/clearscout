# Evaluation methodology

The harness answers one named question at a time. It never treats a user's past
correction, a stored classification, or a model's output as the right answer.

The labels it scores against come from two reviewer agents labeling each sample
blind. Their disagreements are settled separately: by a third agent in the match
family, and by the maintainer in the relevance family.
That tells you whether the prompt agrees with a written rubric as those agents
apply it. It does not tell you whether a person would agree. Every result
carries that limit.

## Each mode answers a different question

- `replay` reads an explicit private calibration snapshot. It compares the
  frozen `current_classification` with the current database. It asks one thing:
  did ledger interpretation change? The stored value is not a gold label, and
  the solver never receives it.
- `scripted` runs the shipped stage against a canned in-process provider and a
  separate tracked contract fixture. It asks whether templates, schemas, all
  five classifications, and citation shapes still compose. It never receives
  the independently reviewed live labels.
- `--live` runs the shipped stage against the configured provider. It is the
  only mode that can measure a prompt-quality change, and it spends money.

The `draft`, `cover`, and `critic` families have a scripted and a live mode, and
no replay mode: nothing in the ledger stores a prior attempt to compare against.
Their scripted mode declares the checks each case's planted defect must fail, so
it measures the verifiers and the templates. Their live mode measures what the
real provider's first attempt does against the checks the product already
enforces.

A live run needs explicit operator approval for that run and its stated scope.
Approval does not carry over to the next run. Amounts are advisory estimates.
Crossing one, or receiving unknown usage, never stops an approved run. Usage and
any available cost estimate are recorded for every returned result.

## What the stored baselines cover, and what they do not

`evals/baselines/` holds one file per case, and every one of them records a
`scripted` run. Each entry measures that case's own canned answer put through
the shipped readers, checks, selection code and scorers. So the baselines are a
deterministic regression suite. They move when a reader, a check, a template or
a scorer changes, which is why a drifted reader fails a comparison instead of
reporting an incomparable result.

They do not move when model quality moves, because no model is in the loop. The
number of baseline files is not a measure of model coverage. Only `--live`
measures a prompt-quality change, and only for the family it runs.

No baseline is recorded against a real provider response. That is deliberate: a
recorded baseline would have to be bought again every time a prompt changed, and
the `--live` families already answer the question it would answer.
`test_every_baseline_records_a_scripted_run` in
`tests/durability/test_eval_methodology_claims.py` pins that, so a baseline
recorded some other way cannot arrive without this paragraph being revisited.

## Samples need an independent answer

A scoring set is built from examples labeled without seeing the prompt's output.
The frozen match file carries only what scoring needs: the input, the dimension,
the expected classification, and the expected strength. The frozen relevance
file carries the input, the expected recommendation, the four expected axes,
and the axes confidence.
The two blind reviews, the adjudication, the decisive evidence, and the digests
that bind them stay in the private calibration snapshot that produced the file.
`evals/inspect/review_resolution.py` defines those records, and
`evals/golden/README.md` lists the fields each frozen file carries.

A frozen row may also carry a `provenance` record: the rubric version, both
reviewer identities, which material fields the two reviews differed on, and who
adjudicated. A row with no such key is a row nobody has written that record for,
which `LabelProvenance` keeps distinct from a record whose `disagreements` list
is empty. The first says nothing was recorded; the second says two reviewers
were recorded differing on nothing.

Part of the match set is held out from day-to-day prompt work, so it can still
reveal overfitting. The split is frozen before any label exists: each candidate's
publication gate in the candidate pool names `development` or `holdout`, and the
pool digest binds it. Selection fills the development slots from development
candidates only and projects every QA-approved, labeled holdout candidate into
the frozen file with `split: holdout`; a selection manifest that holds nothing
out is refused. The runner serves a held-out row only to a run that names
`--split holdout`, so a run with no split, a development run, and an `--ids`
list naming a held-out row never read one. A paid match run names one split,
and its sample limit must equal that split's case count in the approved
manifest. A holdout run is a measurement: its result is never used to tune a
prompt or the rubric.

A case stays in the split it was frozen in, and so do its source and its
normalized requirement. A pool refuses a source or requirement that sits in both
splits. `select_cases` and `select_held_out` take the pool in force as
`previous_pool`, None only for a first pool, and refuse through
`CandidatePool.require_splits_kept` a pool that moves any of the three away from
the split that pool froze.

A selection key binds the candidate pool and the resolved labels of its own
split, never the other split's, so relabeling a held-out case leaves the order
of development cases unchanged. The pool digest still covers both splits, so
adding a candidate to either one changes every key in both.

A small dataset can expose a defect. It cannot support a precise population
claim. Publish counts beside rates and report disagreement. Keep rare failure
classes visible, even though a production stream would bury them.

Selection stratifies by label and by an input-defined challenge class, and
refuses a set that does not represent every class. `evals/inspect/selection.py`
holds that rule and `evals/inspect/review_resolution.py` holds the classes. The
class belongs to the calibration snapshot and is not carried into the frozen
scoring file, so a run's result cannot be broken down by it. `dimension` is the
axis the frozen file does carry, and it is `hard` or `behavioral`.

Samples are never selected on whether the evaluated model got them right. That
outcome is unknown before a blinded run. A class the set never contains is a
class the result says nothing about.

## How reviewer agents label

Two reviewer agents label the same frozen sample against the same written
rubric. Each runs in its own fresh context. Neither sees the other's review or
any current model output. Both originals are frozen.

Who settles a disagreement differs by family. In the match family a separate
adjudicator agent, in another fresh context, settles it, and the protocol
refuses an adjudication whose receipt names either reviewer's agent. In the
relevance family the maintainer settled the cases the two passes split over.
`LabelProvenance.adjudicator_kind` is the field that says which of the two a
record means.

`evals/inspect/review_resolution.py` carries the whole protocol for both
families: the family names the hash domain and the material decision, so one
family's judgment cannot validate as another's. What it does not yet cover is
preparation. The candidate pool, extraction QA, and reviewer packet in
`evals/inspect/labeling.py` are built around a requirement span, so only the
match family has a reviewer packet. A relevance review therefore records no
packet digest and binds its blind input to the frozen review context instead,
and the protocol refuses a review that names the other family's packet or
context.

Agreement is reported. It is not a reason to drop the hard cases. Each record
names the agent and the rubric version. These records are never presented as
reviews by people.

A receipt's runtime identity comes from the agent launch result, never from a
model name the orchestrating session types. `checked_runtime_identity` in
`evals/inspect/labeling.py` requires the launch result, refuses one whose
`agentId` is not the receipt's agent, refuses a claimed identity that differs
from its `resolvedModel`, and records that `resolvedModel` with its source
named. `OperationalReceipt` refuses an identity that does not end with that
source suffix. It checks the string, not a launch result, so a receipt
assembled by hand with the suffix typed in would pass. Only a receipt built
through `checked_runtime_identity` is tied to a launch result.

Reviewers do not see the provider, model, prompt version, or candidate ordering
until they have recorded defects and preference. Input, CV inventory, rules, and
stage settings stay fixed between candidates.

## How a relabel settles

The reviewer rubric, `evals/inspect/prompts/match_reviewer.md`, is versioned and
frozen. A review context records it with its `rubric_version`.
`FROZEN_RUBRIC_SHA256` in `evals/inspect/labeling.py` holds the digest of the
one text each registered version names, and `require_frozen_rubric` refuses a
context whose text differs from the text its version already froze, whether in
that registry or in an earlier round's context. A changed rubric is a new
version.

Each rubric edit made to fit one product rule moves other borderline labels
with it, so the rubric is not where a product miss is fixed. A contested case is
one whose right answer the rubric leaves open between named labels. When the
product misses on one, the fix is a ruling on that case. A ruling is a
`CaseRuling` in `evals/inspect/review_resolution.py`: the case id, at least two
accepted classification and strength pairs, and a short basis. The selection
manifest records the rulings, and `serialize_match_dataset` writes each ruled
case's row with its accepted pairs as two index-paired lists in place of the
single `expected_classification` and `expected_strength`, so the manifest's
`dataset_sha256` binds the ruling and a set applied to the file by hand fails
that digest. A ruling must name a selected case and accept its resolved label.
The private selection freezer takes the rulings as a file, an empty list for a
round with none.

Every accepted member is a rubric classification or strength, and the set is a
list of pairs, not their cross product: accepting `DIRECT`/`strong` and
`TRANSFERABLE`/`moderate` does not accept `DIRECT`/`moderate`.
`accepted_labels` reads a row's pairs and refuses one outside the rubric,
whether the row holds a list or a single value, and the runner checks every
loaded match row this way before it calls a solver. The scorer passes any accepted
classification, and compares the answered strength with the strengths paired
with that classification, still beside the pass mark. An `ASSERTED` or `GAP`
answer forces strength `none`, so it compares no strength and stays out of the
strength count. Rubric fixes that do need
making wait and land together, at most one relabel per prompt cycle.

A relabeling round is judged over the cases it shares with the round before it,
and sets no minimum count of shared cases. It is final when three things hold:

- the rubric version is unchanged;
- every shared uncontested label repeats, meaning the same classification and
  strength;
- the two reviewers' agreement on the shared uncontested items reaches
  Krippendorff's alpha of 0.8, measured on the classification and strength each
  recorded.

`round_finality` in `evals/inspect/review_resolution.py` judges a round and
reports each condition beside the verdict. It takes the selection manifest with
the candidate pool and extraction QA it selected from, and validates the manifest
against them and the current round's resolution set, so a manifest selected from
other labels, or whose rulings its bound dataset does not carry, is refused. A
shared case the manifest rules on is contested only while both rounds' labels
are pairs the ruling accepts; a label outside the ruling is judged like any
uncontested case, so it must repeat and counts toward agreement. It reports
alpha over every case the round reviewed beside the alpha that
decides. A case new to the round has no earlier label to repeat, so it is
reported as provisional and does not hold the round open; it becomes final when
a later round repeats its label. A case the previous round labeled and this one
dropped is reported. A round whose shared uncontested labels never vary has no
measurable agreement and is not final, and a round whose reviews or labels do
not bind to its own review context is refused.

## Rulings are calibration, not gold

A `requirement_rulings` row is a user's correction to an earlier prompt
judgment. Scoring against it as the expected label would reward the exact
output the user disagreed with. The row shows where the current classifier drew
complaints. It is not an independent answer.

`build-calibration` requires an explicit private output path outside the public
repository. Its output is calibration evidence, not labels. A stratified subset
becomes `golden/match.jsonl` only after two isolated reviewer-agent passes and a
separate blinded adjudication. Scoring reads only that frozen file.

Each match label is frozen against the synthetic inventory named by the
dataset's `cv_hash`. Reviewers assign `hard` or `behavioral`. The solver
receives that dimension, but not the expected label or any reviewer material.

- `DIRECT`, `TAILORING_ONLY`, and `TRANSFERABLE` are evidence-backed labels.
  Each cites a synthetic inventory entry.
- `ASSERTED` and `GAP` cite none.
- `ASSERTED` applies only to eligible status facts: authorization, citizenship,
  residency, or location.
- Each label also carries a strength: `strong`, `moderate`, or `weak` for an
  evidence-backed label, and `none` for `ASSERTED` and `GAP`. The two reviews
  must agree on it like any other material field.

The scorer compares strength only on a row whose classification matched, and
reports it beside the pass mark rather than in it, so a run's accuracy means the
same thing with or without strength labels. The run header states
`strength_agreed` out of `strength_compared`, counting only evidence-backed
labels: the product answers `none` for every `ASSERTED` and `GAP`, so those rows
cannot disagree.

The development set holds nineteen cases: a matrix of sixteen and a
participation stratum of three, each case filling exactly one slot. `DIRECT`,
`TAILORING_ONLY`, `TRANSFERABLE`, and `GAP` each get at least one hard and one
behavioral case. Both `ASSERTED` cases are hard status facts, because
eligibility and location conditions cannot be behavioral. The
stratum holds three hard cases whose challenge class is `partial_contribution`,
the limiting wording ("helped with") that `prompts/match_system.md` rules on,
and it takes whatever classification the reviewers resolved, so the set never
presupposes their label. A `partial_contribution` case can fill a matrix cell as
well, and selection takes the lexicographically first set for which some
assignment of cases to slots exists. `_REQUIRED_SLOTS` in
`evals/inspect/selection.py` is the slot list.

## What the document families score against

`draft` and `cover` score against the program's own verifiers rather than against
a reviewed label. A live result says whether attempt one cleared `verify.check`
or `verify.check_letter`, and names each check it failed. That is a claim about
provenance, traced figures, must-have coverage, and the other things those checks
decide. It is not a claim that the prose is good, because no check decides that.

A passing draft can still contain a claim its citation does not support. The
citation check establishes that a line names a selected entry, and the number
check establishes that each figure appears in the entry the line cites. Neither
establishes that the sentence means what the entry says, which is why
`verify.check` returns `factual review` as a standing warning on every run. So a
clean result is a grounding floor, not evidence that the draft is grounded, and
the family cannot be read as measuring fabrication.

`critic` scores each live case on whether the critic doubted a claim, which is
what the case was built to control, and records the six axes beside it. The
shipped outcome those axes produce is reported, not scored: the run header states
`critic_outcome_agreed` out of `critic_outcome_compared` against each case's
`critic_outcome`. The axes are a rubric judgment: a clean document can score
three on human voice and land on `warn` without the critic being wrong, so
scoring the outcome would fail a correct verdict. A disagreement is a row to
read, where either the critic or the document's fit to its label is wrong.
Whether a document carries a claim its cited evidence does not support is
instead a fact of how the case was written.

The task declares `SCORE_KIND = "judged"`, and the rate it reports answers one
question: did the critic doubt the documents built to be doubted, and leave the
others alone. The set holds cases of both kinds, so a critic that doubts
everything fails every clean row and one that doubts nothing fails every row
built to be doubted. That is the only claim this family supports. It says nothing about whether
the surviving verdicts are good, which is still a maintainer's reading.

No document dataset is reviewer-labeled. Every case is synthetic and written to
plant one defect in its canned answer, so the scripted mode can say a verifier
still catches it.

Live mode is different, and what varies between cases decides what a run can
claim. In the draft and cover families each case carries its own posting and the
match answer derived from it, and a test asserts that, so a live run measures the
provider on a distinct task per case. The candidate is deliberately shared: one
CV tailored to many postings is the product's own shape, so a differing result
between cases is about the posting, never about the person.

The critic is the exception worth stating, because there the document is the
variable rather than the posting. Each case carries its own canned resume draft
or cover letter, and a case carrying a planted claim reuses the clean case's
posting so the pair differs by exactly one drafted line. A planted line cites
only evidence its case selected and states no figure, which keeps the
deterministic `numbers traced` check from catching it first and leaves the
critic as the only reviewer that can. A letter case is critiqued as the product
critiques a letter: as a cover letter, indexed by paragraph, beside the
synthetic company research its paragraphs may cite.

A result over five or six cases exposes a defect; it supports no rate on the
quality of a document. The critic family's rate is the exception, and only
because it scores a constructed fact rather than a judgment: read it as "which
cases did the critic get wrong", never as a quality score. Measuring how stable
a stage is needs repeated trials of one fixed input, which is a different run
from this one. `scripts/eval.sh --live ... --trials N` is that run, and
`evals/inspect/diff.py` compares two logs by each case's passes out of its
trials, never trial against trial, beside each run's tokens and dollars. It
refuses two logs that record different selection manifest or dataset digests.

### What the document datasets do not pin

A case pins the check its planted defect trips, and nothing else. Every check
`verify.check` and `verify.check_letter` run carries a coverage decision in the
ledger in `tests/durability/test_eval_methodology_claims.py`: either the case
that plants a defect for it, or a stated reason no case does. Most of them are
only ever observed passing, and a check added or removed without a decision
fails that ledger.

A check nothing plants a defect for is invisible here. Deleting it, or breaking
it so that it always passes, leaves every case green, because a case asserts the
failures and warnings it expects and a removed check produces neither.

`claim ownership` is the gap worth naming: it is one of the support checks, and no
case reaches it, because it only fires on a line whose citations include a company
research source and no draft or cover case supplies research. A first-person achievement
resting on the employer's own words is therefore unmeasured.

## What the relevance family scores against

`relevance_review` scores one reviewed posting per sample. A row passes when the
shipped stage returned the frozen `expected_recommendation`, local validation
kept the answer, and, on a row whose axes the two labeller passes agreed,
returned all four frozen `expected_axes`.

The axes are scored because the recommendation does not stand in for them. The
class definitions in `src/clearscout/application/discovery/relevance_review.py`
allow 57 axis combinations, and 56 of them permit exactly one recommendation, so
the recommendation is close to a function of the axes rather than an independent
fact. It is also not a tight one: every one of the 16 frozen rows has at least
one single-axis change that the stage still accepts under that row's own
recommendation. A recommendation-only pass mark would let those through.

Citations are not scored on their own. The response schema closes
`master_cv_evidence_ids` over the inventory's entries and leaves
`posting_evidence_span_ids` an open string list. Local validation refuses a
span the batch never sent or one owned by another posting, and that rejection
fails the mark, so an ungrounded citation already costs the row.

The two independent labeller passes agreed the axes on 14 of 16 cases. The two
they split over carry `axes_confidence: low`, and their axes do not decide the
mark. The reference on those rows is one adjudication over a disputed label
rather than an agreed one, so a miss against it would report a prompt regression
the labels cannot support. Such a row is scored on its recommendation alone, its
axis comparison still reaches the score metadata, and the run header counts the
rows it set aside as `axes_recorded_not_scored`. The other 14 rows have their
axes decide the mark as the recommendation does.

An answer the stage reports as `insufficient_posting_text` is the exception. The
program nulls the recommendation and all four axes for that outcome, so the row
is scored on the outcome and its axes are recorded as not applicable rather than
as four wrong classifications. An answer local validation rejected is the same
case: the program discarded it, so the row fails on the rejection and states no
axes rather than four misclassifications. The header counts those rows as
`axes_not_recorded`, so the two counters together give the rows whose axes did
not decide the mark and the rest of the total is the rows whose axes did.

## What the jobspec boundary task scores against

Eight synthetic postings (`evals/fixtures/jobspec_boundary_cases.jsonl`) run
against four synthetic boundaries (`evals/fixtures/jobspec_boundaries.json`).
Each row names the boundary that should fire, or none, and why. A run counts
false positives, false negatives, and wrong boundaries per case and arm. It is
live only, and its expected values are written into the fixture, not labeled by
reviewer agents. Eight cases find defects; they do not measure a rate.

## What a result can claim

No model judge takes part in the shipped scoring path. The scorer compares a
completion with a label frozen earlier.

A score against agent labels supports two claims. The prompt is or is not
consistent with the rubric as the agents applied it. The run did or did not
surface defects. It does not show that the labels match human judgment.

Agents can share the same blind spot. So both reviews, the adjudication, the
disagreement counts, the agent identity, and the rubric version are all kept in
the calibration snapshot, and `LabelProvenance` is the shape a frozen row states
them in. Correlated agent error then stays visible. It cannot pass as
independent ground truth.

A live result belongs to the provider, model, prompt, settings, and sample it
names. It is not a lasting claim about the adapter or the provider.

## Known limits

- Replay cannot test a prompt change, because it sends no prompt.
- Scripted and live modes accept two sample shapes. One is a single
  requirement. The other is a whole `job_spec` posting with several
  requirements, sent through one match call and scored per requirement by
  stable id (`evals/support/jobspec.py`).
- Both shapes validate against the synthetic inventory in
  `evals/support/inventory.py` by `cv_hash`. A dataset tied to any other
  inventory needs its own inventory wiring before the harness can use it.
- The sample's own requirement ids are mapped onto the shipped r01..rNN
  vocabulary for the call and mapped back afterward. An outcome is credited only
  when the solver asserts `outcome_complete`: the live and scripted solvers set
  it from the match row the run established, and the replay solver sets it only
  when a stored `MatchEvidence` row exists. A requirement the run left
  unanswered reports `GAP` without completeness, carries the product's
  `gap_class` and `note`, and fails its frozen label rather than agreeing with
  it, so neither a paid row that returned nothing nor a replay row that was
  never written can inflate the agreement number.
- The many-requirement path is exercised only by the scripted contract
  (`match-scripted-multi`) and unit tests. No independently reviewed
  many-requirement `golden` dataset is frozen, so live multi-requirement
  scoring waits on one.
- A document case's plan comes from a canned match answer the case writes, so a
  change to the match prompt cannot move a `draft`, `cover`, or `critic` result.
  Selection is still the shipped one.
- The `critic` family reviews a document assembled from a canned draft. It says
  nothing about how the critic reads a document the drafter actually wrote.
- Each family's reviewed dataset is used for internal defect discovery. Its
  result measures agreement with frozen rubric-based agent labels. It says
  nothing about human judgment across a population. The match holdout is not
  opened because development results improved, and the relevance family has no
  holdout.
