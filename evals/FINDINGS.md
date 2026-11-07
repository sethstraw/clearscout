# What live validation found

ClearScout calls paid models at eleven stages, from CV import to posting recovery. The runs here concentrate on five of them: requirement extraction (jobspec), evidence matching (match), relevance review, drafting of resumes and cover letters, and critique. Between August and November 2026 the maintainer ran about 190 dated paid runs against those stages and kept a private ledger of each. This file publishes what those runs taught, because a claim like "the model never cites evidence it was not given" is only worth what the evidence behind it is worth.

Most of those runs were evals: an eval is a repeatable test of how well one model step does its job.

Read these results as bugs found on small samples, not as accuracy claims. Most runs cover 4 to 19 synthetic cases or a handful of real postings, with 1 to 5 draws each. A number here says a behavior happened at least that often under one prompt and one model. It does not carry to real postings in general. [inspect/METHODOLOGY.md](inspect/METHODOLOGY.md) explains what each eval family measures and what it cannot. [RESULTS.md](RESULTS.md) holds the tables; this file is the narrative.

Runs on the maintainer's real job search appear here only as counts, costs, and neutral descriptions. Synthetic cases are named. Costs are in USD as the ledger priced them.

## The pre-release round in one table

On 2026-11-06, about 27 paid runs (USD 6.37 by the ledger) and six free rescores measured every model stage. A rescore runs the scorer again over model outputs already saved, which costs nothing. The round came just before the replay cassettes were re-recorded for release. A cassette is a saved copy of a real request and the provider's reply, which the tests replay instead of paying for a new call. The sections below hold the detail; this table is what the round taught.

A few terms in the table need a word first. A gold label is the answer a person decided was correct, and a gold set (or gold list) is the set of cases scored against those answers. A "boundary" is a category the critic uses to say what kind of unsupported claim it found; a "planted" case has a deliberate defect inserted, and a "clean" case has none. A "draw" is one sample of the model's answer to the same input. The "probe" is a small synthetic test written for one defect, and a "containment clause" is prompt wording saying a place inside the one asked for counts. Effort is how much the model may reason before it answers, and p95 is the response time that 95 of 100 calls beat. Verbosity is an OpenAI setting for how long the answer is; "search depth" is how much a web search call looks through. A smoke call is one cheap call made before a long paid run, to check the run works at all. "Warm" means the provider already holds the opening of the request from an earlier call, so it bills it at a discount.

| Lesson | What showed it | What changed |
|---|---|---|
| Our answer key was wrong more often than the model. | Three critic cases failed 3 of 3 under a different boundary than their label. One match row answered the way a newer prompt rule says. All five vocabulary misses were plausible job titles missing from the gold lists. For the critic and match cases, the prompt's own definitions sided with the model. | Labels and one ruling were corrected, each confirmed by a free rescore. No prompt changed for them. (Wrong test labels are common; [Northcutt et al.](https://arxiv.org/abs/2103.14749) show fixing them can reverse rankings.) |
| A realistic input finds bugs that single small cases miss. | A whole posting with 33 requirements treated a Canadian work-authorization line as answering a United States work-authorization requirement, 3 of 3 draws. | One prompt sentence plus a containment clause fixed it: probe 42 of 42, match set 57 of 57, the whole posting 31 of 33 per draw. |
| Compare settings only on the prompt you actually ship. | The critic's low-effort win was first measured on an older prompt, and never on clean documents. Rerun on the shipped prompt, both effort levels scored 36 of 36 clean and 59 of 63 planted. | The critic ships at low effort: about 18 per cent cheaper, p95 near 10 s against 24 s. |
| Cut the cost that makes up most of the bill. | Low verbosity cut relevance cost 32 per cent and jobspec 22 per cent with no loss. Lower search depth saved nothing, because search fees and search results are most of that bill. | Low verbosity ships on jobspec and relevance; search depth is unchanged. |
| A test that never runs the code path proves nothing about it. | The draft eval scored only the first attempt. The cover-letter redraft ran 22 draws with no second attempt. A case meant to see a job-board pick saw no board in 104 picks. | Two-attempt tasks were built: draft redraft 42 of 42. The cover-letter redraft path is recorded as unmeasured. |
| Have someone read every paid result before you act on it. | A reviewer blocked or reshaped three decisions, and found a third-party registry page among a recording's real search results. | Its notes were filed and closed. The page was removed, and the audit exemption was limited to web-search recordings. |
| The cost estimate runs high when the cache is warm. | Smoke projections of USD 0.34 and 0.90 came in at 0.055 and 0.398. | Estimates stay advisory and never stop a run. |

## Model choice per stage

**What was tried.** Every stage started on gpt-5.6-terra: extraction at low effort, drafting at high. On 2026-08-15 gpt-5.6-luna drafted one posting. On 2026-09-23 gpt-6-sol and gpt-6-luna were screened on match and relevance. On 2026-10-04 gpt-6.1-sol was screened against gpt-6-sol on match. The product has an Anthropic adapter, but the ledger records no paid Anthropic run, so nothing here compares vendors.

**What the numbers showed.**

- Drafting, 2026-08-15, one posting: luna's drafting tier cost USD 0.0182 against terra's USD 0.1036, 82 per cent less. The critic scored luna a full point lower on specificity, readability, and ATS clarity (3 against 4). Both verified, which only means nothing was invented.
- Match, 2026-09-23, 10 cases x 3: sol 22 of 30 for USD 0.031106, luna 19 of 30 for USD 0.002155. Luna dropped one case to 1 of 3 where sol held 3 of 3.
- Relevance, same day, 16 cases x 3: sol 38 of 48 for USD 0.097379, luna 34 of 48 for USD 0.006198. Sol gave one answer across all three trials on 15 of 16 cases. See [RESULTS.md, Match](RESULTS.md#match) and [Relevance review](RESULTS.md#relevance-review).
- Match, 2026-10-04, 19 cases x 3: gpt-6-sol 57 of 57 for USD 0.101493, gpt-6.1-sol 54 of 57 for USD 0.075995. All three misses were one case, answered GAP on every trial, against the rule that counts a related tool as transferable. gpt-6.1-sol reasoned about a third as much (2,779 against 7,925 tokens).
- Match, 2026-11-06, the same 19 x 3 on gpt-6.1-sol with only the output ceiling changed: 56 of 57 for USD 0.055. Every miss was one case, a named tool the CV shows only through its ecosystem, read as unproven.
- After the switch, one real posting re-evaluated with prompts unchanged moved from 92 to 86 per cent on one re-sampled requirement (2026-10-05).

**What we concluded.** Luna costs about a tenth of sol's price and is not at parity with sol on either extraction stage. Sol stays. gpt-6.1-sol replaced gpt-6-sol on 2026-10-04 on cost, by the maintainer's call, against a reviewer who read its miss on the Helm charts case (a requirement for infrastructure delivery with a related tool) as a regression. The cost of that choice is one known related-tool case.

**Open.** Only match at medium was screened for gpt-6.1-sol. Extraction at low and the drafting stages were never compared on it.

## Reasoning effort as a lever

**What we did.** Paired runs that changed only the effort setting.

**What the numbers showed.**

- Extraction, 2026-08-14, 22 cases on terra: `none` cost USD 1.0806 against `low` at USD 0.9692, 11.5 per cent more. Output barely moved (90,704 against 92,687 tokens). The difference was caching: the `none` arm wrote 329,476 cache tokens and read 123,516, against 226,361 and 226,385. Agreement moved 6 to 7 of 22, which is one case.
- Drafting, 2026-08-15, two postings: terra at medium cost 35 per cent less than at high and scored identically on all six critic axes.
- Match, 2026-09-23, 13 cases x 3, same prompt and manifest: low 36 of 39 for USD 0.041343, medium 39 of 39 for USD 0.057228, 38 per cent more. All nine low-effort trials on the hardest case, across three runs, spent zero reasoning tokens. At medium the same case reasoned 90 to 180 tokens and passed.
- Critic, 2026-09-23, 10 cases: low caught all 10 plants for USD 0.092364 against USD 0.158845 at medium. Per draw, low caught 13 of 15 plant-draws and medium 15 of 15. One plant survived at low only because the merge keeps any single draw's doubt.
- Critic, 2026-11-06, on gpt-6.1-sol: low effort named the planted boundary on every planted case (34 of 34 draws over 17 cases), as default effort did (51 of 51), for about 11 per cent less per call. Default effort false-failed clean cases on 9 of 93 samples. On the reseal prompt, which requires a quoted line for each doubt, both arms then ran every case: 36 of 36 on clean cases each, and 59 of 63 each on the 21 planted and boundary cases. The designed-as-shipped plant inserts a rollout into an entry that says only "designed", so it is labelled `planned_not_shipped` and also accepts `production`, which every draw named; the reseal prompt rules out `absent_from_record` once the cited entry touches the subject. Rescored, low reads 62 of 63 planted and default 98 of 99 overall. Low cost about 18 per cent less per call and its p95 latency was near 10 s against 24 s. The critic ships at low effort. On Anthropic that is a drop from high and has not been measured.

**What we concluded.** Effort is not the cost lever on short extraction calls. Reasoning is a small share of their output, and changing effort disturbs caching more than it saves. It is an accuracy lever where a judgment hinges on reading a qualifier. Match ships at medium. The critic ships at low once the paired arms ran the same cases on the same prompt. Drafting at medium matched high on two postings, which is a price, not a verdict.

**Open.** On terra, `minimal` was rejected with a 400 mid-run (2026-08-14). The accepted effort set is a property of the model, not the provider.

## Cost economics

**Per unit.** The first full baseline (2026-08-13, terra, standard tier) priced screening at USD 0.0888 per posting warm and USD 0.1285 cold. Three of four complete applications cost a mean of USD 0.2874. The fourth hit the redraft loop three times, cost USD 0.5693, and still failed verification. The most expensive outcome was the one that could not be sent.

**Flex tier.** Flex is OpenAI's cheaper, slower service tier. The first flex run (2026-08-14) served all three flex applications at half the standard rate. Unattended work has run on flex since. Two failure modes surfaced later. A flex refusal delivered inside the stream skipped the standard-tier fallback (2026-10-05, nothing billed). One call in the 2026-10-04 screen ran 609 seconds and fell back to standard, 17 per cent of that arm's cost. After the stream fix, 77 flex calls ran with 0 resent on standard.

**Prompt caching.** The cache prefix is the opening part of a request that the provider stores and bills at a discount when the next request starts the same way. A cache read costs a tenth of normal input and a write costs 1.25 times, so a prefix that is written and never read loses money. An audit of 257 calls on 2026-08-31 found five of seven stages below a 1.0 read-to-write ratio, and 50 calls that wrote a cache entry nobody read. Match read 2.02 tokens per token written because the CV evidence block sat at a stable prefix. On 2026-08-14, cutting an 81,235-character duplicate block to 1,030 moved a warm application only 7.8 per cent, because the block was already cached.

The relevance stage taught a subtler lesson: a setting that looks unrelated can break the cache. The schema is the shape the model's answer must have, which the provider enforces, and under explicit caching it sits ahead of the cached prefix, so it is part of what must match (OpenAI's [prompt caching guide](https://developers.openai.com/api/docs/guides/prompt-caching) lists which settings change the prefix). Our schema held an enum, a fixed list of allowed values, built from that run's posting ids. That made every run's prefix unique, and 15 tasks wrote 576,180 cache tokens that a stable prefix would have read. Allowing any string for those ids, and checking them in code instead, fixed it. On 2026-09-28 a second task with different postings read the whole 28,647-token prefix the first wrote, for USD 0.0196 against USD 0.0519 ([RESULTS.md, stage experiments](RESULTS.md#stage-experiments-outside-evalsh)).

**Batching.** Production relevance review sends up to 20 postings per call with the CV evidence once. An eval harness that sent one posting per call cost about four times as much per posting (2026-09-28, USD 0.9223 for 31 calls). Its estimator was six times off (2026-09-20) because it averaged batched production calls.

**Output length and search fees.** On 2026-11-06, asking relevance review for low verbosity on 47 real-workspace postings, in three batched calls, cut output from 19k to 11k tokens and cost from USD 0.225 to 0.154. Rows labelled `investigate` kept out of `unlikely` held at 19 of 20 on both arms. Lower search depth on guided search cut input tokens 9 per cent and saved nothing, because hosted-search fees dominate that bill. An eval-harness change that let repeated trials of one case read the same cache prefix took the critic from 51 to 83 per cent cache share and from USD 0.490 to 0.354, with identical prompt tokens and no scored outcome changed.

**Billed but unusable.** Six calls on archived postings were paid and then refused at commit (2026-09-26, USD 0.1117). Two rows that were dismissed variants failed after spend (2026-10-15, USD 0.0963). A retry after a timeout re-reviewed 36 unchanged rows (2026-10-01, USD 0.179 spent twice). Archived postings are now refused before the paid stages.

**Totals, with their limit.** Of the ledger's 192 entries, the 131 whose headings carry a USD figure sum to about USD 50. That is not a full total. The August entries priced in another format, earlier totals omit the hosted-search fee of USD 10 per thousand calls, and six early rows are estimates rebuilt from a model-written report (2026-08-16 audit).

## Prompt engineering, with before and after

**Match rules.** The frozen 10-case baseline (2026-08-26, prompt `2cb005427148`) scored 7 of 10, with one anti-fabrication hard failure: generic participation upgraded to DIRECT delivery. The corrective prompt `d204058a7e84` scored 9 of 10 the next day, each run about two cents. On 2026-09-23 a rule-5 rewrite fixed its target case (0 of 3 to 3 of 3) and broke two others, one systematically. A narrower rewrite recovered most of it (34 to 36 of 39), and medium effort closed the rest. Strength agreement moved from 20 of 26 to 27 of 27 at prompt `93253db7d4ab` once rule 6 graded an unmet stated qualifier `weak` ([RESULTS.md, Match](RESULTS.md#match)).

**Relevance rounds.** Six rounds on real postings, recorded in [RESULTS.md](RESULTS.md#a-prompt-ab-on-a-real-workspace), mostly taught how not to measure:

- Round 1 credited nothing. Prompt, CV, parameters, batch composition, and notes all changed together.
- Round 2 measured nothing. The harness sent one posting per call, so the old arm could not reproduce the batch-mode refusals. A citation contradiction between prompt and validator landed on its merits.
- Round 3 told the model to decide before summarizing. Unlikely draws rose from 5 to 8 of 27. The response schema puts the summary first on purpose (`tests/durability/test_reasoning_before_label.py`), and wording that fights the schema order loses.
- Round 4 gained one trial on its target and lost one gold trial. Blocked.
- Round 5 cut the target failure from 24 to 11 of 39 trials and moved six controls. Not landed: wording could not enforce the class.
- Rules (a) to (d), 2026-11-01, a four-rule change to the relevance prompt: 36 to 38 of 45 on the real-workspace labels, unchanged on the synthetic gold set, and `absent` verdicts that cited no CV id fell from 14 to 0. Landed, with one target recorded as a limit.

**Jobspec example cues.** On 2026-09-27, three real postings re-read under a rule that keeps "such as" lists with their items. Requirements that lost their cue fell from 15 to 4, all four legitimate trailing splits. The fix exposed a match-stage reading that calls listed examples unshown.

**Jobspec degree ladders.** On 2026-10-19, two branch draws read a degree ladder as one blocking gate holding every option. The control repeated it as a non-blocking years gate (7 calls, USD 0.1343).

**Jobspec boundaries.** The product lets a person list kinds of work they do not want. Jobspec names the one that applies, and a named boundary skips the posting. On the shipped prompt, a live posting for a research AI lead role at a pharmaceutical company was routed to a generic-engineering boundary in 3 of 5 draws. Every hit used a duty sentence as the occupation, and the boundary's "generic" qualifier did not stop it. A synthetic domain-tied lead fired 5 of 5. The fix edits rules 9 and 10 of [`prompts/jobspec_system.md`](../prompts/jobspec_system.md). A duty sentence no longer defines the occupation when a span names the role. A no-domain qualifier fails when the role is tied to a named subject domain. Over 50 draws in round 1 (USD 0.8712) the candidate went 0 of 5 and 0 of 5 on those two. The generic ML platform case still fired 5 of 5, and two earlier true positives 5 of 5 each.

Review then flagged that the round-1 clause could suppress a pure-software boundary on a generic title with a business suffix. Round 2 (60 calls, USD 0.9312) added one sentence to rule 10: when the boundary itself names the domains it excludes or keeps, judge the posting against that list. Both arms read identically, and a new synthetic Software Engineer, Payments case fired 5 of 5. An offline check on 110 recorded occupation texts found 0 hits on the fallback path.

One limit came out of the review. Round 1 never reproduced the duty-sentence span on the live posting: all ten draws used the role sentence. So the fix is shown on the synthetic pair, and the live posting's 0 of 5 is consistent with it but does not prove it.

The cases became a permanent eval of eight synthetic postings. Its first run (81 calls, USD 0.5537) scored the old prompt 40 of 40 and the shipped prompt 39 of 40, one missed draw on the generic ML platform. Neither misfired on its domain-tied cases. That is the variance lesson again: a defect seen 3 of 5 times on one live posting did not appear on new synthetic postings, so the set is a regression guard, not proof of the fix.

**Critic.** The 2026-09-22 reseal (re-recording the replay cassettes) failed on content preservation. All six draws reported no unsupported claim, and every below-floor draw named a line the program copies verbatim from the CV, not model text. The fix declared those lines and stopped a single minority draw from forcing a rejection. On 2026-09-23 three clean fixtures drew doubts on all three draws. Reading the synthetic CV showed the fixtures were wrong, not the critic. It happened again on 2026-11-06: a planted case failed on its planted line 3 of 3 under a different boundary than its label, and the prompt's own definition showed the critic right and the label wrong. A second boundary, planned work written as shipped, the critic never separated from `production` or `adjacent` in six draws, though it caught every claim, so it is recorded as a limit.

**Draft required phrases.** On 2026-11-06 a synthetic career-change draft failed 3 of 3 because it dropped the skill item carrying a phrase the guardrails require. The draft prompt never names required phrases, the program checks them after drafting, and that failure allowed no redraft, so one dropped item sank the document. With the check made redraftable, and its feedback naming the phrase the selected evidence carries, the first attempt still failed 3 of 3 and the redraft passed 3 of 3 (42 of 42 on the redraft task, USD 0.246). The fix costs one extra call on a case that trips it. Cover letters share that redraft loop but have no case that trips it live: a posting asking the letter to cite the request and to give figures the record lacks passed attempt one in 6 of 6 draws, so the cover-specific redraft path is unmeasured.

**Asserted status lines.** The first whole-posting match case (33 requirements, one call) asserted only Canadian work authorization, and the match stage read a United States authorization requirement as answered by it 3 of 3. The single-requirement set never caught this, because both of its asserted cases restate their line exactly. The prompt now says a line about the same kind of fact for another country, place or status does not answer, while a place inside the one asked for, or a status that carries it, does. A synthetic probe went 42 of 42 across both directions, and the 19-case set went 57 of 57. The whole-posting case reads 31 of 33 per draw, after a ruling for one row whose `GAP` label predates the rule that answers a tool shown only through its platform `TRANSFERABLE` at `weak`.

**Cover letter subject.** A prompt line saying the company paragraph states what the employer or the role does, not what the role calls for, cut sentences opening "The role" or "This role" from 5 and 7 per 14 letters to 1, with 14 of 14 letters still cleared (2026-11-06).

## Variance and measurement

- One document, 18 critic trials (2026-09-20 and 09-21): 7 fails, 11 passes, all on a genuine unsupported claim. That is a 61.1 per cent miss rate, with a Wilson 95 per cent interval on the catch rate of 0.20 to 0.61. A Wilson interval is a range around a pass rate showing how far it could move by chance, given how few cases there are; reporting one is the advice in [Miller 2024](https://arxiv.org/abs/2411.00640). Each draw either saw the distortion or did not; none wavered at the floor.
- One match case read DIRECT on 2, 0, 0, and 2 of 3 trials across four runs of an unchanged prompt. A fix that "moves" it in one run moved nothing.
- The boundary defect was 3 of 5 on one posting. One draw or a cached replay cannot see a 3-in-5 effect, so the fix rounds used five draws per case. Even five draws did not reproduce the original span on the live posting.
- Labels come from two blind reviewers and an adjudicator, frozen before a run. A relabel gets a new manifest digest (a digest is a short fingerprint, a hash, of a file or request; any change makes a different one), and rows compare only at the same digest. Rulings are calibration data, not gold.
- A case can run green and measure nothing. A guided-search case written to see when a board page is picked scored 0 of 0 on every trial: none of 104 picks across it and the 13-case baseline was a board, because the prompt allows a board only as a fallback. The same baseline showed one pick in five pointing at a closed posting, stale search-index entries that repeated on both trials.
- A wrong score is not always a wrong answer. On 2026-09-20 two relevance misses were the scorer reading a field the product deliberately empties. It was fixed from the stored logs, with no rerun bought.
- `scripts/eval.sh run` runs a scripted task against a canned provider, or the match replay task against a stored calibration snapshot. Neither tests a prompt; only `--live` does. The cassette replay in `tests/delivery/test_recorded.py` runs the real SDK against a recording. Any change to a prompt, schema, or schema docstring changes the request and invalidates it. Reseals on 2026-09-18, 09-22, and 11-02 cost USD 0.12 to 0.31 each, covering one tailoring run. The 2026-11-06 reseal recorded one call for each stage the recorder knows, seven cassettes, for USD 0.40.

## Structured outputs

- A very large CV would break matching. OpenAI strict mode allows 1000 enum values per schema ([enum size limits](https://developers.openai.com/api/docs/guides/structured-outputs#limitations-on-enum-size)), and 15,000 characters across string enums over 250 values. Match closes cited ids over the CV's entries, so a CV past about 870 entries would fail every call. `src/clearscout/infrastructure/providers/base.py` now refuses such a schema before any request. Whether a shared `$defs` enum counts once is undocumented.
- Editing a docstring changes the request. A Pydantic docstring on a response model reaches the wire schema, the request digest, and the cassette (the same behavior [Instructor documents](https://python.useinstructor.com/concepts/fields/)).
- A closed enum is a grounding tool and a caching hazard at once. Closing over per-run ids broke the cache prefix (above). Close over stable sets and check per-run ids in code.

## Grounding and anti-fabrication

- Every cited id is checked against the CV evidence selected for that run, and every quote against the stored posting body. On the 2026-09-27 queue review, 493 of 493 cited CV ids and 587 of 587 posting citations held at their offsets. On 2026-10-06, 88 of 88 jobspec quotes matched the body after whitespace normalization.
- The guard is code, not the reader. Guided search intersects the model's cited ids with known entry ids and drops a proposal left with none (2026-08-18). A relevance answer that cites no evidence for an established qualification is rejected as `invalid_cv_evidence`, and a contradictory class set as `inconsistent_verdict`.
- What models invented, and what caught it. The match baseline upgraded participation to delivery (2026-08-26). A draft bullet credited the model's own "reasoning" in a career break, which the critic caught (2026-09-18). One relevance answer invented a posting id, recorded as a missing result (2026-09-28).
- `verified` means nothing was invented and every line is cited. It is the floor, not quality: luna passed it while scoring a point lower on three critic axes.

## Failure modes in production

- A non-streaming request waits for headers until the whole answer is ready, and the connection dropped at 60 seconds. Long drafting calls were lost, not slow. Streaming fixed it (2026-08-15).
- A raw stream `ReadTimeout` escaped error translation and the flex fallback, hung about 1,903 seconds, and the retry re-reviewed 36 rows (2026-10-01).
- A flex refusal inside the stream skipped the fallback (2026-10-05). Both stream error shapes are now classified, with no resend after output.
- A request for `gpt-5.4` was served as `gpt-5.4-2026-03-05`, and 46 of 138 listed models carry a date (2026-09-20). A dated served name now resolves to the shipped model it names, so its spend is priced.
- On 2026-08-16 four paid paths spent without writing a ledger row. Every paid entry point now files one. A cache write failure no longer erases a billed response, because usage is recorded before the cache.
- A jobspec on a 9,600-character posting hit a 6,000-token output ceiling on all four calls. The ceiling is now 16,000, and the proof finished at 7,026.

## If you build something similar

These are opinions, each backed by a run above.

- Check every citation in code. The model will cite plausible ids it was not given, and a reviewer will not catch every one.
- "Nothing was invented" and "this is good" are two different measures. Report both.
- Test a cheaper model on your hardest cases, not your average ones. Luna and gpt-6.1-sol both looked fine until one case failed on every trial.
- Raise effort where the answer hinges on reading a qualifier. Do not expect lower effort to save money on short calls.
- Plan what the cache prefix will be before you build. Put stable evidence and a stable schema ahead of per-request content, and measure cache reads against writes for each stage.
- Make your eval send requests shaped like production. Production batches rows per call; an eval with a different request shape measures something else.
- Run enough draws to see the effect, and confirm the defect reproduced before you credit a fix. Two draws could not separate a 3-in-5 defect from chance, and a synthetic pair was what showed the fix. (Miller makes the same point about uncertainty and paired comparisons: [Miller 2024](https://arxiv.org/abs/2411.00640).)
- Change one thing per round. Write down the pass and stop rules before the run, and run a gold set beside it to catch regressions.
- Stream long calls, handle every error shape the provider can return, and refuse work before the paid stages when its result cannot be stored.
