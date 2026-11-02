# A docstring on a response model is prompt text

Status: accepted

## Decision

Pydantic copies a class docstring into the JSON Schema as `description`. The
schema built from these models is what goes on the wire. `RawHardGate` and
`Classification` therefore have docstrings the model reads as part of its
instructions. They are prompt text kept beside the type it constrains, not in
`prompts/`. They describe a field, not a task.

## Consequences

`infrastructure/providers/client.py` puts the schema it sends into `cache_key`
on purpose. Editing one of those sentences changes what the provider is told. It
invalidates every cached answer under the old wording and stops matching the
recorded cassette. Treat them as versioned prompt content. A change to one is a
prompt change, not a comment change. `tests/durability/test_prompts.py` pins the
ones that reach a schema.
