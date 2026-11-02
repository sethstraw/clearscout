The document under review is a tailored {{document}} for the target role below.

Target role: {{role}}

{{requirements}}

{{asserted}}

Any line in that last block that appears in the {{document}} was copied from the
applicant's own record or supplied in their settings. No model wrote it and no
rewrite can change it. These lines are stated by the applicant rather than
evidenced: their name and any credential written beside it, contact details,
work authorization, residency and citizenship, each role's employer, title,
title of record, dates and location, and phrases the applicant requires
verbatim.

<!--invariant-->
Do not list any of them in `unsupported_claims`, and do not report them as
missing from the record. Their accuracy is the applicant's responsibility and
not this review's. Score the {{document}} as though they are true.
<!--/invariant-->

{{draft}}

<!--invariant-->
The block below is a citation index for the {{document}} above: each written
line, or in a letter each paragraph, under the identifiers it cites, then the
text of each record entry it cites, as the candidate record shows it. A company
research identifier has no text beneath it; its text is in the company research
block. The index is not the document. Judge every axis from the {{document}}
itself; the index decides only which entries a claim rests on.

A claim is supported when the entries that line cites carry it, and a line
citing several entries is supported when they carry the claim between them.
Compare each line with the entry text beneath it for who did the work, how much
of it, and how far it got. A line that credits the candidate with work its
entries give to a team, or with leading more of the work than its entries say
the candidate led, claims more than they carry. An entry elsewhere in the
evidence that happens to carry the claim does not support it, because the line
does not rest on that entry; list that claim in `unsupported_claims` under the
`miscited` boundary. A line shown as citing nothing is judged against the
evidence as a whole.

Use this index to trace a claim, not as a reason to find more. A line whose
cited entries carry its claim is supported, however few entries it cites and
whichever entry you would have chosen instead. Restating the cited entries in
more general words is not a miscitation, as long as the line claims nothing
they do not. A line copied from the asserted block stays out of
`unsupported_claims` whatever it cites.
<!--/invariant-->

{{citations}}

<!--invariant-->
The block below lists the identifiers of the record entries this {{document}}
may draw on, one per line. Score `alignment`, and write `weaknesses` and
`guidance`, against the offered evidence: judge and name only the entries
listed in the offered_evidence block. `unsupported_claims`, including the
`miscited` boundary, is still judged against the whole record. An entry outside
this list may not be cited by the {{document}}, and it still counts for
`unsupported_claims`. A claim that only an entry outside this list carries is
`miscited`, never `absent_from_record`.
<!--/invariant-->

{{offered}}

<!--invariant-->
The block below is company research the {{document}} may cite for statements
about the role or employer. A statement about the employer is supported when
this block states it. Company research never supports a claim about the
candidate. An empty block means none was supplied.
<!--/invariant-->

{{research}}

<!--invariant-->
Confine `unsupported_claims` to prose written for this {{document}}:
{{written_parts}}. Those are the parts a model wrote and a rewrite can change.
Everything else in the document, such as a heading the record supplies or a
greeting and sign-off, was copied from the applicant's record or written by
the program.

Judge the boundary a listed claim crosses against the evidence and any company
research in this request, never against the posting's requirements. A
requirement the {{document}} leaves unanswered is a weakness, not a crossed
boundary.
<!--/invariant-->

<!--invariant-->
The block below is a user-supplied hint the applicant added to this posting.
It is not evidence, it never became evidence, and it is not a claim the
{{document}} must satisfy. Use it only to understand what the applicant was
aiming for. Do not raise `unsupported_claims` because the {{document}} does not
mention it, do not treat it as a factual assertion, and do not report it in
your findings.
An empty block means no hint was set.
<!--/invariant-->

{{posting_hint}}

<!--invariant-->
The block below lists requirements the applicant has DISPUTED on the current
posting. The applicant's ruling stands unless you can point to new posting
text that was not seen at the first extraction. Do not raise
`unsupported_claims` because the {{document}} does not answer a disputed
requirement, and do not report the dispute itself as a finding. An empty
block means no active disputes.
<!--/invariant-->

{{active_disputes}}
