---
paths:
  - "src/**/*.py"
  - "tests/**/*.py"
  - "evals/**/*.py"
  - "web/src/**/*.ts"
  - "web/src/**/*.tsx"
---

# Picking the tool for the change shape

`AGENTS.md` says when to reach for symbol tools. This adds what it leaves out.

A text search-and-replace across files edits comments and strings along with
code. Use it for literal text, and a symbol-aware rename for renames.

For a structural change across many call sites, write a `libcst.codemod`
command rather than a per-file regex loop. The AST tool moves imports and call
sites together, which is the part a regex loop gets wrong.
