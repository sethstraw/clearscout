# A URL is folded five ways, and each fold has one owner

Status: accepted

## Decision

A posting URL answers five different questions. So ClearScout keeps five
transforms and improvises no sixth.

Publisher text is the column value itself: `discovery_url`, `canonical_url`,
`posting_urls.url`, `source_scan_outcomes.url`. It is stored as the publisher
wrote it. It is what the SPA links to and what a fetch asks for. "Canonical"
means the publisher's own canonical link, falling back to the discovery link.

Identity is `domain/identity.py`: `normalize_url`, and `url_sha256` over it.
`posting_urls.normalized_url`, `source_scan_outcomes.normalized_url`, and every
fingerprint hold that form. `posting_identity.py::posting_url_key` adds one fold
on top for the compares that must read a posting's apply page as the posting.

The fetch key is `domain/identity.py::normalize_fetch_url`. It is what
`source_scan_frontier_items.normalized_url` holds. It keeps the path and every
query a publisher routes on, so each page of a paginated board holds its own
frontier row. Identity drops those queries. That is why the two cannot collapse
into one function.

The board root is `web_fetch/boards.py::canonicalise_source_url`, keyed by
`board_slugs.py::board_identity_key`. It stays in infrastructure beside the
board patterns it reads.

The exact key is `domain/identity.py::exact_url_key`. It folds only scheme and
host case and a trailing slash, and keeps every query key, so an address a
person or a model cited matches only itself. Assisted content resolution
compares cited addresses with it.

## Consequences

Equality compares a computed key, never stored text. The exception is
`content_resolution/store.py`, which compares the stored column deliberately. The text
it read is the snapshot it re-checks before writing, so a concurrent edit has to
change it.

Apply-stripping stays outside `normalize_url`. Every fingerprint already stored
was computed without it. Folding `/apply` into identity itself would orphan
them. `posting_url_key` applies the fold where the comparison happens instead.
That leaves one gap open. A pending row staged from an `/apply` URL stores the
unstripped fingerprint, so a campaign capturing the detail URL does not see it.
Closing it is a fingerprint rewrite with its own data carry.
