# Frontend development

The web client is a React and TypeScript SPA in `web/`. It talks to the versioned
FastAPI contract, never to Typer or a shell command.

Run these from the repository root:

```bash
npm --prefix web ci
npm --prefix web run api:generate
npm --prefix web run format:check
npm --prefix web run lint
npm --prefix web run typecheck
npm --prefix web run test
npm --prefix web run build
```

`format:check` and `format` run Prettier over the whole repository, not just
`web/`, because one formatter covering TypeScript, TSX, CSS, JSON, Markdown, and
YAML is simpler than one per directory. `format` rewrites every file it covers,
so in a shared checkout format only the files you changed with
`npm --prefix web exec -- prettier --write <files>`. Prettier is configured at
the repository root and described in [CONTRIBUTING.md](../../.github/CONTRIBUTING.md); ESLint keeps only correctness rules and
has no stylistic ones to disagree with.

If you changed the API, run `scripts/generate.sh`. It exports the schema to
`web/openapi.json`, and `openapi-typescript` turns that into
`web/src/api/schema.d.ts`, which you should never hand-edit. The gate
regenerates both into temporary storage and compares them, so a stale client
fails without rewriting tracked files.

To use it locally, run `clearscout db upgrade` against a private data root,
then `clearscout start` (API and worker together), or `clearscout serve` plus
`clearscout worker` in a second terminal. FastAPI serves the production build
in `web/dist`, so run `npm --prefix web run build` first. For live reload, run
`npm --prefix web run dev` (or `just dev-start` for API, worker, and SPA
together): Vite serves on port 5173 and proxies `/api` and `/admin` to the API
on port 8765. `web/dist` is ignored; release CI puts it into both the wheel and
source distribution.

## The API playground

The API playground (`web/src/features/api-playground/`, shown under Settings >
Advanced > Diagnostics) reads `/api/v1/openapi.json` at runtime. Do not add a second
registry of operations or hand-written request examples. It derives parameters
and JSON samples from the generated schema, makes you acknowledge anything that
is not a GET, and deliberately hands SSE and binary requests off to the task and
file interfaces, which have the extra safety behavior those need.
`/api/v1/docs` opens the playground when the built SPA is served, and the raw
OpenAPI document otherwise, rather than loading Swagger UI's assets from a CDN.
The `/api-playground` address redirects to the same place.

## Where things belong

`web/src/pages/` holds route composition only: one thin module per route in
`App.tsx`, plus the tab modules a route page composes, such as the Apply page's
Applications and Documents tabs. `web/src/features/<domain>/` owns what that
domain needs: its route implementation, components, models, hooks, tests, and feature CSS.
`web/src/components/` and `web/src/hooks/` are for what more than one domain
uses, so a component with a single consumer belongs to that consumer's feature
rather than here. Generated transport, providers, truly shared styles, and test
infrastructure cut across features and stay at the top level.

A feature exports through an `index.ts` only where more than one of its symbols
crosses its boundary, and by file path otherwise. Count the symbols rather than
weighing it: `job-detail` publishes several components and types to `JobPage`,
and its `index.ts` is that public surface. A feature publishing one symbol has
no barrel.
`JobDocuments` is imported by path on purpose: `JobPage` loads it with `lazy()`,
and a barrel that is also imported statically would put it back in the parent
chunk.

Shared HTTP parsing is in `web/src/api/http.ts`. Schema exploration and request
construction are in `web/src/api/openapi.ts`. A page component should not
reimplement either. Anything destructive goes through the shared confirmation
provider, and tabbed workspaces use the shared roving-focus tab component.

Sortable headers use `web/src/components/SortableHeader.tsx` with the comparison
helpers in `web/src/components/sorting.ts`. The Jobs ledger sorts on the server,
so the order covers the whole filtered ledger rather than just the page you can
see; small bounded tables sort copied rows in the browser instead. Every sortable
header carries `aria-sort`, a keyboard button, and a visible direction indicator.

The Jobs ledger shows fit and the overall, and leaves the component scores to the
job's own page so the table fits without a horizontal scroll. The shipped policy
gives evidence no weight in the overall on purpose: how well documented an
assessment is says nothing about how good the job is. The job's Evaluation tab
reuses the same component for its current scores and its version history, and the
job summary carries every component score.

## Moving a rule between stylesheets

`web/src/styles.css` imports in cascade order, so a rule that changes file
changes its position in the cascade. `scripts/css_manifest.py` prints the whole
cascade as one line per rule with no filename in it; run it before and after and
diff, and an empty diff means only the files moved.

Feature selectors live beside their feature. Shared stylesheets are limited to
tokens, shell layout, common controls, tables, rendered Markdown, dialogs, and
responsive rules. Split by responsibility and owner, never by an arbitrary line
count.

## Dependencies and assets

Plain CSS this project owns, and inline SVG icons. Do not add a runtime or test
dependency without a real call site and a gate that would notice it breaking.

Every asset here is project-made: the CSS, the text, and the SVG paths in
`web/src/components/Icons.tsx`. No employer logos, no proprietary fonts, no stock
imagery, no copied dashboard template, no third-party component-source library.
Dependency provenance and exact licenses are in `THIRD_PARTY_NOTICES.md`.
