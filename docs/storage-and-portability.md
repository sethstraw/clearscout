# Storage, backup, restore, and portability

One person, one machine, SQLite. The database and the filesystem are separate
durability systems, and they share no transaction. Bytes are staged privately,
hashes and sizes are recorded, and files are promoted or compensated around the
database commit.

## Storage is decided once

Each process settles one storage configuration before it builds anything, and
it never changes. It holds the database URL and SQLite path, the data root, the
writable artifact root, the workspace-root policy, and any read-only import
roots you explicitly allowed.

By default the data root is a `ClearScout` folder in your platform's Documents
directory:

```text
ClearScout/
  clearscout.sqlite3
  .clearscout-runtime/   (serve and worker logs, process state)
  artifacts/
  workspaces/
    <slug>/
      master-cv/
      imports/
      work/
      backups/
      clearscout.toml
```

`--data-root` moves the whole boundary. `--artifact-root` moves only the
writable artifact directory. `--workspaces-root` moves the folder new
workspaces go under, as `<workspaces-root>/<slug>`. `clearscout init --root`
moves only the new workspace's directory, and wins over `--workspaces-root`.

`--workspaces-root` has two limits. A workspace keeps the absolute `root_path`
stored at creation, so changing the flag moves only later workspaces. And
`db import` relocates a `root_path` only inside the source data root, so a
workspace under a separate workspaces root keeps its old path.

With a custom SQLite URL, workspaces and artifacts default to sitting beside
that database, not under Documents. Give both a database URL and `--data-root`
and you get a split: the URL picks the database, and the data root implicitly
picks where workspaces and artifacts go. That is fine if you meant it and
confusing if you did not. Only SQLite is supported;
[database-and-migrations.md](contributing/database-and-migrations.md) says why.

Use the same `--data-root` and `--database-url` for `init`, `serve`, `worker`,
and the database commands. Pass the same optional `--artifact-root` to `init`,
`serve`, `worker`, `start`, `stop`, `status`, and `db import`. Pass the same
optional `--workspaces-root` to `init`, `serve`, `worker`, and `start`.
`db status`, `db upgrade`, `db backup`, `db downgrade`, `db archive-cache`, and
`db rebuild-missed-requirements` touch only the database, so they take neither.
The API and the worker will not create or migrate a missing schema.

After upgrading a database that was evaluated before ClearScout kept missed
requirements for ranking, run `clearscout db rebuild-missed-requirements
--workspace-id <id>`. Running it again replaces the rows with ones that reflect
current requirement rulings.

## SQLite backup and same-root restore

Backup uses SQLite's online backup API. It verifies integrity and foreign keys,
writes `0600` where the platform allows, refuses to overwrite, and names the
file only once the snapshot is complete:

```bash
clearscout db backup ./private-backups/clearscout-backup.sqlite3 \
  --data-root ./private-data
```

The snapshot holds relational state only. Keep the data root's `artifacts` and
`workspaces` directories with it: they hold the immutable bytes and your
settings. Treat the whole set as sensitive.

To restore into the same root (omit `--data-root` for the default Documents
root):

1. Run `clearscout stop` (or stop `clearscout serve` and `clearscout worker` if
   you started them separately).
2. Keep the damaged database separately; do not overwrite the backup.
3. Put the verified snapshot back at that data root's `clearscout.sqlite3`.
4. Restore the matching `artifacts` and `workspaces` directories if they were
   lost. Do not mix them with a snapshot from another point in time.
5. Run `clearscout db status --data-root ./private-data` and confirm it prints
   `Schema current: yes` before starting ClearScout.
6. Open detail or verification workflows to byte-verify referenced artifacts.

A database-only snapshot is not a portable export. Its file URIs still describe
the storage root they came from.

## Importing from another database

`clearscout db import` takes a recognizable ClearScout SQLite database and a
fresh, migrated target, and nothing else. Preflight checks integrity on both
sides and the schema columns it needs, orders dependent history, and produces a
stable plan. Deterministic seed rows already in the target are compared on every
shared column, and only identical rows are skipped. A primary-key collision with
differing rows is refused, naming the table, the key, and the columns that
disagree.

Before importing, stop or cancel queued or running board scans in the old
installation, and wait until none is still reading. If the old worker stopped
mid-scan, start it again so it releases that scan, then stop it. Collected
results and saved links import normally; resume them once the new worker runs.

Always read a dry run first:

```bash
clearscout db upgrade --data-root ./new-private-data
clearscout db import ./old-clearscout.sqlite3 \
  --source-data-root ./old-private-data \
  --data-root ./new-private-data \
  --dry-run
```

Rerun without `--dry-run` once you have read the skipped tables, the added or
dropped columns, the identical rows, the rewritten references, the external
references kept on purpose, and anything unresolved.

## Moving between roots

A database-only import copies the rows it can and claims no portability. Local
references outside an explicit root are reported as unresolved, not guessed.

When both the old and new roots are explicit, ClearScout rewrites only these
columns:

| Column                          | Rule                                                                                |
| ------------------------------- | ----------------------------------------------------------------------------------- |
| `workspaces.root_path`          | Remap the path relative to the old data root.                                       |
| `master_cv_versions.source_uri` | Remap and copy immutable bytes; verify stored SHA-256 and size.                     |
| `artifacts.storage_uri`         | Remap and copy immutable bytes; verify stored SHA-256 and size, then mark verified. |

An artifact root outside the data root needs both `--source-artifact-root` and
`--artifact-root`. Bytes already in the target are accepted only on an exact
hash and size match. Different bytes, a missing file, a missing verification
size, or a path collision are refused. Copies are staged privately and undone
if finalization or the database commit fails.

A non-file URI is kept as an external reference. An absolute local path is
kept only under an explicit, repeatable `--allow-read-root`; otherwise it stays
unresolved in the report. ClearScout never rewrites arbitrary text or an
unknown column that looks like a path.

Keep the old data root until the final report shows no unexpected unresolved
references and the new database's artifact checks pass.

## Keeping the response cache's answers

The provider response cache is a per-user file outside the data root. It
upserts on its key, so an answer is lost when the same request runs again.
`clearscout db archive-cache` copies the answers it still holds into the
database:

```bash
clearscout db archive-cache --data-root ./private-data
```

`--cache-directory` reads a cache outside the per-user default, and `--origin`
labels the rows. The command writes nothing to the cache. Each copy carries the
cache key, the response text, and the build that produced it. The key is a
one-way hash of the request, and an archived row stores no model-call
identifier, so nothing links a copy to a recorded model call. A rerun adds only
keys and timestamps the archive lacks.

## How far this scales

One person, one local SQLite database, one worker. List views keep a fixed
query count as jobs grow and never read artifact files. Byte hashing happens
only in detail and verification views.

There are no distributed workers, remote object storage, or high availability.
The connection settings are in
[database-and-migrations.md](contributing/database-and-migrations.md).
