# The search is configuration, not code

Status: accepted

## Decision

Where somebody may work, which arrangement they will accept, and what a role has
to pay are facts about a person's search. They are not facts about their career
record. They live in `search_parameters/` as one validated model, not as
literals in `src/`. A file-based command reads `[job_search]` from
`tailorcv.toml`. A workspace copies it at creation, then reads and edits its
versioned database record. The same seed-once boundary applies to guardrails.

Writes append, and the browser sends the version it started from. Two tabs
cannot produce a silent last-writer result. The parameters are stored as one
JSON document, not six normalized tables. They are read and written whole. One
model already owns every rule about their shape and validates them. They are
never queried by field.

An unconfigured file seeds no parameter row. A shipped default would be one
person's search written into everybody's database. A workspace with no row has
expressed no opinion. Every reading treats that as unknown, not as a refusal.

## Consequences

The residence gate is split accordingly. The transport object keeps the shape
rule that holds for everybody: an actionable decision names the rule it relied
on. Which rules count as eligible is checked in the service layer. That is the
only layer that can see whose search it is.
