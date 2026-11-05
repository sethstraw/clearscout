# Agent instructions

These instructions are public project guidance for AI coding agents working on
ClearScout. Private maintainer notes belong outside the repository or in ignored
local files.

## Product boundary

ClearScout is a local job-search tool. `README.md` describes the loop it runs and
`docs/architecture.md` explains why it is shaped that way; read those rather than a
summary here.

`pressume` renders Markdown in a separate sibling project. The two applications
stay separate, and ClearScout stays free of any dependency on `pressume`.

## Non-negotiables

- Anti-fabrication is the product. Matching and drafting may cite only
  identifiers that exist in the selected CV evidence for that run.
- Titles, employers, dates, education, and publication citations are copied
  verbatim by the program; a model may reorder and select, and the program
  supplies the text.
- The model drafts and critiques. Deterministic orchestration, selection, and
  checks stay in application code.
- Personal data stays out of the repository. Use synthetic CVs, employers,
  postings, provider responses, and settings in tests and docs.
- Prompts are versioned files under `prompts/`, not inline strings.
- Provider SDKs are the official OpenAI and Anthropic SDKs, called directly.
  A wrapper abstraction such as LiteLLM stays out.
- URLs identify postings, and published policy decides reads. Host
  allowlists and denylists come from that policy at runtime.
- Anything crossing the HTTP wire uses typed contract models. The SPA derives
  closed vocabularies from the generated schema.

## First files to read

Before planning a code change, read:

- `README.md` for the user-facing workflow and current release state.
- `docs/architecture.md` for the maintainer view.
- `docs/contributing/backend-development.md` before placing backend code or tests.
- `docs/contributing/frontend-development.md` before changing the SPA.
- `.github/CONTRIBUTING.md` for test, privacy, AI-use, and review expectations.

## Development workflow

Preserve existing uncommitted work. Committing, pushing, publishing, calling a
live provider, and spending money each wait for the maintainer to ask.

Use focused checks while developing. The public verification interface is:

```bash
scripts/gate.sh [fast|change|related|py|web|generated|security|package|workflows|push|all]
```

`scripts/generate.sh` is the explicit mutating command for generated artifacts.
The gate verifies generated artifacts from temporary outputs and should not
rewrite tracked files.

Tests must be able to fail. Assert the user-visible or caller-visible outcome,
not implementation steps, unless the call itself is the contract. Use realistic
synthetic fixtures and keep private facts out of the suite.

Tests never wait on the clock: no sleep, no polling until a deadline, and no
raised timeout. The wait passes alone and expires under a full parallel run.
Drive time with a controlled clock or with events the test resolves itself. A
timing-dependent test near the code you touch is fixed in the same change; a
test that only fails under load is a defect in the test. `tests/AGENTS.md` and
`web/AGENTS.md` give the mechanics for each suite.

Keep live provider, keyring, job-board, and renderer checks out of ordinary
automation. Live checks are private, deliberate, and cost money.

Commit messages are a single subject line. Add a short body only when the why
cannot fit in the subject.

First-time setup: `cp .envrc.example .envrc && direnv allow` puts
`.venv/bin` and `scripts/` on `PATH` on `cd`. `mise install` provisions python and node at the
versions `mise.toml` pins, and the latest uv. Common project commands live in
the `justfile` (`just --list`), delegating to `scripts/gate.sh` and friends.

## Editing rules

Use repository patterns before adding new abstractions. Keep changes scoped to
the module and product surface implied by the task.

Search before creating. For every proposed file, service, model, helper,
component, hook, or CSS class, find the existing owner and its consumers first.
Extend or move one coherent owner instead of adding a parallel implementation.
A new generic frontend component or hook needs at least two production domains;
a single-consumer surface belongs with its feature. A new top-level Python
module needs a cross-capability responsibility that no existing package owns.

Before finishing, inspect the complete diff and inventory new files and public
symbols. Check that names express one responsibility, dependencies point toward
the owning layer, and similar code represents different change axes
before leaving it duplicated. Static-analysis findings are evidence for this
review, not permission to delete framework-discovered or string-referenced code.

When a symbol-aware code server such as Serena is available, use it for
source-code discovery and symbol-level edits. Any grep for `def ` or `class `
or a "where is X defined" question goes to its symbol lookup first, then its
pattern search, then shell `rg` as a last resort. Use `rg` and ordinary file
tools for documentation, configuration, generated files, CSS, and literal
strings. Prefer structured parsers over ad hoc string manipulation where the
toolchain provides one.

Docstrings on Pydantic models that cross the HTTP wire are copied into the
generated JSON schema and the SPA client. Put cross-references in a comment
above the class, not in the docstring.

Leave unrelated work as you found it. Destructive Git commands wait for the
maintainer to ask for that specific operation.

## Style and documentation

Use ASCII in docs, comments, commit messages, and UI strings. Write plainly
about limits, in a working voice.

Comments are one to three lines and state non-obvious present facts. A
docstring is shorter than the code it documents, and a public function,
method, or class keeps a short one. `scripts/check_comment_policy.py` enforces
the line cap. Historical notes, measurement logs, and defensive essays belong elsewhere. A
TODO, FIXME, or HACK carries an issue reference (`#123`) or an owner
(`@name`).

Documentation describes what the code does now, and points at an executable
artifact rather than restating it in prose. If code and docs disagree, correct
the stale source or state the real limit.

## Agent roles and recipes

`.agents/roles/` defines the agent roles (writer, auditor, researcher, and the
four reviews), with when to dispatch each, its boundaries, and what it
returns. `.agents/skills/` holds the task recipes. Before editing under
`src/`, `tests/`, or `web/`, read that directory's `AGENTS.md`; Codex loads only
the files between the root and its start directory.
[docs/contributing/agent-workflow.md](docs/contributing/agent-workflow.md) explains how they fit
together. One writer owns each file surface at a time, and `cto-review` is
required before a diff lands on the surfaces its role file names.

## AI assistance

AI assistance is allowed, but generated output is untrusted contribution
material. Follow `docs/contributing/ai-assisted-development.md`: disclose substantial AI use
in pull requests, review the output, respect licenses, and keep private data and
secrets out.

## Prompt evaluation

Prompt changes run through `scripts/eval.sh`. Its usage block lists the modes,
`evals/inspect/README.md` shows how to run each one, and
`evals/inspect/METHODOLOGY.md` says what each one measures. Only `--live` tests prompt
quality. It spends money and needs the maintainer's approval for each run and
its stated scope; cost estimates are advisory and never stop the run. Read
`evals/inspect/METHODOLOGY.md` before writing a scoring dataset: rulings are
calibration data, not gold labels.
