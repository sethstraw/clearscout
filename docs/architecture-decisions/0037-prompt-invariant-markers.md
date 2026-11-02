# A prompt file marks the text an override may not change

Status: accepted

## Decision

The kernel is code and cannot be edited. But the prompt files restate its rules
for the model and add rules of their own. An override that deleted one would not
make anything fabricable, because enforcement lives in code. But a model told
less than the program expects is a model likelier to try what the program then
refuses. The refusal costs a run.

The text between `<!--invariant-->` and `<!--/invariant-->` is fixed. An override
has to reproduce every marked region word for word. It is refused at save and
refused again at load. A release that fixes new text must not be weakened by a
copy stored before the rule reached it. Everything outside a marker is editable:
the objective, the style, and the rubric. Those are the parts that belong to the
person doing the editing.

The alternative was naming the fixed regions in code by their section tag. That
reads the same for the XML-sectioned prompts. It does not read at all for
`structure_system`, which has markdown headings and carries the strictest
provenance rules in the suite. Marking in the file is uniform across both. It
puts the statement where the person editing the prompt is looking.

## Consequences

Markers are stripped before a request is built. No model receives one, and
`suite_fingerprint` hashes the stripped form. Adding a pair to a file therefore
moves neither the request nor the fingerprint. That is what makes it possible to
mark a rule without also running an evaluation.
