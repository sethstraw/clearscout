# Schema changes, creation, and database transfer are explicit commands

Status: accepted

## Decision

Schema changes are deliberate operations with reviewable upgrade and recovery
coverage. Starting the application does not alter a database. Normal API, web,
and worker startup refuses a missing or stale schema. It does not migrate it
implicitly.

`clearscout init` may run migrations, create the workspace layout and database
row, select a provider, import a CV, write settings, and register the first
immutable master-CV version. `clearscout db upgrade` is the other explicit
schema-creation boundary. One `MasterCvService` serves both the CLI and HTTP.
Text that already parses is adopted without a model call. A replacement appends
a version instead of erasing one.

Moving data between ClearScout databases is a separate, explicit operation.
`clearscout db import` copies rows from a source database into a fresh one. The
source is opened read-only, and the target must be empty. Unsupported tables are
skipped and named. The target's integrity rules still apply on the way in.

## Consequences

There is no importer for whatever you were using before. An importer for another
tool can only read its author's own directory layout, spreadsheet, sheet names,
and column headings. `clearscout db import` reads ClearScout's own databases and
nothing else.
