/** Show requirements with their cited evidence and unresolved gaps. */
import { useId, useState } from "react";
import { Link } from "react-router-dom";

import type { EvidenceSource, JobDetail } from "../../api/client";
import { matchLabel, matchLegend, matchMeaning } from "../../api/matchLabels";
import { LoadingState } from "../../components/States";
import { titleCase } from "../../components/text";

type Evidence = JobDetail["evidence"][number];
type Factor = JobDetail["factors"][number];
type Requirement = {
  answered: boolean;
  hardGate: boolean;
  id: string;
  text: string;
  posting: Evidence[];
  record: Evidence[];
  verdicts: { classification: string; strength: string }[];
};

export type EvidenceSourceQuery = {
  data?: EvidenceSource;
  error: Error | null;
  isError: boolean;
  isLoading: boolean;
};

/** Longer than this and the quote is clamped until the reader asks for it. */
const CLAMP_LIMIT = 260;

function sourceLabel(sourceKind: EvidenceSource["source_kind"]): string {
  return sourceKind === "candidate" ? "Master CV" : "posting";
}

export function UnsupportedEvidence({
  items,
  onToggle,
  unsupportedOnly,
}: {
  items: JobDetail["unsupported"];
  onToggle: () => void;
  unsupportedOnly: boolean;
}) {
  return (
    <>
      {items.length > 0 && (
        <aside className="unsupported-banner" role="alert">
          <div>
            <strong>
              {items.length} unsupported item{items.length === 1 ? "" : "s"}
            </strong>
            <p>A stored finding with no evidence link behind it. Missing evidence stays visible.</p>
          </div>
          <button className="button button--quiet" onClick={onToggle} type="button">
            {unsupportedOnly ? "Show evidence map" : "Show unsupported only"}
          </button>
        </aside>
      )}
      {unsupportedOnly && (
        <article className="panel unsupported-panel">
          <h2>Unsupported</h2>
          <ul>
            {items.map((item) => (
              <li key={`${item.kind}-${item.id}`}>
                <span>{titleCase(item.kind)}</span>
                <strong>{item.label}</strong>
                <p>{item.reason}</p>
              </li>
            ))}
          </ul>
        </article>
      )}
    </>
  );
}

export function EvidenceMap({
  answered,
  evidenceSource,
  factors,
  fitReportPending,
  jobId,
  onSelectEvidence,
  requirements,
  selectedEvidenceId,
  unattached,
}: {
  answered: number;
  evidenceSource: EvidenceSourceQuery;
  factors: Factor[];
  fitReportPending: boolean;
  jobId: string;
  onSelectEvidence: (evidenceId: string) => void;
  requirements: Requirement[];
  selectedEvidenceId: string;
  unattached: Evidence[];
}) {
  const quoteProps = { jobId, onSelect: onSelectEvidence, selectedEvidenceId };
  return (
    <>
      <CoverageSummary
        answered={answered}
        classifications={requirements.flatMap((item) =>
          item.verdicts.map((verdict) => verdict.classification),
        )}
        total={requirements.length}
      />
      {requirements.length === 0 && (
        <article className="panel">
          <p className="field-note">
            No posting requirement is stored against this evaluation, so there is nothing to pair.
            That is a missing record, not a clean result.
          </p>
        </article>
      )}
      <div className="requirement-list">
        {requirements.map((requirement) => (
          <RequirementCard
            fitReportPending={fitReportPending}
            key={requirement.id}
            requirement={requirement}
            {...quoteProps}
          />
        ))}
      </div>
      {unattached.length > 0 && (
        <article className="panel unattached-evidence">
          <header>
            <div>
              <h2>Evidence not tied to a requirement ({unattached.length})</h2>
              <p>
                These rows cite no requirement and no card above shows them, so they are not counted
                in the figures above.
              </p>
            </div>
          </header>
          <div className="unattached-list">
            {unattached.map((item) => (
              <EvidenceQuote item={item} key={item.id} {...quoteProps} />
            ))}
          </div>
        </article>
      )}
      <div className="detail-grid">
        <FactorsPanel factors={factors} onSelectEvidence={onSelectEvidence} />
        <EvidenceSourcePanel
          evidenceSource={evidenceSource}
          selectedEvidenceId={selectedEvidenceId}
        />
      </div>
    </>
  );
}

/**
 * The three numbers the screen exists to answer. "Answered" means a record
 * entry is cited under the requirement, which is weaker than saying the record
 * proves it; the classification on each card is where that judgment lives.
 */
function CoverageSummary({
  answered,
  classifications,
  total,
}: {
  answered: number;
  classifications: string[];
  total: number;
}) {
  const missing = total - answered;
  const legend = matchLegend(classifications);
  return (
    <aside className="coverage-summary">
      <div className="coverage-figures">
        <div className="coverage-figure">
          <strong>{total}</strong>
          <span>requirement{total === 1 ? "" : "s"} read from the posting</span>
        </div>
        <div className="coverage-figure coverage-figure--answered">
          <strong>{answered}</strong>
          <span>answered by an entry in the record</span>
        </div>
        <div className={`coverage-figure${missing > 0 ? " coverage-figure--gap" : ""}`}>
          <strong>{missing}</strong>
          <span>answered by nothing</span>
        </div>
      </div>
      <p>
        Answered means an entry from the record is cited under the requirement. The chip on each
        card says how well it answers it.
      </p>
      {legend.length > 0 && (
        <dl className="match-legend">
          {legend.map((item) => (
            <div key={item.key}>
              <dt>{item.label}</dt>
              <dd>{item.meaning}</dd>
            </div>
          ))}
        </dl>
      )}
    </aside>
  );
}

function RequirementCard({
  fitReportPending,
  requirement,
  jobId,
  onSelect,
  selectedEvidenceId,
}: {
  fitReportPending: boolean;
  requirement: Requirement;
  jobId: string;
  onSelect: (id: string) => void;
  selectedEvidenceId: string;
}) {
  const quoteProps = { jobId, onSelect, selectedEvidenceId };
  const ask = requirement.text.trim() || requirement.posting[0]?.text.trim();
  const askId = useId();
  return (
    <article
      aria-labelledby={askId}
      className={`requirement-card requirement-card--${requirement.answered ? "answered" : "gap"}`}
    >
      <header>
        <div className="requirement-heading">
          <strong className="requirement-ask" id={askId}>
            {ask || "Requirement text unavailable"}
          </strong>
          <p className="requirement-state">
            {requirement.answered ? "Answered by the record" : "Nothing in the record is cited"}
          </p>
          <span className="requirement-id">{requirement.id}</span>
        </div>
        <div className="requirement-marks">
          {requirement.hardGate && (
            <span className="pair-mark" title="A must-meet condition from the posting">
              Hard gate
            </span>
          )}
          {requirement.verdicts.map((verdict) => (
            <span
              className={`pair-mark pair-mark--${verdict.classification.toLowerCase()}`}
              key={`${verdict.classification} ${verdict.strength}`}
              title={matchMeaning(verdict.classification)}
            >
              {requirement.answered
                ? matchLabel(verdict.classification)
                : `Uncited: ${matchLabel(verdict.classification)}`}
              <small>{verdict.strength} strength</small>
            </span>
          ))}
        </div>
      </header>
      <div className="requirement-pair">
        <section>
          <h3>What the posting asks</h3>
          {requirement.posting.length > 0 ? (
            requirement.posting.map((item) => (
              <EvidenceQuote item={item} key={item.id} {...quoteProps} />
            ))
          ) : fitReportPending ? (
            <p className="field-note">Matching posting quotes to requirements...</p>
          ) : (
            <p className="field-note">
              No posting span cites this requirement; the text above is the extracted requirement.
            </p>
          )}
        </section>
        <section>
          <h3>What the record answers with</h3>
          {requirement.record.map((item) => (
            <EvidenceQuote item={item} key={item.id} {...quoteProps} />
          ))}
          {!requirement.answered && (
            <p className="gap-callout">
              <strong>No candidate-record match is stored.</strong>
              <span>
                The requirement is unsupported here. That is not a finding that the record has
                nothing to offer it.
              </span>
            </p>
          )}
        </section>
      </div>
    </article>
  );
}

/** Name what a quote is, preferring the reading it supports over a raw locator. */
function evidenceOrigin(item: Evidence): string {
  if (/^cv:role-.+-dates$/.test(item.locator)) {
    return "Years the record covers, from a CV role's dates";
  }
  if (/^cv:role-.+-title$/.test(item.locator)) {
    return "What the record reads as, from a CV role's title";
  }
  const reading = /^posting:reading-(?<name>.+)-\d+$/.exec(item.locator)?.groups?.name;
  if (reading) return `Posting text read for ${reading.replaceAll("_", " ")}`;
  return item.locator || item.master_cv_entry_id || sourceLabel(item.source_kind);
}

function EvidenceQuote({
  item,
  jobId,
  onSelect,
  selectedEvidenceId,
}: {
  item: Evidence;
  jobId: string;
  onSelect: (id: string) => void;
  selectedEvidenceId: string;
}) {
  const postingLink =
    item.observation_id && item.char_start !== null && item.char_end !== null
      ? `/jobs/${jobId}?tab=posting&observation=${item.observation_id}&start=${item.char_start}&end=${item.char_end}`
      : "";
  const selected = item.id === selectedEvidenceId;
  return (
    <blockquote className={`evidence-quote${selected ? " evidence-quote--selected" : ""}`}>
      <ClampedText>{item.text}</ClampedText>
      <footer>
        <span>{evidenceOrigin(item)}</span>
        {postingLink ? (
          <Link className="evidence-link" to={postingLink}>
            View exact span
          </Link>
        ) : (
          <button className="text-button" onClick={() => onSelect(item.id)} type="button">
            Open source
          </button>
        )}
      </footer>
    </blockquote>
  );
}

/** Clamp long evidence entries until expanded. */
function ClampedText({ children }: { children: string }) {
  const [expanded, setExpanded] = useState(false);
  if (children.length <= CLAMP_LIMIT) return <p>{children}</p>;
  return (
    <>
      <p className={expanded ? undefined : "clamped"}>{children}</p>
      <button
        aria-expanded={expanded}
        className="text-button"
        onClick={() => setExpanded((value) => !value)}
        type="button"
      >
        {expanded ? "Show less" : "Show full text"}
      </button>
    </>
  );
}

function FactorsPanel({
  factors,
  onSelectEvidence,
}: {
  factors: Factor[];
  onSelectEvidence: (evidenceId: string) => void;
}) {
  return (
    <article className="panel">
      <h2>Factors</h2>
      {["pro", "con", "gap", "risk"].map((type) => (
        <div className="factor-group" key={type}>
          <h3>
            {type === "pro"
              ? "Pros"
              : type === "con"
                ? "Cons"
                : `${type[0]?.toUpperCase()}${type.slice(1)}s`}
          </h3>
          <ul>
            {factors
              .filter((item) => item.type === type)
              .map((item) => (
                <li key={`${type}-${item.position}`}>
                  {item.text}
                  {item.evidence_ids.length ? (
                    <button
                      className="text-button"
                      onClick={() => onSelectEvidence(item.evidence_ids[0] ?? "")}
                      type="button"
                    >
                      View evidence
                    </button>
                  ) : (
                    <strong className="unsupported-label">Unsupported</strong>
                  )}
                </li>
              ))}
          </ul>
        </div>
      ))}
    </article>
  );
}

function EvidenceSourcePanel({
  evidenceSource,
  selectedEvidenceId,
}: {
  evidenceSource: EvidenceSourceQuery;
  selectedEvidenceId: string;
}) {
  if (!selectedEvidenceId)
    return (
      <article className="panel evidence-source">
        <h2>Source span</h2>
        <p>Select a quote, score, or factor to inspect its exact stored source.</p>
      </article>
    );
  if (evidenceSource.isLoading) return <LoadingState label="Verifying evidence source" />;
  if (evidenceSource.isError || !evidenceSource.data)
    return (
      <article className="panel evidence-source">
        <h2>Source unavailable</h2>
        <p className="field-error">
          {evidenceSource.error?.message ?? "The stored source could not be read."}
        </p>
      </article>
    );
  const source = evidenceSource.data;
  return (
    <article className="panel evidence-source">
      <h2>Verified {sourceLabel(source.source_kind)} source</h2>
      <p className="muted">
        SHA-256 {source.sha256}, {source.locator || "exact character span"}
      </p>
      <pre>
        <SourceSpan body={source.body} start={source.char_start} end={source.char_end} />
      </pre>
    </article>
  );
}

function SourceSpan({
  body,
  start,
  end,
}: {
  body: string;
  start: number | null;
  end: number | null;
}) {
  if (start === null || end === null || end > body.length) return body;
  return (
    <>
      {body.slice(0, start)}
      <mark className="evidence-highlight">{body.slice(start, end)}</mark>
      {body.slice(end)}
    </>
  );
}
