import type { PriceMove, PriceRefresh } from "../../api/client/types";
import { useRefreshPrices } from "../../api/hooks";
import { ConfirmedActionButton } from "../../components/ConfirmedActionButton";
import { formatUsd } from "../../components/text";

const rate = (value: number | null) => formatUsd(value, { places: 2, unknown: "not published" });

const factor = (value: number) => `${value}x`;

const threshold = (value: number | null) =>
  value === null ? "no long-context tier" : `${value} tokens`;

const HELD_IN_FORCE = "The prices already in force are unchanged; none was lost.";

type RateMove = { label: string; before: string; after: string };

// Every rate the application bills from, so a move in a cache, search, or
// long-context rate is as visible as one in the token rates.
function movedRates(move: PriceMove): RateMove[] {
  const moved: RateMove[] = [];
  function add<T>(label: string, before: T, after: T, format: (value: T) => string) {
    if (before !== after) moved.push({ label, before: format(before), after: format(after) });
  }
  add(
    "Input per million tokens",
    move.previous_input_usd_per_million,
    move.input_usd_per_million,
    rate,
  );
  add(
    "Output per million tokens",
    move.previous_output_usd_per_million,
    move.output_usd_per_million,
    rate,
  );
  add(
    "Cache read per million tokens",
    move.previous_cache_read_usd_per_million,
    move.cache_read_usd_per_million,
    rate,
  );
  add(
    "Cache write per million tokens",
    move.previous_cache_write_usd_per_million,
    move.cache_write_usd_per_million,
    rate,
  );
  add(
    "Hosted search per thousand calls",
    move.previous_search_usd_per_thousand_calls,
    move.search_usd_per_thousand_calls,
    rate,
  );
  add(
    "Long-context threshold",
    move.previous_long_context_threshold,
    move.long_context_threshold,
    threshold,
  );
  add(
    "Long-context input multiplier",
    move.previous_long_context_input_multiplier,
    move.long_context_input_multiplier,
    factor,
  );
  add(
    "Long-context output multiplier",
    move.previous_long_context_output_multiplier,
    move.long_context_output_multiplier,
    factor,
  );
  return moved;
}

function PriceMoves({ moves }: { moves: readonly PriceMove[] }) {
  return (
    <div className="table-scroll">
      <table aria-label="Prices that moved">
        <thead>
          <tr>
            <th scope="col">Model</th>
            <th scope="col">Provider</th>
            <th scope="col">Rate</th>
            <th scope="col">Change</th>
          </tr>
        </thead>
        <tbody>
          {moves.flatMap((move) =>
            movedRates(move).map((moved) => (
              <tr key={`${move.provider}:${move.model}:${moved.label}`}>
                <th scope="row">{move.model}</th>
                <td>{move.provider}</td>
                <td>{moved.label}</td>
                <td>{`was ${moved.before}, now ${moved.after}`}</td>
              </tr>
            )),
          )}
        </tbody>
      </table>
    </div>
  );
}

function PriceRefreshResult({ report }: { report: PriceRefresh }) {
  const changed = report.changed ?? [];
  const unchanged = report.unchanged ?? [];
  const dropped = report.dropped ?? [];
  if (!report.available) {
    return (
      <>
        <p className="field-note" role="status">
          {report.unavailable_reason ?? "The published price data set no price."}
        </p>
        <p className="field-note">
          {HELD_IN_FORCE} Price data {report.previous_snapshot} stays in force.
        </p>
      </>
    );
  }
  return (
    <>
      <p role="status">
        Price data {report.snapshot} is now in force.{" "}
        {changed.length === 0
          ? "No shipped price moved."
          : `${changed.length} shipped ${changed.length === 1 ? "price" : "prices"} moved.`}
        {unchanged.length > 0 && ` ${unchanged.length} stayed the same.`}
      </p>
      <p className="field-note">
        {report.previous_snapshot === report.snapshot
          ? `Read from ${report.source}.`
          : `Read from ${report.source}, replacing ${report.previous_snapshot}.`}
      </p>
      {changed.length > 0 && <PriceMoves moves={changed} />}
      {dropped.length > 0 && (
        <p className="field-note">
          The refreshed data prices{" "}
          {dropped.length === 1 ? "one model" : `${dropped.length} models`} no longer:{" "}
          {dropped.join(", ")}. Each keeps the rate it already had.
        </p>
      )}
    </>
  );
}

/** Fetch the published price data on request and report what it moved. */
export function PriceRefreshControl() {
  const refresh = useRefreshPrices();
  return (
    <>
      <p className="field-note">
        Prices are shipped with ClearScout. Refreshing re-derives them from the published price
        data, which changes what later runs are priced at and leaves recorded costs as they were. A
        provider answers a request for a model with a dated snapshot of it, and that call is priced
        at the model&apos;s own rate.
      </p>
      <div className="button-row">
        <ConfirmedActionButton
          className="button button--quiet"
          disabled={refresh.isPending}
          onConfirm={() => refresh.mutate()}
          options={{
            title: "Refresh prices from the published data?",
            description:
              "Reads the published price data and re-derives the shipped price table. This is " +
              "a free read; it runs no model and spends no money. If the data cannot be " +
              "reached, the prices already in force stay in force.",
            confirmLabel: "Refresh prices",
          }}
        >
          {refresh.isPending ? "Reading the published prices" : "Refresh prices from the catalog"}
        </ConfirmedActionButton>
      </div>
      {refresh.isError && (
        <>
          <p className="field-error" role="alert">
            {`The published prices could not be read. ${HELD_IN_FORCE}`}
          </p>
          <p className="field-note">{refresh.error.message}</p>
        </>
      )}
      {refresh.data && <PriceRefreshResult report={refresh.data} />}
    </>
  );
}
