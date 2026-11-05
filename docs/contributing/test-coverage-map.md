# What the public checks can prove

The repository discovers its tests and gate steps from executable sources. A
file-by-file inventory here would become stale, so this page records only the
stable boundaries.

## Deterministic tests prove local outcomes

Python tests cover domain rules, application workflows, persistence, the local
API, CLI behavior, provider adapters over controlled transports, and complete
synthetic journeys. Web tests render components in jsdom and exercise user
interactions against typed fixtures.

Those tests can prove the configured input produced the asserted result. They
do not prove live service behavior, real browser layout, operating-system
process contention, or human agreement with a fit judgment.

## Contract tests prove the seam they cross

Official-SDK tests check request construction and response parsing without
reaching a provider. Recorded cassettes, when present, prove that captured bytes
still parse. A synthetic replay seam is not evidence that a full paid run was
observed.

Generated checks export OpenAPI and the TypeScript client to temporary paths,
then compare them with tracked output. They prove the server and client share
the generated contract in this source tree; they do not prove an untyped caller
uses it correctly.

## Evaluations prove bounded behavior

Scripted evaluations run named cases through shipped stages with network access
disabled. Expectations state an invariant. Baselines record measurements that
need human interpretation when they move.

Replay does not measure live prompt quality. Human rulings calibrate the method
and are not gold labels. See [evals/README.md](../../evals/README.md) and
[evaluation methodology](../../evals/inspect/METHODOLOGY.md).

## The gate chooses frequency, not meaning

`scripts/gate.sh change` runs checks an edit can invalidate. `push` runs package,
advisory, and workflow checks whose answers can change without a source edit.
`all` combines both. The scopes change how often a check runs; they do not widen
what that check proves.

[CONTRIBUTING.md](../../.github/CONTRIBUTING.md) owns the commands. [Validation
status](../validation.md) owns live-provider, board, browser, and renderer limits.
