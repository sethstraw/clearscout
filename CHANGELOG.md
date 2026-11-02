# Changelog

Changes a user would notice, in [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) form, versioned by [Semantic Versioning](https://semver.org/).

## [Unreleased]

First public alpha, for technical early adopters. Read
[validation status](docs/validation.md) before relying on it: only the OpenAI
path has run against a live provider, board readers have uneven live coverage,
and every drafted document still needs your own line-by-line review.

### Added

- Create a workspace and save manual search preferences without a CV or provider
  key. The free walkthrough supports skip and return; board scans require a
  saved target title, while individual posting intake remains available.
- Add several sources or posting links with independent outcomes. Scan a selected
  source batch, inspect per-source results, cancel its follow-up work, and resume
  without losing collected postings. Busy sources do not block unrelated reads.
- Schedule daily or weekly local scans with a visible timezone, next run, pause,
  and one startup catch-up. Scheduling runs only while the local worker runs.
- Keep uncertain discoveries accessible in Unconfirmed matches, save strict
  visibility per workspace, and track selected results without a paid evaluation.
- Show when posting details were read and offer explicit fresh reads. Failed
  refreshes retain known content, and person-dismissed postings stay protected.
- Build a master CV from a Word file, PDF, Markdown, plain text, or a
  screenshot. Every restructuring decision is cited back to the source, and an
  import report names anything that could not be read.
- Screen one posting, or two to five related postings side by side, without
  drafting anything.
- Tailor a resume to one posting. Every drafted line is tied to an evidence
  identifier from the CV, and a report shows what was selected, what was left
  out, and why.
- Draft a cover letter from the same evidence and cited employer research.
- Ask about experience the CV does not carry, and add only what is confirmed.
- Review the result line by line and accept, reject, or replace each line.
- Matching and drafting cite only identifiers that exist and only entries
  selected for that run. Titles, employers, dates, education, and publication
  citations are copied from the CV, never rewritten by a model.
- The deterministic reading of a posting is reported beside the model's own
  reading rather than merged into it. Where the two disagree, both are shown and
  neither wins.
- Every finding is an answer or an explicit abstention. A posting that does not
  state something has not stated it, and nothing falls back to the nearest
  label.
- Every requirement and hard gate quotes the posting, and the quote is placed in
  the fetched body before the run continues. One that cannot be placed is kept
  as `needs_review`, reported as needing a person, and excluded from verified
  hard-gate scoring. Extracted keywords the posting does not contain are
  dropped.
- Content rules a regular expression can decide are enforced by the program.
  The rest are injected verbatim into the prompts, so a model is judged against
  wording it was given.
- Paid evaluation confirmations show estimates from
  configured models and prices, including a range, currency, pricing source and
  date, or explain why an estimate is unavailable. Completed-run cost history is
  shown separately. Estimates are advisory and do not stop a confirmed run.
- Prompts are editable in the Prompts page. An override is saved as an immutable
  full-text version; you can inspect its versions and diff, revert by appending
  another version, or reset to the packaged text. The regions each prompt file
  marks as fixed are refused at save and again before sending, so the
  anti-fabrication, evidence, untrusted-data, abstention, citation, and workflow
  rules survive any rewrite of the objective, the style, or the rubric.
- A loopback-only web client over the same services the CLI uses, and a
  separate worker for long operations. `tailorcv start` runs both.
- A job ledger that keeps every evaluation, posting observation, and diff, with
  evidence anchored to the bytes it came from. Recalculating an evaluation adds
  a version rather than overwriting one.
- Job-search parameters: where you may work and on what basis, which working
  arrangement you will accept, compensation bands per place and per currency,
  the seniority band and titles worth reading, and which scoring policy applies.
  They are settable in the settings file, editable in the web client, and
  versioned per workspace. Every field is optional and an empty field abstains
  rather than deciding, so configuring nothing changes nothing.
- Four preference findings in the deterministic reading: which declared place a
  posting is in, whether its arrangement is one you want there, whether its
  level is in your band, and how its pay lands against your floor. A posting is
  matched to a place using only the country, region, metro, and alias strings
  you wrote, so an undeclared market reads as undeclared rather than unsuitable.
  A posting naming two of your places is a conflict rather than a pick. Pay is
  compared only within one currency and one basis; no exchange rate is applied.
- A place may carry its own working arrangement, layered field by field over the
  general one, so remote-only in one market and on-site-acceptable in the city
  you live in is expressible.
- The Jobs ledger filters by place and shows the place each evaluation recorded.
- Countries are read from the territory names Babel maintains. A `countries`
  list in the search parameters adds your own spellings and never narrows what
  is recognized.
- A board scan reads the registered sources whose platforms publish openings as
  data. `PLATFORMS` in `src/tailorcv/infrastructure/web_fetch/platforms/__init__.py`
  lists every supported platform. A career site on its own domain is fetched
  once to see which of those it fronts.
- Two aggregators, Himalayas and Arbeitnow, list many employers and name none.
  An aggregator read is bounded to the newest listings and never reports itself
  complete, so an opening missing from one is never treated as withdrawn. Each
  of its openings keeps the employer its own posting states.
- Himalayas openings apply through the address Himalayas gives for each job,
  and Himalayas and Arbeitnow rows carry a "Job data from" link back to the
  aggregator.
- On a board read across several listing pages, an opening missing from one
  complete read is marked and its own page is checked. It is withdrawn only
  when the next complete read also lacks it, and the check keeps an opening
  whose page is still live.
- A posting whose retained text appears cut off shows Needs recovery, and a
  full evaluation skips it at no cost until the full text is attached or read.
- A labelled pay range under 200 with no period word reads as hourly, and a
  Primary Location range is read over other locations' ranges in the same
  country. Stored pay text and its evidence quote the posting's own line.
- A request whose response schema would pass OpenAI's structured-output enum
  limits, such as a match over a very large master CV, fails with a named error
  before anything is sent.
- A posting whose title its employer marks "do not apply" or "test
  requisition" is screened out of the queue and says why.
- New reviewable listings are staged as pending discoveries for a person to
  screen. A listing is kept out of the queue only on a precise published fact,
  a structured location or working arrangement no saved location accepts, and
  it stays promotable from the screening ledger. Uncertain, multi-place,
  incomplete, or remote-alternative facts stay visible.
- A career site's sitemap is discovered up to 5,000 posting URLs, and every
  discovered page is read or kept as continuation work. A scan whose discovery
  hit a bound stays incomplete. A posting page read more than a week ago, or
  before the search parameters last changed, is read and screened again.
- A residence requirement in a posting is judged against the countries your
  saved locations accept. Work-authorization wording alone is not read as one.
- A found title that names only an area, such as "Revenue Operations", reads as
  a near match rather than a match, and an identical title always matches. A
  title sharing an occupation with a searched title, in a family the shipped
  taxonomy declares adjacent, reaches review rather than being screened out.
- A posting body stating a day count in office ("3 days per week in office")
  reads as attendance; optional, capped, or temporary office wording does not.
- The list of sources is yours to keep, and a source may be an employer board or
  an aggregator. Add one by naming the employer and its address, correct an
  address that has moved, switch it off so a sweep skips it, or retire it and
  keep the scans it recorded. An address naming no recognized platform is kept
  rather than refused, and each sweep records why it could not be read.
- A source may carry the condition its operator publishes for reading it, with
  the address that condition was published at. Switching a source on states the
  condition back and waits for it to be accepted. TailorCV records and shows the
  condition; it does not check that it has been met.
- A guided campaign uses the configured provider's hosted web search to propose
  a bounded set of direct postings and employer boards. Local URL and fetch
  policy decides what may be opened, and no proposal becomes a ledger job until
  the normal evaluation verifies it. Each attempt records how many job-detail
  links its web search returned that were not proposed.
- Posting intake reads publisher JobPosting JSON-LD before generic page text.
  Closing dates retain publisher wording, precision, timezone and provenance.
  Free personal date overrides survive refresh and tracking; reset reveals the
  publisher date or Not stated. A passed date retains readable content and warns
  to verify availability. Explicit publisher closure still governs closed status.
- Scoring weights are versioned per workspace and editable in the web client,
  with a preview of how a proposed policy would reorder the ledger. Saving
  appends a policy, and committed evaluations keep the weights they were scored
  under.
- Evidence carries no weight in the shipped scoring policy, because how well
  documented an assessment is says nothing about how good the job is. The ledger
  shows the evidence score beside the overall as confidence.
- Recorded model spend per workspace: the total, the call count, and a per-stage
  breakdown. A call whose model had no configured price is named rather than
  counted as zero.
- Every model call records its prompt templates, provider, requested and served
  model, effort, tool policy, package revision, latency, and digests of what was
  sent, and keeps the full request and response in the local database. A call
  that was billed and returned something unusable is recorded too, so a run's
  cost matches the invoice.
- Edit the master CV in the web client. An edit appends a version, identical
  content is a no-op, an edit that does not parse is refused, and a pre-save
  report names the citations in committed evaluations that point at text about
  to change.
- Settings has four top-level tabs: Workspace, Search, Drafting, and Activity &
  signals. Sources is a sub-tab under Search.
- Board scans and guided board reads keep hard-constraint exclusions in pending
  review. The pending queue defaults to Not excluded, while Screened out and All
  constraint routes remain available. Each lane states its own range and total, and the filter
  note says how many rows on the current page match, so a filter never hides
  rows silently.
- Review found jobs shows several jobs per screen, each row carrying the facts
  needed to triage it without opening details, and a collapsed per-source table
  of listings each source's latest board scan screened out.
- Scan history filters by source and result across the whole retained history,
  not only the rows one page loaded.
- Activity & signals carries a learning-loop dashboard with three counts per
  workspace: identities re-proposed after a rejection, master-CV proposal
  acceptance, and missed-requirement matches among the top-ranked pending
  discoveries. Each is a numerator over a denominator, and a zero denominator
  reads as "no data yet."
- Two opt-in paid dispute re-checks under Settings > Activity & signals. Refresh
  with dispute re-extraction asks the extractor once to re-check every active
  extraction dispute and reverses one only when its cited quote appears in the
  current body. Refresh with fit re-match asks the matcher once against the
  current master CV and reverses a fit dispute only when it cites a CV entry the
  earlier match did not. The free Refresh records an observation and diff
  without calling a model.
- A batch comparison is filed against every job it compares.
- `tailorcv db import` copies an earlier TailorCV database into a fresh one. The
  source is opened read-only, `--dry-run` prints the plan without writing, the
  target must be empty, every shared-column collision is compared, and anything
  the current schema cannot hold is named
  rather than dropped quietly. Explicit old and new roots relocate only
  recognized paths and verify master-CV and artifact bytes by SHA-256 and size.
- `tailorcv db backup` writes a verified SQLite snapshot through SQLite's backup
  API and refuses to overwrite an existing destination.
- A custom SQLite database keeps its workspaces and artifacts beside the
  database unless explicitly overridden.
- Startup never migrates a database. Back up first and run `tailorcv db upgrade`
  explicitly. Once 0.1.0 is tagged, its schema revision is frozen, and every
  later schema change adds a revision that preserves existing data.
- Releases ship a wheel with the web client built in, a source archive, and
  `SHA256SUMS`. A source checkout is a contributor workflow, not an
  installation method.

### Security

- The local server accepts only loopback Host authorities and exact
  same-origin browser writes, and keeps its content-security, no-store, frame,
  referrer, upload, and download protections.
- The browser never receives an API key. A key is typed once and kept in the
  system keyring.
- Backups, the database, and artifacts are written owner-private.
- robots.txt decides every read except a posting URL you enter yourself. Board
  and listing reads, refreshes, and every later read of a stored URL honor a
  robots disallow.

[Unreleased]: https://github.com/sethstraw/tailorcv/commits/main
