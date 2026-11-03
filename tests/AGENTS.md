# Python test subtree

`docs/contributing/backend-development.md` "Writing the tests" owns naming, effect seams, and
the red-then-green rule. This file covers reuse and placement, where the suite
drifts: private helpers are redefined across files, and a few mixed-owner
modules keep absorbing unrelated cases.

## Reuse before writing a helper or fixture

Search before defining anything at module level in a test file. Use Serena's
symbol tools first, as the root `AGENTS.md` says, and `rg` as the fallback:

- The symbol overview of `tests/support/` lists the shared builders and
  doubles. `tests/support/scenarios.py` builds workspaces, sources, listings,
  evaluation commits, and fit reports; `artifacts.py` gives `runtime_storage`;
  `doubles.py` holds scripted providers, board readers, and fakes;
  `payloads.py` and `plans.py` build provider scripts and plans; `cassette.py`
  and `http.py` own transport doubles.
- `rg -n '@pytest.fixture' tests` lists fixtures already in scope; the shared
  ones live in `tests/conftest.py`, the only conftest under `tests/`.
- A symbol search for `_<name>` under `tests/` finds a private helper another
  file already wrote. A helper that exists in two test files moves to the
  `tests/support/` module that owns its domain, and both files import it in the
  same change.

Compare the bodies before calling two helpers the same helper. A shared name is
not evidence: one private helper name can cover several different functions
with different signatures across files. Merging on the name alone produces the
universal builder this file forbids.

A new support function needs a named domain responsibility and at least two
consumers. No universal fixture object, no builder that knows every domain, and
no `SimpleNamespace` stand-in for a payload: construct the real Pydantic model.

## Put the case in the owner's file

A test belongs to the test module of the production owner it exercises, found
from the package docstrings under `src/clearscout/`. Extend that module rather
than starting a parallel one, and start one only when the owner has none.

`tests/loop/test_discovery_screening.py` is the test module for
the `src/clearscout/application/discovery/screening/` package, and a new case about screening
boundaries, bulk staging, holdouts, or arrangement vocabulary belongs in it.
Role-family, adjacency, rung, and bare-function title rules live in
`tests/loop/test_titles.py` with their production owner, so a case about what a
title may claim goes there instead.

A delivery test proves request validation, status mapping, and response shape
for its route. The service branches behind the route are tested at the service.

## Shape of a test

- One behavior per test, asserted through the public entry point: the result,
  the stored row, the emitted response. Not underscore-prefixed helpers or call
  counts, unless the call is the contract.
- `pytest.raises` names the contract exception and wraps only the raising
  statement.
- Parametrize rows that mean the same thing; different workflows get separate
  tests.
- Synchronize concurrency with events, barriers, or a controlled clock, under
  the root `AGENTS.md` rule that tests never wait on the clock.
- When a change removes behavior, delete its tests in the same change.

Run what you touched in the focused form the `clearscout-command-card` skill
gives, and read the log once.
