# SPA subtree

`docs/contributing/frontend-development.md` owns where components, hooks, and styles belong,
the API playground, moving a rule between stylesheets, and the dependency and
asset policy. Read it before adding a component, a hook, or a CSS class.

Commands run from the repository root as `npm --prefix web run <script>`, in
the forms the `tailorcv-command-card` skill gives. The coordinating session runs
`scripts/gate.sh web`.

The root `AGENTS.md` states the contract between the wire schema and this
subtree. The operational consequence: a backend change reaches here through
`scripts/generate.sh`, so a compile failure after backend edits usually means
the generated client is behind rather than the component being wrong.

Prettier runs through the project's own ignore file. Prompt files stay
unformatted because their exact bytes participate in suite identity.

## Tests

A component's tests sit beside it as `<Component>.test.tsx`. Extend that file;
a case for a different component goes beside that component, not into a page
test that happens to render it. `web/src/features/job-search/PendingDiscoveries.test.tsx`
and `web/src/pages/JobPage.test.tsx` already cover several components, so new
cases belong with the component they exercise.

HTTP goes through MSW. `web/src/test/setup.ts` starts one server with
`onUnhandledRequest: "error"`; a test registers the routes it uses with
`server.use(http.get(...))` from `web/src/test/msw/server.ts`. Do not stub
`globalThis.fetch` or write a per-file fetch router, except where the exact
request is the contract under test. When you touch a file that still stubs
fetch, move the case you touch to handlers.

Response bodies are typed against the generated schema with `satisfies`, so a
missing required field fails `typecheck`. A body or render helper used by more
than one test file lives beside the owning feature, not copied into each test;
search the feature for an existing `render*` or response factory before adding
one. There is no universal fixture builder.

Query by role, label, or visible text, and assert what the user sees or can do,
not component state, hook calls, or CSS classes. When UI is removed, its tests
go in the same change.

The root `AGENTS.md` rule that tests never wait on the clock covers a raised
`waitFor` or `findBy` timeout too. Drive a debounce, a poll, or a retry with
`vi.useFakeTimers()` and `await vi.advanceTimersByTimeAsync(ms)` in a `try`
with `vi.useRealTimers()` in the `finally`, and let a promise the test resolves
itself carry anything else. Advancing a timer also states the interval the
behavior depends on, which a bare sleep hides.

Two skills in `.agents/skills/` cover recurring work here:
`openapi-contract-sync` after a backend contract change, and
`tanstack-query-audit` for `useQuery`, `useMutation`, `useSuspenseQuery`, or
`useInfiniteQuery` work and stale-cache bugs. Pair the second with
`tailorcv-ui-debug` when reproducing.
