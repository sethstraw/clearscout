# How TailorCV works

Notes for whoever maintains this, me included. [README.md](README.md) is the
public front door. [SECURITY.md](SECURITY.md) owns the threat boundary.
[docs/truth-and-provenance.md](docs/truth-and-provenance.md) owns the evidence
model. This file explains how the components hold those rules together: package
direction, durable state, generated contracts, provider calls, prompts, and
artifacts.

There is no autonomous agent here. The workflows are fixed. Python decides which
stage runs next, and the model is used for bounded interpretation and writing.
Assembly, checks, reporting, and writing files are all ordinary code.

| Step    | Main code                                  | Purpose                                                     |
| ------- | ------------------------------------------ | ----------------------------------------------------------- |
| Ingest  | `infrastructure/web_fetch/ingest/`         | Read a URL, text file, PDF, or image                        |
| Extract | `application/tailoring/stages/jobspec.py`  | Turn the posting into structured requirements               |
| Read    | `application/tailoring/fit/`               | Derive the same facts from the raw text by rule, or abstain |
| Match   | `application/tailoring/stages/match.py`    | Link each requirement to CV evidence or a gap               |
| Select  | `application/tailoring/stages/select.py`   | Rank supported evidence within the length budget            |
| Draft   | `application/tailoring/stages/draft.py`    | Draft prose from selected evidence, assemble Markdown       |
| Verify  | `application/tailoring/stages/verify/`     | Run deterministic checks and the model critic               |
| Review  | `application/tailoring/changes/`           | Record every proposed line, its sources, and a digest       |
| Approve | `application/tailoring/approval.py`        | Apply a reviewer's decisions, or refuse them                |
| Report  | `application/tailoring/pipeline/report.py` | Write the review package and provenance table               |

Two commands sit beside that pipeline, not inside it.
`application/tailoring/batch/` runs the screening steps once per posting and
compares the results. `application/tailoring/elicit/` asks the person about the
gaps and can add what they confirm to the master CV. Neither drafts anything.
Elicitation makes no model call at all.

`application/master_cv/` is the shared intake boundary for CLI and HTTP
onboarding. It adopts already-conforming Markdown without a model call. Every
other supported source goes through
`application.tailoring.pipeline.import_cv`, which keeps its numbered citations,
authenticity check, loss check, and audit report. Only a passing canonical
document is registered as an immutable master-CV version.

`application/tailoring/pipeline/` connects the steps, and its bounded rewrite
loop is shared. The resume and the cover letter are both drafted, checked, and
rewritten within one attempt budget. Both are written at the end whether or not
they passed. `screen` stops after matching; `run` continues through the rest.
Provider code lives under `infrastructure/providers/`. Prompts are versioned
files under `prompts/` that ship with the package, documented in
[prompts/README.md](prompts/README.md).

## Borrowed patterns, and the ones left out

The model work follows published patterns. They were picked as much for what
they rule out as for what they add.

Anthropic's
[Building effective agents](https://www.anthropic.com/engineering/building-effective-agents)
separates workflows, where code decides the path, from agents, which direct
themselves. It recommends the simplest one that works. TailorCV is a workflow.
`pipeline/service.py` calls ingest, extraction, the deterministic reading,
matching, and selection in a fixed order, and each stage reads the structured
output of the one before it. That is prompt chaining in the same guide's terms.

Drafting is an evaluator loop in the
[Self-Refine](https://arxiv.org/abs/2303.17651) shape: draft, check, rewrite.
`pipeline/rewrite.py` runs the deterministic checks first. The model critic runs
only when those pass and the draft is risky. The loop stops on a pass, on a
repeated failure, or when its attempt budget is spent.

A draft is risky in three cases: it is a rewrite, its keyword coverage is below
`critic_coverage_floor`, or the share of answered requirements resting on
transferable or stated evidence, or on tailoring-only evidence worded as taking
part in the work (`PARTICIPATION` in `stages/context.py`), is above
`critic_transferable_ceiling`. A critic on every passing attempt costs about a
quarter of an application and runs most often on the drafts with least to find. Both thresholds are settings, because
where they fall is a quality judgment measurement is expected to move. A draft
that passes without a critic is reported as unreviewed, not as approved.

Citation is a contract, not a courtesy. Response schemas accept only the
evidence identifiers that exist for the run, and verification checks provenance
and numbers. [Gao et al.](https://arxiv.org/abs/2305.14627) found that even
strong models lack complete citation support about half the time on one
long-form benchmark when citing is left to them. Here it is not left to them.

Abstention is an answer. The deterministic reader returns `not stated` rather
than guessing (`fit/lexicon.py`), and extraction is told not to infer a
requirement the posting does not state. [Wen et al.](https://arxiv.org/abs/2407.18418)
survey abstention as one of the main ways to cut false output.

Every provider response is a schema with extra fields forbidden, so a reply
either fits the shape or fails loudly. A condition a rule can read is never sent
for a paid opinion (`discovery/relevance_review_preparation.py`). Postings and
CVs are treated as untrusted data, in line with
[OWASP LLM01](https://genai.owasp.org/llmrisk/llm01-prompt-injection/) and
[Greshake et al.](https://arxiv.org/abs/2302.12173). The limits of that are in
[Untrusted input](#untrusted-input).

Left out on purpose. Nothing routes: stages map to model tiers in configuration,
and no model decides which path an input takes. Batch screening reads postings
one at a time, not in parallel model calls. No model judge scores the
evaluations. And the critic shares the drafter's tier by default, so it can
share the drafter's blind spots; [Zheng et al.](https://arxiv.org/abs/2306.05685)
document that model judges favor output like their own.

## Durable application architecture

The bounded pipeline sits inside a layered package map:

```text
bootstrap     --> {delivery, application, infrastructure, domain, shared}
delivery      --> {application, infrastructure, domain, shared}
application   <-> infrastructure
application   --> {domain, shared}
infrastructure --> {domain, shared}
domain        --> shared
delivery.cli  --> bootstrap (entry point, setup, and composed processes only)
```

Application services coordinate concrete local infrastructure. Infrastructure
adapters use application-owned configuration and contracts. Neither side may
reach delivery or bootstrap. Three CLI modules reach bootstrap:
`delivery/cli/__main__.py` loads the composed Typer app, `workspace.py` runs the
setup flow, and `runtime.py` starts a composed server or worker. None of them
assembles a process itself, and an Import Linter contract names the bootstrap
modules each of the three may import, and no others.

- `domain/` owns framework-free identity, vocabulary, and invariants.
- `application/` owns capability policy and orchestration: master CV,
  discovery, job search, tailoring, documents, workspaces, tasks, and
  portability.
- `infrastructure/` owns persistence, artifact storage, providers, and web
  fetching.
- `delivery/` owns typed HTTP routers and CLI commands.
- `bootstrap/` composes the FastAPI app, Typer app, worker, and setup flow.
- `shared/` contains only framework-neutral costs, errors, base models, result
  primitives, and text normalization.

Import Linter holds these directions. It also keeps deterministic tailoring
stages from reaching services, persistence, or delivery. The task package owns
its models, payloads, execution protocols, registry, and durable queue.
`bootstrap/worker.py` binds task types to handlers.

Three capabilities keep one stable service in front of smaller owners.
`TaskQueueService` is the entry point for tasks: commands own state changes,
leases own worker coordination, the event log owns sequence numbers, and queries
shape records. An event is appended inside the transaction that changes the
task. `DocumentService` sits in front of `DocumentVersionCommands`,
`DocumentQueries`, and `DocumentArtifactPromotion`. `MasterCvService` sits in
front of `MasterCvIntake`, `MasterCvVersionCommands`, `MasterCvArtifacts`, and
`MasterCvQueries`. Board scanning splits the same way: `SourceScanService` owns
the scan command, and `SourceScanQueries` owns bounded reads of its history.

The workspace routers are split by concern and composed into one versioned API.
An evaluation's prompt lineage is its `run_prompt_versions` rows plus the prompt
fingerprint in the artifact's provenance.

`application/workspaces/prompt_library.py` resolves packaged prompt defaults
against immutable workspace overrides. The prompt files do not become database
seed data.

Typer and FastAPI call the same services. The API does not invoke CLI commands,
and the SPA cannot bypass server-side validation. The OpenAPI schema generates
the TypeScript contract used by the client.

One local worker leases database tasks and persists every stage as an immutable
event. SSE is a view over those events, not an ephemeral progress channel. What
the schema and the connection guarantee is in
[docs/contributing/database-and-migrations.md](docs/contributing/database-and-migrations.md). There is no
distributed-worker claim here.

A board scan reads a few boards at once, bounded by `SCAN_LANES`. It writes
every result from the scanning thread, so one writer faces SQLite. Politeness is
not the caller's to keep. `PoliteHttpClient` holds a lock per host, so boards
sharing a host still take their turns while different hosts overlap.

Board continuations run across tasks. The worker runs other tasks one at a time,
and runs `source.continue` tasks on up to `TAILORCV_CONTINUATION_SLOTS` slots,
four by default. The claim skips a continuation whose board host a slot is
already reading, and a slot that finishes claims the next one itself. That guard
keys only on the board source's host, not on the handoff hosts a chunk may also
read, so per-host politeness rests on `PoliteHttpClient`'s per-host lock, which
every slot shares because they run in one process with one fetch client.

Several slots write at once. Inserting staged rows and appending structured
hold-outs take one turn on `DiscoveryStaging`, which the board scan shares, and
a chunk waits for that turn no longer than its time limit; one that runs out is
re-armed without charging its links. Everything else a chunk does runs outside
the turn: its publisher reads, Workday detail reads, tracked-job refreshes, and
automatic content resolution, which reads bodies on its own pool of four lanes
and stores them as they arrive. Those writes, and those of other inline tasks
such as a guided campaign, meet at SQLite, where the connection's busy timeout
absorbs them. A worker stopped with Ctrl-C stops new claims and waits for running
slots before it withdraws its presence and closes the fetch client. `tailorcv stop`
sends SIGTERM, which also drains running slots. A worker that outlasts the stop
wait is killed only when every task it is running is in `KILL_SAFE_TASK_TYPES`,
whose work lease recovery resumes; paid handlers file their spend on exit, so
they are never killed.

One worker per database is enforced, not assumed. A worker that announces
itself is refused while another one's presence is live. So is any worker that
tries to claim. Either may proceed once that presence expires. A one-shot run
announces nothing, so two of those are not refused. What protects them is the
claim itself. It is a compare-and-set against the state it read, because SQLite
emits no row lock. The API and the worker both append to one task. An event
sequence another writer has taken is retried inside a savepoint.

Artifact bytes live outside relational storage. `LocalArtifactStore` performs
private staged atomic writes, enforces resolved root boundaries, and verifies
stored SHA-256 and size. It is the only artifact backend. Database commit and
file promotion use explicit finalize, compensation, and reconciliation paths.
There is no claim of cross-system transactions.

Historical facts are protected twice. Application code offers append and commit
operations. Database triggers reject update or delete on the history tables,
which
[database-and-migrations.md](docs/contributing/database-and-migrations.md#what-the-schema-guarantees)
names.
Current application state is a query over history. Corrections refer to the
event they supersede and never erase it.

Prompt edits follow that same boundary through `PromptLibraryService`. A prompt
with no workspace edit reads directly from its packaged file. Saving, reverting,
or adopting a changed packaged default appends a version. Reset removes the
override from the effective view without rewriting the shipped text.

The decisions behind these boundaries are in
[docs/architecture-decisions/README.md](docs/architecture-decisions/README.md). Operational
details are in [docs/contributing/database-and-migrations.md](docs/contributing/database-and-migrations.md),
[docs/storage-and-portability.md](docs/storage-and-portability.md), and
[docs/privacy-and-retention.md](docs/privacy-and-retention.md). What these
layers do from a user's seat is in [docs/user-guide.md](docs/user-guide.md).
What follows is why the seams are where they are.

### One seed and one workspace authority

`tailorcv.toml` is what a file-based command reads, because that run has no
workspace selected. Creating a workspace copies its path-free run settings,
job-search parameters, and configured guardrails into the database once. From
then on, workspace operations read the database. The browser edits the
versioned parameter and guardrail chains. A later file edit cannot silently
replace a browser edit.

Each versioned document is one JSON column, not normalized tables. It is read
and written whole, validated by one model that already owns every rule about its
shape, and never queried by field. Normalizing it would buy a join nobody needs.
It would also restate that model's validation in DDL, where the two would
eventually disagree.

Writes append and supersede, for the same reason evaluations do. A decision
made six weeks ago was made against the parameters of six weeks ago. The API
carries an expected version. Two tabs editing one form should produce a refusal,
not a last-writer result whose loser left no record.

`LearningLoopMetricsService` in `application/workspaces/` is the read model
behind `GET /api/v1/workspaces/{workspace_id}/learning-loop-metrics`. It counts
three signals from durable rows:

- how many proposed identities are re-proposed after a prior rejection,
- master-CV proposal acceptance, and
- whether the title or location of a top-ranked pending discovery carries a
  missed requirement from the current CV version.

Each signal is a numerator and a denominator, never a rate. A zero denominator
crosses the wire as absence of evidence, not as a divide-by-zero on the server.

### A scoring policy is content, not a setting

`score_weight_sets` and `score_weight_components` are immutable. Triggers refuse
UPDATE and DELETE on them. An evaluation stores the policy identifier it was
scored under. Editing a policy in place would rewrite what a recorded decision
meant. So authoring appends instead of editing, and a trigger enforces it where
a convention would not.

Weights are content-addressed with a digest unique across the whole database.
Identical weights are one policy, not two that happen to agree. Authoring a
duplicate is a conflict naming the existing policy, not a constraint violation
the user cannot read.

The commit path and the authoring path share one arithmetic boundary through
`_resolve_weight_set` and `_validate_weights`. A set that could not have scored
an evaluation cannot be written in the first place.

`EvaluationScores.overall` excludes a zero-weight component from the composite.
It does not multiply it by zero, and the two are not the same. Multiplying by
zero would still require the component to be stated. An abstention on a
component nobody weights would then suppress a total the weighted components can
state perfectly well. Exclusion is what lets the shipped policy drop evidence
from the sum without also making it a required input.

### An edit to the master CV cannot reach an evaluation

`MatchEvidence` snapshots every citation with its own text, character span, and
artifact identifier. `JobEvaluation` pins `master_cv_version_id`. A committed
evaluation reads the version it was made against, and no later edit can corrupt
it. An edit can leave the current CV disagreeing with a claim an older
evaluation still makes. That is drift, not corruption. It is worth reporting,
not preventing.

`application/master_cv/anchoring.py` answers that question. It re-anchors each
stored citation against the proposed text. The answer is graded, not boolean:
found or not, changed or not, and material or not. Material means a figure or a
negation moved. The report is sorted material first and capped, and the omitted
count travels with it. A truncated list that does not say it was truncated is
worse than no list.

The web save path parses the edited Markdown before doing anything else. An
edit that breaks the canonical contract fails on a parse error. It does not fall
through to standardization and a paid provider call.

### Archiving is forced, not preferred

`application_events` is immutable and its triggers refuse DELETE. An
application's history cannot be removed. Deleting the parent would either fail
on the foreign key or strand the events. Retiring an application is therefore
archiving, and the same shape holds for jobs and workspace records. `archived_at`
is set, children are preserved, and every active selector filters on it.
Archiving is also the only reversible option. Deleting immutable history could
never be.

The first status event creates an application row. The evaluation commit does
not. Reading a posting is not applying to it. The ledger's status expression
already coalesces a missing application to not applied. For a job nobody has
acted on, that is the truthful answer, not a placeholder standing in for a
record.

### Discovery becomes a job through the evaluation transaction

`discovery.evaluate` is the bridge between the mutable discovery queue and the
immutable job ledger. For a pending discovery it fetches the posting through the
shared polite fetch policy and runs the existing screening pipeline. It then
translates the structured job specification, deterministic reading, CV matches,
and cost ledger into an `EvaluationCommit` and calls
`job_search.commit_evaluation`. The evaluation commit is the one place that
constructs a `JobPosting`.

The pending row is archived inside that same database transaction, after the
evaluation completeness check. A task cannot report a drained queue while
leaving no ledger job. Nor can it commit a job while leaving the discovery
active.

For reruns, the task carries the existing posting's stable database identifier
into the same transaction. Jobs without a URL or requisition identifier are
still evaluated in place, and the new version supersedes the current one. The
browser's selection action queues one `discovery.evaluate_batch` task over the
selected rows.

There is no second durable `screen` operation. Re-evaluating a ledger job uses
`discovery.evaluate` and writes the next immutable evaluation version. A batch
comparison still analyzes each selected posting in isolation. Its members point
directly at the jobs it compared, not at duplicate screening rows. Any edited
prompt lineage belongs to the immutable batch run.

### Identifying a board is not scraping a page

`infrastructure/web_fetch/board_adapters/` reads published listing data
(JSON APIs, feeds, sitemaps, declared page data).
`application/discovery/board_scan.py` coordinates one sweep over them.
Discovery prefers platforms whose terms permit API reads. A known board URL
goes to its structured API before any rendered page is considered.
The shared fetch policy still applies robots checks, a bounded delay, and
per-host serialization to those API calls. A published interface is not an
invitation to send unmetered traffic.

Each platform is declared once, as a `BoardPlatform` in its own module under
`infrastructure/web_fetch/platforms/`. A declaration carries the platform's
board and job URL patterns, its canonical-root and identity rules, its
tenancy, its reader, and any continuation readers. The sweep's board lists
derive from the `PLATFORMS` registry, whose order is the detection order;
single-posting readers in `ingest/url/platform_readers.py`, listing facts, and discovery's
board-host tuples still keep their own per-platform entries. Adding a platform means writing
that module, adding it to `PLATFORMS`, appending its kind to `BoardKind`, whose
order is the wire vocabulary's, and giving it a record declaration under
`board_mappings/`. `test_adapter_completeness.py` names whatever else a new
kind still lacks.

The adapters own transport and paging only. What each board's record means is
declared once per board under `infrastructure/web_fetch/board_mappings/`. Both
the board sweep and the single-posting reader in `ingest/url/` map through
that one declaration, so the two reads cannot drift apart. A declaration holds
field paths and references to typed derivation functions and nothing
conditional.

A work arrangement is always a derivation, never a path. A publisher value
passes through its platform's rule. That includes the check that leaves a hybrid
or on-site value unresolved when the posting also offers remote work. A retained
posting body is digested, so the board sweep and the single-posting read compose
one identical body for a posting.

A page that names no board but declares its own `JobPosting` markup is read the
same way. The title, employer, places, and a passed expiry date it states become
the same staged facts. That is how a guided campaign's single read gets
structured fields from an ordinary careers page.

When a known career URL names no platform, the career page is fetched once. A
bounded prefix of its markup is searched for a board identifier. Only the
identifier is taken, and the openings still come from the platform's API. That
is the line the module is built around. A board that resolves neither way is
reported as needing a person. It is not parsed out of HTML.

The sweep list is filled two ways. Guided search can verify a readable board.
Evaluation can verify that a fetched posting belongs to one. Either stores the
address on the employer and makes it eligible for later free sweeps. The user
also maintains the list directly. A board can be added, its address corrected,
switched off, or retired. An address naming no known platform is kept, not
refused. An employer worth tracking stays visible while each sweep records why
it could not be read.

A source is on or off. There is no per-source schedule and no force flag, so one
control decides whether a source takes part in a sweep. Sources have their own
write route, because an address the product guessed wrong has to be correctable
without editing the database. Retiring archives the source, so the scans it
recorded stay readable. A source row reports what its address states, which
costs no request, and the outcome of its last scan, which is the evidence of
whether it can be read.

URL normalization is also the identity boundary between discovery and the job
ledger. Seeing an untracked URL again updates the mutable pending row with the
board's current title, location, and requisition identifier. Seeing a tracked
URL again does not disappear as a duplicate. It enters the same polite posting
refresh path as an explicit refresh and appends an observation and diff. The
board's title and location serve as current structured hints. Other observation
facts are read again from explicit posting text. They become unknown when the
new body does not state them. An old fact is never copied forward under a new
observation date. The pending queue has no retry counters or retry schedule.

`PostingRefreshService.refresh` accepts optional `client` and `guard`. Both may
be supplied while the workspace learning-loop policy has
`enable_dispute_reextraction` on. Then one `jobspec.extract` call re-reads the
freshly fetched body. It pairs its requirements to the workspace's active
extraction-dispute rulings by the same `derived_proposal_identity` hash the
derived-proposal inbox uses. A ruling becomes `REVERSED` only when the model's
cited quote appears in that body under the normalized-substring check.

The paid entry point is the `posting.refresh_with_reextraction` task type,
driven by `PostingRefreshWithReextractionHandler` in `bootstrap/worker.py`. The
free `posting.refresh` path calls the same service without a client or guard. It
makes no model call and can never mark a ruling reversed. Paid and free refreshes
are separate task types because they are separate contracts, and the SPA reads
whether a task is paid from the server's task registry, not from a payload flag.

Pairing by the derived-proposal hash, not by row, is what makes the comparison
possible. Requirement identifiers are positional within one observation, so a
disputed row cannot be looked up in a re-extraction of the next one. A whitespace
change does not lose a dispute, and a requirement restated in different words
does not resurrect an old one.

The separate `posting.refresh_with_rematch` task re-runs only the match stage
for active fit disputes against the current master CV. It reverses a ruling only
when the current match cites a durable CV entry key that the prior match for
that requirement did not cite. Repeating the old citations does not reverse a
fit dispute. It does not fetch or re-extract the posting. It does not write a
new evaluation.

A sweep reads whichever boards are switched on. A failed posting read stays
visible as a task failure. The one retry below the task queue is the scan
frontier's: an unread board page stays pending until an automatic row has spent
`MAX_AUTOMATIC_ATTEMPTS`, three, in `source_continuation/limits.py`.

Coverage is uneven and the module says so where the difference lives, in the
comments on the fetchers that have never answered a live call. The test suite
reaches no network. It cannot distinguish a fetcher that works from one that
merely parses a payload somebody wrote by hand. Neither the code nor the
documentation is allowed to imply that it can.

### Hosted search proposes; local policy decides

Three separately named tasks divide this work, so what a run will do is legible
before it starts.

`application/discovery/search_vocabulary.py` derives CV-grounded vocabulary. It
sends one text request that requires CV citations and permits no posting or
board URLs in its response. It does not fan out, fetch sources, or review
pending discoveries. Accepted vocabulary feeds the separate free scan of
registered sources.

`discovery.relevance_review` reviews only the pending discoveries named in its
payload. It reviews every readable selected row, including a current review,
and persists every completed batch. The
queue's separate Review relevance action requires an explicit selection.

Relevance review is its own paid task so a lead from any origin, a free board
scan included, can be reviewed without paying for a campaign to reach it, and so
its cost is approved and reported apart from any search. Batches are an
execution detail, not a cap: the handler continues until every readable
candidate in the selection has a validated review or a named validation failure.
A review is current when it matches the same master-CV version, search parameter
set, source URL, and fetched body. Selecting it again can incur another charge.

Before each call the application assigns identifiers to exact source spans, and
the model may cite only those. A review is stored only after its posting
citations and CV evidence identifiers validate, and a later one replaces it when
the source, CV, or search-policy identity changes. The judgment is recall-first.
It compares responsibilities and capabilities before titles, keeps adjacent or
transferable work in front of a person, and treats a missing fact as
uncertainty. It may recommend `unlikely` only from a cited contradiction or
clearly unrelated work, and it never turns that into a disposition.
Deterministic screening remains the only owner of hard refusals.

`search.guided_campaign` runs a guided campaign. `guided_campaign.py` compiles
one coverage branch per title topic or focus bundle across every actionable
market. It groups branches into attempts. It digests the result into a manifest
that also pins the prompts, model, effort, and service tier. The paid approval
binds that manifest digest, and the preview and the worker compile the same one.

`guided_campaign_runner.py` dispatches each attempt as a background response.
`guided_campaign_ledger.py` owns every persisted transition. Each transition is
fenced by the owning task, so a stale lease cannot resume a campaign a newer
task owns. `guided_campaign_admission.py` stages only URLs the provider's own
search actions surfaced, after a local read under the fetch policy.
`guided_campaign_report.py` reports coverage, staged findings, refusals, and
cost.

The bounds size the plan. They never stop a planned attempt, and a search
overrun is a report warning. Provider usage and configured rates feed the
ledger. Unknown prices remain unknown. Some provider-set charges are absent
from usage. The separate organization billing comparison cannot attribute those
to a task.

`infrastructure/providers/client.py` owns caching, validation, and accounting.
Paid workflows use normal official SDK requests with no monetary reservation or
token-count preflight. The URL-policy rules below still govern every hosted
result and application-owned source read.

`shared/costs/prices.py` imports the supported models' rates from the pinned
genai-prices package's bundled catalog. No background pricing requests run. A
person can ask for a refresh, which fetches the publisher's current data and
reports which rates moved; a failed fetch keeps the rates in force. Workspace
overrides still take priority.
Model support stays explicit, because catalog name matching can be permissive.
The adapter rejects pricing tiers that the durable price shape cannot represent.

Recorded token costs use that imported policy. Above 272,000 input tokens, the
shipped OpenAI models use 2x input rates and 1.5x output rates, including cached
input. Output headroom does not trigger the threshold. Returned usage is priced
at each rate. The
[provider pricing notes](https://developers.openai.com/api/docs/models/gpt-6-sol)
state the threshold and multipliers. The threshold is strictly greater than
272,000, and the adapter enforces that boundary itself instead of taking the
catalog's. Service-tier adjustments and spend approvals remain application
responsibilities.

Every returned address is decided locally, because it came from a model. These
are refused unopened:

- a scheme that is not HTTP,
- an address instead of a hostname,
- a local name, and
- credentials in the URL.

A safe direct posting URL reaches only the pending queue. Evaluation must fetch
its contents before it can become a ledger job. A safe board lead is read by the
same board fetcher as a board check. Only listings returned by that board reach
the queue. A careers page that answers but exposes no supported board
representation is retained as a disabled source candidate for review. A
URL-policy refusal or failed fetch is discarded and never becomes source state.

A readable lead is retained as a disabled employer board once at least one of
its listings stages; a board that stages nothing is reported unresolved and is
not registered. The person reviews a retained board and switches on Swept
before a later scan reads it. Model-proposed sources are separate rows. They never overwrite a
person-maintained source for the employer.

Title exclusions are enforced in Python against the normalized form. They are
not asked for in the prompt. A stated refusal that a model can talk its way past
is not a refusal. Under strict title matching nothing synthesized joins the
gate.

Up to eight alternate titles whose citations survived validation are stored as
proposals. Each keeps its cited master-CV entry identifiers and the exact
master-CV version that contained them. Later free sweeps read only proposals the
user accepted, and only for the current CV version. A rejected title is kept so
the same name is not offered again. It does not count against the eight. The
model widens where the tool looks. It never widens what the tool claims about
the person, and it does not promote its own suggestion into the profile. The
vocabulary run that proposed a title still uses it for its own queries, since
that call is already spent. What waits for a ruling is the durable promotion
every later search reads.

The scan is a worker task with a cancellation and progress context. A workspace
scan schedule (`application/workspaces/scan_schedule.py`) has the worker queue
that task when a run is due; runs missed while the worker was down produce one
catch-up scan, not one per missed occurrence. Its title filter is read from
the search parameters in force at scan time. An exclusion is honored in every
mode. A target list narrows in both modes and differs in how far. `strict`
asks for the stated title inside the posting's own. `guided` asks for more
than two-thirds of one stated title's meaningful words. A list nobody stated
narrows nothing.

### Credentials and unpriced calls cross the API boundary in one direction

The provider endpoints are not workspace-scoped. A keyring entry and a settings
file belong to the account, not to a ledger. The screen also has to work before
any workspace or database exists. A key is accepted once and the response
carries presence only. There is no read path.

The CLI's keyring store degrades to "not saved" and lets a terminal print the
reason. The HTTP path raises instead. A screen told the write succeeded would be
claiming something that did not happen. The backend's own message is passed on
with any echo of the key removed, since this code did not write that sentence.

Spend reporting draws the same kind of line around a number. A model call is
written with its cost record in one unit of work. So a call with no cost record
means the price was not configured for the model that served it. Any total that
would include one is withheld, not summed. That holds per stage and per model as
well as overall. An understated total is a wrong answer, and a withheld one is a
true one.

## Finding postings and reading sources

Discovery decides which names a search reads under, what reaches the review
queue, and what leaves it. Each rule below widens where the tool looks without
widening what it claims.

### Coverage is measured against a matrix

A count of titles proposed, leads fetched, or postings staged does not say
whether a search covered the search. A guided campaign plans and reports against
a coverage matrix: every name the run searches under crossed with every place
the person may work, built from their own parameters and never from a shipped
list. A staged opening covers the cell whose title shares most of its words and
whose place its location names. What is left is reported as uncovered.

Each branch of the plan is one title topic or focus bundle across every
actionable market. A title topic carries a floor query the model is asked to
run first; a focus bundle carries no floor query, and the model searches it
with its own queries. A branch counts as searched only when a query the
provider actually ran serves it: for a title topic, a captured query that names
one of its titles; for a focus bundle, a captured query the model's answer
attributes to it. A search the model reports but the provider did not run
counts for nothing. A protected branch family that no captured
query reached leaves the campaign incomplete, and the next preview lists
never-searched branches first. A processed attempt records how many job-detail and
job-page URLs the provider's actions surfaced that the model did not select, so
an attempt that staged nothing shows whether it found nothing or set postings
aside. Every attempt shares byte-identical system instructions, with its branch
count and search budget in the user prompt, so the provider can serve the
instructions from its cache.

The matrix counts three sets of names: the stated titles, the ones the run just
proposed, and the names accepted in the inbox. A board scan searches under the
accepted names too, so a matrix without them would report better coverage than
the person is getting. Under strict title matching neither the scan nor a guided
run uses them. The matrix is capped, and a run states how many cells it dropped
rather than reporting coverage of a shortened denominator.

Each target title may state a track, a leadership or a specialist reading of the
same career, and coverage is counted per track as well as in total. A senior
record is advertised under both kinds of name, and a run can answer well for one
and never reach the other while its total looks healthy. Tracks measure and gate
nothing. A title with no track is counted under `unlaned`, and `Lane` has no
member for it, because a value that can be set by accident cannot be told apart
from a real answer. No track is derived from a title's words. A search may state
one for a name it proposes, and it becomes durable when the name is accepted.

### Proposals wait for a ruling

A search proposes titles, kinds of employer, and capability and domain clusters,
and a person rules on all three in one inbox. Each keeps its own table and
reported shape, because each stores something different. Listing what is
pending, recording a ruling, and refusing a row from another workspace are one
implementation, since three copies would drift. Capabilities and domains share
one table with a kind per row, because they differ in what they name, not in
shape.

A kind of employer is a kind of organization, never one by name. It is cited to
the master-CV version it was read from. An accepted kind is stated to later runs
and never written into the saved search parameters, and an accepted title stays
out of `target_titles` for the same reason: a proposal is bound to a CV version
and the parameter set is not, so promotion would let it outlive the record that
earned it. The person's own kinds outrank a proposed one of the same name.
Nothing derives a kind from an industry or a size.

A capability is the craft and a domain is the subject matter. Each is a name
plus the terms a posting is read against. A title filter alone rates two
openings with unrelated work and one shared title word the same, and no posting
states a capability's name. A cluster with no terms still matches its own name,
so stating one is never worse than stating nothing. A boundary, work the person
does not want, ranks a posting down rather than hiding it. Hiding is what
`excluded_titles` is for.

### What reaches the queue, and what leaves it

Automatic admission keeps a board listing out of review only on a precise,
published fact, such as a structured location naming one place no saved location
accepts. A structured hybrid or on-site arrangement joins it only when the
location list is complete, nothing in the values or the body offers remote work,
and no saved location that may contain a named place accepts that arrangement:
for a named site, a saved location in its subdivision, or in its country when no
subdivision resolves; for a bare country or region, any saved location in that
country. A named site whose country does not resolve keeps the listing in front
of a person, since the reader cannot place it. A listing held out stays in the
screening ledger, where it can be promoted. A bare "in office" counts only beside a day
count in a sentence that requires it.

A guided campaign also holds out a page it fetched from the publisher when a
must-have statement in the body states a condition the person saved as
excluded, through the same gate relevance review applies. A board summary is
never read for this, because it is not the posting's own text.

Removing a listing already in the queue is stricter. Only an explicit conflict
in a verified, active publisher body does it, such as a stated attendance
commitment, and each such dismissal is an append-only event that can be
restored on its own.

A direct posting a guided campaign stages is also a reason to sweep that
employer's board, which costs a policy-permitted read and no model call. The
board address comes from the ledger, where a lead or an earlier sweep recorded
it. It is never derived by truncating the posting URL, because each
applicant-tracking platform lays out its paths differently. An employer with no
recorded board is not swept.

One opening advertised at several URLs is shown as one queue item only when the
publisher supplies the same non-empty requisition identifier under the same
employer identity. The other places are listed beside it, and answering it
answers them. The variants are linked, not merged, because one employer can
have two openings under one title and a merge would hide the second for good.
Boards that mint an identifier per advertisement are handled by a second linker:
rows whose retained bodies are byte-identical under the same employer and title
are one advertisement republished. That is the publisher's own text, not a
resemblance, and it applies only to a body verified as an active posting and
long enough to be useful. Only a row the queue currently shows can head a
family, and a later read that renames a row releases its family.

The same opening found on different sites is neither linked nor merged, because
an aggregator states an employer the program cannot vouch for.
`duplicate_suggestions` in `application/discovery/posting_identity.py` computes
a suggestion when the queue is read and stores it nowhere, and a person confirms
it through the ordinary duplicate dismissal. The guard that refuses to pay twice
for one posting never reads that suggestion. A guard that blocks a run must not
act on a resemblance.

An opening that leaves a board stays in the queue with the date it went and
sorts below openings still listed, so the disappearance is visible without
competing for attention. Absence means withdrawal only when the read was
complete, so every read carries that claim. A platform API that returns every
posting is complete. A page whose markup describes some jobs is not, and
neither is a paged board stopped at its page ceiling, or a large board's first
scan past the ceiling would retire live jobs. A staged row records the source
that listed it, and only that source's complete read may withdraw it. Matching
by employer name across the workspace would withdraw an opening a provider
search staged under an address the board does not list. A row no source staged
is never withdrawn.

A read across several listing pages is complete but not atomic: a board that
reorders between page fetches can drop an opening from every page it served.
So a miss on a multi-page read only marks the row and queues a content read of
its own page. The next complete read that also lacks it withdraws it, unless
that check found the page live. A single-page read withdraws on the first miss.

### Sources that are not one employer

A source is not always one employer. An aggregator carries many and names none,
so a source's `employer_id` is optional and the sweep reads `Source.url`. Each
posting from such a source keeps and displays the employer it declares. That
name is trusted for the free gates only when the page is a single-tenant board,
the declared name matches the page host, or the source itself names that
employer. Otherwise it stays an unverified hint, since `JobPosting` markup
vouches for the posting and not for the employer it names. A host registered to
spell another employer's name defeats the host match, and that limit is accepted
over keeping a list of trusted sites. The schema does not enforce one source per
employer. The code that creates sources does.

An aggregator adapter takes the newest listings up to one shared budget,
whatever page size its API uses, and always returns `complete=False`. A full
read would be hundreds of requests to one host for a product that wants only
what is new. The market moves while a read runs, so an adapter that paged to
exhaustion would still not claim completeness.

A new workspace starts with a short catalog of aggregators, each switched off.
An aggregator states nothing about who anyone should work for and an employer
board would, so the catalog holds no employers, and no ordering, industry, or
country in it expresses a preference. Naming a host in the catalog neither
allows nor refuses it. The fetch policy still decides every read.

Permission to read a source is often conditional: credit it, link back to the
posting, do not republish the listing. The condition is stored on the source in
the words of whoever recorded it, with the address it was published at, and the
browser states it back when the source is switched on. A catalog entry carries
its operator's published condition and passes the same gate. Nothing decides
whether the obligation was met. The record and the prompt are the whole feature.

### Reading a platform and its policy

Some career sites serve from the employer's own domain, with no hostname or
tenant path to match. Each such platform is recognized by a narrow fingerprint
of the fetched markup, defined in its platform module and registered in
`PLATFORMS` (`infrastructure/web_fetch/platforms/__init__.py`): for example
Phenom's `phApp.ddo` object with its site reference, or the `jibecdn.com` asset
host an iCIMS Jibe site loads. A page that merely names a vendor does not
match. Phenom and Jibe APIs, and an Avature portal on the employer's own
domain, answer on the host that served the page, so the adapter contract takes
the resolved address with the match, which matters when a vanity domain
redirects. A classic iCIMS site is different: its
`<label>-<tenant>.icims.com` host is matched directly, and its postings are
enumerated from the site's sitemap. `SourceResponse.platform` is derived
from the address alone, so these sources report no platform until a sweep
reads them.

A robots file answering with a 4xx other than 429 is no policy, and the read is
permitted. RFC 9309 section 2.3.1.3 classes that range as unavailable. A 429
stays refused beside the 5xx codes, because it withholds a policy rather than
stating that none exists. The rule names status codes, not hosts. Refusing on
every 4xx would make a whole supported platform report that it has no board
API.

## The truth boundary

The rule the whole design serves: a model may rephrase supported evidence, and
may not expand the record.

It is enforced by construction, not by instruction. The CV parser gives every
usable entry a content-derived identifier. Response schemas are built at runtime
as closed vocabularies over those identifiers. Matching may cite only
identifiers that exist. Drafting may cite only identifiers that selection
retained. A citation to something absent from the record cannot be expressed,
so it does not need to be caught. Employers, titles, dates, degrees, and
publication citations are copied from the parsed CV, not generated.

Drafted lines keep the identifiers they came from. Deterministic checks then
look for missing provenance, numbers absent from cited evidence, required or
forbidden wording, and structural problems. The critic handles only what code
settles badly: preservation, alignment, specificity, readability, and voice.

Cover letters have two closed evidence vocabularies. That is not an exception
to the rule. Candidate claims cite selected CV entry identifiers. Claims about
the role or employer cite identifiers from a bounded company-research block.

The stored posting is always present. A workspace tailoring task may also read
the employer's verified careers URL and one explicitly labeled same-host About
page. It reads them through the shared polite client, without following
redirects. Each page is bounded before it reaches the prompt. It is stored as a
run artifact with its URL and fetch method. If a fresh page cannot be read, the
newest stored version for that job is reused. A file-based CLI run has no
verified employer URL, so it uses the posting and does not invent one.

This makes some failures impossible and others easy to detect. It does not turn
model output into verified fact. That is why the report prints the provenance
table, and why human review is part of the product and not a disclaimer.

## Two readings of one posting, reported apart

`application/tailoring/fit/` works from the posting text and the parsed CV,
without a model. It answers the questions extraction also answers:

- what family and specialty of work this is,
- whether it states people management,
- what level, how many years, and what degree,
- where the work happens,
- what it says about the right to work,
- which blocks are the requirement sections, and
- which lines are must-have against preferred.

It runs in `screen` and in `run`, costs nothing, and reaches nothing.

It is given the raw posting, not the model's extraction of it. That is the whole
reason it is worth having. Two readings derived from the same source are two
readings. A rule reading of a model's summary would only measure the summary.

Half of what is derived also lives in the shape of the document, not in its
sentences, and extraction has already flattened that away. A line is preferred
because it sits under "Nice to have". A sentence is not a requirement because it
sits under "Benefits and legal".

**The two are printed separately and neither overrides the other.** There is no
merge, no precedence, and no average. Where they differ, the report names the
subject, quotes what each of them read, and stops. One of the two is wrong in
every such row. The program cannot tell which, and a person can usually settle
it in thirty seconds. Merging them would destroy exactly the information that
makes the row worth printing. Letting either win would assert a confidence
neither reading has earned. A disagreement is not a failure either. It is the
output.

**Abstention is the feature.** An analyzer that always answers is a worse
instrument than one that says "not stated" three times in ten. Those three are
the rows a person has to go and read the posting for.

Every answer is an `Answer`. It holds either a value with the text it was read
from, or no value with a note saying what was looked for. `Answer.conflict`
separates the two reasons for having no value, because they are different facts.
A posting that names no level has stated nothing. A posting whose title and
responsibilities describe different jobs has stated something that does not add
up. A model answering that field confidently is then worth arguing with.

**None of it is a score.** No percentage, no grade, no ranking, and no
prediction about an application. The program already has one number about a
posting, keyword coverage, and a documented rule against calling it an ATS
score. This holds the same line. The report section says in its own text that it
was derived by rules, not by a model, and is not a prediction.

The role taxonomy in `domain/taxonomy/data.py` is a Python table, not a data
file. The prompts ship as files because they are text written for a model,
versioned and fingerprinted as a suite. A lookup table consumed by pure
functions gets nothing from that. It would pay with a packaging entry, a
`check-wheel-contents` surface, a parse-error path at runtime, and filesystem
work inside a pure function. That is the same trade `shaping_fingerprint`
refuses (see run identity below). A table in `src/` is also already covered by
the agnosticism scan in `tests/loop/test_agnostic.py`. That scan reads
`src/**/*.py` and would silently skip a `.toml`.

Two rules keep that table from becoming one person's configuration file living
in `src/`. First, it covers more than seventy families across ten sectors. A nurse,
a designer, a financial analyst, and an electrician are all readable in it, and
the test asserts the spread instead of trusting it.

Second, a term is a compound phrase wherever the bare noun would be ambiguous.
"pipeline" is a sales word and a data word. So the table carries "sales
pipeline" and "data pipeline", and neither carries "pipeline". Fewer than ten of
its more than five hundred terms are single words, kept because they name one
thing in any sector. That
preference is most of what makes the reading abstain on the postings it should
abstain on.

What a shipped table cannot know is what a particular market calls things this
year. `[vocabulary]` in the settings file adds phrases and aliases without
touching the package. It can only add.

A found title that names a function with no person noun, such as "Revenue
Operations", reads at most as a near match against a title that names a person.
It stays reviewable without claiming a match. Person and function nouns are
recognized by English word formation, not by a list of fields. Titles that agree
on the work and differ in rung agree, titles naming different occupations
differ, and an identical title always matches. A title sharing a word and an
occupation with a searched one, in a family the taxonomy declares adjacent,
reaches the bounded review lane, not a match.

### Eligibility is not a low score

Relevance and eligibility are different questions, and a policy that mixed them
would rank a job somebody cannot take above one they can. `fit/gates.py` answers
eligibility on its own, from the deterministic reading and the person's own
parameters, so it costs nothing and runs before any model. Each gate stands
eligible, ineligible, or unknown. A posting that states no salary has neither
met a floor nor failed one, and the unknown conditions are the ones worth asking
about. Relocation and employment type have no gate, because nothing reads them
from the posting.

A residence rule refuses a listing only when a sentence requires the candidate
to live somewhere (must reside, live, be based, be located), the person has
saved at least one location, and every country the sentence names lies outside
the places those locations accept. A rule naming no country, such as "the same
country as the job", requires nothing, and country names come from the country
table in force. Work-authorization or sponsorship wording and a time-zone
requirement are not residence rules. The body screen and the relevance review read residence through this one
gate.

A person who marks the place they live is held to that place as well. A rule
naming only countries they neither live in nor will relocate to refuses, unless
its clause accepts work authorization or citizenship instead, or speaks of pay
or a time zone. A negated rule ("no need to be located in") is no rule.

### How a working arrangement is read

Hybrid means weekly attendance. A cadence rarer than weekly, per month or per
quarter, is remote work with visits and reads as remote. A stated share of the
working week in an office is hybrid however it is worded, and a day count is
compared only against the period it states. A posting naming remote alongside on
site reads as offering remote work, with the attendance kept in the evidence.
`REMOTE_REFUSED` covers wording that rules remote out, such as "not eligible for
remote work".

Remote named as an option or a benefit beside a sentence committing this role to
attend is a conflict, and it reads as no arrangement with both sentences kept.
Whose attendance a sentence states cannot be read reliably from wording, and an
unknown arrangement never refuses a posting where a wrong "on site" would. An
onboarding week, an optional visit, and a capped or softened attendance do not
commit. A role declared remote stands wherever the sentence sits.

The arrangement is read from every line, including the company description and
the benefits list that requirement extraction skips. Reading it through that
filter would drop a line carrying `Fully remote` beside `Flexible paid time
off`, and the arrangement with it.

### A verdict follows weighted fit

An evaluation's verdict, set in `tailoring/workflows/evaluation_policy.py` and
not by the rule reading, comes from the weighted share of the posting's
requirements the record answers: above an upper bound it pursues, below a lower one it refuses, and
between them it reports being unsure. A stated absolute condition the record
fails refuses outright, ahead of any score. An unmet essential is reported and
stops a pursue, but it does not decide the verdict alone. A posting stating
twenty to forty requirements almost always has one, so a rule keyed on it would
send most postings to the middle. A run that could not settle a must-have, or
answered more than one stated condition weakly, commits in neither direction.
The bounds are measured against adjudicated postings, not chosen. Weighted fit
feeds both the verdict and the scores the report shows, so the number a reader
sees is the number the decision used.

The shipped scoring policy covers all five components and gives evidence weight
zero. Evidence quality inside the overall would score a job higher because the
tool documented it better, and "is this a good job for me" is a different
question from "how sure is this assessment". Evidence keeps its own score, shown
beside the overall. A workspace that disagrees authors its own policy.

### Rulings on a requirement

A person rules on a requirement as `met`, `not_applicable`, `disputed_fit`, or
`disputed_extraction`. The two disputes select the paid stage that can revisit
the ruling: a fit dispute re-runs matching against the current master CV, and an
extraction dispute re-reads the current posting body. `reversed` is a fifth
value, set only when that stage finds the new evidence it requires. It hides the
requirement from the gaps view as `met` does, and stays separate because no
person marked the requirement met.

## Review does not widen the boundary

`application/tailoring/changes/` writes what drafting proposed.
`application/tailoring/approval.py` decides what a person may do about it. Both
live beside drafting and reporting, not in `application/tailoring/pipeline/`,
which composes stages. What a proposal is and what an approval may change are
facts about the draft, not about the order of operations.

The artifact holds:

- a line and its evidence identifiers,
- the exact master-CV text those identifiers name,
- the requirements it answers,
- whether it is new, reframed, preserved, or omitted,
- a rationale, and
- a digest.

The rationale is assembled in Python. Code already holds the requirement text,
the classification, the excerpts, and the words a line uses that its sources do
not. A sentence built from those can be recomputed. That is what lets the digest
prove one was not tampered with. A model-authored sentence could not be
recomputed. It would be an unverifiable claim inside the artifact whose whole
purpose is that its claims can be checked. It would also cost a schema field, a
prompt change, and a new prompt-suite fingerprint for something no reviewer
asked for.

The digest covers the schema version, the run identity from `RunIdentity` in
`application/tailoring/runs.py`, and the proposal itself. `RunIdentity.approval`
is the one field it leaves out. Proposals are what drafting produced before any
reviewer saw them. A digest that moved when an approval was applied could never
be checked against the approval that moved it. Everything else is in. A review
of one CV, posting, prompt suite, configuration, or release cannot be replayed
against another.

Approving is optional, and the guarantee is that it cannot widen what a resume
may say. A reviewer may reject any line the model wrote and may supply
replacement text. That text goes through `verify.check_supplied`, which calls
the same private checks the drafted text goes through instead of restating them.
It must cite identifiers that exist and were selected for this run. Every figure
in it must appear in its cited evidence. It must pass the wording, character,
forbidden-string, and protected-string rules. The document is then assembled and
checked again in full.

A reviewer may not touch a line the assembler copies instead of drafting.
Employers, titles, dates, education, and publication citations stay where they
came from.

A rejection removes and never substitutes. A section, a role block, or the
earlier-career block that loses every line is dropped with its heading, so no
heading stands over nothing. Rejecting every line of the experience section is
refused, not written. `RunIdentity` carries the approval's digest. A package
somebody reviewed is not returned by the reuse check for a run nobody did. Two
different approvals of one draft are two different runs.

Text the master CV does not support is still refused, and the refusal names the
route: `tailorcv discover`, described next. It is the one place the program
itself creates a new claim. Every other CV version holds text a person
imported or edited.

## Discovery is the one place a claim can be created

Everything above narrows what may be said. `application/tailoring/elicit/` is
the exception. It adds a sentence to the file every other stage treats as true.
So it is built the way the rest of the program is built. The failure is made
unrepresentable. Nobody is merely instructed not to cause it.

**It calls no model.** A question is one of eight templates and a subject. The
subject is a gap the matching stage already found or a phrase the person typed.
There is no prompt file, no new call, and no new prompt-suite fingerprint. A
model would write a better question. It would also become a second author of the
claim, at the one point in the program where the person is supposed to be the
only one.

**Three sources are refused by name.** They are the three ways a claim nobody
made would reach the CV: generated resume text, a model's suggestion, and the
job description. Two independent barriers stand in front of each. Both have to
hold, because either alone can be got around.

Every reply records where it came from, and only a person's is accepted. That
catches text that arrives labeled. Every significant word of a proposed claim
also has to appear in the replies. That catches text that does not arrive
labeled. Wording lifted from a draft, a model, or a posting fails on the words
it uses.

The posting is the dangerous one, because the question was built from it. A
posting asking for "owned Kubernetes in production" answered with "yes" is the
exact shape of the failure.

**The ladder is a rank, and the rank is only ever used to refuse.** Exposure,
use, support, evaluation, configuration, implementation, ownership, leadership,
deployment. Both the claim and the answers are read against the same word list.
A claim standing higher than the answers is refused with both phrases quoted. So
the disagreement is about two sentences, not about a verdict. Nothing moves a
claim down either. Somebody who says they owned a thing is not argued with. The
reading is lexical and says so. The level is confirmed explicitly, and the
phrase it was read from is printed beside it and recorded.

The ladder is not a strict ordering of difficulty, and its middle rungs sit
close together. That does not matter for what it is used for. The distance
between "sat in on the design reviews" and "owned it in production" is the
distance that gets people caught in an interview. Every plausible ordering of
the rungs agrees about that one.

**A figure in a claim must appear in a reply.** This is the fabrication that
would survive everything else in the program. By the time drafting sees a
number in the master CV, it is the source of truth. The numbers check
downstream would confirm it against itself.

**A gap is closed by written material and by nothing else.** Not by having been
asked about, and not by an answer whose proposed wording was then refused.

Material that survives all of that is pending, not evidence. Eight separate
confirmations are required, and there is no partial one:

- the exact wording,
- the metrics, including the absence of any,
- the ownership level,
- the scope,
- the evidence category,
- the job gaps it relates to,
- that its source is user-confirmed discovery, and
- what remains ambiguous.

Each is asked, and refusing any ends it. A single "yes" to a printed block is a
decision about that block's first line. The ownership level and the missing
metric are the two people skip.

The write is the last thing and the most dangerous, because it mutates a user
file. The exact insertion is printed in place before anything happens and
agreed to separately. The result is parsed before it replaces anything. The
change is refused unless every identifier the CV already carried still exists.
The new identifiers must be exactly the confirmed claims, each of the category
and under the heading it was confirmed for. The replacement itself is a write
to a temporary sibling followed by a rename. A reader sees the whole old file or
the whole new one.

A confirmed entry then gets an ordinary content-derived identifier from the
ordinary parser, because it is ordinary evidence once it is there.
`Discovery_Log.md` beside the CV is written first. It is the only record that
the entry came from a conversation. It carries the level, the metrics, the
scope, the gaps, the ambiguities, and the answers themselves.

## A batch is postings kept apart, then compared

`application/tailoring/batch/` runs the shipped screening once per posting and
does arithmetic over the results. Each posting has its own ingestion,
extraction, deterministic reading, evidence map, posting identity, and cache
entries. No object in the loop holds two at once. Nothing in the module imports
drafting, assembly, or the review artifact, and a test asserts that
structurally.

Two more tests measure what the module claims. No request sent while screening
one posting carries a phrase from another. No cache key is shared between two
postings. Cache keys are content-addressed. Two postings stating exactly the
same requirements against one CV would legitimately share a matching answer.
That is the key doing its job, not a leak. The answer depends on the
requirements and the CV and nothing else.

Whether two postings ask for the same thing is decided without a model. The same
sentence twice is one requirement. Otherwise they are joined by a term they both
use that the batch does not use everywhere. That is what finds "experience
running Kubernetes in production" against "hands-on with Kubernetes clusters".
The joining term is printed so a person can disagree with it.

Which words are noise is measured from the batch itself, not listed in the file.
"clinical" is a distinguishing term in one slate and the word every posting uses
in another.

This is deliberately not the wording overlap
`application/tailoring/stages/match.py` uses to pair a gate with the requirement
restating it. That threshold is right for two sentences from one document. It is
wrong across postings. "strong experience with widgets" and "strong experience
with sprockets" share three words in four and are two different requirements.

A gap's leverage is how many of the postings state it. That is a count of
documents. The recommendation is three words: pursue, cautious pursue, or skip.
Stated rules over the evidence map reach it:

- A stated absolute condition with no evidence at all is a skip.
- A condition answered by something weaker than direct evidence is a cautious
  pursue. So is an essential requirement the record does not answer.
- Anything else is a pursue.

Every recommendation carries the reasons it rests on. Each reason quotes the
posting or the record. There is no percentage, no grade, and no ranking
presented as a verdict. The reason is the same one that keeps keyword coverage
from being called an ATS score. A number attached to a job application is read
as a probability whatever it is labeled. The person's own priority orders the
report and never touches a recommendation.

The two features meet at one seam. A batch writes its ranked gaps into
`batch.json` as a shared discovery queue. `tailorcv discover --queue` reads the
subjects out of it. What crosses is a list of things to ask about. What the
posting said cannot become a claim. A claim has to survive the rules above, and
those are about the words the person typed.

`tailorcv import` applies the same idea to a different vocabulary. A Word file,
PDF, image, or text file is reduced to numbered lines. The structuring schema is
built over the line numbers in that document, so a field cannot cite a line that
is not there. The model may join wrapped lines, split packed fields, remove
conversion artifacts, and normalize dates. It may not write professional
content.

Three checks follow, named in `Import_Report.md`:

- `authentic`: each output word appears in the lines that field cited.
- `lossless`: every number and capitalized token reaches both a field citing its
  line and the converted document, or is recorded as a deliberate drop.
- `compliant`: the result parses as a master CV with the bullet count intact.

The names are shorthand, not proofs. They read extracted text, not page layout.
They cannot tell whether interleaved columns put text under the wrong role.

## Every stage sees the rules it is judged by

A stage checked against a rule must be given that rule. A check on a rule the
stage never saw fails the stage for something it could not know, and only a
run against real work tends to expose it. A critic was asked to justify a work authorization line it was never
shown. A drafter was failed by regular expression for phrases it had not been
given. A matcher was judged against distinctions that were never in its prompt.
When adding a check, the question is not whether the check is right. It is
whether the thing being checked could have known.

## What a model call is asked for

A stage builds a `RequestPlan`, never a vendor's request. The plan names three
parts:

- `instructions`, the stage's instructions,
- `task`, the one dynamic part, and
- `evidence`, optional, the large record every drafting stage sends unchanged.

Each is a `Segment` carrying its text and the `prompts/` template it was
rendered from. A call can say which prompts produced it without keeping any of
their output. A fourth segment, the `kernel`, is derived rather than passed, so
no stage can build a request without it. Beside them sit the images, the output
ceiling, and a `SearchPolicy` for hosted web search.

Naming the parts is what makes the adapters comparable. Both vendors get the
kernel and the instructions as instruction text. Both get the evidence as user
content, carrying the cache breakpoint, with the task after it. The candidate
record is data, and it never arrives at instruction authority. OpenAI also keys
the request on a digest of everything ahead of the breakpoint. Those are two
renderings of one plan. They are not two prompt boundaries kept in step by hand.
`tests/delivery/test_adapters.py` pins each whole request shape.

`SearchPolicy` states what a hosted search may do, and each adapter renders the
parts its vendor has. Search-context size and live access reach OpenAI only.
What has to match is the deterministic contract above the adapters, not the
requests below them. Allowed and blocked domains together are
refused when the policy is built, before a run has spent anything, because
Anthropic answers that combination with a 400. A search records its query and
the URL and title of each page it read, never the quoted passages or either
vendor's continuation blob.

A stage states its image detail and verbosity the way it states its effort. Only
the OpenAI adapter renders either, because Anthropic publishes neither control,
and ingest ships `high`. The image measurement reports a ceiling where the vendor
publishes a rule and nothing where it does not. Anthropic's is bounded: a
28-pixel patch, with downscaling past 2576 pixels on the long edge or 4784
visual tokens. OpenAI's is a 32-pixel patch whose fitting edge, patch budget,
and token multiplier depend on the model and the detail level; the measurement
applies the rules OpenAI's vision guide publishes for `gpt-6-astra` and the
`gpt-5.6` family, and reports nothing for a model the guide does not cover. An
image past a limit a vendor documents as an error is refused locally, since that
request could not have been answered.

Matching's `requirement_id` field is a closed vocabulary of one fixed
`VOCABULARY_SIZE`, which grows only for a posting with more requirements than
that, rather than closed over one posting's identifiers. The response schema is part of the prefix the provider caches, and
a schema that changes with every posting invalidates a stable prefix of some
thousands of tokens on each call. Rounding keeps the model inside a small fixed
set. A row naming a requirement that does not exist is discarded by
`_normalized_matches` and recorded on the evidence map, and its requirement
falls through as a missing row: weak output can hide a match and cannot hide a
gap. CV entry identifiers, which carry the citation guarantee, stay exact.

## What a model call reports

Every paid call goes through one method on `Client`. The live narration, the
cost ledger, and the cache all see the same facts. Adapters return a
`CallRecord` describing the call: token usage, the model actually served, the
response identifier, the service tier, why generation stopped, and the full
request and response envelopes as the provider SDK serializes them.

`Client` joins that record to the plan and the resolved model. The result is
one `CallManifest` per call. The ledger keeps them in call order, so a run's
ledger is its manifest. `record_calls` writes each one to `model_calls`.

A manifest identifies its request by four digests. They cover the instructions,
the evidence, the task, and the JSON Schema sent on the wire. Two calls that
agree on all four were made of the same thing, and a single differing digest
names which part moved. A fifth digest covers the response, so a change in what
the model answered is visible without reading the answer.

The text itself is kept too, in `model_call_envelopes`, one row per call holding
the request and response as sent and received. The digests answer whether
something moved; the envelopes answer what it was. Beside the digests the
manifest carries:

- the prompt template names,
- the provider, the requested model, and the served model,
- the effort, the service tier, and the tool policy,
- the package revision and the response identifier,
- the finish reason, tokens, and cache reads and writes,
- latency and cost, and
- whether the response validated.

A call that was billed for and could not be used is recorded too. Truncation, a
refusal, a content filter, and a payload that fails validation all arrive after
the provider has charged for the request. If the failure traveled while the
ledger stayed empty, a run would report a cost lower than the invoice by exactly
the calls that went wrong. `UnusableResponseError` carries the record out with
the error for that reason. The stored call is marked as not validated, not
omitted.

`Finish` normalizes that last field across both vendors. An unusable response
has causes that need different responses, and discarding the reason would make
them look identical.

- `truncated` means the object was cut off at the token ceiling. It needs a
  larger ceiling or less effort.
- `refused` and `filtered` will not improve on retry. The prompt must change.
- `failed` is the provider's own error and is worth retrying.
- `unknown` means the vendor gave a reason this version does not know, or
  paused or canceled part-way.

`unknown` is refused, not parsed. Without that branch it would reach schema
validation and be reported as a mismatch. That confusion is the one the whole
vocabulary exists to prevent.

Normalized is not symmetric. `filtered` and `failed` are Responses API states
with no Anthropic equivalent. A value this enum can express is not a value both
vendors can send, and the enum says so instead of implying parity.

Neither is the evidence behind the two adapters. Both are built from one schema
builder. Both are exercised offline through their real vendor SDK. That is what
makes the guarantee testable that a schema means the same thing to each of
them. Only OpenAI has been run against the live API. A recording of that run is
committed as `tests/cassettes/run_openai.json` and replayed through the real
SDK. The Anthropic adapter is written against the published API and has never
been watched working. That is a different claim from the one the offline suite
supports. [Validation status](docs/validation.md#provider-claims-are-deliberately-uneven)
owns that claim and does not fold it into "both providers are supported".

Provider-hosted tools are separate capabilities. They are not optional
arguments that every adapter pretends to support. A synchronous hosted search
requires the `WebSearchProvider` capability, which both adapters implement. A
guided campaign requires `BackgroundWebSearchProvider`, which only the OpenAI
adapter implements. A campaign on any other provider refuses before it claims
anything.

Anthropic sends `web_search_20260209` beside the same `output_config.format`
schema used by ordinary completions. It names `allowed_callers` as `["direct"]`
instead of letting it default. From that tool version on, the default runs the
search from inside code execution. That provisions a second server tool nobody
asked for, and it carries a different feature's retention terms. So the caller
is stated.

A `pause_turn` response is continued with the paused assistant content and the
identical tool definition, capped at three continuations. Usage from every
response is summed into the one call record.

OpenAI sends `web_search` beside the ordinary strict `text.format` schema in one
Responses API request. A campaign sends the same request in the background.
Both adapters pass the plan's search count as the tool call limit: `max_uses`
for Anthropic and `max_tool_calls` for OpenAI. A campaign report still counts
what the provider actually ran.

Reasoning tokens are reported separately but are a subset of output tokens,
never an addition. Both vendors bill them at the output rate inside the output
total. At high effort, reasoning is usually most of the bill. It is also why a
generous ceiling can still be exhausted. A run that hides it cannot explain its
own cost.

`request_timeout` exists because both SDKs default to ten minutes per request.
Across several drafting and critic calls, that is an hour before a run that will
never finish admits it.

A discount tier is asked for per call and priced from the tier the provider
says served it. OpenAI serves `flex` at half the standard rate, more slowly, as
a `service_tier` value on the Responses call the adapter already makes. It
refuses flex when it has no spare capacity, and a refusal bills nothing, so the
call is retried once at the standard tier and billed at the standard rate.
Anthropic publishes no equivalent, and asking it for one is refused before the
run starts. The tier is an argument, not a setting, so no configuration can put
interactive drafting or market search on a slower tier.
`Settings.unattended_tier()` supplies it for queue work nobody waits on. A run
that asked for a discount and did not get one says so in its own record.

## Untrusted input

**A job description is data written by a stranger.** Raw posting text is used
for structured extraction and is never passed to the drafter. Extracted keywords
are normalized and length-bounded before any later stage sees them. The
evidence restrictions above still hold if a posting contains instructions aimed
at a model. Treat this as defense in depth, not proof against every
prompt-injection technique. Adversarial cases belong in the tests.

**The boundary is declared once, in `prompts.py`, not per prompt.** Every
placeholder of every prompt is declared in `prompts.FIELDS` as one of three
things.

- A block is text the program did not write. `render` wraps it in a named,
  escaped, self-describing untrusted-data block.
- An inline value is a short one it did not write either, such as a job title or
  an employer. It is escaped and flattened to one line, so it cannot open a tag
  or start what looks like a new instruction.
- Everything else is written by the program or by the user's own settings file.
  It is meant to be read as an instruction.

A placeholder with no declaration raises instead of being sent. That is the
part that matters. Two dozen prompt templates making their own decisions would
always leave one of them missed. `tests/durability/test_prompts.py` enumerates the
declarations. It renders every untrusted field with a payload that tries to
close its own tag. `evals/cases/bounded_posting_text.toml` follows one phrase
from a posting, one requirement, and one record entry through extraction,
matching, drafting, and criticism. It counts the times any of them reached a
request outside a boundary.

**A CV is untrusted too.** It is usually the user's own file. It may have been
produced by another tool or received from someone else. So it is parsed under
the same closed-vocabulary rules and gets its own poisoned fixture.

**Non-ASCII is folded by the importer, not by a model.** Typographic punctuation
and combining accents are closed string maps. `importer.plain` applies them to
everything the import writes and to both sides of every comparison it makes. A
character with no ASCII form is reported by name, not transliterated. Guessing
at the spelling of somebody's name is worse than telling them their CV carries a
character this tool cannot.

**Credentials never enter the repository or the output.** A key resolves from a
one-run flag, a provider environment variable, or the system keyring, in that
order. It is never written to the settings file, a cache, a report, or
diagnostic output.

**Everything written is owner-only.** Reports, resumes, cover letters, imported
master CVs, import reports, and cached responses are created 0600. A cache entry
contains model output derived from the whole CV. It deserves the same protection
as the CV. Directories the tool merely writes into are not tightened. Silently
taking a folder the user chose to 0700 is a surprise, not a security
improvement.

## Run storage

Runs are filed as `<company>_<role>--<posting-id>/runs/<timestamp>-<inputs>/`.
The posting identifier is the employer's own requisition number when the URL
carries one. Otherwise it is a hash of the posting text, marked as the weaker of
the two in the manifest.

A run is written into a staging directory and moved into place in one
operation. An interrupted run leaves nothing, not a package that reads as
finished. Promotion never lands on an existing run.

Identical inputs return the run already on disk instead of billing again.
`RunIdentity` writes down what counts as identical. It lives in `runs.py` beside
the manifest it is serialized into. Anything absent from that list is a claim
that it does not affect the output. The list is:

- the posting identifier,
- the master CV's content hash,
- a fingerprint of the prompt suite,
- a hash of the settings that change the output,
- the package version and source identity,
- whether a cover letter or a trim was asked for,
- the digest of the company research available to a cover letter, and
- the digest of any review approval applied.

The settings hash joins `layout.shaping_fingerprint()`, a digest of the
length-budget constants. Editing the budget changes what a run produces while
every declared setting stays the same. The fingerprint is derived from the
constants themselves, not from the bytes of any source file. So it holds under a
packaging that does not lay the source out as files.

`infrastructure/build.py` captures source identity once per process, separately
from the package version. It hashes source and packaged prompt bytes. It records
Git HEAD and dirty state when available, and uses the installed RECORD when
source is absent. Missing identity stays unknown. A run with an unknown or
different source identity is not automatically reused. Cached responses keep
their producer identity. They are not relabeled as the current build.

`run.json` records what a run was: a content hash of every artifact, how many
checks it answered and whether they passed, the measured keyword coverage, and
the model the provider actually served for each billed stage. The last two exist
because prose is not an interface. The next run of a posting answers "did that
help" from the recorded number. It does not run a regular expression over the
previous report. A model substitution changes both the price and the output.
Without the record it would be visible only in the narration of a verbose run.

Everything a paid call depends on is in the cache key:

- the provider, the model, and the effort,
- the stage name and the output ceiling,
- the rendered system and user text and the cached prefix,
- the master CV's hash,
- a digest of any image payload, and
- the JSON Schema actually sent on the wire, not the Pydantic model's own.

The wire schema matters because it is the contract the provider enforces.
Editing how it is built has to move the key with it. The prompt-suite and
shaping fingerprints are deliberately not in the key. Both reach a call only
through the rendered prompt text, which is keyed already. Adding them again
would invalidate every cached answer for a change that did not alter a single
request.

`--force` skips the reuse check and asks the model again for the stages that
write the document. Extraction and matching stay cached, because they read the
posting and do not judge it. Somebody who types `--force` after being told
nothing changed wants a fresh answer, not a new directory filled from the cache.

Deciding that a package is an application is a person's job, not this tool's.

## Why the prompt suite is shaped this way

[prompts/README.md](prompts/README.md) is the working guidance. This is the
reasoning behind it, which is a maintainer's question more than a prompt
author's.

The suite follows a few durable practices. It does not imitate one provider's
favorite format.

- State the outcome, evidence, constraints, success criteria, and output shape.
- Separate long or untrusted data from instructions.
- Use provider-supported structured output for contracts.
- Break a complex workflow into bounded stages.
- Keep reusable prefixes stable where caching matters.
- Evaluate on representative cases and improve from observed failures.
- Use direct, contextual industry language without pretending keyword coverage
  predicts a hiring outcome.

The ATS language is deliberately narrow, and the narrowness is the point.
Greenhouse publicly documents parsing problems with columns, tables, graphics,
headers, footers, unclear sections, and abbreviated titles. Workday documents
contextual direct and indirect skill extraction with source attribution. Those
two facts support conventional structure, clear titles, contextual terminology,
and provenance. They do not support a universal ATS percentage or a keyword
density target, so the program does not claim one.

Primary references change as models and provider guidance do. Recheck them
during a model upgrade. Trust measured behavior in TailorCV's own cases over a
generic claim in any of them.

- [OpenAI structured outputs](https://developers.openai.com/api/docs/guides/structured-outputs)
- [OpenAI web search](https://developers.openai.com/api/docs/guides/tools-web-search)
- [OpenAI evaluation flywheel](https://developers.openai.com/cookbook/examples/evaluation/building_resilient_prompts_using_an_evaluation_flywheel)
- [Anthropic prompting best practices](https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/claude-prompting-best-practices)
- [Anthropic structured outputs](https://platform.claude.com/docs/en/build-with-claude/structured-outputs)
- [Anthropic server tools](https://platform.claude.com/docs/en/agents-and-tools/tool-use/server-tools)
- [Anthropic web search](https://platform.claude.com/docs/en/agents-and-tools/tool-use/web-search-tool)
- [Greenhouse resume parsing guidance](https://support.greenhouse.io/hc/en-us/articles/200989175-Unsuccessful-resume-parse)
- [Workday Skill Extract API](https://developer.workday.com/documentation/GUID-d934c61c-80ee-4513-9800-ae2f6d1d02d2/SkillExtractRESTAPIs)

## Scope

TailorCV writes reviewable local packages. It maintains a local, append-only
job-search and application-history ledger. It can track user-recorded
application state and files. It does not contact employers, submit an
application, or infer that a generated package was sent. Length is budgeted from
an estimate in `layout.py`, calibrated against real renders. A renderer is
always the authority. Those boundaries should not be crossed indirectly through
a prompt.

Every constant in `layout.py` measures one rendered geometry: a single-column
US-Letter resume at a conventional body size. The constants were fitted by
rendering whole documents of known shape through the assembler, varying the
header, summary, skills, role context, bullets, earlier career, education, and
publications, and `tests/loop/test_layout.py` holds each shape with its measured
lines. Selection spends a budget in master-text lines scaled by
`DRAFTED_SHARE`, so it buys more than the page holds; the fit pass then
estimates the drafted Markdown block by block with the same model and trims it
to the page. A different document design needs the experiment repeated and the
file edited, which is why no constant lives inside the selection logic.

`--render` and the document-rendering task do not cross that boundary. They look
for a renderer on PATH, ask it with a fixed argument list to convert the file
just written, and report what came back. That list names the theme and density
`layout.py` was measured in, so a renderer's own default cannot run the page
long. There is no import and no declared dependency, and a test enforces both. The renderer may be absent, fail, or
write nothing. Then the run says so and the Markdown is exactly as good as it
was. The deliverable is the Markdown, and a missing PDF is an inconvenience
rather than a lost afternoon.
