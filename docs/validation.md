# What has actually been validated

Maintainer use on real postings has found mistakes the offline tests missed.
That is one person's use, not an independent quality study.

A green test speaks for the path it ran. It says nothing about a provider,
board, browser, or renderer it never reached.

## The public gate is offline

`scripts/gate.sh change` is the contributor gate. It runs deterministic Python
and web tests, scripted prompt evaluations, contract checks, privacy and
license audits, and generated-artifact checks. Provider credentials are
removed, keyring access is replaced, and non-loopback sockets are refused in
ordinary automation. The gate cannot spend money.

The frontend component suite runs in jsdom. It checks rendered states and
interactions, not real-browser layout or the full SPA, API, and worker journey.
For manual browser review, use the synthetic demo seed.

## CV ingestion remains a limitation

CV ingestion is not yet well tested across real layouts and formats. Live
OpenAI intake and synthetic tests do not show that arbitrary CVs convert
reliably.

Downstream work uses the parsed evidence from a standardized ClearScout
Markdown master. Text kept in an unrecognized section is not automatically
citable evidence.
Start from `clearscout template`, inspect the master and import report, and
confirm the evidence you need is there before evaluating or drafting. The
[intake instructions](user-guide.md#initialize-one-workspace) list the accepted
file types and the paid restructuring path.

## Provider claims are deliberately uneven

Only OpenAI has run against a live provider. That covers CV intake, screening,
drafting, criticism, verification, cover-letter research, guided campaigns,
search vocabulary, posting recovery, and relevance review. Official-SDK
contract tests, the recorded cassettes under `tests/cassettes/`, and synthetic
replay seams also exercise the OpenAI request and response boundary.

The Anthropic adapter uses the official SDK and has offline contract and
synthetic replay coverage. It has never run against Anthropic's live API here,
so the `anthropic-version` header and bearer authentication the current SDK
sends are untested against the live service. Do not fold those two evidence
levels into one word such as "supported." The public gate is not live evidence.

The critic asks the same question `critic_trials` times on one draft, once by
default. It fails the draft on any unsupported claim any trial named, and bills
that many calls per draft attempt it reads. An attempt that fails the
deterministic checks never reaches the critic. Neither does a first attempt
with strong coverage and directly cited evidence. Above one trial, a claim's
"raised by N of M" count groups doubts whose quotes are equal, nested, or share
the same content words; two trials quoting overlapping halves of one line count
as two claims.

For each doubted claim, the critic names the boundary it crosses, from the
closed set in `ClaimBoundary`. The response schema enforces the set, so free
text is refused. A doubted claim fails the draft whatever its boundary. The
boundary describes the failure; it does not decide it.

The long synthetic hybrid CV in `evals/cvs/hybrid_career.md` drives three role
families through persisted evaluation, shortlist, tailoring, review, and
exported artifact read-back in
`tests/durability/test_workflow_hybrid_journey.py`. It is an offline regression,
not independent, blinded, or live prompt-quality proof. The vocabulary-only
service has no live quality measurement.

`tests/loop/test_guided_campaign_execution.py` covers the guided campaign
offline with scripted background responses. It has also run against live OpenAI,
exercising background execution, the flex tier, hosted web search, and local
admission of returned URLs. Relevance review has also run live and kept its
judgments and citations. These are maintainer observations, not independent or
blinded measurements of search or judgment quality, and they show no hiring
outcome.

## Board readers are policy and parser claims

Board and posting tests replay synthetic or sanitized platform-shaped payloads
through the production fetch policy and readers. They check parsing,
deduplication, refusal, and durable commits without reaching a job board.

Some adapters were written against published platform APIs, and some have run
live. That evidence goes stale when a platform changes, so the application
reports failures, refusals, and incomplete reads. A recognized platform does not
guarantee readable or complete coverage. A failed, refused, or incomplete read
is not evidence of zero openings. Read history states the last observation, not
today's availability. The public suite shows no live coverage of any board.

Free scans, publisher-fact refresh, and user-supplied current posting text
call no model. Assisted recovery is a separate paid operation with an estimate
and explicit approval. Neither route bypasses reading policy or guarantees
recovery. The user guide covers
[source coverage](user-guide.md#add-a-board-manually),
[individual posting intake](user-guide.md#add-one-posting-before-widening-the-search),
and [free and paid choices](user-guide.md#choose-the-free-or-paid-path).

Maintainer checks have read real boards and postings through several adapters,
including ADP and SAP SuccessFactors. These are bounded observations of specific
sources, not coverage across a vendor's tenants or later publisher changes. The
application reports each source's actual read outcome.

| Platform                                             | How it is read                                                                                                                                                                                        | Known gap                                                                                                                                               |
| ---------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Classic iCIMS on a `<label>-<tenant>.icims.com` host | Enumerated from the site's sitemap                                                                                                                                                                    | A posting the sitemap does not list is not discovered                                                                                                   |
| UKG                                                  | Listing read where reading policy allows it                                                                                                                                                           | No full-board coverage where policy refuses the listing route; a readable individual posting does not grant permission to read the list                 |
| Darwinbox                                            | Public career lists read from `candidatev2` `allJobs` and `home` URLs through the public search                                                                                                       |                                                                                                                                                         |
| Njoyn and other challenge-protected pages            | Not read                                                                                                                                                                                              | Unreadable                                                                                                                                              |
| Humi-hosted and some corporate pages                 | Not recognized                                                                                                                                                                                        | Vendor attribution alone does not establish board coverage                                                                                              |
| JazzHR                                               | Structured posting data first, then the rendered description when that data is absent; missing or ambiguous required elements fail the read, and unknown company and location stay unknown            | The board reader enumerates links in the fetched page, not pagination or lazy-loaded rows; checked only by a bounded offline replay, not across tenants |
| ADP and Oracle                                       | Employer from structured publisher fields or a bounded leading Company paragraph (ADP), or the published LegalEmployer (Oracle); when absent, the registered source identity with separate provenance | Otherwise the recruiting tenant stays labelled as company not stated; a registered source name is not a publisher-stated company                        |

Intake of individual postings from the observed unrecognized pages above is
unverified. Use an individual posting link or current published text where
intake allows.

## Known limits in 0.1.0

These are remaining defects and coverage limits from maintainer use. None lets
a model cite evidence that was not selected. Read every drafted line before you
send it.

- Fit measures whether the CV answers a posting, not whether you want the work.
  A qualified but unwanted role, one answered weakly on its core must-haves, or
  one paid above your band can still reach Apply now. Seniority or role-type
  exclusions written as long sentences may not cap the decision.
- Requirement extraction can split alternative degree paths into separate
  must-haves, treat a home-office condition as a gate, miss hourly pay, or read
  a truncated teaser as a requirement. Short retained text that ends
  mid-sentence, or that a current relevance review found too short, is held as
  partial and skips evaluation. A longer teaser still reaches extraction. The
  deterministic reader misses some residence-only wording: a clause naming
  residents of one country, or a time zone you must be based in, is not read as
  a residence requirement.
- Deterministic discovery reads explicit constraints from English wording and
  role context. Ambiguous arrangement or location stays unknown and can need
  clarification. A street address, a city name shared by several countries, or a
  place named only in the title can stay unresolved. A title such as Principal
  Consultant can reach review when it matches an explicitly saved role term.
  Unrelated titles and explicit exclusions can still stop at title screening.
- An evaluation stored by an earlier build may quote the posting's opening
  paragraph under a requirement or reading that text does not support. Current
  builds store no posting quote when the anchor misses, so re-evaluating the
  job corrects it. No command repairs an old evaluation in place.
- A relevance verdict of unlikely may cite no CV evidence. It never dismisses a
  row by itself.
- Board recognition and a readable posting do not mean complete board coverage.
  The [reader limits above](#board-readers-are-policy-and-parser-claims)
  describe the unresolved platform and enumeration boundaries.
- Tailoring keeps the evidence its role scoring rates relevant, and the critic
  sees only that. So a strong master-CV item can be left out.
- For publications, the tailoring report lists each paper as available, not
  offered. `must_consider_publications` in `clearscout.toml` names paper ids
  that are always offered within `max_publications`. An offered paper can still
  be trimmed for length, most readily when no requirement cited any paper, and
  the report says so. A paper's id changes when its citation or section heading
  is edited, and the report names any listed id the master CV no longer has.
  Bullets have no such list.
- A match batch can write more prompt caches than it needs, which adds cost.
  Chained publisher reads report their own read counts. The final total stays
  unknown while the board read is incomplete.

## Reading and layout limits

The deterministic reader's vocabulary is English. It reads US and Canadian
postings best. A posting in another language mostly reads as not stated.

The page budget is an estimate from the line model in
`application/tailoring/layout.py`. It is measured against pressume's
traditional theme at balanced density on US Letter. pressume is the separate
project that can render approved Markdown. Selection buys more than the page
holds. The drafted Markdown is estimated with the same model and trimmed in
drop order until it fits. A trim never removes the last printed answer to a
must-have, an entry a prose line cites, or a role's last bullets. If only those
remain and the estimate still runs over, the run warns with the lines over.
ClearScout asks pressume for that theme and density on every render. Another
renderer, paper size, or body size can run long or short.

`max_bullets_per_role` is each role's share before breadth runs out, not a hard
ceiling. Every role fills to it before any role passes it. A full page drawn
from a record with few rich roles can print a role past it, and the report
names that role and its count. Bullets past the cap are the first a trim
removes, taken from whichever role holds the most.

## Evaluation measures the rule, not human agreement

`uv run python -m evals` runs shipped stages against scripted provider answers
and compares the results with recorded baselines. It checks that named failure
modes stay bounded and that deterministic ledger outcomes do not drift.

Replay does not test prompt quality against a live model. `scripts/eval.sh
--live` is the separate paid path, and each run needs explicit operator
approval. Human rulings calibrate the method; they are not gold labels.

## Rendering stays outside the application

ClearScout tests request preparation, artifact promotion, and returned-artifact
verification. The public gate does not run the external renderer, and the
application does not depend on it at runtime. A verified PDF proves the
expected bytes were returned and stored. It does not prove every viewer or
applicant-tracking system reads them the same way.

## What a release gate can claim

A passing release gate says the locked source tree built, the declared packages
passed the repository's audit, generated artifacts matched their owners, and
the offline suites passed on the tested platforms. It does not prove the
software is secure, that later dependency resolutions are unchanged, or that a
live service still behaves as observed.
