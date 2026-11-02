---
name: generated-artifact-sentinel
description: Detect drift between hand-edited TailorCV generated artifacts and their source. Use on a request to check for generated drift, before committing a change that touches generated files, and whenever a diff changes generated files without an obvious regeneration step. Do not use for checked-in files that are authored by hand, such as fixtures and reviewed snapshots.
---

# TailorCV generated artifact sentinel

A hand edit to a generated file is overwritten by the next generate run, and it
hides the real drift until then. This skill lists the generators, maps their
outputs, and stops a hand edit from shipping silently.

## Enumerate generators

Read `scripts/generate.sh` and every script or command it invokes, and list
each output with the generator that writes it. Include the list in your report.

## Scan the diff

For the working tree, or a named ref pair:

```sh
git diff --name-only <from>..<to>
```

Cross-reference the changed paths against the generated list.

## Flag drift

For each changed generated file, identify its generator, configuration, pinned
version, and source inputs. A missing source edit in the same diff is a prompt
to investigate, not proof of corruption:

- the OpenAPI JSON or the TypeScript types changed, but no Pydantic wire model
  or route under `src/tailorcv/delivery/api/` changed
- the gazetteer changed, but `scripts/build_gazetteer.py` and its inputs did not
- the notices changed, but `uv.lock` and `web/package-lock.json` did not

The coordinating session runs `scripts/gate.sh generated`, which regenerates
into temporary outputs and compares them with the checked-in files. Stop only when the regenerated artifact
still disagrees or the generator cannot account for the change.

## Recover

Regenerate:

```sh
scripts/generate.sh
```

Then diff again. If the generated file returns to its committed state, the hand
edit was the drift; discard it. If the file changes further, an upstream source
change went unrecorded; investigate before proceeding.

## Finish

Report the generators, the flagged pairs, and the recovery action taken.
