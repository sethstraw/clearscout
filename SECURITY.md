# Security and privacy

TailorCV holds API credentials and your career history, so please read this
before you point it at a real CV.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting. If that is not available to you,
open an issue asking for a private contact method, and leave out secrets,
personal data, and exploit details.

When you write it up, include the version, your operating system, a minimal
reproduction, what the impact is, and any workaround you found. Ordinary bugs
and bad resume output are fine as public issues once you have taken your own
information out.

Security fixes land on `main` and ship in the next release. This is an early
project with a single maintainer, so there is no promise of a fast response or
of formal support.

## What leaves your computer

TailorCV honors robots.txt and identifies itself with its own user agent. Each
user is responsible for following the terms of the sites they read with it.

During a live request, TailorCV sends the material needed for each model stage
to the Anthropic or OpenAI API you selected. This can include the text extracted
from an imported CV, job-description text, CV evidence, personal rules, and
previous stage output. Importing a screenshot also sends the image to the
provider for transcription. When the source is a URL, TailorCV attempts the read
only through its shared address, robots.txt, redirect, and rate-limit policy.
robots.txt decides every read except a posting URL you enter. That is a URL
argument at the CLI, or a link you add with the add-a-posting action. Its check
while adding and its first content read proceed past a disallow aimed at all
crawlers. A robots group that names TailorCV still refuses them. Board and
listing reads, later refreshes, and every other read of a stored URL honor a
robots disallow.
The URL may still be stored as the posting's identifier when that policy refuses
the read. Text supplied by paste, image, or PDF remains marked as user supplied
rather than being recorded with a fabricated fetch time or HTTP status.

Other workflows reach further than that. Guided search is queued as a durable
task and sends the bounded candidate record and your saved search parameters to
the configured provider, which may run its own server-side web searches before
answering. TailorCV then reads only the URLs those searches surfaced, under the
shared policy; a URL the model wrote with no search behind it is not read.

Cover-letter drafting reads the employer's saved careers page and one same-host
page labeled as an about page. Those reads use the shared policy, do not follow
redirects, and keep only bounded text.

A board scan is queued as a durable task and executed by the worker, so
requests leave after the queuing request returns. For each enabled source it
calls the published listing API named by that source. A URL with no
recognized adapter is fetched once under the shared policy. If the page names a
board, openings come from that platform's API. If that API refuses automated
access, the page already read is used instead. Otherwise TailorCV reads openings
from the site itself, in order:

1. JobPosting markup the page declares about itself.
2. Posting pages its sitemaps list, up to 40 per read.
3. Posting pages the career page links to.

A posting whose static read yields no body can then be loaded in a local
headless Chromium browser. That render runs on your machine against the
third-party page, under the same fetch policy for each request. A scan sends no
CV text, posting text, or search parameters. A scan runs when you start one, or
on the workspace's optional schedule, and a scheduled scan runs only while the
local worker is running. The
[validation status](docs/validation.md) separates parser coverage from live
board observations.

The Cornerstone reader sends one credential: the anonymous bearer token the
public career-site page issues every visitor in its csod.context bootstrap.
TailorCV sends it only to the Cornerstone job-search API that the bootstrap
names, takes a fresh token from the page on each read or resume, and does not
store it or send it with any other request.

Some operators publish a condition on reading their listings. A source records
that condition and the address it was published at, and switching the source on
states the condition back to you first. TailorCV records and shows it; it does
not check that you have met it.

Refreshing model prices, when you ask for it, downloads the published
genai-prices data file from `raw.githubusercontent.com`. It sends nothing of
yours, and a failed download leaves the prices already in force.

Reconciling OpenAI spend, when you ask for it, reads your organization's billed
cost for the window you chose from OpenAI's organization costs API. It needs an
organization admin key, described below, and sends only that key and the
window.

TailorCV never submits an application or sends anything to an employer. What it
cannot do is control your provider's logging, retention, training, data
residency, or account policies. If you are dealing with confidential, regulated,
or third-party information, read their current terms and work out your own
obligations first.

## API keys

A key can come from a one-run command-line option, the provider's environment
variable, or your system keyring. It never goes into a settings file, and
diagnostics only ever tell you where a credential came from, not what it is.

Spend reconciliation uses a separate, optional OpenAI organization admin key,
read from `OPENAI_ADMIN_KEY` or the keyring entry `openai-admin`. Without it,
billing reads are unavailable and nothing else changes.

Try not to put a key straight into your shell history, and do not paste one into
an issue, a report, a log, a test fixture, or a cassette. If you think one has
been exposed, revoke it with the provider and make a new one.

## Files stored locally

Personal settings, import reports, cached responses, and output packages can
contain names, contact details, work authorization, employment history,
unpublished material, source-line provenance, and text derived from the full
CV. TailorCV attempts to create private settings and cache files on systems that
support Unix permissions, but users remain responsible for the destination
chosen for imported CVs and reports, device security, backups, cloud
synchronization, and repository status.

The default `tailorcv.toml`, `CV_Master.md`, `Import_Report.md`, and common run
directories are ignored by this repository. Custom names and destinations are
not. Check `git status` before publishing changes. Do not assume an ignored file
is encrypted or excluded from other backup and sync tools.

The durable database and workspace artifacts default to `TailorCV` inside the
operating system's documents directory, resolved through `platformdirs`. File
pipeline inputs and `--out` packages live wherever you point them. Raw
provider-response caches default to the operating system cache directory under
`tailorcv/responses`, outside the Documents data root. Delete and back up those
locations separately unless you have configured them onto the same private
storage boundary.

SQLite files are restricted to the owner where Unix permissions are available
and use WAL mode. The API does not create or migrate storage on startup and
refuses non-loopback serving. Browser responses use no-store caching, a
same-origin content security policy, frame denial, and referrer suppression.

The database stores structured outcomes and provenance, and the full request
and response of every model call. A request carries the prompt, which carries
the CV evidence the run selected, so the database holds that text. It never
holds provider credentials. Task errors are bounded before they are persisted.
The web API reports credential presence only; it never returns credential
content. Retention and export boundaries are documented in
[docs/privacy-and-retention.md](docs/privacy-and-retention.md).

## Local web threat model

The web application protects a single user on one machine from accidental
network exposure, hostile DNS rebinding or Host headers, cross-origin browser
writes, unsafe inline rendering, oversized uploads, and disclosure through
ordinary browser caches or framing. The server accepts only `localhost`,
`127.0.0.1`, or `[::1]` Host authorities (with valid ports). Browser-originated
`POST`, `PUT`, `PATCH`, and `DELETE` requests must carry an Origin whose scheme,
host, and normalized port exactly match the request Host. A missing Origin is
allowed for documented local non-browser clients.

Every response, including an early Host or Origin rejection, keeps the
same-origin content security policy, `no-store`, frame denial, referrer
suppression, content-type protection, and a bounded correlation identifier.
Uploads are size-bounded while streaming. File downloads use sanitized names;
untrusted HTML and unknown media are attachments rather than inline content.

This is not authentication and does not defend against malware or another local
user already able to read the account's files, connect to loopback, control the
browser, or execute code as the user. Do not run TailorCV on a shared or hostile
account, expose it through a proxy, change the bind address, or weaken these
checks as a substitute for a designed authenticated deployment.

## Job descriptions are untrusted input

A posting can contain instructions intended to manipulate a language model.
TailorCV separates extraction from drafting, bounds the text that crosses later
stages, restricts evidence identifiers, and runs deterministic checks. These
measures reduce risk but do not establish complete prompt-injection resistance.

Please report a case where posting text changes instructions, exposes unrelated
data, causes an unexpected network or file action, bypasses evidence rules, or
appears in output as an unsupported claim.

## CV files are untrusted input too

Import reads local text, images, PDFs, and the XML inside `.docx` archives.
Malformed files should fail rather than be treated as trustworthy. Model-facing
source text is escaped and identified as untrusted data, but prompt-injection
defenses and provenance checks are not a sandbox or a malware scanner.

Import reports repeat source text and dropped lines. Remove personal information
before attaching them to an issue. A passing import means the configured text
checks passed; it does not prove that a document was safe, that extraction saw
every visual element, or that the model assigned every line to the correct role.

## Recorded API responses

Recorded cassettes remove authorization headers and key-shaped values, and tests
check for those patterns. They can still contain the real CV, job description,
model output, request metadata, or other identifying information. Use synthetic
data for public recordings whenever possible and inspect every cassette before
sharing it.

## Dependencies and releases

CI installs exactly what the repository lockfile says and fails if the lockfile
and `pyproject.toml` disagree. The gate audits that installed environment
against the Python Packaging Advisory Database, audits the hash-pinned
Linux/Windows packages that environment markers omit from the current host, and
refuses a dependency tree carrying a license this tool cannot ship under. The
web production tree has a separate version-bound advisory gate. CI also builds
the production web client, puts it into the wheel and source distribution,
checks both archives, and serves the installed wheel's index and referenced
assets from outside the source tree. All of that reduces some supply-chain risk.
None of it proves a dependency is safe.

The release wheel declares compatible dependency ranges. A later installer may
resolve different versions within those ranges, so the repository audit is a
review of the release resolution rather than a promise about every future
resolver outcome.

TailorCV is not published to a package index. Releases are published on GitHub.
The release workflow attaches a wheel, a source distribution, and `SHA256SUMS`
to the GitHub Release. It records build-provenance attestations for the wheel
and source distribution in GitHub's attestation store, not as release assets. Direct Git installation is not
supported because a source checkout does not carry the production web client.
Review a release before pointing the tool at sensitive data.
