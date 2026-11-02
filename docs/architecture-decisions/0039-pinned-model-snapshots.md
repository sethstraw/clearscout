# A snapshot is pinned by the vendor, and proved by the run

Status: accepted

## Decision

An evaluation is reproducible only if the identifier it names resolves to one
fixed model. Both shipped defaults, `DEFAULT_MODELS` in
`application/config.py`, name identifiers their vendors document as pinned
snapshots. Anthropic states that every Claude model ID is one.
The dateless format used from the 4.6 generation is pinned as well. There is no dated identifier to add.

So the mechanism is not a table of snapshot names. Every stage records what the
provider says answered beside what it asked for. A report names every place
those differ. That catches the two ways a run stops being reproducible, without
needing a list of vendor aliases. One is an older alias that resolves to a dated
ID. The other is a deprecation routed to a successor.

## Consequences

A stage comparison refuses to state a saving when its two sides were answered
by different models. By construction, only the effort differs between them. A
number spanning two models would be attributed to a cause that did not produce
it.
