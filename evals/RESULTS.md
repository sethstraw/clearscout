# Live evaluation results

Every paid `scripts/eval.sh --live` run on the synthetic evaluation sets, with
its outcome and cost. Runs against a real workspace appear only in their own
section below, reduced to counts and costs, because their inputs are private.

These are defect-discovery numbers on 4 to 19 cases. They show which cases a
model got wrong under one prompt suite, not an accuracy a reader can carry to
real postings. `inspect/METHODOLOGY.md` explains what each family measures and
what it cannot.

A row names the prompt fingerprint and the dataset or selection-manifest digest
its log records. Two rows compare only where those match: labels and prompts
changed between runs, so a rising count across rows with different digests is
not progress on a fixed test. Cost is the priced spend the log records, in USD.

## Match

Scored against `golden/match.jsonl`. `correct` counts trials whose
classification equals the frozen label.

| date       | model         | effort / tier  | cases x trials | correct | cost     | prompt       | manifest |
| ---------- | ------------- | -------------- | -------------- | ------- | -------- | ------------ | -------- |
| 2026-09-04 | gpt-5.6-terra | low / standard | 10 x 1         | 10/10   | 0.023283 | a9e594159080 | d70afd58 |
| 2026-09-23 | gpt-6-sol     | low / flex     | 10 x 3         | 22/30   | 0.031106 | 1555178428ae | d70afd58 |
| 2026-09-23 | gpt-6-luna    | low / flex     | 10 x 3         | 19/30   | 0.002155 | 1555178428ae | d70afd58 |
| 2026-09-23 | gpt-6-sol     | low / flex     | 10 x 3         | 27/30   | 0.031360 | e1d71c34b966 | 3b54614a |
| 2026-09-23 | gpt-6-sol     | low / flex     | 13 x 3         | 34/39   | 0.042229 | 055b4360bb3c | d55cc1cd |
| 2026-09-23 | gpt-6-sol     | low / flex     | 13 x 3         | 36/39   | 0.041343 | 4adafe2f4508 | d55cc1cd |
| 2026-09-23 | gpt-6-sol     | medium / flex  | 13 x 3         | 39/39   | 0.057228 | 4adafe2f4508 | d55cc1cd |
| 2026-09-23 | gpt-6-sol     | medium / flex  | 13 x 3         | 38/39   | 0.057846 | 4adafe2f4508 | c1d3201a |
| 2026-09-23 | gpt-6-sol     | medium / flex  | 13 x 3         | 39/39   | 0.060968 | 93253db7d4ab | c1d3201a |
| 2026-09-24 | gpt-6-sol     | medium / flex  | 18 x 3         | 48/54   | 0.080542 | 40a92b47a711 | e920e553 |
| 2026-09-24 | gpt-6-sol     | medium / flex  | 19 x 3         | 52/57   | 0.082349 | ea44cf46c98b | 687cf4a3 |
| 2026-09-24 | gpt-6-sol     | medium / flex  | 19 x 3         | 57/57   | 0.081039 | 4e746f175e60 | 35d43845 |

Manifests `d70afd58` and `3b54614a` carry the same ten labels; the second drops
two retention fields. `d55cc1cd` keeps those ten and adds three cases where the
entry only took part in the requested work. Trials passed per case, in row
order:

| case                                                                            | label          | 09-04 terra | 09-23 sol | 09-23 luna | 09-23 sol | 09-23 sol | 09-23 sol | 09-23 sol |
| ------------------------------------------------------------------------------- | -------------- | ----------- | --------- | ---------- | --------- | --------- | --------- | --------- |
| Production Python service development at high event volume                      | DIRECT         | 1/1         | 3/3       | 1/3        | 3/3       | 0/3       | 1/3       | 3/3       |
| Collaborates directly with engineering and customer support to deliver releases | DIRECT         | 1/1         | 1/3       | 1/3        | 3/3       | 3/3       | 3/3       | 3/3       |
| Improves production readiness and scalability of payment services               | TAILORING_ONLY | 1/1         | 0/3       | 1/3        | 0/3       | 3/3       | 3/3       | 3/3       |
| Builds trusted cross-functional partnerships to deliver shared outcomes         | TAILORING_ONLY | 1/1         | 3/3       | 3/3        | 3/3       | 1/3       | 3/3       | 3/3       |
| Coaches a team through ambiguous strategic decisions                            | TRANSFERABLE   | 1/1         | 3/3       | 3/3        | 3/3       | 3/3       | 3/3       | 3/3       |
| Production infrastructure delivery using Helm charts                            | TRANSFERABLE   | 1/1         | 3/3       | 2/3        | 3/3       | 3/3       | 3/3       | 3/3       |
| Must be authorized to work in Canada without sponsorship                        | ASSERTED       | 1/1         | 3/3       | 3/3        | 3/3       | 3/3       | 3/3       | 3/3       |
| Must reside in Calgary, Alberta                                                 | ASSERTED       | 1/1         | 3/3       | 3/3        | 3/3       | 3/3       | 3/3       | 3/3       |
| Five years of production Rust development                                       | GAP            | 1/1         | 0/3       | 0/3        | 3/3       | 3/3       | 3/3       | 3/3       |
| Persuades executive stakeholders to reverse contested portfolio decisions       | GAP            | 1/1         | 3/3       | 2/3        | 3/3       | 3/3       | 3/3       | 3/3       |
| Scales payment platform services to meet production load                        | TAILORING_ONLY | -           | -         | -          | -         | 3/3       | 2/3       | 3/3       |
| 3+ years improving production readiness of payment services                     | TRANSFERABLE   | -           | -         | -          | -         | 3/3       | 3/3       | 3/3       |
| Leads production readiness and scaling work for payment services                | TRANSFERABLE   | -           | -         | -          | -         | 3/3       | 3/3       | 3/3       |

Manifest `c1d3201a` labels strength as well as classification, under a rubric
that also classifies behavioral evidence; it swaps two behavioral cases for two
new ones. `strength` counts
trials whose strength equals the frozen label, among trials whose
classification matched, on evidence-backed labels only (the product answers
`none` for every `ASSERTED` and `GAP`): 20 of 26 on the run at prompt
`4adafe2f4508` and 27 of 27 at prompt `93253db7d4ab`. Per case:

| case                                                                                         | label                   | 4adafe2f4508 | strength | 93253db7d4ab | strength |
| -------------------------------------------------------------------------------------------- | ----------------------- | ------------ | -------- | ------------ | -------- |
| Production Python service development at high event volume                                   | DIRECT strong           | 2/3          | 2/2      | 3/3          | 3/3      |
| Collaborates directly with engineering and customer support to deliver releases              | DIRECT strong           | 3/3          | 3/3      | 3/3          | 3/3      |
| Improves production readiness and scalability of payment services                            | TAILORING_ONLY moderate | 3/3          | 3/3      | 3/3          | 3/3      |
| Scales payment platform services to meet production load                                     | TAILORING_ONLY moderate | 3/3          | 3/3      | 3/3          | 3/3      |
| Steps in to clear the blockers that stall shared delivery work                               | TAILORING_ONLY strong   | 3/3          | 0/3      | 3/3          | 3/3      |
| Production infrastructure delivery using Helm charts                                         | TRANSFERABLE weak       | 3/3          | 3/3      | 3/3          | 3/3      |
| 3+ years improving production readiness of payment services                                  | TRANSFERABLE weak       | 3/3          | 3/3      | 3/3          | 3/3      |
| Leads production readiness and scaling work for payment services                             | TRANSFERABLE weak       | 3/3          | 0/3      | 3/3          | 3/3      |
| Aligns a dozen independent product groups on owners and priorities for a company-wide launch | TRANSFERABLE weak       | 3/3          | 3/3      | 3/3          | 3/3      |
| Must be authorized to work in Canada without sponsorship                                     | ASSERTED none           | 3/3          | -        | 3/3          | -        |
| Must reside in Calgary, Alberta                                                              | ASSERTED none           | 3/3          | -        | 3/3          | -        |
| Five years of production Rust development                                                    | GAP none                | 3/3          | -        | 3/3          | -        |
| Persuades executive stakeholders to reverse contested portfolio decisions                    | GAP none                | 3/3          | -        | 3/3          | -        |

Both strength misses answered `moderate` on all three trials, and both are the
cases whose two blind reviewers disagreed on strength before adjudication. At
prompt `93253db7d4ab`, with rule 6 grading an unmet stated qualifier `weak` and
the behavioral section naming a helping-others trait, both land on their labels,
and the production case holds DIRECT on all three trials.

Manifest `e920e553` takes all eighteen cases of a blind relabel under the
narrowed strength rubric, three of them held out from the prompt work that
narrowed it. Four labels moved from
`c1d3201a`: two TRANSFERABLE cases from `weak` to `moderate`, the
blocker-clearing case from TAILORING_ONLY to DIRECT, and the coaching case,
TRANSFERABLE in `d55cc1cd`, enters as GAP. Strength 29 of 36 at prompt
`40a92b47a711`. Per case:

| case                                                                                                                                        | label                   | 40a92b47a711 | strength |
| ------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------- | ------------ | -------- |
| Production Python service development at high event volume                                                                                  | DIRECT strong           | 3/3          | 3/3      |
| Collaborates directly with engineering and customer support to deliver releases                                                             | DIRECT strong           | 3/3          | 3/3      |
| Steps in to clear the blockers that stall shared delivery work                                                                              | DIRECT strong           | 0/3          | -        |
| Builds payment event ingestion at tens of millions of events per day for our Tallyforge platform, in a language such as Java, Go, or Python | DIRECT strong           | 3/3          | 3/3      |
| Improves production readiness and scalability of payment services                                                                           | TAILORING_ONLY moderate | 3/3          | 3/3      |
| Scales payment platform services to meet production load                                                                                    | TAILORING_ONLY moderate | 3/3          | 3/3      |
| Builds trusted cross-functional partnerships to deliver shared outcomes                                                                     | TAILORING_ONLY moderate | 3/3          | 2/3      |
| Production infrastructure delivery using Helm charts                                                                                        | TRANSFERABLE moderate   | 3/3          | 0/3      |
| Aligns a dozen independent product groups on owners and priorities for a company-wide launch                                                | TRANSFERABLE moderate   | 3/3          | 0/3      |
| Runs scheduled reconciliation batch jobs with Argo Workflows                                                                                | TRANSFERABLE moderate   | 3/3          | 3/3      |
| 3+ years improving production readiness of payment services                                                                                 | TRANSFERABLE weak       | 3/3          | 3/3      |
| Leads production readiness and scaling work for payment services                                                                            | TRANSFERABLE weak       | 3/3          | 3/3      |
| Eight or more years maintaining reconciliation pipelines, partnering with finance and compliance teams                                      | TRANSFERABLE weak       | 3/3          | 3/3      |
| Must be authorized to work in Canada without sponsorship                                                                                    | ASSERTED none           | 3/3          | -        |
| Must reside in Calgary, Alberta                                                                                                             | ASSERTED none           | 3/3          | -        |
| Five years of production Rust development                                                                                                   | GAP none                | 3/3          | -        |
| Persuades executive stakeholders to reverse contested portfolio decisions                                                                   | GAP none                | 3/3          | -        |
| Coaches a team through ambiguous strategic decisions                                                                                        | GAP none                | 0/3          | -        |

Every miss is unanimous across trials: the two `moderate` TRANSFERABLE cases
answer `weak`, the blocker-clearing case answers TAILORING_ONLY `strong`, and
the coaching case answers TRANSFERABLE `weak`. The held-out cases (Argo,
Tallyforge, reconciliation) land on their labels.

Manifest `687cf4a3` takes all nineteen cases of a blind relabel under the
rubric that lets a scale-only or adjacent-degree difference be `moderate`,
adding one held-out case whose only gap is scale. Two labels moved from
`e920e553`: the blocker-clearing case from DIRECT to
TAILORING_ONLY `strong`, and the partnerships case from `moderate` to `strong`.
Strength 32 of 40 at prompt `ea44cf46c98b`. Per case:

| case                                                                                                                                        | label                   | ea44cf46c98b | strength |
| ------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------- | ------------ | -------- |
| Production Python service development at high event volume                                                                                  | DIRECT strong           | 3/3          | 3/3      |
| Collaborates directly with engineering and customer support to deliver releases                                                             | DIRECT strong           | 3/3          | 3/3      |
| Builds payment event ingestion at tens of millions of events per day for our Tallyforge platform, in a language such as Java, Go, or Python | DIRECT strong           | 3/3          | 3/3      |
| Steps in to clear the blockers that stall shared delivery work                                                                              | TAILORING_ONLY strong   | 3/3          | 2/3      |
| Builds trusted cross-functional partnerships to deliver shared outcomes                                                                     | TAILORING_ONLY strong   | 1/3          | 0/1      |
| Improves production readiness and scalability of payment services                                                                           | TAILORING_ONLY moderate | 3/3          | 3/3      |
| Scales payment platform services to meet production load                                                                                    | TAILORING_ONLY moderate | 3/3          | 3/3      |
| Production infrastructure delivery using Helm charts                                                                                        | TRANSFERABLE moderate   | 3/3          | 0/3      |
| Aligns a dozen independent product groups on owners and priorities for a company-wide launch                                                | TRANSFERABLE moderate   | 3/3          | 1/3      |
| Runs scheduled reconciliation batch jobs with Argo Workflows                                                                                | TRANSFERABLE moderate   | 3/3          | 3/3      |
| Operates payment event ingestion at a billion events per day                                                                                | TRANSFERABLE moderate   | 3/3          | 2/3      |
| 3+ years improving production readiness of payment services                                                                                 | TRANSFERABLE weak       | 3/3          | 3/3      |
| Leads production readiness and scaling work for payment services                                                                            | TRANSFERABLE weak       | 3/3          | 3/3      |
| Eight or more years maintaining reconciliation pipelines, partnering with finance and compliance teams                                      | TRANSFERABLE weak       | 3/3          | 3/3      |
| Must be authorized to work in Canada without sponsorship                                                                                    | ASSERTED none           | 3/3          | -        |
| Must reside in Calgary, Alberta                                                                                                             | ASSERTED none           | 3/3          | -        |
| Five years of production Rust development                                                                                                   | GAP none                | 3/3          | -        |
| Persuades executive stakeholders to reverse contested portfolio decisions                                                                   | GAP none                | 3/3          | -        |
| Coaches a team through ambiguous strategic decisions                                                                                        | GAP none                | 0/3          | -        |

The coaching case still answers TRANSFERABLE `weak` on all three trials, and
the partnerships case answers DIRECT on two. The held-out scale case lands on
its class on all three trials.

Manifest `35d43845` relabels the pool under a reviewer rubric that carries the
product prompt's rules 2, 3 and 4 word for word. Against
`687cf4a3`, the Helm case moved from `moderate` to `weak`, one case entered
(production readiness work on payment platform services), and one left (leads
production readiness and scaling work). Prompt `4e746f175e60` answers a
behavioral requirement met only by a different action toward the same people
as GAP. Strength 34 of 42 at that prompt. Per case:

| case                                                                                                                                        | label                   | 4e746f175e60 | strength |
| ------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------- | ------------ | -------- |
| Production Python service development at high event volume                                                                                  | DIRECT strong           | 3/3          | 3/3      |
| Collaborates directly with engineering and customer support to deliver releases                                                             | DIRECT strong           | 3/3          | 3/3      |
| Builds payment event ingestion at tens of millions of events per day for our Tallyforge platform, in a language such as Java, Go, or Python | DIRECT strong           | 3/3          | 3/3      |
| Steps in to clear the blockers that stall shared delivery work                                                                              | TAILORING_ONLY strong   | 3/3          | 3/3      |
| Builds trusted cross-functional partnerships to deliver shared outcomes                                                                     | TAILORING_ONLY strong   | 3/3          | 0/3      |
| Improves production readiness and scalability of payment services                                                                           | TAILORING_ONLY moderate | 3/3          | 3/3      |
| Scales payment platform services to meet production load                                                                                    | TAILORING_ONLY moderate | 3/3          | 3/3      |
| Production readiness work on payment platform services                                                                                      | TAILORING_ONLY moderate | 3/3          | 3/3      |
| Aligns a dozen independent product groups on owners and priorities for a company-wide launch                                                | TRANSFERABLE moderate   | 3/3          | 0/3      |
| Runs scheduled reconciliation batch jobs with Argo Workflows                                                                                | TRANSFERABLE moderate   | 3/3          | 3/3      |
| Operates payment event ingestion at a billion events per day                                                                                | TRANSFERABLE moderate   | 3/3          | 1/3      |
| Production infrastructure delivery using Helm charts                                                                                        | TRANSFERABLE weak       | 3/3          | 3/3      |
| 3+ years improving production readiness of payment services                                                                                 | TRANSFERABLE weak       | 3/3          | 3/3      |
| Eight or more years maintaining reconciliation pipelines, partnering with finance and compliance teams                                      | TRANSFERABLE weak       | 3/3          | 3/3      |
| Must be authorized to work in Canada without sponsorship                                                                                    | ASSERTED none           | 3/3          | -        |
| Must reside in Calgary, Alberta                                                                                                             | ASSERTED none           | 3/3          | -        |
| Five years of production Rust development                                                                                                   | GAP none                | 3/3          | -        |
| Persuades executive stakeholders to reverse contested portfolio decisions                                                                   | GAP none                | 3/3          | -        |
| Coaches a team through ambiguous strategic decisions                                                                                        | GAP none                | 3/3          | -        |

The coaching case answers GAP on all three trials. Every strength miss answers
below its label: the partnerships case `moderate` on all three trials, the
launch-alignment case `weak` on all three, and the held-out scale case `weak`
on two.

## Relevance review

Scored against `golden/relevance_review.jsonl`; `correct` counts trials whose
recommendation equals the frozen one.

| date       | model         | effort / tier  | cases x trials | correct | cost     | prompt       | dataset  |
| ---------- | ------------- | -------------- | -------------- | ------- | -------- | ------------ | -------- |
| 2026-09-20 | gpt-5.6-terra | low / standard | 16 x 1         | 14/16   | 0.193407 | 0f0fdb2bb6d8 | 9449cd0f |
| 2026-09-21 | gpt-5.6-terra | low / standard | 16 x 1         | 15/16   | 0.198831 | 957f913f96bd | 9449cd0f |
| 2026-09-23 | gpt-6-sol     | low / flex     | 16 x 3         | 38/48   | 0.097379 | dca9a8a0c134 | 9449cd0f |
| 2026-09-23 | gpt-6-luna    | low / flex     | 16 x 3         | 34/48   | 0.006198 | dca9a8a0c134 | 9449cd0f |
| 2026-09-23 | gpt-6-sol     | low / flex     | 16 x 3         | 46/48   | 0.097900 | 761d01c2712a | 36018970 |

The two misses on 2026-09-20 are both `insufficient_evidence` rows the scorer used
for that run graded wrongly; the answers matched the labels. Dataset `36018970`
relabels two cases to `review`.

### A prompt A/B on a real workspace

In September 2026 the maintainer tested a relevance prompt round against 19
real pending postings and their own CV, with labels written before the run.
The round taught the reviewer to read work the person said they do not want.
The labels are low-confidence calibration, not gold.

Round 1 ran the new prompt through the product's own task, as one batched
review of 18 postings plus one posting alone. Two calls cost USD 0.12. Seven of
the 19 answers were refused by the product's validation as uncited: the prompt
told the model to cite CV evidence only when the work matched directly or
transferably, while validation required a citation for any established
qualification. Because the
CV, parameters, batch composition, and notes had also changed since the
baseline, the round could credit nothing to the prompt.

Round 2 ran two prompt arms through `scripts/eval.sh --live`: the prompt from
before the round, and the new prompt with its citation sentence fixed. Both
arms used the same CV, parameters, and schema. It cost USD 0.92 over 31
calls. The first 17 were a three-trial launch over all 19 postings, stopped
when its per-call cost showed it would run several times over budget; the
other 14 ran each arm once on the seven refused postings. The harness sends
one posting per call, so the earlier arm never
reproduced the batch-mode refusals, and the comparison could not test the fix.
On the seven refused postings the earlier arm matched 5 labels and the fixed
arm 3.
The citation fix landed because it removes a contradiction between the prompt
and validation. No arm showed a net gain, so the unwanted-work reading stays a
recorded limit: it can call a posting
unwanted work when the posting never says so.

Two lessons carry to any live eval here. First, a harness that changes the
request shape changes what it measures. A render check must compare the whole
request with production's, not look for each posting inside it. Second, that
shape change cost about four times as much per posting in these runs. A batched
request includes the CV evidence once for all its postings, while a
single-posting request includes its own copy. Provider caching is separate
and is not guaranteed.

## Critic

Each case plants one defect in a document assembled offline, or plants none;
the outcome is what the shipped floors assign the critic's merged verdict.

| date       | note               | model         | effort / tier     | cases  | outcomes                 | cost     | prompt       | dataset  |
| ---------- | ------------------ | ------------- | ----------------- | ------ | ------------------------ | -------- | ------------ | -------- |
| 2026-09-20 | -                  | gpt-5.6-terra | medium / standard | 6      | 4 pass, 2 fail           | 0.080439 | faa0f4830e4d | 575e3c07 |
| 2026-09-21 | -                  | gpt-5.6-terra | medium / standard | 6      | 4 pass, 2 fail           | 0.064252 | faa0f4830e4d | 575e3c07 |
| 2026-09-21 | -                  | gpt-5.6-terra | medium / standard | 6      | 3 pass, 3 fail           | 0.073852 | faa0f4830e4d | 575e3c07 |
| 2026-09-23 | -                  | gpt-6-sol     | medium / flex     | 10     | 7 fail, 3 warn           | 0.158845 | edf87418d908 | 2bec0bf3 |
| 2026-09-23 | fixtures corrected | gpt-6-sol     | medium / flex     | 4      | 3 warn, 1 fail           | 0.061194 | edf87418d908 | 7ab7ff75 |
| 2026-09-23 | fixtures corrected | gpt-6-sol     | low / flex        | 10     | 5 fail, 5 warn           | 0.092364 | edf87418d908 | 7ab7ff75 |
| 2026-09-23 | -                  | gpt-6-sol     | medium / flex     | 11     | 6 fail, 5 warn           | 0.194565 | c1cc6951842f | 7b50dc9b |
| 2026-09-23 | -                  | gpt-6-sol     | medium / flex     | 13 x 3 | 24 fail, 10 warn, 5 pass | 0.288156 | 38e1aa7c0f77 | 37818e8f |
| 2026-09-23 | letters corrected  | gpt-6-sol     | medium / flex     | 2 x 3  | 6 warn                   | 0.040501 | 38e1aa7c0f77 | fba1467c |
| 2026-09-23 | letter reworded    | gpt-6-sol     | medium / flex     | 1 x 3  | 2 pass, 1 warn           | 0.019305 | 38e1aa7c0f77 | 1793c30b |

The two 2026-09-21 rows are the same six cases run twice to measure spread.

From prompt `c1cc6951842f` the critic's citation index carries the text of each
cited entry beneath the line, and `planted_scope_widening` widens a team's work into
the candidate's; every planted case was caught on its line and no clean case
drew a doubt.

From prompt `38e1aa7c0f77` the critic asks once per draft instead of three
times, and the set adds two cover letters. The doubt score was 36 of 39: every resume case
held on all three trials, and the three misses are one letter case whose
"To <duty>, I ..." sentences claimed more than their cited entries, which the
critic was right to doubt. Outcome agreement, reported beside the score, was
32 of 39. With both letters corrected so every statement is what its cited
entry says, neither drew a doubt on any trial; the duties letter warns on
human voice as built, and the company-paragraph letter passed 2 of 3 once its
company paragraph stopped borrowing the careers page's phrasing.

## Draft and cover letter

| date       | family | model         | effort / tier     | cases | result                                                   | cost     |
| ---------- | ------ | ------------- | ----------------- | ----- | -------------------------------------------------------- | -------- |
| 2026-09-20 | draft  | gpt-5.6-terra | medium / standard | 6     | 6 cleared `verify.check`; factual review warned on 6     | 0.077315 |
| 2026-09-20 | cover  | gpt-5.6-terra | medium / standard | 5     | 5 cleared `verify.check_letter`; letter echo warned on 5 | 0.039728 |
