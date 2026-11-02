import { api, type JobDetail } from "../../api/client";
import { SortableHeader } from "../../components/SortableHeader";
import { compareValues, type SortDirection } from "../../components/sorting";
import { StatusBadge } from "../../components/StatusBadge";
import { titleCase } from "../../components/text";
import { HistoryPagination } from "./HistoryPagination";
import { EvaluationInputs } from "../../components/EvaluationInputs";

export type ScoreSort = "component" | "score" | "weight" | "product" | "evidence";
export type EvaluationSort = "version_number" | "evaluated_at" | "decision" | "overall_score";

function ConfidenceNote({ arithmetic }: { arithmetic: JobDetail["score_arithmetic"] }) {
  const evidence = arithmetic.find((item) => item.component === "evidence");
  if (!evidence) return null;
  const counted = (evidence.weight ?? 0) > 0;
  return (
    <p className="confidence-note">
      Confidence: evidence {evidence.score ?? "not stated"}.{" "}
      {counted
        ? `Weighted into this total at ${(evidence.weight ?? 0).toFixed(2)} under the policy in force when it was committed, so it moved the score.`
        : "Not part of this total. It reports how well documented this assessment is, not how good the job is."}
    </p>
  );
}

function scoreValue(item: JobDetail["score_arithmetic"][number], sort: ScoreSort) {
  if (sort === "evidence") return item.evidence_links.length;
  return item[sort];
}

// A citation can limit or contradict the score it is attached to, so the bare
// count on its own overstates how well that score is supported.
function sourceSummary(links: JobDetail["score_arithmetic"][number]["evidence_links"]) {
  const noun = links.length === 1 ? "source" : "sources";
  const opposing = links.filter((link) => link.direction !== "supports");
  if (!opposing.length) return `${links.length} ${noun}`;
  const kinds = [...new Set(opposing.map((link) => link.direction))].sort().join(" and ");
  if (opposing.length === links.length) return `${links.length} ${noun} (${kinds})`;
  return `${links.length} ${noun} (${opposing.length} ${kinds})`;
}

export function JobEvaluation({
  data,
  evaluationDirection,
  evaluationSort,
  onOpenStudio,
  onShowEvidence,
  onSortEvaluations,
  onSortScores,
  scoreDirection,
  scoreSort,
  workspaceId,
  evaluationOffset,
  pageSize,
  onEvaluationPageChange,
}: {
  data: JobDetail;
  evaluationDirection: SortDirection;
  evaluationSort: EvaluationSort;
  onOpenStudio: () => void;
  onShowEvidence: (evidenceId: string) => void;
  onSortEvaluations: (column: EvaluationSort) => void;
  onSortScores: (column: ScoreSort) => void;
  scoreDirection: SortDirection;
  scoreSort: ScoreSort;
  workspaceId: string;
  evaluationOffset: number;
  pageSize: number;
  onEvaluationPageChange: (offset: number) => void;
}) {
  const scoreRows = data.score_arithmetic
    .slice()
    .sort((left, right) =>
      compareValues(scoreValue(left, scoreSort), scoreValue(right, scoreSort), scoreDirection),
    );
  const evaluationRows = data.evaluation_history
    .slice()
    .sort((left, right) =>
      compareValues(left[evaluationSort], right[evaluationSort], evaluationDirection),
    );

  return (
    <>
      <section className="panel score-arithmetic">
        <header>
          <div>
            <h2>Current score arithmetic</h2>
            <p>
              Evaluation v{data.evaluation_version}. Policy: {data.score_policy.name} v
              {data.score_policy.version}. Hard gates remain separate.
            </p>
            <ConfidenceNote arithmetic={data.score_arithmetic} />
            <section aria-labelledby="evaluation-inputs-title" id="evaluation-inputs">
              <h3 id="evaluation-inputs-title">Recorded and current evaluation inputs</h3>
              <EvaluationInputs
                evaluated={data.evaluation_id !== null}
                inputs={data.evaluation_inputs}
                detail
              />
            </section>
            <p className="field-note">
              To run this again against the current posting, prompts, and master CV,{" "}
              <button className="text-button" onClick={onOpenStudio} type="button">
                open the Studio
              </button>
              .
            </p>
          </div>
        </header>
        <div className="table-wrap">
          <table aria-label="Current score arithmetic">
            <thead>
              <tr>
                <SortableHeader
                  activeKey={scoreSort}
                  column="component"
                  direction={scoreDirection}
                  label="Component"
                  onSort={onSortScores}
                />
                <SortableHeader
                  activeKey={scoreSort}
                  column="score"
                  direction={scoreDirection}
                  label="Score"
                  onSort={onSortScores}
                />
                <SortableHeader
                  activeKey={scoreSort}
                  column="weight"
                  direction={scoreDirection}
                  label="Weight"
                  onSort={onSortScores}
                />
                <SortableHeader
                  activeKey={scoreSort}
                  column="product"
                  direction={scoreDirection}
                  label="Product"
                  onSort={onSortScores}
                />
                <SortableHeader
                  activeKey={scoreSort}
                  column="evidence"
                  direction={scoreDirection}
                  label="Evidence"
                  onSort={onSortScores}
                />
              </tr>
            </thead>
            <tbody>
              {scoreRows.map((item) => (
                <tr key={item.component}>
                  <th scope="row">{titleCase(item.component)}</th>
                  <td>
                    {item.score ?? "not stated"}
                    {item.finding_state === "conflict" && (
                      <span className="conflict-label"> conflict</span>
                    )}
                    {item.reason && <small>{item.reason}</small>}
                  </td>
                  <td>{item.weight === null ? "not stated" : item.weight.toFixed(2)}</td>
                  <td>{item.product === null ? "not stated" : item.product.toFixed(2)}</td>
                  <td>
                    {item.evidence_links.length ? (
                      <button
                        className="text-button"
                        onClick={() => onShowEvidence(item.evidence_links[0]?.evidence_id ?? "")}
                        type="button"
                      >
                        {sourceSummary(item.evidence_links)}
                      </button>
                    ) : (
                      <strong className="unsupported-label">Unsupported</strong>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
            <tfoot>
              <tr>
                <th scope="row">Weighted sum</th>
                <td>{data.overall_score ?? "not stated"}</td>
                <td>1.00</td>
                <td>
                  {data.score_arithmetic
                    .filter((item) => item.product !== null)
                    .reduce((sum, item) => sum + (item.product ?? 0), 0)
                    .toFixed(2)}
                </td>
                <td>Rounded only when every component is stated</td>
              </tr>
            </tfoot>
          </table>
        </div>
      </section>
      <section className="panel evaluation-history">
        <header>
          <div>
            <h2>Evaluation history</h2>
            <p>
              Recalculations append a new immutable version. Earlier scores remain available for
              audit.
            </p>
          </div>
        </header>
        <div className="table-wrap">
          <table aria-label="Evaluation history">
            <thead>
              <tr>
                <SortableHeader
                  activeKey={evaluationSort}
                  column="version_number"
                  direction={evaluationDirection}
                  label="Version"
                  onSort={onSortEvaluations}
                />
                <SortableHeader
                  activeKey={evaluationSort}
                  column="evaluated_at"
                  direction={evaluationDirection}
                  label="Evaluated"
                  onSort={onSortEvaluations}
                />
                <SortableHeader
                  activeKey={evaluationSort}
                  column="decision"
                  direction={evaluationDirection}
                  label="Decision"
                  onSort={onSortEvaluations}
                />
                <th scope="col">Fit</th>
                <th scope="col">Comp.</th>
                <th scope="col">Eligibility</th>
                <th scope="col">Evidence</th>
                <th scope="col">Opportunity</th>
                <SortableHeader
                  activeKey={evaluationSort}
                  column="overall_score"
                  direction={evaluationDirection}
                  label="Overall"
                  onSort={onSortEvaluations}
                />
                <th scope="col">Report</th>
              </tr>
            </thead>
            <tbody>
              {evaluationRows.map((item) => (
                <tr key={item.id}>
                  <th scope="row">
                    v{item.version_number}
                    {item.is_current && <small> current</small>}
                  </th>
                  <td>{new Date(item.evaluated_at).toLocaleString()}</td>
                  <td>
                    <StatusBadge value={item.decision} />
                    <small>{item.rationale}</small>
                  </td>
                  <td>{item.fit_score ?? "-"}</td>
                  <td>
                    {item.compensation_score ?? "-"}
                    {item.compensation_standing_source === "extracted" && (
                      <small
                        className="score-cell__pill"
                        title="Extracted from the posting; no comparable band, so this score is a placeholder."
                      >
                        {" shown"}
                      </small>
                    )}
                  </td>
                  <td>{item.eligibility_score ?? "-"}</td>
                  <td>{item.evidence_score ?? "-"}</td>
                  <td>{item.opportunity_score ?? "-"}</td>
                  <td>{item.overall_score ?? "-"}</td>
                  <td>
                    {item.fit_report_artifact_id ? (
                      <a
                        href={api.artifactUrl(item.fit_report_artifact_id, workspaceId, "inline")}
                        rel="noreferrer"
                        target="_blank"
                      >
                        Open
                      </a>
                    ) : (
                      "-"
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <HistoryPagination
          count={data.evaluation_count}
          label="Evaluation history"
          offset={evaluationOffset}
          onPageChange={onEvaluationPageChange}
          pageSize={pageSize}
        />
      </section>
    </>
  );
}
