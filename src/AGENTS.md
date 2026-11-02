# Backend subtree

`docs/contributing/backend-development.md` owns where code and tests belong, the durable
workflow entry points, error patterns, and how historical database values are
handled. Read it before placing anything new.

Live code sits in five layers: `src/tailorcv/application/`,
`src/tailorcv/domain/`, `src/tailorcv/delivery/`,
`src/tailorcv/infrastructure/`, and `src/tailorcv/shared/`, with
`src/tailorcv/bootstrap/` wiring the API, CLI, and worker entry points.

Tests for this subtree live in `tests/`, whose `AGENTS.md` covers reuse and
placement. Read the owner's test module before writing a test, so that guidance
and the shared builders are in view.

Focused commands: `just pytest-log <name> <files>` for tests, `uv run --no-sync
mypy` for types, and `uv run --no-sync lint-imports` for layer contracts. `just
test`, `just typecheck`, and `scripts/gate.sh py` cover the whole tree and run
from the coordinating session.

Three skills in `.agents/skills/` cover recurring work here:
`alembic-migration-author` for anything under
`src/tailorcv/infrastructure/persistence/migrations/versions/`,
`fastapi-testcase-scaffold` for route-handler tests, and
`openapi-contract-sync` when a Pydantic wire model or route changes.
