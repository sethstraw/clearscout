# Long work runs in a durable queue

Status: accepted

## Decision

Long operations use a database task queue and a separate local worker. Tasks
carry payload digests, idempotency keys, attempts, leases, heartbeats,
cancellation, bounded failures, progress, and immutable sequenced events.
Server-sent events stream persisted events, and polling stays possible.

## Consequences

The supported scale is one local SQLite database and one worker.
