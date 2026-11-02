# A quote is a claim about the posting, and it is checked

Status: accepted

## Decision

Extraction asks for the span of the posting that supports each requirement and
each hard gate. Unchecked, a condition the model composed out of its own reading
is indistinguishable from one the employer wrote. A report would present both as
stated conditions.

`application/tailoring/stages/spans.py` places every quote in the fetched body
before the run goes on. Comparison collapses whitespace runs and folds case,
because a model asked to copy a line reflows it. It reads nothing else as
harmless. A changed word or mark is a changed claim. A quote that is found keeps
the offsets of the original text. So the span is provenance a reader can follow,
not a boolean.

A quote that is not found is kept as `needs_review`, not dropped. Dropping would
hide a condition somebody should look at. Accepting would let the program repeat
wording no employer wrote. The gate stays visible, stays out of verified
scoring, and is reported as needing a person.

## Consequences

A hard gate without a valid source span is not a verified hard gate. It cannot
make a posting a skip. It cannot cluster as an absolute condition across a
batch. It is labeled advisory in the stored report.

Keywords are checked the same way, and dropped rather than kept. A keyword is
scored as a phrase the posting used, so one the posting does not contain is not
a weaker term but a different kind of thing. There is no reading of coverage
that it belongs in.
