import type { EvaluationPreflight } from "../api/client";
import { formatUsd } from "./text";

export function EvaluationCostDetails({ estimate }: { estimate: EvaluationPreflight }) {
  return (
    <section aria-label="Evaluation cost estimate">
      <p>Estimate prepared: {estimate.generated_at}.</p>
      <p>
        Estimated total at configured rates:{" "}
        {formatUsd(estimate.expected_cost_usd, { unknown: "Unavailable" })} USD.
      </p>
      {estimate.expected_unavailable_reason && <p>{estimate.expected_unavailable_reason}</p>}
      <p>
        Modeled total range for one attempt:{" "}
        {formatUsd(estimate.range_low_usd, { unknown: "Unavailable" })} to{" "}
        {formatUsd(estimate.range_high_usd, { unknown: "Unavailable" })} USD.
      </p>
      <p>
        Actual charges can exceed this range. Expected costs use the requested service tier; the
        range allows standard-tier fallback.
      </p>
      {estimate.range_unavailable_reason && <p>{estimate.range_unavailable_reason}</p>}
      <p>{estimate.basis}</p>
      <ul>
        {estimate.limitations.map((reason) => (
          <li key={reason}>{reason}</li>
        ))}
      </ul>
      {estimate.items.map((item) => (
        <details key={`${item.target_kind}:${item.target_id}`}>
          <summary>
            {item.target_kind === "job" ? "Tracked job" : "Discovery"} {item.target_id}
          </summary>
          <p>Expected: {formatUsd(item.expected_cost_usd, { unknown: "Unavailable" })} USD.</p>
          <p>
            Modeled range: {formatUsd(item.range_low_usd, { unknown: "Unavailable" })} to{" "}
            {formatUsd(item.range_high_usd, { unknown: "Unavailable" })} USD.
          </p>
          {item.expected_unavailable_reason && <p>{item.expected_unavailable_reason}</p>}
          {item.range_unavailable_reason && <p>{item.range_unavailable_reason}</p>}
          {item.current_skip_reason && <p>{item.current_skip_reason}</p>}
          <p>Retained details read: {item.retained_details_read_at ?? "Not recorded"}.</p>
          {(item.stages ?? []).map((stage) => (
            <div key={stage.stage}>
              <p>
                {stage.stage}: {stage.provider} / {stage.model}, requested{" "}
                {stage.requested_service_tier}.
              </p>
              {stage.expected_unavailable_reason && <p>{stage.expected_unavailable_reason}</p>}
              {stage.range_unavailable_reason && <p>{stage.range_unavailable_reason}</p>}
              <p>
                Historical output samples: {stage.historical_output_samples}. Output-token budget:{" "}
                {stage.output_token_budget}. Estimated input: {stage.input_token_estimate}; modeled
                input high: {stage.input_token_range_high}.
              </p>
              {stage.price && (
                <p>
                  Standard rates per million tokens: input {formatUsd(stage.price.input)} USD;
                  output {formatUsd(stage.price.output)} USD.
                </p>
              )}
              {stage.provenance ? (
                <>
                  <p>
                    Price source: {stage.provenance.source_kind.replaceAll("_", " ")} (
                    {stage.provenance.snapshot_label}).
                  </p>
                  <p>
                    Price table date: {stage.provenance.table_date ?? "None"}.{" "}
                    {stage.provenance.table_date_unavailable_reason}
                  </p>
                  <p>Price fetched: {stage.provenance.fetched_at ?? "Not recorded"}.</p>
                  {stage.provenance.source_kind === "configured_override" && (
                    <p>
                      User-configured rates override:{" "}
                      {stage.provenance.overridden_fields.join(", ")}.{" "}
                      {stage.provenance.base_source &&
                        `Remaining rates use ${stage.provenance.base_source.snapshot_label}.`}
                    </p>
                  )}
                  {stage.provenance.source_urls.map((url) => (
                    <p key={url}>
                      <a href={url} rel="noreferrer" target="_blank">
                        Published {stage.provider} pricing
                      </a>
                    </p>
                  ))}
                </>
              ) : (
                <p>No price source is available for this model.</p>
              )}
            </div>
          ))}
        </details>
      ))}
    </section>
  );
}
