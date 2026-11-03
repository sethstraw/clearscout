# A read-only model admin behind the local browser boundary

Status: accepted

## Decision

ClearScout mounts `starlette-admin` at `/admin` from `bootstrap/api.py`, generating
one view per mapped model from the SQLAlchemy registry. Every view offers no
create, edit, or delete, so the admin declines the write in Python before any
handler reaches the database. The `IMMUTABLE_TABLES` triggers still refuse
updates and deletes at the database level, but the admin does not depend on
that backstop to stay read-only: a mutable table is as read-only in the admin as
an append-only one.

The admin reads SQLAlchemy models rather than the SQLite file. That is what
makes it survive a change of database engine, and it is the reason a
SQLite-native browser was rejected despite fitting the immediate need better.
`sqladmin` was the other serious candidate and is the safer choice under a
strict Content-Security-Policy, because it ships no inline scripts at all. It
lost on the JSON columns: the schema has more than forty of them, and only
`starlette-admin` renders them as navigable JSON rather than raw text.

A React admin inside the SPA was rejected on measurement. The OpenAPI document
describes far fewer resources than the schema has tables, and those resources
disagree on their list shape, so any generic data provider would have required a
new generic table endpoint first.

## The Content-Security-Policy is not negotiable here

`delivery/api/http_security.py` sets `script-src 'self'` with no
`'unsafe-inline'`, and that header reaches the mounted admin like every other
response. Two of the admin's own inline scripts therefore never execute.

Do not relax it. The database stores posting text fetched from arbitrary job
boards, so an admin page renders untrusted third-party content. Template
autoescaping is the first defense and the policy is the second; removing the
second because a widget did not initialize trades a real boundary for a
cosmetic gain.

One of the blocked scripts starts the JSON viewers, so `admin_static/` serves a
file that starts them instead. It exists only because the inline call cannot
run, and it is loaded through the library's own static directory and per-view
script links rather than by overriding a template, so there is no upstream fork
to carry.

The rest of what those scripts would have done is row selection, bulk actions
and inline editing. Those are writes, and the read-only views refuse them in
Python regardless of which client sends them; the browser mutation check in
`secure_local_response` is a second, independent barrier that an HTML form
cannot pass because it cannot send the required header. Roughly eighty inline
`style` attributes per page are blocked too, which leaves column widths uncapped
and truncates long labels in the menu. That is accepted.

## Read access is gated by the loopback bind, not by a header

The admin serves GET without authentication. What keeps it private is the
loopback-only bind: the API listens on `127.0.0.1`, so only a process on this
machine can reach `/admin` at all. The browser mutation header gates writes, not
reads, and every view is read-only in any case, so the header is not what
protects the data an admin page displays. Adding a login was considered and
declined: a single-user local tool behind a loopback bind gains no boundary from
a password it would store next to the database it guards. If the bind ever
widens beyond loopback, this decision has to be reopened, because nothing else
here stands between a remote caller and a full read of the ledger.

## Consequences

The mount is registered before the SPA catch-all in `create_app`. Starlette
matches routes in registration order, so an admin mounted after
`/{frontend_path:path}` returns `index.html` with a 200 for every admin path,
which reads as a broken admin rather than a routing mistake.

Wiring lives in `bootstrap` because admin registration imports ORM models, and
the Import Linter contract in [0001](0001-package-ownership.md) forbids that
from `delivery`. The admin shares the API's lazily opened database rather than
holding a second engine, so the existing lifespan disposes it, and the migration
guard still refuses a stale schema before any request reaches a view.

Menu groups follow the model modules, which already partition the schema. A
module that no group names falls into a catch-all rather than disappearing from
a menu that claims to list the schema.

Read-only is declared in Python for every view, not inferred from the database.
The append-only triggers remain the backstop, and they carry a dialect branch of
their own; the SQLite branch is what ships and is exercised, and the Postgres
`23514` branch is untested against a live Postgres. The admin does not depend on
catching a database error to know what it may offer.
