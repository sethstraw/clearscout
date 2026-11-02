import { useRef } from "react";

import type { SpendSummary } from "../../api/client/types";
import { useWorkspaceSpend } from "../../api/hooks";
import { ErrorState, LoadingState } from "../../components/States";
import { formatUsd } from "../../components/text";
import { OpenAiActualSpendPanel } from "./OpenAiActualSpendPanel";

// The qualifier sits in the value, so a screen reader reads it with the Total
// instead of meeting the explanation later as unrelated prose.
function totalValue(summary: SpendSummary): string {
  if (summary.total_usd != null) return formatUsd(summary.total_usd);
  if (summary.unpriced_calls >= summary.call_count) {
    return "Unknown, no call here has a configured price";
  }
  const verb = summary.unpriced_calls === 1 ? "has" : "have";
  return `Unknown, ${summary.unpriced_calls} of ${summary.call_count} calls ${verb} no configured price`;
}

type SpendGroup = SpendSummary["by_stage"][number];

function SpendGrouping({
  ariaLabel,
  nameHeading,
  rows,
}: {
  ariaLabel: string;
  nameHeading: string;
  rows: readonly SpendGroup[];
}) {
  return (
    <div className="table-scroll">
      <table aria-label={ariaLabel} className="spend-table">
        <thead>
          <tr>
            <th scope="col">{nameHeading}</th>
            <th scope="col">Calls</th>
            <th scope="col">Unpriced</th>
            <th scope="col">Cost</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((group) => (
            <tr key={group.name}>
              <th scope="row">{group.name}</th>
              <td>{group.calls}</td>
              <td>{group.unpriced_calls}</td>
              <td>{formatUsd(group.amount_usd)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function SpendReport({ workspaceId }: { workspaceId: string }) {
  const spend = useWorkspaceSpend(workspaceId);
  const recentCalls = useRef<HTMLDivElement>(null);
  const scrollRecentCalls = (direction: -1 | 1) => {
    const container = recentCalls.current;
    if (!container) return;
    container.scrollBy({ left: direction * container.clientWidth * 0.8, behavior: "smooth" });
  };
  return (
    <>
      <article className="panel">
        <header>
          <div>
            <h2>Model spend</h2>
            <p>
              Recorded provider costs. Runs made before cost tracking existed are absent rather than
              counted as zero.
            </p>
          </div>
        </header>
        {spend.isLoading && <LoadingState label="Reading recorded costs" />}
        {spend.isError && <ErrorState error={spend.error} />}
        {spend.data &&
          (spend.data.call_count === 0 ? (
            <p className="field-note">
              No model calls are recorded in this workspace, so nothing has been spent through it.
            </p>
          ) : (
            <>
              <dl className="settings-list">
                <div>
                  <dt>Total</dt>
                  <dd>{totalValue(spend.data)}</dd>
                </div>
                <div>
                  <dt>Model calls</dt>
                  <dd>{spend.data.call_count}</dd>
                </div>
              </dl>
              {spend.data.total_usd == null && (
                <p className="field-note">
                  Add prices for those models in Settings to see a total. A missing price is not a
                  zero price.
                </p>
              )}
              <SpendGrouping
                ariaLabel="Recorded model spend by stage"
                nameHeading="Stage"
                rows={spend.data.by_stage}
              />
              <h3>Spend by model</h3>
              <SpendGrouping
                ariaLabel="Recorded model spend by model"
                nameHeading="Model served"
                rows={spend.data.by_model}
              />
              <h3>Recent model calls</h3>
              {spend.data.recent.length === 0 ? (
                <p className="field-note">No recorded model calls yet.</p>
              ) : (
                <>
                  <div className="button-row">
                    <button
                      className="button button--quiet"
                      onClick={() => scrollRecentCalls(-1)}
                      type="button"
                    >
                      Earlier fields
                    </button>
                    <button
                      className="button button--quiet"
                      onClick={() => scrollRecentCalls(1)}
                      type="button"
                    >
                      More fields
                    </button>
                  </div>
                  <div aria-label="Recent model calls" className="table-scroll" ref={recentCalls}>
                    <p className="field-note" id="recent-model-calls-scroll-note">
                      Scroll horizontally to inspect every recorded field.
                    </p>
                    <table aria-label="Recent model calls">
                      <thead>
                        <tr>
                          <th scope="col">When</th>
                          <th scope="col">Stage</th>
                          <th scope="col">Provider</th>
                          <th scope="col">Requested model</th>
                          <th scope="col">Model served</th>
                          <th scope="col">Input</th>
                          <th scope="col">Output</th>
                          <th scope="col">Cache read</th>
                          <th scope="col">Cache write</th>
                          <th scope="col">Reasoning</th>
                          <th scope="col">Duration</th>
                          <th scope="col">Completion</th>
                          <th scope="col">Price source</th>
                          <th scope="col">Price data</th>
                          <th scope="col">Cached</th>
                          <th scope="col">Cost</th>
                        </tr>
                      </thead>
                      <tbody>
                        {spend.data.recent.map((call) => (
                          <tr key={call.id}>
                            <td>{new Date(call.created_at).toLocaleString()}</td>
                            <td>{call.stage}</td>
                            <td>{call.provider}</td>
                            <td>{call.requested_model}</td>
                            <td>{call.served_model ?? call.model}</td>
                            <td>{call.input_tokens}</td>
                            <td>{call.output_tokens}</td>
                            <td>{call.cache_read_tokens}</td>
                            <td>{call.cache_write_tokens}</td>
                            <td>{call.reasoning_tokens}</td>
                            <td>
                              {call.duration_ms === null
                                ? "Not recorded"
                                : `${call.duration_ms} ms`}
                            </td>
                            <td>{call.completion_reason ?? "Not recorded"}</td>
                            <td>{call.pricing_source ?? "Not recorded"}</td>
                            <td>{call.price_snapshot ?? "Not recorded"}</td>
                            <td>{call.cache_hit ? "Yes" : "No"}</td>
                            <td>{formatUsd(call.amount_usd)}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </>
              )}
            </>
          ))}
      </article>
      <OpenAiActualSpendPanel />
    </>
  );
}
