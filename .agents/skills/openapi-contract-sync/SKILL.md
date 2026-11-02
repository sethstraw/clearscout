---
name: openapi-contract-sync
description: Keep the TailorCV OpenAPI schema and the generated SPA TypeScript types in sync. Use after a wire-model or delivery/api route change, for an explicit regeneration request, or for demonstrated contract drift. Do not use for pure runtime changes.
---

# TailorCV OpenAPI contract sync

The SPA's types and closed vocabularies come from the generated schema, as the
non-negotiables in `AGENTS.md` require. When either side drifts, the SPA either
fails to build or misdescribes the payload shape.

## Identify the surfaces

The contract pair is `web/openapi.json` and `web/src/api/schema.d.ts`. Read
`scripts/generate.sh` and the scripts it calls before regenerating; it names the
generator for each.

## Regenerate

```sh
scripts/generate.sh
```

The script writes other artifacts too. Review changes to those separately from
the contract change.

## Verify

```sh
npm --prefix web run typecheck
npm --prefix web run test
```

Both must pass. A type error naming a generated type means the contract moved.
Fix the SPA consumer, not the generated file.

## Guard rails

- Never edit `web/openapi.json` or `web/src/api/schema.d.ts` by hand. The next
  generate run overwrites the edit and hides the real drift.
- A wire model's docstring reaches the client; `AGENTS.md` says where its
  cross-references go.
- If regeneration produces drift unrelated to the current change, stop and ask.
  Unrelated drift means another change landed without regenerating. Do not fold
  it into this change.

## Finish

Inspect the full diff. Generator code, configuration, or pinned version changes
can alter output without a route diff. The coordinating session confirms the
result with `scripts/gate.sh generated`, then runs the gate scope that fits the
consumers you changed.
