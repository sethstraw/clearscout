---
name: alembic-migration-author
description: Author or modify a TailorCV Alembic migration. Use for any change under src/tailorcv/infrastructure/persistence/migrations/versions/, any request to add a revision, and any schema or data change that existing databases must receive. Do not use for read-only schema inspection or unrelated database work.
---

# TailorCV Alembic migration author

Follow this skill before proposing any migration file. The rules are in
`docs/contributing/database-and-migrations.md`: until the first public tag, the schema is one
revision edited in place; after it, each change adds a forward revision.

## Read state first

1. Decide whether the repository is pre-release:
   ```sh
   git tag --list 'v*'
   ```
   No output means pre-release. Any `v*` tag means the first release shipped.
2. List current revisions:
   ```sh
   uv run --no-sync alembic history --verbose
   ```
3. Read the target model change: the SQLAlchemy declaration and any Pydantic
   mirror. Confirm the change belongs in the schema, not in application code.

## Pre-release: edit v0001 in place

Edit
`src/tailorcv/infrastructure/persistence/migrations/versions/v0001_schema.py`
directly. Do not create a second revision file. Preserve column order, server
defaults, and index names already used elsewhere.

Schema defaults must not invent evidence. A `server_default` states a value for
new rows, not a claim about historical data.

## After the first tag: new revision

Add a forward revision with a stable slug. Do not autogenerate. Hand-write both
`upgrade()` and `downgrade()`, and check that downgrade reverses the change.
Applied revisions are never rewritten.

## Data movement

When existing rows must change value, the data step carries:

- exact provenance: which source column or which recorded value supplies it
- an idempotency guard that skips rows already carrying the new value
- a verification query proving the intended rows moved and no others did

Never repair a database with ad hoc SQLite writes. Never put backfill data in
`server_default`.

## Verify

Run migration work only against a scratch data root, never a real workspace.
An inherited `TAILORCV_DATABASE_URL` beats `--data-root`, so name the scratch
database with `--database-url` as well.

```sh
uv run --no-sync alembic upgrade head --sql
```

Read the emitted SQL as structural evidence only. It does not prove a data step
is safe or idempotent; cover that with focused fixtures.

Run the focused persistence tests:

```sh
just pytest-log persistence tests/durability/test_persistence_foundation.py
```

The coordinating session then runs `scripts/gate.sh change`. Report the
migration file, any data step, and the verification query results.
