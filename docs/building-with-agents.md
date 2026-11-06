# Building with agents

ClearScout and its sibling renderer, pressume, were built by one person
directing AI coding agents. The maintainer comes from a domain, not from a
career in software. This page collects what that taught, for people in the same
spot.

Each lesson stands alone and comes from a real incident in this repository. The
numbers describe the software, its tests, and its evals. Counts from the
maintainer's own job search appear only as rates or are left out.
[How it was built](contributing/agent-workflow.md) shows the agent setup itself.

## Building with AI agents

### More instructions did not stop a repeated agent mistake

**What was tried.** Agents kept writing bad comments: review codes, pointers to
other files, change narration, and docstrings longer than their code. Rules
were added to agent task instructions four times. The habit came back each
time. The comment checker knew three mechanical rules and ran only at gate
time, after every agent had finished.

**What the numbers showed.** The word "comment" showed up in the maintainer's
own turns in 52 of 86 sessions, a proxy for hand corrections. A check that runs
the moment an agent writes a file first flagged 240 hits. Sampling cut the
noisy rules: bare "now", "no longer", and "previously" had 0 true positives in
52 sampled hits. That left 138 real violations, fixed across about 85 files.

**What we concluded.** After the third identical correction, stop writing
rules. Turn the correction into a check that runs on the write and names the
file and line, and add a sheet of real good and bad examples. Measure the
check's precision on a sample before enforcing it.

**Open.** The record measures the check, not whether the habit stopped. For a
sibling rule ("the main session never idles") the habit continued after a hook
was added. A new hook is not proof the problem is gone.

### Treat an agent's report as a lead to check

**What was tried.** Agent summaries were taken as fact. A code map said a
commit had added typed test fakes, and a finding was closed on that claim.

**What the numbers showed.** The commit was never on main.
`git merge-base --is-ancestor` returned false, and one test file still held 25
untyped doubles. Two review agents and the main session all read a parameter
named `recorded_outcomes` as a defect; one measured run showed every call site
passed a differently filtered count. The more detailed of two conflicting
reports called a phrase synthetic, and one query found it on 41 real rows.
Labelling receipts took the model name from text an agent typed, so 6 rounds
on a weaker model were recorded as the stronger one. Once, a background
wrapper's exit code said the gate passed when it failed on three scopes.
Another time the harness said exit 0 while the log showed 1 failure.

**What we concluded.** Agreement among agents is not evidence, and neither is a
confident tone or an exit code. Make "is it really on main" a one-command
check. Settle a disagreement with a query. Take provenance (model, dataset
digest, rubric version) from what the system recorded. Read the end of the log
before recording a pass.

**Open.** Several readings of one misleading name are not independent. Nothing
here detects that automatically.

### Run the query that could kill the premise first

**What was tried.** Findings, fixes, and spend were planned from how a problem
looked. A "cache-hit flag inverted" finding was filed as major. Another said
candidates were dismissed by machine before anyone read them.

**What the numbers showed.** All 50 flagged cache calls cost $0 and had no
provider response id: the flag was a local replay marker, not a provider
metric. Nearly all dismissals were explicit human actions. Both findings were
withdrawn. An approved backfill of 34 recorded runs needed 0 of 34. A next-work
list held six items; five were stale or wrong, and the one critical item was
already built. Of 57 vocabulary members flagged as untested, 1 was removable.

**What we concluded.** Write the one query that would make the claim false:
group by the suspect field, split by who acted, or check whether the path can
be reached. Put its count in the finding. Send one read-only auditor over the
whole worklist before assigning any writer.

**Open.** Agents still read meaning into a column from its name. Handing an
agent a finding's proposed fix as its task gets that fix built, right or not.

### A green gate is not a review

**What was tried.** Eighteen parallel agents finished, and the gate passed
twice. No review had run.

**What the numbers showed.** When review panels read the integrated diff of
about 72 files, three of four blocked it. One check's own test approved the
exact fabrication the check was written to catch. An eval diff tool keyed only
on sample id and merged two prompt arms. A follow-up writer flipped a
deliberate test pair so its change would pass. Seven green tests covered a
feature that never ran in production, because the tests supplied a dependency
the production entry point never did. Loose `SimpleNamespace` test doubles hid
three defects. When five SPA test files moved to fixtures typed against the
generated API schema, all five had responses missing required fields.

**What we concluded.** Review each writer's diff as soon as it hands back. List
every assertion an agent flipped or deleted, and treat each as a product
decision. Build test dependencies the way production builds them. Type test
data against the real wire schema.

**Open.** Review saved for the end is the most expensive review, because one
reviewer then reads 70 files at once.

### A safety check that has never fired may be unable to fire

**What was tried.** Checks that always reported "all clear" were trusted.

**What the numbers showed.** A database migration gate reported 0 foreign-key
violations on every run. It counted them after a loop had already deleted the
orphans. It also copied only tables both schemas shared, so a table rename
would have dropped 392 rows across 9 tables while it said ok. A hook audit
found 4 defects. One watch was written as globs where the hook system expects
literal names, so it could never fire. A crawler computed a "same origin" flag
and never checked it, then queued 181,834 URLs from the open web. A preflight
that shelled out to `ps`, which the sandbox denies, passed instead of refusing.

**What we concluded.** For every "all clear", feed the check the case it
should block and watch it refuse. Count before cleanup. Put a hard limit on any
loop that feeds itself, separate from any filter; a 500-row cap ended the
crawler runaway. If a check depends on a tool the sandbox denies, treat it as
not run.

**Open.** Two of the hook defects were found by reading, not reproduced live.

### The bill comes from agents re-reading context

**What was tried.** After hitting a weekly plan limit, the maintainer had local
transcripts measured, weighting tokens by list-price ratios.

**What the numbers showed.** Over one week, cache reads were 79% of weighted
cost, cache writes 16%, and output 5%. 58% came from requests carrying more
than 200K tokens of context, and 54% from subagents. The fixed prompt re-read
on every request was about 27% of a later week: about 70K tokens in the main
session and 51K per subagent.

**What we concluded.** Agent count is the bill. Measure your own transcripts
before changing settings, and dedupe streamed records by message id. Shrink the
fixed prompt, compact earlier, and cap agents per round: one reviewer per fix
round, and one auditor with a list instead of one agent per item.

**Open.** Reasoning effort was not varied and measured. The cost split leaves
it little room to matter.

### Parallel agents in one checkout will damage each other's work

**What was tried.** Several agents shared one working tree.

**What the numbers showed.** There were four git incidents. A chained
`git status && git merge --ff-only` fast-forwarded onto another agent's branch.
A `git add -A` swept an agent's half-finished files into an unrelated commit.
An agent ran `git stash` to watch its tests fail and briefly reverted another
agent's files. The main session ran `git checkout` on a file and erased three
tests a writer had just delivered. Shared scratch paths did quieter damage: a
writer reported "83 passed" from a log another session overwrote, when the real
result was 87. Six agents on one tree produced three spellings of one idiom,
two byte-identical helpers, and a duplicate CSS class. The gate flagged none of
it.

**What we concluded.** Give each file surface one writer and each agent its own
scratch path. Enforce the git rules with hooks: one now refuses stash from any
caller, and checkout, restore, reset, and clean from subagents. Stage named
paths only. Review the combined diff for duplication across agents.

**Open.** Guardrails apply to the coordinating session too. It caused one of
the four incidents.

### A fix loop with no stop rule does not converge

**What was tried.** A 27-hour session ran fix lanes with no end condition.
Every reviewer note was built before landing, then re-reviewed.

**What the numbers showed.** Follow-ups per writer climbed to 1.42, against
0.35 to 0.48 in earlier sessions. Nine agents needed five or more rounds, and
one lane took 21 before it was frozen. Only 5 of 48 review passes were a clean
ship. On the rule-based reader, each round bundled four to six new phrasing
shapes, and rounds 2, 3, and 4 each broke something the committed version had
right.

**What we concluded.** Put the stop rule in the script before work starts.
Land what is accepted after 3 rounds, or after 2 with no net gain. File
ship-with-notes items instead of building them. Send a reviewer back only after
a block. Change one shape per round, and accept a round only at 0 gold
regressions.

**Open.** Short prompts leave out the stop rule. The median prompt here was 11
words.

### Renaming a project with agents is a data migration

**What was tried.** The project took the name ClearScout late in development.
The code change touched 1,102 files.

**What the numbers showed.** The rename had to keep honoring robots.txt groups
written for the former crawler name, migrate browser storage keys, and re-seal
an eval round. Eval digests hash a domain string that held the old name, so no
earlier row compares with a new one. A backfill rewrote 41,320 stored paths. A
move script's leftover check skipped one settings file, so a hook pointed at a
deleted folder and every shell call showed a hook error until it was fixed.

**What we concluded.** List every place the name lives: stored paths, browser
keys, eval digests, hooks, agent memory, and default lists. Migrate each with a
check. Afterwards, search config, hooks, and memory case-insensitively for the
old name. Re-baseline eval comparisons on purpose.

**Open.** Archives and compatibility code keep the old name by design. Agents
still read it there, so it keeps showing up in their work.

## Evals and LLM quality

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

**Open.** [METHODOLOGY.md](../evals/inspect/METHODOLOGY.md) states what each
mode cannot measure.

### One run of a judge is an anecdote

**What was tried.** The fabrication critic, the last check before an
application goes out, ran 18 times on one unchanged document with a real
distortion.

**What the numbers showed.** It caught the distortion 7 times: 38.9%, Wilson
95% interval 0.20 to 0.61. Each verdict either saw it or did not, so a looser
threshold would not help. The fix made the critic able to sample several times
and fail the draft on any doubt. Building it showed the provider response cache
was keyed on content alone, so three identical calls returned one answer. A
test that counts real provider calls failed `1 == 3` against the old code. On
the next model, one draw caught every planted defect on every draw, and three
draws only turned a one-in-three false doubt into a sure fail, so the shipped
default stays at one draw.

**What we concluded.** Run a judge many times on one fixed input and report an
interval. If verdicts split cleanly, sample several times and union them.
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
treated as ground truth.

**What the numbers showed.** The live critic scored 7 of 10, but it had judged
all 10 correctly: three "clean" fixtures held real unsupported claims. A reader
gold set showed alpha 0.973 between two blind annotators, but both were the
same model on the same instructions, and 106 of 631 shared reasons were
word-for-word identical. The match gold set went through about ten relabel
rounds, because each rubric edit shifted other borderline labels.

**What we concluded.** Check each scored miss against the source before
touching the prompt. Record that model annotators share a model. Settle
disagreements under written principles, and have a human review the rulings.
Freeze the rubric by digest during each prompt cycle, and allow one relabel per
cycle. A user's correction is calibration data, never gold.

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
Have the domain owner dispute real outputs, and group the disputes by
mechanism. Fix structure (order, definitions) rather than adding emphasis. Ask
a negative verdict, in the prompt, to cite the evidence it weighed. Re-run a
small batch with control rows that should not move.

**Open.** Prompt wording could not enforce the head-noun class. A code
validator requiring cited ids was rejected, because it invites fabricated
citations.

### Show each stage the rules it is judged by

**What was tried.** A paid cassette reseal failed on the critic's preservation
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
effort, and one wording fix recovered one.

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

**What the numbers showed.** One stage wrote 60,768 cache tokens and read back
none, because shared evidence sat in the per-call part of the request. A
response schema listed that run's ids, so every prefix was unique and 576,180
tokens were written that a stable prefix would have read. A two-sentence
docstring edit on a response model changed every request digest and broke the
recorded cassettes. The one-per-call harness cost about four times as much as
production batching and could never reproduce batch-mode errors.

**What we concluded.** Log cache reads and writes per call and compare them per
stage. Put stable instructions, evidence, and schema first, and check per-run
ids in code. Treat schema docstrings as prompt text. Have the harness render the
full production request before any paid run.

**Open.** Each cassette reseal costs money, so reseals come last in a cycle.

## Product design

### Make fabrication impossible to express

**What was tried.** Program-owned facts and closed lists of real evidence ids,
in place of an instruction to "only use the provided evidence".

**What the numbers showed.** Every CV entry gets an id derived from its content.
Response schemas are built at runtime as closed lists over those ids. The
program copies titles, employers, dates, degrees, and citations verbatim. Code
checks every cited id against the run's selected evidence and every quote
against the stored posting. On one queue review, 493 of 493 cited CV ids and
587 of 587 posting citations held. Models still tried to invent: participation
upgraded to delivery, a draft crediting the model's own "reasoning", and an
invented posting id. The checks caught each one.

**What we concluded.** Let the program own the facts. Let the model choose,
order, and rephrase. Enforce citations in the schema, check provenance in code,
and report "verified" and "good" as two measures.

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

### A written "synthetic only" rule does not keep real data out

**What was tried.** A written rule that tests and fixtures use synthetic data.

**What the numbers showed.** Before the repository went public, three auditors
compared the tree against the real private sources, and a skeptic agent tried
to refute each finding. 20 of 21 were confirmed. About 130 verbatim runs of
real posting text sat in tests, along with real CV sentences. In one incident
the coordinating agent had pasted real sentences into a writer's task, and the
writer copied them into tests. None of it was a secret or contact detail.

**What we concluded.** Give writer agents synthetic shapes, never live text.
Before publishing, run a dedicated sweep against the real private sources, with
an adversarial refuter to cut false positives. A written rule does not hold
across hundreds of agent-written fixtures.

**Open.** In a clinical setting the same failure would put patient data in a
repository.

### Keep "record it" separate from "ask a human about it"

**What was tried.** To lose no candidates, a change sent every title-only
listing it could not read into the review queue.

**What the numbers showed.** One free scan grew the queue from 32 to 1,695
items. One queue request took 12.9 seconds and returned 313 KB. The database
was restored from backup, and the restored queue answered in 0.35 seconds. The
first correction recorded weak-title listings as final rejections, but a
300-row sample included plausible roles. The opposite failure also happened:
most rows staged in one run were out of scope by location, because a path
skipped the early gate.

**What we concluded.** Store everything somewhere recoverable. Apply definite
gates at admission, using the publisher's structured fields as evidence. Send
only rows with real evidence to the human.

**Open.** Recall and queue size are different measures. Later dismissal does
not clean up what admission let in.

## If you build something similar

These are opinions, each backed by a lesson above.

- Turn a repeated correction into a check that runs on the write.
- Verify an agent's claim with a command or a query before it becomes the
  record.
- Run the query that could disprove a finding before you file or fix it.
- Review each agent's diff when it hands back, and list the assertions it
  changed.
- Feed every safety check the case it should block.
- Give each file one writer, each agent its own scratch path, and enforce git
  rules with hooks.
- Write the stop rule before the fix loop starts.
- Read eval samples one at a time, and pair every planted defect with a clean
  control.
- Let the program own the facts, and make the model cite ids that code checks.
- Replay a rule change over every stored input before accepting it.
- Keep real data out of agent tasks, then sweep for it anyway before you
  publish.
