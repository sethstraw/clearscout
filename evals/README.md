# Evaluations

Contributor tooling. It is not in the wheel, there is no `clearscout` subcommand
for it, and `pytest` will not run it. `scripts/gate.sh py`, and so `change` and
`all`, runs `python -m evals`, so a failing case fails the gate.

```bash
uv run python -m evals                     # run every case against its baseline
uv run python -m evals --case poor_fit     # run one
uv run python -m evals --record-baselines  # rewrite the baselines, comparing nothing
```

## What this measures, and what it does not

A case is an invented posting, an invented master CV, and a scripted set of
model responses. It runs through the shipped stages: the same parser,
extraction, matching, selection, drafting, assembly, and verification a real
run uses. Only one thing is swapped out. The provider is scripted, so the
answers come from the case file, not from a model.

**Scripted cases measure the program.** A case can script a model that returns
an invented figure, an inflated ownership claim, an uncited line, an identifier
that does not exist, or an instruction it absorbed from the posting. That case
measures whether the deterministic layer catches it. That is the failure worth
catching, and the one nobody can afford to test live on every change.

**Scripted cases measure nothing about the prompts.** A case proving an invented
figure is caught says nothing about whether the drafting prompt makes a model
less likely to invent one. Do not read a passing suite as evidence that a prompt
is good. It is evidence that a bad answer would not have reached the page.

**Live cases measure the prompts.** They run only through
`scripts/eval.sh --live`. Each needs per-run operator approval, frozen sample
identity, and a private output directory, and the match family also needs its
selection manifest; the history-backed cost estimate is advisory. The live runner uses
the shipped stage and provider adapter. It records requested and served model
identity, usage, priced spend, latency, and citation validity. It retains
neither prompt text nor raw provider responses. The relevance family sends each
repeat in the batches production would cut: one task per search context, split
by the production batch limits, under the response schema production closes over
that task's CV and conditions. It bills once per call and records each row's batch. It
is deliberately separate from `python -m evals`. [`inspect/README.md`](inspect/README.md)
gives the commands for each family. Read [`inspect/METHODOLOGY.md`](inspect/METHODOLOGY.md)
before preparing or comparing a live run. [`RESULTS.md`](RESULTS.md) lists every
live run on the synthetic sets.

The process runs with `socket.socket`, `socket.create_connection`, and
`socket.getaddrinfo` replaced by functions that raise. Nothing here can reach a
network by accident. A future change that tried would fail loudly instead of
billing.

## Semantic annotations are not scripted cases

`dataset.py` defines the separate metadata contract for sanitized historical fit
annotations and retrieval annotations. An annotation identifies a separately
controlled sanitized artifact by an opaque reference and its SHA-256 digest. It
does not contain the artifact and cannot be executed by itself. A future
semantic runner must resolve that reference from its approved private artifact
store and verify the digest before making a request.

`python -m evals` does not load these annotations. The files under `cases/` are
invented scripted fixtures. Historical annotations stay outside that directory
and outside the public repository until their privacy and redistribution
reviews pass. The loader enforces those publication gates. It also keeps one
sanitized artifact from crossing development, regression, and holdout splits.

`retrieval_metrics.py` is a pure evaluator for outcomes supplied by a future
runner. It makes no provider or product calls. It keeps the counts behind every
rate, and it reports an undefined value when a run has no eligible denominator.

`semantic.py` is the opt-in boundary for a whole-corpus semantic runner. It
partitions the sanitized annotations and creates a reproducible blinded
baseline/candidate order. It aggregates stage usage and defect labels, and it
refuses a result with repeated identifiers or a judgement its evidence does not
support. It has no provider import. The
approved caller injects its executor, and that caller must reuse the production
request compiler and official adapters.

The default configuration is disabled. Exact request and response text is
discarded unless an approved run asks to capture it and names a private
artifact directory outside the repository.

## What a case file is

One TOML file under `cases/`, readable end to end. It carries the posting, the
CV to run against, the model's scripted answers, and `[expect]`.

Evidence is named by an alias, because entry identifiers are content hashes and a
case file full of `b-f64b391d` is a case file nobody maintains:

```toml
[evidence]
kafka = "Rebuilt the ingestion tier on Kafka"
```

The phrase must appear in exactly one entry of that CV. That stops a case from
quietly starting to mean something else after somebody edits the CV. An alias
whose value begins with `id:` is a literal identifier instead. It exists for
exactly one case: citing evidence that does not exist.

A case may also script a reviewer. `[[approval]]` names a proposed line by the
end of its slot, decides it, and may supply replacement text:

```toml
[[approval]]
slot = "summary"
decision = "reject"

[approval.replacement]
text = "Ran the entire data organisation."
```

That is the review workflow measured from the outside. Replacement text goes
through the same checks the model's text does. A case can supply text that
cites nothing, or that cites an identifier outside the run's vocabulary. It
then records the refusal, and that nothing unsupported reached the document.

`mode` is `draft` or `screen`. Screening stops after matching, which is what the
program does and what triaging a slate of postings costs. Selection runs in
both. It is free, and what a posting would have put on a page is the clearest
evidence that two similar postings were read differently.

A case may script a third participant. The model is one and the reviewer is
another. `[discovery]` is the person the CV is about, answering questions about
a gap:

```toml
[discovery]
about = "Kubernetes in production"
gap = "r01"
replies = ["I sat in on the design reviews for it, nothing more than that."]
claim = "Owned the design reviews."
```

That runs the shipped questioning, proposes the wording from the answers, and
records what happened. `forbidden` names sources a reply may not come from. Each
is tried with the same wording, so a case measures three refusals instead of
asserting them. No master CV is written. An evaluation that edited a fixture
would change what every later case measures. `discovery_master_cv_writes`
records the zero so it is not left implied.

A case may also name the other cases its posting forms a batch with:

```toml
[expect]
batch = ["batch_shared_requirement_b"]
batch_marker = "Volt Harbour Freight"
```

A batch needs more than one posting. The cheapest valid second one is another
case that already has a posting, a scripted extraction, and a scripted match.
The marker is a phrase that appears in that case's posting and nowhere else.
That is how `batch_prompt_crossings` can be measured at all.

Four more blocks each reach one stage the others never do.

- `[responses.cover_letter]` drafts a letter from the plan the resume already
  selected from, and runs the shipped letter check over it.
- `[structure]` carries a CV document and the extraction a model returns for it.
  It then runs the importer's authenticity and losslessness checks.
- `[vision]` carries a base64 screenshot and its transcript. It goes through the
  shipped image reader and its length gate.
- `[search_vocabulary]` carries proposed search titles and the search's own
  exclusions. It records which the deterministic gate accepted and which it
  refused.

Two more blocks reach the paid discovery stages.

`[posting_recovery]` carries a hosted search's captured sources and the model's
cited candidate. It runs the pure identity check in
`assisted_content_resolution.py` up to, but never through, `resolver.resolve`. A
single cited publisher-body candidate is recorded as "candidate_ready", not
resolved. An uncited candidate is refused.

`[guided_campaign]` carries a captured attempt's hosted search and the model's
picks. They are dispatched and collected through the shipped background-search
client against a synthetic two-branch campaign. They are scored by the same
`captured_url_rows` and `matched_queries` projections the durable runner
credits a capture with. The scripted response is already complete when it is
first polled. Nothing here exercises waiting, canceling, or an unusable
background response.

## Expectations and baselines are different things

`[expect]` is what must be true for the case to mean anything. A violation is a
defect. The run fails naming the case, the scorer, the expected value, and the
actual one.

`baselines/<case>.json` is what was measured. A change there is not
automatically a regression. It is a diff a reviewer reads and either accepts or
rejects. Each baseline states the prompt suite fingerprint and the response
schema fingerprint that produced it. A baseline recorded under different prompts
is reported as incomparable, not silently compared.

Both fingerprints cover the stages that case reached, not the whole suite.
Editing one prompt marks the baselines that sent it incomparable and leaves
every other case comparable. A reviewer reads a short diff instead of resealing
all of them at once. Resealing everything is how a regression hides.

Every counting scorer defaults to zero, which is what a clean run finds. A case
that deliberately produces something says so:

```toml
[expect.measured]
ownership_inflation = 2
```

That is not a way of excusing a finding. It is how a case asserts the finding it
was written to produce, so that a change in the count is a change in behavior.

`[expect.derived]` is the same idea for the deterministic reading in
`src/clearscout/application/tailoring/fit/`, keyed by the field each answer is stored under:

```toml
[expect.derived]
role_family = "Sales and business development"
seniority = "not stated"
```

"not stated" is a value like any other, and it is what a negative control
asserts. Five of the cases here exist only to show that a posting a keyword
matcher would answer confidently produces no answer at all:

- cross-family terminology,
- an inflated title,
- a posting that manages work, not people,
- a posting stating no level, and
- a requirements block that is entirely legal and benefits language.

A detector that cannot be shown to abstain is not finished. Those cases are part
of the feature, not extra.

## Regenerating a baseline

`--record-baselines` rewrites every baseline from the current run and compares
nothing. It is the only path here that writes a tracked file. The CV-import and
screenshot cases also write a temporary file and delete it, because the shipped
readers they run take a path.

A failing evaluation cannot rewrite its own baseline. The reason is structural,
not a rule somebody remembers. `baseline.py` reads and compares and contains no
code that opens a file for writing. The one baseline write lives in `runner.py`.
It is reached only from the `--record-baselines` branch, which never calls
`compare`. A failure has no result to react to and no path to the writer.

Regenerating is legitimate when you changed something that should move the
numbers and you have read the diff and agree with every line of it. It is hiding
a regression when you ran it because the suite was red. `CONTRIBUTING.md` has
the longer version.

## The scorers

Two kinds, and the difference matters when reading a result.

**Reported checks** run the shipped check and record what it said.
`check.<name>` is one score per finding from
`src/clearscout/application/tailoring/stages/verify/`, verbatim.
Nothing is restated. A check that changes its verdict moves the baseline, and a
check that is deleted disappears from it. `keyword_coverage_percent` and
`unsanitized_keywords` are the same kind. They read `verify.keyword_coverage`
and the bounds `src/clearscout/application/tailoring/stages/jobspec.py`
enforces.

So is `derived.<question>`. It runs the deterministic reading in
`application/tailoring/fit/` over the case's posting. It writes down each answer
as it renders, "not stated" included, beside `derived_abstentions` and
`model_rule_disagreements`.

The reading is taken before the model is asked anything, and from the raw
posting, not from the extraction of it. That independence is the only reason
`model_rule_disagreements` means anything. It counts the places where two
readings of one text do not match. Each is a defect in one of them, and the
harness does not claim to know which. It defaults to zero like every other
count, so a case producing one says so and says why.

**Measured properties** read the artifacts directly:

- requirement precision and recall, and must-have accuracy,
- hard-gate and blocking-gate extraction,
- match classification and gap preservation,
- evidence-vocabulary closure,
- unsupported citations, uncited lines, and unsupported figures,
- immutable-fact preservation and contact preservation,
- unsupported terms in the document,
- injection leaks and posting-language echo,
- ownership inflation, and
- model call count and tokens.

A case that scripts a reviewer adds two more. One is whether the approval was
applied or refused. The other is how many unsupported replacement lines reached
the document, which must be none.

The review artifact is built for every drafting case whether or not the case
decides anything. A builder that crashed on an unusual draft should fail here,
not in somebody's run. Only a case with an `[[approval]]` block is scored on it,
so a case without one keeps its baseline.

A case with a `[discovery]` block adds eight more. Four of them must be zero
whatever the case is trying to demonstrate:

- an ownership upgrade admitted,
- a figure no answer stated admitted,
- a word nobody typed admitted, and
- material admitted from one of the three forbidden sources.

A case cannot declare its way out of them. Raising one would be asserting the
failure, not the guarantee. The other four record what happened: the outcome,
how many questions were asked, how many gaps are still gaps, and that nothing
was written.

A case naming a `batch` adds four more, and the second pair is the reason the
machinery exists. `batch_shared_requirements` and `batch_top_leverage` are what
the comparison found. `batch_prompt_crossings` counts requests built while
screening one posting that carry another's marker phrase.
`batch_cache_collisions` counts cache keys two postings share. Both are zero.
They are what "postings are never combined" and "a cached answer cannot cross"
mean when they are measured, not asserted.

Three of them read the requests, not the responses. They are the only things
this harness can measure about a prompt.

- `campaign_branch_labels_missing_from_task` counts the campaign branches whose
  label the rendered task text does not name. It is the one place a prompt that
  stopped carrying its own subject would show up here.
- `unbounded_untrusted_text` counts the times a phrase a case declares under
  `[expect] bounded` reached a request outside an untrusted-data boundary.
- `deterministic_work_requested` counts requests asking a model to redo work the
  program performs for itself.

The last two are structural. A scripted model does not answer differently when
a prompt changes. So neither can show that a boundary or a deleted instruction
improved an answer. What they show is that the property holds in every stage
that carries the text, and keeps holding as stages are added. They are measured
over what was sent. A caller that assembles a prompt by hand is covered as well
as the prompt files.

Three of them have no shipped equivalent, so they are proposals, not
measurements of a guarantee. `ownership_inflation` and `posting_language_echoed`
find fabrications that pass every deterministic check. `unsupported_citations`
uses knowledge a case has and the program cannot: which real entry is the wrong
evidence for a claim. Each says so where it is defined, with why it has not been
promoted into `src/clearscout/application/tailoring/stages/verify/`. A scorer that keeps finding something is the argument
for promoting it.

Latency is measured and printed and deliberately kept out of the baseline. A
number that moves with machine load turns every comparison into noise. Tokens are
recorded and are zero under a scripted provider, which reports no usage on
purpose, because an instrument that invents usage numbers cannot be trusted to
measure anything else.

## The judge

Not built, and the boundary is worth writing down before anybody builds it.

An LLM judge may assess clarity, specificity, readability, natural voice, role
alignment, and redundancy. It may not decide factual truth, evidence validity,
numerical support, or identity preservation. The deterministic scorers above own
those, and they must stay there. A judge that can be argued out of a verdict is
not a gate.

Building the seam now would mean committing an interface that has never been
exercised, on the guess that it is the right shape. The alternative is this
paragraph and a live run, in that order.

## Adding a case

Add one when you observe a failure the existing cases do not produce. Give it a
`proves` string that says what it demonstrates. Where the answer is
uncomfortable, it also says what it does not. Several cases here record that
every shipped check passes on a draft that should not be sent. That is the
finding, and it is the reason the file exists.

Every fixture is invented. No real person, employer, school, publication, or
posting appears in `cases/` or `cvs/`, and none may.
