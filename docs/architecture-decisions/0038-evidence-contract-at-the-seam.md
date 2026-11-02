# The evidence contract is stated at the seam, not implied by a number

Status: accepted

## Decision

In matching, a stated personal fact answers a requirement and cites nothing. A
row matching cannot interpret becomes a gap. One way to hold that rule is to
force the strength to zero and let selection skip anything scoring zero. That
would hold the rule without saying anywhere what the rule was. Any change that
let one of those rows keep a strength would then put an achievement under the
applicant's own residency. No check would object.

A match carries how well its evidence is established. When it went unanswered,
it also carries why. Selection refuses on that directly. Both remain internal.
The model is asked for neither, and neither crosses the wire.

The two are separate questions on purpose. A self-attested fact is established
well enough to answer a requirement. It is never established well enough to
cite an achievement. That is why it sits outside the citable set rather than
being called weaker evidence.

## Where a gap comes from

Naming the cause of a gap separates two things a report would otherwise show
alike. One is a requirement the record does not answer. The other is one this
run failed to establish either way. Only the second is a defect in the run, not
a fact about the candidate. Calling it a gap tells somebody their CV is missing
something the run never asked about. They are separate everywhere they are
reported: the Markdown report, the CLI, the recommendation, and the job detail.

Where the line falls is the part worth keeping. A row the model never returned,
and a requirement it answered twice, are runs that learned nothing. A third case
is different. A model that held the whole record and returned a classification
with nothing cited did not fail. It had everything it needed and produced no
citation. That is the anti-fabrication catch working. Putting that third case on
the wrong side of the line is what the evaluation suite catches. One case exists
to pin exactly it.

## Consequences

The vocabularies live in the domain and are imported by the evaluation harness.
Two copies of an adjudication contract would agree until the day they mattered.
