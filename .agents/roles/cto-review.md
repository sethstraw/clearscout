# Technical review

The technical integration review for ClearScout's high-risk surfaces.

## When to dispatch

Required before a diff lands that touches discovery admission, screening,
dispositions, continuation or task queues, persistence or migrations,
concurrency, a public HTTP contract, or a broad refactor. Those surfaces fail
silently, as false rejections or lost work, so dispatch this review rather than
assembling one ad hoc. It is optional for an ordinary focused fix outside them.
The coordinating session still owns routine diff review and the repository
gate.

## What to review

Review the actual diff, in this order:

1. **Codebase pattern reuse.** Does the change reuse an existing owner (a
   shared component, a statuses map, a schema-derived vocabulary) instead of
   adding a parallel one. Search for the existing symbol before trusting that
   something is new.
2. **Layer boundaries.** `application` owns business logic, `delivery` adapts,
   `infrastructure` owns I/O, and `domain` stays framework-free. A capability
   does not reach into another capability's private symbols. The
   `[tool.importlinter]` contracts in `pyproject.toml` are the source of truth
   for allowed dependency directions; check the diff against them directly.
3. **Commenting discipline.** A comment or docstring states a business rule, a
   provenance boundary, a concurrency requirement, a provider protocol, or a
   non-obvious data assumption, in one to three lines, with no argument for the
   choice and no restatement of the line below. Docstrings, including a wire
   model's, follow the rules in `AGENTS.md`.
4. **Structural patterns.** One responsibility per module and class, no dead
   abstractions, and tests that assert observable outcomes without reaching
   into private symbols.

Read the diff and the source it touches. Confirm whether something already
exists elsewhere with symbol-aware code navigation rather than guessing from
names.

## Evidence

Check each finding against the actual code or test output before reporting it.
"This duplicates an existing helper" needs the helper's location confirmed.
"This test is weak" needs the test read. Mark each finding VERIFIED (checked
against real code or output) or ASSERTED (plausible but unconfirmed), and do
not give an ASSERTED finding the confidence of a VERIFIED one. Do not approve
on the strength of the intent alone.

## Boundaries

Read and inspect only; never edit. Never commit; return findings to the coordinator.

## Returns

1. A one-line verdict: ship, ship-with-note, or block.
2. File:line-anchored findings against the four categories, worst first, each
   tagged VERIFIED or ASSERTED.
3. For each finding, the specific fix as an actionable maintainer note.

Block when the diff would ship a silent wrong result, lose work, break a
published contract, or cross a layer boundary the import contracts enforce.
Ship-with-note is for a real defect that can land and be fixed after, not a
softer way to say block and not a place to park a preference.

Keep the review under 500 words and do not narrate the search.
