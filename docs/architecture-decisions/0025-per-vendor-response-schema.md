# One model, two vendor documents, built by the application

Status: accepted

## Decision

A response schema is one Pydantic model. `infrastructure/providers/base.py`
renders it into each vendor's own JSON Schema document. It does not hand the
model to either SDK's parse helper. Each adapter is sent its own vendor's
document.

## Why not the SDK helpers

The helpers do not agree. Anthropic copies `required` through unchanged and
demotes `minimum` and `maximum` into a description. OpenAI promotes every
property into `required` and enforces the bounds. One model would therefore mean
two contracts, and the loose one is the field that matters. A critic that simply
omits `unsupported_claims` produces an empty list. The preservation gate reads
that as every claim being supported. A default must never be able to stand in
for an answer the model declined to give.

Owning the request is also what makes owning the response possible. Both helpers
validate inside the call, so a response truncated at the token ceiling raises a
Pydantic error from inside the SDK. That happens before `refuse_unusable` can
look at why generation stopped.

## Why not one document for both

OpenAI accepts numeric, string-length, and array bounds. Anthropic accepts none
of them beyond `minItems` of 0 or 1. Sending the union would mean sending
Anthropic keywords it documents as unsupported. The SDK transformation that
would strip them belongs to the parse helpers this application does not use.

A bound Anthropic cannot express is stated in the description, not dropped.
That is what Anthropic's own SDKs do. The instruction survives even though the
keyword cannot. The response is validated against the unnarrowed model either
way, and that is what actually enforces a bound.

## Consequences

The cache key and the run manifest describe the document that was sent. They do
not describe a third one nobody receives. Changing how a vendor's document is
narrowed invalidates its cached answers.
