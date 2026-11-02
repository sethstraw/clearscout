/* eslint-disable react-refresh/only-export-components -- the funnel owns the vocabulary its ledger reuses */
import { safeHttpUrl } from "../../api/safeUrl";
import type { components } from "../../api/schema";
import { Link } from "react-router-dom";

/** Which discovery task produced the rows, which decides the denominator wording. */
export type DiscoveryKind = "board_scan" | "guided_campaign";

const STAGED_REASON = "staged";

type Outcome = {
  detail: string;
  employer: string;
  kind: string;
  location: string;
  reason: string;
  screening_findings?: Array<{ code: string; dimension: string; subject: string }>;
  source: string;
  stages: string[];
  title: string;
  url: string;
};

type Audit = {
  counts: Record<string, number>;
  omitted_outcomes: number;
  omission_reason: string;
  outcomes: Outcome[];
  total_outcomes: number;
  reason_counts?: Record<string, number>;
};

// The search setting that produces a specialist-IC review finding, and the
// finding code that names it. Both consumers reach it through the link component.
const SPECIALIST_IC_REVIEW = {
  code: "title_specialist_ic_review",
  label: "Edit specialist IC review settings",
  to: "/settings?tab=search",
} as const;

// The reasons a candidate never reached staging; labels name the destination,
// not the internal code, so the always-visible summary can say where it went.
const REASON_DESTINATION_LABELS: Partial<Record<components["schemas"]["FunnelReason"], string>> = {
  already_known: "already known",
  insufficient_title_evidence: "screened on title evidence",
  seniority_outside_band: "screened on seniority",
  location_outside_saved_places: "screened on location",
  hard_constraint_excluded: "excluded by hard constraints",
  no_new_openings: "source reads with no new openings",
  fetch_failed: "failed to read",
};

/** Name where a funnel reason sent the candidate, in the summary's vocabulary. */
export function reasonDestinationLabel(reason: string): string {
  return (
    REASON_DESTINATION_LABELS[reason as components["schemas"]["FunnelReason"]] ??
    reason.replaceAll("_", " ")
  );
}

/** Offer the setting that screened a candidate, where that is the rule that stopped it. */
export function SpecialistIcReviewLink({ findings }: { findings?: { code: string }[] | null }) {
  if (!findings?.some((finding) => finding.code === SPECIALIST_IC_REVIEW.code)) return null;
  return (
    <p className="field-note">
      <Link to={SPECIALIST_IC_REVIEW.to}>{SPECIALIST_IC_REVIEW.label}</Link>
    </p>
  );
}

/** Render the typed discovery funnel shared by free scans and guided search. */
export function DiscoveryFunnelAudit({
  value,
  discoveryKind = "board_scan",
}: {
  value: unknown;
  discoveryKind?: DiscoveryKind;
}) {
  if (!value || typeof value !== "object") return null;
  const audit = value as Partial<Audit>;
  if (!Array.isArray(audit.outcomes) || !audit.counts) return null;
  // Stored progress from an earlier build may carry neither total, so the rows
  // themselves are the last denominator.
  const totalCandidates = audit.total_outcomes ?? audit.counts.proposed ?? audit.outcomes.length;
  const campaign = discoveryKind === "guided_campaign";
  if (campaign && totalCandidates === 0 && audit.outcomes.length === 0) {
    return <p className="field-note">This campaign read no candidates.</p>;
  }
  // A guided campaign records a funnel row only for a URL it read, so the total
  // is the read count and not everything the campaign captured.
  const candidates = campaign
    ? `${totalCandidates} candidates the campaign read`
    : `${totalCandidates} candidates`;
  const stagedCount = audit.counts.staged ?? audit.reason_counts?.[STAGED_REASON] ?? 0;
  // Staged reads carry the "staged" reason, which the staged count already states.
  const stoppedReasons = Object.entries(audit.reason_counts ?? {}).filter(
    ([reason]) => reason !== STAGED_REASON,
  );
  const stopped = stoppedReasons
    .map(([reason, count]) => `${count} ${reasonDestinationLabel(reason)}`)
    .join(", ");
  const summary = audit.reason_counts
    ? `Discovery funnel: ${candidates} - ${stagedCount} staged${stopped ? `, ${stopped}` : ""}`
    : `Discovery funnel: ${candidates}, ${stagedCount} staged, ${audit.counts.rejected ?? 0} rejected`;
  return (
    <details className="discovery-funnel">
      <summary>{summary}</summary>
      {!campaign && (audit.omitted_outcomes ?? 0) > 0 && (
        <p role="note">
          {audit.omitted_outcomes} outcome{audit.omitted_outcomes === 1 ? " was" : "s were"}{" "}
          omitted. {audit.omission_reason}
        </p>
      )}
      {audit.reason_counts && (
        <div className="table-wrap discovery-funnel__reasons">
          <table aria-label="All-candidate reasons" className="data-table">
            <thead>
              <tr>
                <th scope="col">Reason</th>
                <th scope="col">Candidates</th>
              </tr>
            </thead>
            <tbody>
              {Object.entries(audit.reason_counts).map(([reason, count]) => (
                <tr key={reason}>
                  <th scope="row">{reasonDestinationLabel(reason)}</th>
                  <td>{count}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {/* A campaign's candidate ledger carries every row, so only its reason counts render here. */}
      {!campaign && (
        <div className="table-wrap">
          <table aria-label="Discovery funnel outcomes" className="data-table">
            <thead>
              <tr>
                <th scope="col">Candidate</th>
                <th scope="col">Kind</th>
                <th scope="col">Path</th>
                <th scope="col">Outcome</th>
                <th scope="col">Detail</th>
              </tr>
            </thead>
            <tbody>
              {audit.outcomes.map((outcome, index) => {
                const url = safeHttpUrl(outcome.url);
                const label =
                  outcome.title || outcome.employer || outcome.source || `Candidate ${index + 1}`;
                return (
                  <tr key={`${outcome.kind}-${outcome.url}-${index}`}>
                    <th scope="row">
                      {url ? (
                        <a href={url} rel="noreferrer" target="_blank">
                          {label}
                        </a>
                      ) : (
                        label
                      )}
                    </th>
                    <td>{outcome.kind.replaceAll("_", " ")}</td>
                    <td>
                      {outcome.stages.map((stage) => stage.replaceAll("_", " ")).join(" -> ")}
                    </td>
                    <td>{outcome.reason.replaceAll("_", " ")}</td>
                    <td>
                      {outcome.screening_findings?.length
                        ? outcome.screening_findings
                            .map((finding) =>
                              [
                                finding.dimension.replaceAll("_", " "),
                                finding.code.replaceAll("_", " "),
                                finding.subject,
                              ]
                                .filter(Boolean)
                                .join(": "),
                            )
                            .join("; ")
                        : outcome.detail || "-"}
                      <SpecialistIcReviewLink findings={outcome.screening_findings} />
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </details>
  );
}
