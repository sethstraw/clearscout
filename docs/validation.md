# What has actually been validated

Maintainer use on real postings has found mistakes the offline tests did not
catch. That experience is useful, but it is one person's use, not an
independent quality study.

This page separates that experience from what the tests establish. A green
test says something about the path it exercised. It says nothing about a
provider, board, browser, or renderer it never reached.

## The public gate is offline

`scripts/gate.sh change` is the ordinary contributor gate. It runs deterministic
Python and web tests, scripted prompt evaluations, contract checks, privacy and
license audits, and generated-artifact verification. Provider credentials are
removed, keyring access is replaced, and non-loopback sockets are refused in
ordinary automation. The gate cannot spend money.

The frontend component suite runs in jsdom. It checks rendered states and user
interactions, but it does not prove layout in a real browser or the complete
SPA/API/worker journey. The synthetic demo seed is the public source for manual
browser review.

## CV ingestion remains a limitation

CV ingestion is not yet well tested across real-world layouts and formats.
Live OpenAI intake and synthetic tests do not establish reliable conversion
of arbitrary CVs.

Downstream work uses the parsed evidence from a standardized TailorCV Markdown
master. The parser's recognized structure matters: retained text in an
unrecognized section is not automatically citable evidence. Start from
`tailorcv template`, inspect the master and import report, and check that the
needed evidence is available before evaluating or drafting. The
[intake instructions](user-guide.md#initialize-one-workspace) describe the
accepted file types and paid restructuring path.

## Provider claims are deliberately uneven

Only OpenAI has been run against a live provider, for CV intake, screening,
drafting, criticism, verification, cover-letter research, guided campaigns,
and relevance review. The repository also exercises the OpenAI request and response
boundary through official-SDK contract tests and synthetic replay seams.

The Anthropic adapter uses the official SDK and has offline contract and
synthetic replay coverage. It has not been run against Anthropic's live API
here. No public claim should compress those two evidence levels into a single
word such as "supported." The public gate does not stand in for live evidence.

The critic puts the same question `critic_trials` times on one draft, once by
default, and fails it on any unsupported claim any trial named, so the critic
stage bills that many calls for each draft attempt.

The critic names, for each claim it doubts, the boundary that claim crosses,
from the closed set in `ClaimBoundary`. The response schema enforces the set, so
free text is refused. A doubted claim fails the draft whatever boundary it
carries; the boundary describes an existing failure rather than deciding one.

The long synthetic hybrid CV in `evals/cvs/hybrid_career.md` exercises three
role families through persisted evaluation, shortlist, tailoring, review, and
exported artifact read-back in `tests/durability/test_workflow_hybrid_journey.py`.
It is an offline regression, not independently blinded or live prompt-quality
proof. The vocabulary-only service also has no live quality measurement.
The guided campaign is covered offline by scripted background responses in
`tests/loop/test_guided_campaign_execution.py`. It has also run against a live
OpenAI provider. Those runs exercised background execution, the flex tier,
hosted web search, and local admission of returned URLs. Relevance review has
also run live and retained its judgments and citations. These are maintainer
observations, not independent or blinded measurements of search or judgment
quality. They establish no hiring outcome.

## Board readers are policy and parser claims

Board and posting tests replay synthetic or sanitized platform-shaped payloads
through the production fetch policy and readers. They check parsing,
deduplication, refusal, and durable commit behavior without reaching a job
board.

Some adapters were written against published platform APIs and some have also
been exercised live. That evidence can become stale when a platform changes, so
the application reports failures, refusals, and incomplete reads. A recognized
platform does not guarantee readable or complete coverage. A failed, refused,
or incomplete read is not evidence of zero openings. Read history states the
last observation, not a guarantee that a source is available today. The public
suite does not establish live coverage of any board.

Free scans, publisher-fact refresh, and user-supplied current posting text call
no model. Assisted recovery is a separate paid operation with an estimate and
explicit approval. Neither route bypasses reading policy or guarantees recovery.
The user guide describes [source coverage](user-guide.md#add-a-board-manually),
[individual posting intake](user-guide.md#add-one-posting-before-widening-the-search),
and [free and paid choices](user-guide.md#choose-the-free-or-paid-path).

Maintainer checks have read real boards and postings through several adapters,
including ADP and SAP SuccessFactors. These are bounded observations of specific
sources. They do not establish coverage across a vendor's tenants or later
publisher changes. The application reports each source's actual read outcome.

Classic iCIMS boards on a `<label>-<tenant>.icims.com` host are enumerated from
the site's sitemap, so a posting the sitemap does not list is not discovered.
UKG full-board coverage is unavailable where reading policy refuses the listing
route. A readable individual posting does not establish permission to read that
list. Darwinbox public career lists are read from `candidatev2` `allJobs` and
`home` URLs through the public search. Njoyn challenge pages and other
challenge-protected pages are unreadable. Some corporate and
Humi-hosted pages remain unrecognized; vendor attribution alone does not
establish board coverage. Individual intake of postings from those observed
pages has not been verified. Use an individual posting link or current published
text where intake permits it.

JazzHR uses structured posting data first, with a rendered-description fallback
when that data is absent. Missing or ambiguous required elements fail the read;
unknown company and location remain unknown. A bounded offline replay checked
board and detail responses; it does not establish behavior across tenants.
The board reader enumerates links in the fetched page; that does not establish
coverage of pagination or lazy-loaded rows.

ADP reads explicit employer evidence from structured publisher fields and a
bounded leading Company paragraph. Oracle uses the published LegalEmployer.
When absent, discovery can use the registered source identity with separate
provenance. Otherwise the recruiting tenant stays labelled as company not
stated. A registered source name is not a publisher-stated company.

## Known limits in 0.1.0

These include remaining defects and coverage limits from the maintainer's own
use. None lets a model cite evidence that was not selected. Read every drafted
line yourself before you send it.

- Fit measures whether the CV answers a posting, not whether you want the work.
  A qualified but unwanted role, one answered only weakly on its core
  must-haves, or one paid above your band can still reach `apply_now`.
  Seniority or role-type exclusions written as long sentences may not cap the
  decision.
- Requirement extraction can split alternative degree paths into separate
  must-haves, treat a home-office condition as a gate, miss hourly pay, or read
  a truncated teaser as a requirement. Short retained text that ends
  mid-sentence, or that a current relevance review found too short, is held as
  partial text and skips evaluation; a longer teaser still reaches extraction. Some residence-only wording, such as a
  clause naming residents of one country or a time zone you must be based in,
  is not read as a residence requirement by the deterministic reader.
- Deterministic discovery uses English wording and role context to read explicit
  constraints. Ambiguous arrangement or location stays unknown and can need
  clarification. A street address, a city name shared by several countries, or
  a place named only in the title can stay unresolved. Titles such as Principal
  Consultant can reach review when they match an explicitly saved role term.
  Unrelated titles and explicit exclusions can still stop at title screening.
- A relevance verdict of unlikely may cite no CV evidence; it never dismisses a
  row by itself.
- Board recognition and a readable posting do not establish complete board
  coverage. The [reader limits above](#board-readers-are-policy-and-parser-claims)
  describe the unresolved platform and enumeration boundaries.
- Tailoring keeps the evidence its role scoring rates relevant, and the critic
  sees only what was selected, so a strong item from the master CV, such as a
  publication, can be left out without a warning.
- A match batch can write more prompt caches than it needs, which adds cost to
  that batch. Chained publisher reads report their own read counts. The final
  total remains unknown while the board read is incomplete.

## Reading and layout limits

The deterministic reader's vocabulary is English, and it reads US and Canadian
postings best. A posting in another language mostly reads as not stated.

The page budget is an estimate from the line model in
`application/tailoring/layout.py`, measured against pressume's traditional
theme at balanced density on US Letter. Selection buys more than the page holds,
and the drafted Markdown is estimated with the same model and trimmed in drop
order until it fits. A trim never removes the last printed answer to a
must-have, an entry a prose line cites, or a role's last bullets; when those
are all that is left and the estimate still runs over, the run warns with the
lines over rather than fitting silently. TailorCV asks pressume for that theme
and density on every render; another renderer, paper size, or body size can run
long or short.

`max_bullets_per_role` is each role's share before breadth runs out, not a hard
ceiling. Every role fills to it before any role passes it, so a full page drawn
from a record with few rich roles can print a role past it, and the report names
that role and its count. Bullets past the cap are the first a trim removes,
taken from whichever role holds the most.

## Evaluation measures the rule, not human agreement

`uv run python -m evals` runs shipped stages against scripted provider answers
and compares measurements with recorded baselines. It checks that named failure
modes remain bounded and that deterministic ledger outcomes do not drift.

Replay does not test prompt quality against a live model. `scripts/eval.sh
--live` is the separate paid path; it requires an explicit operator approval for
that run. Human rulings calibrate the evaluation method and are not gold labels.

## Rendering stays outside the application

TailorCV tests request preparation, artifact promotion, and returned-artifact
verification. It does not run the external renderer in the public gate and does
not depend on the renderer at runtime. A verified PDF proves the expected bytes
were returned and stored; it does not prove every viewer or applicant-tracking
system will interpret them the same way.

## What a release gate can claim

A passing release gate says the locked source tree built, the declared packages
were audited under repository policy, generated artifacts matched their owners,
and the offline suites passed on the tested platforms. It does not prove the
software is secure, that later dependency resolutions are unchanged, or that a
live service still behaves as observed.
