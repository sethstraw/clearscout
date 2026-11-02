# The generated contract is the wire, and it is checked rather than trusted

Status: accepted

## Decision

The pipeline runs Python to `web/openapi.json` to `web/src/api/schema.d.ts`. CI
diffs all three, so the document cannot drift from the server that produced it.
It is authoritative because it is generated and reviewed, not because somebody
typed it. Editing `openapi.json` by hand and generating server stubs from it
would fight FastAPI and buy nothing here.

What generation alone does not stop is the contract describing less than the
wire carries. A handler can answer with a shape no response model declares. A
field can cross as an open object the client can only type as unknown. A closed
vocabulary can arrive as a bare string that the browser then has to guess at.
`tests/delivery/test_wire_contract.py` refuses all three. Both exemption lists
in it name the reason, not the count, so adding to one is a decision somebody
made on purpose.

## Consequences

The client side of the same rule: the SPA derives its vocabularies from
`schema.d.ts` and declares none of its own. `Record<TaskType, ...>`,
`Record<ArtifactLogicalType, ...>`, and the status tone maps are all keyed by the
generated type. A value the server gains and the client does not handle fails
the build. A lookup by bare string is still allowed where a response from a
newer server has to render as something rather than crash. Those are the only
places a value may arrive unrecognized.

Workspace routes are split by concern into modules under
`delivery/api/routers/workspaces/`, each depending only on the service it
adapts. The package composes them into one router, so the generated document
describes one workspace API under one versioned prefix.
