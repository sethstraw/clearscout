# How it was built

Claude Code and Codex did most of the typing on TailorCV. This page is the
setup used to review that work: who does what, what stops them, and what I
still do by hand. It does not make generated code safe by itself.

The files are real and tracked. If you open this repository in either tool, you
get the same roles, recipes, and guards I use.

You can borrow a review role or a guard without using TailorCV. These are the
working files, not a separate example written for this page.

## One coordinator, bounded workers

A session has one coordinator. It owns the plan, the integrated diff, and the
gate. It hands bounded work to a role, then verifies the result before anything
lands. A worker's report is a claim, not evidence. A role never commits and
never writes project records, meaning findings, session notes, and other
records of the work; it returns what it found, and the coordinator writes them.

| Role                                                              | Does                                                              | Never does                                  |
| ----------------------------------------------------------------- | ----------------------------------------------------------------- | ------------------------------------------- |
| [writer](../../.agents/roles/writer.md)                           | Implements one agreed change on a named set of files              | Decides what to build or runs the full gate |
| [auditor](../../.agents/roles/auditor.md)                         | Answers "what is actually there" across many files                | Judges a design or writes anything          |
| [researcher](../../.agents/roles/researcher.md)                   | Finds outside evidence: how a board, provider, or library behaves | Locates code                                |
| [cto-review](../../.agents/roles/cto-review.md)                   | Technical review; required on the surfaces its role file names    | Edits                                       |
| [cpo-review](../../.agents/roles/cpo-review.md)                   | Product review of workflow, labels, scoring, and priority         | Reviews implementation                      |
| [cuxo-review](../../.agents/roles/cuxo-review.md)                 | Rendered behavior, layout, accessibility, visible copy            | Reviews backend-only changes                |
| [llm-engineer-review](../../.agents/roles/llm-engineer-review.md) | Prompts, provider behavior, eval scoring, grounding               | Starts a paid run                           |

The role text is tool-agnostic. `.claude/agents/` holds a thin adapter for each
role that sets a model and tool list, then points at the role file.
[`.codex/agents/`](../../.codex/agents/) does the same for Codex, one profile per
role, each naming the role file it runs.

This is the orchestrator-workers shape Anthropic describes for its own
[multi-agent research system](https://www.anthropic.com/engineering/multi-agent-research-system):
a lead delegates to workers that each start with their own context and hand
back a condensed result. The clean context is the point. A writer that never
saw the whole conversation cannot be talked into anything by it. The cost is
real too: the same post measures multi-agent runs at roughly fifteen times the
tokens of a chat, which is why workers get bounded tasks and not open ones.

## Why panels

I learned the panel habit curating drug data. Two curators work the
same record without seeing each other's answer. Where they disagree is where the
record was hard, so the disagreement is the useful part. A third person settles
it and writes down why. Nothing goes in without its source.

The development reviews borrow the first rule. A change to admission, queues,
persistence, or the HTTP contract gets a technical review. A change to what the
app claims or recommends gets a product review. Rendered changes get an
experience review, and prompt or provider changes get a model-behavior review.
Each reviewer is a distinct agent, dispatched on its own, and reports to the
coordinator, which settles conflicts. The gate settles facts.

The evaluation labels follow the first two rules. Two reviewer agents label the
same requirements without seeing each other's labels, a recorded adjudication
with its rationale settles each disagreement, and their agreement is measured
with Cohen's kappa. Who adjudicates differs by label family, and the
[methodology](../../evals/inspect/METHODOLOGY.md) says which. That is the double
annotation and adjudication that text-labeling research has used for years; see
[Artstein and Poesio](https://aclanthology.org/J08-4004/) on agreement and
[Finlayson and Erjavec](https://arxiv.org/abs/1602.05753) on the annotation
cycle. The labels are still agent labels, not human gold, and the methodology
says so.

The third rule belongs to the app: a drafted line must cite the selected CV.
The person still checks whether its wording says more than the source supports.

A panel is only independent if its members fail differently.
[Verga et al.](https://arxiv.org/abs/2404.18796) found a panel of models from
different families less biased toward its own outputs than one large judge.
TailorCV's reviewers must be distinct agents, but nothing forces them onto
different model families.

## One writer per surface

Parallel writers get disjoint file sets, and no two agents write the same file.
Two agents sharing a checkout can rewrite each other's work without either one
noticing or reporting it.

So no agent can stash, and a subagent cannot run git that rewrites the working
tree. When two sessions need the same repository at once, one of them moves to
a worktree.

## Route by the cost of a wrong answer

The coordinator runs on a strong model, because it judges everyone else's
work. Writers, reviewers, and search agents run a strong model too: a search
that misses a file reports a clean answer, and the coordinator acts on it. A
route changes only after measuring outcomes: whether the gate passed and how
much rework followed. Token counts alone do not count.

## Recipes

[`.agents/skills/`](../../.agents/skills/) holds the procedures that kept coming up:
migrations, API test scaffolds, OpenAPI regeneration, generated-artifact drift,
TanStack Query audits, UI debugging, and a command card with the verified shape
of every check. Both tools read the same `SKILL.md` files; `.claude/skills` is a
link to that directory.

## Guards

[`.agents/hooks/`](../../.agents/hooks/) holds small scripts that refuse bad habits
before they cost anything. All of them are registered for Claude in
`.claude/settings.json`, and that registration is exercised on every session.
`.codex/hooks.json` registers the PreToolUse guards
`block-piped-test-output`, `block-cat-heredoc`, `block-shared-checkout-git`,
`block-credential-reads`, and `block-extra-migration`, and the Stop checks
`stop-test-collect` and `stop-check-instruction-paths`. Codex asks for trust in
each project hook before it runs. The guards refuse with exit code 2 and a
stderr reason, or with the PreToolUse deny JSON; both tools honor both. Codex's
PreToolUse input carries no `agent_id`,
which only its SubagentStart and SubagentStop events have, so the
subagent-only refusals never fire under Codex. Codex's
default `workspace-write` sandbox keeps `.git`, `.agents`, and `.codex`
read-only, so a sandboxed command cannot write the index, refs, or stash.

- Test output piped into `grep` or `tail` is refused. The run already happened;
  read its log instead of paying for it twice.
- `cat` heredocs that write inside the repository are refused in favor of the
  editor's own write tools.
- `git stash` is refused for everyone. Working-tree-rewriting git and the full
  gate are refused for subagents.
- Reading a credential file from the shell and writing a second migration
  revision are refused.
- A review agent keeps notes in its own memory directory. Agent memory can turn
  Write and Edit back on for it, so a write anywhere else is refused.
- A file written whole with Write is formatted; a patch from Edit is not, and
  the gate checks formatting. A subagent's comments are checked against the
  comment policy as it writes them with Write, Edit, or a Serena edit, in every
  file type that carries comments.
- At the end of a turn, the instruction files are checked for paths that no
  longer exist, changed tests are checked for collection, and substantive work
  done without reading a first-read document draws a nudge.
- A session opens with a read-only record of the working tree, and compaction
  snapshots the repository state first.

In a clone that opts into the Git hooks with
`git config core.hooksPath .githooks`, `.githooks/commit-msg` refuses AI
attribution trailers and non-ASCII text in a commit message, whichever tool
wrote it. Disclosure lives here and in the README rather than in every commit.

## The gate decides

[`scripts/gate.sh`](../../scripts/gate.sh) is the arbiter. It runs the suites,
type checks, import contracts, generated-artifact drift, the comment policy
across every comment-bearing file type, the public-data audit, and the
dependency and license audits. An agent that says a change works has made a claim; the gate is where the claim gets checked.
Tests have to be able to fail, so a test that could never go red is a defect in
the test.

## What stays private

My personal overlay is not in this repository: a local `CLAUDE.local.md`, a
local settings file, my job-search records, and a personal-data guard whose
pattern list names real employers. That guard cannot be published, for reasons
that are its entire job.

## What this does not prove

None of this makes generated code correct. It makes mistakes cheaper to catch
and harder to ship quietly. The review agents catch a lot, but reviewers that
share a model family share its blind spots. When they all say yes, that is
worth remembering.
