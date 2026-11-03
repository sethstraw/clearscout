# Storage, backup, restore, and portability

One person, one machine, SQLite. The database and the filesystem are two
separate durability systems, and this does not pretend otherwise: bytes are
staged privately, hashes and sizes are recorded, and files are either promoted or
compensated around the database commit. Nothing here claims SQLite and your
filesystem share a transaction.

## Storage is decided once

Every process works out one storage configuration before it builds anything, and
then that configuration does not move. It holds the database URL and SQLite path,
the data root, the writable artifact root, the workspace-root policy, and any
read-only import roots you explicitly allowed.

Left alone, the data root is a `ClearScout` folder in your platform's Documents
directory, and it looks like this:

```text
ClearScout/
  clearscout.sqlite3
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
workspaces are created under, as `<workspaces-root>/<slug>`. `clearscout init
--root` moves only the new workspace's own directory, and wins over
`--workspaces-root`.

`--workspaces-root` has two limits. A workspace keeps the absolute `root_path`
stored when it was created, so changing the flag moves only workspaces created
afterwards. `db import` relocates a workspace `root_path` only when it sits
inside the source data root, so a workspace under a separate workspaces root
keeps its old path after an import.

Point at a custom SQLite URL and its workspaces and artifacts default to sitting
beside that database rather than under Documents. Give both a database URL and a
`--data-root` and you get a split: the URL picks the database, the data root
picks where workspaces and artifacts go implicitly. That is fine if you meant it
and confusing if you did not. A non-SQLite URL has to come with an explicit local
data or artifact root; which engines are supported is in
[database-and-migrations.md](contributing/database-and-migrations.md).

Whatever you choose, use the same `--data-root` and `--database-url` for
`init`, `serve`, `worker`, and the database commands. Pass the same optional
`--artifact-root` to `init`, `serve`, `worker`, `start`, `stop`, `status`, and
`db import`, and the same optional `--workspaces-root` to `init`, `serve`,
`worker`, and `start`. `db status`, `db upgrade`, `db backup`, `db downgrade`, and
`db archive-cache` touch only the database, so they do not take it. The API and
the worker will not create or migrate a schema that is not there.

## SQLite backup and same-root restore

Backup goes through SQLite's own online backup API. It verifies integrity and
foreign keys, writes `0600` where the platform allows it, refuses to overwrite
anything, and only names the file once the snapshot is complete:

```bash
clearscout db backup ./private-backups/clearscout-backup.sqlite3 \
  --data-root ./private-data
```

That snapshot is relational state and nothing else, so keep the data root's
`artifacts` and `workspaces` directories with it. They hold the immutable bytes
and your settings. Treat the whole set as sensitive.

To restore into the same root:

1. Stop `clearscout serve` and `clearscout worker`.
2. Preserve the damaged database separately; do not overwrite the backup.
3. Put the verified snapshot back at that data root's `clearscout.sqlite3` path.
4. Restore the matching `artifacts` and `workspaces` directories if they were
   lost. Do not mix them with a snapshot from another point in time.
5. Run `clearscout db status --data-root ./private-data` before starting the API.
6. Open detail or verification workflows to byte-verify referenced artifacts.

The backup test opens the snapshot on its own and checks integrity, foreign
keys, workspaces, and evaluations. Note that a database-only snapshot is not a
portable export: the file URIs in it still describe the storage root they came
from.

## Importing from another database

`clearscout db import` takes a recognizable ClearScout SQLite database and a fresh,
migrated target, and nothing else. Preflight checks integrity on both sides,
checks the schema columns it needs, orders dependent history, and produces a
stable plan. Deterministic seed rows already in the target are compared across
every shared column, and only identical rows are skipped. A primary-key collision
where the rows differ is refused, naming the table, the key, and the columns that
disagree.

Stop or cancel queued and running source scans in the source installation before
importing, and wait for readers to release their reservations. Restart the source
worker to recover reservations left by a stopped reader. Reader ownership cannot transfer to
another process. Collected results and retained links remain importable; resume
them explicitly after starting the destination worker.

Look at a dry run first, every time:

```bash
clearscout db upgrade --data-root ./new-private-data
clearscout db import ./old-clearscout.sqlite3 \
  --source-data-root ./old-private-data \
  --data-root ./new-private-data \
  --dry-run
```

Run it again without `--dry-run` once you have read through the skipped tables,
the added or dropped columns, the identical rows, the rewritten references, the
external references it kept on purpose, and anything it could not resolve.

## Moving between roots

A database-only import copies the rows it can and makes no portability claim.
Local references that sit outside an explicit root come back reported as
unresolved rather than guessed at.

When both the old and new roots are explicit, these are the only columns
ClearScout will rewrite:

| Column                          | Rule                                                                                |
| ------------------------------- | ----------------------------------------------------------------------------------- |
| `workspaces.root_path`          | Remap the path relative to the old data root.                                       |
| `master_cv_versions.source_uri` | Remap and copy immutable bytes; verify stored SHA-256 and size.                     |
| `artifacts.storage_uri`         | Remap and copy immutable bytes; verify stored SHA-256 and size, then mark verified. |

An artifact root outside the data root needs both `--source-artifact-root` and
`--artifact-root`. Bytes already sitting in the target are accepted only when
the hash and size match exactly; different bytes, a missing file, a missing
verification size, or a path collision are all refused. Copies are staged
privately and undone if finalization or the database commit fails.

Anything with a non-file URI scheme is kept as an external reference. An absolute
local path is only preserved under an explicit, repeatable `--allow-read-root`,
and otherwise stays unresolved in the report. ClearScout will not rewrite arbitrary
text, or an unknown column that happens to look like a path.

Keep the old data root until the finished report has no unresolved references you
did not expect and the new database's artifact checks pass.

## Keeping the response cache's answers

The provider response cache is a per-user file outside the data root, and it
upserts on its key, so an answer is gone the moment the same request is made
again. `clearscout db archive-cache` copies the answers it still holds into the
database:

```bash
clearscout db archive-cache --data-root ./private-data
```

Pass `--cache-directory` to read a cache somewhere other than the per-user
default, and `--origin` to label the rows. The command reads the cache and
writes nothing to it. The copies carry the cache key, the response text, and
the build that produced it; the key is a hash of prompt text nobody stored, so
a copy cannot be matched back to a recorded model call. Running the command
again adds only keys and timestamps the archive does not already hold.

## How far this scales

One person, one local SQLite database, one worker. The job list and the
workspace summary issue the same number of SQL statements for a ledger of 1 job
and one of 40. A test caps both counts, and neither reads artifact files.
Listing 12 assigned tags takes at most three statements. Hashing bytes is
reserved for detail and verification, where you asked for it.

There is no distributed-worker story, no remote object storage, and no high
availability, and none of those is being claimed. What the connection itself is
configured with is in
[database-and-migrations.md](contributing/database-and-migrations.md).
