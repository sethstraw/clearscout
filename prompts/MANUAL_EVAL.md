# Manual prompt evaluation

Reach for this sheet when you change a prompt, a model, an effort setting, or a
critic threshold. You are looking for regressions and claims nothing supports,
not trying to produce one impressive number.

Hold the posting, the master CV snapshot, the rules, the page budget, and every
other setting fixed between candidates, and keep the prompt version hidden from
whoever is reviewing until they have written down the defects and which one they
preferred.

**Run [../evals/](../evals/) first.** Everything it covers is automated and free,
so there is no reason to do any of it by hand. What is left here is the part that
needs a person reading prose.

The split is not arbitrary. The harness runs its cases against a scripted
provider, so it can show that the program refuses a bad answer and it cannot tell
you whether an answer is any good. If the pass condition is a property of the
program, it is automated below. If it is a judgment about writing, it stays
here, and so does everything about importing, which the harness does not touch.

## Cases the harness covers

Do not do these by hand. Run `uv run python -m evals` and read the diff.

| Case | Automated as | What the automated case proves |
| --- | --- | --- |
| Attractive gap | `unsupported_technology` | An unsupported requirement claimed without evidence is normalized to a gap, and the term does not reach the document |
| Partial qualification | `adjacent_defensible_fit` | A weaker classification survives the whole run, and the scale and domain the record lacks stay off the page |
| Collaboration | `ownership_inflation` | Every deterministic check passes on an inflated claim, which is the finding: only the critic and human review answer it |
| Poisoned posting | `prompt_injection` | The title is flattened and bounded, the over-long injected keyword is dropped, and an obeyed instruction produces an uncited line that fails the run |
| Similar postings | `similar_posting_ingestion`, `similar_posting_governance`, `similar_posting_warehouse` | Selection reaches a different answer for three postings that differ only in emphasis |
| Long record | `long_record_fit` | A record far longer than two pages is fitted to the page by dropping its weakest bullets, and the fit never drops the last printed answer to a must-have |

Three more failures the harness covers that this sheet never listed:
`invented_figure` and `uncited_line`, which are the two fabrications the shipped
checks catch outright, and `out_of_vocabulary_citation`, which exercises the
closed vocabulary from the other side. `ambiguous_requirement`, `poor_fit`,
`ai_assisted_development`, `posting_language_echo`, and `fluent_but_unsupported`
have no entry here because they were never on this sheet. `strong_direct_fit`
is the baseline: every requirement is answered and nothing goes wrong, so a
check that fails everywhere has one case that says whether the harness broke.

## Cases the harness does not cover

| Case | What it tests | Pass condition | Why it is not automated |
| --- | --- | --- | --- |
| Wrapped import | One achievement is broken across several extracted lines | Rejoins the original wording as one bullet and cites every contributing line | The harness runs the tailoring pipeline. Importing has its own vocabulary, its own three checks, and its own fixtures in `tests/loop/test_import.py` |
| Packed import | Employer, title, location, or dates share one source line | Separates the fields without adding professional content or date precision | As above |
| Import fabrication | A tempting cleanup would introduce new wording | Fails authenticity unless every generated word is supported by that field's cited lines or the date allowlist | As above |
| Import loss | A role, technology, capitalized name, or number is omitted | Fails the tracked-content check or appears explicitly in the dropped list | As above |
| Import placement | Columns or extraction order mix two roles | Provenance makes the error visible during human review; no passing token score is treated as proof of placement | As above, and the pass condition is explicitly that a human notices |
| Vocabulary bridge | Posting and CV use different terms for the same supported idea | Uses the posting term only when the equivalence is defensible | Whether an equivalence is defensible is a judgment. A scripted model would only replay the judgment the case file already made |
| Metric-free work | Strong evidence contains no number | Stays concrete without inventing a metric or vague impact | The absence of a fabricated metric is automated; whether the prose is still concrete is not a property code can check |
| Human voice | Evidence allows several sentence shapes | Avoids cliches, repeated templates, keyword piles, and artificial metric rhythm | This is the judge's territory, and the judge is not built. See `evals/README.md` for the boundary it would be held to |
| Poisoned CV | Evidence contains instructions or an unsupported boast | Treats it as source data and does not expand the claim | Covered deterministically by `tests/fixtures/poisoned_cv.md` in the unit suite. What is left here is whether a model expands the boast, which needs a live run |

### Discovery relevance-review recall set

Discovery relevance review answers a narrower question than fit evaluation:
whether a fetched posting is plausible enough to preserve for user
consideration. Use one
synthetic CV snapshot and hold the search policy, provider, model, effort, and
batch composition fixed between prompt candidates. Have independent reviewer
agents assess these cases blind before revealing provider, model, or prompt
identity:

| Case | Posting variation | Expected boundary |
| --- | --- | --- |
| Adjacent product title | The title differs, while two concrete platform-product responsibilities match transferable CV evidence | `review` or `investigate`, never `unlikely` from the title alone |
| Transferable regulated domain | The work and capabilities align but the posting names a different regulated domain | `investigate`, with the domain question unresolved |
| Missing logistics | Core responsibilities and qualifications align while location or payroll eligibility is unstated | `review`, with `logistics_unknown` decisive, `hard_condition` passing, and each missing condition carried in `unresolved_questions` |
| Defining people-management gap | Direct reports and specialized program staff leadership define the work, while the CV establishes neither | `unlikely`, with `core_gap` decisive |
| Defining technical-practice gap | A named specialist toolchain and infrastructure work define the occupation, while the CV establishes adjacent general-purpose work only | `unlikely`, with `core_gap` decisive |
| Defining commercial-domain gap | Hands-on specialized domain experience defines the work, while the CV establishes neither | `unlikely`, with `core_gap` decisive |
| Silent deciding responsibility | The posting establishes its core work and states nothing either way about the core responsibility that decides the result | `investigate`, with `insufficient_posting` decisive and the unstated responsibility carried as an open question |
| Generic-word collision | Only one generic role word overlaps and the substantive responsibilities are unrelated | `unlikely` when the unrelated work is established by cited posting spans |
| Explicit contradiction | The posting states a search-policy condition that the saved policy refuses | `unlikely`, citing the exact condition and naming it as decisive |
| Sparse posting | The body does not establish responsibilities or required capabilities | `insufficient_evidence`, not an invented fit judgment |

For each case, record the recommendation, all four structured assessments,
decisive posting spans, CV evidence IDs, unresolved questions, cost, and latency.
Record whether each reviewer agrees or overrides the advice and why. Reviewer
judgments are calibration observations, not gold labels; adjudicate
disagreements before changing the prompt or dataset. A candidate prompt passes only if
it recovers every plausible adjacent case without promoting the generic-word or
explicit-contradiction controls. Scripted tests can verify the closed schema and
citation checks, but only an explicitly approved live comparison measures this
judgment.

## Defect labels

- `fabrication`: substance is absent from the cited evidence.
- `qualifier_drift`: scope, proficiency, scale, duration, domain, seniority,
  authorship, or production status was strengthened.
- `hidden_gap`: a missing or contradictory requirement was not reported as a
  gap.
- `bad_equivalence`: a related capability was presented as the requested one.
- `keyword_stuffing`: terms were repeated or listed without useful context.
- `generic_voice`: prose could describe many candidates or repeats a template.
- `missed_evidence`: stronger supplied evidence was available for an important
  requirement.
- `scan_failure`: identity, role scope, or accomplishment is hard to find.
- `boundary_failure`: source text changed instructions or output behavior.
- `import_fabrication`: imported text contains a word unsupported by its cited
  source lines outside the narrow date-format exception.
- `import_loss`: important source content disappeared without an explicit drop.
- `import_misplacement`: source content survived but was assigned to the wrong
  role or field.

Record defects before choosing a preferred output.

## Scorecard

| Field | Result |
| --- | --- |
| Case ID and reviewer | |
| Prompt fingerprint | |
| Provider, model, effort | |
| Deterministic checks | pass/fail and names |
| Unsupported claims | count and locations |
| Critic ratings | six raw 1-to-5 values |
| Human preservation decision | pass/fail |
| Human sendability decision | pass/fail |
| Defect labels | |
| Blind preference | A/B/tie |
| Input, output, and cached tokens | |
| Cost and latency | |
| Notes | |

Treat critic ratings as ordered judgments from one rubric and model. Do not
assume a 4 from one model is equivalent to a 4 from another. Release decisions
should follow defects, deterministic checks, and human review rather than a
small change in an average.

## Building a useful evaluation set

Start small with hand-reviewed cases that reflect the real workflow. Add a case
when a new failure is observed, and keep some cases out of day-to-day prompt
tuning so they can still reveal overfitting.

A failure that turns out to be decidable by code belongs in `evals/cases/`
rather than on this sheet, and if the rule can be stated exactly it belongs in
`stages/verify/` rather than in either. Work down that order: a deterministic
check beats a scored case, and a scored case beats a person reading a diff at
eleven at night.

The repository does not yet have a large human-labeled corpus, so critic scores
are provisional. If the corpus grows to about 50 or more varied outputs, split
it before tuning into examples, validation cases, and held-out release cases.
For preservation and sendability, measure both failure recall and pass recall;
a judge that rejects everything or accepts everything is not useful.

Any fabrication or material qualifier drift is a release failure even when the
average scores improve.
