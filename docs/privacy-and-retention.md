# Privacy and retention

## Where your data lives

This repository holds product code and invented fixtures, nothing else. Your
durable database, saved postings, workspace artifacts, backups, logs, exports,
uploads, and personal settings live under `~/Documents/ClearScout` by default, or
under whatever private folder you choose as the data root. File-pipeline inputs
and `--out` packages live at the paths you choose. Raw provider responses are
cached in the operating system cache directory under `clearscout/responses` (on
macOS, `~/Library/Caches/clearscout/responses`). Set `cache = false` in
`clearscout.toml` to turn the cache off.

Documents is a deliberate default for durable workspace state: you and ordinary
backup tools can reach it without needing access to an
application-support folder. The provider cache is a separate deletion and backup
boundary, with one exception: a
cache entry copied into the database by the archive step stops being separate,
and lives wherever your database lives.

Provider credentials stay in your system keyring, in a provider environment
variable, or in a one-run argument. They are never written to the database,
returned by the API, built into the web bundle, or recorded in task events.
Settings and health tell you whether a key is present, and nothing more.

Cached provider responses are not evidence, and the workflow screens and the
versioned API do not show the on-disk cache. Durable records keep structured
outcomes, provenance, usage metadata, and a provider response identifier where
it is actually needed.

They also keep the full request and response of every model call, in
`model_call_envelopes`. The request carries the prompt, and the prompt carries
the CV evidence that run selected, so your own words are in that table. This is
deliberate: it is what makes it possible to ask later why a run answered as it
did, and on a single-user local tool the alternative is keeping a digest that
can prove something changed without ever showing you what. Nothing about it
leaves the machine.

The local server also mounts a read-only table browser at `/admin`, with one
view per database table. It shows those request and response envelopes and any
cache entries copied into `response_cache_archive`. It offers no create, edit,
or delete, so removing envelopes means deleting rows yourself. The archive table
is append-only, and its database triggers refuse a delete.

## Retention is yours

Nothing is deleted for you. ClearScout does not expire longitudinal records,
artifacts, backups, or cache entries on a timer. Immutable evaluations and
append-only histories stay. Nothing exports old records and then deletes them for you. Records you
are allowed to retire get soft archived; retained records are never quietly
rewritten.

So it is worth going through your private backups, generated packages, and
provider caches from time to time and removing what you no longer want, in
whatever way suits your situation. If you delete an artifact from outside
ClearScout, its metadata and hash stay behind on purpose, so the gap is visible
rather than silent.

## What leaves the machine

The local API binds to loopback only, and the web app talks to that same origin.
[Security](../.github/SECURITY.md#what-leaves-your-computer) lists every request that
does leave and what each one sends. The services those requests reach have
their own terms and retention.

Cover-letter research is stored as job- and run-scoped research artifacts with
the source URL and the fetch method, so you can look at the evidence and a later
run can reuse it. They retain exactly like everything else here, which is to say
until you remove them.

No workflow submits an application, messages an employer, or touches a public
profile.
