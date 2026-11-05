# Contributing

This grew out of one person's job search, so your use will find assumptions
mine did not. A report with a small synthetic example helps even without code.

Small, focused changes review best. Open an issue before substantial work on a
new capability or a product behavior change. Security problems use the private
route in [SECURITY.md](SECURITY.md).

Strip real CV text, employers, postings, provider responses, keys, and local
paths from every issue, fixture, screenshot, and log. `.gitignore` is not a
privacy boundary: an ignored file is still private data on disk.

## The product boundaries do not move for convenience

- Matching and drafting may cite only evidence identifiers selected from the
  master CV for that run. The program copies fixed facts.
- A model drafts and critiques. Application code owns orchestration, selection,
  policy, and deterministic checks.
- Prompts are versioned files under `prompts/`, not inline strings.
- Provider adapters call the official OpenAI and Anthropic SDKs with no wrapper.
- Published source policy decides reads. A URL stays the posting identifier
  even when the read is refused.
- HTTP payloads use typed contract models. The SPA derives closed vocabularies
  from the generated schema.
- `pressume` is an external renderer that ClearScout neither bundles nor depends
  on. Explicit rendering actions may run an installed renderer with fixed
  arguments.

A smaller change that weakens one of these is still a regression. The evidence
and approval model is in
[docs/truth-and-provenance.md](../docs/truth-and-provenance.md).

## Set up one clone

The supported toolchain is Python 3.12 or newer, Node 24, npm 11, and
[uv](https://docs.astral.sh/uv/). Build the web bundle first, because the SPA
ships inside the Python package.

```bash
npm ci --prefix web
npm run --prefix web build
uv sync --all-groups
```

`clearscout serve`, `clearscout worker`, and the other commands default to
`ClearScout/` in the Documents directory, in a clone too, unless you pass
`--data-root`. `scripts/clearscout-local-app` is the one launcher that uses the
ignored `local-data/` directory, unless `CLEARSCOUT_DATA_ROOT` is set. Keep
development data synthetic.

To run without a real CV, seed a scratch data root as
[Try the synthetic workspace](../README.md#try-the-synthetic-workspace) shows.
Background work needs a worker on the same root, in a second terminal:

```bash
uv run clearscout worker --data-root /tmp/clearscout-demo
```

Neither the server nor the worker creates or upgrades the database on its own.

## Make a focused change

Read the owner doc for the surface first:

- [backend development](../docs/contributing/backend-development.md) for Python
  placement, dependency direction, and test seams;
- [frontend development](../docs/contributing/frontend-development.md) for SPA
  feature ownership, generated contracts, CSS, and browser checks;
- [prompts/README.md](../prompts/README.md) before changing prompt bytes; and
- [evals/README.md](../evals/README.md) before changing cases, scorers, or baselines.

Find the existing owner before adding a module, service, hook, component, CSS
class, or document. Tests assert the caller-visible or user-visible outcome,
use realistic synthetic fixtures, and must be able to fail for the defect they
name.

Run `scripts/generate.sh` only when generated output should change. The gate
writes temporary output and compares it with the tracked OpenAPI, TypeScript,
notices, and other generated artifacts. Verification must not mutate the
checkout.

## Run the public gate

The verification interface is `scripts/gate.sh <scope>`. The usage block at the
top of [scripts/gate.sh](../scripts/gate.sh) lists every scope and what it covers.

Run `scripts/gate.sh change` before opening a pull request. It covers everything
an edit can break. `push` covers slower checks whose answer can also change outside
the source tree. The script keeps running after a failure, so one pass reports
everything.

The gate calls no provider and spends no money. The security, workflow, and
package steps fetch advisory data and pinned tools. Ordinary pytest, web tests,
scripted evaluations, and CI remove provider credentials and refuse network
paths that could spend money. Do not weaken that to make a test pass.
[Validation status](../docs/validation.md) says what each kind of evidence
does and does not prove.

Ruff formats Python. Prettier formats TypeScript, TSX, CSS, JSON, Markdown,
YAML, and HTML. Prompt files are excluded from Prettier because their exact
bytes are part of suite identity.

```bash
npm run --prefix web format:check
```

The hooks in `.githooks/` are opt-in, since installing them changes your clone:

```bash
git config core.hooksPath .githooks
```

The pre-commit hook runs `fast` and the pre-push hook runs `push`. The
commit-msg hook refuses non-ASCII text or AI attribution in a commit message.

## Prompt changes need two kinds of evidence

`uv run python -m evals` runs the shipped stages against a scripted provider and
compares results with recorded baselines. It checks deterministic ledger
consistency and named failure modes. It does not show that a live model
understood a changed prompt.

Run `scripts/eval.sh` for prompt work. Read
[evals/inspect/METHODOLOGY.md](../evals/inspect/METHODOLOGY.md) before preparing
a scoring set. A live run is a separate paid operation that needs explicit
operator approval for that run. Do not use `--live` in ordinary contribution
work.

Regenerate a baseline only when intended behavior changed. Read every line of
the diff and explain why each measurement moved. Re-recording because the
suite is red deletes the evidence of the regression.

## Build synthetic browser evidence

Use the demo seed from
[Try the synthetic workspace](../README.md#try-the-synthetic-workspace) for
reproducible screenshots and demo records. Pass `--port` to `clearscout serve`
to run a screenshot workspace beside another server.

The seed refuses a populated database unless you pass `--force`. It makes no
network or provider call. Keep fixtures and migration experiments synthetic.

With the workspace owner's explicit permission, docs may use a live screenshot.
Before capture, remove employer names, identifying domains, logos, contact
details, and unapproved personal facts, including in descriptions, notes, and
tooltips. Replace text with an opaque label, not a blur. Review the saved image,
keep unredacted captures private, and label the result a redacted live
workspace. Job titles may stay if the owner approves. A redacted image is not
synthetic test data.

## Schema changes are explicit

`v0001_schema.py` is the 0.1.0 schema. Applied revisions are frozen, and a
schema change adds a forward revision. Commands and recovery rules are in
[docs/contributing/database-and-migrations.md](../docs/contributing/database-and-migrations.md).

Run migration work only against a scratch data root. A change needs upgrade and
recovery coverage, and must explain any immutable boundary it moves.

## AI assistance remains reviewable work

AI assistance is allowed. Disclose substantial use in the pull request, review
the output, check licenses and copied material, and keep private information
out of prompts and artifacts. Do not submit model output nobody has read. The
policy is in
[docs/contributing/ai-assisted-development.md](../docs/contributing/ai-assisted-development.md).

## Open the pull request with the limits intact

State the problem, the change, the checks you ran, and any remaining limit or
risk. Keep unrelated cleanup separate. A prompt change includes before and after
output on the same input. An importer change includes a synthetic source fixture
and an assertion about the assembled master CV.

Dependency changes include both applicable lockfiles, the license audit, and any
regenerated notices. Shipped imports decide which license policy applies, so do
not move a dependency to development scope to get a looser one.

Participation is covered by [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).

## Releases

Only the maintainer tags and publishes releases. The release workflow drafts a
release from a tag. It refuses a commit without a passing CI run and does not
rerun the tests, so push `main`, wait for CI, then push the tag. Review the
assets, checksums, and provenance before separately approving publication on
GitHub.
