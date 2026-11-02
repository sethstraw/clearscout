import { useMemo } from "react";

import type { SourceScanHistory } from "../../api/client/types";
import { useScreenedOutTitles, useSourceScans } from "../../api/hooks";
import { screenedCandidatesHref } from "./pendingDiscoveryFilterModel";
import { needsReviewOptions } from "./screenedOutTitlesModel";

type ScanRecord = SourceScanHistory["items"][number];

const LATEST_SCANS_LIMIT = 250;

function latestScreenedOutScans(scans: readonly ScanRecord[] | undefined): ScanRecord[] {
  return (scans ?? [])
    .filter((scan) => scan.task_id && scan.screened_out_count > 0)
    .sort(
      (left, right) =>
        right.screened_out_count - left.screened_out_count ||
        left.source_name.localeCompare(right.source_name),
    );
}

/** Tabulate listings each source's latest scan kept out of Pending. */
export function ScreenedOutSummary({ workspaceId }: { workspaceId: string }) {
  const scanHistory = useSourceScans(workspaceId, {
    offset: 0,
    limit: LATEST_SCANS_LIMIT,
    latestOnly: true,
  });
  const titles = useScreenedOutTitles(workspaceId, needsReviewOptions());
  const scans = useMemo(() => latestScreenedOutScans(scanHistory.data?.items), [scanHistory.data]);
  if (scans.length === 0) return null;
  const total = scans.reduce((sum, scan) => sum + scan.screened_out_count, 0);
  const scannedSources = scanHistory.data?.total ?? 0;
  const loadedSources = scanHistory.data?.items.length ?? 0;
  return (
    <details className="field-note">
      <summary>
        Screened out in latest scans ({total} {total === 1 ? "listing" : "listings"} from{" "}
        {scans.length} {scans.length === 1 ? "source" : "sources"})
        {scannedSources > loadedSources
          ? `; covers the first ${loadedSources} of ${scannedSources} scanned sources`
          : ""}
        {titles.data?.needs_review_count
          ? `; ${titles.data.needs_review_count} new or changed ${titles.data.needs_review_count === 1 ? "title" : "titles"} to review`
          : ""}
      </summary>
      <div className="table-wrap">
        <table aria-label="Screened-out listings by source" className="data-table">
          <thead>
            <tr>
              <th scope="col">Source</th>
              <th className="screened-out-summary__count" scope="col">
                Screened out
              </th>
              <th scope="col">Review</th>
            </tr>
          </thead>
          <tbody>
            {scans.map((scan) => (
              <tr key={scan.id}>
                <th scope="row">{scan.source_name}</th>
                <td className="screened-out-summary__count">{scan.screened_out_count}</td>
                <td>
                  <a href={screenedCandidatesHref(scan.task_id ?? "", scan.source_name)}>
                    Review screened candidates
                  </a>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </details>
  );
}
