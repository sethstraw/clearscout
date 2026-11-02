import type { RunCostHistory } from "../../api/client";
import { useRunCostHistory } from "../../api/hooks";
import { TASK_KINDS } from "../../api/taskNames";
import { ErrorState, LoadingState } from "../../components/States";
import { formatUsd } from "../../components/text";
import { PriceRefreshControl } from "./PriceRefreshControl";

// A missing figure here means no run has been measured yet, not that a price is
// absent from settings.
const asMoney = (value: string | number | null | undefined) =>
  formatUsd(value, { unknown: "not yet measured" });

/** What each kind of paid run has actually cost in this workspace. */
export function RunCostHistoryPanel({ workspaceId }: { workspaceId: string }) {
  const history = useRunCostHistory(workspaceId);
  const rows: RunCostHistory[] = history.data ?? [];
  return (
    <article className="panel">
      <header>
        <div>
          <h2>What runs have cost</h2>
          <p>Estimated from recorded usage and configured prices. Invoices may differ.</p>
        </div>
      </header>
      {/* A failed history read still leaves the price refresh reachable, because the
          prices in force are not what the history read failed to say. */}
      {history.isLoading && <LoadingState label="Reading what your runs have cost" />}
      {history.isError && <ErrorState error={history.error} />}
      {history.data && (
        <div className="panel-body">
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th scope="col">Kind of run</th>
                  <th scope="col">Runs measured</th>
                  <th scope="col">Unit</th>
                  <th scope="col">Typical per unit</th>
                  <th scope="col">Range per unit</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => (
                  <tr key={row.task_type}>
                    <th scope="row">{TASK_KINDS[row.task_type].name}</th>
                    <td>
                      {row.measured_runs}
                      {row.unpriced_runs > 0 && (
                        <small> ({row.unpriced_runs} unpriced, so not counted)</small>
                      )}
                      {row.replayed_runs > 0 && (
                        <small> ({row.replayed_runs} replayed, so not counted)</small>
                      )}
                    </td>
                    <td>{row.unit}</td>
                    <td>{asMoney(row.per_unit_median)}</td>
                    <td>
                      {row.per_unit_median === null
                        ? "-"
                        : `${asMoney(row.per_unit_low)} to ${asMoney(row.per_unit_high)}`}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
      <div className="panel-body">
        <PriceRefreshControl />
      </div>
    </article>
  );
}
