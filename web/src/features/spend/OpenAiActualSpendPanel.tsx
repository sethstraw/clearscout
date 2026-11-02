import type { OpenAiActualSpend } from "../../api/client/types";
import { useReconcileOpenAiSpend } from "../../api/hooks";
import { ConfirmedActionButton } from "../../components/ConfirmedActionButton";
import { formatUsd } from "../../components/text";

const WINDOW_DAYS = 7;

// A missing figure here means OpenAI reported none, not that a price is absent
// from settings.
const dollars = (amount: number | null | undefined) =>
  formatUsd(amount, { places: 2, unknown: "Unavailable" });

/** Pull OpenAI's actual billed cost and show it beside the recorded ledger. */
export function OpenAiActualSpendPanel() {
  const reconcile = useReconcileOpenAiSpend();
  const runPull = () => {
    const end = Math.floor(Date.now() / 1000);
    reconcile.mutate({ start_time: end - WINDOW_DAYS * 24 * 60 * 60, end_time: end });
  };
  return (
    <article className="panel">
      <header>
        <div>
          <h2>Actual spend from OpenAI</h2>
          <p>
            OpenAI&apos;s Organization API reports what it actually billed. This compares that with
            the cost TailorCV recorded over the same {WINDOW_DAYS}-day window, so charges that
            cannot be estimated up front, such as retrieved web-search content, become visible.
          </p>
        </div>
      </header>
      <div className="panel-body">
        <p className="field-note">
          This is an organization-wide cross-check, not per task or per workspace. OpenAI reports
          the whole organization the admin key belongs to, while the recorded figure counts only the
          OpenAI calls TailorCV priced in this database.
        </p>
        <div className="button-row">
          <ConfirmedActionButton
            className="button button--quiet"
            disabled={reconcile.isPending}
            onConfirm={runPull}
            options={{
              title: "Refresh actual spend from OpenAI?",
              description:
                "Reads OpenAI's organization billing for the last 7 days. This is a free billing read; it runs no model and spends no money.",
              confirmLabel: "Refresh actual spend",
            }}
          >
            {reconcile.isPending ? "Reading OpenAI billing" : "Refresh actual spend from OpenAI"}
          </ConfirmedActionButton>
        </div>
        {reconcile.isError && <p className="field-error">{reconcile.error.message}</p>}
        {reconcile.data && <ReconciliationResult report={reconcile.data} />}
      </div>
    </article>
  );
}

function ReconciliationResult({ report }: { report: OpenAiActualSpend }) {
  if (!report.available) {
    return (
      <p className="field-note" role="status">
        {report.unavailable_reason ?? "Actual spend from OpenAI is unavailable."}
      </p>
    );
  }
  const windowLabel = `${new Date(report.start_time * 1000).toLocaleDateString()} - ${new Date(
    report.end_time * 1000,
  ).toLocaleDateString()}`;
  return (
    <>
      <dl className="settings-list">
        <div>
          <dt>Window</dt>
          <dd>{windowLabel}</dd>
        </div>
        <div>
          <dt>OpenAI actual (organization-wide)</dt>
          <dd>{dollars(report.openai_actual_usd)}</dd>
        </div>
        <div>
          <dt>Recorded by TailorCV</dt>
          <dd>
            {report.recorded_usd == null
              ? "Withheld - a call had no configured price"
              : dollars(report.recorded_usd)}
          </dd>
        </div>
        <div>
          <dt>Organization total minus TailorCV recorded cost</dt>
          <dd>
            {report.drift_usd == null && report.unpriced_call_count > 0
              ? "Withheld - a call had no configured price"
              : dollars(report.drift_usd)}
          </dd>
        </div>
        <div>
          <dt>Recorded OpenAI calls</dt>
          <dd>{report.recorded_call_count}</dd>
        </div>
        {report.unpriced_call_count > 0 && (
          <div>
            <dt>Without a configured price</dt>
            <dd>{report.unpriced_call_count}</dd>
          </div>
        )}
      </dl>
      <p className="field-note">{report.scope_note}</p>
      <p className="field-note">
        Amounts are rounded to cents. The difference can include other applications and projects,
        billing delays, and price differences; it does not isolate TailorCV accounting errors.
      </p>
    </>
  );
}
