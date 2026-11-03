---
name: clearscout-ui-debug
description: Reproduce, diagnose, and verify ClearScout SPA behavior with synthetic data and browser evidence. Use for UI defects and failed flows in the SPA.
---

# Debugging the web client

Read `AGENTS.md` and `docs/contributing/frontend-development.md` before acting. Preserve
unrelated working-tree changes. Use only synthetic or scratch data.

## Choose the smallest useful tool

- Use a browser automation tool such as Playwright for navigation, forms,
  screenshots, accessibility snapshots, and repeatable regression evidence.
- Use browser developer tools for console errors, request timing, network
  payload shape, source inspection, and performance traces.
- Use symbol search for source definitions and references once the visible
  failure is localized.

## Workflow

1. Inspect repository status. Take the API and Vite commands from `justfile`
   and `web/package.json`. By default the API listens on port 8765 and the Vite
   dev server on port 5173, proxying API calls to the API.
2. Create a scratch workspace with synthetic data:
   ```sh
   uv run --no-sync python scripts/seed_demo_workspace.py --data-root <scratch-dir>
   uv run --no-sync clearscout serve --database-url sqlite+pysqlite:///<scratch-dir>/clearscout.sqlite3 --data-root <scratch-dir>
   ```
   Never seed fake events into a real workspace. An inherited
   `CLEARSCOUT_DATABASE_URL` beats `--data-root`, so name the scratch database
   explicitly.
3. Start only the servers the failing path needs. Record their addresses and
   stop them when the investigation ends.
4. Reproduce the user-visible failure before editing. Capture the minimal
   evidence: steps, visible state, console messages, and relevant network calls.
5. Trace the failure from the rendered component to the query or mutation, the
   generated API types, and the backend contract. Fix the existing owner rather
   than adding a parallel component, hook, cache key, or request path.
6. Add or update a focused Vitest test when the regression is stable enough to
   encode. Watch it fail before the fix when practical.
7. Re-run the same browser path after the fix, then run SPA formatting, type
   checking, and focused tests. The coordinating session runs the gate scope
   that fits the change.

Keep screenshots, traces, downloads, and logs in a scratch directory outside the
repository, and delete them when you finish. Do not commit `.playwright-mcp`,
browser profiles, private data, or captured provider output.

Report the reproduced cause, the owner changed, before and after browser
evidence, checks run, and any unverified edge. Do not claim a UI fix from unit
tests alone.
