# Datasets for prompt evaluation

The scoring datasets live under this directory. Calibration evidence does not:

- `match.jsonl` (and any other stage's `*.jsonl`): the independently reviewed
  dataset the harness scores against. A file only becomes a scoring dataset
  after two isolated reviewer-agent passes and separate blinded adjudication;
  workspace feedback and model output never count as labels. See
  `evals/inspect/METHODOLOGY.md` for the calibration procedure.
- Raw calibration data emitted by `scripts/eval.sh build-calibration` stays at
  an explicit private path outside the public repository. Each row carries a
  workspace ruling paired with the current stored classification. It is not a
  label file. Treating a ruling as an expected label trains the harness to the
  last mistake (bandit-feedback bias). Use rulings to measure the current
  classifier's bias against separately assigned blinded labels.

`match.jsonl` and `relevance_review.jsonl` are synthetic and tracked, so a
score can be reproduced; `scripts/audit_public_data.py` scans them. Any other
`*.jsonl` here stays ignored, because a set built from real postings carries
their text.

## Match.jsonl record shape

One record per line, written only by `serialize_match_dataset` in
`evals/inspect/selection.py`, which is the authority on its fields: the opaque
case id, the requirement text (also as `input`), `dimension`, `cv_hash`,
`split`, `expected_classification`, and `expected_strength`. A row may also
carry the `provenance` record described below.

`expected_classification` and `expected_strength` are the label frozen after
two independent blinded reviewer-agent passes and separate adjudication. They
are not derived from a ruling, stored classification, or evaluated model
output. `expected_strength` is `none` exactly for `ASSERTED` and `GAP`. A case
the selection manifest carries a ruling for has both fields as index-paired
lists, the classification and strength pairs the ruling accepts; the ruling and
its basis stay in the manifest, which `evals/inspect/METHODOLOGY.md` describes.

Reviewer packets and run logs carry only the opaque IDs, so a reviewer labels
blind to where a requirement came from. The source mapping, the candidate pool,
and the selection manifest carry the provenance. Requirement text must be
traceable to an exact source span and must not be paraphrased by the extractor
agent.

Every match row's `cv_hash` must equal the fixed synthetic inventory hash, and
`dimension` must be `hard` or `behavioral`. The runner removes expected labels
and reviewer fields before invoking a solver. `DIRECT`, `TAILORING_ONLY`, and
`TRANSFERABLE` answers require a cited synthetic inventory entry. `ASSERTED`
and `GAP` answers must not cite one; `ASSERTED` is reserved for eligible status
facts rather than CV accomplishments.

The rows are the projection of a selection manifest. Its `development` rows fill
the slots `evals/inspect/METHODOLOGY.md` explains, and its `holdout` rows are
every QA-approved, labeled candidate the pool froze in the holdout split; the
runner serves a held-out row only to a run that names `--split holdout`.
`receipts/match-labeling-2026-09-25/` holds the manifest in force and the
chain behind it: the source mapping, candidates, extraction QA, both reviewer
packets, raw reviews and proposals, the adjudication, the per-case rulings, and
the frozen bundles whose digests the manifest pins. Every receipt names the
model its agent's launch result resolved. The receipts are re-derived from the
2026-09-25 reviews after the digest domain strings were renamed. The packets those agents
read were the same cases in another order, and they stay in the private archive. Only the round in force is kept here,
so the receipts behind an `evals/RESULTS.md` row on an earlier manifest are not
in the repository.

## Relevance_review.jsonl record shape

One record per line. Fields:

```json
{
  "id": "rrp-003",
  "split": "development",
  "cv_hash": "<16 hex>",
  "input": {
    "search_context": "<the search as search_summary renders it>",
    "posting": {
      "id": "rrp-003",
      "employer": "<synthetic employer>",
      "title": "<posting title>",
      "body": "<posting text>"
    }
  },
  "expected_recommendation": "review",
  "expected_axes": {
    "core_work_alignment": "direct",
    "core_qualification_alignment": "established",
    "hard_condition": "passes",
    "decisive_basis": "core_match"
  },
  "axes_confidence": "high",
  "provenance": null
}
```

`expected_recommendation` is one of `review`, `investigate`, `unlikely`, or
`insufficient_evidence`. The four `expected_axes` vocabularies are the shipped
ones, closed in
`src/clearscout/application/discovery/relevance_review.py`. `axes_confidence` is
`high` or `low`.

`expected_recommendation` and `expected_axes` are frozen calibration from two
independent labeller passes and a separate adjudication. They are not gold
labels, and neither a ruling, a stored classification, nor evaluated model
output produced them. A case whose axes the two passes did not agree on before
adjudication carries `axes_confidence: low`.

The scorer grades the recommendation, and it grades the four axes on a row the
two passes agreed. A row carrying `axes_confidence: low` has its axes recorded
in the score metadata and counted in the run header rather than scored; see
`evals/inspect/METHODOLOGY.md` for why. `RelevanceReviewLabel` in
`evals/inspect/relevance_review_dataset.py` is the schema for the reviewed half
of a row, and it refuses axes and a recommendation the shipped stage would never
allow together, and refuses a row carrying a field neither the corpus case nor
the label defines, so a misspelled key cannot pass as an absent one. The input
half of the corpus is `evals/fixtures/relevance_review_corpus.jsonl`.

## The provenance record both families carry

Either file's row may carry a `provenance` object, the review record behind its
label:

```json
{
  "protocol_version": "clearscout-match-development-v1",
  "family": "match | relevance-review",
  "rubric_version": "<the rubric both reviewers judged against>",
  "reviewer_a": "<the first blind reviewer>",
  "reviewer_b": "<the second blind reviewer>",
  "disagreements": ["<material field>"],
  "adjudicator": "<who settled them>",
  "adjudicator_kind": "agent | maintainer",
  "review_a_sha256": null,
  "review_b_sha256": null,
  "adjudication_sha256": null,
  "resolved_label_sha256": null
}
```

An absent `provenance` means nobody has written the record for that row. It is
not the same fact as a record whose `disagreements` is empty, which says two
recorded reviewers differed on nothing, so a record states `disagreements`
rather than leaving it out. `LabelProvenance` in
`evals/inspect/review_resolution.py` refuses a half-written record: every named
disagreement is one of the family's own material fields in that family's
canonical order, an adjudicator appears exactly where there are disagreements
and never matches either reviewer, and the four digests are the ones a family
whose reviews went through the hash-bound protocol can fill. Both families'
scorers validate the record their row carries, so an invented one fails the run
instead of riding along unread. No row in the tracked sets carries one.

## Private calibration record shape

```json
{
  "id": "cal-<posting_id>-<requirement_key>",
  "requirement_key": "<uuid>",
  "requirement_text": "<verbatim from posting_requirements.text>",
  "posting_id": "<uuid>",
  "posting_url": "<one of the recorded URLs, may be empty>",
  "ruling": "met | not_applicable | disputed_fit | disputed_extraction | reversed",
  "current_classification": "DIRECT | TAILORING_ONLY | TRANSFERABLE | ASSERTED | GAP",
  "dimension": "hard",
  "cv_hash": "<sha256 hex of the master CV version the evaluation used>",
  "database_url": "<sqlite URL of the source workspace>"
}
```

Regenerate with:

```bash
scripts/eval.sh build-calibration \
  --data-root <workspace-root> \
  --output <private-path-outside-the-public-repository>
```
