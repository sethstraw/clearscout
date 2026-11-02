# A candidate record is data, and the kernel is not editable

Status: accepted

## Decision

Two kinds of text reach a model: what this program wrote, and what it collected.
They go to different places. OpenAI's `instructions` and Anthropic's `system`
carry application-authored text only. A candidate record is a content block in
the user turn. That is the vendors' own boundary, not one invented here. OpenAI
ranks `developer` above `user`. Anthropic states that system content is not a
place for text from outside the conversation. A CV may have come from anywhere,
so it is never the highest-authority text in a request.

Every prompt under `prompts/` can be rewritten from the web client. That is the
point of the prompt library. But a rule an override can delete is not an
invariant. The anti-fabrication and untrusted-input rules therefore live in
`prompts.py` as code and lead every request. They are hashed into
`suite_fingerprint` like any other prompt content. The prompt files restate that
wording for the model. The restatement is fixed too, by the marked regions in
[0037](0037-prompt-invariant-markers.md).

## Consequences

The boundary costs a cache hit on Anthropic. Anthropic hashes its prefix as
tools, then system, then messages. A breakpoint on the record sits behind the
stage rubric, so the prefix differs per stage. The record is shared across
repeats of one stage, not across a whole run. The per-call manifest records
cache reads and writes, so the size of that is measured rather than argued.

On OpenAI the same placement is what makes caching possible at all. A
breakpoint is accepted only on an input block, never on the `instructions`
string. The model's implicit breakpoint sits on the latest message. A record
inside `instructions` can therefore never be covered by one. It is an input
block carrying an explicit breakpoint. `prompt_cache_options.mode` is set to
`explicit` so a single-draw task behind it is not billed as a cache write; a
task sampled several times carries its own breakpoint, so later draws read it.
