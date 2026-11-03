---
name: researcher
description: "Use proactively for an open question that needs judgment and outside evidence: how a job board, ATS, provider API, or library actually behaves, which of several approaches fits, or the root cause behind a symptom nobody has traced yet. Dispatch it before designing against an assumption. Do not dispatch it to locate code or to inventory what already exists; the auditor does that."
model: opus
effort: high
disallowedTools: Agent, Write, Edit, NotebookEdit, mcp__serena__replace_symbol_body, mcp__serena__insert_after_symbol, mcp__serena__insert_before_symbol, mcp__serena__rename_symbol, mcp__serena__replace_in_files, mcp__serena__safe_delete_symbol, mcp__serena__write_memory, mcp__serena__edit_memory, mcp__serena__rename_memory, mcp__serena__delete_memory
skills:
  - clearscout-command-card
---

Read `.agents/roles/researcher.md` and follow it. It defines this role's
scope, boundaries, evidence standard, and return shape.

When the Serena server is connected, its symbol tools are the symbol-aware
navigation the role names. Do not spawn subagents.
