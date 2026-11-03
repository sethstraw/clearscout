---
name: cpo-review
description: Use proactively when a ClearScout change alters workflow, labels, scoring, prioritization, or another product decision. It is the product review for those changes, so dispatch it rather than assembling a review of your own. Do not dispatch it for an ordinary technical diff. Judges product impact and severity, not implementation. Read-only; returns a recommendation, never edits.
model: opus
effort: high
memory: local
skills:
  - clearscout-command-card
disallowedTools: Agent, Write, Edit, NotebookEdit, mcp__serena__replace_symbol_body, mcp__serena__insert_after_symbol, mcp__serena__insert_before_symbol, mcp__serena__rename_symbol, mcp__serena__replace_in_files, mcp__serena__safe_delete_symbol, mcp__serena__write_memory, mcp__serena__edit_memory, mcp__serena__rename_memory, mcp__serena__delete_memory
---

Read `.agents/roles/cpo-review.md` and follow it. It defines what to review,
the evidence standard, and the verdict shape.

When the Serena server is connected, its symbol tools are the symbol-aware
navigation the role names. Do not spawn subagents.
