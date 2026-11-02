# Database and migrations

SQLite, one user, one machine. SQLAlchemy mappings only change through an
explicit Alembic migration, and startup never creates or upgrades anything for
you. PostgreSQL is not supported and has never been release-tested, so please do
not read the SQLAlchemy layer as a promise that it would work.

Where the database actually lives, what `--data-root` does, and how to back up,
restore, import, or move it are all in
[storage-and-portability.md](../storage-and-portability.md). For options, the
installed command help is the real answer.

## What the schema guarantees

The tables are normalized rather than one wide blob. The mapped models under
`src/tailorcv/infrastructure/persistence/models/` are the list.

History cannot be quietly rewritten. Every table named in `IMMUTABLE_TABLES`, in
[the initial schema migration](../../src/tailorcv/infrastructure/persistence/migrations/versions/v0001_schema.py),
carries database triggers that refuse UPDATE and DELETE, so a bug cannot edit a
decision you already made. Evaluation version N+1 has to name version N, and a changed
commit has to name the current version, which is what stops duplicate, stale, or
branching histories. Anything current is a query over that history.
Records you are allowed to retire use a soft archive field instead of going away.

Every SQLite connection turns on foreign keys and WAL, and waits a bounded time
rather than failing instantly under contention. Database files are owner-only
where the platform has Unix permissions. One database, one worker: that is the
only queue topology this supports.

## Migrating

Before the first release tag, `v0001_schema.py` is the single schema revision
and changes in place. An existing pre-release database needs a fresh migrated
target and the verified import procedure in
[storage-and-portability.md](../storage-and-portability.md#importing-from-another-database).
After the first release tag, applied revisions are frozen and schema changes
add forward revisions. Starting the application never changes the schema.

Downgrade is a destructive development tool and asks you to confirm. If it is
your real data at stake, restore a verified backup instead.
