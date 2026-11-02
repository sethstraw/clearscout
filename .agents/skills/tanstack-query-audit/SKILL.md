---
name: tanstack-query-audit
description: Audit TailorCV TanStack Query keys, invalidation, and cache behavior. Use for explicit query or cache work, or for a demonstrated stale-data defect in the SPA. Do not use merely because a component imports a Query hook, or for non-Query state.
---

# Query cache audit

Stale caches usually come from three causes: keys missing their variables,
mutations without a matching invalidation, and queries relying on the default
staleTime against an endpoint that changes underneath them. This skill exposes
all three.

## Scope the audit

Reuse the route, component, domain noun, or defect scope the task supplies. Ask
only when no usable scope can be inferred.

## Enumerate

Find the hook calls with a symbol search or `rg`:

```sh
rg -n --type ts \
  '\b(useQuery|useMutation|useSuspenseQuery|useInfiniteQuery|useQueries)\b' \
  web/src/<scope>
```

`--type ts` covers `.tsx` files too. Read each match. For every hook, capture:

- file and line
- hook name
- queryKey (literal or factory) and the variables it closes over
- staleTime and gcTime, or their absence, which means the client defaults
- for a mutation: the onSuccess handler and every `invalidateQueries` or
  `setQueryData` inside it
- for a query: the endpoint it hits, and whether that endpoint is mutated
  elsewhere

Read the QueryClient defaults in `web/src/providers.tsx` so you can spot hooks
that rely on them silently. Shared hooks live under `web/src/api/hooks/`.

## Flag

Report a table of findings, one row per hook. Mark:

- keys that omit a variable the query depends on, such as a job query whose key
  lacks the job id
- mutations whose onSuccess invalidates no key that a mounted query reads
- queries with no explicit staleTime whose endpoint is changed by a POST, PUT,
  or DELETE elsewhere in the app
- queries that share a key prefix but disagree on staleTime or gcTime

## Verify

Reproduce any invalidation gap with `tailorcv-ui-debug`: trigger the mutation,
then observe the stale render, or the missing refetch in the network panel. Do not
propose a fix without a reproduced miss.

## Finish

Report only real findings with a reproduction. Suggest the minimal change: a key
factory, an invalidation added to onSuccess, or an explicit staleTime. Do not
rewrite unrelated hooks in the same pass.
