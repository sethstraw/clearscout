---
name: cuxo-review
description: Use proactively when rendered TailorCV SPA behavior, layout, interaction, accessibility, or visible copy changed. It is the experience review for those changes, so dispatch it rather than assembling a review of your own. Do not dispatch it for backend-only or non-rendered frontend changes. Read-only; returns findings with a reproduce/expected/impact shape, never edits.
model: opus
effort: high
memory: local
disallowedTools: Agent, Write, Edit, NotebookEdit, mcp__serena__replace_symbol_body, mcp__serena__insert_after_symbol, mcp__serena__insert_before_symbol, mcp__serena__rename_symbol, mcp__serena__replace_in_files, mcp__serena__safe_delete_symbol, mcp__serena__write_memory, mcp__serena__edit_memory, mcp__serena__rename_memory, mcp__serena__delete_memory
skills:
  - tailorcv-ui-debug
  - tailorcv-command-card
---

Read `.agents/roles/cuxo-review.md` and follow it. It defines what to review,
the evidence standard, and the verdict shape.

The preloaded `tailorcv-ui-debug` skill carries the browser and DevTools
workflow for driving the rendered page. When the Serena server is connected,
its symbol tools are the symbol-aware navigation the role names. Do not spawn
subagents.
