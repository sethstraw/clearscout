# Backend development

The Python application is in `src/tailorcv/`. One set of services sits behind
three ways in: the Typer CLI, the versioned FastAPI adapter, and the durable
worker. A change belongs in the service rather than in whichever entry point you
happened to be holding when you noticed it.

## Durable workflow entry points

| Use case       | CLI                     | HTTP                                        | Worker             |
| -------------- | ----------------------- | ------------------------------------------- | ------------------ |
| Paid tailoring | `tailorcv tasks tailor` | `POST /api/v1/tasks` with `workflow.tailor` | `TailoringHandler` |

The CLI and HTTP adapter validate the same task payload and enqueue through
`TaskQueueService`. The worker claims that durable task and executes the
registered handler; neither entry point runs provider or transaction policy.

## API discovery and recorded costs

The running server serves the same JSON OpenAPI at `/openapi.json` and
`/api/v1/openapi.json`. Use the schema's declared methods and paths rather than
guessing route names.

`GET /api/v1/workspaces/{workspace_id}/spend-history` reports historical costs
per paid task type. `GET /api/v1/workspaces/{workspace_id}/spend` reports recorded
workspace spend. These are usage-based estimates; missing prices remain unknown.
Neither endpoint predicts a new evaluation's cost.

Billing reconciliation and price refresh are POST operations:
`/api/v1/billing/openai/reconciliation` and `/api/v1/billing/prices/refresh`.
Reading cost history does not invoke either operation.

For a complete discovery audit, use
`GET /api/v1/workspaces/{workspace_id}/source-scan-outcomes/export?task_id={task_id}`.
It streams newline-delimited JSON; each line follows `SourceScanOutcomeResponse`
in OpenAPI. It accepts the list route's filters and ordering without pagination,
counts, or facets. A scan includes its continuations; a continuation task exports
only its own rows. The export is body-free and does not read publisher sites.
Use the paginated list for interactive views instead of collecting every page.
for an audit.

## Where things belong

Every package under `src/tailorcv/` opens with a docstring saying what it is
for, and those docstrings are the map. Read them before placing new code instead
of guessing from a neighbouring file, and do not copy them into prose here,
because the copy is what goes stale.

The layers point one way. Domain vocabulary knows nothing about the services
above it, persistence knows nothing about the application, the adapters at the
edges are known by nobody, and pipeline stages depend on neither the services
nor the store. The `[tool.importlinter]` contracts in `pyproject.toml` hold the
current list of edges and `lint-imports` fails on a new crossing, so those
contracts are the authority and this paragraph is only the reason for them.
Wanting one of those edges is a design question rather than an import, and the
circular import you get for free is usually the code saying the same thing more
rudely.

One boundary does not fit a contract and stays in
`tests/durability/test_capability_boundaries.py`: one capability may not import
another's underscore-prefixed names, which import-linter cannot see because it
graphs modules rather than the names imported from them. The same test checks
the contracts themselves, failing on a module a layers contract leaves out, a
redundant forbidden entry, a one-sided exclusion between peers, a wildcard ignore, or a name that is not short ASCII.

The package root is not a placement option. Runtime composition belongs in
`bootstrap/`, HTTP and CLI adapters in `delivery/`, business policy in an
`application/<capability>/` owner, and effects in `infrastructure/`. Only
framework-neutral primitives with real consumers across capabilities belong in
`shared/`; do not create a generic utilities package.

Web reads are owned by `infrastructure/web_fetch/`, including ingestion, board
adapters, company research, and the published fetch policy. SQLAlchemy and
Alembic are owned by `infrastructure/persistence/`. Provider SDK adapters are
owned by `infrastructure/providers/`.

The web client has the same guidance in
[frontend-development.md](frontend-development.md).

## Patterns and errors

This repository is read as a work sample, so a reader should recognize the shape
without being told it: an adapter, a policy object, a registry, a strategy. A
bespoke mechanism where a named one fits costs the reader that recognition and
costs you the paragraph explaining it.

An error type earns its name by being caught. Define one when a caller has to
branch on it, and reuse an existing one otherwise. The web-fetch policy defines
five under the shared base `FetchPolicyError`. `PolitePostingFetcher` answers
four of them differently, the board and publisher readers handle `HostHeldError`
on its own, and ingestion catches the base as a group. Another subclass that
nobody catches separately would be noise.

## Stored values outside the current contract

Database records can fail a later closed-vocabulary validation. A typed read
omits a field or record that cannot validate against the current contract, and
never guesses a successor. The stored fact remains available for an explicit
repair or audit.

## Writing the tests

Test names here are sentences naming the behavior and the condition, not the
function under test. `test_credentials_are_not_carried_onto_a_redirect_target`
says what breaks when it fails; `test_redirect_2` does not.

Where a class takes its effects as constructor arguments, meaning `getter`,
`poster`, `resolver`, `sleeper`, `jitter`, and `wait`, a test passes its own
rather than patching the module underneath. Patch only what has no seam, such as
the pinned transport below `getter`. Never leave a patch sitting beside a seam
that supersedes it: the patch does nothing, and the test reads as covering a
path it never reaches. `tests/support/` holds the shared builders, so look there
before writing a fixture.

A test written against a bug has not been shown to work until it has failed.
Shelve the fix, run the new test, read the failure and check it is the one the
bug describes, then restore the fix and run it again. A regression test nobody
has seen red is a guess.

What the suite covers, and what it deliberately does not, is in
[test-coverage-map.md](test-coverage-map.md).
