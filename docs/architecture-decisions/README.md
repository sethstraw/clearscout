# Design decisions

Why ClearScout is built this way, one architecture decision record per decision
that would be expensive to reverse, each describing the rule the code follows.
Product rules for evaluation, drafting, model calls, and discovery are in
[ARCHITECTURE.md](../../ARCHITECTURE.md).

## Foundations

- [0001](0001-package-ownership.md): six top-level Python
  boundaries and the SPA's page, feature, and API split.
- [0003](0003-one-set-of-services.md): the CLI, API, and
  worker are ways in to one set of application services.
- [0004](0004-immutability-in-the-database.md): database
  triggers enforce immutability and linear evaluation chains.
- [0005](0005-explicit-schema-and-transfer.md): schema
  changes, workspace creation, and database transfer are explicit commands.

## Product truth

- [0036](0036-quotes-are-checked.md): every extracted
  quote is placed in the fetched posting body.
- [0038](0038-evidence-contract-at-the-seam.md): a match
  states how its evidence is established and why a gap is a gap.

## Models and prompts

- [0014](0014-prompt-files-and-overrides.md): prompts
  stay files, and the database holds only overrides.
- [0015](0015-candidate-record-is-data.md): a candidate
  record is user content, and the kernel is code.
- [0025](0025-per-vendor-response-schema.md): the
  application renders each vendor's response schema from one model.
- [0035](0035-response-model-docstrings-are-prompts.md):
  a response model's docstring is prompt text.
- [0037](0037-prompt-invariant-markers.md): marked prompt
  regions cannot be changed by an override.
- [0039](0039-pinned-model-snapshots.md): vendors pin
  snapshots, and every stage records the model that answered.
- [0047](0047-streamed-provider-requests.md): provider
  requests stream so long answers are not lost.

## Search and discovery

- [0011](0011-search-is-configuration.md): search
  parameters are versioned configuration, not code.
- [0012](0012-discovery-deduplication-is-identity.md):
  discovery deduplication is identity, not freshness.
- [0013](0013-published-policy-decides-reads.md):
  published policy decides reads, with one single-posting exception.
- [0049](0049-four-url-folds.md): a posting URL is folded
  five ways, each with one owner.

## Local application

- [0002](0002-model-admin.md): a read-only model admin
  behind the loopback bind.
- [0008](0008-local-spa-without-secrets.md): a
  same-origin SPA that never sees secrets.
- [0009](0009-generated-wire-contract.md): the generated
  OpenAPI contract is the wire, and CI checks it.

## Operations

- [0007](0007-durable-task-queue.md): long work runs in a
  durable database queue with one worker.
- [0026](0026-costs-never-stop-approved-work.md): cost
  estimates inform and never stop approved work.
