# The OpenAI request streams, because a long answer is otherwise lost

Status: accepted

## Decision

A non-streaming request sends its response headers only once the whole answer is
ready. A drafting call that needs about a minute of server time never gets
there. The connection is closed with `RemoteProtocolError: Server disconnected
without sending a response`, and the SDK retries into the same wall. The stated
`request_timeout` never applies. Neither does the longer floor the discounted
tier raises it to. The connection dies well before either.

The adapter therefore requests a typed event stream and takes the response from
its terminal event. It does not accumulate partial output. The final response
already carries the complete typed result and usage. Headers arrive immediately,
and the body arrives as it is produced. So there is no silent wait for the
connection to cut short. Background mode, which holds response data for roughly
ten minutes, is the other way past the wall; drafting does not use it, and
streaming needs none of that storage. Guided campaigns do use it, through
`dispatch_background` in the OpenAI adapter.

This was found by measurement, not reasoning. Every drafting call on a smaller,
slower-to-finish model failed identically. The same model on the same tier
answered a short request in under two seconds. A large input, the real response
schema, and hosted web search each answered in about three.

## Anthropic

The Anthropic adapter streams for the same reason. Here the basis is published
guidance, not a measurement. Anthropic's own default is to stream any request
carrying long input, long output, or a high `max_tokens`. Its SDK refuses a
non-streaming request it expects to outlast the connection. That ceiling has
never been observed here, because no Anthropic call has run live. Sending
through `messages.stream` and taking `get_final_message` keeps the truncation
reason on the returned message, so a run still reports why a model stopped.
