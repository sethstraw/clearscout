# Published policy decides reads; URLs remain identifiers

Status: accepted

## Decision

Every posting read goes through the shared fetch policy. That includes
robots.txt, rate limits, redirect checks, and address validation. No hostname is
allowed or refused by a special-case list. A site's published policy is the
reason a read proceeds or stops. Bulk crawling, board scans, and lead freshness
probes all honor a robots disallow with no exception.

## The single posting read

robots.txt decides every read except one: a posting URL a person enters. That is
the URL argument of the CLI commands (`run`, `screen`, and `batch`), and, for the
local API action that adds a posting link, the publisher check made while adding
or before adding and the first content read of the row that add staged. The
server decides that from the row alone: the add's staging reason, which a later
board relisting keeps, and no content read yet. Any refresh path that takes that
first read gets the exemption; no caller flag grants it. Such a read opens a disallowed
address once, the way a person opening that link would, and may go to the page
or to the publisher API that states it. The page is then classified. A listing
or search page it lands on is recorded as unresolved, not staged.

Every other read is a crawl and honors a robots disallow: board scans and their
continuations, refresh of pending rows and tracked postings, content resolution
of queued rows, posting checks of held postings, evaluation reads of stored
rows, guided campaign captures, relevance-review preparation, assisted
recovery, and the rendered browser tier. Any read after a person-added row's
first content read, including its next refresh, is a crawl.

The exempt read is refused in three cases: a robots group names ClearScout's user
agent, or its former name `tailorcv`, explicitly; robots.txt cannot be read; or
the page answers 401 or 403. The
exemption follows the whole redirect chain the client itself follows, up to the
redirect limit. Each hop still resolves a public address and loads that host's
own robots, and still refuses a group naming either agent. An exempt
read is never retried on Retry-After. The precheck a posting read runs before
its request applies the same rule as the request.

A robots refusal is a policy refusal, not a publisher that declined to answer.
A refresh reports it as a failed read with the robots reason. It does not count
it as unverified.

A response reached through an exempt hop keeps the `single_posting_read` basis.
That holds even when a later hop lands on a page robots allows.

## Consequences

The basis a read proceeded under, `allowed` or `single_posting_read`, is
recorded in three places: pending content attempts, captured campaign URLs, and
posting observations. The pending queue and the posting reader show when a body
came from a single page read. An evaluation committed directly over the local
API states its own posting-body provenance, basis included. It does the same
for its fetch time and extraction method.

A URL still identifies a posting when policy refuses to fetch its body. The
ledger accepts and normalizes that URL. The posting text may come from a paste,
image, or PDF supplied by the user. Observation provenance keeps those sources
distinct. A user-supplied body has its own extraction method. It has no
fabricated fetch time or HTTP status.
