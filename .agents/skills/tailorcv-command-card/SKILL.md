---
name: tailorcv-command-card
description: The command shapes for running TailorCV tests, linters, type checks, and generators. Use before running any check, test, lint, type, or generation command in a TailorCV checkout, and when a command fails in a way that looks like the command shape rather than the code. Do not use for product or architecture questions.
user-invocable: false
---

# TailorCV command card

Run every command from the repository root. When a command fails because of its
shape, use the form below rather than trying variants.

## Python tests

- Focused: `just pytest-log <name> <files>`, then read the log it names, once.
  The log is `/tmp/tailorcv-pytest/<checkout>/<name>.log`, where `<checkout>`
  is the checkout directory's name, and ends with `N passed` or a failure
  summary and `exit=N`. Do not rerun to see a result you already have.
- From a worktree:
  `just --justfile <worktree>/justfile --working-directory <worktree> pytest-log <name> <files>`,
  with `<files>` relative to the worktree. The log lands under the worktree's
  own directory name, so it never collides with main's. `uv` finds
  the worktree's own environment, so a worktree with no synced `.venv` fails
  with `Failed to spawn: pytest`. Never prefix `cd <path> && ...`.
- Never add `-q`. The pyproject `addopts` already carries it, and `-q -q`
  deletes the `N passed` summary line.
- Profiling: `--durations=N` splits setup, call, and teardown per test. That
  same `-q` cancels the `-vv` the pytest docs tell you to pass, so rows under
  5ms stay hidden until `-vvv`. Sum the setup rows to see fixture cost.
- Never use `-p no:xdist`. `addopts` passes `-n auto`, so pytest exits 4 with a
  usage error. `-n0` is the serial switch.
- Red then green: make every behavior-changing test fail in one mutated run
  that names all of them, restore, then one green run.
- Full suite: `just test`.

## Lint, format, and types

- Python: `uv run --no-sync ruff check --fix <files>`, then
  `uv run --no-sync ruff format <files>`. Fix the residue by hand.
- SPA: `npm --prefix web exec -- eslint --fix <files>`, then
  `npm --prefix web exec -- prettier --write <files>`, with paths relative to
  the repository root. `npm --prefix web run format` rewrites the whole
  repository, so leave it out of a shared checkout.
- Python types: `uv run --no-sync mypy` with no file arguments. The project
  config already lists the checked directories.
- SPA types: `npm --prefix web run typecheck`. `tsc -p web` names a solution
  file and checks nothing.

## SPA tests

- Focused Vitest:
  `npm --prefix web run test -- src/features/<area>/<X>.test.tsx`, with the
  path relative to `web/`. The `test` script is already `vitest run`.
- Redirect a long run to a log in a scratch directory such as
  `$TMPDIR/<task>/vitest.log` and read the log, rather than piping it through
  `grep` or `tail`.

## Before reporting a change

- `uv run --no-sync python scripts/check_comment_policy.py` fails a comment
  block over three lines. Comments state a present, non-obvious fact.
- `uv run --no-sync python scripts/audit_public_data.py` checks for personal
  data. Test employers come from the synthetic allowlist in that script.
- `uv run --no-sync python scripts/check_markdown_links.py` checks Markdown
  links.

## Generation and the gate

- `scripts/generate.sh` (or `just gen`) rewrites generated artifacts. Run it
  only when a wire model, route, or other generator input changed.
- `scripts/gate.sh <scope>` (or `just gate <scope>`) is the verification
  interface; the usage line in its header lists the scopes. The gate verifies generated artifacts
  from temporary outputs and does not rewrite tracked files. It runs from the
  coordinating session; a hook refuses it from a subagent.
- Run `scripts/gate.sh` bare, in the client's background mode: no `>` redirect
  and no echo of the exit code.

## Environment

- Use `uv run --no-sync` so a check does not change the environment. Do not
  `uv add`, `uv sync`, `npm install`, or `pip install` as part of a check.
- The system `python3` may be older than the project. Use
  `uv run --no-sync python`.
- Open SQLite read-only from Python with
  `sqlite3.connect("file:<path>?mode=ro", uri=True)`. Use a scratch data root,
  never a real workspace, for debugging. An inherited `TAILORCV_DATABASE_URL`
  beats `--data-root`, so pass `--database-url` for the scratch database too.
- Keep scratch scripts and logs in a scratch directory such as
  `$TMPDIR/<task>/`, not in the repository.
