# Contributing

This grew out of one person's job search. Another person's use will find
assumptions that mine did not. A report with a small synthetic example is useful
even if you do not want to write code.

Small focused changes are easier to review than redesigns. Open an issue before
doing substantial work on a new capability or a change to product behavior.
Security problems use the private route in [SECURITY.md](SECURITY.md).

Strip real CV text, employers, postings, provider responses, keys, and local
paths from every issue, fixture, screenshot, and log. An ignored file is still
private data on disk; `.gitignore` is not a privacy boundary.

## The product boundaries do not move for convenience

- Matching and drafting may cite only evidence identifiers selected from the
  master CV for that run. Fixed facts are copied by the program.
- A model drafts and critiques. Application code owns orchestration, selection,
  policy, and deterministic checks.
- Prompts are versioned files under `prompts/`, not inline strings.
- Provider adapters use the official OpenAI and Anthropic SDKs without a wrapper
  abstraction.
- Published source policy decides reads. A URL remains the posting identifier
  even when the read is refused.
- HTTP payloads use typed contract models. The SPA derives closed vocabularies
  from the generated schema.
- `pressume` is an external renderer. TailorCV does not bundle or depend on it.
  Explicit rendering actions may invoke an installed renderer with fixed arguments.

A smaller change that weakens one of those rules is still a regression. The
full evidence and approval model is in
[docs/truth-and-provenance.md](docs/truth-and-provenance.md).

## Set up one clone

The supported development toolchain is Python 3.12 or newer, Node 24, npm 11,
and [uv](https://docs.astral.sh/uv/). The web bundle must exist before the Python
package can be installed, because the built SPA is part of the distribution.

```bash
npm ci --prefix web
npm run --prefix web build
uv sync --all-groups
```

`tailorcv serve`, `tailorcv worker`, and the other commands default to
`TailorCV/` in the operating system's Documents directory. That holds in a
clone too, unless you pass `--data-root`. `scripts/tailorcv-local-app` is the
one launcher that uses the ignored `local-data/` directory instead. It does so
unless `TAILORCV_DATA_ROOT` is already set. Keep development data synthetic.

To run the local application without supplying a real CV, seed a scratch data
root as [Try the synthetic workspace](README.md#try-the-synthetic-workspace)
shows. Background work also needs a worker on the same root, in a second
terminal:

```bash
uv run tailorcv worker --data-root /tmp/tailorcv-demo
```

Neither the server nor the worker starts or upgrades the database implicitly.

## Make a focused change

Read the owner for the surface first:

- [backend development](docs/contributing/backend-development.md) for Python
  placement, dependency direction, and test seams;
- [frontend development](docs/contributing/frontend-development.md) for SPA
  feature ownership, generated contracts, CSS, and browser checks;
- [prompts/README.md](prompts/README.md) before changing prompt bytes; and
- [evals/README.md](evals/README.md) before changing cases, scorers, or baselines.

Search for an existing owner before adding a module, service, hook, component,
CSS class, or document. Tests should assert the caller-visible or user-visible
outcome. Use realistic synthetic fixtures, and make sure a new test can fail for
the defect it names.

Use `scripts/generate.sh` only when generated output is meant to change. The
gate creates temporary output and compares it with the tracked OpenAPI,
TypeScript, notices, and other generated artifacts; verification must not mutate
the checkout.

## Run the public gate

The verification interface is `scripts/gate.sh <scope>`. The usage block at
the top of [scripts/gate.sh](scripts/gate.sh) lists every scope and what it
covers.

Run `scripts/gate.sh change` before opening a pull request. It covers
everything an edit can break. `push` covers slower checks whose answer can also
change outside the source tree. The script runs every selected check after a
failure so one pass reports the whole result.

The gate calls no provider and spends no money. The security, workflow, and
package steps fetch advisory data and pinned tools; ordinary pytest, web tests,
scripted evaluations, and CI remove provider credentials and refuse network
paths that could spend money.
Do not weaken that boundary to make a test pass. [Validation
status](docs/validation.md) says what each class of evidence does and does not
prove.

Formatting is owned by ruff for Python and Prettier for TypeScript, TSX, CSS,
JSON, Markdown, YAML, and HTML. Prompt files are excluded from Prettier because
their exact bytes participate in suite identity.

```bash
npm run --prefix web format:check
```

The hooks in `.githooks/` are opt-in, because installing them changes
somebody's clone. Enable them with:

```bash
git config core.hooksPath .githooks
```

The pre-commit hook runs `fast`, and the pre-push hook runs `push`. The
commit-msg hook refuses a commit message that carries non-ASCII text or AI
attribution.

## Prompt changes need two kinds of evidence

`uv run python -m evals` runs the shipped stages against a scripted provider and
compares the results with recorded baselines. It is useful for deterministic
ledger consistency and named failure modes. It does not show that a live model
understood a changed prompt.

Run `scripts/eval.sh` for prompt work and read
[evals/inspect/METHODOLOGY.md](evals/inspect/METHODOLOGY.md) before preparing a
scoring set. A live run is a separate paid operation and requires explicit
operator approval for that run. Do not invoke `--live` during ordinary
contribution work.

Regenerate a baseline only when the intended behavior changed. Read every line
of the diff and explain why each measurement moved. Re-recording because the
suite is red deletes the evidence of the regression.

## Build synthetic browser evidence

Use the demo seed from
[Try the synthetic workspace](README.md#try-the-synthetic-workspace) for
reproducible screenshots and demo records. Pass `--port` to `tailorcv serve` to
run a screenshot workspace beside another server.

The seed refuses a populated database unless `--force` is explicit. It makes no
network or provider call. Keep fixtures and migration experiments synthetic.

With the workspace owner's explicit permission, documentation may also use a
live screenshot. Remove employer names, identifying domains, logos, contact
details, and any unapproved personal facts before capture. Check descriptions,
notes, and tooltips too. Replace text with an opaque label rather than blurring
it. Review the saved image, keep unredacted captures private, and label the
result as a redacted live workspace. Job titles may remain when the owner
approves them. A redacted image is not synthetic test data.

The release workflow creates a draft release from a tag. Review the assets,
checksums, and provenance before separately approving publication on GitHub.

## Schema changes are explicit

Before the first release tag, update `v0001_schema.py` in place and import an
existing database into a fresh migrated target. After the first release tag,
applied revisions are frozen and schema changes add forward revisions. The
commands and recovery rules are in
[docs/contributing/database-and-migrations.md](docs/contributing/database-and-migrations.md).

Run migration work only against a scratch data root. A change needs upgrade and
recovery coverage and an explanation of any immutable boundary it moves.

## AI assistance remains reviewable work

AI assistance is allowed. Disclose substantial use in the pull request, review
the output, check licenses and copied material, and keep private information out
of prompts and artifacts. Do not submit model output nobody has read. The full
policy is in
[docs/contributing/ai-assisted-development.md](docs/contributing/ai-assisted-development.md).

## Open the pull request with the limits intact

Explain the problem, the change, the checks you ran, and any remaining limit or
risk. Keep unrelated cleanup separate. A prompt change includes the same-input
before and after; an importer change includes a synthetic source fixture and an
assertion about the assembled master CV.

Dependency changes include both applicable lockfiles, the license audit, and
any regenerated notices. A dependency is not moved to development scope merely
to receive a looser license policy; shipped imports decide which policy applies.

Participation is covered by [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).
