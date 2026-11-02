Target role: {{role}}
Employer: {{company}}

Write the letter from this evidence only. An indented line belongs to the item
above it and is cited by that item's identifier.

{{evidence}}

<!--invariant-->
Use the company research below only for statements about the role or employer.
Each such statement must cite its Source ID. If the block contains only the
posting, do not imply broader company research.
<!--/invariant-->

{{research}}

Address applicable feedback from the previous attempt. On the first attempt the
block below says so.

{{feedback}}

When the block below holds the previous attempt, edit that letter rather than
writing a new one, and return every paragraph. Revise as far as each piece of
feedback requires:

- Feedback on the whole letter, such as its length, its completeness, or the
  critic's guidance, may be answered wherever in the letter that takes.
- Feedback that a paragraph cites no evidence, unknown evidence, or evidence
  that does not support it may be answered by changing that paragraph's
  `source_ids` as well as its sentences.
- Otherwise, rewrite only the sentences the feedback quotes or names.

Keep every sentence no feedback touches word for word in its paragraph. On the
first attempt the block says so.

{{previous_letter}}

<!--invariant-->
The block below is a user-supplied hint the applicant added to this posting.
It is not evidence and it never became evidence. Use it only to shape framing:
which company signals to draw on, which paragraph to open with, or what tone
the applicant wants. Do not cite it in any `source_ids`, do not treat it as a
claim about the applicant's experience or the employer's operations, and do
not introduce any fact that appears only here. An empty block means no hint
was set.
<!--/invariant-->

{{posting_hint}}

<!--invariant-->
The block below lists requirements the applicant has DISPUTED on the current
posting. Treat these rulings as authoritative: shape the letter as though the
applicant meets or is not obliged to meet those requirements. Do not raise
them as concerns and do not overrule the ruling silently. An empty block
means no active disputes.
<!--/invariant-->

{{active_disputes}}
