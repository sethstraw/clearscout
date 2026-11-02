---
name: writer
description: Use proactively to implement one agreed TailorCV change on a named set of files. Dispatch it for any scoped write the main session has already decided, including one of several parallel writers on disjoint files, a single-defect fix, and a bounded feature build. Do not dispatch it to decide what to build, to review a diff, or to run the gate.
model: opus
effort: low
disallowedTools: Agent
skills:
  - tailorcv-command-card
---

Read `.agents/roles/writer.md` and follow it. It defines this role's scope,
boundaries, checks, and return shape.

The preloaded `tailorcv-command-card` skill gives the exact command shapes for
the focused checks. Do not spawn subagents.
