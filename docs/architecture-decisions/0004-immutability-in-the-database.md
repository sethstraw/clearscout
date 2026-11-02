# Relational state, and immutability enforced in the database

Status: accepted

## Decision

SQLAlchemy with explicit Alembic migrations, SQLite by default. Stable
identities and observations are normalized, not collapsed into one mutable job
row. Completed evaluations, history events, and evidence are immutable at the
database layer. Triggers enforce that, not only the mapper, so a script or a
REPL meets the same refusal a service does. Corrections append a reference
instead of overwriting. SQLite is the supported database. PostgreSQL is neither
configured nor release-tested.

Evaluation recalculations form a linear per-posting chain. Each row carries a
positive version number. After version one, it also carries a unique reference
to the exact evaluation it supersedes. A trigger refuses a version that does not
continue its own chain.

## Consequences

The newest version is derived by query, not by a mutable current flag. A changed
commit must name the evaluation it believes is current. A stale writer fails
instead of branching.
