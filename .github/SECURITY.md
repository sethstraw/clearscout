# Security and privacy

ClearScout holds API credentials and your career history. Read this before you
point it at a real CV.

## Reporting a vulnerability

Use GitHub's [private vulnerability reporting](https://github.com/sethstraw/clearscout/security/advisories/new).
If you cannot, open an issue asking for a private contact method. Leave out
secrets, personal data, and exploit details.

Include the version, your operating system, a minimal reproduction, the impact,
and any workaround. Ordinary bugs and bad resume output can be public issues
once your own information is removed.

Security fixes land on `main` and ship in the next release. This is an early,
single-maintainer project, so there is no promise of a fast response or formal
support.

## What leaves your computer

ClearScout honors robots.txt and sends its own user agent. You are responsible
for following the terms of the sites you read with it.

A live request sends each model stage's material to the Anthropic or OpenAI API
you selected. That can include extracted CV text, job-description text, CV
evidence, personal rules, and earlier stage output. An imported screenshot goes
to the provider for transcription.

A URL read goes only through the shared address, robots.txt, redirect, and
rate-limit policy. robots.txt decides every read except a posting URL you enter:
a CLI URL argument, or a link from the add-a-posting action. Its check while
adding and its first content read proceed past a disallow aimed at all crawlers. A robots
group that names ClearScout still refuses them. Board and listing reads, later
refreshes, and every other read of a stored URL honor a robots disallow.

A refused URL may still be stored as the posting's identifier. Pasted, image,
and PDF text stays marked as user supplied, with no invented fetch time or HTTP
status.

Guided search runs as a durable task. It sends the bounded candidate record and
your saved search parameters to the configured provider, which may run its own
server-side web searches. ClearScout then reads only URLs those searches
surfaced, under the shared policy. A URL the model wrote with no search behind
it is not read.

Cover-letter drafting reads the employer's saved careers page and one same-host
page labeled as an about page. Those reads use the shared policy, do not follow redirects, and keep
only bounded text.

A board scan runs as a durable task on the worker, so its requests leave after
the queuing request returns. Each enabled source calls the published listing API
it names. A URL with no recognized adapter is fetched once under the shared
policy. If the page names a board, openings come from that platform's API. If
that API refuses automated access, the page already read is used. Otherwise
ClearScout reads openings from the site itself, in order:

1. JobPosting markup the page declares about itself.
2. Posting pages its sitemaps list, up to 40 per read.
3. Posting pages the career page links to.

A posting whose static read has no body can then load in a local headless
Chromium. That render runs on your machine against the third-party page, under
the same fetch policy for each request. A scan sends no CV text, posting text,
or search parameters. Scans run when you start one, or on the workspace's
optional schedule, which runs only while the local worker runs. The
[validation status](../docs/validation.md) separates parser coverage from live
board observations.

The Cornerstone reader sends one credential: the anonymous bearer token the
public career-site page gives every visitor in its csod.context bootstrap. It
goes only to the Cornerstone job-search API the bootstrap names. ClearScout takes
a fresh token from the page on each read or resume and never stores it or sends it elsewhere.

Some operators publish a condition on reading their listings. A source records
that condition and where it was published, and switching the source on shows it
to you first. ClearScout does not check that you have met it.

Refreshing model prices, when you ask, downloads the genai-prices data file from
`raw.githubusercontent.com`. It sends nothing of yours. A failed download keeps
the current prices.

Reconciling OpenAI spend, when you ask, reads your organization's billed cost
for the chosen window from OpenAI's organization costs API. It needs the
admin key described below, and sends only that key and the window.

ClearScout never submits an application or sends anything to an employer. It
cannot control your provider's logging, retention, training, data residency, or
account policies. For confidential, regulated, or third-party information, read
their current terms and settle your own obligations first.

## API keys

A key comes from a one-run command-line option, the provider's environment
variable, or your system keyring. It never goes into a settings file.
Diagnostics report where a credential came from, never its value.

Spend reconciliation uses a separate, optional OpenAI organization admin key,
from `OPENAI_ADMIN_KEY` or the keyring entry `openai-admin`. Without it, billing
reads are unavailable and nothing else changes.

Keep keys out of your shell history. Never paste one into an issue, report, log,
test fixture, or cassette. If one may be exposed, revoke it with the provider
and make a new one.

## Files stored locally

Personal settings, import reports, cached responses, and output packages can
hold names, contact details, work authorization, employment history,
unpublished material, source-line provenance, and text derived from the full
CV. ClearScout tries to create private settings and cache files where Unix
permissions exist. You remain responsible for where imported CVs and reports
go, device security, backups, cloud sync, and repository status.

This repository ignores the default `clearscout.toml`, `CV_Master.md`,
`Import_Report.md`, and common run directories. Custom names and destinations
are not ignored. Check `git status` before publishing. Do not assume an ignored
file is encrypted or excluded from other backup and sync tools.

The database and workspace artifacts default to `ClearScout` in the operating
system's documents directory, resolved through `platformdirs`. File pipeline
inputs and `--out` packages live wherever you point them. Raw provider-response
caches default to `clearscout/responses` in the operating system cache
directory, outside the Documents data root. Back up and delete those locations
separately unless you configured them onto the same private storage.

SQLite files are owner-only where Unix permissions exist, and use WAL mode. The
API does not create or migrate storage on startup and refuses non-loopback
serving. Browser responses use no-store caching, a same-origin content security
policy, frame denial, and referrer suppression.

The database stores structured outcomes, provenance, and the full request and
response of every model call. A request carries the prompt, and the prompt
carries the run's selected CV evidence, so the database holds that text. It
never holds provider credentials. Task errors are bounded before they are
stored. The web API reports only whether a credential is present, never its
content. Retention and export are in
[docs/privacy-and-retention.md](../docs/privacy-and-retention.md).

## Local web threat model

The web application protects one user on one machine from accidental network
exposure, hostile DNS rebinding or Host headers, cross-origin browser writes,
unsafe inline rendering, oversized uploads, and leaks through ordinary browser
caches or framing.

The server accepts only `localhost`, `127.0.0.1`, or `[::1]` Host
authorities, with valid ports. Browser `POST`, `PUT`, `PATCH`, and `DELETE`
requests must carry an Origin whose scheme, host, and normalized port exactly
match the request Host. A missing Origin is allowed for documented local
non-browser clients.

Every response, including an early Host or Origin rejection, keeps the
same-origin content security policy, `no-store`, frame denial, referrer
suppression, content-type protection, and a bounded correlation identifier.
Uploads are size-bounded while streaming. Downloads use sanitized names.
Untrusted HTML and unknown media are served as attachments, not inline.

This is not authentication. It does not defend against malware or another local
user who can read the account's files, connect to loopback, control the
browser, or run code as the user. Do not run ClearScout on a shared or hostile
account, expose it through a proxy, change the bind address, or weaken these
checks in place of a designed authenticated deployment.

## Job descriptions are untrusted input

A posting can contain instructions aimed at a language model. ClearScout
separates extraction from drafting, bounds the text that crosses later stages,
restricts evidence identifiers, and runs deterministic checks. These reduce risk
but do not make it fully resistant to prompt injection.

Report any case where posting text changes instructions, exposes unrelated
data, causes an unexpected network or file action, bypasses evidence rules, or
shows up in output as an unsupported claim.

## CV files are untrusted input too

Import reads local text, images, PDFs, and the XML inside `.docx` archives.
Malformed files should fail, not be trusted. Model-facing source text is escaped
and marked as untrusted data. Prompt-injection defenses and provenance checks
are not a sandbox or a malware scanner.

Import reports repeat source text and dropped lines, so remove personal
information before attaching one to an issue. A passing import means the
configured text checks passed. It does not prove the document was safe, that
extraction saw every visual element, or that the model gave every line the
right role.

## Recorded API responses

Recorded cassettes strip authorization headers and key-shaped values, and tests
check for those patterns. They can still hold the real CV, job description,
model output, request metadata, or other identifying data. Use synthetic data
for public recordings whenever possible, and inspect every cassette before sharing it.

## Dependencies and releases

CI installs exactly what the lockfile says and fails if the lockfile and
`pyproject.toml` disagree. The gate audits that environment against the Python
Packaging Advisory Database. It also audits the hash-pinned Linux and Windows
packages that environment markers leave off the current host, and refuses a
dependency tree with a license this tool cannot ship under. The web production
tree has its own version-bound advisory gate.

CI builds the production web
client, puts it in the wheel and source distribution, checks both archives, and
serves the installed wheel's index and referenced assets from outside the
source tree.

All of that reduces some supply-chain risk. None of it proves a dependency is
safe.

The release wheel declares compatible dependency ranges. A later installer may
pick other versions in those ranges, so the audit covers the release resolution,
not every future one.

ClearScout is not on a package index. Releases are published on GitHub. The
release workflow attaches a wheel, a source distribution, and `SHA256SUMS` to
the GitHub Release. Build-provenance attestations for the wheel and source
distribution go to GitHub's attestation store, not the release assets. Direct
Git installation is not supported, because a source checkout lacks the
production web client. Review a release before pointing the tool at sensitive
data.
