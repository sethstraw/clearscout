# Prompts stay files, and the database holds only what somebody edited

Status: accepted

## Decision

Files plus git already give versioning, diffing, and blame, and they give better
ones than this. `git log -p prompts/draft_system.md` answers more than any
version list here does. Nothing below is an improvement on it.

Four narrow things it does not do. The person editing a prompt is not
necessarily a git user. Telling them to learn one to change a sentence is
telling them not to change it. The SPA cannot shell out to git. A local server
that could would be a command execution surface added for the sake of a history
view. A run needs a foreign key to the exact text it was produced under. A
commit-message convention cannot supply that, and a query can. And a workspace
export has to carry its prompts, or the export restores into a workspace that
runs different ones.

So the packaged file stays the default and stays in `prompts/`. A prompt nobody
has edited has no row. `load` resolves an override if one exists and the file
otherwise. An override is the whole file, not a merge of keys into it. Saving
appends an immutable version. Reverting to an earlier one appends a copy of it
rather than moving a pointer. That makes "this was a revert" a digest
comparison, not a label somebody has to keep true. Resetting archives the override, and the file
answers again. That is reversible where deleting immutable history could not be.

Text is normalized before it is hashed. Otherwise an override differing from the
packaged file only in line endings would move `suite_fingerprint`. It would miss
the reuse check and bill for a run that would have produced the same package.

Two gates rather than one. A prompt is saved only if its `{{placeholders}}` are
exactly the ones the calling code fills. The dialect stays that one
substitution: no expressions, conditionals, loops, or filters. `render` would
refuse a mismatch anyway, but it refuses in the middle of a paid run.

## Consequences

If those four things do not hold for you, files plus git is the better answer
and this is a worse one.
