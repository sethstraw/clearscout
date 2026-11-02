# Discovery deduplication is identity, not freshness

Status: accepted

## Decision

Normalized URLs prevent a second pending row or ledger job. But a later board
result still carries new evidence. Pending rows are mutable and take the board's
latest fields. Ledger jobs instead go through polite refresh and append an
observation. That refresh reads structured facts again. It abstains when the
current body does not state one. It does not copy an earlier fact into a later
observation merely because the body hash or URL identity stayed the same.

## Consequences

Pending discoveries do not own retry counters or a retry calendar. Queueing
another sweep, durable task failure reporting, and frontier continuation are the
three existing mechanisms for trying work again. Continuation retries an
automatic frontier row up to `MAX_AUTOMATIC_ATTEMPTS` times within a scan. A
row that still failed gets one more retry in the next scan.
