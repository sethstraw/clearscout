---
paths:
  - "**/*.db"
  - "**/*.sqlite3"
  - "src/tailorcv/infrastructure/persistence/**"
---

# Reading a workspace database

Open a workspace database read-only unless the task names it as writable. From
Python, use `sqlite3.connect("file:<path>?mode=ro", uri=True)`; from the shell,
`sqlite3 -readonly -header -column <path>` suits interactive exploration and
bulk output.

A query that needs to write belongs against a scratch copy with synthetic data,
not a workspace someone is using. A schema change goes through a migration;
see `docs/contributing/database-and-migrations.md`.
