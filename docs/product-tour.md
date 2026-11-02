# Follow one requirement through TailorCV

On real postings, the question behind a draft is simple: what in the career
record supports this sentence? A score does not answer that. Neither does
polished wording.

This is a walk through how TailorCV keeps those questions close to the work.
You can also read the [prompts](../prompts/README.md) without running the
application. They show how the model is asked to read, match, draft, and critique.

This tour follows one job from lead to application history. The discovery and
evaluation screenshots come from a live workspace with employer names
redacted. The application timeline comes from the repository's synthetic demo
workspace. Its person, employers, postings, dates, scores, costs, and documents
are invented. Seeding that workspace makes no provider or
job-board request.

## Start with a lead

A URL returned by search or a board scan enters Pending discoveries. TailorCV
keeps how it was found, the publisher facts available before a full read, and
typed screening findings beside the lead, but it does not call that an
evaluation. Unknown facts remain visible rather than becoming an inferred
match. A board listing whose title alone is not enough to support or refuse it
stays in the durable screening-outcome ledger without entering Pending or
relevance review. The person can promote that exact URL into Pending and start
its policy-gated content read; no listing disappears because it missed the
automatic admission rule.

Board and sitemap reads are bounded per task. A supported source can have more
pages or posting URLs than one task can read. TailorCV then keeps the exact
next page or URLs as durable continuation work. It does not restart the source
or drop the unread tail. A sitemap read stopped by a discovery bound says so and
stays incomplete, so the part it reached is never reported as the whole board.

The optional search-vocabulary action suggests search terms from cited CV
evidence; it does not fetch postings or review the pending queue. A free scan
reads the registered sources, and explicit evaluation compares selected
postings with the CV. Historical relevance reviews remain visible with their
recorded inputs.

![Redacted live workspace: pending discoveries from a board scan with their relevance reviews](images/pending-discoveries.png)

Both leads came from a board scan, and each row keeps that origin with the date
it was first seen and its rank. The relevance review marks them Investigate and
names what a person still has to check, such as uncertain compensation. The
**Where it came from** filter separates board-scan leads from postings added by
URL. Employer names and board addresses are replaced with labels.

## A high score cannot fill an evidence gap

The posting below has an overall evaluation of 92. That helps order the list.
It does not know whether you want the job. It also leaves questions unanswered:
of 29 requirements read from the posting, 26 are answered by a CV entry and 3
have no cited candidate-record evidence.

![Redacted live workspace: a job evaluation retaining cited and unsupported requirements](images/evidence-and-gaps.png)

The posting asks for hands-on experience with a named tool and for occasional
travel. The selected CV evidence does not answer either one, so TailorCV shows the posting text and
states that no candidate-record match is stored. It does not infer a preference
or qualification from the rest of the score.

The distinction matters because a fit score orders work under the saved policy.
It is not permission to draft a missing fact.

## Drafting can use only the selected record

A tailoring run selects master-CV entries within the document budget before a
model drafts. The response schema can cite only those identifiers. Titles,
employers, dates, education, and publication citations are copied by the
program, and deterministic checks reject uncited lines or unsupported numbers.

The model can change the wording. Citation checks do not prove every semantic
claim: review qualifications, ownership, and metrics against the linked source.
[Truth and provenance](truth-and-provenance.md) states the complete rule.

If the CV omits relevant experience, discovery asks the person about
that gap without calling a model. A complete, explicitly confirmed answer
creates a new master-CV version; the earlier CV and the evaluation tied to it
remain unchanged.

A direct edit to the master CV is free only when the canonical parser accepts
it. An edit it rejects goes through ordinary import, which makes a paid
STRUCTURE call with no cost dialog, or fails when no key is set.

## Approval belongs to exact bytes

TailorCV stores each document proposal with its evidence identifiers and a
digest. A reviewer can accept it, reject it, or replace the wording. Replacement
is checked against the same evidence set, so review cannot turn an unsupported
source identifier into an approved one. Human review still has to assess the
meaning of the replacement.

If the proposal bytes change, the earlier approval no longer applies. Opening a
document or report approves nothing and calls no model.

## Later corrections do not erase the first record

Application status, notes, attachments, corrections, and reversals append to the
same local history. A correction points at the event it supersedes instead of
rewriting it.

![Synthetic application timeline retaining correction and reversal events](images/application-history.png)

The synthetic timeline keeps the original package-ready and offer events, the
correction, and the later reversal. The current state can change without making
the earlier record disappear.

## What this tour establishes

The seeded workspace lets you inspect the screens and records without a live
service. You can follow a source, see an unanswered requirement, review a draft,
and read a correction without losing the earlier event.

It does not prove that a live model will make a good fit judgment or that a
board will still answer. Nor does it prove that a renderer will produce a
suitable PDF, or that a tailored resume will change a hiring outcome. [Validation
status](validation.md) explains those limits.

To inspect the workspace yourself, follow [Try the synthetic
workspace](../README.md#try-the-synthetic-workspace). To use a real CV after a
release exists, continue with the [user guide](user-guide.md).
