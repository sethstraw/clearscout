# Agentic workflows: lessons from ClearScout's LLM pipelines

ClearScout runs model stages inside deterministic application code: extraction,
matching, relevance review, drafting, criticism, and guided search. This page
collects what building and measuring those pipelines taught. Lessons about
directing the coding agents that built the program are in
[Building with agents](building-with-agents.md).

Each lesson stands alone and comes from a real incident in this repository. The
numbers describe the software, its tests, and its evals.
[Architecture](architecture.md) describes the pipelines as they run now.

Most lessons here are known practice with ClearScout's own measurements
attached; where a lesson repeats published advice, the source is linked. Four
things we did not find in the projects and write-ups we checked. First, the
match answer can cite only CV ids from a fixed list the provider itself
enforces, and the cost of that list is measured. Second, the record of paid
runs is published, failures included. Third, a rule-based reader is shown
beside the model's reading rather than blended into it. Fourth, spend on a paid
eval run is an estimate and a record, never a hard stop.

## Evals and LLM quality

An eval is a repeatable test of how well one model step does its job.
[evals/FINDINGS.md](../evals/FINDINGS.md) holds the run-by-run detail. The
lessons below are the parts that apply beyond this project.

### An eval can run green while measuring nothing

**What was tried.** The first live run of all four eval families reported
success.

**What the numbers showed.** Reading the stored logs turned up 14 defects, most
in the harness. A missing match outcome scored as correct. A scorer read a
field the program leaves null on purpose. Six "cases" were one input repeated.
A billing call inside `finally` threw away a paid result. All 48 stored
baselines were scripted canned answers, so they could catch a code regression
but never a worse model answer. A critic scorer always returned True, and a
critic that failed everything still scored 6 of 9 in a rebuilt scorer.

**What we concluded.** After every paid run, read samples one at a time and ask
what each number means. Count distinct inputs, not rows. Label each eval mode
by the question it can answer: replay, scripted, or live. Pair every planted
defect with a clean control, so a judge that doubts everything fails.

A later live case written to measure when guided search picks a board page
scored 0 of 0 on every trial: none of 104 picks across runs was a board. The
prompt allows a board only when no detail URL turns up, and search almost never
leaves that gap. The scoring is covered offline; the live case cannot force the
path it was written for. The same happened to a fix: the draft eval scored only
the first attempt, so a check made redraftable could never show its redraft
pass. A two-attempt task then showed it (first attempt failed 3 of 3, redraft
passed 3 of 3). The cover-letter twin of that task ran 16 of 16 with no second
attempt, and two cases built to trip it passed the first attempt 6 of 6, so
that path stays unmeasured. Before reading a pass, count how often the path
under test actually ran: attempts used, boards picked, rows that reached the
check.

**Open.** [METHODOLOGY.md](../evals/inspect/METHODOLOGY.md) states what each
mode cannot measure.

### One run of a judge is an anecdote

**What was tried.** The fabrication critic, the last check before an
application goes out, ran 18 times on one unchanged document with a real
distortion.

**What the numbers showed.** It caught the distortion 7 times: 38.9%, Wilson
95% interval 0.20 to 0.61. (A Wilson interval is a range around a pass rate
showing how far it could move by chance, given how few cases there are.) Each
verdict either saw it or did not, so a looser threshold would not help. The fix
made the critic able to sample several times and fail the draft on any doubt.
Building it showed the provider response cache was keyed on content alone, so
three identical calls returned one answer. A
test that counts real provider calls failed `1 == 3` against the old code. On
the next model, one draw caught every planted defect on every draw, and three
draws only turned a one-in-three false doubt into a sure fail, so the shipped
default stays at one draw.

**What we concluded.** Run a judge many times on one fixed input and report an
interval, the same point as [Miller 2024](https://arxiv.org/abs/2411.00640);
one sample of a judge can mislead
([Schroeder and Wood-Doughty](https://arxiv.org/abs/2412.12509)). If verdicts
split cleanly, sample several times and union them.
Test that each sample really reaches the provider. Write down what the evidence
supports separately from what it does not.

**Open.** The projected 77% catch rate at three samples is arithmetic, never
measured. A new rule that scored 1.000 on a 10-row set with no held-out split
shows the rule fires, not that it generalizes.

### Test the check before you change the prompt

**What was tried.** Nine drafting runs passed, then the next three failed the
same checks.

**What the numbers showed.** The failures started the day a list-length counter
shipped. It counted commas across whole clauses, so an ordinary three-item list
scored 5 or 6. The drafter was told to cut a list it had not written. About
1,000 unit tests passed while live output tripped the checks, and eight paid
runs failed on false positives. Two checks contradicted each other: one
required a figure copied exactly, and the other flagged the copy.

**What we concluded.** Line up the date failures began with the commit history.
Ship every check change with a labelled synthetic corpus, a joint test showing
honest items pass all checks together, and a free replay over stored real
output. Decide on purpose which checks block and which only warn.

**Open.** A warning never triggers a redraft, so a warning-level echo survived
two rounds. That is a stated limit: copying cited evidence verbatim is the
safest behavior against fabrication.

### Gold labels can be wrong, and model labellers are not independent

**What was tried.** Hand-made fixtures and model-annotated gold sets were
treated as ground truth. (A gold label is the answer a person decided was
correct, and a gold set is the set of cases scored against those answers.)

**What the numbers showed.** The labels were wrong more often than expected.
The live critic scored 7 of 10, but it had judged all 10 correctly. Three
"clean" fixtures held real unsupported claims.

Agreement between labellers was also weaker evidence than it looked. A reader
gold set showed alpha 0.973 between two blind annotators. (Krippendorff's alpha
measures how much labellers agree beyond chance, where 1.0 means they always
agree.) But both annotators were the same model on the same instructions. Of
631 shared reasons, 106 were word-for-word identical.

The match gold set went through about ten relabel rounds. Each rubric edit
shifted other borderline labels.

One pre-release round of paid runs found the label wrong more often than the
model. Three critic cases failed 3 of 3 under a boundary other than their
label. Each time, the prompt's own definitions sided with the critic. The cited
entry reached a prototype, not a plan. A rollout added to a "designed" entry is
planned work written as shipped. A match row labelled `GAP` was answered
`TRANSFERABLE` at `weak` 3 of 3. That is exactly what a rule added after the
rubric froze tells the model to do. All five search-vocabulary misses were
plausible titles the gold lists lacked.

Each case was settled by a free rescore of the stored outputs against the
corrected label, with no new run bought. (A rescore runs the scorer again over
model outputs already saved, which costs nothing.)

**What we concluded.** Check each scored miss against the source before
touching the prompt. Suppose a miss repeats on every draw, and the prompt's
definitions allow the model's reading. Then suspect the label first.

Record that model annotators share a model. Settle disagreements under written
principles, and have a human review the rulings. Freeze the rubric by digest
during each prompt cycle. (A digest is a short fingerprint, a hash, of a file
or request; any change makes a different one.) Allow one relabel per cycle.
When a later prompt rule makes a frozen label wrong, record a per-case ruling.
A user's correction is calibration data, never gold.

Others report the same problems. Test-set labels are often wrong, and fixing
them can reverse rankings ([Northcutt et al.](https://arxiv.org/abs/2103.14749)).
Grading criteria also drift as people look at outputs
([Shankar et al.](https://arxiv.org/abs/2404.12272)). And models often make the
same mistakes ([Kim et al.](https://arxiv.org/abs/2506.07962)).

**Open.** The agent session made the adjudication rulings, under written
principles, for the maintainer to review afterwards.

### Read your prompt the way the model reads it

**What was tried.** A relevance prompt said to prefer "investigate" when a
deciding fact was missing. That sentence was the last line of its section.

**What the numbers showed.** An earlier "choose the first that applies" list
put `core_gap` ahead of `insufficient_posting`, so the model reached it first.
After a reorder and a new line ("posting silence is not a missing
qualification"), 0 of 43 later `core_gap` verdicts carried an open question.
In another run the maintainer disputed about a quarter of the "unlikely"
verdicts. All of them matched an excluded kind of work by its head noun and
dropped the qualifier. A rewording cut failures from 23 of 39 to 10 of 39 but
moved a control, so it did not land.

**What we concluded.** An explicit ordering beats a later sentence of caution.
Published work finds the same sensitivity to format and position
([Sclar et al.](https://arxiv.org/abs/2310.11324),
[Liu et al.](https://arxiv.org/abs/2307.03172)).
Have the domain owner dispute real outputs, and group the disputes by
mechanism. Fix structure (order, definitions) rather than adding emphasis. Ask
a negative verdict, in the prompt, to cite the evidence it weighed. Re-run a
small batch with control rows that should not move.

**Open.** Prompt wording could not enforce the head-noun class. A code
validator requiring cited ids was rejected, because it invites fabricated
citations.

### Show each stage the rules it is judged by

**What was tried.** A paid cassette reseal (re-recording a cassette: a saved
copy of a real request and the provider's reply, which the tests replay instead
of paying for a new call) failed on the critic's preservation
score, so the obvious moves were a looser floor or more redrafts.

**What the numbers showed.** Every low draw named a line the program copies
verbatim from the CV, such as the name line. No redraft could change those
lines. The block telling the critic which lines were program-copied listed only
four kinds. The merge step read scores from the first draw alone. Preservation
had 0 false lows in 79 cached draws, so it keeps the minimum. Alignment failed
on 2 of 79 draws, so it takes the median.

**What we concluded.** For every check, find where its rule appears in the
checked stage's prompt. If it is not there, add it or drop the check. Pass
program-copied lines to the judge marked as asserted. Choose the merge
statistic per axis from measured per-draw error rates, and keep every draw.

**Open.** [Architecture](architecture.md) records more cases of the same kind:
a stage judged on text or distinctions it was never shown.

### Rule out the setting before the third prompt rewrite

**What was tried.** A new match rule regressed two cases at low reasoning
effort (how much the model may reason before it answers), and one wording fix
recovered one.

**What the numbers showed.** Before another rewrite, a control at medium effort
with the same prompt scored 39 of 39. At low effort the model spent zero
reasoning tokens on the failing case. A newer model scored 54 of 57 against 57
of 57 for 25% less, and all three misses were one case answered the same way on
every trial.

**What we concluded.** Treat effort and model as variables to rule out first.
Look at reasoning tokens on the hard cases and at per-draw catches, not the
aggregate. When you accept a cheaper model, write down exactly which case you
gave up. [FINDINGS.md](../evals/FINDINGS.md#reasoning-effort-as-a-lever) has
the paired runs.

**Open.** Medium effort masks the literal reading rather than removing it.

### Compare two settings only on the same prompt and the same cases

**What was tried.** The critic's low-effort arm caught every planted claim, so
switching the stage to low looked settled.

**What the numbers showed.** A reviewer blocked the switch: the low arm had run
on the previous critic prompt, and nobody had measured low on clean documents,
where the risk is a false fail. Rerun on the shipping prompt, both arms scored
36 of 36 on clean cases and 59 of 63 on planted ones, with the same misses.
Low cost about 18 per cent less and answered in about 10 seconds at p95 against
24. The jobspec verbosity change (an OpenAI setting for how long the answer
is) was judged the same way: both arms ran on one
tree, minutes apart, 52 of 52 requirements each, with low verbosity 22 per cent
cheaper and twice as fast at p95.

**What we concluded.** A comparison holds only when the two arms differ in the
one setting under test. Run the paired arm on the prompt that will ship, cover
the failure the cheaper setting risks (here clean documents), and keep the
receipts side by side, the paired comparison
[Miller 2024](https://arxiv.org/abs/2411.00640) recommends. A win measured on
an older prompt is a reason to run the pair, not a result.

**Open.** The critic ships at low on both providers. On Anthropic that is a
drop from high and has not been measured.

### A realistic case finds what single-requirement cases miss

**What was tried.** Match was evaluated one requirement per case, 19 cases,
each asserted status line restating its requirement exactly.

**What the numbers showed.** The first whole-posting case, 33 requirements in
one call with only Canadian work authorization asserted, read a United States
authorization requirement as answered by that line 3 of 3. One prompt sentence
fixed it, and a containment clause kept a place inside the one asked for, or a
status that carries it, answering: a synthetic probe went 42 of 42 both ways
and the 19-case set 57 of 57. One 33-requirement call also moves all its rows
together, so its draws are not 33 independent samples.

**What we concluded.** Keep a production-shaped case beside the unit cases:
the same request size, the same mix of requirements, the same kind of asserted
facts. Score it per requirement, since the whole row is all or nothing, and
read movement across rows as one draw. Development data should mirror
production, as [Yan et al.](https://applied-llms.org/) also advise.

**Open.** It is one posting. It reads 31 of 33 per draw; one row misses on
every draw and two others intermittently.

### Eval effort lands where testing is easy, not where the risk is

**What was tried.** The offline eval reported "37 case(s) match their
baselines" after a prompt rewrite.

**What the numbers showed.** Six of eleven prompt families had no case at all.
A coverage audit joined each family to its spend. The guided search stage was
77.6% of all spend with zero eval cases. The first audit counted text mentions,
so "match" scored 91 files because it is an English word. One prompt edit would
have invalidated all 45 baselines, inviting a mass re-record that hides
regressions.

**What we concluded.** Rank eval work by spend and by how hard a wrong answer is
to spot. Require every prompt to be covered or listed as uncovered with a
reason; `evals/runner.py` keeps that list. Mutation-test each new case: delete
its rule and confirm it goes red. Fingerprint each case over only the stages it
reaches.

**Open.** Web search has no offline eval. No recorded case can judge real search
results.

### The exact request decides what you measure and what you pay

**What was tried.** Caching was switched on and assumed to work. The eval
harness sent one posting per call because that was easier to write.

**What the numbers showed.** Small details of the request decided the cost.
One stage wrote 60,768 cache tokens and read back none. The shared evidence sat
in the per-call part of the request, so no call could reuse it.

A response schema listed that run's ids, so every cache prefix was unique. (A
cache prefix is the opening part of a request that the provider stores and
bills at a discount when the next request starts the same way.) As a result,
576,180 tokens were written that a stable prefix would have read.

A two-sentence docstring edit on a response model changed every request digest.
That broke the recorded cassettes.

The one-per-call harness cost about four times as much as production batching.
It could also never reproduce batch-mode errors.

The same holds for plain web reads. Rendered page reads took 9 to 12 seconds,
and the delay was assumed to be politeness. A profile showed 25 serial requests
per read. Of those, 13 went to analytics and cookie-consent hosts, and 7.6
seconds were polite sleeps. We skipped those hosts and reused the page already
fetched. That cut a read to 11 requests and made it about 3 seconds faster. The
posting text was unchanged on three platforms.

A round of cost changes showed which levers pay. Each change was held to "no
quality loss" by a paired arm. Asking for low verbosity cut relevance output
from 19k to 11k tokens and cut its cost by 32 per cent. The rows that must stay
open held at 19 of 20 on both arms. Low verbosity also cut jobspec cost by 22
per cent, with every gold requirement still read (52 of 52). Constant draft and
cover schemas raised cover cache share from 25 to 45 per cent. Letting repeated
eval trials share one cache prefix took a critic run from USD 0.490 to 0.354,
with identical prompt tokens. Lower search depth on guided search cut input
tokens 9 per cent but saved nothing. Hosted-search fees and uncached search
results are most of that bill (46 and 38 per cent).

**What we concluded.** Log cache reads and writes per call, and compare them per
stage. Put stable instructions, evidence, and schema first. Check per-run ids in
code instead of in the schema.
([OpenAI's caching guide](https://developers.openai.com/api/docs/guides/prompt-caching)
lists what changes the cached prefix.) Treat schema docstrings as prompt text.
Field descriptions become part of the schema the model sees
([Instructor](https://python.useinstructor.com/concepts/fields/)).

Have the harness render the full production request before any paid run.
Profile a slow step's requests before blaming the delay on politeness. Before
cutting a cost, find the line that dominates that stage's bill. A cut elsewhere
buys nothing.

**Open.** Each cassette reseal costs money, so reseals come last in a cycle.

### A recording matched on the request body breaks on any request-shape change

**What was tried.** Each stage the recorder knows has one live call recorded
as a cassette and replayed through the real provider SDK, so the offline suite
proves the adapter parses what a real server sent.

**What the numbers showed.** A replay matches on the method, the URL, and the
request body. Top-level key order may differ, but a reordered schema property
or any changed value does not match, and an unrecorded request fails rather
than going live. So any change that alters the request text breaks the replay:
a prompt edit, a fixture edit, a response-model docstring that reaches the
schema, or a setting copied into the request. Each break is fixed only by a
paid re-record. This is the known cost of body matching
([vcrpy](https://vcrpy.readthedocs.io/en/latest/advanced.html),
[VCR cassettes for LLM tests](https://dev.to/multigrid/vcr-cassettes-for-llm-tests-in-python-20ac)).

**What we concluded.** Treat request-shape changes as one batch. Park them
together and re-record once, after prompts and paid tests settle, rather than
landing them one at a time and paying for a reseal each.
`scripts/record_cassette.py` lists what a recording is bound to, and
`tests/delivery/test_recorded.py` pins the matching rules and fails a replay
that leaves a recorded call unused, so a stale leftover cannot hide.

Recording the web-search stages captures the real pages a live search
returned. They name real employers, which the public-data audit refuses
elsewhere, and a hand review still found a third-party registry page for an
individual in one response. The audit now exempts only those cassettes from
its employer check, and each recording gets a read before it is committed.

**Open.** Each cassette covers one path at the standard tier. Production asks
for the flex tier (OpenAI's cheaper, slower service tier) on unattended work, and a request shape no recording sent
has no replay evidence.

### A smoke call has to check what the scorer needs, not only that it was paid

**What was tried.** Before a long paid eval, the runner made a smoke call (one
cheap call made before a long paid run, to check the run works at all) on the
first sample and stopped only if that call failed or cost too much.

**What the numbers showed.** A call can be billed and well formed and still
lack the field the scorer reads, so the full run would have paid for every
sample and scored none. A first sample that makes no paid call at all proved
nothing about the provider path, yet ended the check.

**What we concluded.** Each scorer declares the fields it reads in
`required_fields`. The smoke scores its output and fails on a missing field, a
scorer error, or a measurement with no dollars or no prompt usage. It walks
past samples that make no paid call until one does, and fails if none does.
`_smoke` in `evals/inspect/runner.py` is the rule.

**Open.** The smoke checks shape and billing on one paid case. It says nothing
about whether the answer is good.

## Product design

### Make fabrication impossible to express

**What was tried.** Program-owned facts and closed lists of real evidence ids,
in place of an instruction to "only use the provided evidence".

**What the numbers showed.** Every CV entry gets an id derived from its content.
Match's response schema is a closed list over those ids. The drafting, cover
letter, and search-vocabulary schemas stay constant, so the provider can cache
them, and code refuses any id outside the run's selected evidence. The
program copies titles, employers, dates, degrees, and citations verbatim. Code
checks every cited id against the run's selected evidence and every quote
against the stored posting. On one queue review, 493 of 493 cited CV ids and
587 of 587 posting citations held. Models still tried to invent: participation
upgraded to delivery, a draft crediting the model's own "reasoning", and an
invented posting id. The checks caught each one.

**What we concluded.** Let the program own the facts. Let the model choose,
order, and rephrase. Close a schema over a stable set, check per-run ids in
code, check provenance in code, and report "verified" and "good" as two
measures. [JobCtrl](https://github.com/ebarti/JobCtrl/blob/main/docs/guides/resume-tailoring-without-fabrication.md)
and [aarg](https://github.com/joseym/aarg#it-wont-make-things-up) are parallel
designs where the model works in evidence ids; ClearScout differs by closing the
ids in the provider schema.

**Open.** "Verified" is a floor. A cheaper model passed it while scoring a point
lower on three critic axes. The design makes some failures impossible and
others easy to see, which is why human review stays part of the product.

### Anti-fabrication fails where code silently fills a provenance field

**What was tried.** A fallback kept the screen from looking empty. When code
could not find a requirement's text in the posting, it quoted the posting's
first 240 characters.

**What the numbers showed.** A requirement for a specific tool showed the
company's "About us" paragraph as what the posting asked for. A census found
the fallback on about two thirds of requirement rows. The checks for unanswered
requirements had fired on none of them. A sibling defect gave an evidence
anchor a default classification of direct and moderate. Separately, a commit
step used `model_title or stored_title`, so the model's title won whenever it
returned one, including the literal heading "The Role". The first fix covered
only the ledger; the report, the file name, and the drafts kept the model's
title until a second fix. A suite of over 37,000 Python tests passed throughout.

**What we concluded.** Run a census over stored output for the fallback's
signature before trusting what a screen claims. When an anchor misses, store
nothing and say so. Never give a provenance field a default. Before calling a
provenance fix done, list every reader of the field.

**Open.** The README once said every requirement quotes the posting, which
overstated the code. A large green suite is not proof a promise holds.

### Replay a rule change over every real input you hold

**What was tried.** Adding "work from anywhere" to the remote vocabulary, to
recover remote roles.

**What the numbers showed.** A benefits line ("the opportunity to work from
anywhere") outranked a board's own "Hybrid" tag. Across 184 stored postings the
phrase changed one reading, and that change was wrong. The rows it was meant to
rescue numbered 0, so the change was reverted. A job-board URL fix was accepted
on the same kind of table: "USA---City-GA" had been read as the US plus Gabon.
The fix changed about 15% of stored URLs, every change toward the right
country.

**What we concluded.** Use a before-and-after table as the acceptance evidence:
newly correct, newly wrong, unchanged. Revert when the gain is zero. Copy string
shapes from real payloads into synthetic fixtures. Test each changed pattern
against a generated matrix of surface forms: spelled numbers, dashes,
abbreviations, and non-breaking spaces.

**Open.** Benefits and marketing text use the same words with a different
meaning. A replay is only as wide as the stored inputs.

### Treat money as an estimate and a record, never a gate

**What was tried.** A $1.00 per-run spend limit.

**What the numbers showed.** A web search made 6 calls costing $1.04, refused
the seventh, and failed the task, discarding all six paid results. Typical
runs sat near the limit, so it was sure to trip again. Exact caps then refused
whole classes of request. The spending ledger had its own failures: four paid
paths billed without writing a row, a dated served-model name left most calls
in one demo unpriced, and a partial price override made cached reads 5 times
too expensive.

**What we concluded.** Ask for approval with an estimate. Record actual usage in
its own transaction before anything else can fail. Keep partial paid results.
Show partial totals with a count of unpriced calls. Price by the model actually
served, and stop only on hard provider errors. Run unattended work on the
cheaper low-priority tier.

**Open.** A few early cost rows were rebuilt from a table a model wrote, so
those totals are estimates, not measurements.

### Run a rule-based reader beside the model and report them apart

**What was tried.** A fit reader that takes level, years, degree, location,
management, and must-have versus preferred from the raw posting text, with no
model.

**What the numbers showed.** It answers "not stated" when unsure. It reads the
raw text, not the model's extraction, because a rule reading of a summary only
measures the summary. Its taxonomy covers more than 70 role families, and
fewer than 10 of more than 500 terms are single words. Rounds 7, 8, and 9 each
fixed the last replay's defects and introduced a smaller new class.

**What we concluded.** Pair each model judgment with a cheap reading that may
abstain. Print both side by side with their quotes, with no precedence and no
averaging. Their disagreements are the rows a person should review.
[Architecture](architecture.md) explains the design.

**Open.** The reader was frozen after round 10. A rule change now may only fix a
false rejection.

### Synthetic fixtures miss the formats real publishers write

**What was tried.** Posted-date readers built from synthetic board records in
the expected shape: ISO dates and epoch milliseconds.

**What the numbers showed.** Real boards wrote forms none of the fixtures held:
a US-order numeric date with a two-digit year, epoch seconds where the reader
expected milliseconds, and `JobPosting` markup carrying a JavaScript block
comment that made the JSON unparseable. Each one read as "no date" without an
error. Several platforms had no posted-date declaration at all, and nothing
noticed.

**What we concluded.** Copy string shapes from real payloads into synthetic
fixtures. Make absence a declared choice: every platform declares where its
records state a posted date, or names why they state none, and
`test_every_platform_declares_a_posted_date_or_says_why_it_cannot` in
`tests/loop/test_adapter_completeness.py` fails a platform that does neither.

**Open.** A new publisher format still reads as no date until a real payload
shows it.

### Merging an unchanged value must not count as a change

**What was tried.** Every read of a posting merges the posted date it states
into the stored one, counting reads and changes. A date that changes on every
one of three or more reads is treated as rolling and withheld from sorting.

**What the numbers showed.** One pass can read the same fetch twice, through
its check and its body, so one stable date could count as two reads. A guard
built on read counts would then fire on noise.

**What we concluded.** A merge that restates the latest value on the same read
day returns what is stored, and only a read whose identity verified merges at
all. `merged_posted_date` in `src/clearscout/domain/staged_facts.py` and
`_take_posted_date` in
`src/clearscout/application/discovery/content_resolution/store.py` hold the
rule.

**Open.** A board date can still move when an employer reposts, and that reads
as a repost rather than as noise.

### An option every layer carries can still be read by none

**What was tried.** A "fresh scan" option was set on the scan task, copied onto
each board, and passed into the reader options, so new posted-date readers
would reach postings already held.

**What the numbers showed.** No production code read the option. A fresh scan
re-read nothing it already held, and 24 held rows stayed undated after one.
Only a recount of undated rows exposed it. With a re-read cutoff taken from the
fresh scan's start time, one fresh scan re-read 82 held rows and every one came
back dated.

**What we concluded.** Prove an option changes an outcome with a test on that
outcome, not on the value passed along. Delete plumbing nothing reads.

**Open.** A passed-through value looks wired in every diff that touches it.

### Cutting repeated work can cut a side effect another reader needs

**What was tried.** Ordinary scans re-read every body-dismissed row on each
pass, 58 slow reads on one board pass, so the repeat was removed.

**What the numbers showed.** Each repeat had also written a fresh dismissal
event. An audit label matched that event's origin exactly, and without the
repeat it began reporting about half those rows as already known instead of
excluded. A review caught it before landing.

**What we concluded.** Before removing repeated work, list what each repetition
wrote and who reads it, and move any reader that depends on it.

**Open.** Nothing finds such a reader automatically; the list is by hand.

## If you build something similar

These are opinions. Each links the lesson above that backs it.

- Read eval output one sample at a time after every paid run, count distinct
  inputs rather than rows, and pair every planted defect with a clean control.
  ([lesson](#an-eval-can-run-green-while-measuring-nothing))
- Run a judge many times on one fixed input before trusting a verdict, and
  report an interval rather than one result.
  ([lesson](#one-run-of-a-judge-is-an-anecdote))
- Ship every check change with a labelled corpus, a joint test that honest
  items pass, and a replay over stored real output, before touching the prompt.
  ([lesson](#test-the-check-before-you-change-the-prompt))
- Check each scored miss against the source before blaming the prompt; model
  labellers share a model, and a user's correction is calibration, not gold.
  ([lesson](#gold-labels-can-be-wrong-and-model-labellers-are-not-independent))
- Fix a misread prompt with structure, such as order and definitions, not with
  more emphasis. ([lesson](#read-your-prompt-the-way-the-model-reads-it))
- Show every stage the rule it is checked against, or drop the check.
  ([lesson](#show-each-stage-the-rules-it-is-judged-by))
- Rule out effort and model before a third prompt rewrite.
  ([lesson](#rule-out-the-setting-before-the-third-prompt-rewrite))
- Compare two settings only when both arms ran the shipping prompt on the same
  cases, including the cases the cheaper setting puts at risk.
  ([lesson](#compare-two-settings-only-on-the-same-prompt-and-the-same-cases))
- Keep one production-shaped case beside the unit cases, and score it per
  requirement. ([lesson](#a-realistic-case-finds-what-single-requirement-cases-miss))
- Rank eval work by spend and by how hard a wrong answer is to spot, and list
  every uncovered prompt with a reason.
  ([lesson](#eval-effort-lands-where-testing-is-easy-not-where-the-risk-is))
- Measure the exact production request: put stable content first for caching,
  treat schema docstrings as prompt text, batch in the harness as production
  does, and cut the line that dominates the bill.
  ([lesson](#the-exact-request-decides-what-you-measure-and-what-you-pay))
- Batch request-shape changes and re-record a recorded run once, last, rather
  than paying for a reseal per change.
  ([lesson](#a-recording-matched-on-the-request-body-breaks-on-any-request-shape-change))
- Make the smoke call before a long paid run score its output and fail on any
  field the scorer reads but the output lacks.
  ([lesson](#a-smoke-call-has-to-check-what-the-scorer-needs-not-only-that-it-was-paid))
- Let the program own the facts and the model choose among ids that code
  checks; report "verified" and "good" as separate measures.
  ([lesson](#make-fabrication-impossible-to-express))
- Never give a provenance field a default. When an anchor misses, store nothing
  and say so. ([lesson](#anti-fabrication-fails-where-code-silently-fills-a-provenance-field))
- Accept a rule change only on a before-and-after table over every stored
  input, and revert it when the gain is zero.
  ([lesson](#replay-a-rule-change-over-every-real-input-you-hold))
- Treat money as an estimate to approve and a record to keep, never a gate that
  discards paid work. ([lesson](#treat-money-as-an-estimate-and-a-record-never-a-gate))
- Run a rule-based reader that may abstain beside each model judgment, and show
  their disagreements instead of merging them.
  ([lesson](#run-a-rule-based-reader-beside-the-model-and-report-them-apart))
- Copy fixture shapes from real payloads, and make "this source states nothing"
  a declared choice a test enforces.
  ([lesson](#synthetic-fixtures-miss-the-formats-real-publishers-write))
- Make merging an unchanged value a no-op before any guard counts changes.
  ([lesson](#merging-an-unchanged-value-must-not-count-as-a-change))
- Test an option by the outcome it should change, and delete plumbing nothing
  reads. ([lesson](#an-option-every-layer-carries-can-still-be-read-by-none))
- Before cutting repeated work, list what each repetition wrote and who reads
  it. ([lesson](#cutting-repeated-work-can-cut-a-side-effect-another-reader-needs))
