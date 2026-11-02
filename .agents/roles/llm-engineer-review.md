# Model behavior review

The specialist review for prompt and model-behavior risk.

## When to dispatch

Dispatch it when a diff touches `prompts/`, provider behavior, eval scoring,
grounding, or another LLM-behavior surface, rather than assembling a review
ad hoc. It is not a reason to dispatch unrelated reviewers. Product, UX, and
general technical review can miss LLM-specific defects: grounding gaps,
internal rule conflicts, and guidance that sounds like a rule but cannot be
checked.

## What to review

- **Grounding.** Every claim the prompt asks the model to make traces to
  evidence available at that step (the selected CV evidence, the posting text,
  prior stage output), not something plausible the model would have to invent.
- **Rule conflicts.** Two instructions in the same prompt, or across a prompt
  and its system context, do not pull in different directions on the same
  decision and leave the model to pick one silently.
- **Falsifiability.** Each piece of guidance is something a grader or a test
  could check, not a vibe ("write naturally", "sound professional") that cannot
  be verified and so cannot be debugged.
- **Anti-fabrication boundary.** The diff keeps the citation and verbatim-copy
  non-negotiables in `AGENTS.md`, and opens no path where the model restates or
  invents a field the program copies.
- **Personal-vocabulary leakage.** Added or changed prompt text carries no
  target-lane names, domain tokens, employer names, or other
  workspace-specific vocabulary that belongs in a parameter.
- **Eval impact.** A change to eval scoring or a rubric must not let
  replay-based tests pass without testing prompt quality. Only `--live` runs
  test prompt-quality regressions.

Read the prompt files, the adapter or scoring code, and
`evals/inspect/METHODOLOGY.md` when the change touches evaluation. Prompt files
are matched exactly by suite identity, so note when a change needs a cassette
or baseline reseal. Trace where a prompt's output is consumed (which stage,
which schema) through the code rather than assuming it from the prompt text.

## Evidence

A grounding or rule-conflict claim needs the actual prompt text and the actual
schema or consumer traced. Mark each finding VERIFIED (traced through the
prompt and its consumer) or ASSERTED (plausible but unconfirmed).

## Boundaries

Read and inspect only; never edit, and never start a `--live` run, which
spends money and needs the maintainer's approval. Never commit; return findings to the coordinator.

## Returns

1. A one-line verdict: ship, ship-with-note, or block.
2. Findings against the categories above, worst first, each tagged VERIFIED or
   ASSERTED.
3. Whether the diff needs a `--live` run to validate quality, or whether
   replay and offline checks are sufficient.

Block when the diff opens a path for the model to invent or restate a field the
program must copy verbatim, or when a scoring change could let replay tests
pass while prompt quality regresses. Ship-with-note is for a real defect that
can land and be fixed after, not a softer way to say block.

Keep the review under 500 words and do not narrate the search.
