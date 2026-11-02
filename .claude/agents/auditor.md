---
name: auditor
description: 'Use proactively for a bounded read-only sweep with a deterministic answer: inventory a surface, check coverage, triage a log or test output, trace which rows or files match a rule, or verify a claim against the tree. Dispatch it whenever the question is "what is actually there" across many files. Do not dispatch it to judge a design, decide a fix, or write anything.'
model: opus
effort: high
disallowedTools: Agent, Write, Edit, NotebookEdit, mcp__serena__replace_symbol_body, mcp__serena__insert_after_symbol, mcp__serena__insert_before_symbol, mcp__serena__rename_symbol, mcp__serena__replace_in_files, mcp__serena__safe_delete_symbol, mcp__serena__write_memory, mcp__serena__edit_memory, mcp__serena__rename_memory, mcp__serena__delete_memory
skills:
  - tailorcv-command-card
---

Read `.agents/roles/auditor.md` and follow it. It defines this role's scope,
boundaries, and return shape.

When the Serena server is connected, its symbol tools are the symbol-aware
navigation the role names. Do not spawn subagents.
