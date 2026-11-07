# Building with agents

ClearScout and its sibling renderer, pressume, were built by one person
directing AI coding agents. The maintainer comes from a domain, not from a
career in software. This page collects what that taught, for people in the same
spot.

Each lesson stands alone and comes from a real incident in this repository. The
numbers describe the software, its tests, and its evals. Counts from the
maintainer's own job search appear only as rates or are left out.
[How it was built](contributing/agent-workflow.md) shows the agent setup itself.
Lessons about the program's own model pipelines, its evals (repeatable tests of
how well one model step does its job), grounding, and spend, are in
[Agentic workflows](agentic-workflows.md).

Most of these lessons match
[Anthropic's Claude Code best practices](https://code.claude.com/docs/en/best-practices), and each one that does links
there. What this page adds is measured numbers from one project: where the
agent bill went, how precise a comment check was, and how many rounds a fix
loop took.

## Building with AI agents

### More instructions did not stop a repeated agent mistake

**What was tried.** Agents kept writing bad comments: review codes, pointers to
other files, change narration, and docstrings longer than their code. Rules
were added to agent task instructions four times. The habit came back each
time. The comment checker knew three mechanical rules and ran only at gate
time (the gate is the project's full check script), after every agent had
finished.

**What the numbers showed.** The word "comment" showed up in the maintainer's
own turns in 52 of 86 sessions, a proxy for hand corrections. A check that runs
the moment an agent writes a file first flagged 240 hits. Sampling cut the
noisy rules: bare "now", "no longer", and "previously" had 0 true positives in
52 sampled hits. That left 138 real violations, fixed across about 85 files.

**What we concluded.** After the third identical correction, stop writing
rules. Turn the correction into a check that runs on the write and names the
file and line, and add a sheet of real good and bad examples. Measure the
check's precision on a sample before enforcing it. Anthropic's
[Claude Code best practices](https://code.claude.com/docs/en/best-practices) make the same point: a rule Claude keeps
missing belongs in a hook.

**Open.** The record measures the check, not whether the habit stopped. For a
sibling rule ("the main session never idles") the habit continued after a hook
(a script Claude Code runs automatically before or after a tool call, which
can refuse the call) was added. A new hook is not proof the problem is gone.

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
Another time the harness said exit 0 while the log showed 1 failure. A writer
blamed a flaky SPA test on focus returning before a refetch; tracing focus in a
passing and a failing run refuted that. The test focused a control that was
still disabled, which only a stray blur under load exposed.

**What we concluded.** Agreement among agents is not evidence, and neither is a
confident tone or an exit code. Make "is it really on main" a one-command
check. Settle a disagreement with a query. Take provenance (model, dataset
digest, rubric version) from what the system recorded. A digest is a short
fingerprint (a hash) of a file or request; any change makes a different one.
Read the end of the log before recording a pass. Anthropic's
[Claude Code best practices](https://code.claude.com/docs/en/best-practices) also ask for evidence, because "all tests
pass" on its own says very little.

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
data against the real wire schema. Agents gaming their own checks is a known
behavior; [METR](https://metr.org/blog/2025-06-05-recent-reward-hacking)
reports models tampering with scoring code or sidestepping the task.

**Open.** Review saved for the end is the most expensive review, because one
reviewer then reads 70 files at once.

### Have a specialist read each paid result before you act on it

**What was tried.** In one pre-release round of about 27 paid eval runs, every
result that would change a shipped setting or prompt went to a read-only model
review agent before it landed.

**What the numbers showed.** The review changed the decision four times. It
blocked a switch to low reasoning effort (how much the model may reason before
it answers) because the winning arm had run on an
older prompt and had never been measured on clean documents. It found a new
prompt sentence untested on its hardest shape, and the clause and probe that
followed went 42 of 42. It blocked a check that counted program-copied lines as
the model's own writing. On the recorded replays it found a third-party
registry page for an individual among the real pages a live search returned.
A read-only doc sweep after landing found the largest stale claim in a file
outside the list it was handed.

**What we concluded.** Paid evidence is cheap next to the decision it drives.
Give one specialist reviewer the result and the diff, ask for ship, ship with
notes, or block, and file its notes rather than building them on the spot.
Brief a reviewer broadly enough to look past its list.

**Open.** A reviewer is one more model reading. Each of the four calls above
was confirmed by a run, a test, or a direct read before it changed anything.

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
before changing settings, and dedupe streamed records by message id. Anthropic's
[Claude Code best practices](https://code.claude.com/docs/en/best-practices) note the cause: every request sends the whole
conversation. Shrink the
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
it. Worktrees (second checkouts of the same git repository, each in its own
folder) fix sharing but leave copies behind: an eval log recorded its
dataset path inside a worktree, and a free rescore (running the scorer again
over model outputs already saved, which costs nothing) after a relabel read that
old copy and reported no change until the worktree was removed.

**What we concluded.** Give each file surface one writer and each agent its own
scratch path. Enforce the git rules with hooks: one now refuses stash from any
caller, and checkout, restore, reset, and clean from subagents. Stage named
paths only. Review the combined diff for duplication across agents. Anthropic's
[Claude Code best practices](https://code.claude.com/docs/en/best-practices) also run parallel sessions in worktrees.

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
a block; the
[Claude Code best practices](https://code.claude.com/docs/en/best-practices)
warn that a reviewer asked to find problems will always find something. Change one shape per round, and accept a round only at 0 gold
regressions (cases a person had already decided the answer to, which the round
now gets wrong).

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
Long after the rename, the tool that rebuilds an eval's frozen selection
refused its own archived input, because the old name sat inside domain-separated
hashes as well as a version string. Rebuilding the frozen selection byte for
byte from a migrated private copy was the proof required before amending it.

**What we concluded.** List every place the name lives: stored paths, browser
keys, eval digests, hooks, agent memory, and default lists. Migrate each with a
check. Afterwards, search config, hooks, and memory case-insensitively for the
old name. Re-baseline eval comparisons on purpose.

**Open.** Archives and compatibility code keep the old name by design. Agents
still read it there, so it keeps showing up in their work.

## Product and process

### A written "synthetic only" rule does not keep real data out

**What was tried.** A written rule that tests and fixtures use synthetic data.

**What the numbers showed.** Before the repository went public, three auditors
compared the tree against the real private sources, and a skeptic agent tried
to refute each finding. 20 of 21 were confirmed. About 130 verbatim runs of
real posting text sat in tests, along with real CV sentences. In one incident
the coordinating agent had pasted real sentences into a writer's task, and the
writer copied them into tests. None of it was a secret or contact detail.
Recordings of live calls are the other route in. These are cassettes: saved
copies of a real request and the provider's reply, which the tests replay
instead of paying for a new call. A recorded web search carries
whatever real pages the search returned. The audit allows real employer names
only in those recordings, and a hand read of each one still found a page about
an individual that had to come out.

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

These are opinions. Each links the lesson above that backs it.

- After the third identical correction, stop adding instructions and build a
  check that runs on the write and names the file and line.
  ([lesson](#more-instructions-did-not-stop-a-repeated-agent-mistake))
- Treat an agent's report, a confident tone, and an exit code as leads. Confirm
  each with a command, a query, or the log before it becomes the record.
  ([lesson](#treat-an-agents-report-as-a-lead-to-check))
- Before you file or fix a finding, run the one query that could prove it
  false, and put its count in the finding.
  ([lesson](#run-the-query-that-could-kill-the-premise-first))
- Review each agent's diff when it hands back, and treat every assertion it
  flipped or deleted as a product decision.
  ([lesson](#a-green-gate-is-not-a-review))
- Send each paid result that would change a shipped setting to one specialist
  reviewer before it lands, and file its notes.
  ([lesson](#have-a-specialist-read-each-paid-result-before-you-act-on-it))
- Feed every safety check the case it should block and watch it refuse; a
  check that has never fired is unproven.
  ([lesson](#a-safety-check-that-has-never-fired-may-be-unable-to-fire))
- Budget by agent count, not by model price: every agent re-reads its context
  on every request. Use one reviewer per round and one auditor per list.
  ([lesson](#the-bill-comes-from-agents-re-reading-context))
- Give each file surface one writer and each agent its own scratch path, and
  enforce the git rules with hooks rather than instructions.
  ([lesson](#parallel-agents-in-one-checkout-will-damage-each-others-work))
- Write the stop rule into the fix loop before it starts, and land what is
  accepted when it fires.
  ([lesson](#a-fix-loop-with-no-stop-rule-does-not-converge))
- Plan a rename as a data migration: list every place the name is stored, and
  migrate each with a check.
  ([lesson](#renaming-a-project-with-agents-is-a-data-migration))
- Give agents synthetic shapes, never live text, sweep for real data against
  the private sources before you publish anyway, and read every recording of a
  live call before committing it.
  ([lesson](#a-written-synthetic-only-rule-does-not-keep-real-data-out))
- Keep everything recoverable, but send a person only the rows with real
  evidence; apply definite gates at admission.
  ([lesson](#keep-record-it-separate-from-ask-a-human-about-it))
