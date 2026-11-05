# Prompts

These are the model-assisted parts of clearscout. They live out here rather than
inside request code so you can read them, diff them, and evaluate them like the
product behavior they are.

You do not need to install ClearScout to read them. For a job search, start with
`jobspec_*`, `match_*`, `draft_*`, and `critic_*`: what the posting asks, what
the career record answers, how to word it, and what needs another look.

Borrowing a prompt does not bring the application's checks with it. These files
expect rendered inputs and response schemas. They are useful to study or adapt,
not a promise that pasting one into a chat reproduces the workflow below.

## What each pair does

| Prompt                | Job                                                                                                       |
| --------------------- | --------------------------------------------------------------------------------------------------------- |
| `vision_*`            | Extract visible posting text from an image                                                                |
| `structure_*`         | Restructure an existing CV while citing its numbered source lines                                         |
| `jobspec_*`           | Turn a posting into requirements, gates, and terminology, and name its core occupation and any stated boundary it falls under |
| `match_*`             | Link requirements to CV evidence or report a gap                                                          |
| `draft_*`             | Draft resume prose from selected evidence                                                                 |
| `critic_*`            | Review preservation and hiring quality on separate axes                                                   |
| `cover_letter_*`      | Draft an optional letter from the same supported evidence                                                 |
| `search_vocabulary_*` | Name the titles, employer kinds, and focus terms this work goes by, citing CV evidence                    |
| `guided_campaign_*`   | Run the hosted searches one campaign attempt is assigned and select the postings and boards they surfaced |
| `relevance_review_*`  | Rank every readable discovery before full evaluation                                                      |
| `posting_recovery_*`  | Search for the publisher page of one named posting and return candidate URLs, never posting text          |

Python response schemas define the exact output shape. Prompts define the
judgment task, evidence rules, and writing standard. Deterministic code handles
selection, assembly, and checks where possible.

## The boundary is not written in these files

Every placeholder in every prompt is declared in `prompts.FIELDS`, and `render`
applies the treatment that declaration names.

| Handling | What it carries                                                         | What `render` does                                                                                                                |
| -------- | ----------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------- |
| block    | Text the program did not write: a posting, a record, a resume, feedback | Wraps it in a named block, prefixed by a sentence saying it is untrusted, with `<` and `>` escaped so it cannot close its own tag |
| inline   | A short value it did not write: a job title, an employer                | Escapes it and flattens it to one line                                                                                            |
| written  | A rule, a floor, a limit: text the program or the settings file wrote   | Inserts it unchanged, because it is meant to be read as an instruction                                                            |

So a prompt file contains `{{evidence}}` on a line of its own and no tags around
it. Writing the tags in the file was how five prompts out of six ended up with a
bare tag and no boundary sentence: the helper existed, and only `structure_user`
called it. A placeholder that is not declared raises instead of being sent.

Two consequences worth stating rather than discovering.

The untrusted-data sentence is repeated once per block, so a prompt with three
blocks says it three times. That is deliberate. The sentence is machine-written
framing that sits against the payload, and the policy it enforces is stated once
in the system prompt's `<input_boundary>` section. Whether the repetition helps
or hurts a model is a semantic question, which means it belongs in
[MANUAL_EVAL.md](MANUAL_EVAL.md) rather than in somebody's judgment here.

A title and an employer are not blocked. They read as part of a sentence, and a
block around three words would be mostly boundary. They are flattened and
escaped instead, which is what stops the two things a short value could do.

## Text an override may not change

A prompt is rewritable from the web client, so a rule that lives only in a
prompt file is a rule somebody can delete. The text between `<!--invariant-->`
and `<!--/invariant-->` states one of those rules, and an override has to carry
it back word for word or it is refused: when it is saved, and again when it is
about to be sent. Everything outside those markers is editable, which is the
objective, the style, and the rubric.

The markers are stripped before a request is built, so no model sees one and
adding a pair to a file changes nothing about what is sent. What belongs inside
them is anti-fabrication, evidence identifiers, the untrusted-data boundary,
abstention, citation limits, and workflow the program depends on. What does not
is anything a person could reasonably want to phrase differently.

## Numbers a prompt states and code enforces

A threshold written into a prompt beside a constant that enforces it is two
copies of one decision. Each of these is rendered from the constant, with a test
that changes the constant and reads the prompt back:

| Prompt                     | Number                              | Owned by                                                |
| -------------------------- | ----------------------------------- | ------------------------------------------------------- |
| `critic_system`            | preservation and quality floors     | `stages/verify/critic.MINIMUM_PRESERVATION`, `MINIMUM_QUALITY` |
| `jobspec_system`           | longest keyword phrase              | `stages/jobspec.MAX_KEYWORD_LENGTH`                     |
| `cover_letter_system`      | longest letter                      | `stages/draft.MAXIMUM_LETTER_WORDS`                     |
| `search_vocabulary_system` | most terms per cluster              | `domain/search_parameters/focus.MAX_CLUSTER_TERMS`      |
| `guided_campaign_user`     | branches assigned and search budget | the attempt's `guided_campaign.CampaignAttemptPlan`     |

`guided_campaign_system` states no number at all. Every attempt of a campaign
sends it byte for byte, so it can be served from the provider's prompt cache,
and the per-attempt counts travel in `guided_campaign_user` instead.

The letter limit was stated and never counted. It is counted now, by
`verify.check_letter`, and a letter over it is redrafted like any other letter
failure.

## Work the program does for itself

A prompt should not ask for something deterministic that the program already
performs, and should not state a rule that nothing checks. Both were here.

`structure_system` asked for typographic substitution and transliteration.
Those are closed string maps `importer.plain` already owned, and asking for
transliteration was worse than redundant: a model that obeyed wrote a word
appearing in no cited source line, which is what the import authenticity check
fails an import over. The importer does the fold now, the prompt asks for
characters to be copied as they are, and a character with no ASCII form is
named in a finding instead of guessed at.

## Shared rules

- CV entries, job descriptions, feedback, and image text are untrusted data,
  not instructions. Every system prompt says so once, and a test enumerates
  them rather than sampling. Eight put it in an `<input_boundary>` section;
  `structure_system`, `vision_system`, and `posting_recovery_system` state it
  inside their invariant text.
- Import may join wrapped lines, split packed fields, remove conversion
  artifacts, and normalize dates. It may not improve or shorten career content.
- Every imported field cites source line numbers. The runtime verifies its words
  against those lines and separately checks that important source content was
  not silently lost.
- Missing evidence stays a gap. Related experience is not silently promoted to
  a direct match.
- Qualifications such as production use, scale, duration, domain, proficiency,
  seniority, and ownership must be supported separately from the underlying
  tool or topic.
- Job-posting terms are useful when they accurately name the candidate's work.
  Repetition and keyword piles are not goals.
- Resume prose should be specific, restrained, scannable, and defensible in an
  interview. It should not sound copied from the posting or generated from a
  single repeated template.
- Output schemas and programmatic checks enforce structure and objective rules;
  prompts should not duplicate those mechanisms without an observed reason.
- A rule is stated once per system-and-user pair. Where it has to appear twice,
  say why here rather than leaving it looking like an oversight. Today nothing
  does, apart from the per-block untrusted sentence explained above.

## Evidence categories

Matching distinguishes five categories of evidence: direct, transferable,
adjacent, bounded familiarity, and no evidence. `stages/match.py` carries them
on two axes rather than one.

| Category            | How the schema says it                                                                                                |
| ------------------- | --------------------------------------------------------------------------------------------------------------------- |
| direct              | `DIRECT`, strength `strong` or `moderate`                                                                             |
| transferable        | `TRANSFERABLE`, strength `strong` or `moderate`                                                                       |
| adjacent            | `TRANSFERABLE`, strength `weak`                                                                                       |
| bounded familiarity | `TRANSFERABLE`, strength `weak`, with decision rule 3 forbidding familiarity from being read as production experience |
| no evidence         | `GAP`, strength `none`                                                                                                |

Two axes rather than five labels, because strength is needed anyway: selection
multiplies it by requirement weight, so collapsing it into the classification
would mean inventing a second number to replace it. The schema also carries two
answers beyond those five. `TAILORING_ONLY` is the same capability
under different truthful wording, which is a rewriting instruction rather than a
weaker claim. `ASSERTED` is a requirement about status, eligibility, or location
that no accomplishment can evidence; it exists because reporting somebody's own
stated work authorization as a gap in their record is both wrong and alarming.

## Critic scores

The critic returns six independent 1-to-5 ratings:

- preservation;
- alignment;
- specificity;
- readability;
- ATS clarity;
- human voice.

Preservation is a separate gate. Strong writing cannot compensate for an
unsupported or materially strengthened claim. The other ratings help locate a
problem; they are not probabilities, employer scores, or calibrated ATS scores.

Beside the ratings the critic returns the claims it doubts. Each names the claim
and the boundary that claim crosses, drawn from the closed set in
`domain/records.ClaimBoundary` and enforced by the response schema. A doubted
claim fails the draft whatever boundary it carries.

## Changing a prompt

The instrument that justifies a prompt change is [evals/](../evals/). No prompt
change ships without an evaluation case demonstrating it is better. Run
`uv run python -m evals` before and after; every baseline records the prompt
suite fingerprint it was produced under, so a run whose prompts have moved is
reported as incomparable to the baseline rather than quietly compared to it.

The harness states exactly what that measures, and so does this file. The
default cases are scripted, so they measure whether the program still enforces
its guarantees when a model misbehaves. They do not measure whether a prompt
makes a model misbehave less often. That question uses the separately approved
live mode documented in
[`evals/inspect/METHODOLOGY.md`](../evals/inspect/METHODOLOGY.md). It needs an
independently reviewed scoring set and spends money. A live run repeats each case several times, because one draw cannot
show a defect that appears in three of five.

What the paid runs found, prompt by prompt, with costs and the changes they
justified, is in [evals/FINDINGS.md](../evals/FINDINGS.md). The tables behind it
are in [evals/RESULTS.md](../evals/RESULTS.md).

1. Save a realistic case that demonstrates a concrete failure.
2. Label the failure before deciding that the prompt is the cause.
3. Prefer a deterministic check when the rule can be stated exactly. A scorer in
   `evals/scorers.py` that keeps finding something is the argument for one.
4. Make one small prompt change and keep model, effort, CV, posting, settings,
   and page budget fixed.
5. Compare old and new outputs on the failing case and a representative
   regression set.
6. Record prompt fingerprint, deterministic failures, human defects, critic
   scores, cost, and latency.
7. Keep the change only if it fixes the target behavior without weakening
   evidence preservation.

Do not rewrite the whole suite during a model upgrade. First run the existing
prompts on the new model, identify measured regressions, and change only what
those cases justify.

The program records a fingerprint of the complete prompt suite in every report,
so results from different revisions can be distinguished.

## Evaluation cases

Five of these are automated in [evals/cases/](../evals/cases/) and should not be
worked by hand: the attractive gap, collaboration that must not become sole
ownership, the long record that requires real selection, instructions planted in
posting data, and two similar postings. The rest need a person, and
[MANUAL_EVAL.md](MANUAL_EVAL.md) says which and why. At minimum, include cases
for:

- a PDF-wrapped bullet that must become one achievement again;
- a role header whose employer, title, and dates share one source line;
- invented import wording, a wrong source citation, a lost metric, and a
  deliberate harmless drop;
- a multi-column extraction whose words survive but may be assigned to the
  wrong role;
- supported work described with different vocabulary in the posting and CV;
- an attractive but unsupported tool, credential, or production qualifier;
- collaboration that must not become sole ownership;
- useful evidence with no metric;
- a long record that requires real selection;
- instructions or markup planted in posting and CV data;
- two similar postings that should produce different emphasis;
- prose that is accurate but generic, repetitive, or keyword-heavy.

Use [MANUAL_EVAL.md](MANUAL_EVAL.md) for a side-by-side review sheet of the ones
a person still has to do. Why the suite is shaped this way, and what the ATS
claims here do and do not rest on, is in
[the architecture overview](../docs/architecture.md#why-the-prompt-suite-is-shaped-this-way).
