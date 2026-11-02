import { MarkdownView } from "../../components/MarkdownView";
import { StatusBadge } from "../../components/StatusBadge";
import { ErrorState, LoadingState } from "../../components/States";
import type { FitReport as FitReportData } from "../../api/client/types";

export function FitReport({
  error,
  exportUrl,
  onShowEvidence,
  isError,
  isLoading,
  report,
  markdown,
  staleReason,
}: {
  error: Error | null;
  exportUrl: string;
  onShowEvidence: (evidenceId: string) => void;
  isError: boolean;
  isLoading: boolean;
  report?: FitReportData;
  markdown?: string;
  staleReason?: string;
}) {
  const requirements = report?.requirements ?? [];
  const hardGates = requirements.filter((item) => item.is_hard_gate);
  const plainRequirements = requirements.filter((item) => !item.is_hard_gate);
  const needsReview = hardGates.some((gate) => gate.span_status === "needs_review");
  // A gate is unestablished when no settled requirement answers it: none restates
  // it, or the one that does is itself unestablished.
  const unestablished = plainRequirements.filter((item) => !item.established).length;
  const unestablishedGates = hardGates.filter((item) => !item.established).length;
  const renderEvidence = (evidenceKeys: string[]) =>
    evidenceKeys.length ? (
      <div className="fit-report__evidence">
        {evidenceKeys.map((evidenceId) => (
          <button
            className="button button--quiet"
            key={evidenceId}
            onClick={() => onShowEvidence(evidenceId)}
            type="button"
          >
            Evidence {evidenceId.slice(0, 8)}
          </button>
        ))}
      </div>
    ) : (
      <span className="muted">No linked evidence</span>
    );
  const renderItems = (items: FitReportData["requirements"], label: string) => {
    const sectionId = `fit-report-${label.toLowerCase().replaceAll(" ", "-")}`;
    return items.length ? (
      <section className="fit-report__structured-section" aria-labelledby={sectionId}>
        <h3 id={sectionId}>{label}</h3>
        <ul className="fit-report__requirements">
          {items.map((item) => (
            <li key={item.requirement_id}>
              <div className="fit-report__requirement-heading">
                <strong>{item.text}</strong>
                <StatusBadge value={item.span_status} />
                {item.unmet && <span className="fit-report__unmet">Unmet</span>}
                {!item.established && (
                  <span className="fit-report__unestablished">Not established</span>
                )}
                <span className="muted">{item.importance.replaceAll("_", " ")}</span>
              </div>
              {renderEvidence(item.evidence_keys)}
            </li>
          ))}
        </ul>
      </section>
    ) : null;
  };

  return (
    <section className="panel fit-report">
      <header>
        <div>
          <h2>Stored fit report</h2>
          <p>
            This Markdown was generated from committed findings. Opening it never calls a model.
          </p>
        </div>
        <a className="button button--quiet" href={exportUrl}>
          Export Markdown
        </a>
      </header>
      {isLoading && <LoadingState label="Verifying fit report" />}
      {isError && <ErrorState error={error} />}
      {staleReason && (
        <p className="fit-report__review-notice" role="status">
          {staleReason}
        </p>
      )}
      {report && (
        <p className="muted">
          Evaluation v{report.evaluation_version} <span aria-hidden="true">|</span>{" "}
          <span className="sr-only">Evaluation id </span>
          <code>{report.evaluation_id}</code>
        </p>
      )}
      {report && requirements.length > 0 && (
        <div className="fit-report__structured" aria-label="Structured fit findings">
          {needsReview && (
            <p className="fit-report__review-notice" role="status">
              One or more hard gates need review because their source quote could not be verified.
            </p>
          )}
          {unestablished > 0 && (
            <p className="fit-report__review-notice" role="status">
              This run did not establish {unestablished} requirement(s) either way. They are not
              gaps in the master CV. Re-run the evaluation to settle them.
            </p>
          )}
          {unestablishedGates > 0 && (
            <p className="fit-report__review-notice" role="status">
              This run did not settle {unestablishedGates} hard gate(s) either way. Read each one
              marked Not established and decide whether you meet it.
              {unestablished > 0 &&
                " If a requirement below restates it, re-running the evaluation may settle both."}
            </p>
          )}
          {renderItems(hardGates, "Hard gates")}
          {renderItems(plainRequirements, "Requirements")}
        </div>
      )}
      {markdown !== undefined && <MarkdownView>{markdown}</MarkdownView>}
    </section>
  );
}
